import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent.gemini import GCloudToken
from mobile_agent.tests.timing import bound
from mobile_agent.transport import TransportError


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.old_token, self.old_until = GCloudToken._token, GCloudToken._until
        GCloudToken._token, GCloudToken._until = '', 0

    def tearDown(self):
        GCloudToken._token, GCloudToken._until = self.old_token, self.old_until

    def test_token_is_cached_only_briefly_without_external_writes(self):
        with patch('mobile_agent.gemini.subprocess.run',
                   return_value=SimpleNamespace(returncode=0, stdout='test-token\n')) as run:
            self.assertEqual(GCloudToken.get(), 'test-token')
            self.assertEqual(GCloudToken.get(), 'test-token')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:], ['auth', 'print-access-token', '--quiet'])
        self.assertTrue(run.call_args.args[0][0].endswith('gcloud'))
        self.assertLessEqual(GCloudToken._until - time.monotonic(), 300)
        self.assertTrue(run.call_args.kwargs['capture_output'])

    def test_invalid_token_or_cli_error_is_redacted(self):
        for result in [SimpleNamespace(returncode=1, stdout='private-marker'),
                       SimpleNamespace(returncode=0, stdout='private-marker\x00'),
                       SimpleNamespace(returncode=0, stdout='private marker')]:
            with patch('mobile_agent.gemini.subprocess.run',
                       return_value=result), self.assertRaises(TransportError) as caught:
                GCloudToken.get()
            self.assertNotIn('private', str(caught.exception))
            self.assertFalse(GCloudToken._token)

    def test_cli_timeout_is_bounded_and_redacted(self):
        with patch('mobile_agent.gemini.subprocess.run',
                   side_effect=subprocess.TimeoutExpired('private-marker', .1)) as run:
            with self.assertRaises(TransportError) as caught:
                GCloudToken.get(timeout=.1)
        self.assertLessEqual(run.call_args.kwargs['timeout'], .1)
        self.assertNotIn('private-marker', str(caught.exception))

    def test_token_lock_wait_honors_deadline(self):
        GCloudToken._lock.acquire()
        try:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                GCloudToken.get(timeout=.01)
            self.assertLess(time.monotonic() - started, bound(.3))
        finally:
            GCloudToken._lock.release()

    def test_invalidation_does_not_delete_newer_token(self):
        GCloudToken._token, GCloudToken._until = 'newer', 10
        GCloudToken.invalidate('older')
        self.assertEqual(GCloudToken._token, 'newer')
        GCloudToken.invalidate('newer')
        self.assertFalse(GCloudToken._token)


if __name__ == '__main__':
    unittest.main()
