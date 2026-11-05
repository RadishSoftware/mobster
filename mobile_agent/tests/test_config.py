import unittest

from mobile_agent.api_errors import APIError
from mobile_agent.errors import MobsterError
from mobile_agent.journal import JournalError
from mobile_agent.pool import NoDeviceAvailable
from mobile_agent.transport import TransportError
from mobile_agent.wda.guards import DriverRejection
from mobile_agent.workflows.runs import WorkflowDispatchUncertain


class ErrorFamilyTests(unittest.TestCase):
    def test_domain_errors_share_one_root(self):
        for cls in (TransportError, JournalError, APIError, NoDeviceAvailable, DriverRejection,
                    WorkflowDispatchUncertain):
            self.assertTrue(issubclass(cls, MobsterError), cls.__name__)
            self.assertTrue(issubclass(cls, RuntimeError), cls.__name__)

    def test_nothing_is_retryable_by_default(self):
        for cls in (TransportError, JournalError, APIError, NoDeviceAvailable, DriverRejection):
            self.assertIs(cls.retryable, False, cls.__name__)
