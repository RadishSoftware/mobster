"""Smart on any OpenAI-compatible Chat Completions endpoint."""

import http.client
import json
import re
import threading
import time
import urllib.parse

SERVER_ERRORS = frozenset({500, 502, 503, 504})
BACKOFF_RETRIES = 2
MAX_ADAPTATIONS = 4
QUOTA = re.compile(r"quota|insufficient|usage limit|limit reached|exceeded your|billing|credit|balance|plan", re.I)
_adapted = {}
_adapted_lock = threading.Lock()


PLAIN_HTTP = ("{name}'s address starts with http://, which would send your key unencrypted. Use https://, or http:// "
              "only for this Mac or your local network.")


def plain_http_ok(url):
    """Whether ``url`` may be plain http://: only to this Mac or a private network (Ollama, LM Studio, a LAN box)."""
    from ..outlets.webhook import _private
    parts = urllib.parse.urlsplit(str(url or ""))
    return parts.scheme == "http" and _private((parts.hostname or "").rstrip(".").lower())


class ProviderError(RuntimeError):
    def __init__(self, message, *, status=None, quota=False):
        super().__init__(message)
        self.status, self.quota = status, quota


class CompatChat:
    """``provider``: providers.Provider (name, base URL, extra headers, vision)."""

    cancelled = None
    per_call_strict = True

    def __init__(self, provider, model, *, key, vision=None, headers=None, token=None):
        self.spec = provider
        self.provider = provider.id
        self.label = provider.name
        self.model = f"{provider.id}:{model}"
        self.slug = model
        self.key = key
        self.token = token  # a callable that returns a fresh bearer (a plan's refreshing token), else None
        self.vision = provider.vision(model) if vision is None else vision
        self.extra_headers = dict(provider.headers or {}, **(headers or {}))
        url = urllib.parse.urlsplit(provider.base_url)
        self.scheme, self.host = url.scheme, url.netloc
        self.path = url.path.rstrip("/") + "/chat/completions"
        self.connection = None
        self.model_failed = False
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
                      "calls": 0}
        self._claim = threading.Lock()
        self._count_lock = threading.Lock()
        with _adapted_lock:
            self.adapted = set(_adapted.get(self.model, ()))

    def body(self, messages, schema, *, strict=True):
        from ..model_clients import TOOL_DESCRIPTION
        out = []
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                out.append({"role": message["role"], "content": content})
                continue
            parts = []
            for part in content:
                if part.get("type") == "image_url":
                    if self.vision:
                        parts.append({"type": "image_url", "image_url": {"url": part["image_url"]["url"]}})
                    continue
                parts.append({"type": "text", "text": part["text"]})
            if message["role"] == "system" or not self.vision:
                # Some endpoints take parts only on user messages and some text-only ones none,
                # so these go as one string.
                out.append({"role": message["role"], "content": "".join(p.get("text", "") for p in parts)})
            else:
                out.append({"role": message["role"], "content": parts})
        body = {"model": self.slug, "messages": out}
        description = TOOL_DESCRIPTION if "actions" in schema.get("properties", {}) else "Your answer."
        if "no_tools" in self.adapted:
            body["response_format"] = {"type": "json_object"}
            out.append({"role": "system", "content": "Answer with one JSON object that matches this schema, and "
                        "nothing else:\n" + json.dumps(schema)})
        else:
            function = {"name": "step", "description": description, "parameters": schema}
            if "loose" not in self.adapted:
                function["strict"] = strict
            body["tools"] = [{"type": "function", "function": function}]
            body["tool_choice"] = ("auto" if "choice_auto" in self.adapted else "required"
                                   if "choice_required" in self.adapted else
                                   {"type": "function", "function": {"name": "step"}})
            if "no_parallel" not in self.adapted:
                body["parallel_tool_calls"] = False
        return body

    def adapt(self, error):
        text = str(error).lower()
        change = None
        if "parallel_tool_calls" in text:
            change = "no_parallel"
        elif "tool_choice" in text or "tool choice" in text:
            change = "choice_required" if "choice_required" not in self.adapted else "choice_auto"
        elif "strict" in text or "additionalproperties" in text or "schema" in text:
            change = "loose" if "loose" not in self.adapted else "no_tools"
        elif "tool" in text or "function" in text:
            change = "no_tools"
        if change is None or change in self.adapted:
            return False
        self.adapted.add(change)
        with _adapted_lock:
            _adapted.setdefault(self.model, set()).add(change)
        return True

    def complete(self, messages, schema, *, timeout=60, strict=True):
        from ..model_clients import pause
        started, backoffs, adaptations = time.monotonic(), 0, 0
        while True:
            left = timeout - (time.monotonic() - started)
            if left <= .05:
                raise TimeoutError(f"{self.label} call deadline exceeded")
            options = {"strict": strict} if self.per_call_strict else {}
            payload = json.dumps(self.body(messages, schema, **options)).encode()
            try:
                data = self._send(payload, left)
                break
            except ProviderError as error:
                # A 400 that names a refused field ran nothing, so the adapted turn is safe to send again.
                if error.status == 400 and adaptations < MAX_ADAPTATIONS and self.adapt(error):
                    adaptations += 1
                    continue
                if (error.status in SERVER_ERRORS or error.status == 429 and not error.quota) \
                        and backoffs < BACKOFF_RETRIES:
                    backoffs += 1
                    pause(2 * backoffs, getattr(self, "cancelled", None))
                    continue
                self.model_failed = True
                raise
        raw = data.get("usage") or {}
        if "input_tokens" in raw:  # an Anthropic Messages reply
            raw = _anthropic_usage(data)
        details = raw.get("prompt_tokens_details") or {}
        usage = {"prompt_tokens": raw.get("prompt_tokens", 0) or 0,
                 "completion_tokens": raw.get("completion_tokens", 0) or 0,
                 "cached_tokens": details.get("cached_tokens", 0) or raw.get("prompt_cache_hit_tokens", 0) or 0,
                 "cache_write_tokens": 0}
        with self._count_lock:
            for name in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                self.usage[name] += usage[name]
            self.usage["calls"] += 1
        self.model_failed = False
        return self.answer(data, self.label), usage

    def _connect(self, timeout):
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, timeout=timeout)
        if not plain_http_ok(self.spec.base_url):
            # The key would cross the internet unencrypted: never sent.
            raise ProviderError(PLAIN_HTTP.format(name=self.label))
        return http.client.HTTPConnection(self.host, timeout=timeout)

    def _headers(self):
        from .. import __version__
        bearer = self.token() if callable(self.token) else self.key
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": f"Mobster/{__version__}"}
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        headers.update(self.extra_headers)
        return headers

    def _send(self, payload, budget):
        own = self._claim.acquire(blocking=False)
        connection, reopened = (self.connection if own else None), False
        try:
            while True:
                if connection is None:
                    connection = self._connect(budget)
                    if own:
                        self.connection = connection
                try:
                    connection.timeout = budget
                    connection.request("POST", self.path, payload, self._headers())
                    response = connection.getresponse()
                    raw = response.read()
                except (OSError, http.client.HTTPException) as error:
                    connection.close()
                    connection = None
                    if own:
                        self.connection = None
                    if isinstance(error, TimeoutError) or reopened:
                        raise
                    reopened = True
                    continue
                if response.status >= 300:
                    text = raw.decode(errors="replace")
                    try:
                        detail = json.loads(text)
                        detail = (detail.get("error") or {}).get("message") if isinstance(detail.get("error"), dict) \
                            else detail.get("error") or detail.get("message") or text
                    except (ValueError, AttributeError):
                        detail = text
                    detail = " ".join(str(detail).split())[:300]
                    raise ProviderError(f"{self.label} HTTP {response.status}: {detail}", status=response.status,
                                        quota=response.status in (402, 429) and bool(QUOTA.search(detail)))
                return json.loads(raw)
        finally:
            if own:
                self._claim.release()
            elif connection is not None:
                connection.close()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    @staticmethod
    def answer(data, label="The model"):
        choice = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
        calls = choice.get("tool_calls") or ()
        call = next((c for c in calls if (c.get("function") or {}).get("name") == "step"), calls[0] if calls else None)
        text = (call.get("function") or {}).get("arguments") if call else choice.get("content")
        if isinstance(text, (dict, list)):
            return text
        text = str(text or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        if not text:
            raise RuntimeError(f"{label}: no answer in the reply")
        try:
            return json.loads(text)
        except ValueError:
            found = re.search(r"\{.*\}", text, re.S)
            if found:
                return json.loads(found.group(0))
            raise RuntimeError(f"{label}: the reply was not JSON: {text[:120]!r}") from None


class AnthropicCompatChat(CompatChat):
    """A third-party Anthropic Messages endpoint (MiniMax, Kimi Code's Anthropic route, gateways)."""

    per_call_strict = False  # its schema has no strict flag

    def __init__(self, provider, model, **kwargs):
        super().__init__(provider, model, **kwargs)
        url = urllib.parse.urlsplit(provider.base_url)
        self.path = url.path.rstrip("/") + "/v1/messages"

    def body(self, messages, schema):
        system, out = [], []
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if message["role"] == "system":
                system.append("".join(p.get("text", "") for p in content if p.get("type") != "image_url"))
                continue
            blocks = []
            for part in content:
                if part.get("type") == "image_url":
                    if not self.vision:
                        continue
                    url = part["image_url"]["url"]
                    media, _, data = url.partition(";base64,")
                    blocks.append({"type": "image", "source": {"type": "base64",
                                                               "media_type": media.removeprefix("data:")
                                                               or "image/jpeg",
                                                               "data": data}})
                else:
                    blocks.append({"type": "text", "text": part["text"]})
            out.append({"role": message["role"], "content": blocks or [{"type": "text", "text": "."}]})
        body = {"model": self.slug, "max_tokens": 4096, "messages": out,
                "tools": [{"name": "step", "description": "Your answer.", "input_schema": schema}],
                "tool_choice": {"type": "tool", "name": "step"} if "choice_auto" not in self.adapted
                else {"type": "auto"}}
        if system:
            body["system"] = "\n\n".join(system)
        return body

    def _headers(self):
        headers = super()._headers()
        bearer = headers.pop("Authorization", "")
        headers["x-api-key"] = bearer.removeprefix("Bearer ")
        headers["anthropic-version"] = "2023-06-01"
        if self.spec.bearer:
            headers["Authorization"] = bearer
        return headers

    @staticmethod
    def answer(data, label="The model"):
        for block in data.get("content") or ():
            if block.get("type") == "tool_use" and block.get("name") == "step":
                return block.get("input") or {}
        text = "".join(b.get("text", "") for b in data.get("content") or () if b.get("type") == "text").strip()
        if text:
            return CompatChat.answer({"choices": [{"message": {"content": text}}]}, label)
        raise RuntimeError(f"{label}: no answer in the reply")

def _anthropic_usage(data):
    raw = data.get("usage") or {}
    return {"prompt_tokens": raw.get("input_tokens", 0) or 0, "completion_tokens": raw.get("output_tokens", 0) or 0,
            "prompt_tokens_details": {"cached_tokens": raw.get("cache_read_input_tokens", 0) or 0}}
