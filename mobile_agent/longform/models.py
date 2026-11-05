"""Model routing for a long task (DESIGN §6.5): the strong model where it decides, the cheap one where it reads."""

from dataclasses import dataclass
from typing import Any, Callable, Optional

HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"
LUNA, SOL = "gpt-5.6-luna", "gpt-5.6-sol"
# The synthesis model by plan kind, from the A/B on recorded notebooks (§11.3; mobile_agent/docs/longform.md has the
# numbers). Opus stays a kind's default only where its mean rubric beats Sonnet's by at least 0.05. On 8 Oct 2026 it
# didn't: feed 0.849 against 0.844 (2 runs each), survey 0.902 against 0.885 (4 runs each), while Sonnet wrote the
# report in 11 s instead of 21 to 25 s for about 40% of the cost ($0.031 against $0.076 to $0.080). So Sonnet writes
# every kind's report.
SYNTH_DEFAULT = {"feed": SONNET, "survey": SONNET, "chain": SONNET, "browse": SONNET}
SYNTH_EFFORT = {OPUS: "medium", SONNET: "low", SOL: "low"}
OPUS_MIN_ITEMS = 12


def provider_of(model):
    return "anthropic" if str(model or "").startswith("claude-") else "openai"


def extract_model(nav_model):
    return HAIKU if provider_of(nav_model) == "anthropic" else LUNA


def synth_model_for(kind, items, nav_model=SONNET):
    """The report's model: SYNTH_DEFAULT for the kind on Claude (Opus only with at least OPUS_MIN_ITEMS items or a
    survey), gpt-5.6-sol with an OpenAI key only."""
    if provider_of(nav_model) != "anthropic":
        return SOL
    model = SYNTH_DEFAULT.get(kind, SONNET)
    if model == OPUS and kind != "survey" and (items or 0) < OPUS_MIN_ITEMS:
        return SONNET
    return model


def followup_model(nav_model):
    return SONNET if provider_of(nav_model) == "anthropic" else SOL


class NoHedge:
    """A hedger that never sends a twin: one attempt, and a call that failed in transit is not resent at once (the
    caller retries with its own backoff)."""

    resends = 0

    def run(self, purpose, attempt, *, timeout, on_attempt=None, clock=None):
        return attempt(0, timeout)


@dataclass
class ModelSet:
    nav: Any                                   # the frontier's client (Smart's model), metered as today
    extract: Callable[[], Any]                 # a new metered client for one extraction worker (purpose "extract")
    digest: Any                                # metered, purpose "digest"
    synth: Any                                 # metered, purpose "synthesis"
    synth_fallback: Optional[Any] = None       # Sonnet when synth is Opus; used once if the synthesis call fails


def client(model, *, key, reasoning="low", hedge=True):
    """A client for ``model`` on ``key`` (model_clients.chat_client), never hedged when ``hedge`` is False."""
    from ..model_clients import AnthropicChat, chat_client
    made = chat_client(model, reasoning=reasoning, key=key)
    if not hedge and isinstance(made, AnthropicChat):
        made.hedger = NoHedge()
    return made


def model_set(nav_client, key, *, provider=None, emit, items_hint=0, kind="feed", factory=None):
    """The run's ModelSet."""
    from ..costs import meter
    nav_model = getattr(nav_client, "model", SONNET) or SONNET
    make = factory or (lambda model, reasoning, hedge: client(model, key=key, reasoning=reasoning, hedge=hedge))
    cheap = extract_model(nav_model)
    synth_name = synth_model_for(kind, items_hint, nav_model)

    def metered(model, reasoning, hedge, purpose):
        made = make(model, reasoning, hedge)
        return meter(made, emit, provider=provider_of(model), purpose=purpose)

    synth = _Lazy(lambda: metered(synth_name, SYNTH_EFFORT.get(synth_name, "low"), True, "synthesis"), synth_name)
    # A failed synthesis call is sent once more, on Sonnet (a fresh client), before the deterministic report.
    fallback_model = SONNET if provider_of(nav_model) == "anthropic" else SOL
    fallback = _Lazy(lambda: metered(fallback_model, SYNTH_EFFORT.get(fallback_model, "low"), True, "synthesis"),
                     fallback_model)
    digest = _Lazy(lambda: metered(cheap, "none", False, "digest"), cheap)
    return ModelSet(nav=nav_client, extract=lambda: metered(cheap, "none", False, "extract"), digest=digest,
                    synth=synth, synth_fallback=fallback)


class _Lazy:
    """A client made on first use (no connection or key read until the run needs it)."""

    def __init__(self, make, model):
        self._make, self.model, self._client = make, model, None

    def get(self):
        if self._client is None:
            self._client = self._make()
        return self._client

    def complete(self, messages, schema, **kwargs):
        return self.get().complete(messages, schema, **kwargs)

    @property
    def usage(self):
        return getattr(self._client, "usage", {}) if self._client is not None else {}

    def close(self):
        connection = getattr(self._client, "connection", None)
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass
