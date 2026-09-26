"""Provider-call telemetry, never prompts, credentials, or generated content.

Success means a provider response arrived, not that its answer or task was correct.
Missing usage stays missing: tokens are reported, never estimated or synthesized.
"""

import time

from .costs import estimate_cost
from .hybrid import inference_path
from .transport import TransportError


def reported_usage(raw, provider=None):
    if not isinstance(raw, dict):
        return {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens", "promptTokenCount"),
        "output_tokens": ("output_tokens", "completion_tokens", "candidatesTokenCount"),
        "total_tokens": ("total_tokens", "totalTokenCount"),
        "cached_input_tokens": ("cached_input_tokens", "cachedContentTokenCount"),
        "reasoning_tokens": ("reasoning_tokens", "thoughtsTokenCount"),
        "tool_input_tokens": ("tool_input_tokens", "toolUsePromptTokenCount"),
    }
    result = {}
    for field, names in aliases.items():
        for name in names:
            value = raw.get(name)
            if type(value) is int and 0 <= value <= 9_007_199_254_740_991:
                result[field] = value
                break
    if (provider == "typesafe" and "total_tokens" not in result
            and "input_tokens" in result and "output_tokens" in result):
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


class ProviderResponseRejected(TransportError):
    """An unusable answer can still have billable, provider-reported usage."""

    def __init__(self, model, usage):
        super().__init__(f"{model} returned an incomplete or blocked result; no action authorized")
        self.model = model
        self.usage = reported_usage(usage, "google")


def request_inference(http, path, body, timeout, *, emit, provider, call_id, model, purpose):
    if emit is None:
        return http.request("POST", path, body, timeout)
    # Hybrid path is diagnostic only: System One never shares action authority.
    metadata = {"provider": provider, "call_id": call_id, "model": model, "purpose": purpose,
                "hybrid_path": (inference_path(provider).value if inference_path(provider) else "unknown")}
    emit({"event": "inference_started", **metadata})
    started = time.monotonic()
    try:
        response = http.request("POST", path, body, timeout)
    except Exception as exc:
        usage = exc.usage if isinstance(exc, ProviderResponseRejected) else {}
        if isinstance(exc, ProviderResponseRejected):
            metadata["model"] = exc.model
        pricing = estimate_cost(provider, metadata["model"], usage, getattr(http, "location", None))
        emit({"event": "inference_finished", **metadata, "success": False,
              "latency_ms": round((time.monotonic() - started) * 1000, 2),
              "usage": usage, "error_type": type(exc).__name__, **pricing})
        raise
    elapsed = round((time.monotonic() - started) * 1000, 2)
    actual = response.get("model") if isinstance(response, dict) else None
    if isinstance(actual, str) and 0 < len(actual) <= 200:
        metadata["model"] = actual
    usage = reported_usage(response.get("usage"), provider) if isinstance(response, dict) else {}
    pricing = estimate_cost(provider, metadata["model"], usage, getattr(http, "location", None))
    emit({"event": "inference_finished", **metadata, "success": True, "latency_ms": elapsed,
          "usage": usage, **pricing})
    return response
