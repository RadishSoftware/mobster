"""Minimal multimodal Vertex AI ``generateContent`` client for the baselines.

Uses the operator's existing gcloud login exactly like Mobster's helper
(``gemini.GCloudToken``): the token lives in memory only, goes only to
Google's fixed endpoint, and is never logged. Project and region come from the
same env file (``GOOGLE_CLOUD_PROJECT``, ``GOOGLE_CLOUD_LOCATION``); they are
never printed. No automatic retries: a failed call is a failed step.

Rates are Vertex AI's published standard global rates (read 2026-09-23 from
cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing).
Gemini 3.6-3.8 Flash have introductory rates of half these through
2026-12-31; the report uses the standard rates so numbers stay comparable
after the promotion, and says so.
"""

import os
import re
import threading
import time

from ..gemini import GCloudToken
from ..transport import HTTP, TransportError

RATE_DATE = "2026-09-23"
# USD per 1M tokens (input, output incl. thinking) at <=200K input tokens, global endpoint.
RATES = {
    "gemini-3.8-flash": (1.50, 7.50),
    "gemini-3.7-flash": (1.50, 7.50),
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-pro-preview": (2.00, 12.00),
}
INTRO_RATES = {"gemini-3.8-flash": (0.75, 3.75), "gemini-3.7-flash": (0.75, 3.75), "gemini-3.6-flash": (0.75, 3.75)}


def cost_usd(model, usage):
    """Published-rate estimate; None when the model or usage is unknown (never a synthetic zero)."""
    base = next((name for name in RATES if model == name or model.startswith(name + "-")), None)
    if base is None or not isinstance(usage, dict):
        return None
    prompt = usage.get("promptTokenCount")
    output = (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0)
    if not isinstance(prompt, int):
        return None
    input_rate, output_rate = RATES[base]
    return (prompt * input_rate + output * output_rate) / 1_000_000


class Vertex:
    RETRY_BACKOFF = (2, 4, 8, 16, 30)

    def __init__(self, project=None, location=None, sleep=time.sleep):
        self.sleep = sleep
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        self.location = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]|[0-9]{6,20}", self.project):
            raise ValueError("Set GOOGLE_CLOUD_PROJECT (the baselines use the helper's Vertex project)")
        host = ("aiplatform.googleapis.com" if self.location == "global"
                else f"{self.location}-aiplatform.googleapis.com")
        self.base = "https://" + host
        self._local = threading.local()

    def _http(self):
        http = getattr(self._local, "http", None)
        if http is None:
            http = self._local.http = HTTP(self.base, timeout=120)
        return http

    def generate(self, model, body, timeout=90, retries=None):
        """One model call. Returns (response, info) with latency, usage and cost; raises on failure.

        Throttling (HTTP 429/503) and dropped connections are retried with backoff
        (``RETRY_BACKOFF``). They are infrastructure, not the agent: the time they
        cost is reported as ``throttle_ms`` and excluded from the agent's time and
        budget by the caller. A request that got a model answer is never retried.
        """
        if not re.fullmatch(r"gemini-[a-z0-9.-]{1,70}", model):
            raise ValueError("Expected a Gemini model id")
        route = (f"/v1/projects/{self.project}/locations/{self.location}/publishers/google/models/"
                 f"{model}:generateContent")
        backoff = list(self.RETRY_BACKOFF if retries is None else self.RETRY_BACKOFF[:retries])
        throttled = 0.0
        attempts = 0
        while True:
            token = GCloudToken.get(10)
            http = self._http()
            http.key = token
            started = time.monotonic()
            attempts += 1
            try:
                response = http.request("POST", route, body, timeout)
                break
            except TransportError as error:
                message = str(error)
                if message.startswith("HTTP 401;"):
                    GCloudToken.invalidate(token)
                transient = message.startswith(("HTTP 429", "HTTP 503", "HTTP 500", "HTTP SSLError",
                                                "HTTP ConnectionResetError", "HTTP RemoteDisconnected",
                                                "HTTP BrokenPipeError", "HTTP TimeoutError"))
                if not transient or not backoff:
                    raise
                wait = backoff.pop(0)
                throttled += (time.monotonic() - started) * 1000 + wait * 1000
                self.sleep(wait)
            finally:
                http.key = ""
        latency = (time.monotonic() - started) * 1000
        usage = response.get("usageMetadata") or {}
        info = {"model": response.get("modelVersion", model), "latency_ms": round(latency, 1),
                "throttle_ms": round(throttled, 1), "attempts": attempts,
                "prompt_tokens": usage.get("promptTokenCount"),
                "output_tokens": (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0),
                "cost_usd": cost_usd(model, usage)}
        return response, info

    def probe(self, model, timeout=30, retries=2):
        """Is this model callable from the configured project? A tiny text request (~20 tokens)."""
        try:
            _, info = self.generate(model, {"contents": [{"role": "user", "parts": [{"text": "Reply with OK."}]}],
                                            "generationConfig": {"maxOutputTokens": 256}}, timeout, retries)
            return True, "ok" + (f" after {info['attempts']} attempts" if info["attempts"] > 1 else "")
        except Exception as error:
            return False, f"{type(error).__name__}: {str(error)[:80]}"


# Computer use on Vertex (Gemini 3.5 Flash or later, mobile environment), best first.
CU_PREFERENCE = ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash")
SOM_PREFERENCE = ("gemini-3.1-pro-preview", "gemini-3.8-flash", "gemini-3.7-flash")


def pick_model(vertex, preference, log=None):
    """The first model in ``preference`` that answers now (throttled models are skipped, and logged)."""
    for model in preference:
        ok, reason = vertex.probe(model)
        if log:
            log(f"  model {model}: {'ok' if ok else reason}")
        if ok:
            return model
    return None

    def close(self):
        http = getattr(self._local, "http", None)
        if http is not None:
            http.close()


def parts_of(response):
    candidates = response.get("candidates") or []
    if not candidates:
        feedback = response.get("promptFeedback") or {}
        raise TransportError(f"no candidates (blockReason={feedback.get('blockReason')})")
    content = candidates[0].get("content") or {"role": "model", "parts": []}
    content.setdefault("role", "model")
    return content, content.get("parts") or [], candidates[0].get("finishReason")
