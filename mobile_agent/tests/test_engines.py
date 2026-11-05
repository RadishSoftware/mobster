import dataclasses
import os
import threading
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import costs, drivers, engines, smart_events, smart_result
from mobile_agent.frontier import prompt_text
from mobile_agent.model_clients import AnthropicChat
from mobile_agent.state import Element, Snapshot

CLEAN = {k: v for k, v in os.environ.items() if not k.startswith(("MOBSTER_", "OPENAI", "TEXT_MODEL", "TYPESAFE"))}
# Smart on an OpenAI key alone; tests of OpenAI's words name it (the default is Claude)
SOL = engines.OPENAI_SMART_MODEL


def screen(*elements, bundle="com.apple.MobileSMS"):
    return Snapshot(list(elements), "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                    bundle_id=bundle)


class Driver(drivers.Driver):
    def __init__(self, screens):
        self.screens, self.index, self.actions = list(screens), 0, []

    def observe(self, timeout=10):
        return self.screens[min(self.index, len(self.screens) - 1)]

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, getattr(target, "label", target), text))
        self.index += 1

    def close(self):
        pass


class Script:

    def __init__(self, steps, items, answer="Sent.", updates=None, fail=None):
        self.steps, self.items, self.answer, self.prompts = list(steps), items, answer, []
        self.updates, self.fail = dict(updates or {}), fail  # {turn: checklist_updates}; fail: raised on that turn
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}
        self.model = "gpt-5.6-sol"

    def complete(self, messages, schema, timeout=60):
        if "items" in schema["properties"]:
            return {"items": self.items}, {"prompt_tokens": 300, "completion_tokens": 40}
        if self.fail is not None and len(self.prompts) == self.fail[0]:
            raise self.fail[1]
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += 2000
        self.usage["completion_tokens"] += 150
        prompt = prompt_text(messages)
        self.prompts.append(prompt)
        rows = prompt.split("Screen elements:\n", 1)[1].splitlines()
        chunk = self.steps.pop(0)
        actions = [{"operation": op, "target": next((r.split()[0] for r in rows if label and f'"{label}"' in r), None)
                    if i == 0 else None, "target_label": label if i else None, "text": text, "app": None}
                   for i, (op, label, text) in enumerate(chunk)]
        return {"thought": "", "plan": "", "notes_add": [], "answer": self.answer if chunk[-1][0] == "DONE" else None,
                "checklist_updates": self.updates.get(len(self.prompts) - 1, []),
                "actions": actions}, {"prompt_tokens": 2000, "completion_tokens": 150}


def item(kind="COMMIT", app="Messages", what="message to Sam", act="send_message", quote="Text Sam", payload=""):
    return {"kind": kind, "app": app, "what": what, "payload": payload, "act": act,
            "count": "1" if kind == "COMMIT" else "", "condition": "", "quote": quote}


FIELD = Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value="Message")
SEND = Element("1", "Send", "Button", (.8, .9, .1, .04))
COMPOSE = screen(Element("0", "Sam", "StaticText", (.1, .1, .6, .04)), FIELD, SEND)
QUICK = dataclasses.replace(engines.SMART_CONFIG, settle_seconds=0, screenshots=False)


def draft(text):
    return screen(Element("0", "Sam", "StaticText", (.1, .1, .6, .04)),
                  Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value=text), SEND)


def sent(text):
    return screen(Element("0", "Sam", "StaticText", (.1, .1, .6, .04)),
                  Element("3", text, "StaticText", (.1, .5, .6, .04)),
                  FIELD, SEND)


class ConfigTests(unittest.TestCase):

    def build_both(self, env=None):
        from mobile_agent.bench import iosworld
        with patch.dict(os.environ, {**CLEAN, **(env or {}), "ANTHROPIC_API_KEY": "sk-ant-test-000000000000"},
                        clear=True):
            harness, harness_client = iosworld.frontier_agent(Driver([COMPOSE]), {"com.apple.MobileSMS": "Messages"},
                                                              lambda e: None, ["com.apple.MobileSMS"])
        with patch.dict(os.environ, {**CLEAN, **(env or {})}, clear=True):
            client = engines.build_client(key="sk-ant-test-000000000000")
            app = engines.build_frontier(Driver([COMPOSE]), client, apps={"com.apple.MobileSMS": "Messages"},
                                         emit=lambda e: None, max_cost_usd=1.0)
        return (harness, harness_client), (app, client)

    @staticmethod
    def shape(agent):
        return {"max_steps": agent.max_steps, "max_seconds": agent.max_seconds, "screenshots": agent.screenshots,
                "settle_seconds": agent.settle_seconds}

    def test_the_app_and_the_harness_build_the_same_agent(self):
        (harness, harness_client), (app, client) = self.build_both()
        self.assertEqual(self.shape(harness), self.shape(app))
        self.assertEqual((harness_client.model, harness_client.reasoning), (client.model, client.reasoning))
        self.assertEqual((client.model, client.reasoning), ("claude-sonnet-5-5", "low"))
        self.assertIsInstance(client, AnthropicChat)
        self.assertEqual(self.shape(app), {"max_steps": 50, "max_seconds": 900, "screenshots": True,
                                           "settle_seconds": .6})
        # The one deliberate difference: whose spend cap. The app's Stop is passed per task (run_smart); the
        # harness has none, so its loop never stops early.
        self.assertEqual(app.max_cost_usd, 1.0)
        self.assertLess(harness.max_cost_usd, 1.0)
        self.assertIsNone(harness.cancelled)
        self.assertIsNone(app.cancelled)

    def test_the_app_and_the_harness_both_decide_early(self):
        (harness, _), (app, _) = self.build_both()
        self.assertTrue(app.early_decision and harness.early_decision)

class KeyTests(unittest.TestCase):
    def test_smart_uses_the_openai_key_else_an_openai_helper_key(self):
        self.assertEqual(engines.smart_key({"OPENAI_API_KEY": " sk-a "}), "sk-a")
        self.assertEqual(engines.smart_key({"MOBSTER_HELPER_PROVIDER": "openai", "TEXT_MODEL_API_KEY": "sk-h"}), "sk-h")
        self.assertIsNone(engines.smart_key({"MOBSTER_HELPER_PROVIDER": "openrouter", "TEXT_MODEL_API_KEY": "sk-or"}))
        self.assertIsNone(engines.smart_key({}))

    def test_the_provider_chosen_in_setup_picks_smarts_model_only_while_its_key_is_there(self):
        both = {"OPENAI_API_KEY": "sk-a", "ANTHROPIC_API_KEY": "sk-ant-b"}
        # No choice: Claude's model with both keys (OpenAI's until 12 Oct).
        self.assertEqual(engines.smart_model(both), engines.ANTHROPIC_SMART_MODEL)
        chose_claude = {**both, "MOBSTER_SMART_PROVIDER": "anthropic"}
        self.assertEqual((engines.smart_model(chose_claude), engines.smart_key(chose_claude)),
                         (engines.ANTHROPIC_SMART_MODEL, "sk-ant-b"))
        self.assertEqual(engines.smart_model({**both, "MOBSTER_SMART_PROVIDER": "openai"}), engines.OPENAI_SMART_MODEL)
        # A choice whose key is gone falls back to the key that's there; MOBSTER_SMART_MODEL still wins outright.
        self.assertEqual(engines.smart_model({"OPENAI_API_KEY": "sk-a", "MOBSTER_SMART_PROVIDER": "anthropic"}),
                         engines.OPENAI_SMART_MODEL)
        self.assertEqual(engines.smart_model({"ANTHROPIC_API_KEY": "sk-ant-b", "MOBSTER_SMART_PROVIDER": "openai"}),
                         engines.ANTHROPIC_SMART_MODEL)
        self.assertEqual(engines.smart_model({**chose_claude, "MOBSTER_SMART_MODEL": "gpt-5.6-sol"}), "gpt-5.6-sol")
        # Only the model is chosen: the loop's other settings stay SMART_CONFIG's.
        config = engines.smart_config({**both, "MOBSTER_SMART_PROVIDER": "openai"})
        self.assertEqual(config.model, engines.OPENAI_SMART_MODEL)
        self.assertEqual(dataclasses.replace(config, model=engines.SMART_CONFIG.model), engines.SMART_CONFIG)

    def test_with_no_choice_claude_plans_whenever_its_key_is_there(self):
        claude, openai = {"ANTHROPIC_API_KEY": "sk-ant-b"}, {"OPENAI_API_KEY": "sk-a"}
        both = {**claude, **openai}
        self.assertEqual(engines.SMART_CONFIG.model, "claude-sonnet-5-5")
        for env, model, key in ((both, "claude-sonnet-5-5", "sk-ant-b"),
                                (claude, "claude-sonnet-5-5", "sk-ant-b"),
                                (openai, "gpt-5.6-sol", "sk-a"),
                                ({}, "claude-sonnet-5-5", None),
                                # Not a valid choice: no choice at all.
                                ({**both, "MOBSTER_SMART_PROVIDER": "nobody"}, "claude-sonnet-5-5", "sk-ant-b"),
                                ({**both, "MOBSTER_SMART_PROVIDER": ""}, "claude-sonnet-5-5", "sk-ant-b")):
            with self.subTest(env=sorted(env)):
                self.assertEqual((engines.smart_model(env), engines.smart_key(env)), (model, key))
        # Where OpenAI still plans: chosen in Settings › Models or by `mobster login openai`, or named outright.
        self.assertEqual(engines.smart_model({**both, "MOBSTER_SMART_PROVIDER": "openai"}), "gpt-5.6-sol")
        self.assertEqual(engines.smart_key({**both, "MOBSTER_SMART_PROVIDER": "openai"}), "sk-a")
        self.assertEqual(engines.smart_model({**both, "MOBSTER_SMART_MODEL": "gpt-5.6-terra"}), "gpt-5.6-terra")
        self.assertEqual(engines.smart_model({**claude, "MOBSTER_SMART_MODEL": "claude-opus-5-5"}), "claude-opus-5-5")
        # The OpenAI key test (`mobster doctor`, Setup) checks the model Smart plans on with an OpenAI key.
        from mobile_agent import keys
        self.assertEqual(keys.SMART_MODEL, engines.OPENAI_SMART_MODEL)

    def test_with_no_key_the_app_still_asks_for_either_key(self):
        self.assertEqual(engines.smart_model({}), "claude-sonnet-5-5")
        self.assertEqual(engines.no_key_reason(True, env={}), engines.NO_SMART_KEY)
        self.assertEqual(engines.no_key_reason(False, env={}),
                         "Add OPENAI_API_KEY or ANTHROPIC_API_KEY to the agent's env file")
        chosen = {"MOBSTER_SMART_MODEL": "claude-sonnet-5-5"}
        self.assertEqual(engines.no_key_reason(True, env=chosen),
                         "Add ANTHROPIC_API_KEY to the agent's env file to run Smart on claude-sonnet-5-5")
        self.assertIn("ANTHROPIC_API_KEY", engines.no_key_reason(True, "claude-opus-5-5", env={}))

class ReachTests(unittest.TestCase):
    class Opener:
        def __init__(self, status=None, error=None):
            self.status, self.error, self.urls = status, error, []

        def __call__(self, request, timeout=15):
            self.urls.append(request.full_url)
            if self.error:
                raise self.error
            if self.status and self.status >= 300:
                raise urllib.error.HTTPError(request.full_url, self.status, "x", {}, None)
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"{}"

    def test_probe_outcomes(self):
        self.assertEqual(engines.ModelReach(SOL, opener=self.Opener(200)).probe("sk"), (True, None))
        self.assertEqual(engines.ModelReach(SOL, opener=self.Opener(404)).probe("sk"),
                         (False, "Your OpenAI key can't use gpt-5.6-sol."))
        self.assertFalse(engines.ModelReach(SOL, opener=self.Opener(401)).probe("sk")[0])
        self.assertEqual(engines.ModelReach(SOL, opener=self.Opener(500)).probe("sk"), (None, None))
        self.assertEqual(engines.ModelReach(SOL, opener=self.Opener(error=urllib.error.URLError("x"))).probe("sk"),
                         (None, None))

    def test_unknown_is_available_and_a_check_runs_once_in_the_background(self):
        opener = self.Opener(404)
        reach = engines.ModelReach(SOL, opener=opener, enabled=True)
        self.assertEqual(reach.state("sk-1"), (True, None))
        for thread in threading.enumerate():
            if thread.name == "mobster-model-reach":
                thread.join(2)
        self.assertEqual(reach.state("sk-1"), (False, "Your OpenAI key can't use gpt-5.6-sol."))
        self.assertEqual(opener.urls, ["https://api.openai.com/v1/models/gpt-5.6-sol"])
        self.assertEqual(reach.state(None), (False, engines.NO_SMART_KEY))

    def test_an_empty_account_makes_smart_unavailable_and_a_rate_limit_does_not(self):
        quota = ('RuntimeError: OpenAI HTTP 429: {"error": {"message": "You exceeded your current quota, please check '
                 'your plan and billing details.", "type": "insufficient_quota", "param": '
                 'null, "code": "insufficient_quota"}}')
        exhausted = ('OpenAI HTTP 429: {"error": {"message": "Your credit balance is too low.", '
                     '"code": "credit_balance_exhausted"}}')
        payment = 'OpenAI HTTP 402: {"error": {"message": "Payment required"}}'
        for error in (quota, exhausted, payment):
            self.assertEqual(engines.model_failure(error), (False, engines.NO_CREDIT), error)
            self.assertEqual(engines.plain_error(error), engines.NO_CREDIT)
        self.assertEqual(engines.NO_CREDIT,
                         "Your OpenAI account is out of credit. Add credit at platform.openai.com, then try again.")
        rate = 'OpenAI HTTP 429: {"error": {"message": "Rate limit reached", "code": "rate_limit_exceeded"}}'
        self.assertEqual(engines.model_failure(rate), (None, None))
        self.assertIn("rate limiting", engines.plain_error(rate))

    def test_an_empty_account_stays_unavailable_until_a_test_or_a_run_says_otherwise(self):
        opener = self.Opener(200)
        reach = engines.ModelReach(opener=opener, enabled=True)
        reach.record("sk-1", False, engines.NO_CREDIT)
        self.assertEqual(reach.state("sk-1"), (False, engines.NO_CREDIT))
        with patch.object(engines.time, "time", return_value=engines.time.time() + 7 * 3600):
            self.assertEqual(reach.state("sk-1"), (False, engines.NO_CREDIT))
        self.assertEqual(opener.urls, [])
        # A check already in flight when a run found the account empty doesn't overwrite it either.
        reach._check("sk-1", reach.digest("sk-1"))
        self.assertEqual(reach.state("sk-1"), (False, engines.NO_CREDIT))
        reach.record("sk-1", True)
        self.assertEqual(reach.state("sk-1"), (True, None))
        reach.record("sk-1", False, engines.NO_CREDIT)
        reach.forget("sk-1")
        self.assertEqual(reach.state("sk-1"), (True, None))
        # A rejected key is still the check's to revise after its TTL.
        rejected = engines.ModelReach(opener=self.Opener(200), enabled=True)
        rejected.record("sk-2", False, "OpenAI rejected your key. Check it in Settings › Models.")
        with patch.object(engines.time, "time", return_value=engines.time.time() + 7 * 3600):
            rejected.state("sk-2")
        for thread in threading.enumerate():
            if thread.name == "mobster-model-reach":
                thread.join(2)
        self.assertEqual(rejected.state("sk-2"), (True, None))

    def test_a_failed_model_call_is_openais_not_the_phones(self):
        reach = "Mobster couldn't reach OpenAI. Check your internet connection, then try again."
        self.assertEqual(engines.plain_error("timed out", model_failed=True, model=SOL), reach)
        self.assertEqual(engines.plain_error("URLError: <urlopen error [Errno 8] nodename nor servname provided, "
                                             "or not known>", model_failed=True, model=SOL), reach)
        self.assertEqual(engines.plain_error("OpenAI HTTP 401: bad key", model_failed=True),
                         "OpenAI didn't accept your key. Check it in Settings › Models.")
        self.assertIn("iPhone stopped answering", engines.plain_error("timed out"))  # a driver or WDA timeout
        self.assertEqual(engines.plain_error("ValueError: Nonfinite WDA element bounds"),
                         "Mobster couldn't read your iPhone's screen partway through. Try the task again.")


class SmartRunTests(unittest.TestCase):

    def run_smart(self, screens, steps, items, approve=None, answer="Sent.", request="Text Sam I'm running late",
                  updates=None):
        driver, script, out = Driver(screens), Script(steps, items, answer, updates), []
        events = smart_events.SmartEvents(out.append, driver=driver, frame=lambda: b"\xff\xd8jpeg",
                                     apps={"com.apple.MobileSMS": "Messages", "com.apple.reminders": "Reminders"})
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            agent = engines.build_frontier(driver, script, apps=events.apps, emit=events, config=QUICK,
                                           approve=events.approving(approve) if approve else None)  # as run_smart
            events.agent = agent
            result = agent.run(request)
            events.finish()
        return driver, script, result, out, events, agent

    def test_a_message_asks_once_at_send_with_the_exact_text_and_its_place(self):
        asked = []
        driver, _, result, out, events, _ = self.run_smart(
            [COMPOSE, COMPOSE, sent("I'm running late"), sent("I'm running late")],
            [[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)], [("DONE", None, None)]],
            [item()], approve=lambda request: asked.append(request) or "approved")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(driver.actions, [("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)])
        self.assertEqual(len(asked), 1)
        request = asked[0]
        self.assertEqual((request["kind"], request["title"], request["act"], request["text"], request["app_name"]),
                         ("commit", "Send this message to Sam?", "send_message", "I'm running late", "Messages"))
        self.assertEqual(request["target"], {"x": .8, "y": .9, "w": .1, "h": .04})
        kinds = [e["event"] for e in out]
        self.assertLess(kinds.index("plan"), kinds.index("frontier_action"))
        plan = next(e for e in out if e["event"] == "plan")
        self.assertEqual(plan["line"], "This task will send 1 message to Sam.")
        self.assertEqual(plan["commits"], [{"act": "send_message", "app": "Messages", "count": 1, "target": "Sam"}])
        self.assertEqual(plan["apps"], ["Messages"])
        steps = [e for e in out if e["event"] == "step"]
        self.assertEqual([s["text"] for s in steps], ["Wrote the message", "Tapped Send"])
        self.assertEqual([s["step"] for s in steps], [0, 0])
        self.assertTrue(all(s["_frame"] == b"\xff\xd8jpeg" for s in steps))
        receipt = next(e for e in out if e["event"] == "receipt")
        # The exact text approved and sent, as typed (not the folded row the check matched).
        self.assertEqual((receipt["itemId"], receipt["quote"], receipt["app"]), (1, "I'm running late", "Messages"))
        items = [e for e in out if e["event"] == "contract_item"]
        self.assertEqual([e["state"] for e in items], ["open", "done"])
        self.assertEqual(items[0]["text"], "Send the message to Sam in Messages")
        self.assertEqual(items[0]["label"], "COMMIT send_message (Messages): message to Sam")
        self.assertIn("cost", kinds)
        self.assertNotIn("frontier_prompt", kinds)
        self.assertNotIn("result", kinds)

    def test_a_private_write_never_asks(self):
        new = Element("1", "New Reminder", "Button", (.1, .9, .4, .04))
        title = Element("2", "Title", "TextField", (.1, .3, .8, .04), editable=True, value="")
        save = Element("3", "Save", "Button", (.8, .05, .15, .04))
        done = Element("4", "Buy milk", "StaticText", (.1, .3, .8, .04))
        rem = lambda *e: screen(*e, bundle="com.apple.reminders")
        for items in ([item("WRITE", "Reminders", "reminder", "none", "Add a reminder", "Buy milk")],
                      [item("COMMIT", "Reminders", "reminder", "save", "Add a reminder to buy milk")]):
            asked = []
            driver, _, result, _, _, _ = self.run_smart(
                [rem(new), rem(title, save), rem(title, save), rem(done), rem(done)],
                [[("TAP", "New Reminder", None), ("TYPE", "Title", "Buy milk"), ("TAP", "Save", None)],
                 [("DONE", None, None)]],
                items, approve=lambda request: asked.append(request) or "approved", answer="Added.",
                request="Add a reminder to buy milk")
            self.assertEqual(asked, [], items[0]["kind"])
            self.assertIn(("TAP", "Save", None), driver.actions)

    def test_decline_and_redirect_continues_and_asks_again_with_the_new_text(self):
        answers = iter(["redirected:say ten minutes late", "approved"])
        asked = []
        driver, script, result, out, _, _ = self.run_smart(
            [COMPOSE, COMPOSE, draft("I'm ten minutes late"), sent("I'm ten minutes late"),
             sent("I'm ten minutes late")],
            [[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)],
             [("SET_TEXT", "Message", "I'm ten minutes late"), ("TAP", "Send", None)], [("DONE", None, None)]],
            [item()], approve=lambda request: asked.append(request) or next(answers))
        self.assertEqual([a["text"] for a in asked], ["I'm running late", "I'm ten minutes late"])
        self.assertEqual(driver.actions.count(("TAP", "Send", None)), 1)
        self.assertIn("The user's later instruction: say ten minutes late", script.prompts[1])
        self.assertIn("asked instead", script.prompts[1])
        self.assertEqual(result["status"], "completed")

    def test_after_a_redirect_the_proof_is_only_what_was_sent(self):
        answers = iter(["redirected:say ten minutes late", "approved"])
        write = item("WRITE", "Messages", "message text", "none", "Text Sam I'm running late")
        _, _, result, out, events, agent = self.run_smart(
            [COMPOSE, draft("I'm running late"), draft("I'm ten minutes late"), sent("I'm ten minutes late"),
             sent("I'm ten minutes late")],
            [[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)],
             [("SET_TEXT", "Message", "I'm ten minutes late"), ("TAP", "Send", None)], [("DONE", None, None)]],
            [item(), write], approve=lambda request: next(answers), answer="Sent Sam “I'm ten minutes late”.")
        self.assertEqual(result["status"], "completed")
        quotes = [e["quote"] for e in out if e["event"] == "receipt"]
        self.assertNotIn("I'm running late", quotes)
        summary = smart_result.summarize(result, agent, events, request="Text Sam I'm running late")
        self.assertEqual([p["quote"] for p in summary["proof"]], ["I'm ten minutes late"])
        self.assertEqual(summary["outcome"], "done")
        # One field written twice is two steps here: the first write was shown before the user was asked.
        self.assertEqual([e["text"] for e in out if e["event"] == "step"],
                         ["Wrote the message", "Rewrote the message", "Tapped Send"])

    def test_a_redirect_in_a_chat_that_already_holds_the_draft_text(self):
        old = Element("5", "I'm running late", "StaticText", (.1, .3, .6, .04))
        history = lambda *e: screen(Element("0", "Sam", "StaticText", (.1, .1, .6, .04)), old, *e)
        field = lambda value: Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value=value)
        answers = iter(["redirected:say ten minutes late", "approved"])
        write = item("WRITE", "Messages", "message to Sam", "none", "Text Sam I'm running late", "I'm running late")
        _, _, result, out, events, agent = self.run_smart(
            [history(field("Message"), SEND), history(field("I'm running late"), SEND),
             history(field("I'm ten minutes late"), SEND),
             history(Element("3", "I'm ten minutes late", "StaticText", (.1, .5, .6, .04)), field("Message"), SEND),
             history(Element("3", "I'm ten minutes late", "StaticText", (.1, .5, .6, .04)), field("Message"), SEND)],
            [[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)],
             [("SET_TEXT", "Message", "I'm ten minutes late"), ("TAP", "Send", None)], [("DONE", None, None)]],
            [write, item()], approve=lambda request: next(answers))
        self.assertNotIn("I'm running late", [e["quote"] for e in out if e["event"] == "receipt"])
        summary = smart_result.summarize(result, agent, events, request="Text Sam I'm running late")
        self.assertEqual([p["quote"] for p in summary["proof"]], ["I'm ten minutes late"])

    def test_a_sent_message_is_one_chip_not_its_write_too(self):
        write = item("WRITE", "Messages", "message text", "none", "Text Sam I'm running late")
        _, _, result, out, events, agent = self.run_smart(
            [COMPOSE, draft("I'm running late"), sent("I'm running late"), sent("I'm running late")],
            [[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)], [("DONE", None, None)]],
            [item(), write])
        summary = smart_result.summarize(result, agent, events, request="Text Sam I'm running late")
        self.assertEqual([(p["quote"], p["app"]) for p in summary["proof"]], [("I'm running late", "Messages")])
        # It says it was a message sent, and to whom, so the dashboard draws it as that message; a read value
        # (the iOS version test below) carries neither.
        self.assertEqual((summary["proof"][0]["act"], summary["proof"][0]["to"]), ("send_message", "Sam"))
        final = {e["id"]: e["state"] for e in out if e["event"] == "contract_item"}
        self.assertEqual(final, {1: "done", 2: "done"})

    def test_a_saved_reminder_is_one_chip_in_its_own_words_and_nothing_stays_open(self):
        new = Element("1", "New Reminder", "Button", (.1, .9, .4, .04))
        title = lambda value: Element("2", "Title", "TextField", (.1, .3, .8, .04), editable=True, value=value)
        save = Element("3", "Save", "Button", (.8, .05, .15, .04))
        bar = Element("9", "Reminders", "NavigationBar", (0, .05, 1, .06))
        row = Element("4", "buy oat milk", "TextField", (.1, .3, .8, .04), editable=True)
        rem = lambda *e: screen(*e, bundle="com.apple.reminders")
        items = [item("WRITE", "Reminders", "reminder title", "none", "Add a reminder to buy oat milk", "buy oat milk"),
                 item("COMMIT", "Reminders", "reminder", "save", "Add a reminder to buy oat milk")]
        _, _, result, out, events, agent = self.run_smart(
            [rem(bar, new), rem(bar, title("")), rem(bar, title("buy oat milk"), save), rem(bar, row), rem(bar, row)],
            [[("TAP", "New Reminder", None), ("TYPE", "Title", "buy oat milk"), ("TAP", "Save", None)],
             [("DONE", None, None)]], items, answer="Added “buy oat milk”.", request="Add a reminder to buy oat milk")
        summary = smart_result.summarize(result, agent, events, request="Add a reminder to buy oat milk")
        self.assertEqual(summary["outcome"], "done")
        self.assertEqual([(p["quote"], p["screen"]) for p in summary["proof"]], [("buy oat milk", "Reminders")])
        self.assertIsNotNone(next(e for e in out if e["event"] == "receipt").get("_frame"))
        final = {}
        for event in out:
            if event["event"] == "contract_item":
                final[event["id"]] = event["state"]
        self.assertEqual(final, {1: "done", 2: "done"})
        self.assertEqual([e["text"] for e in out if e["event"] == "step"],
                         ["Tapped New Reminder", "Typed “buy oat milk” in Title", "Tapped Save"])

    def test_a_write_the_model_found_already_there_is_check_not_done(self):
        row = Element("4", "micah.chen@example.com", "TextField", (.1, .3, .8, .04), editable=True)
        rem = screen(Element("9", "Reminders", "NavigationBar", (0, .05, 1, .06)), row, bundle="com.apple.reminders")
        _, _, result, _, events, agent = self.run_smart(
            [rem, rem], [[("DONE", None, None)]],
            [item("WRITE", "Reminders", "reminder title", "none", "add a reminder")],
            answer="Added a reminder titled “micah.chen@example.com”.",
            request="add a reminder whose title is Micah's email",
            updates={0: [{"id": 1, "status": "done", "value": "micah.chen@example.com"}]})
        summary = smart_result.summarize(result, agent, events, request="add a reminder")
        self.assertEqual((summary["status"], summary["outcome"]), ("completion_not_confirmed", "check"))
        self.assertEqual(summary["reason"],
                         "Already done, so Mobster changed nothing: Write reminder title in Reminders.")

    def test_a_value_read_quotes_its_row_and_screen_and_is_a_step(self):
        about = screen(Element("0", "About", "NavigationBar", (0, .05, 1, .06)),
                       Element("1", "iOS Version", "StaticText", (.05, .3, .9, .05), value="26.4"),
                       bundle="com.apple.Preferences")
        _, _, result, out, events, agent = self.run_smart(
            [about, about], [[("DONE", None, None)]],
            [item("REPORT", "Settings", "iOS version", "none", "What iOS version")], answer="iOS 26.4.",
            request="What iOS version is this iPhone on?", updates={0: [{"id": 1, "status": "found", "value": "26.4"}]})
        receipt = next(e for e in out if e["event"] == "receipt")
        self.assertEqual((receipt["quote"], receipt["screen"], receipt["kind"]),
                         ("iOS Version 26.4", "About", "report"))
        # VoiceOver's "iOS Version, 26.4" (one cell's label) reads as the screen shows it.
        cell = screen(Element("0", "About", "NavigationBar", (0, .05, 1, .06)),
                      Element("1", "iOS Version, 26.4", "Cell", (.05, .3, .9, .05)), bundle="com.apple.Preferences")
        _, _, _, out, _, _ = self.run_smart(
            [cell, cell], [[("DONE", None, None)]],
            [item("REPORT", "Settings", "iOS version", "none", "What iOS version")],
            answer="iOS 26.4.", request="What iOS version is this iPhone on?",
            updates={0: [{"id": 1, "status": "found", "value": "26.4"}]})
        self.assertEqual(next(e for e in out if e["event"] == "receipt")["quote"], "iOS Version 26.4")
        self.assertIn("Read iOS Version: 26.4", [e["text"] for e in out if e["event"] == "step"])
        summary = smart_result.summarize(result, agent, events, request="What iOS version is this iPhone on?")
        self.assertEqual(summary["proof"], [{"quote": "iOS Version 26.4", "app": "Settings", "screen": "About",
                                             "frameId": None}])

    def test_a_model_that_cannot_be_reached_is_said_so(self):
        driver, script, out = Driver([COMPOSE]), Script([], [item()], fail=(0, TimeoutError("timed out"))), []
        events = smart_events.SmartEvents(out.append, driver=driver, frame=lambda: None)
        costs.meter(script, events.inference)
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            agent = engines.build_frontier(driver, script, apps={}, emit=events, config=QUICK)
            events.agent = agent
            result = agent.run("Text Sam hi")
        summary = smart_result.summarize(result, agent, events, request="Text Sam hi")
        self.assertEqual((summary["outcome"], summary["reason"]),
                         ("couldnt_finish",
                          "Mobster couldn't reach OpenAI. Check your internet connection, then try again."))

    def test_declining_ends_the_task_without_sending(self):
        driver, _, result, _, _, _ = self.run_smart(
            [COMPOSE, COMPOSE], [[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)]],
            [item()], approve=lambda request: "denied")
        self.assertEqual(result["status"], "approval_denied")
        self.assertNotIn(("TAP", "Send", None), driver.actions)

    def test_without_approve_declared_commits_go_through_as_in_the_benchmark(self):
        driver, _, result, _, _, _ = self.run_smart(
            [COMPOSE, COMPOSE, sent("I'm running late"), sent("I'm running late")],
            [[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)], [("DONE", None, None)]], [item()])
        self.assertEqual(result["status"], "completed")
        self.assertIn(("TAP", "Send", None), driver.actions)

    def test_without_a_contract_a_requested_commit_control_still_asks(self):
        asked = []
        driver, _, result, _, _, _ = self.run_smart(
            [COMPOSE, COMPOSE, sent("I'm running late"), sent("I'm running late")],
            [[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)], [("DONE", None, None)],
             [("DONE", None, None)]], [], approve=lambda request: asked.append(request) or "approved",
            request="Send Sam a message saying I'm running late")
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0]["title"], "Tap 'Send'?")

class StopTests(unittest.TestCase):

    def test_stop_mid_chain_takes_no_further_action(self):
        driver, script = Driver([COMPOSE] * 10), Script([[("TYPE", "Message", f"hello {i}") for i in range(4)]] * 3,
                                                          [item("READ", what="Sam", act="none", quote="Sam")])
        stop = threading.Event()
        execute = driver.execute

        def stopping(*args, **kwargs):
            execute(*args, **kwargs)
            stop.set()  # the user presses Stop right after the first action

        driver.execute = stopping
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            agent = engines.build_frontier(driver, script, apps={}, emit=lambda e: None, cancelled=stop.is_set,
                                           config=QUICK)
            result = agent.run("Type hello in the message field")
        self.assertEqual(len(driver.actions), 1)
        self.assertEqual((result["status"], result["actions"]), ("stopped", 1))
        self.assertEqual(len(script.prompts), 1)  # no model call after Stop either

    def test_stop_during_a_model_call_takes_no_action(self):
        driver, stop = Driver([COMPOSE] * 4), threading.Event()
        script = Script([[("TYPE", "Message", "hi"), ("TAP", "Send", None)]], [item()])
        complete = script.complete

        def thinking(messages, schema, timeout=60):
            out = complete(messages, schema, timeout)
            if "actions" in schema["properties"]:
                stop.set()  # pressed while the model was thinking
            return out

        script.complete = thinking
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            result = engines.build_frontier(driver, script, apps={}, emit=lambda e: None, cancelled=stop.is_set,
                                            config=QUICK).run("Text Sam hi")
        self.assertEqual((result["status"], driver.actions), ("stopped", []))


class FailureCodeTests(unittest.TestCase):

    def summary(self, outcome, model_failed=False):
        agent = SimpleNamespace(_contract=None, client=SimpleNamespace(model="gpt-5.6-sol", model_failed=model_failed))
        return smart_result.summarize({"steps": 2, "actions": 1, **outcome}, agent, None, request="Open Settings")

    def test_a_guard_stop_carries_the_stop_code_a_quick_task_and_a_refused_preflight_carry(self):
        summary = self.summary({"status": "blocked", "code": "unplugged",
                                "reason": "Your iPhone was unplugged. Plug it back in and try again."})
        self.assertEqual((summary["status"], summary["stop_code"], summary["code"], summary["reason"]),
                         ("blocked", "unplugged", "unplugged",
                          "Your iPhone was unplugged. Plug it back in and try again."))
        self.assertNotIn("model_error", summary)

    def test_each_provider_failure_has_its_code_and_its_sentence(self):
        cases = [
            ("OSError: [Errno 8] nodename nor servname provided", True, "unreachable",
             "Mobster couldn't reach OpenAI. Check your internet connection, then try again."),
            ("RuntimeError: OpenAI HTTP 404: model_not_found: The model does not exist", False, "model_unavailable",
             "Your OpenAI key can't use gpt-5.6-sol."),
            ("RuntimeError: OpenAI HTTP 401: invalid_api_key: Incorrect API key", False, "rejected_key", None),
            ("RuntimeError: OpenAI HTTP 429: insufficient_quota: You exceeded your current quota", False, "no_credit",
             engines.NO_CREDIT),
            ("RuntimeError: OpenAI HTTP 429: rate_limit_exceeded: Slow down", False, "rate_limited", None),
            ("RuntimeError: OpenAI HTTP 503: server_error: Overloaded", False, "provider_error", None),
        ]
        for reason, failed, code, sentence in cases:
            with self.subTest(code=code):
                summary = self.summary({"status": "error", "reason": reason}, model_failed=failed)
                self.assertEqual(summary["model_error"], code)
                self.assertEqual(summary["reason"], engines.plain_error(reason, failed, "gpt-5.6-sol"))
                if sentence:
                    self.assertEqual(summary["reason"], sentence)
                self.assertNotIn("stop_code", summary)

    def test_a_phone_failure_is_never_called_the_providers(self):
        summary = self.summary({"status": "error",
                                "reason": "ConnectionResetError: [Errno 54] Connection reset by peer"})
        self.assertNotIn("model_error", summary)
        self.assertEqual(summary["reason"], "Your iPhone stopped answering. Check it is unlocked and connected, then "
                                            "try again.")


if __name__ == "__main__":
    unittest.main()
