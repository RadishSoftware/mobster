"""Oracle grading logic with a fake probe. No device; the harness path stays live-only."""

import unittest

from mobile_agent.errors import MobsterError
from mobile_agent.evals.oracles import (
    Abstained, ActionsAtLeast, ActionsAtMost, CitationsOnDevice, ForegroundApp,
    NavigationTitle, ProbeUnavailable, ReturnedValue, ScreenLacks, ScreenShows, StatusIn,
    navigation_title,
)
from mobile_agent.evals.tasks import COMPLETED, calculator_task, suite
from mobile_agent.state import Element, Snapshot

BUNDLE = "com.apple.Preferences"


def snapshot(text, elements=(), bundle_id=BUNDLE):
    return Snapshot(list(elements), text, 402, 874, "wda", bundle_id=bundle_id)


def title_element(label):
    return Element("t", label, "StaticText", (.3, .05, .4, .04), actions=())


class FakeProbe:
    def __init__(self, observed):
        self.observed = observed

    def observe(self, bundle_id, timeout=10):
        return self.observed


def run(summary=None, status="completed_unverified"):
    return {"status": status, "summary": summary or {}}


class CitationOracleTests(unittest.TestCase):
    def test_quotes_present_on_device_pass(self):
        probe = FakeProbe(snapshot("iOS 26.6.1"))
        oracle = CitationsOnDevice(BUNDLE)
        ok, detail = oracle.check(run({"data": {"v": "26.6.1"},
                                       "citations": [{"quote": "26.6.1"}]}), probe, None)
        self.assertTrue(ok)
        self.assertIn("1/1", detail)

    def test_quotes_missing_from_device_fail(self):
        probe = FakeProbe(snapshot("Something else"))
        ok, detail = CitationsOnDevice(BUNDLE).check(run({"data": {"v": "26.6.1"},
            "citations": [{"quote": "26.6.1"}]}), probe, None)
        self.assertFalse(ok)
        self.assertIn("0/1", detail)

    def test_answer_without_citations_fails(self):
        probe = FakeProbe(snapshot("26.6.1"))
        ok, detail = CitationsOnDevice(BUNDLE).check(
            run({"data": {"v": "26.6.1"}, "citations": []}), probe, None)
        self.assertFalse(ok)
        self.assertIn("without citations", detail)

    def test_no_answer_needs_no_citations(self):
        probe = FakeProbe(snapshot("26.6.1"))
        ok, _ = CitationsOnDevice(BUNDLE).check(
            run({"data": None, "data_status": "insufficient_evidence"}), probe, None)
        self.assertTrue(ok)


class ValueAndAbstentionTests(unittest.TestCase):
    def test_returned_value_exact_match_with_path(self):
        oracle = ReturnedValue("26.6.1", "/ios_version")
        ok, _ = oracle.check(run({"data": {"ios_version": "26.6.1"}}), None, None)
        self.assertTrue(ok)
        ok, _ = oracle.check(run({"data": {"ios_version": "26.6"}}), None, None)
        self.assertFalse(ok)
        ok, _ = oracle.check(run({"data": None}), None, None)
        self.assertFalse(ok)

    def test_abstention_accepts_only_evidence_failures(self):
        for status in ("insufficient_evidence", "no_observed_evidence", "not_extracted",
                       "helper_unavailable", "extraction_failed", "unsupported_answer"):
            ok, _ = Abstained().check(run({"data": None, "data_status": status}), None, None)
            self.assertTrue(ok, status)
        ok, _ = Abstained().check(run({"data": {"a": 1}, "data_status": "extracted"}), None, None)
        self.assertFalse(ok)
        ok, _ = Abstained().check(run({"data": None, "data_status": "extracted"}), None, None)
        self.assertFalse(ok)


class NavigationOracleTests(unittest.TestCase):
    def test_nav_title_reads_the_title_band(self):
        elements = [title_element("About"),
                    Element("r", "About", "Button", (.1, .5, .8, .06))]
        self.assertEqual(navigation_title(snapshot("rows", elements)), "About")

    def test_row_label_is_not_a_location(self):
        elements = [title_element("General"),
                    Element("r", "About", "Button", (.1, .5, .8, .06))]
        oracle = NavigationTitle(BUNDLE, "About")
        ok, detail = oracle.check(run(), FakeProbe(snapshot("rows", elements)), None)
        self.assertFalse(ok)
        self.assertIn("General", detail)

    def test_foreground_checks_bundle(self):
        ok, _ = ForegroundApp(BUNDLE).check(run(), FakeProbe(snapshot("x")), None)
        self.assertTrue(ok)
        ok, _ = ForegroundApp("other.app").check(run(), FakeProbe(snapshot("x")), None)
        self.assertFalse(ok)

    def test_screen_shows_and_lacks(self):
        probe = FakeProbe(snapshot("Airplane Mode"))
        ok, _ = ScreenShows(BUNDLE, "airplane mode").check(run(), probe, None)
        self.assertTrue(ok)
        ok, _ = ScreenLacks(BUNDLE, "airplane mode").check(run(), probe, None)
        self.assertFalse(ok)


class StatusAndActionTests(unittest.TestCase):
    def test_status_membership(self):
        ok, _ = StatusIn(COMPLETED).check(run(status="completed_unverified"), None, None)
        self.assertTrue(ok)
        ok, _ = StatusIn(COMPLETED).check(run(status="blocked"), None, None)
        self.assertFalse(ok)

    def test_action_bounds(self):
        ok, _ = ActionsAtLeast(2).check(run({"actions": 2}), None, None)
        self.assertTrue(ok)
        ok, _ = ActionsAtLeast(2).check(run({"actions": 1}), None, None)
        self.assertFalse(ok)
        ok, _ = ActionsAtMost(0).check(run({"actions": 0}), None, None)
        self.assertTrue(ok)
        ok, _ = ActionsAtMost(0).check(run({"actions": 1}), None, None)
        self.assertFalse(ok)
        ok, _ = ActionsAtMost(0).check(run({}), None, None)
        self.assertFalse(ok)


class SuiteShapeTests(unittest.TestCase):
    def test_suite_declares_seven_tasks_across_four_categories(self):
        tasks = suite(which="settings")
        self.assertEqual(len(tasks), 7)
        self.assertEqual({t.category for t in tasks},
                         {"retrieval", "navigation", "abstention", "precision"})
        self.assertEqual(len({t.id for t in tasks}), 7)

    def test_retrieval_tasks_cite_their_answers(self):
        retrieval = [t for t in suite() if t.category == "retrieval"]
        self.assertEqual(len(retrieval), 2)
        for task in retrieval:
            kinds = {type(o).__name__ for o in task.oracles}
            self.assertIn("ReturnedValue", kinds)
            self.assertIn("CitationsOnDevice", kinds)

    def test_calculator_stays_a_documented_gap(self):
        task = calculator_task()
        self.assertEqual(task.category, "known_gap")

    def test_probe_failure_is_a_domain_error(self):
        self.assertTrue(issubclass(ProbeUnavailable, MobsterError))
        self.assertIs(ProbeUnavailable.retryable, False)


WDA_ABOUT = ('<XCUIElementTypeApplication width="400" height="800">'
             '<XCUIElementTypeNavigationBar label="About" x="0" y="50" width="400" height="50">'
             '<XCUIElementTypeButton label="General" x="0" y="55" width="90" height="40"/>'
             '</XCUIElementTypeNavigationBar>'
             '<XCUIElementTypeButton label="iOS Version, 26.0.1" x="0" y="200" width="400" height="44"/>'
             '</XCUIElementTypeApplication>')


class WDAProbeTests(unittest.TestCase):
    def probe(self, *foregrounds):
        from mobile_agent.evals.oracles import WDAProbe
        probe = WDAProbe("http://127.0.0.1:8100", session="s")
        self.addCleanup(probe.close)
        apps = iter(foregrounds)
        def request(method, path, body=None, timeout=10):
            if path == "/wda/activeAppInfo":
                return {"value": {"bundleId": next(apps)}}
            return {"value": WDA_ABOUT}
        probe.http.request = request
        return probe

    def test_reads_are_sessionless_and_stamped_with_the_foreground_app(self):
        snapshot = self.probe("com.apple.Preferences", "com.apple.Preferences").observe("com.apple.Preferences")
        self.assertEqual(snapshot.bundle_id, "com.apple.Preferences")
        self.assertEqual(navigation_title(snapshot), "About")

    def test_a_read_that_straddles_an_app_switch_is_not_graded(self):
        with self.assertRaises(ProbeUnavailable):
            self.probe("com.apple.Preferences", "com.apple.springboard").observe("com.apple.Preferences")

    def test_wrong_foreground_fails_the_foreground_oracle(self):
        probe = self.probe("com.apple.springboard", "com.apple.springboard")
        ok, _ = ForegroundApp("com.apple.Preferences").check({}, probe, None)
        self.assertFalse(ok)

    def test_back_button_is_the_leading_navigation_bar_button(self):
        from mobile_agent.evals.oracles import navigation_back
        from mobile_agent.state import from_wda
        back = navigation_back(from_wda(WDA_ABOUT))
        self.assertEqual(back.label, "General")


class DeviceTruthTests(unittest.TestCase):
    def test_every_device_has_a_complete_suite(self):
        from mobile_agent.evals.tasks import DEVICES
        for device in DEVICES:
            with self.subTest(device=device):
                tasks = suite(device)
                self.assertEqual(len({t.id for t in tasks}), len(tasks))
                self.assertLessEqual({"retrieval", "navigation", "abstention", "precision"},
                                     {t.category for t in tasks})

    def test_web_tasks_start_in_safari_and_check_the_answer(self):
        web = [t for t in suite("iphone15pro") if t.id.startswith("web.")]
        self.assertGreaterEqual(len(web), 7)
        for task in web:
            self.assertEqual(task.bundle_id, "com.apple.mobilesafari")
            self.assertTrue(task.reset_url.startswith("https://"))
            kinds = {type(o).__name__ for o in task.oracles}
            self.assertTrue(kinds & {"ReturnedValue", "ReturnedMatches", "Abstained"}, task.id)

    def test_battery_cycles_is_not_an_abstention_on_a_phone_that_shows_it(self):
        self.assertNotIn("settings.absent.battery_cycles", {t.id for t in suite("iphone15pro")})
