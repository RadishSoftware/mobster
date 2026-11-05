"""A verification run audits XML reads made inside WDA, including reads without a saved frame."""

from types import SimpleNamespace
from unittest import mock

from mobile_agent import costs, engines
from mobile_agent.drivers import WDA
from mobile_agent.tests.test_verify_fixtures import app, node
from mobile_agent.tests.test_verify_runner import BUNDLE, Manager, RunnerCase
from mobile_agent.verify import runner
from mobile_agent.verify.checks import check_from_dict
from mobile_agent.verify.runner import VerifyRun
from mobile_agent.verify.source_observation import SourceObservation, _xml_source
from mobile_agent.wda.observe import WDA_SOURCE_PATH

READY = app(node("StaticText", "Ready"), bundle=BUNDLE)
BROKEN = app(node("StaticText", "Ready"), node("StaticText", "Payment failed"), bundle=BUNDLE)


class SourceObservationTests(RunnerCase):
    def source_run(self, *, screens=(READY,), a11y=None, throughout=True):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "Ready"}],
                                 **({"throughout": [{"no_text": "Payment failed"}]} if throughout else {}),
                                 **({"a11y": a11y} if a11y else {})})
        driver = WDA("http://127.0.0.1:8100", "source-test")
        driver.active_app = lambda timeout=5: BUNDLE
        requested = []
        remaining = list(screens)

        def request(method, path, body=None, timeout=None):
            requested.append((method, path))
            if method == "GET" and path.startswith("/session/source-test/source?"):
                xml = remaining.pop(0) if len(remaining) > 1 else remaining[0]
                return {"value": xml}
            if (method, path) == ("GET", "/session/source-test/alert/text"):
                return {"value": ""}
            raise AssertionError(f"unexpected WDA request {method} {path}")

        driver.http.request = request
        manager = Manager()
        self.watch(driver, manager)
        run = VerifyRun(check, mode="keyless", runs_dir=self.runs, manager=manager)
        with mock.patch.object(runner, "build_driver", lambda *args: driver):
            run.prepare()
        self.addCleanup(run._release)
        return run, driver, requested, remaining

    def test_internal_wda_observe_failure_survives_a_clean_final_screen(self):
        run, driver, requested, remaining = self.source_run(screens=(READY, BROKEN, READY))
        before = len(requested)
        driver.observe()  # An internal Smart or replay read, without a screenshot callback.
        self.assertEqual(len(requested) - before, 1)  # Full attributes ride the existing source request.
        self.assertEqual(remaining, [READY])
        result = run.finish()
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "finding"))
        self.assertTrue(result["screenCoverage"]["complete"])
        self.assertTrue(any("excluded_attributes=accessible,index" in path for _, path in requested))
        self.assertTrue(any(item["evidence"] == "an observed accessibility screen" for item in result["findings"]))

    def test_settle_source_catches_a_bad_transient_before_the_final_clean_tree(self):
        run, _, _, _ = self.source_run(screens=(READY, BROKEN, READY))
        result = run.finish()
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "finding"))
        self.assertTrue(result["screenCoverage"]["complete"])

    def test_default_wda_grounding_path_is_unchanged_without_per_screen_checks(self):
        run, driver, requested, _ = self.source_run(throughout=False)
        self.assertIsNone(run.source_observation)
        driver.observe()
        self.assertIn(("GET", "/session/source-test" + WDA_SOURCE_PATH), requested)
        self.assertEqual(run.finish()["verdict"], "passed")

    def test_full_visibility_does_not_turn_a_hidden_control_into_a_target(self):
        hidden = app(node("StaticText", "Ready"), node("Button", "Hidden", visible=False), bundle=BUNDLE)
        run, driver, _, _ = self.source_run(screens=(READY, hidden, READY))
        snapshot = driver.observe()
        self.assertNotIn("Hidden", [element.label for element in snapshot.elements])
        self.assertEqual(run.finish()["verdict"], "passed")

    def test_a_matching_saved_frame_enriches_earlier_raw_source_evidence(self):
        run, driver, _, remaining = self.source_run(screens=(READY,))
        remaining[:] = [BROKEN, BROKEN, READY]   # the screens after the launch, however many reads it took (perf.py)
        driver.observe()
        run.frame("step")
        result = run.finish()
        violation = next(item for item in result["findings"] if item["level"] == "fail")
        self.assertIn("-step.jpg", violation["frame"])
        self.assertEqual(violation["evidence"], violation["frame"])

    def test_replay_reads_are_audited_without_a_separate_capture(self):
        run, driver, _, _ = self.source_run(screens=(READY, BROKEN, READY))
        run.mode = "smart"
        driver.tune = lambda **kwargs: None

        def replay(_cancelled):
            driver.observe()
            return SimpleNamespace(status="done", model_status=None, cost_usd=0, reason="", heals=(), replayed=1)

        run.replay = SimpleNamespace(path={"steps": [{}]}, replay=replay, after=lambda result: None)
        run.run_replay()
        result = run.finish()
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "finding"))
        self.assertTrue(result["screenCoverage"]["complete"])

    def test_smart_internal_observe_is_audited_without_a_prompt_frame(self):
        run, driver, _, _ = self.source_run(screens=(READY, BROKEN, READY))
        run.mode, run.key = "smart", "sk-test-never-sent"

        class Agent:
            max_seconds = None

            def run(self, request):
                driver.observe()
                return {"status": "completed", "steps": 1, "usage": {}, "answer": "Done."}

        client = SimpleNamespace(model="test", usage={}, model_failed=False)
        with mock.patch.object(engines, "build_client", return_value=client), \
                mock.patch.object(engines, "build_frontier", return_value=Agent()), \
                mock.patch.object(costs, "meter"):
            run.run_smart()
        result = run.finish()
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "finding"))
        self.assertTrue(result["screenCoverage"]["complete"])

    def test_pixel_observe_makes_strict_audit_incomplete(self):
        run, driver, _, _ = self.source_run(a11y="strict")
        original = driver._slow_screen
        driver._slow_screen = lambda deadline: SimpleNamespace(source="wda_pixels")
        try:
            driver.observe()
        finally:
            driver._slow_screen = original
        result = run.finish()
        self.assertEqual((result["verdict"], result["reason"]["class"]),
                         ("needs_review", "screen_coverage"))
        self.assertFalse(result["screenCoverage"]["complete"])

    def test_detach_restores_own_bound_methods_after_close(self):
        run, driver, _, _ = self.source_run()
        adapter = run.source_observation
        self.assertIsNotNone(adapter)
        self.assertIs(getattr(driver, "_verify_source_owner"), adapter)
        run.finish()
        self.assertFalse(hasattr(driver, "_verify_source_owner"))
        self.assertNotIn("observe", driver.__dict__)
        self.assertNotIn("call", driver.__dict__)

    def test_only_xml_source_is_upgraded_and_other_query_options_survive(self):
        self.assertEqual(_xml_source("/source?format=xml&foo=bar&excluded_attributes=visible,traits"),
                         "/source?format=xml&foo=bar&excluded_attributes=accessible,index")
        for path in ("/source?format=json", "/source?format=xml&format=json", "/other?format=xml",
                     "https://example.test/source?format=xml"):
            with self.subTest(path=path):
                self.assertIsNone(_xml_source(path))

    def test_a_source_read_pending_when_verdict_starts_cannot_certify_coverage(self):
        run, driver, _, _ = self.source_run()
        release = run._reserve_source_read()
        self.assertIsNotNone(release)
        run._seal_screen_frames()
        release()  # The read finishes before the final source read and collector export.
        result = run.finish()
        self.assertEqual((result["verdict"], result["reason"]["class"]),
                         ("needs_review", "screen_coverage"))
        self.assertFalse(result["screenCoverage"]["complete"])


class UnsupportedDriverTests(RunnerCase):
    def test_unobservable_driver_cannot_certify_an_audit(self):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "a11y": "strict"})
        run = VerifyRun(check, mode="launch", runs_dir=self.runs)
        self.assertFalse(SourceObservation(run).attach(object()))
        self.assertIsNotNone(run._screen_coverage_issue)

    def test_slotted_driver_is_refused_without_changing_its_callbacks(self):
        class Slotted:
            __slots__ = ("call", "observe", "_verify_source_owner")

            def __init__(self):
                self.call = lambda *args, **kwargs: READY
                self.observe = lambda *args, **kwargs: None
                self._verify_source_owner = None

        check = check_from_dict({"app": {"bundle": BUNDLE}, "a11y": "strict"})
        run = VerifyRun(check, mode="launch", runs_dir=self.runs)
        driver = Slotted()
        original_call, original_observe = driver.call, driver.observe
        adapter = SourceObservation(run)
        self.assertFalse(adapter.attach(driver))
        self.assertFalse(adapter.active)
        self.assertIs(driver.call, original_call)
        self.assertIs(driver.observe, original_observe)
        self.assertIsNone(driver._verify_source_owner)
        self.assertIsNotNone(run._screen_coverage_issue)
