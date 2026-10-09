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


# ---------------------------------------------------------------- Smart's speed and settle paths (Track 2, 5 Oct)

import json as _json
import os
import threading as _threading
import time as _time
from pathlib import Path as _Path
from unittest.mock import patch as _patch

from mobile_agent import drivers as _drivers
from mobile_agent import engines as _engines
from mobile_agent import frontier as _frontier
from mobile_agent.frontier import FrontierAgent, prompt_text
from mobile_agent.tests.timing import bound

_FIXTURES = _Path(__file__).parent / "fixtures" / "frontier"


def _fixture_screens(name):
    data = _json.loads((_FIXTURES / f"{name}.json").read_text())
    out = {}
    for key, rows in data["screens"].items():
        elements = [Element(r["id"], r["label"], r["role"], tuple(r["rect"]), editable=r.get("editable", False),
                            value=r.get("value", ""), locator=f"/{r['id']}") for r in rows]
        out[key] = Snapshot(elements, "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                            bundle_id=data["bundle"])
    return out


class _Still:
    """A phone whose screen changes only when told; records each action and when it ran."""

    def __init__(self, screens, clock=_time.monotonic):
        self.screens, self.index, self.actions, self.clock = list(screens), 0, [], clock
        self.first_action_at = None

    def observe(self, timeout=10):
        return self.screens[min(self.index, len(self.screens) - 1)]

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        if self.first_action_at is None:
            self.first_action_at = self.clock()
        self.actions.append((operation, getattr(target, "label", target), text))
        self.index += 1

    def tap_point(self, x, y, snapshot, timeout=10):
        self.execute("TAP_POINT", (round(x, 2), round(y, 2)), snapshot)


class _SlowModel:
    """The measured latencies (quality baseline B3, 5 Oct): the contract call 2.6 s, a decision 2.4 s; scaled."""

    def __init__(self, steps, scale, items=(), data_json=None):
        self.steps, self.scale, self.items, self.data_json = list(steps), scale, list(items), data_json
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}
        self.lock = _threading.Lock()
        self.kinds = []

    def complete(self, messages, schema, timeout=60, **kwargs):
        planning = "items" in schema["properties"] and "actions" not in schema["properties"]
        _time.sleep((2.6 if planning else 2.4) * self.scale)
        with self.lock:
            self.kinds.append("planning" if planning else "decision" if "actions" in schema["properties"] else "other")
            if planning:
                return {"items": list(self.items)}, {}
            if "actions" not in schema["properties"]:
                return {"data": {"battery": "87%"}}, {}  # a structuring call
            self.usage["calls"] += 1
            prompt = prompt_text(messages)
            rows = prompt.split("Screen elements:\n", 1)[1].splitlines()
            op, label = self.steps.pop(0) if self.steps else ("DONE", None)
            target = next((r.split()[0] for r in rows if label and f'"{label}"' in r), None)
            reply = {"thought": "", "plan": None, "notes_add": [], "checklist_updates": [],
                     "actions": [{"op": op, "target": target, "text": None}],
                     "answer": "Battery health is 87%." if op == "DONE" else None}
            if "data_json" in schema["properties"]:
                reply["data_json"] = self.data_json if op == "DONE" else None
            return reply, {}


class EarlyDecisionTests(unittest.TestCase):
    """C5: the first decision runs while the task contract compiles; the contract is joined before any action."""
    SCALE = .2
    ITEMS = [{"kind": "REPORT", "app": "Settings", "what": "battery health", "payload": "", "act": "none",
              "count": "", "condition": "", "quote": "battery health"}]

    def first_action(self, early):
        row = Snapshot([Element("1", "Battery", "Button", (.1, .3, .8, .05))], "Battery", 400, 800,
                       "synthetic_fixture", bundle_id="com.apple.Preferences")
        phone = _Still([row, row, row])
        model = _SlowModel([("TAP", "Battery")], self.SCALE, self.ITEMS)
        events = []
        agent = FrontierAgent(phone, model, apps={"com.apple.Preferences": "Settings"}, emit=events.append,
                              screenshots=False, settle_seconds=0, early_decision=early, skills=())
        started = _time.monotonic()
        agent.run("What's my battery health?")
        return phone.first_action_at - started, events, agent

    def test_time_to_first_action_drops_by_the_contract_call(self):
        serial, _, _ = self.first_action(False)
        early, events, agent = self.first_action(True)
        self.assertGreaterEqual(serial, 5.0 * self.SCALE)       # contract, then decision: >= 5.0 s at full scale
        self.assertLessEqual(early, bound(2.8 * self.SCALE))    # max(2.6, 2.4) plus overhead: <= 2.8 s
        self.assertGreaterEqual(serial - early, 2.0 * self.SCALE)
        kinds = [e["event"] for e in events]
        # The contract is announced (and gates) before the first action.
        self.assertLess(kinds.index("frontier_contract"), kinds.index("frontier_action"))
        self.assertIsNotNone(agent._contract)

    def test_the_app_turns_it_on_and_a_bare_agent_keeps_the_measured_loop(self):
        self.assertEqual(dict(_engines.SMART_CONFIG.switches)["MOBSTER_EARLY_DECISION"], "on")
        with _patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOBSTER_EARLY_DECISION", None)
            self.assertFalse(FrontierAgent(None, None, skills=()).early_decision)
            os.environ["MOBSTER_EARLY_DECISION"] = "on"
            self.assertTrue(FrontierAgent(None, None, skills=()).early_decision)


class SchemaInDoneTests(unittest.TestCase):
    """C5: the schema-shaped answer comes in the DONE turn; no structuring call when it fits."""
    SCHEMA = {"type": "object", "properties": {"battery": {"type": "string"}}, "required": ["battery"],
              "additionalProperties": False}

    def run_with(self, data_json):
        row = Snapshot([Element("1", "Maximum Capacity", "StaticText", (.1, .3, .8, .05), value="87%")], "", 400, 800,
                       "synthetic_fixture", bundle_id="com.apple.Preferences")
        model = _SlowModel([("DONE", None)] * 3, 0, data_json=data_json)
        agent = FrontierAgent(_Still([row] * 4), model, apps={"com.apple.Preferences": "Settings"}, screenshots=False,
                              settle_seconds=0, skills=(), contract=False, output_schema=self.SCHEMA)
        outcome = agent.run("What's my battery health?")
        before = list(model.kinds)
        summary = _engines.summarize(outcome, agent, None, request="What's my battery health?",
                                     output_schema=self.SCHEMA)
        return summary, [k for k in model.kinds[len(before):] if k == "other"], before

    def test_a_fitting_answer_needs_no_structuring_call(self):
        summary, structuring, kinds = self.run_with('{"battery": "87%"}')
        self.assertEqual(structuring, [])
        self.assertEqual((summary["data"], summary["data_status"]), ({"battery": "87%"}, "validated"))
        self.assertEqual(kinds.count("other"), 0)

    def test_an_answer_that_does_not_fit_is_structured_as_before(self):
        summary, structuring, _ = self.run_with('{"battery": 87}')
        self.assertEqual(structuring, ["other"])
        self.assertEqual(summary["data"], {"battery": "87%"})

    def test_the_schema_is_asked_for_only_when_there_is_one(self):
        self.assertNotIn("data_json", _frontier._schema({}, ("DONE",))["properties"])
        shaped = _frontier._schema({}, ("DONE",), data=True)
        self.assertIn("data_json", shaped["required"])


class ReadListCapTests(unittest.TestCase):
    """B8: READ_LIST stops at 8 s or 60 rows (or at the row it was asked to find), and the prompt says so."""

    def test_a_list_of_500_rows_stops_within_eight_seconds_and_sixty_rows(self):
        clock = [1000.0]

        class Long(_Still):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                super().execute(operation, target, snapshot, text, timeout)
                clock[0] += 1.2  # one swipe on the phone

        pages = [Snapshot([Element(f"{p}-{i}", f"Place {p * 12 + i}", "Cell", (0, .1 + .07 * i, 1, .06))
                           for i in range(12)], "", 400, 800, "synthetic_fixture", bundle_id="com.apple.Maps")
                 for p in range(42)]  # 504 rows
        phone = Long(pages, clock=lambda: clock[0])
        agent = FrontierAgent(phone, None, screenshots=False, skills=())
        with _patch("mobile_agent.frontier.time.monotonic", lambda: clock[0]), \
                _patch("mobile_agent.frontier.time.sleep", lambda s: clock.__setitem__(0, clock[0] + s)):
            started = clock[0]
            rows = agent._read_list(pages[0])
            spent = clock[0] - started
        self.assertLessEqual(spent, _frontier.READ_LIST_SECONDS)
        self.assertLessEqual(len(rows), _frontier.READ_LIST_ROWS)
        self.assertIn("before the end of the list", agent._macro_feedback)

    def test_it_stops_at_the_row_it_was_asked_to_find(self):
        pages = [Snapshot([Element(f"{p}-{i}", f"Cafe {p * 5 + i}", "Cell", (0, .1 + .1 * i, 1, .08)) for i in range(5)],
                          "", 400, 800, "synthetic_fixture", bundle_id="com.apple.Maps") for p in range(10)]
        phone = _Still(pages)
        agent = FrontierAgent(phone, None, screenshots=False, skills=())
        with _patch("mobile_agent.frontier.time.sleep"):
            rows = agent._read_list(pages[0], "Cafe 7")
        self.assertEqual(len(phone.actions), 1)
        self.assertIn("Cafe 7", rows)
        self.assertIn("first row containing", agent._macro_feedback)

    def test_the_prompt_states_the_cost(self):
        self.assertIn("costs up to 8 seconds and reads at most 60 rows", _frontier.SYSTEM)


class CalculatorTests(unittest.TestCase):
    """B5: Calculator's keys (XCUIElementTypeKey) are listed with ids, and TAP on them works."""
    SCREENS = _fixture_screens("calculator_keypad")

    def test_the_keys_are_listed_and_eight_is_tappable(self):
        keypad = self.SCREENS["keypad"]
        aliases, lines = _frontier.screen_rows(keypad)
        listed = {element.label for element in aliases.values()}
        self.assertTrue({"8", "Add", "Equals", "All Clear", "Point"} <= listed)
        self.assertNotIn(_frontier.KEYBOARD_ROW, lines)
        phone, events = _Still([keypad] * 6), []
        script = _SlowModel([("TAP", "8"), ("DONE", None)], 0)
        FrontierAgent(phone, script, apps={"com.apple.calculator": "Calculator"}, emit=events.append,
                      screenshots=False, settle_seconds=0, skills=(), contract=False).run("In Calculator, press 8.")
        self.assertEqual(phone.actions[0], ("TAP", "8", None))
        self.assertFalse([e for e in events if e["event"] == "frontier_refused"
                          and "listed element" in str(e.get("detail"))])

    def test_a_software_keyboard_still_hides_its_keys(self):
        keys = [Element(str(i), k, "Key", (.1 * i, .8, .1, .05)) for i, k in enumerate("1234567890")]
        typing = Snapshot(keys, "", 400, 800, "synthetic_fixture", keyboard="visible")
        aliases, lines = _frontier.screen_rows(typing)
        self.assertEqual(aliases, {})
        self.assertIn(_frontier.KEYBOARD_ROW, lines)

    def test_tap_xy_is_allowed_on_a_matching_element_the_model_was_not_shown(self):
        hidden = Element("9", "8", "Key", (.5, .6, .2, .08))
        snapshot = Snapshot([hidden], "8", 400, 800, "synthetic_fixture", bundle_id="com.apple.calculator")
        agent = FrontierAgent(None, None, skills=())
        point, why = agent._tap_xy("press 8", {"target": "60,64", "text": "8"}, {}, snapshot)
        self.assertIsNone(why)
        point, why = agent._tap_xy("press 8", {"target": "60,64", "text": "8"}, {"e1": hidden}, snapshot)
        self.assertIn("is a listed element (e1)", why)

    def test_the_compiled_keys_are_offered_when_the_request_states_the_arithmetic(self):
        agent = FrontierAgent(None, None, apps={"com.apple.calculator": "Calculator"}, skills=())
        lines = agent._stable_lines("In Calculator, compute 123 plus 456 and tell me the result.")
        self.assertIn("1, 2, 3, Add, 4, 5, 6, Equals", lines)
        self.assertEqual(agent._stable_lines("What's my battery health?"), "")

    def test_no_keys_for_a_request_that_does_not_name_calculator(self):
        # Every run lists Calculator among the phone's apps (server.smart_apps): "2 x 3" in an order or "5/12" as
        # a date is no calculation to key in (review of #68).
        agent = FrontierAgent(None, None, apps={"com.apple.MobileSMS": "Messages", "com.apple.calculator": "Calculator"},
                              skills=())
        for request in ("Order 2 x 3 packs of AA batteries on Amazon.", "Is my dentist on 5/12? Check Calendar."):
            with self.subTest(request=request):
                self.assertEqual(agent._stable_lines(request), "")

    def test_tap_xy_on_any_other_element_left_out_of_the_rows_is_still_refused(self):
        # Only an app keypad's key may be pressed by its point: an unlisted App Store "Get" (a long page past
        # MAX_ELEMENTS) goes through TAP, where the contract gates it and asks (review of #68).
        get = Element("9", "Get", "Button", (.7, .9, .2, .04))
        snapshot = Snapshot([get], "Get", 400, 800, "synthetic_fixture", bundle_id="com.apple.AppStore")
        agent = FrontierAgent(None, None, skills=())
        point, why = agent._tap_xy("Install Duolingo", {"target": "80,92", "text": "Get"}, {}, snapshot)
        self.assertIsNone(point)
        self.assertIn("TAP it with target_label 'Get'", why)


class SettlePollTests(unittest.TestCase):
    """B4: settle reads are spaced at least 250 ms apart (they ran every 80-90 ms before runner restarts)."""

    def test_settle_reads_start_at_least_250_ms_apart(self):
        clock = [100.0]
        starts = []
        first = Snapshot([Element("1", "A", "Button", (.1, .1, .2, .05))], "", 400, 800, "synthetic_fixture",
                         bundle_id="com.apple.calculator")
        changing = iter([Snapshot([Element("1", label, "Button", (.1, .1, .2, .05))], "", 400, 800,
                                  "synthetic_fixture", bundle_id="com.apple.calculator") for label in "BCDDDDDDDDDD"])
        driver = WDA("http://localhost:8100", "test")

        def observe(timeout=10):
            starts.append(clock[0])
            clock[0] += .08  # an 80 ms read
            return next(changing)
        driver.observe = observe
        with _patch("mobile_agent.drivers.time.monotonic", lambda: clock[0]), \
                _patch("mobile_agent.drivers.time.sleep", lambda s: clock.__setitem__(0, clock[0] + s)), \
                _patch("mobile_agent.transport.time.monotonic", lambda: clock[0]):
            driver.wait_for_change(first, timeout=5, wait_seconds=.6)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertTrue(gaps)
        self.assertGreaterEqual(min(gaps), _drivers.WDA_SETTLE_POLL_SECONDS - 1e-9)
        self.assertGreaterEqual(_drivers.WDA_SETTLE_POLL_SECONDS, .25)


class ScreenshotStallTests(unittest.TestCase):
    """B4: two screenshot timeouts switch settling to AX reads for the next five steps."""

    def test_two_timeouts_turn_off_the_frame_clock_for_five_steps(self):
        driver = WDA("http://localhost:8100", "test")
        driver.frame_clock, driver.frame_clock_mode = object(), "on"
        self.assertIsNotNone(driver._frame_clock())
        self.assertFalse(driver.note_screenshot_timeout())
        self.assertTrue(driver.note_screenshot_timeout())
        self.assertIsNone(driver._frame_clock())
        for left in (4, 3, 2, 1, 0):
            self.assertEqual(driver.next_step(), left)
        self.assertIsNotNone(driver._frame_clock())

    def test_a_screenshot_that_times_out_counts(self):
        driver = WDA("http://localhost:8100", "test")

        class Stuck:
            def request(self, *args, **kwargs):
                raise TimeoutError("timed out")
        driver.preview_http = Stuck()
        for _ in range(2):
            with self.assertRaises(TimeoutError):
                driver.capture_preview()
        self.assertEqual(driver.ax_only_steps, _drivers.WDA_AX_ONLY_STEPS)

    def test_a_stalled_stream_counts_once_per_stall(self):
        from mobile_agent.frame_clock import STALL_SECONDS, Frame, FrameClock
        clock = FrameClock.__new__(FrameClock)
        clock.condition, clock.clock, clock.stalls, clock._stalled_after = _threading.Condition(), lambda: 50.0, 0, None
        clock.records, clock.log_path, clock._log_lock = __import__("collections").deque(maxlen=8), None, _threading.Lock()
        clock.latest = Frame(7, 50.0 - STALL_SECONDS - 1, None, (), False)
        self.assertTrue(clock.check_stall())
        self.assertFalse(clock.check_stall())
        driver = WDA("http://localhost:8100", "test")
        driver.frame_clock, driver.frame_clock_mode = clock, "on"
        clock.stalls = 2
        driver.next_step()
        self.assertEqual(driver.ax_only_steps, _drivers.WDA_AX_ONLY_STEPS)

    def test_the_model_gets_no_screenshot_request_while_ax_only(self):
        class Phone:
            ax_only_steps = 3

            def capture_preview(self, timeout=3):
                raise AssertionError("no screenshot while WDA's screenshots time out")
        self.assertIsNone(_frontier.screenshot_part(Phone()))


class SwipeTargetTests(unittest.TestCase):
    """B7: a sideways swipe strokes through its target row; an untargeted one over a list row is refused."""
    SCREENS = _fixture_screens("alarm_rows")

    def test_the_stroke_runs_through_the_target_row(self):
        alarms = self.SCREENS["alarms"]
        target = next(e for e in alarms.elements if e.label.startswith("6:15"))
        driver, bodies = WDA("http://localhost:8100", "test"), []
        driver.call = lambda method, path, body=None, timeout=10: bodies.append((path, body))
        driver.execute("SWIPE_LEFT", target, alarms)
        path, body = bodies[-1]
        moves = [a for a in body["actions"][0]["actions"] if a["type"] == "pointerMove"]
        centre = (target.rect[1] + target.rect[3] / 2) * alarms.height
        self.assertTrue(all(abs(m["y"] - centre) <= 1 for m in moves))
        self.assertTrue(all(target.rect[0] * 400 <= m["x"] <= (target.rect[0] + target.rect[2]) * 400 for m in moves))
        self.assertEqual(_drivers._swipe_points("SWIPE_LEFT", 400, 800), (300, 400, 100, 400))  # untargeted: as before

    def test_an_untargeted_row_swipe_is_refused_and_a_targeted_one_runs(self):
        alarms = self.SCREENS["alarms"]
        phone, events = _Still([alarms] * 6), []
        model = _SlowModel([("SWIPE_LEFT", None), ("SWIPE_LEFT", "6:15 AM, Mobster test"), ("DONE", None),
                            ("DONE", None)], 0)
        FrontierAgent(phone, model, apps={"com.apple.mobiletimer": "Clock"}, emit=events.append, screenshots=False,
                      settle_seconds=0, skills=(), contract=False).run("Delete my 6:15 AM Mobster test alarm.")
        self.assertIn("untargeted_swipe", [e.get("label") for e in events if e["event"] == "frontier_refused"])
        self.assertEqual(phone.actions[0], ("SWIPE_LEFT", "6:15 AM, Mobster test", None))

    def test_the_prompt_tells_the_model_to_target_rows(self):
        self.assertIn("a list row is never swiped without one", _frontier.SYSTEM)


class FreshnessTests(unittest.TestCase):
    """B-x: the model's picture is never older than the tree it goes with."""

    def test_a_stream_frame_older_than_the_tree_is_not_used(self):
        class Video:
            def __init__(self, at):
                self.at = at

            def latest(self):
                return (1, "image/jpeg", b"jpeg", 0, self.at, "wda_mjpeg")

        class Clock:
            def __init__(self, at):
                self.video = Video(at)

            def still_for(self):
                return 1.0
        phone = type("Phone", (), {})()
        phone.frame_clock = Clock(10.0)
        self.assertEqual(_frontier.video_frame(phone, since=9.5), b"jpeg")
        self.assertIsNone(_frontier.video_frame(phone, since=10.5))
        self.assertEqual(_frontier.video_frame(phone), b"jpeg")


class SettleProfileTests(unittest.TestCase):
    """C5: where a chunk's settles go, on the fake-latency harness. The last action's settle starts the next call
    on two agreeing reads (MOBSTER_EARLY_CALL, already on); the ones before it wait for a proven screen. No settle
    change ships here without the owner's paired run."""

    def test_the_last_settle_starts_the_next_call_early_and_the_timing_report_has_it(self):
        screens = [Snapshot([Element("1", f"Step {i}", "Button", (.1, .3, .8, .05))], "", 400, 800,
                            "synthetic_fixture", bundle_id="com.apple.Preferences") for i in range(6)]

        class Settling(_Still):
            settles_early, settles_on_one_read = True, True
            settled_by = "agreeing_reads"

            def __init__(self, screens):
                super().__init__(screens)
                self.settles = []

            def wait_for_change(self, before, wait_seconds=.6, timeout=2, **options):
                _time.sleep(.02)  # a settle's cost on the harness
                self.settles.append(options)
                return self.observe()
        phone = Settling(screens)
        model = _SlowModel([("TAP", "Step 0"), ("DONE", None), ("DONE", None)], 0)
        with _patch.dict(os.environ, {"MOBSTER_EARLY_CALL": "on"}):
            result = FrontierAgent(phone, model, apps={"com.apple.Preferences": "Settings"}, screenshots=False,
                                   settle_seconds=.6, skills=(), contract=False).run("Open Step 0.")
        self.assertEqual(phone.settles[-1], {"single_read": True, "early": True})
        self.assertGreater(result["timing"]["settle"], 0)
