"""The shared CLI works without app extensions and refuses operations that require them."""

import io
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from mobile_agent import share, transport
from mobile_agent.api_errors import APIError
from mobile_agent.testkit import cli, pr_report
from mobile_agent.workflows.service import Workflows


class StandaloneExtensionsTests(unittest.TestCase):
    def test_service_requests_do_not_import_remote(self):
        connection = mock.Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b'{"ok": true}'
        with mock.patch.dict(sys.modules, {"mobile_agent.remote": None}), \
                mock.patch.object(share, "api_base", return_value="https://example.com"), \
                mock.patch.object(share.pinning, "PinnedHTTPSConnection", return_value=connection):
            self.assertEqual(share.call("POST", "/api/feedback", "test-token", {}), (200, {"ok": True}))
        self.assertEqual(connection.request.call_args.kwargs["headers"]["User-Agent"], transport.user_agent())
        connection.close.assert_called_once()

    def test_missing_teams_keeps_the_local_report_and_explains_sharing(self):
        err = io.StringIO()
        with mock.patch.dict(sys.modules, {"mobile_agent.teams": None}):
            self.assertIsNone(cli._share("local-report", 7, err))
        self.assertIn("needs the Mobster app's team extension", err.getvalue())
        self.assertIn("Your report is in local-report", err.getvalue())

    def test_missing_teams_refuses_hosted_link_rendering(self):
        with mock.patch.dict(sys.modules, {"mobile_agent.teams": None}), \
                self.assertRaisesRegex(ValueError, "team extension"):
            pr_report.hosted_pages("https://console.mobster.dev/r/example#k=example")

    def test_missing_teams_refuses_approval_links_without_changing_local_approval(self):
        service = SimpleNamespace(_links_lock=threading.Lock(), _approval_links=None)
        with mock.patch.dict(sys.modules, {"mobile_agent.teams": None}), \
                self.assertRaises(APIError) as caught:
            Workflows.approval_links.fget(service)
        self.assertEqual(caught.exception.code, "extension_unavailable")
        self.assertIn("Approve this step locally", str(caught.exception))
        self.assertIsNone(service._approval_links)

    def test_a_broken_installed_extension_is_not_reported_as_absent(self):
        import builtins
        original = builtins.__import__

        def broken(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "teams":
                raise ModuleNotFoundError("No module named 'broken_dependency'", name="broken_dependency")
            return original(name, globals, locals, fromlist, level)

        with mock.patch("builtins.__import__", side_effect=broken), self.assertRaises(ModuleNotFoundError) as caught:
            cli._share("local-report", 7, io.StringIO())
        self.assertEqual(caught.exception.name, "broken_dependency")
