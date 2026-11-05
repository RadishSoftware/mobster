import unittest
from unittest.mock import Mock

from mobile_agent.runtime_driver import PhoneNotReady, launch_app, prepare_wda_phone
from mobile_agent.transport import StaleSession, TransportError


class Phone:
    def __init__(self, fail_first, front_after=None, recover=True):
        self.fail_first, self.front_after, self.calls, self.recovered = fail_first, front_after, [], 0
        if not recover:
            self.recover = None

    def call(self, method, path, body=None, timeout=None):
        self.calls.append((method, path))
        if path == "/wda/apps/activate" and self.fail_first is not None:
            error, self.fail_first = self.fail_first, None
            raise error
        if path == "/wda/activeAppInfo":
            return {"bundleId": self.front_after}
        return None

    def recover(self, timeout=None):
        self.recovered += 1


ACTIVATE = ("POST", "/wda/apps/activate")


class LaunchTests(unittest.TestCase):
    def test_a_launch_lost_to_a_restarted_helper_reattaches_and_opens_the_app(self):
        for error in (TransportError("HTTP RemoteDisconnected; request outcome unknown; not retried"),
                      StaleSession("HTTP 404 StaleSession: invalid session id; nothing sent; not retried")):
            with self.subTest(error=str(error)[:30]):
                phone = Phone(error, front_after="com.apple.springboard")
                launch_app(phone, "com.apple.Preferences")
                self.assertEqual(phone.recovered, 1)
                self.assertEqual(phone.calls, [ACTIVATE, ("GET", "/wda/activeAppInfo"), ACTIVATE])

    def test_an_app_that_opened_before_the_answer_was_lost_is_not_opened_again(self):
        phone = Phone(TransportError("HTTP ConnectionResetError; request outcome unknown; not retried"),
                      front_after="com.apple.Preferences")
        launch_app(phone, "com.apple.Preferences")
        self.assertEqual(phone.calls, [ACTIVATE, ("GET", "/wda/activeAppInfo")])

    def test_any_other_failure_is_raised_as_before_with_no_retry(self):
        phone = Phone(TransportError("HTTP 500; request not retried"))
        with self.assertRaises(TransportError):
            launch_app(phone, "com.apple.Preferences")
        self.assertEqual((phone.calls, phone.recovered), ([ACTIVATE], 0))
        bare = Phone(TransportError("HTTP ConnectionResetError; request outcome unknown"), recover=False)
        with self.assertRaises(TransportError):
            launch_app(bare, "com.apple.Preferences")
        self.assertEqual(bare.calls, [ACTIVATE])


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
