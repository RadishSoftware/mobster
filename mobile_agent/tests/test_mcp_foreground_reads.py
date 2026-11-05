from unittest.mock import Mock, patch

from mobile_agent.agent_hooks import GuardVerdict
from mobile_agent.mcp_server.direct import PhoneStop
from mobile_agent.permissions import outside
from mobile_agent.tests.test_mcp_devices import PHONE
from mobile_agent.tests.test_mcp_new_tools import NewToolsHarness, PhoneRun, Screens

BUNDLE = "com.apple.Preferences"


class SettingsRun(PhoneRun):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        settings = Screens("signin")
        settings.data = {"bundle": BUNDLE, "width": 402, "height": 874,
                         "elements": [{"label": "General", "role": "Button", "rect": [.1, .3, .8, .1]}]}
        self.driver.apps = {BUNDLE: settings}
        self.driver.front = self.driver.hint = BUNDLE


class ForegroundReadTests(NewToolsHarness):
    def make(self, **options):
        tools = super().make(device_run=SettingsRun, **options)
        self.permit_app = self.enterContext(patch.object(outside, "check_app"))
        self.enterContext(patch.object(outside, "check_action", return_value=False))
        self.assertFalse(self.call("screen", device=PHONE).is_error)
        self.session = self.tools.device_sessions[PHONE]
        return tools

    def test_warm_screen_and_tap_read_the_foreground_without_the_accessibility_tree(self):
        self.make()
        driver = self.session.run.driver
        active = driver.active_app
        for tool, args in (("screen", {"image": False}), ("screen", {}), ("tap", {"target": {"label": "General"}})):
            reads = {}
            for metadata in (False, True):
                with self.subTest(tool=tool, args=args, metadata=metadata), \
                        patch.object(driver, "active_app", Mock(wraps=active) if metadata else None), \
                        patch.object(driver, "observe", wraps=driver.observe) as observe:
                    self.permit_app.reset_mock()
                    result = self.call(tool, device=PHONE, **args)
                    self.assertFalse(result.is_error, result.text)
                    reads[metadata] = observe.call_count
                    self.assertGreaterEqual(self.permit_app.call_count, 1)
                    if metadata:
                        self.assertGreaterEqual(driver.active_app.call_count, 1)
            self.assertLess(reads[True], reads[False], (tool, reads))

    def test_missing_or_failed_metadata_never_falls_back_to_ax_or_reads_the_screen(self):
        self.make()
        driver = self.session.run.driver
        for value in (None, "", "  ", {}, OSError("metadata failed")):
            with self.subTest(value=value), patch.object(driver, "observe") as observe, \
                    patch.object(self.session.run, "read_tree") as tree, \
                    patch.object(self.tools, "_screenshot") as capture, \
                    patch.object(driver, "active_app", **({"side_effect": value} if isinstance(value, Exception)
                                                         else {"return_value": value})):
                result = self.call("screen", device=PHONE)
                self.assertTrue(result.is_error)
                self.assertIn("couldn't identify the app", result.text)
                observe.assert_not_called()
                tree.assert_not_called()
                capture.assert_not_called()

    def test_fresh_metadata_permission_denial_prevents_tree_capture_and_dispatch(self):
        self.make()
        driver = self.session.run.driver
        with patch.object(driver, "active_app", return_value="com.example.private"), \
                patch.object(driver, "observe") as observe, \
                patch.object(self.session.run, "read_tree") as tree, \
                patch.object(driver, "execute") as dispatch:
            self.permit_app.side_effect = outside.Refused("App access was declined")
            result = self.call("tap", device=PHONE, target={"label": "General"})
        self.assertTrue(result.is_error)
        # Only the check after pressing Home reads the foreground; no tree is read and nothing is dispatched.
        self.assertTrue(all(call.kwargs == {"timeout": 5} for call in observe.call_args_list))
        tree.assert_not_called()
        dispatch.assert_not_called()
        self.assertEqual(self.permit_app.call_args.args[0], "com.example.private")

    def test_phone_lock_still_refuses_before_foreground_metadata(self):
        self.make()
        stop = PhoneStop(GuardVerdict("stop", "phone_locked", "Unlock the phone"))
        self.session.run.check_phone = Mock(side_effect=stop)
        with patch.object(self.session.run.driver, "active_app") as active, \
                patch.object(self.session.run.driver, "execute") as dispatch:
            result = self.call("tap", device=PHONE, target={"label": "General"})
        self.assertTrue(result.is_error)
        self.assertIn("Unlock", result.text)
        active.assert_not_called()
        dispatch.assert_not_called()
