"""Model clients: the price table and the provider transports Mobster calls models through."""

import http.client
import json
import math
import os
import re
import threading
import time

from . import harness_api
from .transport import Deadline, TransportError

# List prices, USD per 1M tokens (input, output, cached input, cache write; None = input rate), read 2026-09-25 to
# 2026-10-09; estimates, not invoices.
PRICES = {"gpt-5.5": (5.0, 30.0, 0.5, None), "gpt-5.6-sol": (4.0, 20.0, 0.4, 5.0),
          "gpt-5.6-terra": (2.0, 12.0, 0.2, 2.5), "gpt-5.6-luna": (0.2, 1.2, 0.02, 0.25),
          "gpt-5-mini": (0.25, 2.0, 0.025, None)}
# GPT-6: the same page, read 2026-09-28, with GPT-5.6's shape.
PRICES.update({"gpt-6-astra": (10.0, 50.0, 1.0, 12.5), "gpt-6-sol": (2.0, 10.0, 0.2, 2.5),
               "gpt-6-luna": (0.1, 0.5, 0.01, 0.125)})
PRICES["gpt-6.1-sol"] = (2.0, 10.0, 0.1, 2.5)
# Gemini on Vertex: Vertex AI's standard global rates, read 2026-09-23 (3.6-3.8 Flash are half price until
# 2026-12-31, listed at standard).
PRICES.update({model: (rate[0], rate[1], None, None) for model, rate in {
    "gemini-3.8-flash": (1.50, 7.50), "gemini-3.7-flash": (1.50, 7.50), "gemini-3.6-flash": (1.50, 7.50),
    "gemini-3.5-flash": (1.50, 9.00), "gemini-3.1-pro-preview": (2.00, 12.00)}.items()})
# Claude: platform.claude.com/docs/en/about-claude/pricing, read 2026-09-28; 5-minute cache writes are 1.25x, hits 0.1x
# (Opus 5.5 0.05x, Fable 5.1 0.025x).
PRICES.update({
    "claude-sonnet-5-5": (2.0, 10.0, 0.1, 2.5), "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5),
    "claude-opus-5-5": (4.0, 20.0, 0.2, 5.0), "claude-opus-5": (5.0, 25.0, 0.5, 6.25),
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5), "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25)})
# Claude Haiku 5.5 (7 Oct 2026), prompts up to 100k tokens, read 2026-10-09; Sonnet 5.5's hits are 0.05x input there.
PRICES["claude-haiku-5-5"] = (0.10, 0.50, 0.01, 0.125)


def cost_usd(model, usage):
    """Estimated list-price cost of ``usage`` (OpenAIChat.usage), or None for an unpriced model."""
    if str(model or "").startswith("chatgpt:"):
        return 0.0 if usage else None
    providers = harness_api.hooks().model_routes()
    routed, _ = providers.split(model)
    if routed:
        # A plan costs nothing per call; a provider's key is billed at its own rates, which Mobster doesn't list.
        return (0.0 if usage else None) if providers.get(routed).group == "plan" else None
    price = PRICES.get(model)
    if price is None or not usage:
        return None
    cached, written = usage.get("cached_tokens", 0), usage.get("cache_write_tokens", 0)
    fresh = usage.get("prompt_tokens", 0) - cached - written
    return round((fresh * price[0] + cached * (price[0] if price[2] is None else price[2])
                  + written * (price[0] if price[3] is None else price[3])
                  + usage.get("completion_tokens", 0) * price[1]) / 1e6, 5)


def ceiling_cost_usd(usage):
    """``usage`` at the highest listed rates, so a spend cap stops a run with no listed price early."""
    prompt = max(max(p[0], p[3] or 0.0) for p in PRICES.values())
    output = max(p[1] for p in PRICES.values())
    usage = usage or {}
    return round((usage.get("prompt_tokens", 0) * prompt + usage.get("completion_tokens", 0) * output) / 1e6, 5)


RATE_LIMIT_RETRIES = 5
# A model call is a pure read, so one that got a server error is sent again twice, 1 s then 2 s later.
SERVER_ERROR_RETRIES = 2
SERVER_ERRORS = frozenset({500, 502, 503, 504})


# The wording matters: as "Your next actions on the phone." chains shortened from 1.9 to 1.4-1.5 actions a call (36
# replayed turns, 25 Sep).
TOOL_DESCRIPTION = ("Your answer: the next 1 to 6 actions. Chain every action whose screen you can predict (fill "
                    "several fields then Save, search then open the result...); stop only where you must see the "
                    "result first.")


class CallCancelled(RuntimeError):
    """The task was stopped while a model call waited to be sent again: it ends ``stopped``, never retried."""


def pause(seconds, cancelled=None):
    """Wait ``seconds`` (a retry's backoff)."""
    seconds = max(0.0, float(seconds))
    if not callable(cancelled):
        time.sleep(seconds)
        return
    slices = max(1, math.ceil(seconds / .2))
    for _ in range(slices):
        if cancelled():
            raise CallCancelled("Stopped while a model call waited to retry")
        time.sleep(seconds / slices)
    if cancelled():
        raise CallCancelled("Stopped while a model call waited to retry")


def error_text(label, status, raw, limit=300):
    """"<label> HTTP <status>: <detail>" for a failed call."""
    body = raw.decode(errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        names = [error[key] for key in ("code", "type") if isinstance(error.get(key), str) and error[key]]
        message = error.get("message") if isinstance(error.get("message"), str) else ""
        detail = ": ".join([*dict.fromkeys(names), " ".join(message.split())] if message else dict.fromkeys(names))
        if detail:
            return f"{label} HTTP {status}: {detail[:limit]}"
    return f"{label} HTTP {status}: {body[:limit]}"


class _KeptAliveJSON:
    """One kept-alive HTTPS connection for JSON POSTs, shared by the OpenAI and Vertex clients."""

    cancelled = None

    # The task contract is compiled while the first decision is in flight,
    # so one call's reasoning is set without touching ``reasoning``.
    per_call_reasoning = True

    def __init__(self, host):
        self.host = host
        self.connection = None
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
                      "calls": 0}
        self._claim = threading.Lock()  # held while the kept-alive connection carries a request
        self._count_lock = threading.Lock()

    def _post(self, path, payload, headers, timeout, *, label, final_429=lambda raw: False):
        """``json.loads`` of the reply; ``headers()`` is called per attempt (a Vertex token may refresh)."""
        deadline = Deadline(timeout)
        claim = getattr(self, "_claim", None)
        own = claim is None or claim.acquire(blocking=False)
        connection = self.connection if own else None
        server_errors = 0
        try:
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                left = deadline.remaining()
                if connection is None:
                    connection = http.client.HTTPSConnection(self.host, timeout=left)
                    if own:
                        self.connection = connection
                try:
                    request_headers = headers()
                    left = deadline.remaining()
                    connection.timeout = left
                    if getattr(connection, "sock", None) is not None:
                        connection.sock.settimeout(left)
                    connection.request("POST", path, payload, request_headers)
                    response = connection.getresponse()
                    raw = response.read()
                except (OSError, http.client.HTTPException):
                    connection.close()
                    connection = None
                    if own:
                        self.connection = None
                    if attempt >= 1:
                        raise
                    continue
                if response.status == 429 and not final_429(raw) and attempt < RATE_LIMIT_RETRIES:
                    pause(min(2 ** attempt, 16, deadline.remaining()), getattr(self, "cancelled", None))
                    continue
                if (response.status in SERVER_ERRORS and server_errors < SERVER_ERROR_RETRIES
                        and attempt < RATE_LIMIT_RETRIES):
                    server_errors += 1
                    pause(min(server_errors, deadline.remaining()), getattr(self, "cancelled", None))
                    continue
                if response.status >= 300:
                    raise RuntimeError(error_text(label, response.status, raw))
                deadline.remaining()
                return json.loads(raw)
            raise RuntimeError("unreachable")
        finally:
            if own and claim is not None:
                claim.release()
            elif not own and connection is not None:
                connection.close()

    def _count(self, usage):
        """Add one call's normalised usage (prompt/completion/cached/cache-write tokens) to the running total."""
        lock = getattr(self, "_count_lock", None) or threading.Lock()
        with lock:
            for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens"):
                self.usage[key] += usage.get(key, 0)
            self.usage["calls"] += 1


def explicit_caching(model):
    """Models that take cache breakpoints (GPT-5.6 on): older ones answer prompt_cache_options with a 400."""
    return model.startswith(("gpt-5.6", "gpt-6"))


class OpenAIChat(_KeptAliveJSON):
    """The Responses API over one kept-alive HTTPS connection; error bodies surface (truncated)."""

    provider = "openai"
    per_call_strict = True

    def __init__(self, model, *, key=None, reasoning="low", host="api.openai.com"):
        self.model, self.reasoning = model, reasoning
        self.key = key or os.environ.get("OPENAI_API_KEY", "")
        if not self.key:
            raise ValueError("OPENAI_API_KEY is not set")
        super().__init__(host)

    def body(self, messages, schema, reasoning=None, *, strict=True):
        """The /v1/responses request for Chat-style ``messages``; ``reasoning`` overrides the client's for it."""
        explicit = explicit_caching(self.model)
        items = []
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            parts = []
            for part in content:
                if part.get("type") == "image_url":
                    parts.append({"type": "input_image", "image_url": part["image_url"]["url"],
                                  "detail": part["image_url"].get("detail", "auto")})
                    continue
                parts.append({"type": "input_text", "text": part["text"]})
                if part.get("cache") and explicit:
                    parts[-1]["prompt_cache_breakpoint"] = {"mode": "explicit"}
            items.append({"role": "developer" if message["role"] == "system" else message["role"], "content": parts})
        # A forced function call: as a json_schema text format the answer came back repeated, and billed,
        # in 15 of 36 replies.
        body = {"model": self.model, "input": items, "store": False, "parallel_tool_calls": False,
                "tools": [{"type": "function", "name": "step", "parameters": schema, "strict": strict,
                           "description": TOOL_DESCRIPTION if "actions" in schema.get("properties", {})
                           else "Your answer."}],
                "tool_choice": {"type": "function", "name": "step"}}
        if explicit:
            body["prompt_cache_options"] = {"mode": "explicit"}
        effort = reasoning or self.reasoning
        if effort:
            body["reasoning"] = {"effort": effort}
        return body

    def complete(self, messages, schema, *, timeout=60, reasoning=None, strict=True):
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.key}
        body = self.body(messages, schema, reasoning=reasoning, strict=strict)
        # insufficient_quota is an empty account, not a busy one: retrying cannot clear it.
        data = self._post("/v1/responses", json.dumps(body).encode(), lambda: headers,
                          timeout, label="OpenAI", final_429=lambda raw: b"insufficient_quota" in raw
                          or b"credit_balance_exhausted" in raw)
        raw, details = data.get("usage") or {}, (data.get("usage") or {}).get("input_tokens_details") or {}
        usage = {"prompt_tokens": raw.get("input_tokens", 0), "completion_tokens": raw.get("output_tokens", 0),
                 "cached_tokens": details.get("cached_tokens", 0),
                 "cache_write_tokens": details.get("cache_write_tokens", 0)}
        self._count(usage)
        return self.answer(data), usage

    @staticmethod
    def answer(data):
        """The arguments of a /v1/responses reply's function call."""
        call = next((item for item in data.get("output") or () if item.get("type") == "function_call"), None)
        if call is None:
            raise RuntimeError(f"OpenAI: no function call in the reply ({data.get('status')})")
        return json.loads(call.get("arguments") or "")


class GeminiChat(_KeptAliveJSON):
    """Vertex AI generateContent over one kept-alive connection, with OpenAIChat's interface."""

    # "none" is LOW, the least every Gemini 3 model takes: gemini-3.8-flash answers MINIMAL with a 400.
    LEVELS = {"none": "LOW", "minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM", "high": "HIGH"}
    provider = "google"

    def __init__(self, model, *, reasoning="low", project=None, location=None):
        self.model, self.reasoning = model, reasoning
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        self.location = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "global") or "global"
        if not self.project:
            raise ValueError("GOOGLE_CLOUD_PROJECT is not set")
        super().__init__("aiplatform.googleapis.com" if self.location == "global"
                         else f"{self.location}-aiplatform.googleapis.com")

    @staticmethod
    def _parts(content):
        if isinstance(content, str):
            return [{"text": content}]
        parts = []
        for part in content:
            if part.get("type") == "text":
                parts.append({"text": part["text"]})
            elif part.get("type") == "image_url":
                # "detail": "low" is not forwarded: Gemini uses its default media resolution (unmeasured).
                header, data = part["image_url"]["url"].split(",", 1)
                parts.append({"inlineData": {"mimeType": header[5:].split(";")[0], "data": data}})
        return parts

    def complete(self, messages, schema, *, timeout=60, reasoning=None):
        from .gemini import GCloudToken
        system = [p for m in messages if m["role"] == "system" for p in self._parts(m["content"])]
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": self._parts(m["content"])}
                    for m in messages if m["role"] != "system"]
        config = {"responseMimeType": "application/json", "responseJsonSchema": schema, "maxOutputTokens": 8000}
        level = reasoning if reasoning in self.LEVELS else self.reasoning  # "none" keeps the client's own level
        if level in self.LEVELS:
            config["thinkingConfig"] = {"thinkingLevel": self.LEVELS[level]}
        body = {"contents": contents, "systemInstruction": {"parts": system}, "generationConfig": config}
        path = (f"/v1/projects/{self.project}/locations/{self.location}/publishers/google/models/"
                f"{self.model}:generateContent")
        # Every 429 is retried: shared capacity, not an empty account, so it is waited out.
        data = self._post(path, json.dumps(body).encode(),
                          lambda: {"Content-Type": "application/json",
                                   "Authorization": "Bearer " + GCloudToken.get(20)},
                          timeout, label="Vertex")
        meta = data.get("usageMetadata") or {}
        usage = {"prompt_tokens": meta.get("promptTokenCount", 0),
                 "completion_tokens": meta.get("candidatesTokenCount", 0) + meta.get("thoughtsTokenCount", 0),
                 "cached_tokens": meta.get("cachedContentTokenCount", 0)}
        self._count(usage)
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return json.loads(text), usage


ANTHROPIC_HOST = "api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
# Replies are ~160 tokens plus thinking; the cap only stops a runaway reply.
ANTHROPIC_MAX_TOKENS = 16000
# One decision attempt's own deadline: p90 is 3.8-6.7 s by model (9 Oct); one Claude call hung 90 s.
ATTEMPT_CAP_SECONDS = 30.0
# Claude's structured outputs refuse a schema with any of these (400),
# so the loop and ``engines.structure`` check them instead.
UNSUPPORTED_SCHEMA_KEYS = frozenset({
    "maxItems", "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "pattern", "minProperties", "maxProperties", "uniqueItems", "patternProperties", "propertyNames", "contains",
    "minContains", "maxContains", "not", "if", "then", "else", "dependentRequired", "dependentSchemas",
    "unevaluatedProperties", "unevaluatedItems", "prefixItems"})
SCHEMA_FORMATS = frozenset({"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6",
                            "uuid"})
# Models whose thinking cannot be turned off: "none" runs them at effort low.
ALWAYS_THINKING = ("claude-opus-5-5", "claude-fable-", "claude-mythos-")
# Sonnet 5.5 refuses thinking disabled; "between_tools" turns off up-front thinking.
BETWEEN_TOOLS = ("claude-sonnet-5-5",)
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def anthropic_schema(schema):
    """``schema`` as Claude's structured outputs take it."""
    if isinstance(schema, list):
        return [anthropic_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key in UNSUPPORTED_SCHEMA_KEYS or key == "format" and value not in SCHEMA_FORMATS:
            continue
        if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
            value = {name: anthropic_schema(sub) for name, sub in value.items()}
        elif key in ("items", "anyOf", "allOf", "oneOf"):
            value = anthropic_schema(value)
        elif key == "minItems" and isinstance(value, int):
            value = min(value, 1)
        out["anyOf" if key == "oneOf" else key] = value
    kinds = out.get("type") if isinstance(out.get("type"), list) else [out.get("type")]
    if "object" in kinds or "properties" in out:
        out["additionalProperties"] = False
    return out


def anthropic_thinking(model, reasoning):
    """(thinking, effort) for an OpenAI-style reasoning level. low ... max: adaptive thinking at that effort."""
    level = {"minimal": "low"}.get(reasoning, reasoning)
    if model.startswith("claude-haiku-4"):
        return None, None  # no adaptive thinking and no effort parameter
    # Haiku 5.5 thinks by default (9 Oct) and takes adaptive thinking and effort like Sonnet, which keeps a Smart turn
    # at effort low.
    if level in EFFORTS:
        return {"type": "adaptive"}, level
    if model.startswith(ALWAYS_THINKING):
        return None, "low"
    if model.startswith(BETWEEN_TOOLS):
        return {"type": "between_tools"}, "low"
    return {"type": "disabled"}, None


class AnthropicError(TransportError):
    """A failed Messages API request."""


class AnthropicChat:
    """Claude on the Messages API, with OpenAIChat's interface."""

    provider = "anthropic"
    per_call_reasoning = True
    cancelled = None  # as _KeptAliveJSON's: set by the loop for its run

    def __init__(self, model, *, key=None, reasoning="low", host=ANTHROPIC_HOST, hedger=None):
        self.model, self.reasoning, self.host = model, reasoning, host
        self.key = key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self.connection = None
        self.hedger = hedger
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
                      "calls": 0, "hedges": 0}
        self._claim = threading.Lock()  # held while the kept-alive connection carries a request
        self._count_lock = threading.Lock()
        self.on_hedge_usage = None

    @staticmethod
    def _blocks(content, *, cache=True):
        """Content blocks for Chat-style ``content``; a text part marked ``cache`` ends a breakpoint."""
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        blocks = []
        for part in content:
            if part.get("type") == "image_url":
                header, data = part["image_url"]["url"].split(",", 1)
                # Claude sees the whole 640 px shot (~1,150 visual tokens), so OpenAI's "detail" has no counterpart.
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": header[5:].split(";")[0],
                                                           "data": data}})
                continue
            blocks.append({"type": "text", "text": part["text"]})
            if cache and part.get("cache"):
                blocks[-1]["cache_control"] = {"type": "ephemeral"}
        return blocks

    def body(self, messages, schema, reasoning=None):
        """The /v1/messages request for Chat-style ``messages``; ``reasoning`` overrides the client's for it."""
        system = [block for m in messages if m["role"] == "system" for block in self._blocks(m["content"], cache=False)]
        if system:
            system[-1]["cache_control"] = {"type": "ephemeral"}
        turns = [{"role": "assistant" if m["role"] == "assistant" else "user", "content": self._blocks(m["content"])}
                 for m in messages if m["role"] != "system"]
        # Structured output, not a forced tool call: Sonnet 5.5 and Opus 5.5 refuse tool_choice any/tool with a 400.
        body = {"model": self.model, "max_tokens": ANTHROPIC_MAX_TOKENS, "messages": turns,
                "output_config": {"format": {"type": "json_schema", "schema": anthropic_schema(schema)}}}
        if system:
            body["system"] = system
        thinking, effort = anthropic_thinking(self.model, reasoning or self.reasoning)
        if thinking is not None:
            body["thinking"] = thinking
        if effort is not None:
            body["output_config"]["effort"] = effort
        return body

    def complete(self, messages, schema, *, timeout=60, reasoning=None):
        payload = json.dumps(self.body(messages, schema) if reasoning is None
                             else self.body(messages, schema, reasoning=reasoning)).encode()
        hedge = harness_api.hooks().hedge()
        purpose = "decision" if "actions" in (schema.get("properties") or {}) else "other"
        replies = {}

        def attempt(index, budget):
            # A decision attempt past ATTEMPT_CAP_SECONDS fails in transit (a pure read) so the hedger can resend it.
            cap = min(budget, ATTEMPT_CAP_SECONDS) if purpose == "decision" else budget
            replies[index] = self._attempt(payload, index, cap)
            return replies[index]

        def lost(index, start, end, ok, won):
            if ok and not won:
                self._count(self.usage_of(replies[index]), hedge=True)

        if hedge is not None and hedge.enabled():
            hedger = self.hedger or hedge.HEDGERS["anthropic"]
            data = hedger.run(purpose, attempt, timeout=timeout, on_attempt=lost)
        else:  # one attempt: hedging off, or the open-source engine, which has no hedge module
            data = attempt(0, timeout)
        usage = self.usage_of(data)
        self._count(usage)
        return self.answer(data), usage

    @staticmethod
    def usage_of(data):
        """A reply's usage in OpenAIChat's terms."""
        raw = data.get("usage") or {}
        read, written = raw.get("cache_read_input_tokens") or 0, raw.get("cache_creation_input_tokens") or 0
        return {"prompt_tokens": (raw.get("input_tokens") or 0) + read + written,
                "completion_tokens": raw.get("output_tokens") or 0, "cached_tokens": read,
                "cache_write_tokens": written}

    def _count(self, usage, *, hedge=False):
        with self._count_lock:
            for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens"):
                self.usage[key] += usage.get(key, 0)
            self.usage["hedges" if hedge else "calls"] += 1
        hook = self.on_hedge_usage if hedge else None
        if hook is not None:
            try:
                hook(usage)
            except Exception:
                pass  # a run that already ended may refuse late events; usage above still counts it

    @staticmethod
    def answer(data):
        """The JSON in a reply's text; a refusal or a reply cut at max_tokens raises."""
        stop = data.get("stop_reason")
        if stop == "refusal":
            category = (data.get("stop_details") or {}).get("category")
            raise RuntimeError(f"Anthropic declined the request (refusal{': ' + category if category else ''})")
        text = "".join(block.get("text", "") for block in data.get("content") or () if block.get("type") == "text")
        if stop == "max_tokens":
            raise RuntimeError(f"Anthropic: the reply stopped at max_tokens ({ANTHROPIC_MAX_TOKENS})")
        try:
            return json.loads(text)
        except ValueError:
            raise RuntimeError(f"Anthropic: the reply was not JSON ({stop}): {text[:120]!r}") from None

    def _headers(self):
        headers = {"Content-Type": "application/json", "x-api-key": self.key, "anthropic-version": ANTHROPIC_VERSION}
        workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
        if workspace:
            headers["anthropic-workspace-id"] = workspace
        return headers

    def _connect(self, timeout):
        return http.client.HTTPSConnection(self.host, timeout=timeout)

    def _attempt(self, payload, index, budget):
        """One attempt of a call: the primary (index 0) on the kept-alive connection unless a losing attempt of an
        earlier call still holds it, a twin or a resend on a fresh one."""
        started = time.monotonic()
        own = index == 0 and self._claim.acquire(blocking=False)
        connection, reopened = None, False
        try:
            for tries in range(RATE_LIMIT_RETRIES + 1):
                left = budget - (time.monotonic() - started)
                if left <= .05:
                    raise TimeoutError("Anthropic call deadline exceeded")
                if connection is None:
                    connection = (self.connection if own and self.connection is not None else self._connect(left))
                    if own:
                        self.connection = connection
                try:
                    connection.timeout = left
                    if connection.sock is not None:
                        connection.sock.settimeout(left)
                    connection.request("POST", "/v1/messages", payload, self._headers())
                    response = connection.getresponse()
                    raw = response.read()
                    status, wait = response.status, response.getheader("retry-after")
                except (OSError, http.client.HTTPException) as error:
                    connection.close()
                    connection = None
                    if own:
                        self.connection = None
                    if isinstance(error, TimeoutError) or reopened:
                        raise AnthropicError(
                            f"HTTP {type(error).__name__}; Anthropic request outcome unknown") from None
                    reopened = True  # a kept-alive connection the server had closed: reopen once
                    continue
                # A 429 without retry-after is a spend cap, which waiting can't clear.
                if status in (429, 529) and tries < RATE_LIMIT_RETRIES and (status == 529 or wait is not None):
                    try:
                        delay = max(0.0, float(wait)) if wait is not None else min(2 ** tries, 16)
                    except ValueError:
                        delay = min(2 ** tries, 16)
                    if delay < budget - (time.monotonic() - started) - 1:
                        pause(delay, getattr(self, "cancelled", None))
                        continue
                if status >= 300:
                    raise AnthropicError(f"HTTP {status}; " + error_text("Anthropic", status, raw))
                return json.loads(raw)
            raise AnthropicError("HTTP 429; Anthropic HTTP 429: still rate limited after retries")
        finally:
            if own:
                self._claim.release()
            elif connection is not None:
                connection.close()


def chat_client(model, reasoning="low", *, key=None):
    """The model's client: a Settings › Models provider for "<provider>:<model>", else Gemini,
    Claude or OpenAI by id."""
    providers = harness_api.hooks().model_routes()
    if providers.split(model)[0]:
        return providers.client(model, reasoning)
    if model.startswith("gemini-"):
        return GeminiChat(model, reasoning=reasoning)
    if model.startswith("claude-"):
        return AnthropicChat(model, key=key, reasoning=reasoning)
    return OpenAIChat(model, key=key, reasoning=reasoning)


def transient_model_error(error):
    """Whether a model call failed in transit, so sending it again may succeed."""
    hedge = harness_api.hooks().hedge()
    text = str(error)
    if any(code in text for code in ("insufficient_quota", "credit_balance_exhausted", "credit balance is too low",
                                     "billing_error")):
        return False
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    try:
        if hedge is not None and hedge.transient(error):
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(re.search(r"\bHTTP (429|5\d\d)\b", text)) or "outcome unknown" in text \
        or "deadline exceeded" in text


def http_detail(error):
    """The failure's HTTP status ("HTTP 400"), "timed out", or the error's type: what a history line names."""
    message = str(error)
    found = re.search(r"\bHTTP (\d{3})\b", message)
    if found:
        return f"HTTP {found.group(1)}"
    if isinstance(error, TimeoutError) or "timeout" in message.casefold():
        return "timed out"
    return type(error).__name__
