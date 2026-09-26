"""Pre-dispatch spend guard: observed priced spend stops further model calls."""

import unittest

from mobile_agent.agent import Agent
from mobile_agent.config import RunBudgets
from mobile_agent.costs import NANODOLLARS, SpendLedger, usd_to_nanodollars
from mobile_agent.demo import DemoDriver, DemoHelper, DemoModel
from mobile_agent.errors import MobsterError, SpendCapExceeded


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

    def test_cap_comparison_is_exact(self):
        ledger = SpendLedger()
        ledger.add_event(priced(42_000_000))
        self.assertTrue(ledger.at_cap(42_000_000))
        self.assertFalse(ledger.at_cap(42_000_001))


class CapValidationTests(unittest.TestCase):
    def test_budgets_default_to_unenforced(self):
        self.assertIsNone(RunBudgets().spend_cap_usd)

    def test_budgets_reject_invalid_caps(self):
        for value in (0, -2, float("nan"), float("inf"), "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RunBudgets(spend_cap_usd=value)

    def test_cap_without_ledger_fails_closed(self):
        with self.assertRaises(ValueError):
            Agent(DemoDriver(), DemoModel(), spend_cap_usd=1)

    def test_non_ledger_rejected(self):
        with self.assertRaises(ValueError):
            Agent(DemoDriver(), DemoModel(), spend_cap_usd=1, spend_ledger=object())

    def test_cap_error_is_a_non_retryable_domain_error(self):
        self.assertTrue(issubclass(SpendCapExceeded, MobsterError))
        self.assertIs(SpendCapExceeded.retryable, False)
        self.assertFalse(issubclass(SpendCapExceeded, TimeoutError))


class CapEnforcementTests(unittest.TestCase):
    def test_spend_already_at_cap_stops_before_any_dispatch(self):
        ledger = SpendLedger()
        ledger.add_event(priced(NANODOLLARS))
        driver = DemoDriver()
        result = Agent(driver, DemoModel(), DemoHelper(), spend_cap_usd=1,
                       spend_ledger=ledger).run("Search coffee", execute=True)
        self.assertEqual(result["status"], "spend_cap")
        self.assertIn("spend cap", result["reason"])
        self.assertEqual(driver.actions, [])
        self.assertEqual(result["attempted_actions"], 0)
        self.assertEqual(result["decisions"], 0)

    def test_spend_below_cap_runs_normally(self):
        ledger = SpendLedger()
        ledger.add_event(priced(1_000))
        driver = DemoDriver()
        result = Agent(driver, DemoModel(), DemoHelper(), spend_cap_usd=100,
                       spend_ledger=ledger).run("Search for coffee", execute=True,
                                               expected_text="Coffee brewing guide")
        self.assertEqual(result["status"], "expected_text_visible")

    def test_mid_run_spend_stops_before_dispatch(self):
        ledger = SpendLedger()
        driver, model = DemoDriver(), DemoModel()
        decide = model.decide

        def expensive(*args, **kwargs):
            ledger.add_event(priced(10 * NANODOLLARS))
            return decide(*args, **kwargs)

        model.decide = expensive
        result = Agent(driver, model, DemoHelper(), spend_cap_usd=1,
                       spend_ledger=ledger).run("Search coffee", execute=True)
        self.assertEqual(result["status"], "spend_cap")
        self.assertEqual(driver.actions, [])
        self.assertEqual(result["attempted_actions"], 0)

    def test_no_cap_preserves_existing_behavior(self):
        result = Agent(DemoDriver(), DemoModel(), DemoHelper(),
                       spend_ledger=SpendLedger()).run(
            "Search for coffee", execute=True, expected_text="Coffee brewing guide")
        self.assertEqual(result["status"], "expected_text_visible")
