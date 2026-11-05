import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent.frontier.screen import screen_signature
from mobile_agent.model_clients import OpenAIChat
from mobile_agent.replay_core.path import signature_hash
from mobile_agent.state import Element, signature


class ConnectionOwnershipTests(unittest.TestCase):
    def test_busy_primary_uses_and_closes_a_separate_connection(self):
        client = OpenAIChat("gpt-5.6-sol", key="test")
        primary = client.connection = Mock(sock=None)
        secondary = Mock(sock=None)
        secondary.getresponse.return_value = SimpleNamespace(status=200, read=lambda: b'{}')
        client._claim.acquire()
        try:
            with patch("mobile_agent.model_clients.http.client.HTTPSConnection", return_value=secondary):
                self.assertEqual(client._post("/test", b'{}', dict, 5, label="test"), {})
            primary.request.assert_not_called()
            primary.close.assert_not_called()
            secondary.request.assert_called_once()
            secondary.close.assert_called_once()
            self.assertIs(client.connection, primary)
            self.assertTrue(client._claim.locked())
        finally:
            client._claim.release()

    def test_primary_claim_is_released_when_response_is_invalid(self):
        client = OpenAIChat("gpt-5.6-sol", key="test")
        primary = client.connection = Mock(sock=None)
        primary.getresponse.return_value = SimpleNamespace(status=200, read=lambda: b'not JSON')
        with self.assertRaises(ValueError):
            client._post("/test", b'{}', dict, 5, label="test")
        self.assertFalse(client._claim.locked())


class ScreenSignatureTests(unittest.TestCase):
    def test_live_and_recorded_signatures_keep_the_same_digest(self):
        pairs = (("Button", "Café ☕"), ("StaticText", "你好"))
        for labels, expected in (((), "97d170e1550eee4a"), (pairs, "aad944f405d7f867")):
            snapshot = SimpleNamespace(elements=[
                Element(str(index), label, role, (0, 0, .1, .1))
                for index, (role, label) in enumerate(labels)
            ])
            with self.subTest(labels=labels):
                self.assertEqual(screen_signature(snapshot), expected)
                self.assertEqual(signature_hash(signature(snapshot)), expected)

    def test_unreadable_screen_has_no_signature(self):
        self.assertEqual(screen_signature(None), "")


if __name__ == "__main__":
    unittest.main()
