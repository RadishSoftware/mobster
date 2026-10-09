"""Offline Google adapter/evaluator checks. All auth and remote requests are mocked."""

import json
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mobile_agent.agent import Agent
from mobile_agent.demo import DemoDriver, DemoHelper, DemoModel
from mobile_agent.drivers import DriverRejection
from mobile_agent.eval_gemini import MODELS, explicit_abstention, summarize
from mobile_agent.extraction import InsufficientEvidence
from mobile_agent.gemini import GCloudToken, VertexHTTP
from mobile_agent.models import Helper
from mobile_agent.transport import TransportError
from mobile_agent.inference import request_inference
from mobile_agent.tests.timing import bound


def body(model='gemini-3.5-flash-lite'):
    return {'model': model, 'messages': [{'role': 'system', 'content': 'Return JSON.'},
            {'role': 'user', 'content': 'Hello'}], 'max_tokens': 300,
            'response_format': {'type': 'json_object'}}


def response(**changes):
    return {'candidates': [{'finishReason': 'STOP', 'content': {'role': 'model',
        'parts': [{'text': '{"text":"Hello"}'}]}}],
        'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 7, 'totalTokenCount': 19},
        'modelVersion': 'gemini-3.5-flash-lite', **changes}


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.old_token, self.old_until = GCloudToken._token, GCloudToken._until
        GCloudToken._token, GCloudToken._until = '', 0

    def tearDown(self):
        GCloudToken._token, GCloudToken._until = self.old_token, self.old_until

    def test_token_is_cached_only_briefly_without_external_writes(self):
        with patch('mobile_agent.gemini.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='test-token\n')) as run:
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
            with patch('mobile_agent.gemini.subprocess.run', return_value=result), self.assertRaises(TransportError) as caught:
                GCloudToken.get()
            self.assertNotIn('private', str(caught.exception))
            self.assertFalse(GCloudToken._token)

    def test_cli_timeout_is_bounded_and_redacted(self):
        with patch('mobile_agent.gemini.subprocess.run', side_effect=subprocess.TimeoutExpired('private-marker', .1)) as run:
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


class VertexTests(unittest.TestCase):
    def adapter(self):
        adapter = VertexHTTP('project-test', 'global')
        adapter.http = Mock(key='')
        adapter.http.request.return_value = response()
        return adapter

    def test_fixed_google_hosts_and_no_path_injection(self):
        adapter = VertexHTTP('project-test', 'us-central1')
        self.assertEqual(adapter.http.connection.host, 'us-central1-aiplatform.googleapis.com')
        adapter.close()
        for project, location in [('project/../../evil', 'global'), ('project-test', 'global.attacker.test')]:
            with self.assertRaises(ValueError):
                VertexHTTP(project, location)

    def test_translates_json_mode_and_strips_private_thoughts(self):
        adapter = self.adapter()
        data = response()
        data['candidates'][0]['content']['parts'].insert(0, {'thought': True, 'text': 'private-reasoning'})
        adapter.http.request.return_value = data
        with patch.object(GCloudToken, 'get', return_value='test-token'):
            result = adapter.request('POST', '/chat/completions', body())
        self.assertNotIn('private-reasoning', json.dumps(result))
        self.assertEqual(result['choices'][0]['finish_reason'], 'stop')
        route, payload = adapter.http.request.call_args.args[1:3]
        self.assertIn('/projects/project-test/locations/global/publishers/google/models/gemini-3.5-flash-lite:generateContent', route)
        self.assertEqual(payload['generationConfig']['responseMimeType'], 'application/json')
        self.assertEqual(payload['generationConfig']['thinkingConfig'], {'thinkingLevel': 'MINIMAL'})
        self.assertEqual(adapter.http.key, '')

    def test_25_flash_lite_disables_thinking(self):
        adapter = self.adapter()
        with patch.object(GCloudToken, 'get', return_value='test-token'):
            adapter.request('POST', '/chat/completions', body('gemini-2.5-flash-lite'))
        payload = adapter.http.request.call_args.args[2]
        self.assertEqual(payload['generationConfig']['thinkingConfig'], {'thinkingBudget': 0})

    def test_token_cleared_and_not_retried_on_unauthorized(self):
        adapter = self.adapter()
        adapter.http.request.side_effect = TransportError('HTTP 401; request not retried')
        with patch.object(GCloudToken, 'get', return_value='test-token'), patch.object(GCloudToken, 'invalidate') as invalidate:
            with self.assertRaises(TransportError):
                adapter.request('POST', '/chat/completions', body())
        invalidate.assert_called_once_with('test-token')
        self.assertEqual(adapter.http.request.call_count, 1)
        self.assertEqual(adapter.http.key, '')

    def test_other_provider_errors_are_not_retried(self):
        adapter = self.adapter()
        adapter.http.request.side_effect = TransportError('HTTP 429; request not retried')
        with patch.object(GCloudToken, 'get', return_value='test-token'), patch.object(GCloudToken, 'invalidate') as invalidate:
            with self.assertRaises(TransportError):
                adapter.request('POST', '/chat/completions', body())
        invalidate.assert_not_called()
        self.assertEqual(adapter.http.request.call_count, 1)

    def test_malformed_stopped_or_blocked_response_cannot_authorize(self):
        bad = [None, response(candidates=[]), response(candidates=[None]),
               response(promptFeedback={'blockReason': 'SAFETY'}), response(usageMetadata={'totalTokenCount': True})]
        for reason in ['MAX_TOKENS', 'SAFETY', '', None]:
            data = response()
            data['candidates'][0]['finishReason'] = reason
            bad.append(data)
        for parts in [[None], [{'functionCall': {}}], [{'text': 'x', 'thought': 'false'}], [{'text': '', 'thought': True}]]:
            data = response()
            data['candidates'][0]['content']['parts'] = parts
            bad.append(data)
        for item in bad:
            adapter = self.adapter()
            adapter.http.request.return_value = item
            with self.subTest(item=item), patch.object(GCloudToken, 'get', return_value='test-token'), self.assertRaises(TransportError):
                adapter.request('POST', '/chat/completions', body())

    def test_invalid_request_never_acquires_credentials(self):
        bad = [body(), body(), body(), body(), body()]
        bad[0]['model'] = 'gemini-3.5/../../evil'
        bad[1]['messages'] = [{'role': 'system', 'content': 'Only system'}]
        bad[2]['max_tokens'] = True
        bad[3]['messages'][1]['content'] = 'x' * 128001
        bad[4]['response_format'] = {'type': 'json_schema'}
        for request in bad:
            adapter = self.adapter()
            with patch.object(GCloudToken, 'get') as get, self.assertRaises(ValueError):
                adapter.request('POST', '/chat/completions', request)
            get.assert_not_called()

    def test_rejected_answer_preserves_usage_and_price_without_content(self):
        for reason in ("MAX_TOKENS", "SAFETY"):
            adapter = self.adapter()
            data = response()
            data["candidates"][0]["finishReason"] = reason
            data["candidates"][0]["content"]["parts"][0]["text"] = "private answer"
            adapter.http.request.return_value = data
            events = []
            with patch.object(GCloudToken, "get", return_value="test-token"), self.assertRaises(TransportError):
                request_inference(adapter, "/chat/completions", body(), 2, emit=events.append,
                    provider="google", call_id="helper:1", model="gemini-3.5-flash-lite", purpose="planning")
            finished = events[-1]
            self.assertFalse(finished["success"])
            self.assertEqual(finished["usage"]["total_tokens"], 19)
            self.assertEqual(finished["cost_nanodollars"], 21100)
            self.assertNotIn("private answer", str(events))
            self.assertEqual(adapter.http.request.call_count, 1)

    def test_unknown_usage_metadata_is_not_exposed(self):
        adapter = self.adapter()
        adapter.http.request.return_value = response(usageMetadata={'promptTokenCount': 1, 'untrusted': 'private-marker'})
        with patch.object(GCloudToken, 'get', return_value='test-token'):
            result = adapter.request('POST', '/chat/completions', body())
        self.assertEqual(result['usage'], {'promptTokenCount': 1})

    def test_concurrent_requests_do_not_mix_credentials(self):
        adapter = self.adapter()
        adapter._inflight.acquire()
        try:
            # It waits for the request in flight until its own deadline: a short one, not the default 20 s.
            with patch.object(GCloudToken, 'get') as get, self.assertRaises(TransportError):
                adapter.request('POST', '/chat/completions', body(), timeout=.1)
            get.assert_not_called()
        finally:
            adapter._inflight.release()


class ContractTests(unittest.TestCase):
    def helper(self, content):
        with patch.dict('os.environ', {'TEXT_MODEL_PROVIDER': 'openrouter', 'TEXT_MODEL_API_KEY': 'test', 'TEXT_MODEL': 'test'}):
            helper = Helper()
        helper.http.request = Mock(return_value={'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}], 'usage': {}})
        return helper

    def test_only_exact_explicit_abstention_has_distinct_status(self):
        helper = self.helper('{"data":null,"citations":[]}')
        with self.assertRaises(InsufficientEvidence):
            helper.extract({'entries': []}, 'Read', {'type': 'string'})
        helper = self.helper('{"data":{"title":"invented"},"citations":[]}')
        with self.assertRaises(ValueError) as caught:
            helper.extract({'entries': []}, 'Read', {'type': 'string'})
        self.assertNotIsInstance(caught.exception, InsufficientEvidence)

    def test_eval_abstention_scoring_does_not_convert_generic_errors_to_pass(self):
        helper = Mock()
        helper.extract.side_effect = ValueError('invalid extracted fact')
        with self.assertRaises(ValueError):
            explicit_abstention(helper, {}, {})
        helper.extract.side_effect = InsufficientEvidence('not observed')
        self.assertTrue(explicit_abstention(helper, {}, {}))

    def test_eval_speed_selection_excludes_failed_models_and_fast_failures(self):
        helpers = {model: SimpleNamespace(usage=[]) for model in MODELS}
        samples = [
            {'model': MODELS[0], 'ms': 1, 'passed': False, 'error': 'TransportError'},
            {'model': MODELS[0], 'ms': 50, 'passed': True, 'error': None},
            {'model': MODELS[1], 'ms': 1000, 'passed': True, 'error': None},
            {'model': MODELS[2], 'ms': 100, 'passed': True, 'error': None},
            {'model': MODELS[2], 'ms': 200, 'passed': True, 'error': None},
            {'model': MODELS[3], 'ms': 1, 'passed': False, 'error': 'ValueError'}]
        summary, winner = summarize(samples, helpers)
        self.assertEqual(winner, MODELS[2])
        self.assertEqual(summary[0]['all_median_ms'], 25.5)
        self.assertEqual(summary[0]['success_median_ms'], 50)
        self.assertIsNone(summary[3]['success_median_ms'])

    def test_prompt_defines_citation_path_relative_to_data(self):
        helper = self.helper('{"data":null,"citations":[]}')
        with self.assertRaises(InsufficientEvidence):
            helper.extract({'entries': []}, 'Read', {'type': 'string'})
        prompt = helper.http.request.call_args.args[2]['messages'][0]['content']
        self.assertIn('RELATIVE TO THE DATA VALUE', prompt)
        self.assertIn('NEVER "/data/title"', prompt)

    def test_agent_abstention_is_not_schema_success(self):
        driver = DemoDriver()
        driver.stage = 'results'
        helper = DemoHelper()
        helper.extract = Mock(side_effect=InsufficientEvidence('not observed'))
        result = Agent(driver, DemoModel(), helper).run('Read title', execute=True, output_schema={'type': 'string'})
        self.assertEqual(result['data_status'], 'insufficient_evidence')
        self.assertFalse(result['schema_validated'])
        self.assertIsNone(result['data'])

    def test_native_error_exposes_only_allowlisted_code_and_keeps_ambiguity(self):
        driver, events = DemoDriver(), []
        driver.execute = Mock(side_effect=DriverRejection('activation_declined'))
        result = Agent(driver, DemoModel(), emit=events.append).run('Search', execute=True)
        self.assertIn('activation_declined', result['reason'])
        self.assertEqual(result['last_action_outcome'], 'unknown')
        self.assertEqual(next(e for e in events if e['event'] == 'error')['native_code'], 'activation_declined')


if __name__ == '__main__':
    unittest.main()
