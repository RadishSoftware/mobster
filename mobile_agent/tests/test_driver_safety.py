import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from mobile_agent.drivers import WDA
from mobile_agent.state import Element, Snapshot
from mobile_agent.tests.test_exact_actuation import FIELD
from mobile_agent.transport import TransportError
from mobile_agent.wda import watchdog as wda_watchdog
from mobile_agent.wda.guards import DriverRejection

URL = "http://127.0.0.1:8100"


class FieldHttp:
    def __init__(self, found, error=None):
        self.found, self.error, self.sent = found, error, []

    def request(self, method, path, body=None, timeout=None):
        self.sent.append((method, path))
        if path.endswith("/elements"):
            return {"value": self.found}
        if path.endswith("/value") and self.error:
            raise self.error
        return {"value": None}

    def close(self):
        pass


def search_field():
    element = Element("0", "Search", "TextField", (.05, .2, .9, .3), True, FIELD, "", ("TAP", "TYPE"))
    return element, Snapshot([element], "", 400, 800, "wda", bundle_id="com.example.app", keyboard="parked")


class DriverTests(unittest.TestCase):
    def test_a_lost_active_field_clear_response_never_continues_writing(self):
        from mobile_agent.tests.test_exact_actuation import FakeWDA
        driver = FakeWDA(role="TextField", value="old text")
        self.addCleanup(driver.close)
        recorded = driver.call

        def call(method, path, body=None, timeout=10):
            answer = recorded(method, path, body, timeout)
            if path.endswith("/clear"):
                raise TransportError("request outcome unknown; not retried")
            return answer

        driver.call = call
        with self.assertRaises(TransportError):
            driver.write_text(driver.element(), driver.observe(), "new text", submit=True)
        self.assertEqual(driver.calls[-1], ("POST", "/element/field/clear"))
        self.assertEqual(driver.calls.count(("POST", "/element/field/clear")), 1)
        self.assertEqual(driver.value, "")
        self.assertFalse(driver.submitted)

    def test_point_taps_are_bounded_and_refuse_invalid_or_stale_observations(self):
        driver = WDA(URL, "S3")
        self.addCleanup(driver.close)
        driver.call = mock.Mock()
        snapshot = Snapshot([], "first", 400, 800, "wda")
        driver.tap_point(1, 1, snapshot, purpose="tap_xy")
        move = driver.call.call_args.args[2]["actions"][0]["actions"][0]
        self.assertEqual((move["x"], move["y"]), (399, 799))
        driver.call.reset_mock()
        for x, y in ((True, .5), (float("nan"), .5), (.5, 1.1), (-.1, .5)):
            with self.subTest(x=x, y=y), self.assertRaises(ValueError):
                driver.tap_point(x, y, snapshot)
        with self.assertRaises(ValueError):
            driver.tap_point(.5, .5, SimpleNamespace(width=True, height=800))
        snapshot.captured_at = 0
        driver.observe = mock.Mock(return_value=Snapshot([], "changed", 400, 800, "wda"))
        with self.assertRaises(DriverRejection):
            driver.tap_point(.5, .5, snapshot)
        driver.call.assert_not_called()

    def test_long_press_requires_the_observed_control_and_frame(self):
        driver = WDA(URL, "S4")
        self.addCleanup(driver.close)
        driver.call = mock.Mock()
        target = Element("1", "Options", "Button", (.1, .1, .2, .1), locator="/button[1]")
        snapshot = Snapshot([target], "Options", 400, 800, "wda")
        with self.assertRaises(ValueError):
            driver.long_press(replace(target, rect=(.7, .7, .2, .1)), snapshot)
        driver.call.assert_not_called()
        driver.long_press(target, snapshot)
        driver.call.assert_called_once()

    def test_a_row_swipe_is_a_short_slow_drag_and_other_swipes_are_as_before(self):
        from mobile_agent.wda import gestures as wda_gestures
        row = (0, .2, 1, .08)
        x1, _, x2, _ = wda_gestures._swipe_points("SWIPE_LEFT", 400, 800, row, reveal=True)
        self.assertEqual((round(x1), round(x2)), (300, 160))
        self.assertEqual(tuple(round(v) for v in wda_gestures._swipe_points("SWIPE_LEFT", 400, 800, row)),
                         (300, 192, 100, 192))
        self.assertEqual(wda_gestures._swipe_points("SWIPE_LEFT", 400, 800), (300, 400, 100, 400))

    def test_a_line_break_in_a_chat_composer_is_never_a_return_and_a_note_keeps_it(self):
        from mobile_agent.tests.test_exact_actuation import FakeWDA
        from mobile_agent.wda import text as wda_text

        class Chat(FakeWDA):
            def observe(self, timeout=10):
                return Snapshot([self.element()], self.value, 400, 800, "wda", bundle_id="com.apple.MobileSMS",
                                keyboard="parked")

            def _enter(self, text):
                if "\n" in text:
                    self.value += text.partition("\n")[0]
                    self.submitted = True
                    return
                self.value += text

        with mock.patch.object(wda_text, "WDA_PARKED_FOCUS_SECONDS", 0), mock.patch("time.sleep"), \
                mock.patch.object(wda_text, "WDA_PASTE_MENU_SECONDS", 0):
            chat = Chat(paste_menu=False)
            receipt = chat.write_text(chat.element(), chat.observe(), "line one\nline two")
            self.assertFalse(chat.submitted)
            self.assertEqual((chat.value, receipt["flattened"]), ("line one line two", True))
            note = FakeWDA()
            note.write_text(note.element(), note.observe(), "eggs\nmilk")
            self.assertEqual(note.value, "eggs\nmilk")

    def test_a_type_whose_outcome_is_unknown_is_not_typed_again_by_keystrokes(self):
        isolated = mock.patch.object(wda_watchdog, "tracker", wda_watchdog.ActionTracker())
        isolated.start()
        self.addCleanup(isolated.stop)
        driver = WDA(URL, "S1")
        driver.http = FieldHttp([{"ELEMENT": "E1"}],
                                TransportError("HTTP TimeoutError; request outcome unknown; not retried"))
        driver._check_current = lambda snapshot, deadline: None
        element, snapshot = search_field()
        with self.assertRaises(TransportError):
            driver.execute("TYPE", element, snapshot, "hello")
        paths = [path for _, path in driver.http.sent]
        self.assertNotIn("/wda/keys", [path.rsplit("/session/S1", 1)[-1] for path in paths])
        self.assertEqual([(m, p.rsplit("/session/S1", 1)[-1]) for m, p,
                          _ in wda_watchdog.tracker.unknown_outcomes(URL)],
                         [("POST", "/element/E1/value")])

    def test_a_field_with_no_unique_match_is_tapped_and_typed_once(self):
        driver = WDA(URL, "S2")
        driver.http = FieldHttp([])
        driver._check_current = lambda snapshot, deadline: None
        driver._await_keyboard = lambda deadline: True
        element, snapshot = search_field()
        driver.execute("TYPE", element, snapshot, "hello")
        paths = [path.rsplit("/session/S2", 1)[-1] for _, path in driver.http.sent]
        self.assertEqual((paths.count("/actions"), paths.count("/wda/keys")), (1, 1))
        self.assertNotIn("/element/E1/value", paths)


if __name__ == "__main__":
    unittest.main()
