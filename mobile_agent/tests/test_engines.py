"""The Smart engine (engines.py): one frozen config for the app and the benchmark, its keys, estimates,
metering, contract events and approvals. Offline: no network, phone or model."""

import dataclasses
import json
import os
import statistics
import threading
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import patch

from mobile_agent import contract as K
from mobile_agent import engines
from mobile_agent.frontier import FrontierAgent, OpenAIChat, prompt_text
from mobile_agent.journal import Journal
from mobile_agent.state import Element, Snapshot
from pathlib import Path

CLEAN = {k: v for k, v in os.environ.items() if not k.startswith(("MOBSTER_", "OPENAI", "TEXT_MODEL", "TYPESAFE"))}


def screen(*elements, bundle="com.apple.MobileSMS"):
    return Snapshot(list(elements), "\n".join(e.label for e in elements), 400, 800, "synthetic_fixture",
                    bundle_id=bundle)


class Driver:
    def __init__(self, screens):
        self.screens, self.index, self.actions = list(screens), 0, []

    def observe(self, timeout=10):
        return self.screens[min(self.index, len(self.screens) - 1)]

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, getattr(target, "label", target), text))
        self.index += 1


class Script:
    """A model answering from a list of chunks; the contract call returns ``items``."""

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
                "checklist_updates": self.updates.get(len(self.prompts) - 1, []), "actions": actions}, {"prompt_tokens": 2000, "completion_tokens": 150}


def item(kind="COMMIT", app="Messages", what="message to Sam", act="send_message", quote="Text Sam", payload=""):
    return {"kind": kind, "app": app, "what": what, "payload": payload, "act": act, "count": "1" if kind == "COMMIT" else "",
            "condition": "", "quote": quote}


FIELD = Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value="Message")
SEND = Element("1", "Send", "Button", (.8, .9, .1, .04))
COMPOSE = screen(Element("0", "Sam", "StaticText", (.1, .1, .6, .04)), FIELD, SEND)
QUICK = dataclasses.replace(engines.SMART_CONFIG, settle_seconds=0, screenshots=False)


def draft(text):
    """The composer holding typed text, not yet sent."""
    return screen(Element("0", "Sam", "StaticText", (.1, .1, .6, .04)),
                  Element("2", "Message", "TextField", (.1, .9, .6, .04), editable=True, value=text), SEND)


def sent(text):
    return screen(Element("0", "Sam", "StaticText", (.1, .1, .6, .04)), Element("3", text, "StaticText", (.1, .5, .6, .04)),
                  FIELD, SEND)


class ConfigTests(unittest.TestCase):
    """The app's Smart engine is exactly the benchmark's frontier policy."""

    def build_both(self, env=None):
        from mobile_agent.bench import iosworld
        with patch.dict(os.environ, {**CLEAN, **(env or {}), "OPENAI_API_KEY": "sk-test-000000000000"}, clear=True):
            harness, harness_client = iosworld.frontier_agent(Driver([COMPOSE]), {"com.apple.MobileSMS": "Messages"},
                                                              lambda e: None, ["com.apple.MobileSMS"])
            harness_env = {name: os.environ.get(name) for name in engines.switches()}
        with patch.dict(os.environ, {**CLEAN, **(env or {})}, clear=True):
            client = engines.build_client(key="sk-test-000000000000")
            app = engines.build_frontier(Driver([COMPOSE]), client, apps={"com.apple.MobileSMS": "Messages"},
                                         emit=lambda e: None, max_cost_usd=1.0)
            app_env = {name: os.environ.get(name) for name in engines.switches()}
        return (harness, harness_client, harness_env), (app, client, app_env)

    @staticmethod
    def shape(agent):
        return {"max_steps": agent.max_steps, "max_seconds": agent.max_seconds, "screenshots": agent.screenshots,
                "settle_seconds": agent.settle_seconds, "macros": agent.macros, "contract": agent.use_contract}

    def test_the_app_and_the_harness_build_the_same_agent(self):
        (harness, harness_client, harness_env), (app, client, app_env) = self.build_both()
        self.assertEqual(self.shape(harness), self.shape(app))
        self.assertEqual((harness_client.model, harness_client.reasoning), (client.model, client.reasoning))
        self.assertEqual((client.model, client.reasoning), ("gpt-5.6-sol", "low"))
        self.assertIsInstance(client, OpenAIChat)
        self.assertEqual(harness_env, app_env)
        self.assertEqual(app_env, engines.switches())
        self.assertEqual(self.shape(app), {"max_steps": 50, "max_seconds": 900, "screenshots": True,
                                           "settle_seconds": .6, "macros": True, "contract": True})
        # The one deliberate difference: whose spend cap. The app's Stop is passed per task (run_smart); the
        # harness has none, so its loop never stops early.
        self.assertEqual(app.max_cost_usd, 1.0)
        self.assertLess(harness.max_cost_usd, 1.0)
        self.assertIsNone(harness.cancelled)
        self.assertIsNone(app.cancelled)

    def test_the_harness_takes_its_defaults_from_the_engine(self):
        from mobile_agent.bench import iosworld
        self.assertEqual(iosworld.DEFAULT_FRONTIER_MODEL, engines.SMART_CONFIG.model)
        self.assertEqual(iosworld.MAX_STEPS, engines.SMART_CONFIG.max_steps)
        self.assertEqual(iosworld.MAX_SECONDS, engines.SMART_CONFIG.max_seconds)
        # The run command's reasoning default is the engine's.
        with patch.object(iosworld, "cmd_run", side_effect=lambda a: self.assertEqual(a.reasoning, "low") or 0):
            iosworld.main(["run", "--repo", "x", "--udid", "u", "--wda-url", "http://127.0.0.1:1", "--out", "o"])

    def test_the_app_pins_its_switches_and_the_harness_keeps_an_ablation(self):
        (harness, _, harness_env), (app, _, app_env) = self.build_both({"MOBSTER_EXACT_TEXT": "off",
                                                                         "MOBSTER_FRONTIER_CONTRACT": "off"})
        self.assertEqual(app_env["MOBSTER_EXACT_TEXT"], "on")
        self.assertTrue(app.use_contract)
        self.assertEqual(harness_env["MOBSTER_EXACT_TEXT"], "off")
        self.assertFalse(harness.use_contract)

    def test_the_config_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            engines.SMART_CONFIG.model = "gpt-5.6-luna"


class KeyTests(unittest.TestCase):
    def test_smart_uses_the_openai_key_else_an_openai_helper_key(self):
        self.assertEqual(engines.smart_key({"OPENAI_API_KEY": " sk-a "}), "sk-a")
        self.assertEqual(engines.smart_key({"MOBSTER_HELPER_PROVIDER": "openai", "TEXT_MODEL_API_KEY": "sk-h"}), "sk-h")
        self.assertIsNone(engines.smart_key({"MOBSTER_HELPER_PROVIDER": "openrouter", "TEXT_MODEL_API_KEY": "sk-or"}))
        self.assertIsNone(engines.smart_key({}))

    def test_smart_and_setup_agree_on_the_key(self):
        """Setup, Settings and doctor read keys.openai_key; the server reads smart_key. They never disagree."""
        from mobile_agent.keys import openai_key
        by_url = {"TEXT_MODEL": "gpt-4.1-mini", "TEXT_MODEL_BASE_URL": "https://api.openai.com/v1", "TEXT_MODEL_API_KEY": "sk-u"}
        self.assertEqual(engines.smart_key(by_url), "sk-u")  # an OpenAI helper set up by hand, without the preset
        for env in ({}, {"OPENAI_API_KEY": "sk-a"}, by_url, {**by_url, "OPENAI_API_KEY": "sk-a"},
                    {"MOBSTER_HELPER_PROVIDER": "openai", "TEXT_MODEL_API_KEY": "sk-h"},
                    {"MOBSTER_HELPER_PROVIDER": "openrouter", "TEXT_MODEL_API_KEY": "sk-or"},
                    {"TEXT_MODEL": "llama", "TEXT_MODEL_BASE_URL": "http://127.0.0.1:8080/v1", "TEXT_MODEL_API_KEY": "local-server-no-key"}):
            with self.subTest(env=env):
                self.assertEqual(engines.smart_key(env), openai_key(env)[0])
                self.assertEqual(engines.default_engine({**env, "TYPESAFE_API_KEY": "j"}) == "smart", openai_key(env)[0] is not None)

    def test_the_provider_chosen_in_setup_picks_smarts_model_only_while_its_key_is_there(self):
        both = {"OPENAI_API_KEY": "sk-a", "ANTHROPIC_API_KEY": "sk-ant-b"}
        # No choice: OpenAI's model with both keys, exactly as before.
        self.assertEqual(engines.smart_model(both), engines.SMART_CONFIG.model)
        chose_claude = {**both, "MOBSTER_SMART_PROVIDER": "anthropic"}
        self.assertEqual((engines.smart_model(chose_claude), engines.smart_key(chose_claude)), (engines.ANTHROPIC_SMART_MODEL, "sk-ant-b"))
        self.assertEqual(engines.smart_model({**both, "MOBSTER_SMART_PROVIDER": "openai"}), engines.SMART_CONFIG.model)
        # A choice whose key is gone falls back to the key that's there; MOBSTER_SMART_MODEL still wins outright.
        self.assertEqual(engines.smart_model({"OPENAI_API_KEY": "sk-a", "MOBSTER_SMART_PROVIDER": "anthropic"}), engines.SMART_CONFIG.model)
        self.assertEqual(engines.smart_model({"ANTHROPIC_API_KEY": "sk-ant-b", "MOBSTER_SMART_PROVIDER": "openai"}), engines.ANTHROPIC_SMART_MODEL)
        self.assertEqual(engines.smart_model({**chose_claude, "MOBSTER_SMART_MODEL": "gpt-5.6-sol"}), "gpt-5.6-sol")
        # Only the model is chosen: the loop's other settings stay SMART_CONFIG's.
        config = engines.smart_config(chose_claude)
        self.assertEqual(config.model, engines.ANTHROPIC_SMART_MODEL)
        self.assertEqual(dataclasses.replace(config, model=engines.SMART_CONFIG.model), engines.SMART_CONFIG)

    def test_a_refused_key_reads_the_same_for_both_providers_with_no_env_names(self):
        # The app names the account as people know it: Claude for Anthropic's (MESSAGING § 11).
        for provider, name in (("OpenAI", "OpenAI"), ("Anthropic", "Claude")):
            reason = engines.rejected_key(provider)
            self.assertEqual(reason, f"{name} didn't accept your key. Check it in Settings › AI account.")
            self.assertNotIn("_API_KEY", reason)

    def test_default_engine(self):
        self.assertEqual(engines.default_engine({}), "smart")  # a new user
        self.assertEqual(engines.default_engine({"OPENAI_API_KEY": "sk"}), "smart")
        self.assertEqual(engines.default_engine({"TYPESAFE_API_KEY": "j"}), "fast")  # only a Jev key
        self.assertEqual(engines.default_engine({"TYPESAFE_API_KEY": "j", "OPENAI_API_KEY": "sk"}), "smart")
        self.assertEqual(engines.default_engine({"OPENAI_API_KEY": "sk", "MOBSTER_DEFAULT_ENGINE": "fast"}), "fast")
        self.assertEqual(engines.default_engine({"MOBSTER_DEFAULT_ENGINE": "turbo"}), "smart")


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
        self.assertEqual(engines.ModelReach(opener=self.Opener(200)).probe("sk"), (True, None))
        self.assertEqual(engines.ModelReach(opener=self.Opener(404)).probe("sk"),
                         (False, "Your OpenAI key can't use gpt-5.6-sol."))
        self.assertFalse(engines.ModelReach(opener=self.Opener(401)).probe("sk")[0])
        self.assertEqual(engines.ModelReach(opener=self.Opener(500)).probe("sk"), (None, None))
        self.assertEqual(engines.ModelReach(opener=self.Opener(error=urllib.error.URLError("x"))).probe("sk"), (None, None))

    def test_unknown_is_available_and_a_check_runs_once_in_the_background(self):
        opener = self.Opener(404)
        reach = engines.ModelReach(opener=opener, enabled=True)
        self.assertEqual(reach.state("sk-1"), (True, None))
        for thread in threading.enumerate():
            if thread.name == "mobster-model-reach":
                thread.join(2)
        self.assertEqual(reach.state("sk-1"), (False, "Your OpenAI key can't use gpt-5.6-sol."))
        self.assertEqual(opener.urls, ["https://api.openai.com/v1/models/gpt-5.6-sol"])
        self.assertEqual(reach.state(None), (False, engines.NO_OPENAI_KEY))

    def test_a_disabled_check_never_calls_out(self):
        opener = self.Opener(200)
        self.assertEqual(engines.ModelReach(opener=opener).state("sk"), (True, None))
        self.assertEqual(opener.urls, [])

    def test_run_errors_in_plain_words(self):
        self.assertEqual(engines.model_failure("OpenAI HTTP 404: model gpt-5.6-sol does not exist")[0], False)
        self.assertEqual(engines.model_failure("TimeoutError"), (None, None))
        self.assertIn("out of credit", engines.plain_error('OpenAI HTTP 429: {"code": "insufficient_quota"}'))

    def test_an_empty_account_makes_smart_unavailable_and_a_rate_limit_does_not(self):
        """QA-1: a Smart run that failed for want of credit left Smart on offer, and every task failed the same
        way. The run's error (frontier.OpenAIChat: "OpenAI HTTP <status>: <body>") now says Smart can't run."""
        quota = ('RuntimeError: OpenAI HTTP 429: {"error": {"message": "You exceeded your current quota, please check '
                 'your plan and billing details.", "type": "insufficient_quota", "param": null, "code": "insufficient_quota"}}')
        exhausted = 'OpenAI HTTP 429: {"error": {"message": "Your credit balance is too low.", "code": "credit_balance_exhausted"}}'
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
        """The background check reads the model, which is free and passes on an empty account: it never clears
        the refusal, however long ago it was recorded. A passing record (a key test, a run) does; so does
        forgetting (a saved key)."""
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
        self.assertEqual(engines.plain_error("timed out", model_failed=True), reach)
        self.assertEqual(engines.plain_error("URLError: <urlopen error [Errno 8] nodename nor servname provided, "
                                             "or not known>", model_failed=True), reach)
        self.assertEqual(engines.plain_error("OpenAI HTTP 401: bad key", model_failed=True),
                         "OpenAI didn't accept your key. Check it in Settings › AI account.")
        self.assertIn("iPhone stopped answering", engines.plain_error("timed out"))  # a driver or WDA timeout
        self.assertEqual(engines.plain_error("ValueError: Nonfinite WDA element bounds"),
                         "Mobster couldn't read your iPhone's screen partway through. Try the task again.")


class EstimateTests(unittest.TestCase):
    def test_longer_multi_app_tasks_cost_more_and_short_ones_cents(self):
        short = engines.smart_estimate("What iOS version is this iPhone on?", ["Settings"])
        long = engines.smart_estimate("Check Uber and Lyft for a ride to SFO, then text Sam the cheaper one",
                                      ["Uber", "Lyft", "Messages"])
        self.assertLess(short["highUsd"], long["lowUsd"] * 3)
        self.assertLess(short["lowUsd"], short["highUsd"])
        self.assertLess(long["highUsd"], .75)
        self.assertTrue(.02 < engines.midpoint(short) < .12)
        self.assertEqual((short["model"], short["provider"], short["available"]), ("gpt-5.6-sol", "OpenAI", True))
        self.assertAlmostEqual(short["highUsd"] / short["lowUsd"], 4, delta=.05)  # halved to doubled
        self.assertEqual(engines.named_apps("Check Uber, then text Sam in messages", ["Uber", "Messages", "Mail"]), 2)

    def test_the_estimate_holds_on_recorded_runs(self):
        """Out of sample on recorded runs of this loop (tests/smart_costs.json, split by task), the estimate
        is within 2x of the actual cost on at least 80% of runs, and neither side of it by much on the median."""
        runs = json.loads((Path(__file__).parent / "smart_costs.json").read_text())["runs"]
        ratios = {split: [r["costUsd"] / engines.midpoint(engines.smart_estimate(r["goal"], r["apps"]))
                          for r in runs if r["split"] == split] for split in ("fit", "heldout")}
        self.assertEqual((len(ratios["fit"]), len(ratios["heldout"])), (128, 56))
        within = {split: sum(.5 <= x <= 2 for x in values) / len(values) for split, values in ratios.items()}
        self.assertGreaterEqual(within["heldout"], .80)
        self.assertGreaterEqual(within["fit"], .78)
        for values in ratios.values():
            self.assertTrue(.6 < statistics.median(values) < 1.5)
        long = [r for r in runs if r["run"].startswith("ab7-b")]  # the 32 runs that were 2.55x over the old one
        self.assertGreaterEqual(sum(.5 <= r["costUsd"] / engines.midpoint(engines.smart_estimate(r["goal"], r["apps"]))
                                    <= 2 for r in long), 30)

    def test_fast_takes_the_planning_range(self):
        fast = engines.fast_estimate({"estimated_min_usd": .001, "estimated_max_usd": .004}, available=False,
                                     reason=engines.NO_JEV_KEY)
        self.assertEqual((fast["lowUsd"], fast["highUsd"], fast["available"], fast["provider"]),
                         (.001, .004, False, "TypeSafe"))


class MeterTests(unittest.TestCase):
    def test_every_call_is_priced_and_recorded_in_the_usage_ledger(self):
        journal = Journal()
        self.addCleanup(journal.close)
        journal.create({"id": "r1", "finishedAt": None})
        events = []
        client = Script([[("DONE", None, None)]], [])
        engines.meter(client, events.append)
        client.complete([{"role": "system", "content": "x"}, {"role": "user", "content": [
            {"type": "text", "text": "Screen elements:\n"}]}], {"properties": {"actions": {}}})
        self.assertEqual([e["event"] for e in events], ["inference_started", "inference_finished"])
        finished = events[1]
        self.assertEqual((finished["provider"], finished["model"], finished["purpose"]), ("openai", "gpt-5.6-sol", "decision"))
        self.assertEqual(finished["usage"]["total_tokens"], 2150)
        self.assertAlmostEqual(finished["estimated_usd"], (2000 * 4 + 150 * 20) / 1e6)
        self.assertEqual(finished["cost_nanodollars"], 11_000_000)
        for seq, event in enumerate(events):
            journal.append({"id": "r1", "finishedAt": None}, {**event, "seq": seq, "timestamp": 1000.0 + seq})
        self.assertEqual(journal.usage()["providers"][0]["provider"], "openai")
        self.assertAlmostEqual(journal.usage()["totals"]["estimated_usd"], .011)

    def test_a_failed_call_is_recorded_unpriced(self):
        events = []

        class Down:
            model = "gpt-5.6-sol"

            def complete(self, messages, schema, timeout=60):
                raise RuntimeError("OpenAI HTTP 500: x")

        client = engines.meter(Down(), events.append)
        with self.assertRaises(RuntimeError):
            client.complete([], {"properties": {}})
        self.assertEqual((events[1]["success"], events[1]["cost_nanodollars"]), (False, None))


class SmartRunTests(unittest.TestCase):
    """The loop with the app's events and approvals (FrontierAgent's ``approve``)."""

    def run_smart(self, screens, steps, items, approve=None, answer="Sent.", request="Text Sam I'm running late",
                  updates=None):
        driver, script, out = Driver(screens), Script(steps, items, answer, updates), []
        events = engines.SmartEvents(out.append, driver=driver, frame=lambda: b"\xff\xd8jpeg",
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
            [COMPOSE, COMPOSE, COMPOSE, sent("I'm ten minutes late"), sent("I'm ten minutes late")],
            [[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)],
             [("SET_TEXT", "Message", "I'm ten minutes late"), ("TAP", "Send", None)], [("DONE", None, None)]],
            [item()], approve=lambda request: asked.append(request) or next(answers))
        self.assertEqual([a["text"] for a in asked], ["I'm running late", "I'm ten minutes late"])
        self.assertEqual(driver.actions.count(("TAP", "Send", None)), 1)
        self.assertIn("The user's later instruction: say ten minutes late", script.prompts[1])
        self.assertIn("asked instead", script.prompts[1])
        self.assertEqual(result["status"], "completed")

    def test_after_a_redirect_the_proof_is_only_what_was_sent(self):
        """The declined draft sat in the compose box (its WRITE looked proven there); only the sent text is
        proof, once, as typed."""
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
        summary = engines.summarize(result, agent, events, request="Text Sam I'm running late")
        self.assertEqual([p["quote"] for p in summary["proof"]], ["I'm ten minutes late"])
        self.assertEqual(summary["outcome"], "done")
        # One field written twice is two steps here: the first write was shown before the user was asked.
        self.assertEqual([e["text"] for e in out if e["event"] == "step"],
                         ["Wrote the message", "Rewrote the message", "Tapped Send"])

    def test_a_redirect_in_a_chat_that_already_holds_the_draft_text(self):
        """The same words sent last week sit in the history: they prove neither the draft nor the write."""
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
        summary = engines.summarize(result, agent, events, request="Text Sam I'm running late")
        self.assertEqual([p["quote"] for p in summary["proof"]], ["I'm ten minutes late"])

    def test_a_sent_message_is_one_chip_not_its_write_too(self):
        write = item("WRITE", "Messages", "message text", "none", "Text Sam I'm running late")
        _, _, result, out, events, agent = self.run_smart(
            [COMPOSE, draft("I'm running late"), sent("I'm running late"), sent("I'm running late")],
            [[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)], [("DONE", None, None)]],
            [item(), write])
        summary = engines.summarize(result, agent, events, request="Text Sam I'm running late")
        self.assertEqual([(p["quote"], p["app"]) for p in summary["proof"]], [("I'm running late", "Messages")])
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
        summary = engines.summarize(result, agent, events, request="Add a reminder to buy oat milk")
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
            [rem, rem], [[("DONE", None, None)]], [item("WRITE", "Reminders", "reminder title", "none", "add a reminder")],
            answer="Added a reminder titled “micah.chen@example.com”.", request="add a reminder whose title is Micah's email",
            updates={0: [{"id": 1, "status": "done", "value": "micah.chen@example.com"}]})
        summary = engines.summarize(result, agent, events, request="add a reminder")
        self.assertEqual((summary["status"], summary["outcome"]), ("completion_not_confirmed", "check"))
        self.assertEqual(summary["reason"], "Already done, so Mobster changed nothing: Write reminder title in Reminders.")

    def test_a_value_read_quotes_its_row_and_screen_and_is_a_step(self):
        about = screen(Element("0", "About", "NavigationBar", (0, .05, 1, .06)),
                       Element("1", "iOS Version", "StaticText", (.05, .3, .9, .05), value="26.4"),
                       bundle="com.apple.Preferences")
        _, _, result, out, events, agent = self.run_smart(
            [about, about], [[("DONE", None, None)]],
            [item("REPORT", "Settings", "iOS version", "none", "What iOS version")], answer="iOS 26.4.",
            request="What iOS version is this iPhone on?", updates={0: [{"id": 1, "status": "found", "value": "26.4"}]})
        receipt = next(e for e in out if e["event"] == "receipt")
        self.assertEqual((receipt["quote"], receipt["screen"], receipt["kind"]), ("iOS Version 26.4", "About", "report"))
        # VoiceOver's "iOS Version, 26.4" (one cell's label) reads as the screen shows it.
        cell = screen(Element("0", "About", "NavigationBar", (0, .05, 1, .06)),
                      Element("1", "iOS Version, 26.4", "Cell", (.05, .3, .9, .05)), bundle="com.apple.Preferences")
        _, _, _, out, _, _ = self.run_smart(
            [cell, cell], [[("DONE", None, None)]], [item("REPORT", "Settings", "iOS version", "none", "What iOS version")],
            answer="iOS 26.4.", request="What iOS version is this iPhone on?",
            updates={0: [{"id": 1, "status": "found", "value": "26.4"}]})
        self.assertEqual(next(e for e in out if e["event"] == "receipt")["quote"], "iOS Version 26.4")
        self.assertIn("Read iOS Version: 26.4", [e["text"] for e in out if e["event"] == "step"])
        summary = engines.summarize(result, agent, events, request="What iOS version is this iPhone on?")
        self.assertEqual(summary["proof"], [{"quote": "iOS Version 26.4", "app": "Settings", "screen": "About",
                                             "frameId": None}])

    def test_a_model_that_cannot_be_reached_is_said_so(self):
        driver, script, out = Driver([COMPOSE]), Script([], [item()], fail=(0, TimeoutError("timed out"))), []
        events = engines.SmartEvents(out.append, driver=driver, frame=lambda: None)
        engines.meter(script, events.inference)
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            agent = engines.build_frontier(driver, script, apps={}, emit=events, config=QUICK)
            events.agent = agent
            result = agent.run("Text Sam hi")
        summary = engines.summarize(result, agent, events, request="Text Sam hi")
        self.assertEqual((summary["outcome"], summary["reason"]),
                         ("couldnt_finish", "Mobster couldn't reach OpenAI. Check your internet connection, then try again."))

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

    def test_summary_carries_the_outcome_answer_and_proof(self):
        _, _, result, _, events, agent = self.run_smart(
            [COMPOSE, COMPOSE, sent("I'm running late"), sent("I'm running late")],
            [[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)], [("DONE", None, None)]], [item()],
            answer="Sent Sam “I'm running late”. Nothing else was changed.")
        summary = engines.summarize(result, agent, events, request="Text Sam I'm running late", cost_usd=.012)
        self.assertEqual((summary["status"], summary["outcome"], summary["engine"]), ("completed", "done", "smart"))
        self.assertEqual(summary["answer"], "Sent Sam “I'm running late”.")
        self.assertEqual(summary["data"], "Sent Sam “I'm running late”. Nothing else was changed.")
        self.assertEqual(summary["resolved_output_format"], "text")
        self.assertIsNone(summary["reason"])
        self.assertEqual(summary["costUsd"], .012)
        self.assertEqual([p["quote"] for p in summary["proof"]], ["I'm running late"])


class StopTests(unittest.TestCase):
    """The app's Stop (FrontierAgent's ``cancelled``) ends the loop before its next action."""

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


class StepTextTests(unittest.TestCase):
    def test_steps_say_what_was_done(self):
        self.assertEqual(engines.step_text("TYPE", "Title", text="buy oat milk", field="field"),
                         "Typed “buy oat milk” in Title")
        self.assertEqual(engines.step_text("TYPE", "chat_compose_field", text="I'm late", field="composer"),
                         "Wrote the message")
        self.assertEqual(engines.step_text("TYPE", "Add a comment…", text="Nice", field="composer", again=True),
                         "Rewrote the comment")
        self.assertEqual(engines.step_text("TYPE_SUBMIT", "Search", text="Zara", field="search"), "Searched for “Zara”")
        self.assertEqual(engines.step_text("SET_TEXT", "Title", text="", field="field"), "Cleared Title")
        self.assertEqual(engines.step_text("SET_TEXT", "chat_compose_field", text="", field="composer"),
                         "Cleared the message")
        self.assertEqual(engines.step_text("TYPE", "Notes", text="x" * 80, field="field"), "Typed “" + "x" * 39 + "…” in Notes")
        self.assertEqual(engines.step_text("SCROLL_TO", None, text="Zara Okonkwo"), "Scrolled to Zara Okonkwo")
        self.assertEqual(engines.step_text("READ_LIST", None, where="Reminders"), "Read the Reminders list")
        self.assertEqual(engines.step_text("TAP", "searchButton"), "Tapped search button")

    def test_steps_name_what_was_swiped_and_what_an_icon_does(self):
        # From live Smart runs: "Swiped left" three times, "Tapped circle", "Closed an overlay".
        self.assertEqual(engines.step_text("SWIPE_LEFT", "Buy oat milk, Incomplete"), "Swiped left on Buy oat milk")
        self.assertEqual(engines.step_text("SWIPE_RIGHT", "Call Mom, Completed"), "Swiped right on Call Mom")
        self.assertEqual(engines.step_text("SWIPE_LEFT", None), "Swiped left")
        self.assertEqual(engines.step_text("SWIPE_UP", "Reminders list"), "Scrolled down")
        self.assertEqual(engines.step_text("TAP", "Buy oat milk, Incomplete"), "Tapped Buy oat milk")
        self.assertEqual(engines.step_text("TAP", "circle"), "Tapped the selection circle")
        self.assertEqual(engines.step_text("TAP", "ellipsis"), "Tapped More")
        self.assertEqual(engines.step_text("TAP", "ellipsis.circle"), "Tapped More")
        self.assertEqual(engines.step_text("TAP", "checkmark"), "Tapped Done")
        self.assertEqual(engines.step_text("TAP", "trash"), "Tapped Delete")
        self.assertEqual(engines.step_text("TAP", "square.and.pencil"), "Tapped Compose")
        self.assertEqual(engines.step_text("TAP", "Circle K"), "Tapped Circle K")  # a real name stays
        self.assertEqual(engines.step_text("DISMISS", None), "Closed a pop-up")

    def feed(self, *events):
        out = []
        smart = engines.SmartEvents(out.append, frame=lambda: b"jpeg")
        for event in events:
            smart(event)
        smart.finish()
        return [e["text"] for e in out if e["event"] == "step"]

    def test_typing_one_field_twice_in_a_row_is_one_step(self):
        decide = lambda *ops, label=None: {"event": "frontier_decision", "step": 0, "ops": list(ops),
                                           "operation": ops[0], "target_label": label}
        act = lambda op, label, text=None, index=0, field="field": {
            "event": "frontier_action", "step": 0, "index": index, "operation": op, "target_label": label,
            "text": text, "field": field if op in engines.TYPING else None}
        self.assertEqual(self.feed(decide("TYPE", label="Title"), act("TYPE", "Title", "buy milk"),
                                   decide("SET_TEXT", "TAP", label="Title"), act("SET_TEXT", "Title", "buy oat milk"),
                                   act("TAP", "Save", index=1)),
                         ["Typed “buy oat milk” in Title", "Tapped Save"])
        self.assertEqual(self.feed(decide("TYPE", "TYPE", label="Title"), act("TYPE", "Title", "a"),
                                   act("TYPE", "Notes", "b", index=1)),
                         ["Typed “a” in Title", "Typed “b” in Notes"])
        self.assertEqual(self.feed(decide("TYPE", label="Title"), act("TYPE", "Title", "a"),
                                   decide("TAP", label="Save"), act("TAP", "Save")),
                         ["Typed “a” in Title", "Tapped Save"])
        self.assertEqual(self.feed(decide("READ_LIST"), {"event": "frontier_action", "step": 0, "index": 0,
                                                         "operation": "READ_LIST", "target_label": None,
                                                         "app_name": "Reminders"}),
                         ["Read the Reminders list"])


class OutcomeTests(unittest.TestCase):
    def contract(self, *items):
        return K.Contract.from_items(list(items), "Text Sam and tell me the weather")

    def test_statuses_map_to_five_outcomes(self):
        proven = self.contract()
        self.assertEqual(engines.outcome_of("completed", proven, "x"), "done")
        self.assertEqual(engines.outcome_of("completed", None, "x"), "check")
        self.assertEqual(engines.outcome_of("max_steps", proven, "partial"), "check")
        self.assertEqual(engines.outcome_of("max_steps", proven, None), "couldnt_finish")
        self.assertEqual(engines.outcome_of("blocked", proven, "no"), "couldnt_finish")
        self.assertEqual(engines.outcome_of("approval_denied", proven, None), "declined")
        self.assertEqual(engines.outcome_of("stopped", proven, None), "stopped")
        self.assertEqual(engines.outcome_of("approval_timeout", proven, None), "stopped")

    def test_a_commit_proven_without_a_receipt_is_done(self):
        contract = self.contract(item("COMMIT", "Reminders", "reminder", "save", "Text Sam"))
        commit = contract.items[0]
        self.assertEqual(engines.item_state(contract, commit), "open")
        commit.claim = "done"
        contract.visited.add("Reminders")
        self.assertTrue(contract.proof(commit)[0])
        self.assertEqual(engines.item_state(contract, commit), "done")

    def test_a_message_is_never_proven_by_the_words_already_in_the_chat(self):
        contract = self.contract(item(what="message to Sam"))
        message = contract.items[0]
        contract.observe(sent("I'm running late"), "Messages", 1)  # last week's message, same words
        message.claim = "done"  # the model took it for this one: nothing was sent
        self.assertEqual(contract.proof(message), (False, "not done yet"))
        self.assertEqual(engines.item_state(contract, message), "open")

    def test_items_in_plain_words(self):
        items = self.contract(item(what="message to Zara Okonkwo", app="QuickChat"),
                              item("REPORT", "Weather", "the weather", "none", "the weather"),
                              item("WRITE", "Notes", "note", "none", "x", "Buy milk"),
                              item("COMMIT", "Reminders", "reminder", "save", "Text Sam"),
                              item("READ", "Settings", "iOS version in Settings", "none", "x")).items
        self.assertEqual([engines.item_text(i) for i in items],
                         ["Send the message to Zara Okonkwo in QuickChat", "Find the weather in Weather",
                          "Enter “Buy milk” in Notes", "Save reminder in Reminders", "Look at iOS version in Settings"])

    def test_the_first_sentence(self):
        self.assertEqual(engines.first_sentence("iOS 26.4 is installed. It came out in May."), "iOS 26.4 is installed.")
        self.assertEqual(engines.first_sentence("Done"), "Done")
        self.assertIsNone(engines.first_sentence("  "))

    def test_a_list_answer_keeps_its_headline_whole(self):
        """Review of #41: "Here are your reminders for today: 1." came out as the headline of a numbered list."""
        numbered = "Here are your reminders for today:\n1. Buy oat milk.\n2. Call the dentist at 10.\n3. Water the plants."
        bulleted = "Here are your reminders for today:\n- Buy oat milk.\n- Call the dentist at 10.\n- Water the plants."
        for text in (numbered, bulleted, bulleted.replace("- ", "• "), numbered.replace(".\n", "\n")):
            self.assertEqual(engines.first_sentence(text), "Here are your reminders for today:", text)
        # A list's number never ends a sentence, on its own line or inline after a colon; a time or an hour does.
        self.assertEqual(engines.first_sentence("Your reminders for today: 1. Buy oat milk. 2. Water the plants."),
                         "Your reminders for today: 1. Buy oat milk.")
        self.assertEqual(engines.first_sentence("1. Buy oat milk.\n2. Water the plants."), "1. Buy oat milk.")
        self.assertEqual(engines.first_sentence("Your alarm is set for 7:30. It repeats on weekdays."),
                         "Your alarm is set for 7:30.")
        self.assertEqual(engines.first_sentence("The dentist is at 10. Bring your card."), "The dentist is at 10.")
        # A first line with its own sentence end is still one sentence; a short one is no headline.
        self.assertEqual(engines.first_sentence("You have three reminders.\n1. Buy oat milk.\n2. Call Sam."),
                         "You have three reminders.")
        self.assertEqual(engines.first_sentence("Yes.\nThe store opens at 9."), "Yes. The store opens at 9.")
        # A single line with no sentence end is the whole answer, as before.
        self.assertEqual(engines.first_sentence("Buy oat milk, call the dentist"), "Buy oat milk, call the dentist")

    def test_a_smart_list_answer_is_a_headline_over_the_list(self):
        """summarize: the run's answer is the list's own first line; data keeps the list as written."""
        numbered = "Here are your reminders for today:\n1. Buy oat milk.\n2. Call the dentist at 10.\n3. Water the plants."
        result = engines.summarize({"status": "completed", "answer": numbered, "actions": 2, "steps": 3, "elapsed": 1.0},
                                   None, None, request="What are my reminders today?")
        self.assertEqual(result["answer"], "Here are your reminders for today:")
        self.assertEqual(result["data"], numbered)

    def test_answers_read_like_the_steps_without_a_first_person_subject(self):
        drop = engines.without_first_person
        self.assertEqual(drop("I added and saved the reminder “Buy oat milk”."),
                         "Added and saved the reminder “Buy oat milk”.")
        self.assertEqual(drop("I couldn't find a reminder called Buy oat milk."),
                         "Couldn't find a reminder called Buy oat milk.")
        self.assertEqual(drop("I couldn’t find a reminder called Buy oat milk."),
                         "Couldn’t find a reminder called Buy oat milk.")
        self.assertEqual(drop("I found 3 unread emails."), "Found 3 unread emails.")
        self.assertEqual(drop("Added the reminder."), "Added the reminder.")
        self.assertEqual(drop("iOS 26.4 is installed."), "iOS 26.4 is installed.")
        self.assertEqual(drop("I-95 is clear."), "I-95 is clear.")
        self.assertEqual(drop("I'm not able to see the battery."), "I'm not able to see the battery.")
        self.assertEqual(drop(""), "")
        self.assertIsNone(drop(None))

    def test_the_subject_stays_unless_a_past_tense_verb_follows(self):
        # Without "I" these read as instructions or broken sentences, or change what the phone said.
        for kept in ("I have added the reminder “Buy oat milk”.", "I had 3 missed calls.",
                     "I don't see a note called Trip.", "I do not have access to Health.",
                     "I did not find a reminder called Trip.", "I didn't find it.", "I was not able to open it.",
                     "I should mention the note is empty.", "I could not open Notes.", "I see 3 unread emails.",
                     "I need a passcode to continue.", "I miss you, call me when you land.", "I 95 is closed."):
            self.assertEqual(engines.without_first_person(kept), kept)

    def test_only_the_one_line_answer_drops_it(self):
        # The run's answer and data are what the phone showed: the bench and exports read them verbatim.
        raw = "I added the reminder “Buy oat milk”. It is due today."
        outcome = {"status": "completed", "answer": raw, "steps": 4, "elapsed": 1.0}
        added = K.Contract.from_items([item("WRITE", "Reminders", "reminder", "none", "Add a reminder", "Buy oat milk")],
                                      "Add a reminder")
        summary = engines.summarize(outcome, SimpleNamespace(_contract=added), None, request="Add a reminder")
        self.assertEqual(summary["answer"], "Added the reminder “Buy oat milk”.")
        self.assertEqual(summary["data"], raw)
        note = "I left the keys under the mat. Text me when you land."
        summary = engines.summarize({"status": "completed", "answer": note, "steps": 2, "elapsed": 1.0},
                                    SimpleNamespace(_contract=None), None, request="Read my last note")
        self.assertEqual(summary["data"], note)

    def test_words_read_on_the_phone_keep_their_i(self):
        """AUDIT-3: "Read my last note" answered "Left the keys under the mat." right above the proof quoting
        "I left the keys under the mat.". A task that only reads, or a sentence a proof quote holds, is the
        phone's own words; only what the agent did loses its "I"."""
        class Events:
            def __init__(self, *quotes):
                self.quotes = quotes

            def final_proof(self):
                return [{"quote": q, "app": "Notes", "screen": "Note", "frameId": None} for q in self.quotes]

        read = K.Contract.from_items([item("REPORT", "Notes", "the last note", "none", "Read my last note")],
                                     "Read my last note")
        note = "I left the keys under the mat. Text me when you land."
        done = {"status": "completed", "steps": 2, "elapsed": 1.0}
        summary = engines.summarize({**done, "answer": note}, SimpleNamespace(_contract=read),
                                    Events("I left the keys under the mat."), request="Read my last note")
        self.assertEqual(summary["answer"], "I left the keys under the mat.")
        self.assertEqual(summary["data"], note)
        for said in ("I made it home, thanks for dinner!", "I sent Dana the invoice?"):
            summary = engines.summarize({**done, "answer": said}, SimpleNamespace(_contract=read), Events(),
                                        request="Read my last message")
            self.assertEqual(summary["answer"], said)
        # A task that acts still drops it, unless the sentence is a quote the proof holds (a note it wrote).
        wrote = K.Contract.from_items([item("WRITE", "Notes", "note", "none", "Write a note", "I left the keys")],
                                      "Write a note")
        summary = engines.summarize({**done, "answer": "I wrote the note “I left the keys”."},
                                    SimpleNamespace(_contract=wrote), Events("I left the keys"), request="Write a note")
        self.assertEqual(summary["answer"], "Wrote the note “I left the keys”.")
        summary = engines.summarize({**done, "answer": "I left the keys under the mat."},
                                    SimpleNamespace(_contract=wrote), Events("I left the keys under the mat"),
                                    request="Write a note")
        self.assertEqual(summary["answer"], "I left the keys under the mat.")

    def test_a_schema_is_filled_from_the_answer_as_written(self):
        seen = []

        class Client:
            def complete(self, messages, schema, timeout=60):
                seen.append(messages[1]["content"][0]["text"])
                return {"data": {"where": "under the mat"}}, {}

        schema = {"type": "object", "required": ["where"], "properties": {"where": {"type": "string"}}}
        outcome = {"status": "completed", "answer": "I left the keys under the mat.", "steps": 2, "elapsed": 1.0}
        summary = engines.summarize(outcome, SimpleNamespace(_contract=None, client=Client()), None,
                                    request="Where are the keys?", output_schema=schema)
        self.assertIn("Answer: I left the keys under the mat.", seen[0])
        self.assertEqual(summary["full_answer"], "I left the keys under the mat.")

    def test_automatic_csv_comes_back_as_rows(self):
        """DASHBOARD-7: Result CSV (or JSON, YAML) with no schema of the user's came back as plain text on Smart.
        One small call names the columns and fills the rows; code builds the records and their schema."""
        class Client:
            model = "gpt-5.6-sol"

            def __init__(self, table):
                self.table, self.schemas = table, []

            def complete(self, messages, schema, timeout=60):
                self.schemas.append(schema)
                return self.table, {}

        answer = "You have 3 reminders: Pick up dry cleaning (tomorrow 9 AM), Call the dentist (Friday), Renew passport."
        rows = {"columns": ["title", "due"], "single": False,
                "rows": [["Pick up dry cleaning", "tomorrow 9 AM"], ["Call the dentist", "Friday"], ["Renew passport", None]]}
        client = Client(rows)
        outcome = {"status": "completed", "answer": answer, "steps": 3, "elapsed": 9.0}
        summary = engines.summarize(outcome, SimpleNamespace(_contract=None, client=client), None,
                                    request="Export my reminders as CSV", output_format="csv")
        self.assertEqual(client.schemas, [engines.TABLE])
        self.assertEqual(summary["resolved_output_format"], "csv")
        self.assertEqual(summary["data"], [{"title": "Pick up dry cleaning", "due": "tomorrow 9 AM"},
                                           {"title": "Call the dentist", "due": "Friday"},
                                           {"title": "Renew passport", "due": None}])
        self.assertEqual((summary["schema_validated"], summary["schema_source"], summary["data_status"]),
                         (True, "automatic", "validated"))
        self.assertEqual(list(summary["output_schema"]["items"]["properties"]), ["title", "due"])
        self.assertEqual(summary["full_answer"], answer)
        self.assertEqual(summary["answer"], "You have 3 reminders: Pick up dry cleaning (tomorrow 9 AM), Call the "
                                            "dentist (Friday), Renew passport.")
        # One thing is one record, for JSON and YAML too.
        one = Client({"columns": ["ios", "model"], "rows": [["26.4", "iPhone 17 Pro"]], "single": True})
        summary = engines.summarize({**outcome, "answer": "iOS 26.4 on an iPhone 17 Pro."},
                                    SimpleNamespace(_contract=None, client=one), None, request="iOS version and model",
                                    output_format="yaml")
        self.assertEqual((summary["data"], summary["resolved_output_format"]),
                         ({"ios": "26.4", "model": "iPhone 17 Pro"}, "yaml"))
        # Text and Markdown keep the answer as written, with no extra call.
        for fmt in ("auto", "text", "markdown"):
            spare = Client(rows)
            summary = engines.summarize(outcome, SimpleNamespace(_contract=None, client=spare), None, request="x",
                                        output_format=fmt)
            self.assertEqual((summary["data"], spare.schemas), (answer, []))

    def test_an_answer_that_wont_structure_falls_back_to_text_with_a_note(self):
        class Client:
            model = "gpt-5.6-sol"

            def __init__(self, reply):
                self.reply = reply

            def complete(self, messages, schema, timeout=60):
                if isinstance(self.reply, Exception):
                    raise self.reply
                return self.reply, {}

        answer = "You have no reminders due this week."
        outcome = {"status": "completed", "answer": answer, "steps": 3, "elapsed": 9.0}
        for reply in ({"columns": ["title", "title"], "rows": [], "single": False},
                      {"columns": ["title", "due"], "rows": [["only one value"]], "single": False},
                      {"columns": ["title"], "rows": [["a"], ["b"]], "single": True},
                      {"columns": [], "rows": [], "single": False},
                      RuntimeError('OpenAI HTTP 500: {"error": "server"}')):
            summary = engines.summarize(outcome, SimpleNamespace(_contract=None, client=Client(reply)), None,
                                        request="Export my reminders as CSV", output_format="csv")
            self.assertEqual((summary["data"], summary["resolved_output_format"], summary["data_status"]),
                             (answer, "text", "observed"), reply)
            self.assertEqual(summary["output_note"], "Mobster couldn’t turn this answer into CSV, so it’s shown as text.")
            self.assertTrue(summary["structure_error"])
        # An empty list is a real answer: a table with its columns and no rows.
        summary = engines.summarize(outcome, SimpleNamespace(
            _contract=None, client=Client({"columns": ["title"], "rows": [], "single": False})), None,
            request="Export my reminders as CSV", output_format="csv")
        self.assertEqual((summary["data"], summary["resolved_output_format"]), ([], "csv"))

    def test_the_notes_use_curly_apostrophes(self):
        """Review 2 of #41: the run view shows ``output_note`` among strings that all spell "couldn’t" curly."""
        for output_format in engines.AUTOMATIC_FORMATS:
            for problem in ("The table's columns and rows did not line up.",
                            "RuntimeError: OpenAI HTTP 429: insufficient_quota"):
                note = engines.structure_note(output_format, problem)
                self.assertNotIn("'", note)
                self.assertIn("it’s shown as text", note)

    def test_a_structuring_call_that_finds_no_credit_says_so(self):
        """Review of #41: the 160-character cut dropped insufficient_quota from OpenAI's body, so the run said
        "couldn't turn this answer into CSV" and Smart stayed on offer."""
        quota = RuntimeError('OpenAI HTTP 429: {"error": {"message": "You exceeded your current quota, please check '
                             'your plan and billing details. For more information on this error, read the docs: '
                             'https://platform.openai.com/docs/guides/error-codes/api-errors.", "type": '
                             '"insufficient_quota", "param": null, "code": "insufficient_quota"}}')
        self.assertIsNone(engines.model_failure(f"RuntimeError: {str(quota)[:160]}")[0])  # the old cut

        class Client:
            model = "gpt-5.6-sol"

            def complete(self, messages, schema, timeout=60):
                raise quota

        answer = "You have 2 reminders: Buy oat milk, Call the dentist."
        outcome = {"status": "completed", "answer": answer, "steps": 3, "elapsed": 9.0}
        summary = engines.summarize(outcome, SimpleNamespace(_contract=None, client=Client()), None,
                                    request="Export my reminders as CSV", output_format="csv")
        self.assertEqual(summary["structure_error"], "RuntimeError: OpenAI HTTP 429: insufficient_quota")
        self.assertEqual(engines.model_failure(summary["structure_error"]), (False, engines.NO_CREDIT))
        self.assertEqual(engines.plain_error(summary["structure_error"]), engines.NO_CREDIT)
        self.assertEqual((summary["data"], summary["resolved_output_format"]), (answer, "text"))
        self.assertEqual(summary["output_note"], "Your OpenAI account ran out of credit before Mobster could turn "
                                                 "this answer into CSV, so it’s shown as text.")
        # A schema's call says the same; a 402 with no code keeps its status.
        schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
        summary = engines.summarize(outcome, SimpleNamespace(_contract=None, client=Client()), None,
                                    request="How many reminders?", output_schema=schema)
        self.assertEqual(engines.model_failure(summary["structure_error"]), (False, engines.NO_CREDIT))
        self.assertIn("ran out of credit", summary["output_note"])
        self.assertEqual(engines.call_problem(RuntimeError("OpenAI HTTP 402: " + "x" * 400)),
                         "RuntimeError: OpenAI HTTP 402")
        # Any other failure is cut as before.
        self.assertEqual(engines.call_problem(RuntimeError("OpenAI HTTP 500: " + "x" * 400)),
                         ("RuntimeError: OpenAI HTTP 500: " + "x" * 400)[:160])

    def test_the_estimate_counts_the_structuring_call(self):
        plain = engines.smart_estimate("Export my reminders", ["Reminders"])
        structured = engines.smart_estimate("Export my reminders", ["Reminders"], structured=True)
        self.assertAlmostEqual(engines.midpoint(structured) - engines.midpoint(plain), engines.SMART_STRUCTURE_USD,
                               delta=.001)

    def test_structured_output_is_validated_against_the_schema(self):
        schema = {"type": "object", "additionalProperties": False, "required": ["version"],
                  "properties": {"version": {"type": "string"}}}

        class Client:
            model = "gpt-5.6-sol"

            def __init__(self, data):
                self.data, self.bodies = data, []

            def body(self, messages, schema_):
                return {"tools": [{"strict": True, "parameters": schema_}], "reasoning": {"effort": "low"}}

            def complete(self, messages, schema_, timeout=60):
                self.bodies.append(self.body(messages, schema_))
                return {"data": self.data}, {}

        good = Client({"version": "26.4"})
        self.assertEqual(engines.structure(good, "r", "iOS 26.4", [], schema), ({"version": "26.4"}, None))
        self.assertFalse(good.bodies[0]["tools"][0]["strict"])
        self.assertEqual(good.bodies[0]["reasoning"], {"effort": "none"})
        self.assertNotIn("body", vars(good))  # the non-strict body was only for that call
        self.assertIsNone(engines.structure(Client({"version": 26}), "r", "x", [], schema)[0])


class PlanLineTests(unittest.TestCase):
    def test_plan_lines(self):
        c = K.Contract.from_items([item(what="Sam"), item(kind="COMMIT", app="Reminders", what="reminder", act="save",
                                                          quote="Text Sam")], "Text Sam")
        self.assertEqual(K.plan_line(c), "This task will send 1 message to Sam.")
        self.assertIsNone(K.plan_line(K.Contract.from_items([item("REPORT", what="weather", act="none")], "x")))
        self.assertIsNone(K.plan_line(None))
        pay = K.Contract.from_items([item(app="Venmo", what="Maya", act="pay", quote="pay Maya $12")], "pay Maya $12")
        self.assertEqual(K.plan_line(pay), "This task will make 1 payment to Maya.")
        self.assertEqual(K.approval_title(pay.items[0], 12), "Pay $12.00 to Maya?")
        follow = K.Contract.from_items([item(app="X", what="@nasa", act="follow", quote="follow")], "follow @nasa")
        self.assertEqual(K.approval_title(follow.items[0]), "Follow @nasa?")
        self.assertFalse(K.asks(K.Item(1, "COMMIT", act="save")))
        self.assertTrue(K.asks(K.Item(1, "COMMIT", act="delete")))
        self.assertFalse(K.asks(K.Item(1, "WRITE")))



class CheckTheAnswerTests(unittest.TestCase):
    """B6 (5 Oct): right answers labelled "Check the answer" or "Already done"."""

    def run_smart(self, screens, steps, items, request, answer, updates=None):
        driver, script, out = Driver(screens), Script(steps, items, answer, updates), []
        events = engines.SmartEvents(out.append, driver=driver, frame=lambda: None,
                                     apps={"com.apple.mobilecal": "Calendar", "com.google.ios.youtube": "YouTube",
                                           "com.apple.Preferences": "Settings"})
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            agent = engines.build_frontier(driver, script, apps=events.apps, emit=events, config=QUICK, skills=())
            events.agent = agent
            result = agent.run(request)
            events.finish()
            return engines.summarize(result, agent, events, request=request), out

    def test_an_empty_day_answer_ends_done(self):
        day = json.loads((Path(__file__).parent / "fixtures" / "frontier" / "calendar_empty_day.json").read_text())
        rows = day["screens"]["day"]
        snapshot = screen(*[Element(r["id"], r["label"], r["role"], tuple(r["rect"])) for r in rows],
                          bundle="com.apple.mobilecal")
        summary, out = self.run_smart(
            [snapshot] * 3, [[("DONE", None, None)], [("DONE", None, None)]],
            [item("REPORT", "Calendar", "events tomorrow", "none", "What's on my calendar tomorrow")],
            "What's on my calendar tomorrow?", "No events tomorrow.",
            updates={0: [{"id": 1, "status": "found", "value": "No events tomorrow"}]})
        self.assertEqual((summary["status"], summary["outcome"]), ("completed", "done"))
        self.assertNotIn("chevron.forward", json.dumps(summary["proof"]))

    def test_a_search_typed_as_the_requested_write_is_not_already_done(self):
        c = K.Contract.from_items([item("WRITE", "YouTube", "search query", "none", "Enter 'pasta carbonara recipe'",
                                        "pasta carbonara recipe")], "Enter 'pasta carbonara recipe' in YouTube")
        search = Element("1", "Search YouTube", "SearchField", (.1, .1, .8, .05), editable=True)
        c.record_typed("YouTube", search, "pasta carbonara recipe", 1)
        self.assertEqual(engines.untouched_writes(c), [])
        other = K.Contract.from_items([item("WRITE", "Notes", "note body", "none", "Write a note", "buy milk")],
                                      "Write a note: buy milk")
        other.record_typed("Notes", search, "groceries", 1)
        self.assertEqual(len(engines.untouched_writes(other)), 1)  # an unrelated search is still not the write

    def test_a_value_is_never_quoted_from_an_sf_symbol_or_inside_a_word(self):
        c = K.Contract.from_items([item("REPORT", "Settings", "Bluetooth state", "none", "is Bluetooth on")],
                                  "In Settings, is Bluetooth on or off?")
        c.observe(screen(Element("1", "chevron.forward", "Image", (.9, .3, .05, .03)),
                         Element("2", "Bluetooth", "Button", (.05, .3, .8, .05), value="On")), "Settings", 0)
        c.update([{"id": 1, "status": "found", "value": "on"}], 0)
        row, _ = engines.SmartEvents._report_row(c, c.items[0])
        self.assertEqual(row, "Bluetooth On")
        c2 = K.Contract.from_items([item("REPORT", "Settings", "Bluetooth state", "none", "is Bluetooth on")], "x")
        c2.observe(screen(Element("1", "chevron.forward", "Image", (.9, .3, .05, .03))), "Settings", 0)
        c2.update([{"id": 1, "status": "found", "value": "on"}], 0)
        self.assertEqual(engines.SmartEvents._report_row(c2, c2.items[0]), (None, None))

    def test_blocked_always_carries_the_models_own_sentence(self):
        from mobile_agent.frontier import FrontierAgent
        sentence = "The Wi-Fi settings need your passcode, which Mobster can't enter."

        class Blocking(Script):
            def complete(self, messages, schema, timeout=60, **kwargs):
                out, usage = super().complete(messages, schema, timeout)
                if "actions" in out:
                    out = {**out, "answer": None, "actions": [{**out["actions"][0], "text": sentence}]}
                return out, usage
        script = Blocking([[("BLOCKED", None, None)]], [])
        with patch.dict(os.environ, dict(CLEAN), clear=True):
            result = FrontierAgent(Driver([COMPOSE] * 2), script, screenshots=False, settle_seconds=0,
                                   skills=()).run("Join the office Wi-Fi.")
        self.assertEqual((result["status"], result["answer"], result["reason"]), ("blocked", sentence, sentence))
        summary = engines.summarize(result, SimpleNamespace(client=script, _contract=None), None,
                                    request="Join the office Wi-Fi.")
        self.assertEqual(summary["reason"], sentence)

    def test_a_guard_stop_keeps_its_code_in_the_summary(self):
        outcome = {"status": "blocked", "answer": None, "reason": "Your iPhone was unplugged. Plug it back in and try "
                   "again.", "code": "unplugged", "steps": 1, "actions": 0, "elapsed": 1.0}
        summary = engines.summarize(outcome, SimpleNamespace(client=None, _contract=None), None, request="x")
        self.assertEqual((summary["code"], summary["reason"], summary["outcome"]),
                         ("unplugged", "Your iPhone was unplugged. Plug it back in and try again.", "couldnt_finish"))

    def test_the_install_act_reads_in_plain_words(self):
        install = K.Contract.from_items([item("COMMIT", "App Store", "Duolingo", "install", "Install Duolingo")],
                                        "Install Duolingo").items[0]
        self.assertEqual(engines.item_text(install), "Install Duolingo in App Store")


if __name__ == "__main__":
    unittest.main()
