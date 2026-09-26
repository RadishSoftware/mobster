"""Gemini on Vertex AI using the operator's existing gcloud login.

Tokens are captured in memory, sent only to Google's fixed endpoint, never logged.
No API enablement, billing changes, credential-file edits, or automatic retries.
"""
import json
import os
import re
import subprocess
import threading
import time

from .transport import Deadline, TransportError
from .inference import ProviderResponseRejected
from .helper_models import thinking_config


def gcloud_path():
    """gcloud, also when the desktop app was opened from Finder with a minimal PATH."""
    from pathlib import Path
    import shutil
    for candidate in ('/opt/homebrew/bin/gcloud', '/usr/local/bin/gcloud',
                      str(Path.home() / 'google-cloud-sdk' / 'bin' / 'gcloud'),
                      '/opt/homebrew/share/google-cloud-sdk/bin/gcloud'):
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which('gcloud') or 'gcloud'


class GCloudToken:
    _lock = threading.Lock()
    _token = ""
    _until = 0
    # gcloud may return an already-cached token, so its default one-hour lifetime is
    # not one fresh hour from this call. Keep our extra cache short and invalidate on401.
    CACHE_SECONDS = 300

    @classmethod
    def get(cls, timeout=10):
        deadline = Deadline(timeout)
        if not cls._lock.acquire(timeout=deadline.remaining()):
            raise TimeoutError("Google authentication is busy")
        try:
            if cls._token and time.monotonic() < cls._until:
                deadline.remaining()
                return cls._token
            try:
                result = subprocess.run([gcloud_path(), 'auth', 'print-access-token', '--quiet'],
                    capture_output=True, text=True, timeout=min(10, deadline.remaining()), check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise TransportError("Existing Google Cloud credentials are unavailable") from None
            token = result.stdout.strip()
            if (result.returncode or not token or len(token) > 16384
                    or not re.fullmatch(r'[A-Za-z0-9._~+/=-]+', token)):
                raise TransportError("Existing Google Cloud credentials could not authenticate")
            deadline.remaining()
            cls._token, cls._until = token, time.monotonic() + cls.CACHE_SECONDS
            return token
        finally:
            cls._lock.release()

    @classmethod
    def invalidate(cls, token):
        if not cls._lock.acquire(blocking=False):
            return  # Another request is already refreshing; do not extend this one's deadline.
        try:
            if cls._token == token:
                cls._token, cls._until = '', 0
        finally:
            cls._lock.release()


# Image input limits for one request: enough for a screen of grid cells or all
# photos of one profile, small enough that one call never uploads megabytes.
MAX_IMAGES = 24
MAX_IMAGE_BASE64_BYTES = 12_000_000
_DATA_URL = re.compile(r'data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/]+={0,2})')
# Gemini 3 media resolution per image: ~256 / ~560 / ~1,100 tokens measured for a
# 512x512 JPEG on gemini-3.5-flash-lite (low / default-high), 2026-09-23.
MEDIA_RESOLUTION = {'low': 'MEDIA_RESOLUTION_LOW', 'medium': 'MEDIA_RESOLUTION_MEDIUM',
                    'high': 'MEDIA_RESOLUTION_HIGH'}
MAX_SCHEMA_BYTES = 16000


def _image_part(part):
    """OpenAI-style image_url data URL -> Gemini inlineData (never a remote fetch)."""
    image = part.get('image_url')
    if not isinstance(image, dict) or set(image) - {'url', 'detail'} or not isinstance(image.get('url'), str):
        raise ValueError("Image parts need an inline data URL")
    match = _DATA_URL.fullmatch(image['url'])
    if not match:
        raise ValueError("Only inline base64 JPEG, PNG or WebP images are supported")
    detail = image.get('detail')
    if detail is not None and detail not in {'low', 'high', 'auto'}:
        raise ValueError("Invalid image detail")
    out = {'inlineData': {'mimeType': match.group(1), 'data': match.group(2)}}
    if detail in ('low', 'high'):  # 'auto' leaves the model's default resolution.
        out['mediaResolution'] = {'level': MEDIA_RESOLUTION[detail]}
    return out, len(match.group(2))


def _logprobs(candidate, limit):
    """Vertex logprobsResult -> OpenAI-shaped choices[0].logprobs, or None.

    Logprobs are an optional signal: malformed ones are dropped, never trusted.
    """
    result = candidate.get('logprobsResult')
    if not isinstance(result, dict):
        return None
    try:
        chosen, top = result['chosenCandidates'], result.get('topCandidates', [])
        if not isinstance(chosen, list) or not isinstance(top, list) or len(chosen) > 4000:
            return None
        content = []
        for index, token in enumerate(chosen):
            entry = {'token': token['token'], 'logprob': token['logProbability'], 'top_logprobs': []}
            if index < len(top):
                for alt in top[index]['candidates'][:limit]:
                    entry['top_logprobs'].append({'token': alt['token'], 'logprob': alt['logProbability']})
            for item in (entry, *entry['top_logprobs']):
                if (not isinstance(item['token'], str) or len(item['token']) > 200
                        or type(item['logprob']) not in (int, float) or not item['logprob'] <= 0):
                    return None
            content.append(entry)
        return {'content': content}
    except (KeyError, TypeError, IndexError):
        return None


class VertexHTTP:
    """Translate Mobster's small helper contract into native generateContent.

    Text-only requests are translated exactly as before. Optional features are
    enabled per request: OpenAI-style image content parts (inline data URLs
    only), ``media_resolution``, a strict JSON schema, and logprobs.
    """
    def __init__(self, project=None, location=None):
        from .http_pool import pooled_http

        self.project = project or os.environ.get('GOOGLE_CLOUD_PROJECT', '')
        self.location = location or os.environ.get('GOOGLE_CLOUD_LOCATION', 'global')
        if not re.fullmatch(r'[a-z][a-z0-9-]{4,61}[a-z0-9]|[0-9]{6,20}', self.project):
            raise ValueError("Set an explicit GOOGLE_CLOUD_PROJECT for Gemini")
        if not re.fullmatch(r'global|[a-z]+-[a-z]+[0-9]', self.location):
            raise ValueError("Invalid Google Cloud region")
        host = 'aiplatform.googleapis.com' if self.location == 'global' else self.location + '-aiplatform.googleapis.com'
        self.base_url = 'https://' + host
        # Pooled per host: the bearer token is set per request and cleared after,
        # so an idle pooled connection never holds a credential.
        self.http = pooled_http(self.base_url)
        self._inflight = threading.Lock()

    def warm(self):
        """Pre-open a connection and pre-fetch the gcloud token, both off-thread."""
        from .http_pool import POOL

        def token():
            try:
                GCloudToken.get(10)
            except Exception:
                pass  # The first real request fetches (and reports) it.
        threading.Thread(target=token, name="mobster-token-prewarm", daemon=True).start()
        return POOL.prewarm(self.base_url)

    def request(self, method, path, body=None, timeout=20):
        deadline = Deadline(timeout)
        if method != 'POST' or path != '/chat/completions' or not isinstance(body, dict):
            raise ValueError("Vertex adapter accepts only text-helper requests")
        model = body.get('model')
        if not isinstance(model, str) or not re.fullmatch(r'gemini-[a-z0-9.-]{1,70}', model):
            raise ValueError("Expected a Gemini model identifier")
        messages = body.get('messages')
        if not isinstance(messages, list) or not messages or len(messages) > 16:
            raise ValueError("Expected bounded helper messages")
        system, contents = [], []
        input_bytes = image_bytes = images = 0
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("Only text messages are supported")
            content = message.get('content')
            if isinstance(content, str):
                texts, parts = [content], [{'text': content}]
            elif isinstance(content, list) and content and message.get('role') == 'user' and len(content) <= 2 * MAX_IMAGES + 16:
                texts, parts = [], []
                for part in content:
                    if not isinstance(part, dict):
                        raise ValueError("Invalid content part")
                    if part.get('type') == 'text' and set(part) == {'type', 'text'} and isinstance(part['text'], str):
                        texts.append(part['text'])
                        parts.append({'text': part['text']})
                    elif part.get('type') == 'image_url' and set(part) == {'type', 'image_url'}:
                        translated, size = _image_part(part)
                        images += 1
                        image_bytes += size
                        parts.append(translated)
                    else:
                        raise ValueError("Unsupported content part")
                if images > MAX_IMAGES or image_bytes > MAX_IMAGE_BASE64_BYTES:
                    raise ValueError("Gemini image input exceeds its budget")
            else:
                raise ValueError("Only text messages are supported")
            try:
                input_bytes += sum(len(text.encode('utf-8')) for text in texts)
            except UnicodeError:
                raise ValueError("Gemini input contains invalid Unicode") from None
            if input_bytes > 128000:
                raise ValueError("Gemini helper input exceeds its byte budget")
            if message.get('role') == 'system':
                system.extend(parts)
            elif message.get('role') in {'user', 'assistant'}:
                contents.append({'role': 'model' if message['role'] == 'assistant' else 'user', 'parts': parts})
            else:
                raise ValueError("Unsupported Gemini message role")
        if not contents or contents[0]['role'] != 'user':
            raise ValueError("Gemini requires an initial user message")
        response_format = body.get('response_format', {'type': 'json_object'})
        schema = None
        if response_format != {'type': 'json_object'}:
            spec = response_format.get('json_schema') if isinstance(response_format, dict) else None
            if (not isinstance(response_format, dict) or response_format.get('type') != 'json_schema'
                    or set(response_format) != {'type', 'json_schema'} or not isinstance(spec, dict)
                    or not isinstance(spec.get('schema'), dict) or set(spec) - {'name', 'schema', 'strict'}
                    or len(json.dumps(spec['schema'])) > MAX_SCHEMA_BYTES):
                raise ValueError("Gemini helper adapter supports JSON-object mode or one bounded JSON schema")
            schema = spec['schema']
        count = body.get('max_completion_tokens', body.get('max_tokens', 300))
        if type(count) is not int or not 1 <= count <= 4000:
            raise ValueError("Invalid helper output token budget")
        config = {'responseMimeType': 'application/json', 'maxOutputTokens': count, **thinking_config(model)}
        if schema is not None:
            config['responseJsonSchema'] = schema
        resolution = body.get('media_resolution')
        if resolution is not None:
            if resolution not in MEDIA_RESOLUTION:
                raise ValueError("Invalid media resolution")
            config['mediaResolution'] = MEDIA_RESOLUTION[resolution]
        top_logprobs = None
        if body.get('logprobs') is not None:
            top_logprobs = body.get('top_logprobs', 1)
            if body['logprobs'] is not True or type(top_logprobs) is not int or not 1 <= top_logprobs <= 20:
                raise ValueError("Invalid logprobs request")
            config['responseLogprobs'], config['logprobs'] = True, top_logprobs
        payload = {'contents': contents, 'systemInstruction': {'parts': system}, 'generationConfig': config}
        route = f'/v1/projects/{self.project}/locations/{self.location}/publishers/google/models/{model}:generateContent'
        # One request at a time on the shared client (its key is per request). A second
        # caller -- a deferred survey compile while an answer probe runs -- waits its turn
        # within its own deadline rather than failing.
        if not self._inflight.acquire(timeout=max(0.0, deadline.remaining())):
            raise TransportError("Gemini helper busy for the whole deadline; not retried")
        try:
            token = GCloudToken.get(deadline.remaining())
            self.http.key = token
            try:
                response = self.http.request('POST', route, payload, deadline.remaining())
            except TransportError as exc:
                if str(exc).startswith('HTTP 401;'):
                    # Refresh on a future request only. Never replay this request automatically.
                    GCloudToken.invalidate(token)
                raise
        finally:
            self.http.key = ''
            self._inflight.release()
        # Usage is independent of answer validity. A blocked or truncated answer
        # may still be billed; never discard those counts or expose its content.
        raw_usage = response.get("usageMetadata", {}) if isinstance(response, dict) else {}
        usage = {key: value for key, value in raw_usage.items() if key in {
            'promptTokenCount', 'candidatesTokenCount', 'totalTokenCount', 'thoughtsTokenCount',
            'cachedContentTokenCount', 'toolUsePromptTokenCount'}} if isinstance(raw_usage, dict) else {}
        usage_valid = isinstance(raw_usage, dict) and all(
            type(value) is int and 0 <= value <= 9_007_199_254_740_991 for value in usage.values())
        actual_model = response.get('modelVersion', model) if isinstance(response, dict) else model
        model_valid = isinstance(actual_model, str) and re.fullmatch(r'gemini-[a-zA-Z0-9._-]{1,100}', actual_model)
        try:
            if not isinstance(response, dict):
                raise ValueError()
            feedback = response.get('promptFeedback')
            if feedback is not None and (not isinstance(feedback, dict) or feedback.get('blockReason')):
                raise ValueError()
            candidates = response['candidates']
            if (not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict)
                    or candidates[0].get('finishReason') != 'STOP'):
                raise ValueError()
            parts = candidates[0]['content']['parts']
            if not isinstance(parts, list) or not 1 <= len(parts) <= 128:
                raise ValueError()
            text_parts = []
            for part in parts:
                if (not isinstance(part, dict) or not isinstance(part.get('text'), str)
                        or type(part.get('thought', False)) is not bool):
                    raise ValueError()
                if not part.get('thought'):
                    text_parts.append(part['text'])
            content = ''.join(text_parts)
            if not content.strip() or len(content.encode('utf-8')) > 128000:
                raise ValueError()
            if not usage_valid or not model_valid:
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError, UnicodeError):
            raise ProviderResponseRejected(actual_model if model_valid else model,
                                           usage if usage_valid else {}) from None
        try:
            deadline.remaining()
        except TimeoutError:
            raise ProviderResponseRejected(actual_model, usage) from None
        choice = {'message': {'content': content}, 'finish_reason': 'stop'}
        if top_logprobs is not None:
            logprobs = _logprobs(candidates[0], top_logprobs)
            if logprobs is not None:
                choice['logprobs'] = logprobs
        return {'choices': [choice], 'usage': usage, 'model': actual_model}

    def close(self):
        self.http.close()


def configured():
    if os.environ.get('TEXT_MODEL_PROVIDER') == 'vertex':
        return bool(os.environ.get('GOOGLE_CLOUD_PROJECT') and os.environ.get('TEXT_MODEL'))
    return bool(os.environ.get('TEXT_MODEL_API_KEY') and os.environ.get('TEXT_MODEL'))
