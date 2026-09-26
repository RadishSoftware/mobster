"""Before a task: a locked or wedged phone is refused plainly, overlays are cleared. Offline."""

import unittest
from unittest.mock import Mock

from mobile_agent.server import PhoneNotReady, prepare_wda_phone


def driver(locked=False, locked_error=None, home_error=None):
    d = Mock()
    d.http.request.side_effect = locked_error or (lambda *a: {"value": locked})
    d.call.side_effect = home_error
    return d


class PreflightTests(unittest.TestCase):
    def test_an_unlocked_phone_goes_home_first(self):
        d = driver()
        prepare_wda_phone(d)
        d.call.assert_called_once_with("POST", "/wda/pressButton", {"name": "home"}, 4)

    def test_a_locked_phone_is_refused_without_any_action(self):
        d = driver(locked=True)
        with self.assertRaisesRegex(PhoneNotReady, "locked"):
            prepare_wda_phone(d)
        d.call.assert_not_called()

    def test_a_wedged_phone_is_named_instead_of_a_transport_error(self):
        d = driver(locked_error=TimeoutError())
        with self.assertRaisesRegex(PhoneNotReady, "isn't responding.*No action"):
            prepare_wda_phone(d)
        d.call.assert_not_called()
        with self.assertRaisesRegex(PhoneNotReady, "Home Screen"):
            prepare_wda_phone(driver(home_error=TimeoutError()))


if __name__ == "__main__":
    unittest.main()
