import argparse
import contextlib
import io
import json
import plistlib
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mobile_agent import cli_parser, harness_api, plan_gate, result_blocks
from mobile_agent.api_errors import APIError
from mobile_agent.netxray import attribution, capture, cli, job, register, store, target
from mobile_agent.server import make_handler
from mobile_agent.tests import netxray_synthetic as synthetic
from mobile_agent.tests.seam_support import request
from mobile_agent.tests.test_netxray import APPS, OTHER_UDID, SESSION, UDID
from mobile_agent.tests.test_server_engine import Base


class FakeProcess:
    def __init__(self, command, out, code=None, stderr=b""):
        self.command, self.signals, self.code = command, [], code
        self.returncode = code
        self.stderr = io.BytesIO(stderr)
        self.killed = False
        Path(command[command.index("--out") + 1]).write_bytes(b"pcapng bytes")

    def poll(self):
        return self.returncode

    def send_signal(self, number):
        self.signals.append(number)
        if number == signal.SIGTERM and not getattr(self, "stubborn", False):
            self.returncode = 0

    def wait(self, timeout=None):
        if self.returncode is None:
            if timeout is not None:
                raise subprocess.TimeoutExpired(self.command, timeout)
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -signal.SIGKILL


class RecordingTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.out = self.folder / "capture.pcapng"
        self.started = []

    def popen(self, command, **kwargs):
        process = FakeProcess(command, self.out, **getattr(self, "process", {}))
        self.started.append(process)
        return process

    def record(self, network=False, seconds=60, out=None, udid=UDID):
        return capture.Recording(udid, seconds, out or self.out, network=network, locate=lambda: "/app/mobster-pcap",
                                 popen=self.popen)

    def test_the_tool_gets_one_udid_and_the_network_flag_only_over_wifi(self):
        self.record()
        self.assertEqual(self.started[0].command, ["/app/mobster-pcap", "--udid", UDID, "--seconds", "65", "--max-mb",
                                                   "200", "--out", str(self.out)])
        self.record(network=True, out=self.folder / "w.pcapng")
        command = self.started[1].command
        self.assertEqual(command[command.index("--network") - 2:command.index("--network") + 1],
                         ["--udid", UDID, "--network"])

    def test_stop_asks_politely_then_kills(self):
        recording = self.record()
        recording.stop()
        self.assertEqual(self.started[0].signals, [signal.SIGTERM])
        self.assertFalse(self.started[0].killed)
        recording.stop()
        self.assertEqual(self.started[0].signals, [signal.SIGTERM])
        stubborn = self.record(out=self.folder / "b.pcapng")
        self.started[1].stubborn = True
        stubborn.stop()
        self.assertTrue(self.started[1].killed)

    def test_finish_returns_the_size_of_a_stopped_capture(self):
        recording = self.record()
        recording.stop()
        self.assertEqual(recording.finish(), len(b"pcapng bytes"))

    def test_a_tool_that_fails_says_what_to_do(self):
        for code, words in ((3, "Plug it in"), (4, "tap Trust"), (6, "write"), (8, "broke")):
            with self.subTest(code=code):
                self.out.unlink(missing_ok=True)
                self.process = {"code": code}
                with self.assertRaises(capture.CaptureError) as caught:
                    self.record().finish()
                self.assertIn(words, str(caught.exception))

    def test_a_wifi_refusal_for_an_unencrypted_service_says_so_in_the_tools_own_words(self):
        line = (b"the iPhone offered the packet capture service without encryption, so Mobster won't capture it over "
                b"Wi-Fi: use a cable\n")
        self.process = {"code": 5, "stderr": line}
        with self.assertRaises(capture.CaptureError) as caught:
            self.record(network=True).finish()
        self.assertEqual(str(caught.exception), "The iPhone offered the packet capture service without encryption, "
                                                "so Mobster won't capture it over Wi-Fi: use a cable.")

    def test_a_missing_tool_says_what_to_install(self):
        def missing():
            raise LookupError("libimobiledevice isn't installed. Run `brew install libimobiledevice`.")
        with self.assertRaises(capture.CaptureError) as caught:
            capture.Recording(UDID, 60, self.out, locate=missing, popen=self.popen)
        self.assertIn("brew install libimobiledevice", str(caught.exception))
        self.assertEqual(self.started, [])

    def test_refusals_run_nothing(self):
        self.out.write_bytes(b"old")
        cases = [((UDID, 60, self.out), "never overwrites"), (("not a udid", 60, self.folder / "n.pcapng"), "UDID"),
                 ((UDID, 4, self.folder / "n.pcapng"), "5 to 3600"),
                 ((UDID, 3601, self.folder / "n.pcapng"), "5 to 3600"),
                 ((UDID, 60, self.folder / "n.pcap"), ".pcapng")]
        for args, words in cases:
            with self.subTest(words=words), self.assertRaises(capture.CaptureError) as caught:
                capture.Recording(*args, locate=lambda: "/x", popen=self.popen)
            self.assertIn(words, str(caught.exception))
        self.assertEqual(self.started, [])
        self.assertEqual(self.out.read_bytes(), b"old")

    def test_the_c_tool_refuses_wifi_without_encryption(self):
        from mobile_agent.netxray import tool
        source = Path(tool.SOURCE).read_text(encoding="utf-8")
        self.assertIn("network_only && !service->ssl_enabled", source)
        self.assertIn("return REFUSED;", source.split("network_only && !service->ssl_enabled", 1)[1][:600])


def usb(id_, name=None, state="ready", **more):
    return {"id": id_, "kind": "usb", "name": name or f"Phone {id_[-2:]}", "udid": id_, "state": state, **more}


SIMULATOR = {"id": "sim-1", "kind": "simulator", "name": "iPhone 17 Pro", "udid": "ABCD", "state": "ready"}
WDA = {"id": "wda-1", "kind": "wda", "name": "Remote phone", "state": "ready"}


class TargetTest(unittest.TestCase):
    def pick(self, query=None, records=(), **kwargs):
        return target.pick(query, records=list(records), **kwargs)

    def test_a_phone_is_named_by_id_udid_or_name_and_gives_exactly_one_udid(self):
        records = [usb(UDID, "Test iPhone"), usb(OTHER_UDID, "Spare")]
        for query in (UDID, UDID.lower(), "test iphone"):
            with self.subTest(query=query):
                phone = self.pick(query, records)
                self.assertEqual((phone.udid, phone.name, phone.network), (UDID, "Test iPhone", False))

    def test_no_name_with_one_phone_connected_picks_it(self):
        self.assertEqual(self.pick(None, [usb(UDID), usb(OTHER_UDID, state="disconnected"), SIMULATOR]).udid, UDID)

    def test_no_name_with_two_phones_connected_refuses_and_lists_them(self):
        with self.assertRaises(target.TargetError) as caught:
            self.pick(None, [usb(UDID, "Test iPhone"), usb(OTHER_UDID, "Spare")])
        self.assertIn("2 iPhones are connected", str(caught.exception))
        self.assertIn("Spare", str(caught.exception))
        self.assertIn("--device", str(caught.exception))

    def test_no_phone_at_all_says_to_plug_one_in(self):
        for records in ([], [usb(UDID, state="disconnected")], [SIMULATOR]):
            with self.subTest(records=len(records)), self.assertRaises(target.TargetError) as caught:
                self.pick(None, records)
            self.assertIn("sees no iPhone", str(caught.exception))

    def test_a_name_that_matches_nothing_or_two_phones_is_refused_with_the_list(self):
        with self.assertRaises(target.TargetError) as caught:
            self.pick("Nope", [usb(UDID, "Test iPhone")])
        self.assertIn("Test iPhone", str(caught.exception))
        with self.assertRaises(target.TargetError) as caught:
            self.pick("Twin", [usb(UDID, "Twin"), usb(OTHER_UDID, "Twin")])
        self.assertIn("2 devices are named", str(caught.exception))

    def test_simulators_and_addresses_are_refused(self):
        for record, words in ((SIMULATOR, "is a simulator"), (WDA, "UDID")):
            with self.subTest(record=record["kind"]), self.assertRaises(target.TargetError) as caught:
                self.pick(record["name"], [record])
            self.assertIn(words, str(caught.exception))

    def test_the_transport_follows_the_phone_and_wifi_forces_the_network(self):
        self.assertFalse(self.pick(UDID, [usb(UDID)]).network)
        self.assertTrue(self.pick(UDID, [usb(UDID, state="disconnected")]).network)
        self.assertTrue(self.pick(UDID, [usb(UDID, transport="wifi")]).network)
        self.assertFalse(self.pick(UDID, [usb(UDID, transport="usb")]).network)
        self.assertTrue(self.pick(UDID, [usb(UDID)], wifi=True).network)
        self.assertEqual(self.pick(None, [usb(UDID, state="disconnected")], wifi=True).udid, UDID)

    def test_the_phones_apps_are_read_with_their_executables_over_the_right_connection(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            kind = "system" if "--system" in command else "user"
            row = ({"CFBundleIdentifier": "com.apple.mobilesafari", "CFBundleDisplayName": "Safari",
                    "CFBundleExecutable": "MobileSafari"} if kind == "system" else
                   {"CFBundleIdentifier": "com.example.weather", "CFBundleDisplayName": "Weather",
                    "CFBundleExecutable": "Weather"})
            return subprocess.CompletedProcess(command, 0, stdout=plistlib.dumps([row]), stderr=b"")
        names = target.phone_names(target.Phone("x", "X", UDID, True), tool="/bin/ideviceinstaller", runner=runner)
        self.assertEqual(names.label("MobileSafari"), ("Safari", "app"))
        self.assertEqual(names.label("Weather"), ("Weather", "app"))
        self.assertTrue(all("-n" in command and command[command.index("-u") + 1] == UDID for command in calls))
        cable = target.phone_names(target.Phone("x", "X", UDID, False), tool="/bin/ideviceinstaller", runner=runner)
        self.assertTrue(cable)
        self.assertNotIn("-n", calls[-1])

    def test_an_unreadable_app_list_gives_empty_names(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"ERROR: Could not connect to lockdownd")
        names = target.phone_names(target.Phone("x", "X", UDID, False), tool="/bin/ideviceinstaller", runner=runner)
        self.assertFalse(names)
        self.assertEqual(names.label("Weather"), ("Weather", "process"))


class FakeRecording:
    made = []

    def __init__(self, udid, seconds, out, network=False, fail=None, ends=False):
        self.udid, self.seconds, self.out, self.network, self.fail = udid, seconds, Path(out), network, fail
        self.stopped, self.ends, self.stops = threading.Event(), ends, 0
        with open(self.out, "wb") as stream:
            synthetic.flashlight(stream)
        self.out.chmod(0o600)
        FakeRecording.made.append(self)

    def running(self):
        return not (self.stopped.is_set() or self.ends)

    def stop(self, wait=None):
        self.stops += 1
        self.stopped.set()

    def finish(self):
        if self.fail:
            raise capture.CaptureError(self.fail)
        return self.out.stat().st_size


class JobCase(Base):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        FakeRecording.made = []
        self.runtime_ = self.runtime()
        self.phone = target.Phone(UDID, "Test iPhone", UDID, False)
        self.board = job.Board(self.runtime_, recorder=self.recorder, namer=lambda phone: attribution.Names(APPS),
                               folder=lambda: store.new_folder(base=self.tmp), every=0.02)
        self.addCleanup(self.board.close)
        self.options = {}

    def recorder(self, udid, seconds, out, network=False):
        return FakeRecording(udid, seconds, out, network, **self.options)

    def until(self, check, limit=10.0):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if check():
                return True
            time.sleep(0.01)
        return False


class JobTest(JobCase):
    def test_record_then_live_counts_then_done_with_a_finished_task(self):
        started = self.board.start(self.phone, 60)
        self.assertEqual(started.view()["state"], "recording")
        self.assertTrue(self.until(lambda: started.view()["live"]["totals"]["domains"] == 14 + 1))
        view = started.view()
        self.assertEqual((view["device"], view["seconds"]), ({"id": UDID, "name": "Test iPhone"}, 60))
        self.assertEqual({item["name"] for item in view["live"]["apps"]}, {"Flashlight Pro", "iOS"})
        self.assertEqual(set(view["live"]["companies"][0]), {"name", "category", "categoryLabel", "apps", "domains",
                                                             "bytes"})
        self.assertEqual(set(view["live"]["apps"][0]), {"name", "kind", "process", "domains", "companies", "tracking",
                                                        "bytes"})
        self.assertNotIn("runId", view)
        FakeRecording.made[0].ends = True
        self.assertTrue(self.until(lambda: started.view()["state"] == "done"))
        view = started.view()
        run = self.runtime_.runs[view["runId"]]
        self.assertEqual((run.status, run.origin, run.goal, run.app["id"]),
                         ("completed", "app", "Network X-ray of Test iPhone for 60 seconds", "any"))
        self.assertEqual((run.device_id, run.device_name), (UDID, "Test iPhone"))
        self.assertTrue(harness_api.stays_on_mac(run.extras))
        self.assertEqual(run.summary["answer"],
                         "In 59 s 2 apps contacted 15 domains at 11 companies. 6 of those domains are ads, analytics "
                         "or attribution.")
        self.assertNotIn("blocks_problems", run.summary)
        envelope, problems = result_blocks.validate(run.summary["blocks"])
        self.assertEqual(problems, [])
        tables = [block for block in envelope["blocks"] if block["type"] == "table"]
        self.assertEqual([table["title"] for table in tables], ["Who your apps talked to", "Every domain"])
        for table in tables:
            self.assertEqual([c["type"] for c in table["columns"] if c["label"] == "Company"], ["brand"])
        self.assertEqual(envelope["blocks"][-1]["type"], "markdown")
        self.assertIn("encrypted DNS", envelope["blocks"][-1]["text"])

    def test_the_folder_holds_the_reports_privately_and_the_capture_is_gone(self):
        started = self.board.start(self.phone, 60)
        FakeRecording.made[0].ends = True
        self.assertTrue(self.until(lambda: started.view()["state"] == "done"))
        (folder,) = list(self.tmp.iterdir())
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["netxray.json", "report.html", "report.md"])

    def test_stop_ends_early_and_the_analysis_and_the_task_still_happen(self):
        started = self.board.start(self.phone, 600)
        self.board.stop(started.id)
        self.assertTrue(self.until(lambda: started.view()["state"] == "done"))
        view = started.view()
        self.assertTrue(view["early"])
        self.assertEqual(self.runtime_.runs[view["runId"]].status, "completed")
        self.assertGreaterEqual(FakeRecording.made[0].stops, 1)

    def test_stop_before_the_phone_sent_anything_is_stopped_with_no_task(self):
        self.options = {"fail": "mobster-pcap wrote no capture."}
        started = self.board.start(self.phone, 600)
        self.board.stop(started.id)
        self.assertTrue(self.until(lambda: started.view()["state"] == "stopped"))
        self.assertNotIn("runId", started.view())
        self.assertEqual(self.runtime_.runs, {})

    def test_one_capture_per_phone(self):
        first = self.board.start(self.phone, 60)
        with self.assertRaises(APIError) as caught:
            self.board.start(self.phone, 60)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "xray_busy"))
        other = self.board.start(target.Phone(OTHER_UDID, "Spare", OTHER_UDID, False), 60)
        self.assertNotEqual(first.id, other.id)
        FakeRecording.made[0].ends = True
        self.assertTrue(self.until(lambda: first.view()["state"] == "done"))
        self.board.start(self.phone, 60)

    def test_the_transport_reaches_the_tool(self):
        self.board.start(target.Phone("a", "A", UDID, True), 60)
        self.assertEqual((FakeRecording.made[0].udid, FakeRecording.made[0].network), (UDID, True))

    def test_a_retried_start_returns_the_same_job(self):
        first = self.board.start(self.phone, 60, key="k1")
        self.assertIs(self.board.start(self.phone, 60, key="k1"), first)
        self.assertEqual(len(FakeRecording.made), 1)

    def test_a_tool_that_fails_ends_the_job_failed_with_the_reason_and_leaves_no_capture(self):
        self.options = {"fail": "The iPhone doesn't trust this Mac. Unlock it and tap Trust.", "ends": True}
        started = self.board.start(self.phone, 60)
        self.assertTrue(self.until(lambda: started.view()["state"] == "failed"))
        self.assertEqual(started.view()["error"], {"message": "The iPhone doesn't trust this Mac. Unlock it and tap "
                                                             "Trust.", "code": "xray_capture"})
        (folder,) = list(self.tmp.iterdir())
        self.assertFalse((folder / "capture.pcapng").exists())
        self.assertEqual(self.runtime_.runs, {})

    def test_an_error_while_reading_stops_the_tool_and_deletes_the_capture(self):
        with patch("mobile_agent.netxray.store.conclude", side_effect=RuntimeError("boom with 10.0.0.1")), \
                self.assertLogs("mobster.xray", "WARNING") as logged:
            started = self.board.start(self.phone, 60)
            FakeRecording.made[0].ends = True
            self.assertTrue(self.until(lambda: started.view()["state"] == "failed"))
        self.assertNotIn("10.0.0.1", json.dumps(started.view()) + "".join(logged.output))
        self.assertEqual(started.view()["error"]["code"], "xray_failed")
        self.assertGreaterEqual(FakeRecording.made[0].stops, 1)
        (folder,) = list(self.tmp.iterdir())
        self.assertFalse((folder / "capture.pcapng").exists())

    def test_a_tool_that_cant_start_is_refused_at_once_and_leaves_nothing(self):
        def missing(*args, **kwargs):
            raise capture.CaptureError("libimobiledevice isn't installed. Run `brew install libimobiledevice`.")
        board = job.Board(self.runtime_, recorder=missing, folder=lambda: store.new_folder(base=self.tmp))
        with self.assertRaises(APIError) as caught:
            board.start(self.phone, 60)
        self.assertEqual((caught.exception.status, caught.exception.code), (503, "xray_unavailable"))
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual(board.jobs, {})

    def test_shutdown_stops_every_tool_and_drops_the_jobs(self):
        first = self.board.start(self.phone, 600)
        second = self.board.start(target.Phone(OTHER_UDID, "Spare", OTHER_UDID, False), 600)
        self.board.close()
        self.assertTrue(all(recording.stopped.is_set() for recording in FakeRecording.made))
        self.assertEqual(self.board.jobs, {})
        self.assertEqual((first.view()["state"], second.view()["state"]), ("stopped", "stopped"))
        self.assertEqual(self.runtime_.runs, {})
        with self.assertRaises(APIError):
            self.board.start(self.phone, 60)

    def test_only_the_last_twenty_jobs_are_kept(self):
        self.options = {"ends": True}
        for number in range(job.MAX_JOBS + 6):
            udid = f"00008130-000A1B2C3D4E{number:04d}"
            made = self.board.start(target.Phone(udid, "P", udid, False), 60)
            self.until(lambda made=made: made.view()["state"] in ("done", "failed"))
        self.assertLessEqual(len(self.board.jobs), job.MAX_JOBS)
        self.assertEqual(len(self.board.keys), 0)


class RouteTest(JobCase):
    def call(self, method, path, body=None, session=SESSION):
        status, data, _ = request(make_handler(self.runtime_, app_session=SESSION), method, path, body,
                                  headers={"X-Mobster-App-Session": session} if session else None)
        return status, data

    def setUp(self):
        super().setUp()
        self.runtime_.netxray_board = self.board
        picked = patch("mobile_agent.netxray.target.pick", return_value=self.phone)
        self.pick = picked.start()
        self.addCleanup(picked.stop)

    def test_only_the_mac_app_starts_reads_or_stops_a_capture(self):
        for session in (None, "x" * 40):
            for method, path in (("POST", "/api/xray/network"), ("GET", "/api/xray/network/" + "a" * 12),
                                 ("POST", "/api/xray/network/" + "a" * 12 + "/stop")):
                with self.subTest(method=method, path=path[:20], session=session):
                    status, body = self.call(method, path, {"device": UDID} if path.endswith("network") else None,
                                             session=session)
                    self.assertEqual((status, body["code"]), (403, "app_only"))
        self.assertEqual(FakeRecording.made, [])
        self.pick.assert_not_called()

    def test_start_status_and_stop(self):
        status, body = self.call("POST", "/api/xray/network", {"device": "Test iPhone", "seconds": 30})
        self.assertEqual((status, list(body)), (202, ["jobId"]))
        self.pick.assert_called_once_with("Test iPhone", wifi=False)
        status, view = self.call("GET", "/api/xray/network/" + body["jobId"])
        self.assertEqual(status, 200)
        self.assertEqual(set(view), {"state", "elapsed", "seconds", "device", "live"})
        self.assertEqual((view["state"], view["seconds"]), ("recording", 30))
        self.assertEqual(set(view["live"]), {"apps", "companies", "totals"})
        self.call("POST", "/api/xray/network", {"device": OTHER_UDID, "wifi": True})
        self.pick.assert_called_with(OTHER_UDID, wifi=True)
        status, stopped = self.call("POST", "/api/xray/network/" + body["jobId"] + "/stop", None)
        self.assertEqual(status, 200)
        self.assertTrue(self.until(lambda: self.call("GET", "/api/xray/network/"
                                                     + body["jobId"])[1]["state"] == "done"))
        final = self.call("GET", "/api/xray/network/" + body["jobId"])[1]
        self.assertIn(final["runId"], self.runtime_.runs)

    def test_bad_requests_are_refused_before_anything_runs(self):
        for body in ({}, {"device": ""}, {"device": 5}, {"device": UDID, "seconds": 9},
                     {"device": UDID, "seconds": 601},
                     {"device": UDID, "seconds": True}, {"device": UDID, "seconds": "60"}, {"device": UDID, "wifi": 1},
                     {"device": UDID, "key": ""}):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "/api/xray/network", body)[0], 400)
        self.assertEqual(FakeRecording.made, [])

    def test_a_phone_that_cant_be_captured_is_a_400_with_the_sentence(self):
        self.pick.side_effect = target.TargetError("“iPhone 17 Pro” is a simulator. Network X-ray reads a real "
                                                   "iPhone's traffic: name an iPhone.")
        status, body = self.call("POST", "/api/xray/network", {"device": "iPhone 17 Pro"})
        self.assertEqual((status, body["code"]), (400, "xray_device"))
        self.assertIn("simulator", body["error"] if "error" in body else json.dumps(body))
        self.assertEqual(FakeRecording.made, [])

    def test_an_unknown_job_is_a_404(self):
        self.assertEqual(self.call("GET", "/api/xray/network/" + "b" * 12)[0], 404)

    def test_the_plan_is_asked_for_before_a_capture_starts(self):
        gate = Mock()
        gate.require.side_effect = APIError("Start your plan to record.", 403, "plan_required")
        self.runtime_.plan_gate = gate
        status, body = self.call("POST", "/api/xray/network", {"device": UDID})
        self.assertEqual((status, body["code"]), (403, "plan_required"))
        self.assertEqual(FakeRecording.made, [])

    def test_the_track_registers_its_routes_and_service(self):
        from mobile_agent import api_routes, tracks
        tracks.load()
        self.assertEqual(tracks.STATUS.get("netxray"), "ok")
        patterns = {(route.method, route.pattern) for route in api_routes._routes if route.owner == "netxray"}
        self.assertEqual(patterns, {("POST", "/api/xray/network"), ("GET", "/api/xray/network/{id}"),
                                    ("POST", "/api/xray/network/{id}/stop")})

    def test_closing_the_runtime_closes_the_board(self):
        runtime = self.runtime()
        runtime.netxray_board = None
        register.open_board(runtime)
        board = runtime.netxray_board
        register.close_board(runtime)
        self.assertTrue(board.closed)
        self.assertIsNone(runtime.netxray_board)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        self.mine = self.base / "mine.pcapng"
        with open(self.mine, "wb") as stream:
            synthetic.flashlight(stream)
        data = patch("mobile_agent.paths.user_data_dir", return_value=self.base / "data")
        data.start()
        self.addCleanup(data.stop)
        FakeRecording.made = []

    def parse(self, *argv):
        from mobile_agent.xray import cli as group
        parser = argparse.ArgumentParser(prog="mobster xray")
        group.add_arguments(parser, None)
        return parser.parse_args(["network", *argv])

    def call(self, *argv, pick=None, names=None):
        out, err = io.StringIO(), io.StringIO()
        phone = pick or target.Phone(UDID, "Test iPhone", UDID, False)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch("mobile_agent.netxray.target.pick", return_value=phone) as picked, \
                patch("mobile_agent.netxray.target.phone_names", return_value=names or attribution.Names()), \
                patch("mobile_agent.netxray.capture.Recording",
                      side_effect=lambda udid, seconds, out_, network=False: FakeRecording(udid, seconds, out_, network,
                                                                                         ends=True)), \
                patch("mobile_agent.netxray.cli.time.sleep"):
            code = cli.run(self.parse(*argv))
        self.picked = picked
        return code, out.getvalue(), err.getvalue()

    def reports(self):
        return sorted((self.base / "data" / "xray" / "network").iterdir())

    def test_the_whole_command_line_takes_app(self):
        from mobile_agent.__main__ import check_placement
        from mobile_agent.cli_parser import build_parser
        parser = build_parser()
        args = parser.parse_args(["xray", "network", "--app", "Weather"])
        check_placement(parser, args, on_error=lambda message: self.fail(message))
        self.assertEqual(args.only_app, "Weather")

    def test_seconds_outside_5_to_3600_are_refused(self):
        for bad in ("4", "3601", "ten"):
            with self.subTest(seconds=bad), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.parse("--seconds", bad)

    def test_a_capture_you_have_prints_one_line_per_app_and_the_report_with_no_phone(self):
        code, out, err = self.call("--from", str(self.mine))
        self.assertEqual((code, err), (0, ""))
        self.picked.assert_not_called()
        self.assertIn("In 59 s Flashlight Pro contacted 14 domains at 10 companies; 6 of them are ads, "
                      "analytics or attribution.",
                      out.splitlines())
        self.assertTrue(out.splitlines()[-1].startswith("Report: "))
        self.assertTrue(self.mine.exists())
        (folder,) = self.reports()
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertTrue(out.splitlines()[-1].endswith(str(folder / "report.html")))

    def test_app_reports_only_that_apps_lines(self):
        code, out, _ = self.call("--from", str(self.mine), "--app", "flashlight pro")
        self.assertEqual(code, 0)
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("In ")]), 1)
        self.assertNotIn("courier.push.apple.com", (self.reports()[0] / "report.md").read_text())
        code, out, _ = self.call("--from", str(self.mine), "--app", "Nope")
        self.assertIn("No traffic from Nope in this capture.", out)

    def test_json_is_one_object(self):
        code, out, _ = self.call("--from", str(self.mine), "--json")
        data = json.loads(out)
        self.assertEqual((code, data["result"]["schema"], data["device"], data["capture"]),
                         (0, "mobster.netxray/1", None, None))
        self.assertEqual(set(data["report"]), {"json", "html", "markdown"})
        self.assertEqual(len(data["lines"]), 2)

    def test_from_refuses_the_options_that_need_a_phone(self):
        for extra in (("--device", "x"), ("--seconds", "30"), ("--wifi",), ("--keep-capture",)):
            with self.subTest(extra=extra):
                code, out, err = self.call("--from", str(self.mine), *extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--from", err)
        self.assertFalse((self.base / "data").exists())

    def test_a_file_that_isnt_a_capture_is_one_sentence_and_no_leftover_folder(self):
        for name, content, words in (("gone.pcapng", None, "no capture at"), ("bad.pcapng", b"nope", "capture")):
            path = self.base / name
            if content:
                path.write_bytes(content)
            code, out, err = self.call("--from", str(path))
            self.assertEqual((code, out), (1, ""))
            self.assertIn(words, err)
        self.assertEqual(self.reports(), [])

    def test_recording_uses_the_picked_phone_and_names_the_apps(self):
        names = attribution.Names({"Flashlight Pro": "Flashlight Pro Max"})
        code, out, err = self.call("--device", "Test iPhone", "--seconds", "30", names=names)
        self.assertEqual(code, 0)
        self.picked.assert_called_once_with("Test iPhone", wifi=False)
        self.assertIn("Recording Test iPhone over the cable for 30 s", err)
        self.assertEqual((FakeRecording.made[0].udid, FakeRecording.made[0].seconds), (UDID, 30))
        self.assertIn("Flashlight Pro Max contacted 14 domains", out)
        (folder,) = self.reports()
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["netxray.json", "report.html", "report.md"])

    def test_wifi_forces_the_network_and_keep_capture_keeps_the_file(self):
        code, out, err = self.call("--wifi", "--keep-capture", "--json")
        self.assertEqual(code, 0)
        self.picked.assert_called_once_with(None, wifi=True)
        self.assertIn("Recording Test iPhone over the cable", err)
        kept = json.loads(out)["capture"]
        self.assertTrue(Path(kept).is_file())
        code, _, err = self.call("--wifi", pick=target.Phone(UDID, "Test iPhone", UDID, True))
        self.assertEqual(code, 0)
        self.assertIn("over Wi-Fi", err)
        self.assertTrue(FakeRecording.made[-1].network)

    def test_a_phone_that_cant_be_chosen_is_one_sentence_and_exit_3(self):
        with patch("mobile_agent.netxray.target.pick", side_effect=target.TargetError("2 iPhones are connected.")), \
                contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            code = cli.run(self.parse())
        self.assertEqual((code, err.getvalue().strip()), (3, "2 iPhones are connected."))
        self.assertFalse((self.base / "data").exists())

    def test_a_capture_that_fails_says_why_and_leaves_nothing(self):
        def failing(udid, seconds, out_, network=False):
            return FakeRecording(udid, seconds, out_, network, fail="The connection to the iPhone broke.", ends=True)
        with patch("mobile_agent.netxray.target.pick", return_value=target.Phone(UDID, "T", UDID, False)), \
                patch("mobile_agent.netxray.capture.Recording", side_effect=failing), \
                contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()), \
                patch("mobile_agent.netxray.cli.time.sleep"):
            code = cli.run(self.parse())
        self.assertEqual(code, 1)
        self.assertIn("broke", err.getvalue())
        self.assertEqual(self.reports(), [])

    def test_the_command_is_in_the_parser_and_asks_for_the_plan_only_with_a_phone(self):
        parser = cli_parser.build_parser()
        phone = parser.parse_args(["xray", "network", "--device", "x"])
        capture_file = parser.parse_args(["xray", "network", "--from", "a.pcapng"])
        self.assertTrue(plan_gate.cli_needs_plan("xray", phone))
        self.assertFalse(plan_gate.cli_needs_plan("xray", capture_file))
        self.assertFalse(plan_gate.cli_needs_plan("xray", argparse.Namespace(xray_command="diff")))
        self.assertFalse(plan_gate.cli_needs_plan("xray", argparse.Namespace()))


if __name__ == "__main__":
    unittest.main()
