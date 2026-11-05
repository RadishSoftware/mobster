import unittest

from mobile_agent.costs import NANODOLLARS, SpendLedger, usd_to_nanodollars
from mobile_agent.frontier import FrontierAgent
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import Driver, Script, screen


def priced(nanos):
    return {"event": "inference_finished", "cost_nanodollars": nanos}


class ConversionTests(unittest.TestCase):
    def test_none_stays_unenforced(self):
        self.assertIsNone(usd_to_nanodollars(None))

    def test_valid_amounts_convert_to_exact_integers(self):
        self.assertEqual(usd_to_nanodollars(1), NANODOLLARS)
        self.assertEqual(usd_to_nanodollars(0.042), 42_000_000)
        result = usd_to_nanodollars(2.5)
        self.assertIs(type(result), int)
        self.assertEqual(result, 2_500_000_000)

    def test_invalid_amounts_rejected(self):
        for value in (0, -1, float("nan"), float("inf"), "1", True, [1]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                usd_to_nanodollars(value)


class LedgerTests(unittest.TestCase):
    def test_only_inference_finished_counts(self):
        ledger = SpendLedger()
        self.assertFalse(ledger.add_event({"event": "inference_started"}))
        self.assertFalse(ledger.add_event({"event": "decision"}))
        self.assertFalse(ledger.add_event(None))
        self.assertFalse(ledger.add_event("inference_finished"))
        self.assertEqual((ledger.cost_nanodollars, ledger.priced_calls, ledger.unpriced_calls), (0, 0, 0))

    def test_priced_spend_sums(self):
        ledger = SpendLedger()
        self.assertTrue(ledger.add_event(priced(100)))
        self.assertTrue(ledger.add_event(priced(50)))
        self.assertEqual(ledger.cost_nanodollars, 150)
        self.assertEqual(ledger.priced_calls, 2)

    def test_unpriced_calls_counted_never_zero_filled(self):
        ledger = SpendLedger()
        for cost in (None, 1.5, -1, 9_007_199_254_740_992, "100"):
            self.assertTrue(ledger.add_event(priced(cost)))
        self.assertEqual(ledger.cost_nanodollars, 0)
        self.assertEqual(ledger.priced_calls, 0)
        self.assertEqual(ledger.unpriced_calls, 5)


class CapEnforcementTests(unittest.TestCase):

    def run_capped(self, cap, spend_on_first_call):
        row = screen(Element("1", "Next", "Button", (.1, .1, .2, .05)))
        script = Script([("TAP", "Next", None), ("TAP", "Next", None), ("DONE", None, None), ("DONE", None, None),
                         ("DONE", None, None)])
        script.model = "gpt-5.5"
        complete, schemas = script.complete, []

        def priced(messages, schema, timeout=60):
            if "items" not in schema["properties"]:
                schemas.append(schema)
                if len(schemas) == 1:
                    script.usage["prompt_tokens"] += spend_on_first_call
            return complete(messages, schema, timeout)

        script.complete = priced
        driver = Driver([row] * 8)
        result = FrontierAgent(driver, script, screenshots=False, settle_seconds=0,
                               max_cost_usd=cap).run("Tap next and add it.")
        return result, driver, schemas

    def test_mid_run_spend_stops_every_action_after_it(self):
        result, driver, schemas = self.run_capped(1.0, 2_000_000)
        self.assertEqual(result["status"], "budget")
        self.assertEqual(driver.actions, [("TAP", "Next", None)])
        self.assertEqual(len(schemas), 2)
        self.assertEqual(schemas[-1]["properties"]["actions"]["items"]["properties"]["op"]["enum"], ["DONE", "BLOCKED"])

    def test_spend_below_cap_runs_normally(self):
        result, driver, _ = self.run_capped(100.0, 1_000)
        self.assertNotEqual(result["status"], "budget")
        self.assertEqual(driver.actions[0], ("TAP", "Next", None))
        self.assertGreaterEqual(len(driver.actions), 2)


if __name__ == "__main__":
    unittest.main()
