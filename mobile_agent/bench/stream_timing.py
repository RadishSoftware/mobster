"""Time to first token for the benchmark's Claude calls: the same request, streamed, timed, reassembled.

frontier.AnthropicChat asks for a whole reply, so a turn's latency cannot be split into waiting and
generating. ``TimedAnthropicChat`` sends the identical request body with ``"stream": true`` over the same
kept-alive connection, with the same 429/529 handling, reads the server-sent events and rebuilds the reply
the non-streamed call returns (content blocks, stop reason, usage). The loop sees the same answer and the
same usage; each call's usage also carries:

- ``ttft_ms``: request sent to the first content delta of any kind (thinking included);
- ``text_ttft_ms``: to the first delta of the answer's text;
- ``first_action_ms``: to the moment the reply's text first holds a whole action object (the first item of
  ``"actions"``): what dispatching on the first action, instead of the whole reply, would wait for;
- ``total_ms``: to the end of the stream.

It is a measurement, for the benchmark only (``bench/iosworld.py`` with MOBSTER_BENCH_TTFT=on); the app
keeps the non-streamed call. Both arms of an A/B run it alike.
"""

import http.client
import json
import time

from ..frontier import RATE_LIMIT_RETRIES, AnthropicChat, AnthropicError, error_text, pause

TIMING_KEYS = ("ttft_ms", "text_ttft_ms", "first_action_ms", "total_ms")
ACTIONS_KEY = '"actions"'


def first_action_end(text):
    """The index just past the first complete object in ``text``'s ``"actions"`` array, or None (strings and
    escapes respected)."""
    at = text.find(ACTIONS_KEY)
    if at < 0:
        return None
    at = text.find("[", at + len(ACTIONS_KEY))
    if at < 0:
        return None
    depth, in_string, escaped = 0, False, False
    for index in range(at + 1, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        elif char == "]" and depth == 0:
            return None
    return None


class StreamError(AnthropicError):
    """An ``error`` event mid-stream (overloaded): transient, as a 529 is."""


def read_events(lines):
    """(event name, data dict) pairs from an iterable of SSE byte lines."""
    name, data = None, []
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n") if isinstance(raw, bytes) else raw.rstrip("\r\n")
        if not line:
            if data:
                try:
                    yield name, json.loads("\n".join(data))
                except ValueError:
                    pass
            name, data = None, []
            continue
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
    if data:
        try:
            yield name, json.loads("\n".join(data))
        except ValueError:
            pass


def assemble(events, clock=time.monotonic, started=None):
    """The non-streamed reply's JSON from a stream's events, plus ``_timing`` (ms from ``started``)."""
    started = clock() if started is None else started
    message, blocks = {}, {}
    timing = {"ttft_ms": None, "text_ttft_ms": None, "first_action_ms": None, "total_ms": None}
    for name, data in events:
        kind = data.get("type") or name
        if kind == "message_start":
            message = dict(data.get("message") or {})
            message["usage"] = dict(message.get("usage") or {})
        elif kind == "content_block_start":
            block = dict(data.get("content_block") or {})
            blocks[data.get("index", len(blocks))] = block
        elif kind == "content_block_delta":
            at = round((clock() - started) * 1000)
            if timing["ttft_ms"] is None:
                timing["ttft_ms"] = at
            delta = data.get("delta") or {}
            block = blocks.setdefault(data.get("index", 0), {"type": "text", "text": ""})
            if delta.get("type") == "text_delta":
                if timing["text_ttft_ms"] is None:
                    timing["text_ttft_ms"] = at
                block["text"] = block.get("text", "") + delta.get("text", "")
                if timing["first_action_ms"] is None and first_action_end(block["text"]) is not None:
                    timing["first_action_ms"] = at
            elif delta.get("type") == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif delta.get("type") == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif delta.get("type") == "input_json_delta":
                block["partial_json"] = block.get("partial_json", "") + delta.get("partial_json", "")
        elif kind == "message_delta":
            delta = data.get("delta") or {}
            for key in ("stop_reason", "stop_sequence", "stop_details"):
                if key in delta:
                    message[key] = delta[key]
            message.setdefault("usage", {}).update({k: v for k, v in (data.get("usage") or {}).items()
                                                    if v is not None})
        elif kind == "error":
            error = data.get("error") or {}
            raise StreamError(f"HTTP 529; Anthropic stream error: {error.get('type')}: {str(error.get('message'))[:200]}")
        elif kind == "message_stop":
            break
    timing["total_ms"] = round((clock() - started) * 1000)
    message["content"] = [blocks[i] for i in sorted(blocks)]
    message["_timing"] = timing
    return message


class TimedAnthropicChat(AnthropicChat):
    """AnthropicChat with each attempt streamed and timed (see the module docstring)."""

    @staticmethod
    def usage_of(data):
        usage = AnthropicChat.usage_of(data)
        usage.update({k: v for k, v in (data.get("_timing") or {}).items() if k in TIMING_KEYS})
        return usage

    def _attempt(self, payload, index, budget):
        body = json.loads(payload)
        body["stream"] = True
        payload = json.dumps(body).encode()
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
                    sent = time.monotonic()
                    connection.request("POST", "/v1/messages", payload, self._headers())
                    response = connection.getresponse()
                    status, wait = response.status, response.getheader("retry-after")
                    if status >= 300:
                        raw = response.read()
                    else:
                        reply = assemble(read_events(iter(response.readline, b"")), started=sent)
                        response.read()  # drain to the end so the connection can be kept alive
                except (OSError, http.client.HTTPException) as error:
                    connection.close()
                    connection = None
                    if own:
                        self.connection = None
                    if isinstance(error, TimeoutError) or reopened:
                        raise AnthropicError(f"HTTP {type(error).__name__}; Anthropic request outcome unknown") from None
                    reopened = True
                    continue
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
                return reply
            raise AnthropicError("HTTP 429; Anthropic HTTP 429: still rate limited after retries")
        finally:
            if own:
                self._claim.release()
            elif connection is not None:
                connection.close()


def timed(client):
    """``client`` as a TimedAnthropicChat when it is a plain AnthropicChat; any other client unchanged."""
    if type(client) is not AnthropicChat:
        return client
    copy = TimedAnthropicChat(client.model, key=client.key, reasoning=client.reasoning, host=client.host,
                              hedger=client.hedger)
    return copy
