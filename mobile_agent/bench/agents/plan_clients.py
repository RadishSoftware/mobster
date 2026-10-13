"""Bench-only model clients on the operator's own subscriptions, with OpenAIChat's interface.

- ``CodexCLIChat`` ("codex:<model>"): the Codex backend's Responses API (chatgpt.com/backend-api/codex/responses),
  authorised by the Codex CLI's own sign-in (~/.codex/auth.json, read only; the CLI refreshes it). It is the same
  request the coding-plan branch's ``ai_plans/codex_chat.CodexPlanChat`` sends, cut to what a benchmark needs:
  one forced ``step`` function with a strict schema, images inline, a session-wide prompt cache key. A model on the
  plan costs nothing per call, so ``frontier.cost_usd`` prices it at 0 ("chatgpt:" prefix in ``model``).
- ``ClaudeCLIChat`` ("claude-cli:<model>"): one headless ``claude -p`` process per call (Claude Code on the
  operator's plan), structured output through ``--json-schema``, images through stream-json input. A process
  start costs seconds (8.3 s for a trivial call, 2026-10-09), so it serves offline agreement gates, not timing.

Neither prints, logs or stores a token. Neither hedges (a twin would spend the plan twice).
"""

import base64
import http.client
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

CODEX_AUTH = Path.home() / ".codex" / "auth.json"
CODEX_HOST, CODEX_PATH = "chatgpt.com", "/backend-api/codex/responses"


class PlanCallError(RuntimeError):
    """A failed plan call; the message starts "ChatGPT HTTP <status>" or "Claude CLI" (never a token)."""


def _codex_token():
    data = json.loads(CODEX_AUTH.read_text())
    tokens = data.get("tokens") or {}
    if not tokens.get("access_token"):
        raise PlanCallError("ChatGPT: the Codex CLI is not signed in (codex login)")
    return tokens["access_token"], tokens.get("account_id") or ""


def _responses_input(messages):
    instructions, items = [], []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if message["role"] == "system":
            instructions.append("".join(p.get("text", "") for p in content if p.get("type") != "image_url"))
            continue
        parts = []
        for part in content:
            if part.get("type") == "image_url":
                parts.append({"type": "input_image", "image_url": part["image_url"]["url"],
                              "detail": part["image_url"].get("detail", "auto")})
            else:
                parts.append({"type": "output_text" if message["role"] == "assistant" else "input_text",
                              "text": part["text"]})
        items.append({"type": "message", "role": message["role"], "content": parts})
    return "\n\n".join(instructions) or "You operate an iPhone.", items


class CodexCLIChat:
    provider = "chatgpt"
    per_call_reasoning = True
    cancelled = None

    def __init__(self, model, *, reasoning="low", host=CODEX_HOST):
        self.slug = model.split(":", 1)[1] if ":" in model else model
        self.model = "chatgpt:" + self.slug
        self.reasoning, self.host = reasoning, host
        self.session = str(uuid.uuid4())
        self.connection = None
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
                      "calls": 0}
        self._lock = threading.Lock()
        self.on_first_action = None  # streaming dispatch hook (unused here)

    def body(self, messages, schema, reasoning=None, strict=True):
        from ...frontier import TOOL_DESCRIPTION
        instructions, items = _responses_input(messages)
        effort = reasoning or self.reasoning
        if effort == "none":
            effort = "low"   # the backend refuses "none" for the sol models
        description = TOOL_DESCRIPTION if "actions" in schema.get("properties", {}) else "Your answer."
        return {"model": self.slug, "instructions": instructions, "input": items, "store": False, "stream": True,
                "prompt_cache_key": self.session, "parallel_tool_calls": False, "tool_choice": "required",
                "tools": [{"type": "function", "name": "step", "description": description, "parameters": schema,
                           "strict": strict}],
                "reasoning": {"effort": effort}}

    def complete(self, messages, schema, *, timeout=60, reasoning=None, strict=True):
        payload = json.dumps(self.body(messages, schema, reasoning=reasoning, strict=strict)).encode()
        token, account = _codex_token()
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "Authorization": "Bearer " + token,
                   "chatgpt-account-id": account, "originator": "codex_cli_rs", "OpenAI-Beta": "responses=experimental",
                   "session_id": self.session}
        deadline = time.monotonic() + timeout
        for attempt in range(4):
            connection = http.client.HTTPSConnection(self.host, timeout=max(1.0, deadline - time.monotonic()))
            try:
                connection.request("POST", CODEX_PATH, payload, headers)
                response = connection.getresponse()
                if response.status != 200:
                    raw = response.read()[:400].decode(errors="replace")
                    if response.status in (429, 500, 502, 503, 504) and attempt < 3 and "usage_limit" not in raw:
                        time.sleep(2 * (attempt + 1))
                        continue
                    raise PlanCallError(f"ChatGPT HTTP {response.status}: {' '.join(raw.split())[:300]}")
                final = self._read(response, deadline)
                break
            except (OSError, http.client.HTTPException) as error:
                if attempt >= 1:
                    raise PlanCallError(f"ChatGPT HTTP {type(error).__name__}; outcome unknown") from None
            finally:
                connection.close()
        else:
            raise PlanCallError("ChatGPT: retries exhausted")
        raw = final.get("usage") or {}
        usage = {"prompt_tokens": raw.get("input_tokens", 0), "completion_tokens": raw.get("output_tokens", 0),
                 "cached_tokens": (raw.get("input_tokens_details") or {}).get("cached_tokens", 0),
                 "cache_write_tokens": 0}
        with self._lock:
            for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                self.usage[key] += usage[key] or 0
            self.usage["calls"] += 1
        call = next((item for item in final.get("output") or () if item.get("type") == "function_call"), None)
        if call is not None:
            return json.loads(call.get("arguments") or "{}"), usage
        text = "".join(c.get("text", "") for item in final.get("output") or () if item.get("type") == "message"
                       for c in item.get("content") or () if c.get("type") == "output_text")
        try:
            return json.loads(text), usage
        except ValueError:
            raise PlanCallError(f"ChatGPT: no function call in the reply ({final.get('status')})") from None

    @staticmethod
    def _read(response, deadline):
        final, items, data = None, [], []
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("ChatGPT call deadline exceeded")
            line = response.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text.startswith("data:"):
                data.append(text[5:].lstrip())
                continue
            if text or not data:
                continue
            payload, data = "\n".join(data), []
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "response.output_item.done":
                items.append(event.get("item") or {})
            elif kind in ("response.completed", "response.done"):
                final = event.get("response") or {}
            elif kind in ("response.failed", "error", "response.incomplete"):
                detail = json.dumps(event.get("response", event))[:300]
                raise PlanCallError(f"ChatGPT: the reply failed: {detail}")
        if final is None:
            raise PlanCallError("ChatGPT: the stream ended before the reply completed")
        if not final.get("output"):
            final["output"] = items
        return final


class ClaudeCLIChat:
    """``claude -p`` per call. ``model``: "claude-cli:<model id>"."""

    provider = "claude-cli"
    per_call_reasoning = True
    cancelled = None

    def __init__(self, model, *, reasoning="low", binary="claude"):
        self.slug = model.split(":", 1)[1] if ":" in model else model
        self.model = "claude-cli:" + self.slug
        self.reasoning, self.binary = reasoning, binary
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
                      "calls": 0}
        self.last_api_ms = None

    def complete(self, messages, schema, *, timeout=120, reasoning=None):
        from ...frontier import anthropic_schema
        system = "\n\n".join(m["content"] if isinstance(m["content"], str) else
                             "".join(p.get("text", "") for p in m["content"]) for m in messages if m["role"] == "system")
        blocks = []
        for message in messages:
            if message["role"] == "system":
                continue
            content = message["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for part in content:
                if part.get("type") == "image_url":
                    header, data = part["image_url"]["url"].split(",", 1)
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": header[5:].split(";")[0],
                                                               "data": data}})
                else:
                    blocks.append({"type": "text", "text": part["text"]})
        effort = reasoning or self.reasoning
        args = [self.binary, "-p", "--model", self.slug, "--system-prompt", system or "Answer in JSON.",
                "--json-schema", json.dumps(anthropic_schema(schema)), "--output-format", "stream-json", "--verbose",
                "--input-format", "stream-json", "--tools", "", "--no-session-persistence", "--strict-mcp-config"]
        if effort in ("low", "medium", "high"):
            args += ["--effort", effort]
        line = json.dumps({"type": "user", "message": {"role": "user", "content": blocks}})
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # the plan, never the API key
        done = subprocess.run(args, input=line + "\n", capture_output=True, text=True, timeout=timeout, env=env,
                              cwd=os.environ.get("TMPDIR", "/tmp"))
        try:
            out = next(item for item in (json.loads(line) for line in reversed(done.stdout.strip().splitlines())
                                         if line.startswith("{")) if item.get("type") == "result")
        except (ValueError, StopIteration):
            raise PlanCallError(f"Claude CLI: no JSON result ({done.returncode}): {done.stderr[:200]}") from None
        if out.get("is_error") or out.get("structured_output") is None:
            raise PlanCallError(f"Claude CLI: {out.get('subtype')} {str(out.get('result'))[:200]}")
        raw = out.get("usage") or {}
        usage = {"prompt_tokens": (raw.get("input_tokens") or 0) + (raw.get("cache_read_input_tokens") or 0)
                 + (raw.get("cache_creation_input_tokens") or 0),
                 "completion_tokens": raw.get("output_tokens") or 0,
                 "cached_tokens": raw.get("cache_read_input_tokens") or 0,
                 "cache_write_tokens": raw.get("cache_creation_input_tokens") or 0}
        for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens"):
            self.usage[key] += usage[key]
        self.usage["calls"] += 1
        self.last_api_ms = out.get("duration_api_ms")
        return out["structured_output"], usage


def plan_client(model, reasoning="low"):
    """A subscription client for "codex:<model>" or "claude-cli:<model>", else None."""
    if model.startswith("codex:"):
        return CodexCLIChat(model, reasoning=reasoning)
    if model.startswith("claude-cli:"):
        return ClaudeCLIChat(model, reasoning=reasoning)
    return None


def image_bytes(part):
    return base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
