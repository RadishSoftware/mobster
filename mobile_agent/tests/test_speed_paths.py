"""Fewer model calls without weaker gates: text selection, same-screen completion,
speculative decisions during settling. Offline."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from mobile_agent.agent import Agent, decision_key
from mobile_agent.drivers import WDA
from mobile_agent.models import (Decision, Jev, TEXT_SELECTION_FLOOR, request_text_candidates,
                                 selected_text)
from mobile_agent.state import Element, Snapshot, from_wda
from mobile_agent.task_policy import ActionSupport, StopGate
from mobile_agent.tests.test_task_policy import choice


# These tests exercise the model's speed paths on "Open General"; the route
# compiler (routes.py, tested in test_routes.py) would answer those hops itself.
_ROUTES_OFF = None


def setUpModule():
    global _ROUTES_OFF
    import os as _os
    from unittest.mock import patch as _patch
    _ROUTES_OFF = _patch.dict(_os.environ, {"MOBSTER_ROUTES": "0"})
    _ROUTES_OFF.start()


def tearDownModule():
    _ROUTES_OFF.stop()



def decision(operation, target=None, *, goal=.05, text=None, confidence=.97):
    return Decision(operation, target, confidence, goal, .02, "t", 0, {}, StopGate.CONTINUE,
                    risk_tier="navigation" if operation == "TAP" else None, side_effect_risk=.01, text=text)


class TextCandidateTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        flag = patch.dict("os.environ", {"MOBSTER_TEXT_SELECTION": "1"})  # opt-in since 24 Sep
        flag.start()
        self.addCleanup(flag.stop)

    def test_search_query_address_and_quote_are_offered_verbatim(self):
        request = "Search the web for Ada Lovelace, open her Wikipedia article, and report the year she was born."
        self.assertEqual(request_text_candidates(request)[0], "Ada Lovelace")
        self.assertIn("en.m.wikipedia.org/wiki/Guido_van_Rossum", request_text_candidates(
            "In Safari, go to en.m.wikipedia.org/wiki/Guido_van_Rossum and report the year he was born."))
        self.assertEqual(request_text_candidates('Reply to Sam saying "running 10 minutes late"')[0],
                         "running 10 minutes late")
        self.assertEqual(request_text_candidates("Create a note titled Groceries with the text 'eggs, milk'")[:2],
                         ["eggs, milk", "Groceries"])

    def test_every_candidate_is_an_exact_span_without_cut_quotations(self):
        requests = ("Search Wikipedia for the Eiffel Tower and report its height",
                    "Get directions to Golden Gate Park, then report the travel time",
                    "Search for Joe's Pizza", "Create a note titled Groceries with the text 'eggs, milk'")
        for request in requests:
            for candidate in request_text_candidates(request):
                self.assertIn(candidate, request)
                self.assertNotIn('"', candidate)
        self.assertIn("Eiffel Tower", request_text_candidates(requests[0]))
        self.assertIn("Joe's Pizza", request_text_candidates(requests[2]))

    def test_selection_needs_a_confident_strict_winner(self):
        candidates = ["Ada Lovelace", "open her article"]

        def answer(pick, confidence, split=None):
            probabilities = split or {"t0": 0.0, "t1": 0.0, "none": 0.0}
            if split is None:
                probabilities[pick] = 1.0
            return {"type": "choice", "choice": pick, "confidence": confidence, "probabilities": probabilities}

        self.assertEqual(selected_text(answer("t0", .95), candidates), "Ada Lovelace")
        self.assertIsNone(selected_text(answer("t0", TEXT_SELECTION_FLOOR - .01), candidates))
        self.assertIsNone(selected_text(answer("none", .99), candidates))
        self.assertIsNone(selected_text(answer("t0", .9, {"t0": .5, "t1": .5, "none": 0}), candidates))
        self.assertIsNone(selected_text({"garbage": True}, candidates))

    def test_decision_carries_text_only_for_text_operations(self):
        with self.assertRaises(ValueError):
            decision("TAP", "0", text="Ada Lovelace")
        with self.assertRaises(ValueError):
            decision("TYPE", "0", text="line\nbreak")
        self.assertEqual(decision("TYPE", "0", text="Ada Lovelace").text, "Ada Lovelace")


FIELD = '<XCUIElementTypeTextField label="Address" value="" x="10" y="440" width="380" height="40"/>'


def app(*children):
    return '<XCUIElementTypeApplication width="400" height="800">' + "".join(children) + "</XCUIElementTypeApplication>"


class JevTextQuestionTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        flag = patch.dict("os.environ", {"MOBSTER_TEXT_SELECTION": "1"})  # opt-in since 24 Sep
        flag.start()
        self.addCleanup(flag.stop)

    def decide(self, *, can_type=True, text_pick="t0", operation="TYPE_SUBMIT"):
        model = Jev("offline-test-only")
        self.addCleanup(model.close)
        asked = {}

        def reply(_method, _path, body, _timeout):
            asked.update(body["questions"])
            answers = {}
            for name, question in body["questions"].items():
                if question["type"] == "noul":
                    answers[name] = {"type": "noul", "noul": 0}
                elif question["type"] == "score":
                    answers[name] = {"type": "score", "score": 0}
                else:
                    pick = {"operation": operation, "stop_gate": "continue", "type_text": text_pick}.get(name)
                    answers[name] = choice(question, pick or next(iter(question["criteria"])))
            return {"model": "offline-test-only", "answers": answers}
        model.http.request = Mock(side_effect=reply)
        result = model.decide(from_wda(app(FIELD)), "Search the web for Ada Lovelace", [], can_type=can_type)
        return result, asked

    def test_text_rides_in_the_decision_call(self):
        result, asked = self.decide()
        self.assertIn("type_text", asked)
        self.assertEqual((result.operation, result.text), ("TYPE_SUBMIT", "Ada Lovelace"))

    def test_abstention_leaves_text_to_the_helper(self):
        result, _ = self.decide(text_pick="none")
        self.assertEqual((result.operation, result.text), ("TYPE_SUBMIT", None))

    def test_no_text_question_without_text_entry(self):
        result, asked = self.decide(can_type=False, operation="WAIT")
        self.assertNotIn("type_text", asked)
        self.assertIsNone(result.text)

    def test_other_operations_drop_the_selected_text(self):
        result, _ = self.decide(operation="WAIT")
        self.assertIsNone(result.text)


class TextSelectionAgentTests(unittest.TestCase):
    def run_agent(self, *, text, value="", helper=None):
        field = Element("0", "Address", "TextField", (0, .5, 1, .05), True, "/f[1]", value,
                        ("TAP", "TYPE", "TYPE_SUBMIT"))
        screen = Snapshot([field], "Address", 400, 800, "wda")
        after = Snapshot([Element("0", "Ada Lovelace - Wikipedia", "StaticText", (0, .1, 1, .05), locator="/t[1]")],
                         "Ada Lovelace", 400, 800, "wda")
        reads = iter([screen, screen, screen, screen, after, after, after, after])
        driver = SimpleNamespace(can_type=True, execute=Mock(), observe=lambda timeout=10: next(reads))
        model = Mock(spec=["decide", "verify_action"])
        model.decide.side_effect = [decision("TYPE_SUBMIT", "0", text=text),
                                    decision("DONE", goal=.9), decision("DONE", goal=.9)]
        model.verify_action.return_value = ActionSupport.ALLOWED
        events = []
        result = Agent(driver, model, helper, settle_seconds=0, emit=events.append).run(
            "Search the web for Ada Lovelace", execute=True)
        return result, driver, model, events

    def test_selected_text_is_typed_without_a_helper(self):
        result, driver, model, events = self.run_agent(text="Ada Lovelace")
        self.assertEqual(driver.execute.call_args.kwargs["text"], "Ada Lovelace")
        self.assertEqual(result["status"], "completed_unverified")
        self.assertIn("text_selected", [event["event"] for event in events])
        # The typed text is still checked by the action verifier.
        self.assertEqual(model.verify_action.call_args.kwargs["text"], "Ada Lovelace")

    def test_unselected_text_still_needs_the_helper(self):
        result, driver, _, _ = self.run_agent(text=None)
        self.assertEqual(result["status"], "needs_text_helper")
        driver.execute.assert_not_called()

    def test_a_partly_typed_field_goes_to_the_helper(self):
        helper = Mock(calls=0)
        helper.ask.return_value = " Lovelace"
        self.run_agent(text="Ada Lovelace", value="Ada", helper=helper)
        helper.ask.assert_called_once()


class SameScreenCompletionTests(unittest.TestCase):
    def run_agent(self, screens, *, stable=True):
        reads = iter(screens)
        driver = SimpleNamespace(can_type=False, execute=Mock(), observe=lambda timeout=10: next(reads))
        model = Mock(spec=["decide", "stable_completion"])
        model.stable_completion = stable
        model.decide.side_effect = [decision("DONE", goal=.9), decision("DONE", goal=.9)]
        events = []
        result = Agent(driver, model, settle_seconds=0, emit=events.append).run("Open About", execute=True)
        return result, model, events

    def screen(self, label):
        return Snapshot([Element("0", label, "StaticText", (0, .1, 1, .05), locator="/t[1]")], label, 400, 800, "wda")

    def test_unchanged_screen_is_not_asked_twice(self):
        about = self.screen("About")
        result, model, events = self.run_agent([about, about, about])
        self.assertEqual(result["status"], "completed_unverified")
        self.assertEqual(model.decide.call_count, 1)
        self.assertIn("completion_same_screen", [event["event"] for event in events])

    def test_a_changed_screen_is_asked_again(self):
        result, model, _ = self.run_agent([self.screen("About"), self.screen("Loading"), self.screen("Loading")])
        self.assertEqual(model.decide.call_count, 2)

    def test_models_without_measured_stability_are_always_asked_again(self):
        about = self.screen("About")
        _, model, _ = self.run_agent([about, about, about, about], stable=False)
        self.assertEqual(model.decide.call_count, 2)


class SpeculationTests(unittest.TestCase):
    """A decision started while the screen settles is used only for the identical request."""

    def setUp(self):
        # Remembered transitions are process-wide; each test starts without them.
        from mobile_agent import agent
        agent._transitions.clear()
        self.addCleanup(agent._transitions.clear)

    def setup(self, *, settle_on, final):
        tap_target = Element("0", "General", "Cell", (0, .3, 1, .05), locator="/c[1]")
        home = Snapshot([tap_target], "General", 400, 800, "wda")
        self.final = final

        class Driver:
            can_type = False
            reports_settling = True

            def __init__(self):
                self.execute = Mock()

            def observe(self, timeout=10):
                return self.current

            def observe_ready(self, timeout=10):
                return self.current

            def wait_for_change(self, snapshot, timeout=2, wait_seconds=.6, on_settling=None):
                on_settling(settle_on)
                self.current = final
                return final

        driver = Driver()
        driver.current = home
        speculative = decision("DONE", goal=.9)
        model = Mock(spec=["decide", "side_channel", "speculative_decisions", "stable_completion"])
        model.speculative_decisions, model.stable_completion = True, True
        model.side_channel.return_value = "side-http"
        calls = []

        def decide(snapshot, goal, history, **kwargs):
            calls.append(kwargs.get("http"))
            if len(calls) == 1:
                return decision("TAP", "0", confidence=.99)
            return speculative if kwargs.get("http") else decision("DONE", goal=.9)
        model.decide.side_effect = decide
        return driver, model, calls

    def screen(self, label):
        return Snapshot([Element("0", label, "StaticText", (0, .1, 1, .05), locator="/t[1]")], label, 400, 800, "wda")

    def test_identical_settled_screen_uses_the_speculative_decision(self):
        about = self.screen("About")
        driver, model, calls = self.setup(settle_on=about, final=about)
        events = []
        result = Agent(driver, model, emit=events.append).run("Open General", execute=True)
        kinds = [event["event"] for event in events]
        self.assertEqual(result["status"], "completed_unverified")
        self.assertEqual(calls, [None, "side-http"])  # No live call for the settled screen.
        self.assertIn("decision_speculation_used", kinds)

    def test_a_different_settled_screen_decides_live(self):
        driver, model, calls = self.setup(settle_on=self.screen("Loading"), final=self.screen("About"))
        events = []
        Agent(driver, model, emit=events.append).run("Open General", execute=True)
        self.assertEqual(calls[0], None)
        self.assertEqual(calls[1], "side-http")
        self.assertEqual(calls[2], None)  # The live decision for the real screen.
        self.assertIn("decision_speculation_discarded", [event["event"] for event in events])

    def test_request_identity_covers_screen_and_inputs(self):
        about = self.screen("About")
        request = {"goal": "g", "history": [], "hint": "", "allowed_bundles": frozenset({"a.b"})}
        self.assertEqual(decision_key(about, request), decision_key(self.screen("About"), dict(request)))
        self.assertNotEqual(decision_key(about, request), decision_key(self.screen("General"), request))
        self.assertNotEqual(decision_key(about, request), decision_key(about, {**request, "hint": "x"}))


class WDASettlingHookTests(unittest.TestCase):
    def test_reports_the_first_agreeing_pair_once_and_survives_callback_errors(self):
        before = from_wda(app('<XCUIElementTypeStaticText label="Settings" x="0" y="100" width="400" height="40"/>'))
        after = from_wda(app('<XCUIElementTypeStaticText label="General" x="0" y="100" width="400" height="40"/>'))
        driver = WDA("http://localhost:8100", "s")
        self.addCleanup(driver.close)
        driver.observe = Mock(return_value=after)
        seen = []

        def hook(read):
            seen.append(read.text)
            raise RuntimeError("speculation failure must not break settling")
        result = driver.wait_for_change(before, timeout=3, wait_seconds=.6, on_settling=hook)
        self.assertEqual(result.text, "General")
        self.assertEqual(seen, ["General"])


if __name__ == "__main__":
    unittest.main()


class WDAConfigureTests(unittest.TestCase):
    """A fresh session must not depend on /window/size, which can hang on a system overlay."""

    def setUp(self):
        WDA._screen_sizes.clear()
        self.addCleanup(WDA._screen_sizes.clear)

    def test_springboard_size_is_used_and_later_runs_configure_in_one_request(self):
        driver = WDA("http://localhost:8100", "s")
        self.addCleanup(driver.close)
        driver.http.request = Mock(return_value={"value": {"screenSize": {"width": 393, "height": 852}}})
        driver.call = Mock(return_value=None)
        driver.configure()
        self.assertEqual(driver.call.call_args.args[2]["settings"]["activeAppDetectionPoint"], "196,426")
        self.assertNotIn("/window/size", [c.args[1] for c in driver.call.call_args_list])
        later = WDA("http://localhost:8100", "s2")
        self.addCleanup(later.close)
        later.call = Mock(return_value=None)
        later.configure()
        later.call.assert_called_once()
        self.assertEqual(later.call.call_args.args[2]["settings"]["activeAppDetectionPoint"], "196,426")

    def test_settings_are_applied_before_the_window_size_is_asked(self):
        driver = WDA("http://localhost:8100", "s")
        self.addCleanup(driver.close)
        paths = []

        def call(method, path, body=None, timeout=10):
            paths.append((method, path))
            return {"width": 393, "height": 852} if path == "/window/size" else None
        driver.call = Mock(side_effect=call)
        from mobile_agent.transport import TransportError
        driver.http.request = Mock(side_effect=TransportError("no WDA in unit tests"))
        driver.configure()
        self.assertEqual(paths[0], ("POST", "/appium/settings"))
        self.assertEqual(driver.call.call_args.args[2]["settings"]["activeAppDetectionPoint"], "196,426")

    def test_a_hanging_window_size_falls_back_to_the_first_observation(self):
        from mobile_agent.transport import TransportError
        driver = WDA("http://localhost:8100", "s")
        self.addCleanup(driver.close)
        posted = []

        def call(method, path, body=None, timeout=10):
            if path == "/window/size":
                self.assertLessEqual(timeout, 3)
                raise TransportError("stale element reference")
            if path == "/appium/settings":
                posted.append(body["settings"])
                return None
            return app('<XCUIElementTypeStaticText label="General" x="0" y="100" width="400" height="40"/>')
        driver.call = Mock(side_effect=call)
        # /wda/screen goes through the raw client: never let a unit test reach a real
        # WDA on localhost:8100 (a connected phone answered it and broke this test).
        driver.http.request = Mock(side_effect=TransportError("no WDA in unit tests"))
        driver.configure()
        self.assertEqual(posted[0]["activeAppDetectionPoint"], "160,320")  # Never WDA's (64, 64).
        driver.observe()
        self.assertEqual(posted[-1]["activeAppDetectionPoint"], "200,400")
        driver.observe()
        self.assertEqual(len(posted), 2)  # Applied once.


class WDAForegroundHintTests(unittest.TestCase):
    """A read that fails on an overlay is retried once with the app we brought forward."""

    def setUp(self):
        WDA._overlay_apps.clear()
        self.addCleanup(WDA._overlay_apps.clear)

    def test_a_later_driver_hints_up_front_for_an_app_that_needed_it(self):
        first = self.driver(failures=1)
        first.call("POST", "/wda/apps/activate", {"bundleId": "com.apple.mobilesafari"})
        first.observe()
        second = self.driver(failures=0)
        second.call("POST", "/wda/apps/activate", {"bundleId": "com.apple.mobilesafari"})
        self.assertEqual(self.settings, [{"defaultActiveApplication": "com.apple.mobilesafari"}])
        second.observe()
        self.assertEqual(self.reads, 1)  # No failed read on the second driver.

    def driver(self, failures):
        from mobile_agent.transport import TransportError
        driver = WDA("http://localhost:8100", "s")
        self.addCleanup(driver.close)
        self.settings, self.reads = [], 0
        page = app('<XCUIElementTypeStaticText label="Eiffel Tower" x="0" y="100" width="400" height="40"/>')

        def request(method, path, body=None, timeout=10):
            if path.endswith("/appium/settings"):
                self.settings.append(body["settings"])
            elif "/source" in path:
                self.reads += 1
                if self.reads <= failures:
                    raise TransportError("HTTP 404; request not retried")
                return {"value": page, "status": 0}
            return {"value": None, "status": 0}
        driver.http.request = Mock(side_effect=request)
        return driver

    def test_failed_read_is_retried_with_the_activated_app_as_hint(self):
        driver = self.driver(failures=1)
        driver.call("POST", "/wda/apps/activate", {"bundleId": "com.apple.mobilesafari"})
        self.assertEqual(driver.observe().text, "Eiffel Tower")
        # Named on activation, and named again after the failed read (another client of
        # the session can reset detection to "auto").
        self.assertEqual(self.settings, [{"defaultActiveApplication": "com.apple.mobilesafari"}] * 2)
        # A later app switch moves the hint with it.
        driver.call("POST", "/wda/apps/activate", {"bundleId": "com.apple.Preferences"})
        self.assertEqual(self.settings[-1], {"defaultActiveApplication": "com.apple.Preferences"})

    def test_without_a_known_app_or_after_one_retry_the_failure_stands(self):
        from mobile_agent.transport import TransportError
        with self.assertRaises(TransportError):
            self.driver(failures=1).observe()
        driver = self.driver(failures=2)
        driver.call("POST", "/wda/apps/activate", {"bundleId": "com.apple.mobilesafari"})
        with self.assertRaises(TransportError):
            driver.observe()
        self.assertEqual(self.reads, 2)
