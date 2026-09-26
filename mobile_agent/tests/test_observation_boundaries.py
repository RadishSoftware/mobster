"""Occlusion, discovery fairness and independent previews; fully offline."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from ..drivers import DriverRejection, WDA
from ..server import Run, Runtime
from ..transport import TransportError


XML = ('<XCUIElementTypeApplication width="400" height="800">'
       '<XCUIElementTypeStaticText label="Current page" x="0" y="0" width="100" height="40"/>'
       '</XCUIElementTypeApplication>')
APP = {"id": "settings", "name": "Settings", "bundleId": "test.app"}


def node(identifier, label, blocked=None):
    return {"id": identifier, "label": label, "value": "", "role": "UILabel",
            "rect": [0, 0, .5, .1], "actions": [], "editable": False,
            "action_blocked_reason": blocked}


class PreviewBoundaryTests(unittest.TestCase):
    def driver(self):
        driver = WDA("http://localhost:8100", "test-session")
        self.addCleanup(driver.close)
        driver.http.request = Mock(return_value={"value": XML})
        return driver

    def test_wda_observation_never_requests_a_screenshot(self):
        driver = self.driver()
        driver.preview_http.request = Mock(side_effect=TransportError("Screenshot unavailable"))
        self.assertEqual(driver.observe().text, "Current page")
        driver.preview_http.request.assert_not_called()
        self.assertEqual(driver.http.request.call_args.args[:2],
                         ("GET", "/session/test-session/source?format=xml&excluded_attributes=visible,accessible,index,traits"))

    def test_preview_and_observation_do_not_share_an_inflight_lock(self):
        driver = self.driver()
        self.assertIsNot(driver.http, driver.preview_http)
        driver.preview_http._inflight.acquire()
        try:
            acquired = driver.http._inflight.acquire(blocking=False)
            self.assertTrue(acquired)
            if acquired:
                driver.http._inflight.release()
            self.assertEqual(driver.observe().text, "Current page")
        finally:
            driver.preview_http._inflight.release()


    def test_preview_failure_does_not_fail_subsequent_observation(self):
        driver = self.driver()
        driver.preview_http.request = Mock(side_effect=TransportError("Screenshot unavailable"))
        runtime = object.__new__(Runtime)
        runtime.config = SimpleNamespace(wda_url=None)
        run = Run(dict(APP), "Read current page", "live", status="running", capture=driver.capture_preview)
        self.assertIsNone(runtime.preview(run)["image"])
        self.assertEqual(driver.observe().text, "Current page")

    def test_dashboard_can_capture_a_wda_preview(self):
        driver = self.driver()
        driver.preview_http.request = Mock(return_value={"value": "dGVzdA=="})
        runtime = object.__new__(Runtime)
        runtime.config = SimpleNamespace(wda_url="http://localhost:8100")
        run = Run(dict(APP), "Read current page", "live", status="running", capture=driver.capture_preview)
        frame = runtime.preview(run)
        self.assertEqual(frame["image"], "data:image/png;base64,dGVzdA==")
        self.assertIsNotNone(frame["capturedAt"])
        self.assertEqual(driver.preview_http.request.call_args.args[:2],
                         ("GET", "/session/test-session/screenshot"))
        driver.http.request.assert_not_called()

    def test_invalid_preview_clears_previous_frame_and_capture_time(self):
        runtime = object.__new__(Runtime)
        runtime.config = SimpleNamespace(wda_url="http://localhost:8100")
        for code in ("app_preview_uniform", "app_preview_unavailable", "app_preview_oversized"):
            with self.subTest(code=code):
                run = Run({"id": "tiktok", "name": "TikTok"}, "Read the screen", "live", status="running")
                run.image, run.image_captured_at = "old frame", 123
                run.capture = Mock(side_effect=DriverRejection(code))
                self.assertEqual(runtime.preview(run), {
                    "image": None, "source": "live", "capturedAt": None})


if __name__ == "__main__":
    unittest.main()
