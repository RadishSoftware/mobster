import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent.api_errors import APIError
from mobile_agent.journal import JournalError, Lease
from mobile_agent.manual_control import ManualControl, parse_command
from mobile_agent.server import Runtime


class ParseTests(unittest.TestCase):
    def test_valid_gestures(self):
        self.assertEqual(parse_command({"action": "tap", "x": .5, "y": 1})["holdMs"], 40)
        self.assertEqual(parse_command({"action": "swipe", "x1": 0, "y1": .9, "x2": 0, "y2": .1,
                                        "durationMs": 200})["durationMs"], 200)
        self.assertTrue(parse_command({"action": "type", "text": "hi", "submit": True})["submit"])
        self.assertEqual(parse_command({"action": "button", "name": "home"})["name"], "home")

    def test_everything_else_is_refused(self):
        for body in ({"action": "tap", "x": 1.5, "y": 0}, {"action": "tap", "x": float("nan"), "y": 0},
                     {"action": "tap", "x": 10 ** 400, "y": 0},
                     {"action": "tap", "x": True, "y": 0}, {"action": "tap", "x": .1, "y": .1, "holdMs": 9000},
                     {"action": "swipe", "x1": 0, "y1": 0, "x2": 1, "y2": 1, "durationMs": 5},
                     {"action": "type", "text": "hi\nthere"}, {"action": "type", "text": " "},
                     {"action": "button", "name": "power"}, {"action": "tap", "x": 0, "y": 0, "extra": 1},
                     {"action": "launch"}, [], None):
            with self.assertRaises(ValueError, msg=body):
                parse_command(body)


class DispatchTests(unittest.TestCase):
    def control(self, width=1, height=1, springboard=None):
        control = ManualControl("http://127.0.0.1:8100", lambda: "S1")
        driver = Mock()
        driver.http.request.side_effect = springboard
        driver.http.request.return_value = {"value": {"screenSize": {"width": 393, "height": 852}, "scale": 3}}
        size = {"width": width, "height": height}
        driver.call.side_effect = lambda method, path, body=None, timeout=10: size if path == "/window/size" else None
        control._driver = lambda: driver
        return control, driver

    def test_a_tap_lands_on_the_matching_screen_point(self):
        control, driver = self.control()
        control.perform({"action": "tap", "x": .5, "y": .25})
        move = driver.call.call_args_list[-1].args[2]["actions"][0]["actions"][0]
        self.assertEqual((move["x"], move["y"]), (196, 213))

    def test_a_swipe_follows_the_users_speed(self):
        control, driver = self.control()
        control.perform({"action": "swipe", "x1": .5, "y1": .8, "x2": .5, "y2": .2, "durationMs": 180})
        steps = driver.call.call_args_list[-1].args[2]["actions"][0]["actions"]
        self.assertEqual(steps[2], {"type": "pointerMove", "duration": 180, "x": 196, "y": 170})

    def test_typing_and_buttons(self):
        control, driver = self.control()
        control.perform({"action": "type", "text": "coffee", "submit": True})
        self.assertEqual(driver.call.call_args.args[1:3], ("/wda/keys", {"value": ["coffee", "\n"]}))
        control.perform({"action": "button", "name": "volume_up"})
        self.assertEqual(driver.call.call_args.args[1:3], ("/wda/pressButton", {"name": "volumeUp"}))

    def test_falls_back_to_the_window_size_when_springboard_does_not_answer(self):
        control, driver = self.control(414, 896, springboard=TimeoutError())
        control.perform({"action": "tap", "x": 1, "y": 1})
        move = driver.call.call_args_list[-1].args[2]["actions"][0]["actions"][0]
        self.assertEqual((move["x"], move["y"]), (414, 896))


class RouteGateTests(unittest.TestCase):
    def runtime(self, live=True, busy=False):
        runtime = object.__new__(Runtime)
        runtime.config = SimpleNamespace(enable_live=live, wda_url="http://127.0.0.1:8100")
        runtime.lock = threading.Lock()
        runtime.active_runs = {"r": object()} if busy else {}
        runtime.control = Mock()
        return runtime

    def test_control_is_refused_while_a_task_runs_or_actions_are_off(self):
        with self.assertRaises(APIError) as busy:
            self.runtime(busy=True).control_device({"action": "button", "name": "home"})
        self.assertEqual(busy.exception.code, "device_busy")
        with self.assertRaises(APIError) as off:
            self.runtime(live=False).control_device({"action": "button", "name": "home"})
        self.assertEqual(off.exception.code, "live_disabled")

    def test_a_phone_error_is_reported_plainly(self):
        runtime = self.runtime()
        runtime.control.perform.side_effect = ConnectionError("gone")
        with self.assertRaises(APIError) as failed:
            runtime.control_device({"action": "button", "name": "home"})
        self.assertEqual(failed.exception.status, 503)
        runtime.control.perform.side_effect = ValueError("x must be a number from 0 to 1")
        with self.assertRaises(ValueError):
            runtime.control_device({"action": "tap"})

    def test_control_holds_the_device_lease_until_dispatch_finishes(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            def reserve(_identity):
                return Lease(f"{folder}/phone.lock")

            def dispatch(_body):
                self.assertTrue(runtime.lock.acquire(blocking=False))
                runtime.lock.release()
                with self.assertRaises(JournalError):
                    reserve(runtime.config.wda_url)
                return "button"

            runtime.control.perform.side_effect = dispatch
            with patch("mobile_agent.journal.Lease.device", side_effect=reserve):
                self.assertEqual(runtime.control_device({"action": "button", "name": "home"}), "button")
                reserve(runtime.config.wda_url).close()
                runtime.control.perform.side_effect = ConnectionError("gone")
                with self.assertRaises(APIError):
                    runtime.control_device({"action": "button", "name": "home"})
                reserve(runtime.config.wda_url).close()

    def test_control_refuses_a_device_held_by_another_process(self):
        runtime = self.runtime()
        with patch("mobile_agent.journal.Lease.device", side_effect=JournalError("held")):
            with self.assertRaises(APIError) as failed:
                runtime.control_device({"action": "button", "name": "home"})
        self.assertEqual(failed.exception.code, "device_busy")
        runtime.control.perform.assert_not_called()

    def test_shutdown_refuses_control_before_reserving_or_touching_the_phone(self):
        runtime = self.runtime()
        runtime.closing = True
        with patch("mobile_agent.journal.Lease.device") as lease:
            with self.assertRaises(APIError) as failed:
                runtime.control_device({"action": "button", "name": "home"})
        self.assertEqual(failed.exception.code, "shutting_down")
        lease.assert_not_called()
        runtime.control.perform.assert_not_called()


if __name__ == "__main__":
    unittest.main()
