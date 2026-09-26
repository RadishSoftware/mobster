"""One error family, one retry policy, one budget definition. No devices."""

import unittest

from mobile_agent.agent import Agent
from mobile_agent.api_errors import APIError
from mobile_agent.config import RunBudgets
from mobile_agent.demo import DemoDriver, DemoModel
from mobile_agent.drivers import DriverRejection
from mobile_agent.errors import MobsterError
from mobile_agent.inference import ProviderResponseRejected
from mobile_agent.journal import JournalError
from mobile_agent.pool import NoDeviceAvailable
from mobile_agent.transport import TransportError
from mobile_agent.workflows import WorkflowDispatchUncertain


class ErrorFamilyTests(unittest.TestCase):
    def test_domain_errors_share_one_root(self):
        for cls in (TransportError, JournalError, APIError, NoDeviceAvailable,
                    DriverRejection, ProviderResponseRejected, WorkflowDispatchUncertain):
            self.assertTrue(issubclass(cls, MobsterError), cls.__name__)
            self.assertTrue(issubclass(cls, RuntimeError), cls.__name__)

    def test_nothing_is_retryable_by_default(self):
        for cls in (TransportError, JournalError, APIError, NoDeviceAvailable,
                    DriverRejection, ProviderResponseRejected):
            self.assertIs(cls.retryable, False, cls.__name__)

    def test_stdlib_meanings_are_untouched(self):
        self.assertFalse(issubclass(ValueError, MobsterError))
        self.assertFalse(issubclass(TimeoutError, MobsterError))

    def test_api_error_shape_survives_rebasing(self):
        error = APIError("busy", 503, "device_busy", activeRunId="abc")
        self.assertEqual((error.status, error.code), (503, "device_busy"))
        self.assertEqual(error.details, {"activeRunId": "abc"})


class RunBudgetTests(unittest.TestCase):
    def test_defaults_match_agent(self):
        budgets = RunBudgets()
        agent = Agent(DemoDriver(), DemoModel())
        for field in ("max_steps", "max_seconds", "max_helper_calls", "settle_seconds",
                      "max_undispatched", "max_extraction_retries"):
            self.assertEqual(getattr(budgets, field), getattr(agent, field), field)
        self.assertEqual(agent.budgets, budgets)

    def test_invalid_budgets_rejected_with_agent_contract(self):
        bad = [dict(max_steps=0), dict(max_steps=10001), dict(max_steps="30"),
               dict(max_seconds=0), dict(max_seconds=-1), dict(max_seconds=float("nan")),
               dict(max_seconds=float("inf")), dict(settle_seconds=-.1), dict(settle_seconds=6),
               dict(settle_seconds=float("nan")), dict(max_helper_calls=-1),
               dict(max_undispatched=101), dict(max_extraction_retries=11)]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RunBudgets(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Agent(DemoDriver(), DemoModel(), **kwargs)

    def test_boundaries_accepted(self):
        budgets = RunBudgets(max_steps=1, max_seconds=.001, max_helper_calls=0,
                             settle_seconds=0, max_undispatched=0, max_extraction_retries=0)
        self.assertEqual(budgets.max_steps, 1)
        budgets = RunBudgets(max_steps=10000, settle_seconds=5, max_undispatched=100,
                             max_extraction_retries=10, max_helper_calls=10000)
        self.assertEqual(budgets.settle_seconds, 5)
