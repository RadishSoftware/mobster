
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import device_targets
from mobile_agent.sim.api import AppInfo
from mobile_agent.testkit import reports
from mobile_agent.tests.test_testing import validate_junit
from mobile_agent.tests.test_verify_fixtures import paywall
from mobile_agent.tests.test_verify_runner import TARGET, Driver, Manager, RunnerCase
from mobile_agent.verify import crashes, evidence

FIXTURES = Path(__file__).parent / "fixtures" / "evidence"
BUNDLE = "software.radish.mobster.crashfixture"
EXE = "CrashFixture"
UDID = "11111111-2222-3333-4444-555555555555"
PLANS = paywall()


def stamp(wall):
    return datetime.fromtimestamp(wall).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-2] + \
        datetime.fromtimestamp(wall).astimezone().strftime(" %z")


def log_stamp(wall):
    return datetime.fromtimestamp(wall).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f%z")


def crash_file(folder, *, wall=None, pid=218, name=None, fixture="crash-release.ips", body=None):
    wall = time.time() if wall is None else wall
    head, _, rest = (FIXTURES / fixture).read_text().partition("\n")
    header, data = json.loads(head), json.loads(rest)
    header["timestamp"] = stamp(wall)
    data["captureTime"] = stamp(wall)
    data["pid"] = pid
    data.update(body or {})
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name or f"{EXE}-{datetime.fromtimestamp(wall).strftime('%Y-%m-%d-%H%M%S')}.ips")
    path.write_text(json.dumps(header) + "\n" + json.dumps(data, indent=2) + "\n")
    return path


def log_lines(wall, *, pid=218, crash=True):
    lines = ['Filtering the log data using "process == "CrashFixture""']
    for offset, kind, text in ((-1.0, "Default", "fixture launched"), (-0.5, "Error", "fixture: about to crash"),
                               (-0.2, "Debug", "a debug line")):
        lines.append(json.dumps({"eventType": "logEvent", "messageType": kind, "subsystem": BUNDLE,
                                 "category": "fixture", "processImagePath": f"/x/{EXE}.app/{EXE}", "processID": pid,
                                 "timestamp": log_stamp(wall + offset), "eventMessage": text}))
    code = "signal(2) code:SIGTRAP(5)" if crash else "frontboard(10) code:force-quit(0xfbfbfbfb)"
    lines.append(json.dumps({"eventType": "logEvent", "messageType": "Default", "subsystem": "com.apple.FrontBoard",
                             "category": "Process", "processImagePath": "/x/SpringBoard.app/SpringBoard",
                             "timestamp": log_stamp(wall + .05),
                             "eventMessage": f"[app<{BUNDLE}>:{pid}] Process exited: <RBSProcessExitContext| specific, "
                                             f"status:<RBSProcessExitStatus| domain:{code}>>."}))
    return [line + "\n" for line in lines]


class Process:

    pid = 999_999

    def __init__(self, lines):
        self.stdout = iter(lines)
        self.returncode = 0
        self.killed = False

    def poll(self):
        return 0

    def terminate(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class FakeTools(evidence.SimulatorTools):

    def __init__(self, reports_dir, lines=(), answers=None, start_error=None):
        super().__init__(UDID, reports=reports_dir)
        self.lines, self.answers, self.argv, self.ran = list(lines), dict(answers or {}), None, []
        self.start_error = start_error
        self.popen, self.run = self._popen, self._run

    def _popen(self, argv, **kwargs):
        if self.start_error is not None:
            raise self.start_error
        self.argv = argv
        return Process(self.lines)

    def _run(self, argv, **kwargs):
        self.ran.append(argv)
        out = self.answers.get(argv[1] if len(argv) > 1 else argv[0], "")
        return SimpleNamespace(returncode=0, stdout=out(argv) if callable(out) else out, stderr="")


APP_INFO = SimpleNamespace(bundle_id=BUNDLE, executable=EXE, path=None, name="Crash Fixture")


class CrashReportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)

    def test_reads_a_crash_and_refuses_what_isnt_one(self):
        report = crashes.read(FIXTURES / "crash-release.ips")
        self.assertEqual((report.bundle_id, report.process, report.pid), (BUNDLE, EXE, 218))
        self.assertIsNotNone(report.wall)
        for name, text in (("hang.ips", '{"bug_type":"288"}\n{"threads":[]}'), ("broken.ips", "{nope"),
                           ("empty.ips", "")):
            (self.dir / name).write_text(text)
            self.assertIsNone(crashes.read(self.dir / name), name)

    def test_a_report_is_this_runs_only_by_its_app_time_and_process(self):
        now = time.time()
        report = crashes.read(crash_file(self.dir, wall=now, pid=4242))
        ok = dict(bundle_id=BUNDLE, executable=EXE, since=now - 5, until=now + 5)
        self.assertTrue(crashes.belongs(report, **ok))
        self.assertTrue(crashes.belongs(report, **ok, pids={4242}))
        self.assertTrue(crashes.belongs(report, **ok, udid=UDID))
        self.assertFalse(crashes.belongs(report, **{**ok, "bundle_id": "com.example.other"}))
        self.assertFalse(crashes.belongs(report, **{**ok, "since": now + 1}))
        self.assertFalse(crashes.belongs(report, **ok, pids={1}))
        self.assertFalse(crashes.belongs(report, **ok, udid="99999999-2222-3333-4444-555555555555"))
        self.assertFalse(crashes.belongs(report, **ok, pids={1}, udid=UDID))

    def test_symbolicates_the_apps_frames_with_a_matching_dsym(self):
        report = crashes.read(FIXTURES / "crash-release.ips")
        uuid = report.body["usedImages"][0]["uuid"].upper()
        dsym = self.dir / "CrashFixture.app.dSYM"
        dsym.mkdir()
        answers = {"dwarfdump": f"UUID: {uuid} (arm64) {dsym}/Contents/Resources/DWARF/CrashFixture\n",
                   "atos": lambda argv: "specialized CrashTrigger.crashNow() (in CrashFixture) "
                                        "(CrashFixtureApp.swift:11)\n"
                                        "thunk for @escaping @callee_guaranteed () -> () (in CrashFixture) "
                                        "(<compiler-generated>:0)\n"}
        tools = FakeTools(self.dir, answers=answers)
        summary = crashes.summary(report, executable=EXE, dsyms=[dsym], run=tools.run)
        self.assertTrue(summary["symbolicated"])
        top = summary["frames"][0]
        self.assertEqual((top["image"], top["own"], top["symbol"], top["file"], top["line"]),
                         (EXE, True, "specialized CrashTrigger.crashNow()", "CrashFixtureApp.swift", 11))
        atos = next(argv for argv in tools.ran if argv[1] == "atos")
        self.assertEqual(atos[atos.index("-l") + 1], hex(report.body["usedImages"][0]["base"]))
        self.assertEqual((summary["type"], summary["signal"], summary["thread"]),
                         ("EXC_BREAKPOINT", "SIGTRAP", "com.apple.main-thread"))
        self.assertEqual(crashes.headline(summary, "Crash Fixture"),
                         "Crash Fixture crashed: EXC_BREAKPOINT (SIGTRAP), in specialized CrashTrigger.crashNow() "
                         "(CrashFixtureApp.swift:11).")

    def test_a_dsym_that_doesnt_match_names_nothing(self):
        report = crashes.read(FIXTURES / "crash-release.ips")
        tools = FakeTools(self.dir, answers={"dwarfdump": "UUID: 00000000-0000-0000-0000-000000000000 (arm64) /x\n"})
        summary = crashes.summary(report, executable=EXE, dsyms=[self.dir], run=tools.run)
        self.assertFalse(summary["symbolicated"])
        self.assertIsNone(summary["frames"][0]["symbol"])
        self.assertFalse(any(argv[1] == "atos" for argv in tools.ran))

    def test_the_kept_copy_goes_through_the_secret_filter_and_keeps_the_numbers(self):
        key = "sk-ant-" + "a1B2c3D4" * 4
        report = crashes.read(crash_file(self.dir, body={"asi": {"CrashFixture": [f"token {key} rejected"]}}))
        copy = crashes.write_copy(report, self.dir / "kept")
        text = copy.read_text()
        self.assertNotIn(key, text)
        kept = crashes.read(copy)
        self.assertEqual(kept.body["threads"][0]["frames"][0]["imageOffset"],
                         report.body["threads"][0]["frames"][0]["imageOffset"])


class ExitTests(unittest.TestCase):
    def test_a_signal_a_watchdog_and_jetsam_are_crashes_a_requested_quit_isnt(self):
        def line(domain, code):
            return (f"[app<{BUNDLE}>:7] Process exited: <RBSProcessExitContext| specific, "
                    f"status:<RBSProcessExitStatus| "
                    f"domain:{domain} code:{code}>>.")
        cases = {("signal(2)", "SIGTRAP(5)"): True, ("signal(2)", "SIGSEGV(11)"): True,
                 ("signal(2)", "SIGKILL(9)"): False, ("frontboard(10)", "force-quit(0xfbfbfbfb)"): False,
                 ("frontboard(10)", "watchdog(0x8badf00d)"): True, ("jetsam(3)", "highwater(7)"): True}
        for (domain, code), crashed in cases.items():
            with self.subTest(domain=domain, code=code):
                event = evidence.exit_event(line(domain, code), BUNDLE)
                self.assertEqual((event.pid, event.crashed), (7, crashed))
        self.assertIsNone(evidence.exit_event(line("signal(2)", "SIGTRAP(5)"), "com.example.other"))


class LogTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)

    def test_the_iphones_syslog(self):
        tools = evidence.PhoneTools("00008030-000000000000000E", tool=lambda name: f"/bin/{name}")
        self.assertEqual(tools.log_argv(EXE, BUNDLE), ["/bin/idevicesyslog", "-u", "00008030-000000000000000E",
                                                       "--no-colors", "-p", "CrashFixture|SpringBoard"])
        capture = evidence.LogCapture(SimpleNamespace(log_argv=tools.log_argv, parse=tools.parse,
                                                      popen=lambda argv, **_: Process(
                                                          (FIXTURES / "syslog.txt").read_text().splitlines(True))),
                                      EXE, BUNDLE, self.dir / "app.log", t0_wall=0, clock=lambda: 1.5)
        exits = []
        capture.on_exit = exits.append
        capture.start()
        capture._thread.join(5)
        capture.stop()
        texts = [(line.process, line.level, line.text) for line in capture.all()]
        self.assertEqual(texts[0], (EXE, "notice", "fixture launched"))
        self.assertEqual(texts[3], (EXE, "notice", "second line of the same entry"))
        self.assertFalse(any("mobilesafari" in text for _, _, text in texts))
        self.assertEqual(texts[-1][0], "SpringBoard")
        self.assertEqual([(event.pid, event.crashed) for event in exits], [(218, True)])
        self.assertTrue(all(line.at_ms == 1500 for line in capture.all()))

    def test_the_file_is_capped_keeps_the_tail_and_masks_secrets(self):
        key = "ghp_" + "A1b2C3d4E5" * 3
        lines = [f"Oct 16 10:00:00.000000 iPhone {EXE}(x)[1] <Notice>: line {n:05d} " + "x" * 80 + "\n"
                 for n in range(40)] + [f"Oct 16 10:00:00.000000 iPhone {EXE}(x)[1] <Error>: token {key}\n"]
        tools = evidence.PhoneTools("U", tool=lambda name: name)
        fake = SimpleNamespace(log_argv=tools.log_argv, parse=tools.parse, popen=lambda argv, **_: Process(lines))
        with mock.patch.object(evidence, "LOG_CAP_BYTES", 2000), mock.patch.object(evidence, "TAIL_LINES", 5):
            capture = evidence.LogCapture(fake, EXE, BUNDLE, self.dir / "logs" / "app.log", t0_wall=0, clock=lambda: 0)
            capture.tail = evidence.deque(maxlen=5)
            capture.start()
            capture._thread.join(5)
            capture.stop()
        text = (self.dir / "logs" / "app.log").read_text()
        self.assertNotIn(key, text)
        self.assertIn("lines dropped", text)
        self.assertIn("line 00000", text)
        self.assertIn("line 00039", text)
        self.assertLessEqual(len(text.encode()), 2000 + 6 * 140)
        self.assertEqual(capture.count, 41)


class PhoneToolTests(unittest.TestCase):
    def test_crash_reports_are_copied_and_never_removed_from_the_phone(self):
        ran = []

        def run(argv, **kwargs):
            ran.append(argv)
            crash_file(argv[-1], name=f"{EXE}-2026-10-16-120000.ips")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        tools = evidence.PhoneTools("00008030-000000000000000E", run=run, tool=lambda name: f"/bin/{name}")
        paths, scratch = tools.crash_reports(EXE, 0)
        try:
            argv = ran[0]
            self.assertEqual(argv[0], "/bin/idevicecrashreport")
            self.assertIn("--keep", argv)
            self.assertNotIn("--remove-all", argv)
            self.assertEqual(argv[argv.index("--filter") + 1], EXE)
            self.assertEqual([path.name for path in paths], [f"{EXE}-2026-10-16-120000.ips"])
        finally:
            import shutil
            shutil.rmtree(scratch, ignore_errors=True)

    def test_only_a_usb_iphone_offers_evidence(self):
        usb = device_targets.WdaDevice({"kind": "usb", "udid": "00008030-000000000000000E", "wdaUrl": "http://x"})
        with mock.patch.dict(os.environ, {"MOBSTER_NO_DEVICES": ""}):
            self.assertIsInstance(usb.evidence_tools(TARGET), evidence.PhoneTools)
        self.assertIsNone(device_targets.WdaDevice({"kind": "wda", "wdaUrl": "http://x"}).evidence_tools(TARGET))
        with mock.patch.dict(os.environ, {"MOBSTER_NO_DEVICES": "1"}):
            self.assertIsNone(usb.evidence_tools(TARGET))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.reports = self.dir / "DiagnosticReports"
        self.reports.mkdir()
        self.t0 = time.time() - 30

    def evidence(self, lines, **kwargs):
        tools = FakeTools(self.reports, lines=lines)
        made = evidence.Evidence(tools, APP_INFO, self.dir / "run", t0_wall=self.t0, sleep=lambda s: None, **kwargs)
        made.start()
        made._ready()
        made.capture._thread.join(2)
        self.addCleanup(made.stop)
        return made, tools

    def test_a_crash_with_its_report(self):
        wall = time.time() - 2
        crash_file(self.reports, wall=wall, pid=218, name=f"{EXE}-this-run.ips")
        other = "99999999-2222-3333-4444-555555555555"
        crash_file(self.reports, wall=wall, pid=31337, name=f"{EXE}-another-simulator.ips",
                   body={"procPath": f"/Users/USER/Library/Developer/CoreSimulator/Devices/{other}/data/x/{EXE}",
                         "coalitionName": f"com.apple.CoreSimulator.SimDevice.{other}"})
        accessibility = json.dumps({"eventType": "logEvent", "messageType": "Default", "category": "AXRuntime",
                                    "subsystem": "com.apple.accessibility", "processImagePath": f"/x/{EXE}.app/{EXE}",
                                    "processID": 218, "timestamp": log_stamp(wall - 0.4),
                                    "eventMessage": "AX Lookup problem"}) + "\n"
        lines = log_lines(wall)
        made, tools = self.evidence(lines[:2] + [accessibility] + lines[2:])
        self.assertEqual(made.crashed().pid, 218)
        crash = made.crash(suspect=True)
        made.stop()
        self.assertEqual((crash["type"], crash["pid"], crash["report"]), ("EXC_BREAKPOINT", 218,
                                                                         f"crashes/{EXE}-this-run.ips"))
        self.assertTrue((self.dir / "run" / crash["report"]).is_file())
        summary = made.summary(steps=[{"index": 1, "at_ms": 10}], failed=True)
        self.assertEqual(summary["atMs"], crash["atMs"])
        self.assertEqual(summary["step"], 1)
        texts = [line["text"] for line in summary["window"]["lines"]]
        self.assertTrue(any("about to crash" in text for text in texts))
        # The app's own lines are marked app; an Apple framework in its process and SpringBoard aren't (ICP-3).
        app = lambda words: next(line["app"] for line in summary["window"]["lines"] if words in line["text"])  # noqa: E731
        self.assertEqual((app("about to crash"), app("AX Lookup problem"), app("exited: signal")), (True, False, False))
        self.assertTrue(any("exited: signal SIGTRAP (a crash)" in text for text in texts))
        self.assertEqual(summary["log"]["path"], "logs/app.log")

    def test_late_reports_put_the_stack_in_the_run_and_the_suite(self):
        wall = time.time() - 2
        made, tools = self.evidence(log_lines(wall))
        with mock.patch.dict(evidence.REPORT_WAIT, {"simulator": 0}):
            made.crash(suspect=True)
        made.stop()
        run_dir = self.dir / "run"
        stored = made.summary(failed=True)
        result = {"verdict": "failed", "reason": {"class": "app_crashed", "message": "Crash Fixture crashed: SIGTRAP."},
                  "summary": "x", "app": {"bundle_id": BUNDLE, "name": "Crash Fixture", "path": None},
                  "device": {"udid": UDID}, "evidence": stored, "check": {"name": "c"}, "assertions": [], "steps": []}
        (run_dir / "result.json").write_text(json.dumps(result))
        cell = {"reason": dict(result["reason"]), "evidence": {**stored, "runDir": str(run_dir)},
                "runs": [{"runDir": str(run_dir), "class": "app_crashed", "message": "old"}]}
        doc = {"checks": [{"results": [cell]}]}
        clock = SimpleNamespace(now=0.0)

        def sleep(seconds):
            clock.now += seconds
            if clock.now >= 3:
                crash_file(self.reports, wall=wall, pid=218)
        lines = []
        found = evidence.late_reports(doc, progress=lines.append, budget=10, clock=lambda: clock.now, sleep=sleep,
                                      tools_for=lambda source, udid: FakeTools(self.reports))
        self.assertEqual(found, 1)
        self.assertIn("Waiting up to 10 s for 1 crash report", lines[0])
        self.assertEqual(cell["evidence"]["crash"]["type"], "EXC_BREAKPOINT")
        self.assertIn("EXC_BREAKPOINT", cell["reason"]["message"])
        self.assertIn("EXC_BREAKPOINT", cell["runs"][0]["message"])
        written = json.loads((run_dir / "result.json").read_text())
        self.assertTrue(written["evidence"]["crash"]["report"].startswith("crashes/"))
        self.assertEqual(written["evidence"]["atMs"], written["evidence"]["crash"]["atMs"])
        self.assertEqual(cell["evidence"]["atMs"], written["evidence"]["crash"]["atMs"])
        self.assertIn("EXC_BREAKPOINT", (run_dir / "report.html").read_text())

    def test_late_reports_give_up_at_the_budget(self):
        cell = {"evidence": {"crash": {"report": None, "process": EXE}, "runDir": str(self.dir / "gone")}}
        lines = []
        self.assertEqual(evidence.late_reports({"checks": [{"results": [cell]}]}, progress=lines.append, budget=1,
                                               clock=time.monotonic, sleep=lambda s: None), 0)
        self.assertEqual(len(lines), 1)


class EvidenceManager(Manager):
    def __init__(self, tools, **kwargs):
        super().__init__(app_info=AppInfo(bundle_id=BUNDLE, name="Crash Fixture", version="1.0 (1)", path=None,
                                          executable=EXE), **kwargs)
        self.tools = tools

    def evidence_tools(self, target):
        return self.tools


class RunnerTests(RunnerCase):
    def setUp(self):
        super().setUp()
        self.reports = self.root / "DiagnosticReports"
        self.reports.mkdir()

    def run_check(self, lines, *, crash_report=True, foreground=BUNDLE, manager=None, screens=None, **check):
        wall = time.time()
        tools = FakeTools(self.reports, lines=lines(wall))
        on = {"launch": lambda: crash_file(self.reports, wall=time.time(), pid=1001)} if crash_report else {}
        manager = manager or EvidenceManager(tools, on=on)
        check = self.check(bundle_id=BUNDLE, expect=[{"text": "Choose your plan"}], **check)
        with mock.patch.dict(evidence.REPORT_WAIT, {"simulator": 0}), mock.patch.object(evidence, "EXIT_WAIT", 0):
            result, _ = self.verify(check, Driver(screens or [PLANS], foreground=foreground), manager=manager)
        return result

    def test_a_crash_fails_the_run_even_when_the_expectations_held(self):
        result = self.run_check(lambda wall: log_lines(wall, pid=1001))
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "app_crashed"))
        self.assertIn("Crash Fixture crashed: EXC_BREAKPOINT (SIGTRAP)", result["reason"]["message"])
        crash = result["evidence"]["crash"]
        self.assertEqual(crash["pid"], 1001)
        self.assertTrue(Path(result["run_dir"], crash["report"]).is_file())
        self.assertTrue(Path(result["run_dir"], "logs", "app.log").is_file())
        page = Path(result["report"]).read_text()
        self.assertIn("Logs and crash", page)
        self.assertIn('class="ev-file" href="crashes/', page)

    def test_without_a_crash_a_pass_stays_a_pass_and_keeps_its_log(self):
        result = self.run_check(lambda wall: log_lines(wall, pid=1001, crash=False), crash_report=False)
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(set(result["evidence"]), {"source", "startedAt", "log"})

    def test_another_simulators_crash_isnt_this_runs(self):
        result = self.run_check(lambda wall: log_lines(wall, pid=1001, crash=False), foreground="com.apple.springboard")
        self.assertEqual(result["reason"]["class"], "app_not_running")

    def test_a_crash_explains_a_wda_failure_but_never_a_stop(self):
        from mobile_agent.tests.test_verify_runner import SimError
        for error, expected in ((SimError("wda", "WebDriverAgent stopped answering"), "app_crashed"),
                                (SimError("stopped", "Stopped with ctrl+c."), "stopped"),
                                (SimError("usage", "That's not a check."), "usage")):
            with self.subTest(kind=error.kind):
                tools = FakeTools(self.reports, lines=log_lines(time.time() - .5, pid=1001))
                manager = EvidenceManager(tools, fail={"screenshot": error}, on={"launch": lambda: crash_file(
                    self.reports, wall=time.time(), pid=1001, name=f"{EXE}-{error.kind}.ips")})
                check = self.check(bundle_id=BUNDLE, expect=[{"text": "Choose your plan"}])
                from mobile_agent.verify.runner import CouldntRun, VerifyRun
                run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager)
                self.watch(Driver([PLANS]), manager)
                with mock.patch("mobile_agent.verify.runner.build_driver", lambda *a: Driver([PLANS])), \
                        mock.patch.dict(evidence.REPORT_WAIT, {"simulator": 0}):
                    run.prepare()
                    run._fail(CouldntRun(error.kind, str(error)))
                    result = run.finish()
                self.assertEqual(result["reason"]["class"], expected)


class SuiteReportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)

    def doc(self):
        run_dir = self.dir / "runs-src" / "R1"
        (run_dir / "logs").mkdir(parents=True)
        (run_dir / "crashes").mkdir()
        (run_dir / "logs" / "app.log").write_text("   1.000  error  CrashFixture: boom\n")
        (run_dir / "crashes" / "CrashFixture-1.ips").write_text("{}\n{}\n")
        (run_dir / "report.html").write_text('<a class="ev-file" href="logs/app.log">Full log</a>')
        crash = {"type": "EXC_BAD_ACCESS", "signal": "SIGSEGV", "message": None, "atMs": 4200, "pid": 7,
                 "process": EXE, "symbolicated": True, "report": "crashes/CrashFixture-1.ips",
                 "frames": [{"index": 0, "image": EXE, "own": True, "symbol": "Feed.load()", "file": "Feed.swift",
                             "line": 12, "offset": None}]}
        ev = {"source": "simulator", "log": {"path": "logs/app.log", "lines": 1, "bytes": 40, "dropped": 0},
              "crash": crash, "atMs": 4200, "step": 2,
              "window": {"fromMs": -5800, "toMs": 14200, "lines": [
                  {"atMs": 4100, "level": "error", "process": EXE, "text": "about to <crash>"}]},
              "runId": "R1", "runDir": str(run_dir)}
        result = {"device": "iPhone 17 Pro · iOS 26.4", "deviceKey": "k", "status": "failed", "attempts": 1,
                  "flaky": False, "report": str(run_dir / "report.html"), "durationMs": 5000, "healed": [],
                  "reason": {"class": "app_crashed", "message": "Crash Fixture crashed: EXC_BAD_ACCESS (SIGSEGV).",
                             "fix": None},
                  "quarantined": False, "frame": None, "video": None, "assertions": [], "costUsd": 0,
                  "evidence": ev, "runs": [{"repetition": 1, "attempt": 1, "runId": "R1", "verdict": "failed",
                                            "class": "app_crashed", "message": "m", "seconds": 5,
                                            "report": str(run_dir / "report.html"), "runDir": str(run_dir)}]}
        return {"schema": reports.SCHEMA, "suite": "S", "devices": [{"key": "k", "name": "iPhone 17 Pro · iOS 26.4"}],
                "checks": [{"name": "Feed loads", "file": "checks/feed.yaml", "classname": "checks.feed", "tags": [],
                            "quarantined": False, "results": [result]}],
                "summary": {"failed": 1, "total": 1}, "options": {}, "exitCode": 1, "mobster": {"version": "t"}}

    def test_the_suite_folder_carries_the_evidence_and_its_reports_show_it(self):
        folder = self.dir / "suite"
        folder.mkdir()
        doc = self.doc()
        reports.write_all(doc, folder, cwd=self.dir)
        ev = doc["checks"][0]["results"][0]["evidence"]
        self.assertEqual(ev["log"]["path"], "runs/R1/logs/app.log")
        self.assertEqual(ev["crash"]["report"], "runs/R1/crashes/CrashFixture-1.ips")
        self.assertNotIn("runDir", ev)
        self.assertTrue((folder / ev["log"]["path"]).is_file())
        self.assertIn('href="R1/logs/app.log"', (folder / "runs" / "R1.html").read_text())
        data = (folder / "junit.xml").read_bytes()
        validate_junit(data)
        text = data.decode()
        self.assertIn('type="app_crashed"', text)
        self.assertIn("0  CrashFixture  Feed.load() (Feed.swift:12)", text)
        self.assertIn("about to &lt;crash&gt;", text)
        self.assertIn("Log: suite/runs/R1/logs/app.log", text)
        page = (folder / "index.html").read_text()
        self.assertIn("Crashed: EXC_BAD_ACCESS (SIGSEGV)", page)
        self.assertIn("about to &lt;crash&gt;", page)
        self.assertNotIn("about to <crash>", page)
        self.assertIn('href="runs/R1/logs/app.log"', page)


if __name__ == "__main__":
    unittest.main()
