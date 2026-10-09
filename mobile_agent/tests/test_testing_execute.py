"""`mobster test` end to end on verify's own fakes (a fake simulator manager and WebDriverAgent, as
test_verify_runner.py uses): real VerifyRuns, the suite, and its reports. Also each attempt's rules (key-less, no
key, a stop), the screen recorder's commands, the simulator limit for --parallel, and the approver a real iPhone
gets. Offline: no Xcode, simulator, network or key."""

import json
import os
from pathlib import Path
import signal
import tempfile
import threading
import unittest
from unittest import mock
from xml.etree import ElementTree as ET

from mobile_agent.testkit import api, execute as E
from mobile_agent.testkit import targets as T
from mobile_agent.tests.test_verify_runner import PLANS, Driver, Manager, RunnerCase
from mobile_agent.verify import runner
from mobile_agent.verify.checks import check_from_dict

BUNDLE = "dev.mobster.daybreak"


def validate_junit(data):
    """JUnit XML as CI readers (Jenkins, GitLab, GitHub test reporters) take it: the element and attribute rules of
    the common JUnit schema, with ``<properties>`` allowed on a test case, and counts that add up. Raises
    AssertionError."""
    root = ET.fromstring(data)
    assert root.tag == "testsuites", root.tag
    numeric = ("tests", "failures", "errors", "skipped")
    for name in numeric:
        int(root.get(name))
    float(root.get("time"))
    totals = dict.fromkeys(numeric, 0)
    for suite in root:
        assert suite.tag == "testsuite", suite.tag
        for name in ("name", "timestamp"):
            assert suite.get(name), name
        float(suite.get("time"))
        tags = [child.tag for child in suite]
        assert set(tags) <= {"properties", "testcase", "system-out", "system-err"}, tags
        assert tags.count("properties") <= 1 and (not tags.count("properties") or tags[0] == "properties"), tags
        counts = dict.fromkeys(numeric, 0)
        for case in suite.findall("testcase"):
            counts["tests"] += 1
            for name in ("classname", "name", "time"):
                assert case.get(name) is not None, name
            float(case.get("time"))
            kids = [child.tag for child in case]
            outcome = [tag for tag in kids if tag in ("failure", "error", "skipped")]
            assert len(outcome) <= 1, kids
            order = {"properties": 0, "failure": 1, "error": 1, "skipped": 1, "system-out": 2, "system-err": 3}
            assert all(tag in order for tag in kids), kids
            assert [order[tag] for tag in kids] == sorted(order[tag] for tag in kids), kids
            for prop in case.iter("property"):
                assert prop.get("name") and prop.get("value") is not None
            for tag in outcome:
                if tag == "failure":
                    counts["failures"] += 1
                    assert case.find("failure").get("type") and case.find("failure").get("message") is not None
                elif tag == "error":
                    counts["errors"] += 1
                else:
                    counts["skipped"] += 1
        for name in numeric:
            assert int(suite.get(name)) == counts[name], (name, suite.get(name), counts[name])
            totals[name] += counts[name]
    for name in numeric:
        assert int(root.get(name)) == totals[name], (name, root.get(name), totals[name])
    return root


class EndToEndTests(RunnerCase):
    def setUp(self):
        super().setUp()
        checks = self.root / ".mobster" / "checks"
        checks.mkdir(parents=True)
        (checks / "paywall.yaml").write_text(
            "version: 1\nname: The paywall shows <three> plans\napp: {bundle: dev.mobster.daybreak}\n"
            "launch: {url: 'daybreak://paywall'}\ntags: [smoke]\nexpect:\n  - text: Choose your plan\n")
        (checks / "restore.yaml").write_text(
            "version: 1\nname: Restore shows an error\napp: {bundle: dev.mobster.daybreak}\n"
            "expect:\n  - text: Restore failed\n")
        (checks / "onboarding.yaml").write_text(
            "version: 1\nname: Onboarding reaches the paywall\napp: {bundle: dev.mobster.daybreak}\n"
            "steps: [Complete onboarding.]\nexpect:\n  - text: Choose your plan\n")
        self.manager = Manager()
        self.driver = Driver([PLANS])
        self.watch(self.driver, self.manager)
        patch = mock.patch.object(runner, "build_driver", lambda target, m, run_dir, mode_: self.driver)
        patch.start()
        self.addCleanup(patch.stop)

    def run_suite(self, **options):
        planned = api.plan(api.Options(**options), cwd=self.root)
        attempts = E.Attempts(runs_dir=self.root / ".mobster" / "runs", keyless=options.get("keyless", False),
                              simulators=self.manager, assert_timeout=1)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "",
                                          "MOBSTER_SMART_MODEL": ""}):
            return api.execute(planned, attempts=attempts, cwd=self.root)

    def test_a_suite_on_real_verify_runs_writes_junit_html_and_json_with_the_exit_code(self):
        doc, paths = self.run_suite(retries=0, junit=str(self.root / "build" / "junit.xml"),
                                    html=str(self.root / "build" / "report"))
        results = {check["name"]: check["results"][0] for check in doc["checks"]}
        self.assertEqual(results["The paywall shows <three> plans"]["status"], "passed")
        self.assertEqual(results["Restore shows an error"]["status"], "failed")
        onboarding = results["Onboarding reaches the paywall"]
        self.assertEqual((onboarding["status"], onboarding["reason"]["class"]), ("couldnt_run", "usage"))
        self.assertIn("need Smart", onboarding["reason"]["message"])
        self.assertEqual(doc["exitCode"], 1)
        self.assertEqual(doc["schema"], "mobster.test/1")
        self.assertEqual([device["name"] for device in doc["devices"]], ["Default simulator",
                                                                           "iPhone 17 Pro · iOS 26.4"])
        # The folder is self-contained: every report a result names is a copy in it.
        folder = paths["folder"]
        self.assertEqual(folder.parent, self.root / ".mobster" / "test-results")
        passed = results["The paywall shows <three> plans"]
        self.assertRegex(passed["report"], r"^runs/\d{8}-\d{6}-[0-9a-f]{4}\.html$")
        self.assertTrue((folder / passed["report"]).is_file())
        self.assertTrue(passed["frame"].endswith("-verdict-ax.jpg"))
        on_disk = json.loads((folder / "results.json").read_text())
        self.assertEqual(on_disk["summary"], doc["summary"])
        self.assertEqual(set(on_disk["checks"][0]["results"][0]) >= {"device", "status", "attempts", "flaky",
                                                                       "report", "durationMs", "healed"}, True)
        # JUnit, in the suite folder and at --junit, valid and pointing at the copies.
        data = (folder / "junit.xml").read_bytes()
        self.assertEqual(data, (self.root / "build" / "junit.xml").read_bytes())
        root = validate_junit(data)
        self.assertEqual((root.get("tests"), root.get("failures"), root.get("skipped")), ("3", "2", "0"))
        case = next(c for c in root.iter("testcase") if c.get("name") == "The paywall shows <three> plans")
        report = next(p.get("value") for p in case.iter("property") if p.get("name") == "report")
        self.assertTrue((self.root / report).is_file())
        self.assertEqual(case.get("classname"), "checks.paywall")
        failure = next(c for c in root.iter("testcase") if c.get("name") == "Restore shows an error").find("failure")
        self.assertEqual(failure.get("type"), "assertion")
        # The HTML: escaped, self-contained, with the verdict frames, and copied with its runs to --html.
        page = (folder / "index.html").read_text()
        self.assertIn("The paywall shows &lt;three&gt; plans", page)
        self.assertNotIn("<three>", page)
        self.assertIn("default-src 'none'", page)
        self.assertNotRegex(page, r"(src|href)=\"https?:")
        self.assertIn('src="data:image/jpeg;base64,', page)
        self.assertTrue((self.root / "build" / "report" / "index.html").is_file())
        self.assertTrue((self.root / "build" / "report" / passed["report"]).is_file())
        self.assertEqual((self.root / ".mobster" / ".gitignore").read_text(), runner.GITIGNORE)

    def test_tags_and_keyless(self):
        doc, _ = self.run_suite(tags=("smoke",), keyless=True)
        self.assertEqual([check["name"] for check in doc["checks"]], ["The paywall shows <three> plans"])
        self.assertEqual(doc["exitCode"], 0)
        with self.assertRaisesRegex(api.UsageError, "No check has the tag nightly"):
            api.plan(api.Options(tags=("nightly",)), cwd=self.root)
        doc, _ = self.run_suite(paths=(".mobster/checks/onboarding.yaml",), keyless=True)
        reason = doc["checks"][0]["results"][0]["reason"]
        self.assertEqual(reason["class"], "usage")
        self.assertIn("--keyless", reason["message"])
        self.assertNotIn("acquire", self.manager.names()[-1:])


class AttemptRuleTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name) / ".mobster" / "runs"

    def check(self, **data):
        return check_from_dict({"bundle_id": BUNDLE, "expect": [{"text": "Hi"}], **data})

    def test_a_stopped_suite_and_steps_without_a_key_never_prepare(self):
        manager = Manager()
        attempts = E.Attempts(runs_dir=self.runs, simulators=manager)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "MOBSTER_SMART_MODEL": ""}):
            result = attempts(self.check(steps=["Open Settings"]), None, label="x")
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "usage"))
        attempts.stop.set()
        result = attempts(self.check(), None, label="x")
        self.assertEqual(result["reason"]["class"], "stopped")
        self.assertEqual(manager.names(), [])

    def test_a_real_iphone_gets_the_approver_that_refuses_every_commit(self):
        record = {"id": "usb-1", "kind": "usb", "name": "Sam's iPhone", "wdaUrl": "http://127.0.0.1:8100"}
        manager = E.Attempts(runs_dir=self.runs).manager_for(record)
        self.assertIs(runner.check_approver(manager), runner.deny_commits)
        self.assertEqual(runner.deny_commits({"act": "send", "label": "Send"}), "denied")
        sim = {"id": "sim-1", "kind": "simulator", "udid": "U-1", "name": "Mobster · iPhone 17 Pro"}
        pinned = E.Attempts(runs_dir=self.runs, simulators=Manager()).manager_for(sim)
        self.assertEqual(pinned.udid, "U-1")
        self.assertIs(runner.check_approver(pinned), runner.deny_money)

    def test_parallel_raises_the_simulator_limit_never_lowers_it(self):
        with mock.patch.dict(os.environ, {"MOBSTER_MAX_SIMS": "2", "MOBSTER_DATA_DIR": str(self.runs.parent)}):
            self.assertEqual(E.suite_simulators(4, None).max_sims, 4)
            self.assertEqual(E.suite_simulators(1, None).max_sims, 2)

    def test_the_recorder_starts_with_the_lease_and_stops_before_it_is_released(self):
        events = []

        class Process:
            def __init__(self, argv, **kwargs):
                events.append(("start", argv, kwargs.get("start_new_session")))
                self.alive = True

            def poll(self):
                return None if self.alive else 0

            def send_signal(self, number):
                events.append(("signal", number))
                self.alive = False

            def wait(self, timeout=None):
                return 0

        class Lease:
            target = type("Target", (), {"udid": "UDID-1"})()

            def release(self):
                events.append(("release",))

        class Sims:
            def acquire(self, device=None, runtime=None, **kwargs):
                return Lease()
            other = "kept"
        video = self.runs.parent / "videos" / "a.mp4"
        recording = E.Recording(Sims(), video, popen=Process)
        lease = recording.acquire("iPhone 17 Pro", None)
        self.assertEqual(recording.other, "kept")
        lease.release()
        lease.release()
        self.assertEqual(events, [("start", E.video_argv("UDID-1", video), True), ("signal", signal.SIGINT),
                                  ("release",), ("release",)])
        self.assertEqual(E.video_argv("U", "/v.mp4"), ["xcrun", "simctl", "io", "U", "recordVideo", "--codec=h264",
                                                         "--force", "/v.mp4"])

        def broken(argv, **kwargs):
            raise FileNotFoundError("xcrun")
        lease = E.Recording(Sims(), video, popen=broken).acquire()
        lease.release()  # no recording, and the run goes on

    def test_abort_all_stops_runs_still_preparing(self):
        aborted = threading.Event()

        class Run:
            state = "preparing"

            def abort(self, klass, message, fix=""):
                aborted.set()
        attempts = E.Attempts(runs_dir=self.runs)
        attempts._live.add(Run())
        attempts.abort_all()
        self.assertTrue(attempts.stop.is_set())
        self.assertTrue(aborted.wait(2))


class DeviceTargetTests(RunnerCase):
    def test_an_app_store_app_on_the_phone_is_couldnt_run_with_no_tap(self):
        """Acceptance 5: on a real device, a check naming an App Store bundle is couldnt_run before any tap."""
        checks = self.root / ".mobster" / "checks"
        checks.mkdir(parents=True)
        (checks / "store.yaml").write_text("version: 1\nname: Store app\napp: {bundle: com.example.storeapp}\n"
                                           "expect: [{text: Hi}]\n")
        phone = {"id": "usb-1", "udid": "00008140-1", "kind": "usb", "name": "Sam's iPhone",
                 "wdaUrl": "http://127.0.0.1:8100"}

        def devicectl(argv):
            out = Path(argv[argv.index("--json-output") + 1])
            out.write_text(json.dumps({"result": {"apps": [{"bundleIdentifier": "com.example.storeapp",
                                                            "name": "Store App", T.DEVELOPER_FIELD: False}]}}))
            return 0, ""
        calls = []
        planned = api.plan(api.Options(devices=("Sam's iPhone",)), cwd=self.root,
                           named_device=lambda name: phone if name == "Sam's iPhone" else None)
        doc, _ = api.execute(planned, attempts=lambda *a, **k: calls.append(a), cwd=self.root,
                             gate=T.DeviceGate(runner=devicectl, allowed=lambda: True))
        result = doc["checks"][0]["results"][0]
        self.assertEqual(calls, [])
        self.assertEqual((result["status"], result["attempts"]), ("couldnt_run", 0))
        self.assertEqual(result["reason"]["message"],
                         "Mobster tests your own apps on your iPhone. Store App is from the App Store.")
        self.assertEqual(result["device"], "Sam's iPhone")


if __name__ == "__main__":
    unittest.main()
