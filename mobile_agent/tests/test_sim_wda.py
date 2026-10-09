"""WebDriverAgent on Mobster's simulators: the lsof loopback parser, reuse and restart decisions with faked
ps, lsof and /status, the build cache, pinning the test plan to 127.0.0.1, and the sessionless alert calls
over faked HTTP and over a real local HTTP server."""

from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest import mock

from mobile_agent import device_manager, wda_source
from mobile_agent.sim import SimError, wda
from mobile_agent.sim.simctl import Result
from mobile_agent.tests.test_sim_fakes import TOOLS

UDID = "0A1B2C3D-0000-4000-8000-000000000001"
OTHER = "0A1B2C3D-0000-4000-8000-000000000002"
PLAN = ("/Users/example/Library/Application Support/app.mobster.desktop/dev/wda/build-17E192-00c38220-0000aaaa/"
        "Build/Products/WebDriverAgentRunner_iphonesimulator26.4-arm64-x86_64.xctestrun")
HEADER = "COMMAND     PID USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME\n"


def lsof_line(host_port, pid=4242):
    return f"WebDriver {pid} someone    7u  IPv4 0x75e325d2062be109      0t0  TCP {host_port} (LISTEN)\n"


def ps_line(pid, udid, plan=PLAN):
    return (f"{pid} /Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild test-without-building "
            f"-xctestrun {plan} -destination id={udid}\n")


RUNNER_PATH = ("/Users/example/Library/Developer/CoreSimulator/Devices/{udid}/data/Containers/Bundle/Application/"
               "998F1166-50C9-4ADF-B10A-F6B77AD64B9D/WebDriverAgentRunner-Runner.app/WebDriverAgentRunner-Runner\n")


class LsofTests(unittest.TestCase):
    def test_loopback_listener(self):
        listeners = wda.parse_lsof(HEADER + lsof_line("127.0.0.1:8310"))
        self.assertEqual(listeners, [wda.Listener("WebDriver", 4242, "127.0.0.1", 8310)])
        self.assertTrue(wda.loopback_only(listeners))
        self.assertEqual(wda.exposed(listeners), [])

    def test_every_interface_or_another_address_is_exposed(self):
        for name, host in (("*:9310", "*"), ("[::1]:9310", "::1"), ("192.168.1.20:9310", "192.168.1.20"),
                           ("[::]:9310", "::")):
            with self.subTest(name=name):
                listeners = wda.parse_lsof(HEADER + lsof_line("127.0.0.1:9310") + lsof_line(name))
                self.assertEqual(listeners[1].host, host)
                self.assertFalse(wda.loopback_only(listeners))
                self.assertEqual(len(wda.exposed(listeners)), 1)

    def test_nothing_listening_is_not_loopback_only(self):
        self.assertEqual(wda.parse_lsof(""), [])
        self.assertEqual(wda.parse_lsof(HEADER), [])
        self.assertFalse(wda.loopback_only([]))

    def test_garbage_lines_are_skipped(self):
        text = HEADER + "lsof: WARNING: can't stat() nfs file system\n" + lsof_line("127.0.0.1:notaport")
        self.assertEqual(wda.parse_lsof(text), [])

    def test_the_listener_names_its_simulator(self):
        self.assertEqual(wda.device_of(RUNNER_PATH.format(udid=UDID.lower())), UDID)
        self.assertIsNone(wda.device_of("/usr/local/bin/some-server --port 8310"))


class RunnerTests(unittest.TestCase):
    def test_runner_commands_only_for_this_simulator(self):
        output = ps_line(11, UDID) + ps_line(12, OTHER) + "13 /bin/zsh\n" + \
            f"14 xcodebuild build-for-testing -destination id={UDID}\n"
        self.assertEqual([pid for pid, _ in wda.runner_commands(UDID, output)], [11])

    def test_the_test_plan_of_a_runner_with_spaces_in_its_path(self):
        command = wda.runner_commands(UDID, ps_line(11, UDID))[0][1]
        self.assertEqual(wda.xctestrun_of(command), PLAN)
        self.assertIsNone(wda.xctestrun_of("xcodebuild test-without-building"))

    def plan(self, **state):
        base = {"udid": UDID, "listening": True, "owner": UDID, "ready": True,
                "runners": wda.runner_commands(UDID, ps_line(11, UDID)), "xctestrun": PLAN}
        return wda.runner_plan(**{**base, **state})

    def test_reuse_a_ready_runner_from_this_build(self):
        self.assertEqual(self.plan(), wda.REUSE)
        self.assertEqual(self.plan(owner=wda.UNCHECKED), wda.REUSE)

    def test_restart_a_wedged_runner(self):
        self.assertEqual(self.plan(ready=False), wda.RESTART)

    def test_restart_a_runner_from_another_build(self):
        self.assertEqual(self.plan(xctestrun=PLAN.replace("17E192", "17F100")), wda.RESTART)

    def test_restart_when_the_app_serves_but_its_xcodebuild_is_gone(self):
        self.assertEqual(self.plan(runners=[]), wda.RESTART)

    def test_move_off_a_port_another_program_holds(self):
        self.assertEqual(self.plan(owner=None), wda.MOVE)
        self.assertEqual(self.plan(owner=OTHER), wda.MOVE)

    def test_start_when_nothing_listens(self):
        self.assertEqual(self.plan(listening=False, owner=None, ready=False, runners=[]), wda.START)

    def test_a_stray_runner_elsewhere_is_restarted(self):
        self.assertEqual(self.plan(listening=False, owner=None, ready=False), wda.RESTART)


class ServiceTests(unittest.TestCase):
    """WebDriverAgent.plan and friends over faked ps, lsof and /status."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        self.service = wda.WebDriverAgent(self.data)
        self.listening, self.lsof, self.ps, self.ready = set(), {}, "", set()
        self.commands = {}

        def run(argv, timeout=60, env=None):
            argv = [str(part) for part in argv]
            if argv[0] == "/usr/sbin/lsof":
                return Result(0, self.lsof.get(int(argv[2].split(":")[1]), ""), "")
            if argv[:2] == ["/bin/ps", "-axo"]:
                return Result(0, self.ps, "")
            if argv[:2] == ["/bin/ps", "-o"]:
                return Result(0, self.commands.get(int(argv[-1]), ""), "")
            raise AssertionError(argv)

        self.service.run = run
        self.service.listening = lambda port: port in self.listening
        self.service.ready = lambda url: url in self.ready
        self.service.sleep = lambda seconds: None

    def serve(self, port, udid, pid=4242, ready=True):
        self.listening.add(port)
        self.lsof[port] = HEADER + lsof_line(f"127.0.0.1:{port}", pid)
        self.commands[pid] = RUNNER_PATH.format(udid=udid)
        if ready:
            self.ready.add(f"http://127.0.0.1:{port}")

    def test_reuse_takes_one_ps_and_one_status_and_no_lsof(self):
        self.serve(8310, UDID)
        self.ps = ps_line(11, UDID)
        self.lsof = {}  # an lsof here would find nothing, and the plan would not be reuse
        self.assertEqual(self.service.plan(UDID, 8310, 9310, PLAN), wda.REUSE)

    def test_a_stale_build_on_the_port_is_restarted_after_the_owner_check(self):
        self.serve(8310, UDID)
        self.serve(9310, UDID)
        self.ps = ps_line(11, UDID, PLAN.replace("17E192", "17F100"))
        self.assertEqual(self.service.plan(UDID, 8310, 9310, PLAN), wda.RESTART)

    def test_a_stranger_on_the_mjpeg_port_moves_even_when_the_api_port_is_ours(self):
        self.serve(8310, UDID, ready=False)
        self.serve(9310, OTHER, pid=5151)
        self.ps = ps_line(11, UDID)
        self.assertEqual(self.service.plan(UDID, 8310, 9310, PLAN), wda.MOVE)

    def test_restart_when_status_does_not_answer(self):
        self.serve(8310, UDID, ready=False)
        self.ps = ps_line(11, UDID)
        self.assertEqual(self.service.plan(UDID, 8310, 9310, PLAN), wda.RESTART)

    def test_move_when_another_simulator_holds_the_port(self):
        self.serve(8310, OTHER)
        self.assertEqual(self.service.plan(UDID, 8310, 9310, PLAN), wda.MOVE)

    def test_move_when_something_else_holds_the_mjpeg_port(self):
        self.listening.add(9310)
        self.lsof[9310] = HEADER + "python3 777 someone 3u IPv4 0x1 0t0 TCP 127.0.0.1:9310 (LISTEN)\n"
        self.commands[777] = "python3 -m http.server 9310\n"
        self.assertEqual(self.service.plan(UDID, 8310, 9310, PLAN), wda.MOVE)

    def test_start(self):
        self.assertEqual(self.service.plan(UDID, 8310, 9310, PLAN), wda.START)

    def test_owner(self):
        self.serve(8310, UDID)
        self.assertEqual(self.service.owner(8310), UDID)
        self.assertIsNone(self.service.owner(8399))

    def test_loopback_check_waits_for_mjpeg_and_rejects_exposure(self):
        self.lsof[8310] = HEADER + lsof_line("127.0.0.1:8310")
        answers = iter(["", HEADER + lsof_line("127.0.0.1:9310")])
        original = self.service.run

        def run(argv, timeout=60, env=None):
            if argv[0] == "/usr/sbin/lsof" and argv[2] == "-iTCP:9310":
                return Result(0, next(answers, HEADER + lsof_line("127.0.0.1:9310")), "")
            return original(argv, timeout, env)
        self.service.run = run
        ok, found = self.service.check_loopback(8310, 9310)
        self.assertTrue(ok)
        self.assertEqual(found[9310][0].host, "127.0.0.1")
        self.service.run = original
        self.lsof[9310] = HEADER + lsof_line("*:9310")
        ok, found = self.service.check_loopback(8310, 9310)
        self.assertFalse(ok)

    def test_start_passes_ports_and_loopback(self):
        popen = mock.Mock()
        self.service.popen = popen
        self.service.start(UDID, 8310, 9310, PLAN, TOOLS)
        command = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(command, [TOOLS.xcodebuild, "test-without-building", "-xctestrun", PLAN,
                                   "-destination", f"id={UDID}"])
        self.assertEqual({key: kwargs["env"][key] for key in ("TEST_RUNNER_USE_PORT", "TEST_RUNNER_MJPEG_SERVER_PORT",
                                                              "TEST_RUNNER_USE_IP")},
                         {"TEST_RUNNER_USE_PORT": "8310", "TEST_RUNNER_MJPEG_SERVER_PORT": "9310",
                          "TEST_RUNNER_USE_IP": "127.0.0.1"})
        self.assertTrue(kwargs["start_new_session"])
        self.assertIn("TEST_RUNNER_USE_IP=127.0.0.1", self.service.runner_log(UDID).read_text())

    def test_wait_ready_reports_an_early_exit_with_the_log(self):
        self.service.logs.mkdir(parents=True)
        self.service.runner_log(UDID).write_text("Testing started\nerror: Failed to install the runner\n")
        process = mock.Mock()
        process.poll.return_value = 65
        with self.assertRaises(SimError) as caught:
            self.service.wait_ready(UDID, 8310, process, timeout=10)
        self.assertEqual(caught.exception.kind, "wda")
        self.assertIn("Failed to install the runner", str(caught.exception))

    def test_wait_ready_times_out(self):
        self.assertEqual(wda.READY_TIMEOUT, 240)
        clock = iter(range(0, 1000, 5))
        self.service.clock = lambda: next(clock)
        with self.assertRaises(SimError) as caught:
            self.service.wait_ready(UDID, 8310, None)
        self.assertEqual(str(caught.exception), "WebDriverAgent didn't answer on http://127.0.0.1:8310 within 240 s, "
                                                "which happens when this Mac is busy.")
        self.assertTrue(caught.exception.fix.startswith("Run the command again."))

    def test_the_restarter_is_a_simulator_wda_started_with_mobsters_xcode_on_loopback(self):
        from mobile_agent.bench.sim_wda import SimulatorWDA
        plan = self.data / "plan.xctestrun"
        plan.write_text("")
        xcode = "/Applications/Xcode.app/Contents/Developer"
        tools = TOOLS._replace(env={"PATH": "/usr/bin:/bin", "DEVELOPER_DIR": xcode})
        restarter = wda.SimRestarter(UDID, 8310, 9310, plan, tools, log_path=str(self.data / "wda.log"))
        self.assertIsInstance(restarter, SimulatorWDA)
        self.assertEqual(restarter.command(), [TOOLS.xcodebuild, "test-without-building", "-xctestrun", str(plan),
                                               "-destination", f"id={UDID}"])
        with mock.patch("mobile_agent.sim.wda.subprocess.Popen") as popen:
            restarter.start()
        env = popen.call_args.kwargs["env"]
        self.assertEqual((env["DEVELOPER_DIR"], env["TEST_RUNNER_USE_PORT"], env["TEST_RUNNER_MJPEG_SERVER_PORT"],
                          env["TEST_RUNNER_USE_IP"]),
                         (xcode, "8310", "9310", "127.0.0.1"))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_stop_signals_only_this_simulators_runner(self):
        from mobile_agent.bench import sim_wda
        self.ps = ps_line(11, UDID) + ps_line(12, OTHER)
        with mock.patch.object(sim_wda, "_signal") as signal, mock.patch.object(sim_wda, "_alive", return_value=False):
            self.assertEqual(self.service.stop(UDID), [11])
        self.assertEqual({call.args[0] for call in signal.call_args_list}, {11})


class BuildTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        self.service = wda.WebDriverAgent(self.data)

    def test_the_cache_key(self):
        build = wda.build_dir(self.data, "17E192")
        self.assertEqual(build.name, f"build-17E192-{wda_source.COMMIT[:8]}-{wda.PATCH_DIGEST}")
        self.assertEqual(wda.parse_build_dir(build.name), ("17E192", wda_source.COMMIT[:8], wda.PATCH_DIGEST))
        self.assertIsNone(wda.parse_build_dir("build-oops"))
        self.assertEqual(wda.source_dir(self.data).name, f"src-{wda_source.COMMIT[:8]}")

    def cached_plan(self, use_ip):
        products = wda.build_dir(self.data, TOOLS.build) / "Build" / "Products"
        products.mkdir(parents=True)
        plan = products / "WebDriverAgentRunner_iphonesimulator26.4-arm64.xctestrun"
        with open(plan, "wb") as stream:
            plistlib.dump({"TestConfigurations": [{"TestTargets": [
                {"BlueprintName": "WebDriverAgentRunner", "EnvironmentVariables": {"USE_IP": use_ip}}]}],
                "__xctestrun_metadata__": {"FormatVersion": 2}}, stream)
        self.service.run_logged = mock.Mock(side_effect=AssertionError("built again"))
        return plan

    def test_a_cached_build_is_reused_without_xcodebuild_or_the_build_lock(self):
        plan = self.cached_plan("127.0.0.1")
        with mock.patch.object(self.service, "build_lock", side_effect=AssertionError("locked")):
            self.assertEqual(self.service.ensure_build(TOOLS), plan)

    def test_a_cached_build_that_was_never_pinned_is_pinned_before_it_is_used(self):
        plan = self.cached_plan("")  # the process died between xcodebuild writing the plan and the pin
        self.assertFalse(wda.plan_is_pinned(plan))
        self.assertEqual(self.service.ensure_build(TOOLS), plan)
        self.assertTrue(wda.plan_is_pinned(plan))
        self.service.run_logged.assert_not_called()

    def test_first_build_runs_build_for_testing_unsigned_for_any_simulator(self):
        build = wda.build_dir(self.data, TOOLS.build)
        source = wda.source_dir(self.data)

        def run_logged(command, log, timeout, env):
            products = build / "Build" / "Products"
            products.mkdir(parents=True)
            with open(products / "WebDriverAgentRunner_iphonesimulator26.4-arm64.xctestrun", "wb") as stream:
                plistlib.dump({"WebDriverAgentRunner": {"TestHostPath": "x", "EnvironmentVariables": {}},
                               "__xctestrun_metadata__": {"FormatVersion": 1}}, stream)
            return 0
        self.service.ensure_source = mock.Mock(return_value=source)
        self.service.run_logged = mock.Mock(side_effect=run_logged)
        plan = self.service.ensure_build(TOOLS)
        command = self.service.run_logged.call_args.args[0]
        self.assertEqual(command, [TOOLS.xcodebuild, "build-for-testing", "-project",
                                   str(source / "WebDriverAgent.xcodeproj"), "-scheme", "WebDriverAgentRunner",
                                   "-destination", "generic/platform=iOS Simulator", "-derivedDataPath", str(build),
                                   "CODE_SIGNING_ALLOWED=NO"])
        self.assertTrue(wda.plan_is_pinned(plan))

    def test_a_failed_build_names_the_error_line(self):
        def run_logged(command, log, timeout, env):
            Path(log).write_text("CompileC ...\n/path/FBWebServer.m:12: error: use of undeclared identifier\n"
                                 "** TEST BUILD FAILED **\n")
            return 65
        self.service.ensure_source = mock.Mock(return_value=wda.source_dir(self.data))
        self.service.run_logged = run_logged
        with self.assertRaises(SimError) as caught:
            self.service.ensure_build(TOOLS)
        self.assertEqual(caught.exception.kind, "wda")
        self.assertIn("use of undeclared identifier", str(caught.exception))
        self.assertIn("wda-build.log", caught.exception.fix)

    def test_the_source_is_cloned_pinned_verified_and_patched(self):
        calls = []

        def run(argv, timeout=60, env=None):
            calls.append(argv)
            Path(argv[-1]).mkdir(parents=True)
            return Result(0, "", "")
        self.service.run = run
        with mock.patch.object(wda_source, "is_pinned", return_value=False), \
                mock.patch.object(wda_source, "verify") as verify, \
                mock.patch.object(device_manager, "patch_wda") as patch:
            source = self.service.ensure_source(TOOLS)
        self.assertEqual(calls[0][:-1], wda_source.clone_command(TOOLS.git, "x")[:-1])
        verify.assert_called_once()
        patch.assert_called_once_with(source)
        self.assertTrue(source.is_dir())
        self.assertEqual(list(source.parent.glob(".src-*")), [])

    def test_a_failed_clone_is_a_wda_error(self):
        self.service.run = lambda argv, timeout=60, env=None: Result(128, "", "fatal: unable to access github.com")
        with mock.patch.object(wda_source, "is_pinned", return_value=False), self.assertRaises(SimError) as caught:
            self.service.ensure_source(TOOLS)
        self.assertEqual(caught.exception.kind, "wda")
        self.assertIn("unable to access", str(caught.exception))

    def test_pin_loopback_in_both_plan_formats(self):
        products = self.data / "Products"
        products.mkdir()
        one = products / "one_iphonesimulator.xctestrun"
        two = products / "two_iphonesimulator.xctestrun"
        with open(one, "wb") as stream:
            plistlib.dump({"WebDriverAgentRunner": {"TestHostPath": "x", "EnvironmentVariables": {"USE_IP": ""}},
                           "__xctestrun_metadata__": {"FormatVersion": 1}}, stream)
        with open(two, "wb") as stream:
            plistlib.dump({"TestConfigurations": [{"TestTargets": [{"BlueprintName": "WebDriverAgentRunner"}]}],
                           "__xctestrun_metadata__": {"FormatVersion": 2}}, stream)
        self.assertFalse(wda.plan_is_pinned(one))
        self.assertFalse(wda.plan_is_pinned(two))
        wda.pin_loopback(products)
        self.assertTrue(wda.plan_is_pinned(one))
        self.assertTrue(wda.plan_is_pinned(two))

    def test_a_long_build_times_out_and_can_be_cancelled(self):
        import threading
        import time
        log = self.data / "build.log"
        self.assertIsNone(self.service.run_logged(["/bin/sleep", "30"], log, 0.2, None))
        self.assertIn("did not finish in 0.2 s", log.read_text())
        codes = []
        thread = threading.Thread(target=lambda: codes.append(self.service.run_logged(["/bin/sleep", "30"], log, 60,
                                                                                       None)))
        started = time.monotonic()
        thread.start()
        while not self.service._running and time.monotonic() - started < 5:
            time.sleep(.01)
        self.service.cancel()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertNotEqual(codes, [0])
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.service._running, set())

    def test_the_first_build_names_its_log_and_is_never_silent(self):
        """Review ux-11: up to 20 minutes of silence. The first line names the log and the usual time; a "Still
        building" line follows every HEARTBEAT_SECONDS."""
        import time
        said = []
        self.service.progress = said.append

        def run_logged(command, log, timeout, env):
            time.sleep(.25)
            products = Path(command[command.index("-derivedDataPath") + 1]) / "Build" / "Products"
            products.mkdir(parents=True, exist_ok=True)
            (products / "WebDriverAgentRunner_iphonesimulator26.4-arm64.xctestrun").write_bytes(
                plistlib.dumps({"WebDriverAgentRunner": {"TestHostPath": "x", "EnvironmentVariables": {}}}))
            return 0
        self.service.run_logged = run_logged
        self.service.ensure_source = lambda tools: self.data / "src"
        with mock.patch.object(wda, "HEARTBEAT_SECONDS", .05):
            self.service.ensure_build(TOOLS)
        self.assertIn("usually 2 to 5 minutes (log: ", said[0])
        self.assertIn("wda-build.log", said[0])
        self.assertTrue(any(line.startswith("Still building WebDriverAgent (") for line in said), said)

    def test_heartbeat_words(self):
        self.assertEqual([wda.elapsed_words(n) for n in (0, 45, 60, 125)], ["0 s", "45 s", "1 min 00 s", "2 min 05 s"])
        said = []
        with wda.heartbeat(said.append, "booting X", interval=.02):
            import time
            time.sleep(.1)
        count = len(said)
        time.sleep(.06)
        self.assertGreater(count, 0)
        self.assertEqual(len(said), count)  # it stops with the block
        self.assertTrue(said[0].startswith("Still booting X ("))

    def test_last_error(self):
        log = self.data / "log"
        self.assertEqual(wda.last_error(log), "")
        log.write_text("one\nerror: first\ntwo\nerror: second\nthree\n")
        self.assertEqual(wda.last_error(log), "error: second")
        log.write_text("one\nlast words\n")
        self.assertEqual(wda.last_error(log), "last words")


class AlertTests(unittest.TestCase):
    """The sessionless alert calls: over faked HTTP, then http_json against a real server on 127.0.0.1."""

    URL = "http://127.0.0.1:8310"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.service = wda.WebDriverAgent(Path(folder.name))
        self.requests, self.answers = [], []

        def http(method, url, body=None, timeout=10):
            self.requests.append((method, url, body))
            return self.answers.pop(0)
        self.service.http = http

    def test_alert_text(self):
        self.answers = [(200, "Open in “Daybreak”?"),
                        (404, {"error": "no such alert", "message": "An attempt was made to operate on a modal"}),
                        (None, None), (200, ""), (200, {"unexpected": True})]
        self.assertEqual(self.service.alert_text(self.URL), "Open in “Daybreak”?")
        for _ in range(4):
            self.assertIsNone(self.service.alert_text(self.URL))
        self.assertEqual(self.requests, [("GET", self.URL + "/alert/text", None)] * 5)

    def test_accept_and_dismiss(self):
        self.answers = [(200, None), (200, None), (500, {"error": "unknown error"})]
        self.assertTrue(self.service.alert_action(self.URL, "accept", "Open"))
        self.assertTrue(self.service.alert_action(self.URL, "dismiss"))
        self.assertFalse(self.service.alert_action(self.URL, "accept", "Allow"))
        self.assertEqual(self.requests, [("POST", self.URL + "/alert/accept", {"name": "Open"}),
                                         ("POST", self.URL + "/alert/dismiss", {}),
                                         ("POST", self.URL + "/alert/accept", {"name": "Allow"})])
        with self.assertRaises(ValueError):
            self.service.alert_action(self.URL, "tap")

    def test_http_json_against_a_local_server(self):
        import http.server
        import json
        import threading
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def reply(self, status, value):
                body = json.dumps({"value": value, "sessionId": None}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                seen.append(("GET", self.path, None))
                if self.path == "/alert/text":
                    self.reply(200, "Open in “Sim Smoke”?")
                else:
                    self.reply(404, {"error": "unknown command"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                seen.append(("POST", self.path, json.loads(self.rfile.read(length) or b"null")))
                self.reply(200, None)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        service = wda.WebDriverAgent(self.service.data_dir)
        self.assertEqual(service.alert_text(url), "Open in “Sim Smoke”?")
        self.assertTrue(service.alert_action(url, "accept", "Open"))
        self.assertEqual(wda.http_json("GET", url + "/nothing"), (404, {"error": "unknown command"}))
        self.assertEqual(seen, [("GET", "/alert/text", None), ("POST", "/alert/accept", {"name": "Open"}),
                                ("GET", "/nothing", None)])
        server.shutdown()
        self.assertEqual(wda.http_json("GET", url + "/alert/text", timeout=1), (None, None))


if __name__ == "__main__":
    unittest.main()
