"""A missing intermediate AX read cannot certify every-screen checks or an accessibility baseline."""

import threading
from dataclasses import replace
from pathlib import Path
from unittest import mock

from mobile_agent.tests import replay_world as W
from mobile_agent.tests.test_replay_core import Project
from mobile_agent.tests.test_verify_fixtures import app, node
from mobile_agent.tests.test_verify_runner import BUNDLE, Driver, Manager, RunnerCase
from mobile_agent.verify.checks import check_from_dict
from mobile_agent.verify.l10n import Collector as LocaleCollector
from mobile_agent.verify.runner import VerifyRun
from mobile_agent.verify.tree import TEXT_LIMIT, parse_tree

READY = app(node("StaticText", "Ready"), bundle=BUNDLE)
BROKEN = app(node("StaticText", "Ready"), node("StaticText", "Payment failed"), bundle=BUNDLE)


class ScreenCoverageTests(RunnerCase):
    def test_locale_audit_rechecks_a_subpoint_clipping_change(self):
        collector = LocaleCollector("de")

        def screen(width):
            return parse_tree(app(node("StaticText", "Datenschutzerklärung", rect=(100, 100, width, 30)),
                                  bundle=BUNDLE))
        self.assertTrue(collector.add(screen(302.9)))
        self.assertEqual(collector.findings(), [])
        self.assertTrue(collector.add(screen(303.1)))
        self.assertEqual([item["id"] for item in collector.findings()], ["text-truncated"])

    def test_glance_does_not_call_an_enabled_only_transition_stable(self):
        disabled = app(node("Button", "Continue", enabled=False), bundle=BUNDLE)
        enabled = app(node("Button", "Continue", enabled=True), bundle=BUNDLE)
        run = VerifyRun(self.check(expect=[{"visible": {"role": "button", "enabled": True}}]),
                        mode="launch", runs_dir=self.runs)
        run.driver = Driver([disabled, enabled, disabled, disabled])
        results, _, stable = run._settle(run.check.expect, timeout=1)
        self.assertFalse(results[0].ok)
        self.assertTrue(stable)
        self.assertGreaterEqual(run.driver.reads, 3)

    def ready(self, *, throughout=(), a11y=None):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "Ready"}],
                                 "throughout": list(throughout), **({"a11y": a11y} if a11y else {})})
        driver = Driver([READY])
        run, _ = self.keyless(check, driver)
        return run, driver

    def test_oversized_tree_refuses_a_negative_assertion_instead_of_passing(self):
        screen = app(node("StaticText", "x" * TEXT_LIMIT + "Payment failed"), bundle=BUNDLE)
        result, _ = self.verify(self.check(expect=[{"no_text": "Payment failed"}]), Driver([screen]), mode="launch")
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "wda"))

    def test_failed_intermediate_read_prevents_a_clean_final_screen_from_certifying_throughout(self):
        run, driver = self.ready(throughout=[{"no_text": "Payment failed"}])
        driver.screens, driver.reads = [ConnectionError("WDA read lost"), ConnectionError("WDA read lost"), READY], 0
        run._observe_screen(None)
        result = run.finish()
        self.assertEqual((result["verdict"], result["reason"]["class"]),
                         ("needs_review", "screen_coverage"))
        self.assertEqual(result["screenCoverage"], {"complete": False, "a11yScreens": 0})

    def test_an_observed_violation_still_fails_when_a_later_read_is_missing(self):
        run, driver = self.ready(throughout=[{"no_text": "Payment failed"}])
        driver.screens, driver.reads = [BROKEN], 0
        run._observe_screen(None)
        driver.screens, driver.reads = [ConnectionError("WDA read lost"), ConnectionError("WDA read lost"), READY], 0
        run._observe_screen(None)
        result = run.finish()
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "finding"))
        self.assertFalse(result["screenCoverage"]["complete"])

    def test_keyless_step_frame_sweeps_the_intermediate_screen(self):
        run, driver = self.ready(throughout=[{"no_text": "Payment failed"}])
        driver.screens, driver.reads = [BROKEN], 0
        run.frame("step")
        driver.screens, driver.reads = [READY], 0
        result = run.finish()
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "finding"))
        self.assertTrue(result["screenCoverage"]["complete"])

    def test_strict_audit_refuses_missing_evidence_while_warn_stays_advisory(self):
        for level, verdict in (("strict", "needs_review"), ("warn", "passed")):
            with self.subTest(level=level):
                run, driver = self.ready(a11y=level)
                driver.screens, driver.reads = [ConnectionError("WDA read lost"), ConnectionError("WDA read lost"),
                                                READY], 0
                run._observe_screen(None)
                result = run.finish()
                self.assertEqual(result["verdict"], verdict)
                self.assertEqual(result["screenCoverage"], {"complete": False, "a11yScreens": 1})

    def test_audit_cap_makes_baseline_coverage_incomplete(self):
        run, driver = self.ready(a11y="strict")
        run._a11y.LIMIT = 1
        driver.screens, driver.reads = [app(node("StaticText", "Other"), bundle=BUNDLE), READY], 0
        run._observe_screen(None)
        result = run.finish()
        self.assertEqual((result["verdict"], result["reason"]["class"]),
                         ("needs_review", "screen_coverage"))
        self.assertEqual(result["screenCoverage"], {"complete": False, "a11yScreens": 1})

    def test_complete_audit_has_a_positive_baseline_marker(self):
        run, _ = self.ready(a11y="strict")
        result = run.finish()
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["screenCoverage"], {"complete": True, "a11yScreens": 1})

    def test_early_verdict_error_cannot_claim_the_launch_audit_is_complete(self):
        run, _ = self.ready(a11y="warn")
        self.assertEqual(run._a11y.screens, 1)
        run.error = {"klass": "wda", "message": "WebDriverAgent stopped answering.", "fix": ""}
        result = run.finish()
        self.assertEqual(result["verdict"], "couldnt_run")
        self.assertEqual(result["screenCoverage"], {"complete": False, "a11yScreens": 1})

    def test_in_flight_step_capture_cannot_certify_a_clean_final_audit(self):
        run, _ = self.ready(a11y="strict")
        entered, release = threading.Event(), threading.Event()
        screenshot = run.manager.screenshot

        def paused(target, path, **kwargs):
            if Path(path).name.endswith("-step.jpg"):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("paused step frame")
            return screenshot(target, path, **kwargs)

        with mock.patch.object(run.manager, "screenshot", paused):
            worker = threading.Thread(target=lambda: run.frame("step"), daemon=True)
            worker.start()
            self.assertTrue(entered.wait(2))
            try:
                result = run.finish()
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual((result["verdict"], result["reason"]["class"]),
                         ("needs_review", "screen_coverage"))
        self.assertEqual(result["screenCoverage"], {"complete": False, "a11yScreens": 1})

    def test_reserved_unscheduled_step_capture_cannot_certify_audit(self):
        run, _ = self.ready(a11y="strict")
        release = run.reserve_step_frame()  # keyless tool reserves before starting its worker
        self.assertIsNotNone(release)
        result = run.finish()
        release()
        release()
        self.assertEqual((result["verdict"], result["reason"]["class"]),
                         ("needs_review", "screen_coverage"))
        self.assertFalse(result["screenCoverage"]["complete"])
        self.assertIsNone(run.frame("step"))  # a delayed worker cannot add post-verdict evidence

    def test_failed_audit_evaluation_is_not_a_clean_scan(self):
        run, _ = self.ready(a11y="strict")
        with mock.patch.object(run._a11y, "add", side_effect=ValueError("collector failed")):
            run._observe_screen(None, parse_tree(READY))
        result = run.finish()
        self.assertEqual((result["verdict"], result["reason"]["class"]),
                         ("needs_review", "screen_coverage"))
        self.assertFalse(result["screenCoverage"]["complete"])

    def test_missing_screenshot_does_not_discard_available_ax_evidence(self):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "Ready"}], "a11y": "strict"})
        run, _ = self.keyless(check, Driver([READY]), Manager(fail={"screenshot": OSError("no image")}))
        result = run.finish()
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["screenCoverage"], {"complete": True, "a11yScreens": 1})

    def test_synthetic_smart_and_cached_replay_cannot_certify_full_a11y_coverage(self):
        project = Project(self, check=replace(W.check(), a11y="warn"))
        first = project.run()
        self.assertEqual(first["verdict"], "passed", first["reason"])
        self.assertFalse(first["screenCoverage"]["complete"])
        second = project.run()
        self.assertEqual(second["replay"]["mode"], "replayed")
        # This fake's observe() returns a synthetic snapshot without a matching source XML read.
        self.assertFalse(second["screenCoverage"]["complete"])
        self.assertGreaterEqual(second["screenCoverage"]["a11yScreens"], 1)
