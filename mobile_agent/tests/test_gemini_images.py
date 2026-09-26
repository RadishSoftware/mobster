"""Image input, media resolution, strict schemas and logprobs through the Vertex adapter and Helper."""

import base64
import json
import os
import unittest
from unittest.mock import Mock, patch

from mobile_agent.gemini import GCloudToken, VertexHTTP
from mobile_agent.models import Helper, image_part


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
DATA_URL = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()


def response(text='{"results":[]}', **changes):
    return {'candidates': [{'finishReason': 'STOP', 'content': {'role': 'model', 'parts': [{'text': text}]},
                            **changes}],
            'usageMetadata': {'promptTokenCount': 300, 'candidatesTokenCount': 7, 'totalTokenCount': 307},
            'modelVersion': 'gemini-3.5-flash-lite'}


def adapter(result=None):
    vertex = VertexHTTP('project-test', 'global')
    vertex.http = Mock(key='')
    vertex.http.request.return_value = result or response()
    return vertex


def send(vertex, body):
    with patch.object(GCloudToken, 'get', return_value='test-token'):
        result = vertex.request('POST', '/chat/completions', body)
    return result, vertex.http.request.call_args.args[2]


def body(content, **extra):
    return {'model': 'gemini-3.5-flash-lite', 'max_tokens': 300, 'response_format': {'type': 'json_object'},
            'messages': [{'role': 'system', 'content': 'Judge.'}, {'role': 'user', 'content': content}], **extra}


class VertexImageTests(unittest.TestCase):
    def test_text_only_payload_is_unchanged(self):
        _, payload = send(adapter(), body('Hello'))
        self.assertEqual(payload, {
            'contents': [{'role': 'user', 'parts': [{'text': 'Hello'}]}],
            'systemInstruction': {'parts': [{'text': 'Judge.'}]},
            'generationConfig': {'responseMimeType': 'application/json', 'maxOutputTokens': 300,
                                 'thinkingConfig': {'thinkingLevel': 'MINIMAL'}}})

    def test_image_parts_become_inline_data_with_per_image_resolution(self):
        content = [{'type': 'text', 'text': 'Image 1:'}, {'type': 'image_url', 'image_url': {'url': DATA_URL}},
                   {'type': 'text', 'text': 'Image 2:'},
                   {'type': 'image_url', 'image_url': {'url': DATA_URL, 'detail': 'high'}}]
        _, payload = send(adapter(), body(content, media_resolution='low'))
        parts = payload['contents'][0]['parts']
        self.assertEqual(parts[0], {'text': 'Image 1:'})
        self.assertEqual(parts[1], {'inlineData': {'mimeType': 'image/jpeg', 'data': base64.b64encode(JPEG).decode()}})
        self.assertEqual(parts[3]['mediaResolution'], {'level': 'MEDIA_RESOLUTION_HIGH'})
        self.assertEqual(payload['generationConfig']['mediaResolution'], 'MEDIA_RESOLUTION_LOW')

    def test_strict_schema_and_logprobs(self):
        schema = {'type': 'object', 'properties': {'answer': {'type': 'string', 'enum': ['yes', 'no']}}}
        logprobs = {'chosenCandidates': [{'token': '{"answer":"', 'logProbability': -0.01},
                                         {'token': 'yes', 'logProbability': -0.2},
                                         {'token': '"}', 'logProbability': 0.0}],
                    'topCandidates': [{'candidates': [{'token': '{"answer":"', 'logProbability': -0.01}]},
                                      {'candidates': [{'token': 'yes', 'logProbability': -0.2},
                                                      {'token': 'no', 'logProbability': -1.7}]},
                                      {'candidates': [{'token': '"}', 'logProbability': 0.0}]}]}
        vertex = adapter(response('{"answer":"yes"}', logprobsResult=logprobs))
        result, payload = send(vertex, body('Q', logprobs=True, top_logprobs=5, response_format={
            'type': 'json_schema', 'json_schema': {'name': 'judgment', 'schema': schema, 'strict': True}}))
        config = payload['generationConfig']
        self.assertEqual(config['responseJsonSchema'], schema)
        self.assertEqual((config['responseLogprobs'], config['logprobs']), (True, 5))
        tokens = result['choices'][0]['logprobs']['content']
        self.assertEqual(''.join(t['token'] for t in tokens), '{"answer":"yes"}')
        self.assertEqual(tokens[1]['top_logprobs'][1], {'token': 'no', 'logprob': -1.7})

    def test_malformed_logprobs_are_dropped_not_trusted(self):
        vertex = adapter(response('{"answer":"yes"}', logprobsResult={'chosenCandidates': [{'token': 1}]}))
        result, _ = send(vertex, body('Q', logprobs=True, top_logprobs=2))
        self.assertNotIn('logprobs', result['choices'][0])

    def test_invalid_image_requests_never_acquire_credentials(self):
        bad = [
            [{'type': 'image_url', 'image_url': {'url': 'https://example.com/a.jpg'}}],
            [{'type': 'image_url', 'image_url': {'url': 'data:image/gif;base64,R0lGOD'}}],
            [{'type': 'image_url', 'image_url': {'url': DATA_URL, 'detail': 'ultra'}}],
            [{'type': 'image_url', 'image_url': {'url': DATA_URL}, 'extra': 1}],
            [{'type': 'audio', 'data': 'x'}],
            [{'type': 'image_url', 'image_url': {'url': DATA_URL}}] * 25,
        ]
        for content in bad:
            with self.subTest(content=str(content)[:80]), patch.object(GCloudToken, 'get') as get:
                with self.assertRaises(ValueError):
                    adapter().request('POST', '/chat/completions', body(content))
                get.assert_not_called()
        for extra in [{'media_resolution': 'ultra'}, {'logprobs': True, 'top_logprobs': 50},
                      {'response_format': {'type': 'json_schema', 'json_schema': {'schema': 'x'}}}]:
            with self.subTest(extra=extra), patch.object(GCloudToken, 'get') as get:
                with self.assertRaises(ValueError):
                    adapter().request('POST', '/chat/completions', body('Q', **extra))
                get.assert_not_called()

    def test_images_only_in_user_messages(self):
        request = body('Q')
        request['messages'][0]['content'] = [{'type': 'image_url', 'image_url': {'url': DATA_URL}}]
        with patch.object(GCloudToken, 'get') as get, self.assertRaises(ValueError):
            adapter().request('POST', '/chat/completions', request)
        get.assert_not_called()


class HelperImageTests(unittest.TestCase):
    def helper(self, provider):
        env = ({'TEXT_MODEL_PROVIDER': 'vertex', 'GOOGLE_CLOUD_PROJECT': 'project-test', 'TEXT_MODEL': 'gemini-3.5-flash-lite'}
               if provider == 'vertex' else
               {'TEXT_MODEL_PROVIDER': 'openrouter', 'TEXT_MODEL_API_KEY': 'test', 'TEXT_MODEL': 'test-model'})
        with patch.dict(os.environ, env):
            return Helper()

    def test_text_only_body_is_unchanged(self):
        helper = self.helper('openrouter')
        self.assertEqual(helper.completion_body([{'role': 'user', 'content': 'x'}], 300),
                         {'model': 'test-model', 'max_tokens': 300, 'response_format': {'type': 'json_object'},
                          'messages': [{'role': 'user', 'content': 'x'}]})

    def test_openai_compatible_resolution_is_per_image_detail(self):
        helper = self.helper('openrouter')
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'x'}, image_part(JPEG),
                                                 image_part(JPEG, 'high')]}]
        built = helper.completion_body(messages, 100, media_resolution='low', logprobs=3)
        parts = built['messages'][0]['content']
        self.assertEqual((parts[1]['image_url']['detail'], parts[2]['image_url']['detail']), ('low', 'high'))
        self.assertNotIn('media_resolution', built)
        self.assertEqual((built['logprobs'], built['top_logprobs']), (True, 3))
        self.assertNotIn('detail', messages[0]['content'][1]['image_url'])  # caller's messages untouched

    def test_vertex_helper_passes_resolution_and_reports_logprob_support(self):
        helper = self.helper('vertex')
        built = helper.completion_body([{'role': 'user', 'content': 'x'}], 100, media_resolution='high')
        self.assertEqual(built['media_resolution'], 'high')
        self.assertFalse(helper.supports_logprobs)  # gemini-3.5-flash-lite rejects logprobs (HTTP 400)
        helper.model = 'gemini-2.5-flash-lite'
        self.assertTrue(helper.supports_logprobs)
        with self.assertRaises(ValueError):
            helper.completion_body([], 100, media_resolution='ultra')

    def test_image_part_requires_jpeg(self):
        self.assertTrue(image_part(JPEG)['image_url']['url'].startswith('data:image/jpeg;base64,'))
        for bad in (b'GIF89a', 'text'):
            with self.assertRaises(ValueError):
                image_part(bad)


if __name__ == '__main__':
    unittest.main()
