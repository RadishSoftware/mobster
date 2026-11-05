import unittest
from unittest.mock import patch

from mobile_agent.mcp_server.direct import DeviceRun
from mobile_agent.mcp_server.protocol import Call
from mobile_agent.mcp_server.tools import ToolSet
from mobile_agent.permissions import outside
from mobile_agent.tests.test_mcp_devices import PHONE, FakeDeviceRun, records
from mobile_agent.tests.test_mcp_tools import Node, ToolsHarness, Tree, button


class ActionApprovalTests(ToolsHarness):
    def ready(self):
        self.make(devices=records, device_run=FakeDeviceRun)
        self.call("screen", device=PHONE)
        run = self.tools.device_sessions[PHONE].run
        run.tree = Tree([button(1, "Send")], bundle_id="com.apple.MobileSMS")
        return run

    def test_unchanged_approved_action_dispatches_once(self):
        with patch.object(outside, "check_app"), patch.object(outside, "check_action", return_value=True), \
                patch.object(ToolSet, "_rebind", lambda self, session, element, snapshot, call: (element, snapshot)):
            run = self.ready()
            result = self.call("tap", device=PHONE, target={"label": "Send"})
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(sum(isinstance(c, tuple) and c[0] == "execute" for c in run.driver.calls), 1)

    def test_cancelled_or_changed_approval_never_dispatches(self):
        for change in ("cancel", "screen", "revision", "permission"):
            with self.subTest(change=change), patch.object(outside, "check_app") as apps:
                run, call = self.ready(), Call(1)

                def approve(*args, **kwargs):
                    if change == "cancel":
                        call.cancel()
                    elif change == "screen":
                        run.tree = Tree([button(1, "Send"), button(2, "Another recipient")],
                                        bundle_id="com.apple.MobileSMS")
                    elif change == "revision":
                        fresh = run.driver.observe()
                        fresh.fingerprint += "-new-routing-revision"
                        run.driver.observe = lambda **kwargs: fresh
                    else:
                        apps.side_effect = outside.Refused("The app was turned off. Nothing was done.")
                    return True

                with patch.object(outside, "check_action", side_effect=approve):
                    result = self.tools.call("tap", {"device": PHONE, "target": {"label": "Send"}}, call)
                self.assertTrue(result.is_error)
                self.assertFalse(any(isinstance(c, tuple) and c[0] == "execute" for c in run.driver.calls))

    def test_a_call_cancelled_after_focusing_a_secure_field_never_types(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled), patch.object(outside, "check_app"), \
                    patch.object(outside, "check_action", return_value=False):
                run, call = self.ready(), Call(1)
                run.tree = Tree([Node("/app/pw", "SecureTextField", label="Password", rect=(10, 10, 100, 40))],
                                bundle_id="com.apple.MobileSMS")
                with patch.object(ToolSet, "_await_focus",
                                  lambda driver, seconds=2.0: call.cancel() if cancelled else None):
                    result = self.tools.call("type_text", {"device": PHONE, "target": {"label": "Password"},
                                                           "text": "hunter2"}, call)
                typed = [c for c in run.driver.calls if isinstance(c, tuple) and c[1:2] == ("/wda/keys",)]
                self.assertEqual(result.is_error, cancelled, result.text)
                self.assertEqual(len(typed), 0 if cancelled else 1)

    def test_alert_approval_only_dispatches_the_unchanged_alert(self):
        for change in (None, "cancel", "alert", "permission"):
            with self.subTest(change=change), patch.object(outside, "check_app") as apps:
                run, call = self.ready(), Call(1)
                run.driver.alert = ("Delete note?", ["Delete", "Cancel"])

                def approve(*args, **kwargs):
                    if change == "cancel":
                        call.cancel()
                    elif change == "alert":
                        run.driver.alert = ("Delete another note?", ["Delete", "Cancel"])
                    elif change == "permission":
                        apps.side_effect = outside.Refused("The app was turned off. Nothing was done.")
                    return True

                with patch.object(outside, "check_action", side_effect=approve):
                    result = self.tools.call("alert", {"device": PHONE, "action": "accept", "button": "Delete"}, call)
                sent = [c for c in run.driver.calls if isinstance(c, tuple) and c[:2] == ("POST", "/alert/accept")]
                self.assertEqual(result.is_error, change is not None, result.text)
                self.assertEqual(len(sent), 0 if change else 1)

    def test_an_approved_row_swipe_goes_only_on_the_unchanged_screen(self):
        for change in (None, "screen", "cancel"):
            with self.subTest(change=change), patch.object(outside, "check_app"):
                run, call = self.ready(), Call(1)

                def approve(*args, **kwargs):
                    if change == "screen":
                        run.tree = Tree([button(1, "Send"), button(2, "Another row")], bundle_id="com.apple.MobileSMS")
                    elif change == "cancel":
                        call.cancel()
                    return True

                with patch.object(outside, "check_action", side_effect=approve):
                    result = self.tools.call("swipe", {"device": PHONE, "direction": "left",
                                                       "target": {"label": "Send"}}, call)
                swiped = [c for c in run.driver.calls if isinstance(c, tuple) and c[1:2] == ("/actions",)]
                self.assertEqual(result.is_error, change is not None, result.text)
                self.assertEqual(len(swiped), 0 if change else 1)


class UrlPermissionTests(ToolsHarness):
    def make(self, *, refused=None, redirect=None):
        self.events = []
        events = self.events

        class LinkedRun(FakeDeviceRun):
            open_url = DeviceRun.open_url

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                call = self.driver.call

                def dispatch(method, path, body=None, timeout=10):
                    if path == "/url":
                        events.append(("open", body))
                        self.tree.bundle_id = redirect or body.get("bundleId") or "com.apple.mobilesafari"
                    return call(method, path, body, timeout)

                self.driver.call = dispatch

        def check_app(bundle, **kwargs):
            events.append(("permit", bundle))
            if bundle == refused:
                raise outside.Refused("The user declined access to this app. Nothing was done.")

        for patcher in (patch.object(outside, "check_app", side_effect=check_app),
                        patch("mobile_agent.deeplinks.time.sleep"),
                        patch("mobile_agent.wda.system_dialogs.read", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return super().make(devices=records, device_run=LinkedRun)

    def test_web_links_check_safari_before_dispatch_and_bind_the_url_to_it(self):
        self.make()
        result = self.call("open_url", device=PHONE, url="https://example.com/check")
        self.assertFalse(result.is_error, result.text)
        sent = ("open", {"url": "https://example.com/check", "bundleId": "com.apple.mobilesafari"})
        self.assertIn(sent, self.events)
        self.assertLess(self.events.index(("permit", "com.apple.mobilesafari")), self.events.index(sent))

    def test_refused_browser_never_opens_the_url_or_captures_an_after_frame(self):
        self.make(refused="com.apple.mobilesafari")
        result = self.call("open_url", device=PHONE, url="http://example.com/check")
        self.assertTrue(result.is_error)
        self.assertIn("Nothing was opened.", result.text)
        self.assertFalse(any(event[0] == "open" for event in self.events))
        run = next(iter(self.tools.device_sessions.values())).run
        self.assertEqual(run.frames, [])

    def test_protocol_cancellation_while_link_approval_waits_never_opens_it(self):
        self.make()
        call = Call(1)
        with patch.object(outside, "check_link", side_effect=lambda *a, **kw: call.cancel()):
            result = self.tools.call("open_url", {"device": PHONE, "url": "unknownapp://do-something"}, call)
        self.assertTrue(result.is_error)
        self.assertIn("cancelled before dispatch", result.text)
        self.assertFalse(any(event[0] == "open" for event in self.events))

    def test_app_permission_revoked_during_link_approval_never_opens_it(self):
        self.make()
        def revoke(*args, **kwargs):
            def blocked(bundle, **kwargs):
                if bundle == "com.apple.MobileSMS":
                    raise outside.Refused("The app was turned off. Nothing was done.")
            outside.check_app.side_effect = blocked
        with patch.object(outside, "check_link", side_effect=revoke):
            result = self.call("open_url", device=PHONE, url="sms:+15555550100&body=hello")
        self.assertTrue(result.is_error)
        self.assertIn("Nothing was opened.", result.text)
        self.assertFalse(any(event[0] == "open" for event in self.events))


if __name__ == "__main__":
    unittest.main()
