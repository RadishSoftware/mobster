import json
import statistics
import unittest
from pathlib import Path

from mobile_agent import costs, engines
from mobile_agent.journal import Journal
from mobile_agent.tests.test_engines import Script

SOL = engines.OPENAI_SMART_MODEL


class EstimateTests(unittest.TestCase):
    def test_longer_multi_app_tasks_cost_more_and_short_ones_cents(self):
        short = costs.smart_estimate("What iOS version is this iPhone on?", ["Settings"])
        long = costs.smart_estimate("Check Uber and Lyft for a ride to SFO, then text Sam the cheaper one",
                                      ["Uber", "Lyft", "Messages"])
        self.assertLess(short["highUsd"], long["lowUsd"] * 3)
        self.assertLess(short["lowUsd"], short["highUsd"])
        self.assertLess(long["highUsd"], .75)
        self.assertTrue(.02 < costs.midpoint(short) < .12)
        self.assertEqual((short["model"], short["provider"], short["available"]),
                         ("claude-sonnet-5-5", "Anthropic", True))
        self.assertAlmostEqual(short["highUsd"] / short["lowUsd"], 4, delta=.05)

    def test_the_estimate_holds_on_recorded_runs(self):
        runs = json.loads((Path(__file__).parent / "smart_costs.json").read_text())["runs"]
        self.assertEqual(costs.SMART_COST_RATIO[SOL], 1.0)
        ratios = {split: [r["costUsd"] / costs.midpoint(costs.smart_estimate(r["goal"], r["apps"], model=SOL))
                          for r in runs if r["split"] == split] for split in ("fit", "heldout")}
        self.assertEqual((len(ratios["fit"]), len(ratios["heldout"])), (128, 56))
        within = {split: sum(.5 <= x <= 2 for x in values) / len(values) for split, values in ratios.items()}
        self.assertGreaterEqual(within["heldout"], .80)
        self.assertGreaterEqual(within["fit"], .78)
        for values in ratios.values():
            self.assertTrue(.6 < statistics.median(values) < 1.5)


class MeterTests(unittest.TestCase):
    def test_every_call_is_priced_and_recorded_in_the_usage_ledger(self):
        journal = Journal()
        self.addCleanup(journal.close)
        journal.create({"id": "r1", "finishedAt": None})
        events = []
        client = Script([[("DONE", None, None)]], [])
        costs.meter(client, events.append)
        client.complete([{"role": "system", "content": "x"}, {"role": "user", "content": [
            {"type": "text", "text": "Screen elements:\n"}]}], {"properties": {"actions": {}}})
        self.assertEqual([e["event"] for e in events], ["inference_started", "inference_finished"])
        finished = events[1]
        self.assertEqual((finished["provider"], finished["model"], finished["purpose"]),
                         ("openai", "gpt-5.6-sol", "decision"))
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

        client = costs.meter(Down(), events.append)
        with self.assertRaises(RuntimeError):
            client.complete([], {"properties": {}})
        self.assertEqual((events[1]["success"], events[1]["cost_nanodollars"]), (False, None))
