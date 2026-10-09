"""The two engines the Mac app runs, and the one frozen Smart config the benchmark shares.

- ``smart`` (the default): frontier.FrontierAgent on gpt-5.6-sol, exactly as ``bench.iosworld --policy
  frontier`` runs it. SMART is the only place its model, reasoning, limits and switches are set; the
  harness imports them from here, and tests/test_engines.py proves both paths build the same agent. The
  model is the one thing a user may change (``smart_model``): MOBSTER_SMART_MODEL, or Claude for a user whose
  only key is Anthropic's; every other setting stays SMART_CONFIG's.
- ``fast``: today's Jev pipeline (agent.Agent with the helper), cheap and quick, weak on long tasks.

This module also holds what the app needs around the Smart loop and the benchmark does not: which key it
uses, whether that key reaches the model, the cost estimate before a task, the metering that puts every model
call in the usage ledger, and SmartEvents, which turns the loop's trace into the contract events the
dashboard renders (docs/engine-contract.md). Nothing here changes a decision the loop makes.
"""

import dataclasses
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

SMART, FAST = "smart", "fast"
ENGINES = (SMART, FAST)


@dataclass(frozen=True)
class SmartConfig:
    """The frozen Smart loop. Change it only with a benchmark run that shows why."""
    model: str = "gpt-5.6-sol"
    reasoning: str = "low"
    max_steps: int = 50          # iOSWorld's own step limit
    max_seconds: int = 900
    screenshots: bool = True
    settle_seconds: float = .6
    # The environment switches the loop reads, at the values it was measured with: compact replies (REPLY),
    # READ_LIST and SCROLL_TO (MACROS), Bet 1's task contract, declared commits and DONE gate (CONTRACT), and
    # Bet 2's exact actuation (EXACT_TEXT, RICH_ROWS, TAP_XY, WDA_RECOVERY, GLIDE_ALL, EARLY_CALL).
    switches: tuple = (
        ("MOBSTER_FRONTIER_REPLY", "compact"), ("MOBSTER_FRONTIER_MACROS", "on"), ("MOBSTER_FRONTIER_CONTRACT", "on"),
        ("MOBSTER_EXACT_TEXT", "on"), ("MOBSTER_RICH_ROWS", "on"), ("MOBSTER_TAP_XY", "on"),
        ("MOBSTER_WDA_RECOVERY", "on"), ("MOBSTER_GLIDE_ALL", "on"), ("MOBSTER_GLIDE_READ_LIST", "off"),
        ("MOBSTER_EARLY_CALL", "on"), ("MOBSTER_FLICK", "on"), ("MOBSTER_FRAME_CLOCK", "on"),
        # The first decision runs while the task contract compiles (joined before any action): time to the first
        # action 5.0 -> 2.6 s on the fake-latency harness (tests/test_speed_paths.py). No live claim until the
        # owner's paired run.
        ("MOBSTER_EARLY_DECISION", "on"))


SMART_CONFIG = SmartConfig()
SMART_PROVIDER, FAST_PROVIDER = "OpenAI", "TypeSafe"
# Smart's model for a user whose only key is Anthropic's (MOBSTER_SMART_MODEL picks any model outright).
ANTHROPIC_SMART_MODEL = "claude-sonnet-5-5"
PROVIDER_NAMES = {"openai": "OpenAI", "anthropic": "Anthropic"}
# The name a person knows the account by, in every sentence the app shows: "Claude", never "Anthropic" (MESSAGING § 11;
# "by Anthropic" stays only on Setup's provider card and in Help). PROVIDER_NAMES stays the company, for errors' labels.
ACCOUNT_NAMES = {"OpenAI": "OpenAI", "Anthropic": "Claude"}


def account_name(provider):
    """The account's name in a sentence ("Claude" for Anthropic) from a company name or a model provider id."""
    company = PROVIDER_NAMES.get(provider, provider)
    return ACCOUNT_NAMES.get(company, company)
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def model_provider(model):
    """Who serves ``model``: anthropic for claude-*, openai otherwise (the app runs Smart on these two)."""
    return "anthropic" if str(model or "").startswith("claude-") else "openai"


def provider_name(model):
    return PROVIDER_NAMES[model_provider(model)]


def anthropic_key(env=None):
    """The Anthropic key Smart uses on Claude: ANTHROPIC_API_KEY, else an Anthropic helper's (``keys.anthropic_key``)."""
    from .keys import anthropic_key as find
    key, _ = find(os.environ if env is None else env)
    return (key or "").strip() or None


def smart_model(env=None):
    """The model Smart runs on: MOBSTER_SMART_MODEL when it is set (any OpenAI or Claude model id); else the
    provider the user chose (MOBSTER_SMART_PROVIDER, saved by Setup's "Connect your AI account") while its key is
    there; else SMART_CONFIG.model with an OpenAI key, ANTHROPIC_SMART_MODEL for a user whose only key is
    Anthropic's, and SMART_CONFIG.model with no key at all. Only the model is chosen here: the loop is SMART_CONFIG's."""
    env = os.environ if env is None else env
    chosen = (env.get("MOBSTER_SMART_MODEL") or "").strip()
    if chosen and MODEL_ID.fullmatch(chosen) and not chosen.startswith("gemini-"):
        return chosen
    preferred = (env.get("MOBSTER_SMART_PROVIDER") or "").strip()
    if preferred == "anthropic" and anthropic_key(env):
        return ANTHROPIC_SMART_MODEL
    if preferred == "openai" and openai_smart_key(env):
        return SMART_CONFIG.model
    if not openai_smart_key(env) and anthropic_key(env):
        return ANTHROPIC_SMART_MODEL
    return SMART_CONFIG.model


def smart_config(env=None):
    """SMART_CONFIG on ``smart_model``: the frozen loop, only its model chosen."""
    model = smart_model(env)
    return SMART_CONFIG if model == SMART_CONFIG.model else dataclasses.replace(SMART_CONFIG, model=model)


def switches(config=SMART_CONFIG):
    return dict(config.switches)


def apply_switches(config=SMART_CONFIG, *, pin=True):
    """Set the loop's switches in this process. The app pins them (``pin``), so a stray variable in the
    user's env file cannot change Smart; the harness only fills in what is unset, so a research ablation
    (MOBSTER_EXACT_TEXT=off ...) still works there. With a clean environment both are identical."""
    for name, value in config.switches:
        if pin:
            os.environ[name] = value
        else:
            os.environ.setdefault(name, value)


def frontier_kwargs(config=SMART_CONFIG, *, max_cost_usd=None):
    """FrontierAgent's keyword arguments for ``config``. ``max_cost_usd`` is the one deliberate difference
    between the paths: the app stops at the user's per-task limit, the harness at its per-task cap."""
    # macros and contract stay None: FrontierAgent reads them from the switches set by apply_switches.
    return {"max_steps": config.max_steps, "max_seconds": config.max_seconds, "screenshots": config.screenshots,
            "settle_seconds": config.settle_seconds, "max_cost_usd": max_cost_usd}


def build_client(config=None, *, key=None):
    """The Smart model's client on the user's key: frontier.OpenAIChat, or frontier.AnthropicChat for a Claude
    model. ``config``: SMART_CONFIG on ``smart_model`` unless given."""
    from .frontier import chat_client
    config = smart_config() if config is None else config
    return chat_client(config.model, reasoning=config.reasoning, key=key or None)


def build_frontier(driver, client, *, apps, emit, max_cost_usd=None, approve=None, cancelled=None, config=SMART_CONFIG,
                   pin=True, guard=None, skills=None, output_schema=None, context_blocks=(), turn_context=None,
                   steering=None, clarify=None, step_observer=None, checkpoint=None, max_steps=None,
                   max_seconds=None, run_context=None):
    """The Smart agent: FrontierAgent with ``config``'s arguments and switches. ``cancelled`` is the app's
    Stop (the harness passes none). ``guard``: the phone guard (lockscreen.guard_for, agent_hooks.PhoneGuard);
    ``skills``: None for the default skills; ``output_schema``: the user's schema, asked for in the DONE turn
    (``summarize`` then skips its structuring call when the answer fits).

    Seam S7: ``context_blocks``, ``turn_context``, ``steering``, ``clarify``, ``step_observer``, ``checkpoint`` and
    ``run_context`` go to FrontierAgent as they are (none set: the prompts are exactly as before);
    ``max_steps`` and ``max_seconds`` (harness_api.frontier_options) replace ``config``'s when given."""
    from .frontier import FrontierAgent
    apply_switches(config, pin=pin)
    kwargs = frontier_kwargs(config, max_cost_usd=max_cost_usd)
    if max_steps is not None:
        kwargs["max_steps"] = int(max_steps)
    if max_seconds is not None:
        kwargs["max_seconds"] = max_seconds
    if skills:
        # The tool policy (harness/tools): the default skills as they are; registered tools only when available,
        # interactive-only ones only when a person watches, at most 8.
        from .harness.tools import offered
        skills = offered(skills, run_context=run_context, driver=driver, guard=guard)
    hooks = {name: value for name, value in (
        ("context_blocks", tuple(context_blocks or ())), ("turn_context", turn_context), ("steering", steering),
        ("clarify", clarify), ("step_observer", step_observer), ("checkpoint", checkpoint),
        ("run_context", run_context)) if value}
    return FrontierAgent(driver, client, apps=apps, emit=emit, approve=approve, cancelled=cancelled, guard=guard,
                         skills=skills, output_schema=output_schema, **hooks, **kwargs)


def acquire_client(key):
    """The Smart model's client for one run (seam S6.2): today, a new one on ``key`` (``build_client``). Harness
    may pool clients later under http_pool's rules; every run hands its client back through ``release_client``."""
    return build_client(key=key)


def release_client(client, ok):
    """A run is done with ``client`` (``ok``: it ended without an exception). Today: close its connection."""
    connection = getattr(client, "connection", None)
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass


# -- keys and availability -------------------------------------------------------------------------

def smart_key(env=None):
    """The key Smart uses: ANTHROPIC_API_KEY when ``smart_model`` is a Claude model, else ``openai_smart_key``."""
    env = os.environ if env is None else env
    if model_provider(smart_model(env)) == "anthropic":
        return anthropic_key(env)
    return openai_smart_key(env)


def openai_smart_key(env=None):
    """The OpenAI key Smart uses: OPENAI_API_KEY, else the helper's key when the helper is OpenAI.

    The same rule as ``keys.openai_key``, which Setup, Settings and doctor read: an OpenAI helper counts
    whether Settings saved it (MOBSTER_HELPER_PROVIDER) or its base URL is OpenAI's, so Setup never calls
    the key step done while Smart asks for a key."""
    from .keys import openai_key
    env = os.environ if env is None else env
    key = (env.get("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    helper, source = openai_key({name: value for name, value in env.items() if name != "OPENAI_API_KEY"})
    if source != "helper":
        return None
    return helper.strip() or None


def fast_ready(env=None):
    env = os.environ if env is None else env
    return bool(env.get("TYPESAFE_API_KEY"))


def default_engine(env=None):
    """The saved default, else smart, except for a user who has only a Jev key (fast)."""
    env = os.environ if env is None else env
    saved = env.get("MOBSTER_DEFAULT_ENGINE")
    if saved in ENGINES:
        return saved
    return FAST if fast_ready(env) and not smart_key(env) else SMART


NO_OPENAI_KEY = "Add your OpenAI key in Setup"
NO_JEV_KEY = "Add your Jev key in Setup"
NO_CREDIT = "Your OpenAI account is out of credit. Add credit at platform.openai.com, then try again."
NO_ANTHROPIC_CREDIT = "Your Claude account is out of credit. Add credit at platform.claude.com, then try again."
CREDIT_REASONS = (NO_CREDIT, NO_ANTHROPIC_CREDIT)
# The error codes OpenAI gives an account with no credit left; its 402 (Payment Required) says the same.
CREDIT_CODES = ("insufficient_quota", "credit_balance_exhausted")
# What Anthropic says to an account with no credit: a 400 whose message reads "Your credit balance is too low to
# access the Anthropic API", or a 402 billing_error.
ANTHROPIC_CREDIT = ("credit balance is too low", "billing_error")
PROVIDER_ERROR = re.compile(r"(OpenAI|Anthropic) HTTP (\d{3})")


def no_key_reason(managed=True, model=None):
    """Why Smart can't run without a key: the dashboard's words for the OpenAI default (Setup asks for that key),
    the env file's for a terminal user or a Claude model."""
    model = model or smart_model()
    if model_provider(model) == "anthropic":
        return f"Add ANTHROPIC_API_KEY to the agent's env file to run Smart on {model}"
    return NO_OPENAI_KEY if managed else "Add OPENAI_API_KEY or ANTHROPIC_API_KEY to the agent's env file"


def no_credit(model=None):
    return NO_ANTHROPIC_CREDIT if model_provider(model or smart_model()) == "anthropic" else NO_CREDIT


def credit_provider(text):
    """Whose account an out-of-credit error is about: Anthropic when the error names it, else OpenAI."""
    return "Anthropic" if "Anthropic" in str(text or "") else "OpenAI"


def out_of_credit(text):
    """Whether a model error (its status and body, as a run's reason carries them) says the account has no
    credit: OpenAI's insufficient_quota or credit_balance_exhausted code, Anthropic's "credit balance is too low",
    or HTTP 402 from either. A plain 429 is a rate limit, which passes on its own."""
    text = str(text or "")
    return (any(code in text for code in CREDIT_CODES + ANTHROPIC_CREDIT) or "OpenAI HTTP 402" in text
            or "Anthropic HTTP 402" in text)


class ModelReach:
    """Whether the saved key can run Smart's model, cached per key and model. Unknown counts as reachable: a
    check never blocks a task.

    Two kinds of refusal. The key's own (rejected, or its project can't use the model) comes from a
    background check (GET /v1/models/<model> at OpenAI or Anthropic, once per key and model, again after
    TTL_SECONDS) or from a run that hit it. An empty account (NO_CREDIT, NO_ANTHROPIC_CREDIT) comes from a run
    or a key test: the free model read can't see it, since both providers enforce credit only when they
    generate, so that read never clears it and the refusal has no expiry. What clears it: a key test that
    generates and passes (``Runtime.check_key``), a Smart run whose model calls went through
    (``record(key, True)``), or saving the key (``forget``). ``model`` None follows ``smart_model``."""

    TTL_SECONDS = 6 * 3600

    def __init__(self, model=None, opener=urllib.request.urlopen, enabled=False):
        self.model, self.opener, self.enabled = model, opener, enabled
        self.lock = threading.Lock()
        self.results = {}  # key digest -> (ok, reason, checked_at, credit): credit marks an empty account
        self.checking = set()

    def _model(self, model=None):
        return model or self.model or smart_model()

    @staticmethod
    def digest(key, model=""):
        return hashlib.sha256(f"{model}\0{key}".encode()).hexdigest()[:16]

    def state(self, key, model=None):
        """(available, reason) for ``key``; starts a check when none is fresh (never over an empty account)."""
        model = self._model(model)
        if not key:
            return False, no_key_reason(model=model)
        digest = self.digest(key, model)
        with self.lock:
            found = self.results.get(digest)
            stale = found is None or not found[3] and time.time() - found[2] > self.TTL_SECONDS
            start = self.enabled and stale and digest not in self.checking
            if start:
                self.checking.add(digest)
        if start:
            threading.Thread(target=self._check, args=(key, digest, model), name="mobster-model-reach",
                             daemon=True).start()
        return (True, None) if found is None else (found[0], found[1])

    def record(self, key, ok, reason=None, model=None):
        """What a run or a key test found: ``ok`` True clears any refusal, False with an out-of-credit reason is
        an empty account (it stays until one of the ways in the class docstring clears it)."""
        if key:
            with self.lock:
                self.results[self.digest(key, self._model(model))] = (ok, reason, time.time(),
                                                                      ok is False and reason in CREDIT_REASONS)

    def forget(self, key, model=None):
        """Drop what is known about ``key``: the next ``state`` counts it reachable and checks it again."""
        if key:
            with self.lock:
                self.results.pop(self.digest(key, self._model(model)), None)

    def _check(self, key, digest, model=None):
        try:
            ok, reason = self.probe(key, model)
            if ok is not None:
                with self.lock:
                    found = self.results.get(digest)
                    # The model read can't see credit: a pass says nothing about an account a run found empty.
                    if not (ok and found is not None and found[3]):
                        self.results[digest] = (ok, reason, time.time(), False)
        finally:
            with self.lock:
                self.checking.discard(digest)

    def probe(self, key, model=None):
        """(True, None), (False, why) or (None, None) when the answer is unknown (offline, a 5xx)."""
        model = self._model(model)
        if model_provider(model) == "anthropic":
            from .frontier import ANTHROPIC_VERSION
            request = urllib.request.Request(f"https://api.anthropic.com/v1/models/{model}", headers={
                "x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "User-Agent": "Mobster"})
        else:
            request = urllib.request.Request(f"https://api.openai.com/v1/models/{model}", headers={
                "Authorization": f"Bearer {key}", "User-Agent": "Mobster"})
        try:
            with self.opener(request, timeout=15) as response:
                response.read(65536)
            return True, None
        except urllib.error.HTTPError as error:
            return unreachable(error.code, model)
        except (urllib.error.URLError, TimeoutError, OSError):
            return None, None


def rejected_key(provider):
    """Why Smart can't run on a key its provider refused, in the app's words (no env names: the app saves the key)."""
    return f"{account_name(provider)} didn't accept your key. Check it in Settings › AI account."


def unreachable(status, model=None):
    """(ok, reason) for a model provider's HTTP status, or (None, None) when it says nothing about the key."""
    model = model or smart_model()
    if status in (401, 403):
        return False, rejected_key(provider_name(model))
    if status == 404:
        return False, f"Your {account_name(provider_name(model))} key can't use {model}."
    return None, None


def model_failure(reason, model=None):
    """(False, why) when a run's error says Smart can't run on this key (rejected, the model out of its
    project's reach, or the account out of credit), else (None, None). A plain 429 is a rate limit and says
    nothing about the key."""
    text = str(reason or "")
    found = PROVIDER_ERROR.search(text)
    if not found:
        return None, None
    if out_of_credit(text):
        return False, NO_ANTHROPIC_CREDIT if found.group(1) == "Anthropic" else NO_CREDIT
    if found.group(2) == "404" and "model" not in text.casefold():
        return None, None
    return unreachable(int(found.group(2)), model or smart_model())


NO_OPENAI_REACH = "Mobster couldn't reach OpenAI. Check your internet connection, then try again."
NO_ANTHROPIC_REACH = "Mobster couldn't reach Claude. Check your internet connection, then try again."


def plain_error(reason, model_failed=False, model=None):
    """A run error in words a person can act on, or None. ``model_failed``: the task's last model call
    failed (engines.meter), so a timeout or a network error was the model provider's, not the phone's.
    ``model``: the run's model (its provider names the service), else ``smart_model``."""
    text = str(reason or "")
    model = model or smart_model()
    found = PROVIDER_ERROR.search(text)
    name = found.group(1) if found else provider_name(model)
    status = found.group(2) if found else None
    if out_of_credit(text):
        return NO_ANTHROPIC_CREDIT if name == "Anthropic" else NO_CREDIT
    if status in ("401", "403"):
        return rejected_key(name)
    if status == "404":
        return f"Your {account_name(name)} key can't use {model}."
    if status == "429":
        return f"{account_name(name)} is rate limiting your key. Wait a minute, then try again."
    if status and status.startswith("5"):
        return f"{account_name(name)} had a problem answering. Try again in a moment."
    if "Anthropic declined the request" in text:
        return "Claude declined this request. Try rewording it, or run it on another model."
    if model_failed:
        return NO_ANTHROPIC_REACH if name == "Anthropic" else NO_OPENAI_REACH
    if re.search(r"TimeoutError|timed out|ConnectionRefused|ConnectionReset|RemoteDisconnected", text):
        return "Your iPhone stopped answering. Check it is unlocked and connected, then try again."
    if re.match(r"[A-Z]\w*(Error|Exception)\b", text):
        # An internal error's name and message mean nothing to the user; the steps show how far it got.
        return "Mobster couldn't read your iPhone's screen partway through. Try the task again."
    return None


# -- costs -----------------------------------------------------------------------------------------

# What a Smart task costs, fitted on recorded runs of this loop on gpt-5.6-sol at list prices (27 Sep): the 152
# iOSWorld runs of sol-a/b, ab6 and ab7 (research/iosworld-runs) and 32 app runs on simulator 4, split by task,
# 70% to fit (app runs weighted 3x: they are the product) and 30% held out.
# log(cost) = a + b·log(words in the request) + c·(apps it names, up to 6). Held out, the estimate is within 2x
# of the actual cost on 46 of 56 runs (82%; the per-turn model it replaces managed 57% on the iOSWorld part),
# median actual/estimate 0.78; on ab7-b1/b2 30 of 32 (was 11). On app runs it errs high: median 0.67, within
# 2x on 16 of 32 (short tasks cost 1.7-14 cents for the same request). tests/smart_costs.json holds the runs
# and test_engines pins the held-out rate.
SMART_FIT = (-4.48, 0.615, 0.163)
SMART_MAX_APPS = 6
# The range around the estimate: halved to doubled holds about 80% of the recorded runs (10th and 90th
# percentiles of actual/estimate: 0.50 and 1.94).
SMART_SPREAD = 2.0
SMART_STRUCTURE_USD = 0.004  # the answer put into a structure (the user's schema, or an automatic format): one small call
# The fit is sol's; another model's tasks cost this multiple of sol's on the same tasks, both at list prices (sol at
# $4/$20). Paired iOSWorld A/B, 16 never-run tasks, 28 Sep: Sonnet 5.5 $0.197 against sol's $0.291 a task (32 runs
# and 21; 95% CI of the difference -$0.143 to -$0.044), Opus 5.5 $0.297 (16 runs).
SMART_COST_RATIO = {"claude-sonnet-5-5": 0.68, "claude-opus-5-5": 1.02}


def named_apps(goal, app_names=()):
    """How many of ``app_names`` the request names (whole words, any case)."""
    folded = " ".join(str(goal or "").split()).casefold()
    return sum(1 for name in set(app_names) if len(name) > 2 and re.search(rf"\b{re.escape(name.casefold())}\b", folded))


def smart_point(goal, app_names=()):
    """The likeliest cost (USD) of a Smart task for ``goal``: longer requests that name more apps cost more."""
    words = max(1, len(str(goal or "").split()))
    a, b, c = SMART_FIT
    return math.exp(a + b * math.log(words) + c * min(SMART_MAX_APPS, named_apps(goal, app_names)))


def smart_estimate(goal, app_names=(), *, available=True, reason=None, structured=False, model=None):
    model = model or smart_model()
    point = smart_point(goal, app_names) * SMART_COST_RATIO.get(model, 1.0) + (SMART_STRUCTURE_USD if structured else 0)
    return {"available": available, "reason": reason, "model": model, "provider": provider_name(model),
            "lowUsd": round(point / SMART_SPREAD, 4), "highUsd": round(point * SMART_SPREAD, 4)}


def fast_estimate(planning, *, available=True, reason=None, model="jev-latest"):
    """Fast's range from costs.planning_estimate (Jev and the helper at published rates)."""
    low, high = planning.get("estimated_min_usd"), planning.get("estimated_max_usd")
    return {"available": available, "reason": reason, "model": model, "provider": FAST_PROVIDER,
            "lowUsd": round(low, 6) if low is not None else 0.0, "highUsd": round(high, 6) if high is not None else 0.0}


def midpoint(estimate):
    """The estimate's single figure: the geometric mean of its range."""
    low, high = estimate["lowUsd"], estimate["highUsd"]
    return round(math.sqrt(low * high), 4) if low > 0 and high > 0 else high


def meter(client, emit, *, provider=None):
    """Wrap ``client.complete`` so every model call emits inference_started / inference_finished with its
    list-price cost (the usage ledger and the spend limits read those). The loop's own accounting
    (client.usage, frontier.cost_usd) is untouched. ``client.model_failed`` says whether the last call failed.
    A client that hedges (frontier.AnthropicChat) reports a losing twin's tokens through ``on_hedge_usage``
    when it finishes: it is billed, so it is recorded as a call of its own (purpose "hedge")."""
    from .costs import NANODOLLARS
    from .frontier import cost_usd
    inner = client.complete
    client.model_failed = False
    provider = provider or getattr(client, "provider", None) or "openai"

    def complete(messages, schema, **kwargs):
        call_id = uuid.uuid4().hex[:16]
        model = getattr(client, "model", "") or "unknown"
        purpose = "planning" if "items" in (schema.get("properties") or {}) and "actions" not in (
            schema.get("properties") or {}) else "decision"
        meta = {"provider": provider, "call_id": call_id, "model": model, "purpose": purpose}
        emit({"event": "inference_started", **meta})
        started = time.monotonic()
        try:
            out, usage = inner(messages, schema, **kwargs)
        except Exception as error:
            client.model_failed = True
            emit({"event": "inference_finished", **meta, "success": False,
                  "latency_ms": round((time.monotonic() - started) * 1000, 2), "usage": {},
                  "error_type": type(error).__name__, "estimated_usd": None, "cost_nanodollars": None,
                  "cost_basis": None})
            raise
        if purpose != "planning":
            # The task contract compiles alongside the first decision (the early decision): its success, which
            # may land later, never hides that decision's failure.
            client.model_failed = False
        usage = usage or {}
        finished(meta, usage, started)
        return out, usage

    def finished(meta, usage, started):
        prompt, completion = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        usd = cost_usd(meta["model"], usage)
        emit({"event": "inference_finished", **meta, "success": True,
              "latency_ms": round((time.monotonic() - started) * 1000, 2),
              "usage": {"input_tokens": prompt, "output_tokens": completion, "total_tokens": prompt + completion,
                        "cached_input_tokens": int(usage.get("cached_tokens", 0))},
              "estimated_usd": usd, "cost_nanodollars": None if usd is None else round(usd * NANODOLLARS),
              "cost_basis": None if usd is None else "published_rate"})

    def hedged(usage):
        meta = {"provider": provider, "call_id": uuid.uuid4().hex[:16],
                "model": getattr(client, "model", "") or "unknown", "purpose": "hedge"}
        emit({"event": "inference_started", **meta})
        finished(meta, usage or {}, time.monotonic())

    client.complete = complete
    if hasattr(client, "on_hedge_usage"):
        client.on_hedge_usage = hedged
    return client


# -- the dashboard's events --------------------------------------------------------------------------

# FrontierAgent events kept out of the run's history: the full prompt and screenshot of every turn is
# for offline replay (bench/replay.py), 20-60 KB each; the loop's own result is replaced by the server's.
DROPPED = frozenset({"frontier_prompt", "result"})
TYPING = frozenset({"TYPE", "TYPE_SUBMIT", "SET_TEXT"})


# SF Symbol names iOS gives icon-only buttons as their label, and what the button does.
SYMBOLS = {"circle": "the selection circle", "ellipsis": "More", "ellipsis.circle": "More", "checkmark": "Done",
           "trash": "Delete", "trash.fill": "Delete", "square.and.pencil": "Compose"}
# A row's state that iOS appends to its label ("Buy oat milk, Incomplete" in Reminders).
ROW_STATE = re.compile(r",\s*(?:Incomplete|Completed)$")


def readable(label):
    """A control's label for people: an identifier ("chat_compose_field", "searchButton") in words, an SF
    Symbol name ("ellipsis") as what it does, a row without its state ("Buy oat milk, Incomplete")."""
    label = ROW_STATE.sub("", " ".join(str(label or "").split()))
    if label in SYMBOLS:
        return SYMBOLS[label]
    if label and " " not in label and re.fullmatch(r"[A-Za-z0-9_.-]+", label) and re.search(r"[_.]|[a-z][A-Z]", label):
        label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", label).replace("_", " ").replace(".", " ").replace("-", " ")
        label = " ".join(label.split()).lower()
    return label


def shorten(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def in_quotes(text, limit=40):
    return f"“{shorten(text, limit)}”"


def written_kind(label):
    """What a composer holds, by its label: the comment, the reply, the post or (else) the message."""
    folded = readable(label).casefold()
    for word, kind in (("comment", "comment"), ("reply", "reply"), ("caption", "post"), ("tweet", "post"),
                       ("post", "post"), ("on your mind", "post")):
        if word in folded:
            return kind
    return "message"


def step_text(op, label, apps=None, *, text=None, field=None, where=None, again=False):
    """One step in plain words: "Tapped Send", "Opened Messages", "Typed “buy oat milk” in Title",
    "Wrote the message", "Searched for “Zara”", "Scrolled to Zara Okonkwo", "Read the Reminders list".
    ``text`` is what was typed (or scrolled to), ``field`` the kind of field typed into (search, composer or
    field), ``where`` the screen or app a list was read in, ``again`` a composer written before."""
    label = readable(label) if op not in ("LAUNCH_APP",) else " ".join(str(label or "").split())
    short = shorten(label, 60)
    if op == "LAUNCH_APP":
        return f"Opened {(apps or {}).get(label, label) or 'an app'}"
    if op == "TAP_XY":
        return f"Tapped {re.sub(r' at [0-9]+,[0-9]+$', '', short) or 'the screen'}"
    if op in TYPING and text is not None:
        submit = " and pressed Return" if op == "TYPE_SUBMIT" else ""
        if op == "SET_TEXT" and not str(text).strip():
            return f"Cleared the {written_kind(label)}" if field == "composer" else f"Cleared {short}" if short \
                else "Cleared the field"
        if field == "search":
            return f"Searched for {in_quotes(text)}"
        if field == "composer":
            return f"{'Rewrote' if again else 'Wrote'} the {written_kind(label)}{submit}"
        place = f" in {short}" if short and len(short) <= 30 and short.casefold() != " ".join(str(text).split()).casefold() else ""
        return f"Typed {in_quotes(text)}{place}{submit}"
    if op == "SCROLL_TO" and text:
        return f"Scrolled to {shorten(text, 60)}"
    if op == "READ_LIST" and where:
        return f"Read the {shorten(where, 40)} list"
    return {
        "TAP": f"Tapped {short}" if short else "Tapped the screen",
        "LONG_PRESS": f"Pressed and held {short}",
        "TYPE": f"Typed in {short}" if short else "Typed text",
        "TYPE_SUBMIT": f"Typed in {short} and pressed Return" if short else "Typed text and pressed Return",
        "SET_TEXT": f"Entered text in {short}" if short else "Entered text",
        "SUBMIT": "Pressed Return",
        "SWIPE_UP": "Scrolled down", "SWIPE_DOWN": "Scrolled up",
        "SWIPE_LEFT": f"Swiped left on {short}" if short else "Swiped left",
        "SWIPE_RIGHT": f"Swiped right on {short}" if short else "Swiped right",
        "DISMISS": "Closed a pop-up", "HOME": "Went to the Home Screen",
        "READ_LIST": "Read the whole list", "SCROLL_TO": "Scrolled to the item",
        "ASK_USER": "Asked you a question",
    }.get(op, op.replace("_", " ").capitalize())


def item_state(contract, item):
    """open | done | unproven, as the dashboard shows a contract item."""
    if item.kind == "FORBID":
        return "done"
    if item.unproven and not contract.proof(item)[0]:
        return "unproven"
    if item.kind == "REPORT":
        return "done" if item.status in ("found", "missing") else "open"
    if item.kind == "COMMIT":
        return "done" if item.status == "closed" or contract.proof(item)[0] else "open"
    return "done" if contract.proof(item)[0] else "open"


# How a checklist item reads to a person, by act: (the verb and object, whether it names the recipient).
ITEM_ACTS = {
    "send_message": "Send the message", "reply": "Send the reply", "post": "Post it", "comment": "Post the comment",
    "react": "React", "pay": "Pay", "request_money": "Request money", "transfer": "Make the transfer",
    "order": "Place the order", "book": "Make the booking", "follow": "Follow", "unfollow": "Unfollow",
    "share": "Share", "call": "Call", "delete": "Delete", "cancel": "Cancel", "save": "Save", "archive": "Archive",
    "install": "Install"}


def item_text(item):
    """A checklist item in plain words ("Send the message to Zara Okonkwo in QuickChat"), for any list a person
    sees; ``label`` stays the contract's own wording, for Developer details."""
    from . import contract as K
    what = " ".join(str(item.what or "").split())
    where = f" in {item.app}" if item.app and not what.casefold().endswith(f" in {item.app}".casefold()) else ""
    if item.kind == "COMMIT":
        who = K.recipient(item)
        verb = ITEM_ACTS.get(item.act)
        if verb is None:
            text = what[:1].upper() + what[1:]
        elif item.act in ("send_message", "reply", "transfer"):
            text = verb + (f" to {who}" if who else "")
        elif item.act in ("pay", "follow", "unfollow", "call"):
            text = f"{verb} {who or what}"
        elif item.act == "request_money":
            text = verb + (f" from {who}" if who else "")
        elif item.act in ("order", "book", "post", "comment", "react"):
            text = verb if item.act != "react" else f"React to {what}"
        else:
            text = f"{verb} {what}" if what else verb
        if item.condition:
            text += f" if {item.condition}"
        return text + where
    if item.kind == "WRITE":
        return (f"Enter {in_quotes(item.payload, 60)}" if item.payload else f"Write {what}") + where
    if item.kind == "READ":
        return f"Look at {what}" + where
    if item.kind == "REPORT":
        return f"Find {what}" + where
    verb = ITEM_ACTS.get(item.act)
    return f"Don't {verb.split()[0].lower()} {what}" if verb else f"Don't {what}"


class SmartEvents:
    """FrontierAgent's ``emit``: passes its trace through (less the replay prompts) and adds the contract
    events of docs/engine-contract.md: ``plan`` once before the first action, ``contract_item`` as items
    change, ``receipt`` when the screen proves one, ``step`` per action in plain words with the screen as a
    ``_frame`` (the server stores it and substitutes ``frameId``), and ``cost`` after each model call.

    Typing the same field again right after is one step (the text it ends with); a typing step waits for
    the next action or model call to know. Proof is the latest receipt per item: a message quotes the exact
    text sent, a write is proven only when its text is read back outside the field it was typed in (a draft
    in a compose box proves nothing), a value quotes its row as shown ("iOS Version 26.4").

    Frames are the live stream's newest JPEG (frontier.video_frame): free, never a screenshot request."""

    def __init__(self, emit, driver=None, apps=None, frame=None):
        self.out, self.driver, self.apps = emit, driver, dict(apps or {})
        self.agent = None
        self.frame = frame or self._video_frame
        self.steps = 0
        self.planned = False
        self.states = {}
        self.receipts_seen = 0
        self.reports = {}
        self.proof = {}          # itemId -> {itemId, kind, quote, app, screen, frameId}: the latest receipt
        self.write_keys = {}     # WRITE itemId -> the text its receipt quotes (folded)
        self.stale = set()       # WRITE items whose quoted text was typed over and not read back again
        self.typed_at = {}       # index in contract.typed -> the first contract.screens index after it
        self.held = None         # a typing step waiting to see whether the same field is typed again
        self.ops = []            # the current model call's chain of operations
        self.written = set()     # composers written in (a second write "Rewrote the message")
        self.declined = set()    # texts the user declined at an approval (folded): never proof
        self.cost_usd = None
        self.lock = threading.Lock()

    def _video_frame(self):
        from .frontier import video_frame
        return video_frame(self.driver) if self.driver is not None else None

    def _grab(self):
        try:
            data = self.frame()
        except Exception:
            data = None
        return bytes(data) if isinstance(data, (bytes, bytearray)) and data else None

    def _with_frame(self, event, data=None):
        data = data if data is not None else self._grab()
        if data:
            event["_frame"] = data
        return event

    def _private(self):
        """Where the screen the last action left shows text fields and messages (normalized frames, never their
        text), so a saved clip can blur them (run_gif); None when unknown."""
        screen = getattr(self.agent, "last_screen", None)
        if screen is None:
            return None
        try:
            from .run_gif import private_rects
            return private_rects(screen.elements, getattr(screen, "bundle_id", None))
        except Exception:
            return None

    @property
    def contract(self):
        return getattr(self.agent, "_contract", None)

    def __call__(self, event):
        kind = event.get("event")
        with self.lock:
            if kind not in DROPPED:
                self.out(event)
            if kind in ("frontier_contract", "frontier_checklist") and not self.planned:
                self._plan()
            elif kind == "frontier_decision":
                self.ops = list(event.get("ops") or ())
                held = self.held
                if held is not None and not (event.get("operation") in TYPING
                                             and event.get("target_label") == held["label"]):
                    self._flush()
                self._cost()
            elif kind == "frontier_action":
                self._action(event)
            if kind in ("frontier_decision", "frontier_action", "frontier_contract_update", "frontier_failed",
                        "frontier_chunk_stop"):
                self._receipts(event.get("step"))

    def inference(self, event):
        """Where ``meter`` sends its events (the same stream)."""
        with self.lock:
            self.out(event)

    def approving(self, approve):
        """``approve`` for FrontierAgent: the step list is up to date (a held typing step shown) before the
        user is asked."""
        def ask(request):
            from . import contract as K
            with self.lock:
                self._flush()
            answer = approve(request)
            if answer != "approved" and request.get("text"):
                with self.lock:
                    self.declined.add(K.plain(request["text"]))
            return answer
        return ask

    # -- steps

    def _step(self, text, step, data=None, private=None):
        self.steps += 1
        event = self._with_frame({"event": "step", "n": self.steps, "step": step, "text": text}, data)
        if "_frame" in event:
            private = private if private is not None else self._private()
            if private is not None:
                event["redact"] = private
        self.out(event)

    def _action(self, event):
        op, label = event.get("operation"), event.get("target_label")
        contract = self.contract
        if op in TYPING and contract is not None and contract.typed:
            self.typed_at.setdefault(len(contract.typed) - 1, max(0, len(contract.screens) - 1))
        if op in TYPING:
            if self.held is not None and self.held["label"] == label:
                self.held.update(op=op, text=event.get("text"), step=event.get("step"), frame=self._grab(),
                                 private=self._private(), field=event.get("field") or self.held["field"])
            else:
                self._flush()
                self.held = {"op": op, "label": label, "text": event.get("text"), "field": event.get("field"),
                             "step": event.get("step"), "frame": self._grab(), "private": self._private()}
            index = event.get("index")
            following = self.ops[index + 1] if isinstance(index, int) and index + 1 < len(self.ops) else None
            if following is not None and following not in TYPING:
                self._flush()  # the chain goes on to something else: nothing to merge
            return
        self._flush()
        where = None
        if op == "READ_LIST":
            shown = contract.shown[-1] if contract is not None and contract.shown else None
            where = (shown[2] if shown and shown[2] else None) or event.get("app_name")
        self._step(step_text(op, label, self.apps, text=event.get("text"), where=where), event.get("step"))

    def _flush(self):
        held, self.held = self.held, None
        if held is None:
            return
        again = held["field"] == "composer" and held["label"] in self.written
        if held["field"] == "composer":
            self.written.add(held["label"])
        self._step(step_text(held["op"], held["label"], self.apps, text=held["text"], field=held["field"],
                             again=again), held["step"], held["frame"], held.get("private"))

    # -- the plan, costs and checklist

    def _plan(self):
        from . import contract as proof
        self.planned = True
        contract = self.contract
        apps = []
        for item in (contract.items if contract else ()):
            if item.app and item.app not in apps:
                apps.append(item.app)
        self.out({"event": "plan", "line": proof.plan_line(contract), "apps": apps,
                  "commits": proof.plan_commits(contract)})
        for item in (contract.items if contract else ()):
            state = item_state(contract, item)
            self.states[item.id] = state
            self.out({"event": "contract_item", "id": item.id, "kind": item.kind.lower(), "label": item.label(),
                      "text": item_text(item), "state": state})

    def _cost(self):
        from .frontier import cost_usd
        client = getattr(self.agent, "client", None)
        usage = getattr(client, "usage", None)
        if not usage:
            return
        usd = cost_usd(getattr(client, "model", ""), usage)
        self.cost_usd = usd
        self.out({"event": "cost", "usd": usd, "calls": usage.get("calls", 0)})

    # -- proof

    def _receipts(self, step):
        from . import contract as K
        contract = self.contract
        if contract is None:
            return
        by_id = {item.id: item for item in contract.items}
        fresh = contract.events[self.receipts_seen:]
        self.receipts_seen = len(contract.events)
        for entry in fresh:
            item = by_id.get(entry.get("item"))
            if entry.get("event") != "receipt" or item is None:
                continue
            self._receipt(item, self._commit_quote(contract, item, entry), entry.get("step", step),
                          screen=entry.get("screen"))
        for item in contract.items:
            if item.kind == "REPORT" and item.status == "found" and item.value and self.reports.get(item.id) != item.value:
                self.reports[item.id] = item.value
                row, screen = self._report_row(contract, item)
                label = None
                if row and K.plain(row).endswith(K.plain(item.value)) and len(row) > len(item.value):
                    label = row[:len(row) - len(item.value)].strip(" :,-–—")
                    # "iOS Version, 26.4" is how VoiceOver reads the row; the screen shows "iOS Version  26.4"
                    row = f"{label} {row[len(row) - len(item.value):]}" if label else row
                self._flush()
                taken = item.value_step if item.value_step is not None else step
                data = self._grab()
                self._step(f"Read {shorten(label or item.what, 40)}: {shorten(item.value, 60)}", taken, data)
                self._receipt(item, row or item.value, taken, screen=screen, data=data)
        self._writes(contract)
        self._states()

    def _commit_quote(self, contract, item, entry):
        """A commit's proof as the user knows it: the exact message sent, the confirmation row as shown, or
        (a save) the typed text the screen now shows."""
        from . import contract as K
        payload = " ".join(str(entry.get("payload") or "").split())
        if item.act in K.MESSAGE_ACTS and payload:
            return payload
        if entry.get("shown"):
            return entry["shown"]
        receipt = str(entry.get("receipt") or "")
        if receipt == "screen changed":
            last = contract.shown[-1][3] if contract.shown else {}
            for typed in reversed(contract.typed):
                if typed["search"] or typed.get("picker") or not contract._app_ok(item, typed["app"]):
                    continue
                key = K.plain(next((line for line in typed["text"].split("\n") if line.strip()), ""))[:40]
                if key and any(K._shows(key, text) for text in last):
                    return " ".join(typed["text"].split())
            return item.payload or item.what or (item.receipts[-1]["label"] if item.receipts else "")
        return re.sub(r"^(sent|shown): ", "", receipt)

    @staticmethod
    def _report_row(contract, item):
        """(the row that shows a found value, as shown, and its screen's title), latest first, before the
        value was recorded; (None, None) when no short row holds it (a value worked out from several)."""
        from . import contract as K
        value = K.plain(item.value)
        if not value:
            return None, None
        # The value as a whole word: "on" is not in "chevron.forward" (B6, bench/state.bluetooth), and an SF
        # Symbol's name is never a quote.
        whole = re.compile(r"(?<![\w])" + re.escape(value) + r"(?![\w])")
        for (step, _, _, _), (_, _, title, originals) in reversed(list(zip(contract.screens, contract.shown))):
            if item.value_step is not None and step > item.value_step:
                continue
            for text, original in originals.items():
                if whole.search(text) and len(original) <= 80 and not K.SF_SYMBOL.fullmatch(original.strip()):
                    return original, title
        return None, None

    def _writes(self, contract):
        """WRITE receipts: the latest text typed for the item, once a screen after the typing shows it
        outside the field it went into. A draft in a compose box, or text typed over, proves nothing."""
        from . import contract as K
        for item in contract.items:
            if item.kind != "WRITE":
                continue
            typed = [(index, t) for index, t in enumerate(contract.typed) if not t["search"] and not t.get("picker")
                     and contract._app_ok(item, t["app"]) and (not item.payload or K._overlaps(item.payload, t["text"]))]
            if not typed:
                continue
            index, latest = typed[-1]
            key = K.plain(next((line for line in latest["text"].split("\n") if line.strip()), ""))[:40]
            if not key:
                continue
            if self.write_keys.get(item.id) == key:
                self.stale.discard(item.id)
                continue
            found = self._read_back(contract, item, latest, key, self.typed_at.get(index))
            if found is None:
                if item.id in self.write_keys:
                    self.stale.add(item.id)
                continue
            quote, screen = found
            self.write_keys[item.id] = key
            self.stale.discard(item.id)
            self._receipt(item, quote, latest["step"], screen=screen)

    @staticmethod
    def _read_back(contract, item, typed, key, since):
        """(quote, title) of the latest screen after ``typed`` that shows ``key`` where it is saved or sent,
        in more rows than the screen before the typing did (the same text sent or saved before is not this
        one): a row that is not a field, or with the keyboard down a field other than the one typed in (a
        list's editable rows), never a compose box. None when no screen does."""
        from . import contract as K
        field = K.plain(readable(typed.get("field") or ""))
        composer = bool(K.COMPOSER.search(typed.get("field") or "") or K.COMPOSER.search(field))

        def showing(rows, keyboard):
            return [text for text, editable, _ in rows if key in text
                    and not (editable and (keyboard or composer or field and text.startswith(field + " ")))]

        pairs = list(zip(contract.screens, contract.shown))
        first = since if since is not None else 0
        before = pairs[first - 1] if first > 0 else None
        baseline = len(showing(before[0][2], before[0][3])) if before and before[0][1] == (
            pairs[first][0][1] if first < len(pairs) else None) else 0
        for position in range(len(pairs) - 1, first - 1, -1):
            (step, app, rows, keyboard), (_, _, title, originals) = pairs[position]
            if since is None and step < typed["step"] or not contract._app_ok(item, app):
                continue
            shown = showing(rows, keyboard)
            if len(shown) <= baseline:
                continue
            original = originals.get(shown[-1]) or typed["text"]
            return (original if K.plain(original) == K.plain(typed["text"]) else " ".join(typed["text"].split())), title
        return None

    def _receipt(self, item, quote, step, *, screen=None, data=None):
        quote = shorten(quote, 300)
        if not quote:
            return
        event = self._with_frame({"event": "receipt", "itemId": item.id, "kind": item.kind.lower(), "quote": quote,
                                  "app": item.app or None, "screen": screen or None, "step": step}, data)
        self.out(event)  # the server swaps the event's ``_frame`` for a ``frameId``
        self.proof.pop(item.id, None)  # the latest receipt for an item replaces the one before
        self.proof[item.id] = {"itemId": item.id, "kind": item.kind, "quote": quote, "app": event["app"],
                               "screen": event["screen"], "frameId": event.get("frameId")}

    def _states(self):
        contract = self.contract
        for item in (contract.items if contract else ()):
            state = item_state(contract, item)
            if self.states.get(item.id) != state:
                self.states[item.id] = state
                self.out({"event": "contract_item", "id": item.id, "kind": item.kind.lower(), "label": item.label(),
                          "text": item_text(item), "state": state})

    def final_proof(self):
        """The result's proof: one entry per item, the latest; a write that a sent message or a save in the
        same app already quotes, and a write typed over since, are left out."""
        from . import contract as K
        entries = [e for e in self.proof.values() if e["quote"]]
        commits = [e for e in entries if e["kind"] == "COMMIT"]
        kept, quotes = [], set()
        for entry in entries:
            if K.plain(entry["quote"]) in self.declined and entry["kind"] != "COMMIT":
                continue  # a draft the user turned down, however it came to be on screen
            if entry["kind"] == "WRITE" and (entry["itemId"] in self.stale or any(
                    (not c["app"] or not entry["app"] or K.plain(c["app"]) == K.plain(entry["app"]))
                    and K._overlaps(c["quote"], entry["quote"]) for c in commits)):
                continue
            folded = K.plain(entry["quote"])
            if folded in quotes:
                continue
            quotes.add(folded)
            kept.append({"quote": entry["quote"], "app": entry["app"], "screen": entry["screen"],
                         "frameId": entry.get("frameId")})
        return kept[-12:]

    def finish(self):
        """Final item states (the DONE gate may have marked some unproven) and the last held step."""
        with self.lock:
            self._receipts(None)
            self._flush()


# -- the result --------------------------------------------------------------------------------------

STATUS = {"completed": "completed", "blocked": "blocked", "max_steps": "max_steps", "timeout": "timeout",
          "budget": "spend_cap", "error": "error", "approval_denied": "approval_denied",
          "approval_timeout": "approval_timeout", "stopped": "stopped"}


# Past-tense verbs "I" can go before: "I found 3 reminders" reads as "Found 3 reminders", like the steps.
# Anything else keeps it: "I have added…", "I don't see…" or "I see…" would read as a broken sentence or
# an instruction without it, and "I 95 is closed" or "I miss you" are the phone's own words.
DROPS_SUBJECT = frozenset({"found", "sent", "made", "set", "put", "wrote", "took", "got", "saw", "ran", "read",
                           "left", "opened", "closed", "couldn't"})
# Words ending in -ed that are not past tense ("I need…").
NOT_PAST = frozenset({"need", "feed", "bleed", "breed", "speed", "exceed", "proceed", "succeed", "heed", "seed",
                      "weed", "bed", "wed", "red", "shed", "embed"})


def without_first_person(answer):
    """"I added the reminder…" → "Added the reminder…", for the one-line answer only: the run's answer and
    data stay as the model wrote them. "I" goes only before a past-tense verb (DROPS_SUBJECT, or a word
    ending in -ed); "I have added…" or "I did not find…" keep it. ``summarize`` applies it only to a
    sentence about what the agent did (``tells_what_it_did``)."""
    found = re.match(r"I ([A-Za-z]+(?:['’][A-Za-z]+)?)\b", answer or "")
    if not found:
        return answer
    verb = found.group(1).casefold().replace("’", "'")
    if verb not in DROPS_SUBJECT and not (verb.endswith("ed") and verb not in NOT_PAST):
        return answer
    return answer[2].upper() + answer[3:]


def tells_what_it_did(sentence, contract, proof=()):
    """Whether the answer's first sentence is the agent saying what it did, the only kind the one-line answer
    loses its "I" in: the task had something to do (a COMMIT or WRITE item), and no proof quote holds the
    sentence. A task that only reads (a note, a message, a list) answers in the phone's own words, where
    "I left the keys under the mat." is the note speaking, not Mobster. Without a contract nobody knows."""
    from . import contract as K
    if contract is None or not any(item.kind in ("COMMIT", "WRITE") for item in contract.items):
        return False
    said = K.plain(sentence).rstrip(".!?…")
    return not any(said and said in K.plain(entry.get("quote")) for entry in proof or ())


SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
# A list's own number ("1." at a line's start, or after "today: "): it ends no sentence. "at 10." and "7:30." do.
LIST_NUMBER = re.compile(r"(?:^|\n[ \t]*|[:;][ \t]+)\d{1,2}\.$")


def sentence_end(text):
    """Where the first sentence of ``text`` ends (just past its mark), or None."""
    for mark in SENTENCE_END.finditer(text):
        if not LIST_NUMBER.search(text[:mark.end()]):
            return mark.end()
    return None


def first_sentence(text, limit=300):
    """The answer's headline, for the one-line answer: its first sentence, or its first line when that line
    has no sentence end and more lines follow ("Here are your reminders for today:" over a list). A sentence
    under 12 characters ("Yes.") is no headline: the whole text stands in. The dashboard splits the full
    answer the same way (result-status.ts › answerText)."""
    text = str(text or "").strip()
    if not text:
        return None
    line, end = text.find("\n"), sentence_end(text)
    if line != -1 and (end is None or end > line):
        sentence = text[:line]
    elif end is not None and len(" ".join(text[:end].split())) >= 12:
        sentence = text[:end]
    else:
        sentence = text
    sentence = " ".join(sentence.split())
    return sentence if len(sentence) <= limit else sentence[:limit - 1].rstrip() + "…"


def outcome_of(status, contract, answer):
    """done | check | couldnt_finish | stopped | declined."""
    if status == "approval_denied":
        return "declined"
    if status in ("stopped", "approval_timeout"):
        return "stopped"
    if status == "completed":
        unproven = [i for i in (contract.unproven() if contract else ()) if i.kind in ("COMMIT", "WRITE", "REPORT")]
        return "check" if unproven or contract is None else "done"
    if status in ("max_steps", "timeout", "spend_cap", "blocked", "error") and answer and status != "error":
        return "check" if status != "blocked" else "couldnt_finish"
    return "couldnt_finish"


def reason_of(status, outcome, raw_reason, contract, model_failed=False, model=None):
    if outcome == "done":
        return None
    if outcome == "check":
        unproven = [i.what for i in (contract.unproven() if contract else ()) if i.kind in ("COMMIT", "WRITE")]
        if unproven:
            return "The screen never showed this finished: " + "; ".join(unproven[:3]) + "."
        if status == "spend_cap":
            return "The task reached its cost limit. This is what it found before stopping."
        if status in ("max_steps", "timeout"):
            return "The task ran out of steps. This is what it found before stopping."
        return "Mobster couldn't prove every part of this on screen. Check the answer."
    plain = plain_error(raw_reason, model_failed, model)
    if plain:
        return plain
    if status == "spend_cap":
        return "The task reached its cost limit before finishing."
    if status == "max_steps":
        return f"The task used all {SMART_CONFIG.max_steps} steps before finishing."
    if status == "timeout":
        return "The task ran out of time before finishing."
    if status == "blocked":
        return str(raw_reason or "Mobster couldn't do this on your iPhone.")[:300]
    return str(raw_reason or "The task stopped before finishing.")[:300]


def _findings(request, answer, notes, instruction):
    """The messages of a call that puts the loop's findings (its answer and notes) into a structure."""
    return [{"role": "system", "content": instruction + " Use only values stated in its answer or notes, exactly as "
             "written there; use null where a value was not found."},
            {"role": "user", "content": [{"type": "text", "text": f"Request: {request}\n\nAnswer: {answer}\n\n"
                                          "Notes:\n" + ("\n".join(f"- {n}" for n in notes or ()) or "(none)")}]}]


def _loose_call(client, messages, schema, timeout):
    """One call through the client's ``complete`` (so ``meter`` records it), non-strict and without reasoning: a
    user's schema is not always strict-compatible (strict needs every property required). Claude
    (frontier.AnthropicChat) takes any schema once ``anthropic_schema`` has closed it: only its reasoning drops."""
    if getattr(client, "provider", None) == "anthropic":
        saved = client.reasoning
        client.reasoning = "none"
        try:
            out, _ = client.complete(messages, schema, timeout=timeout)
            return out or {}
        finally:
            client.reasoning = saved
    original = getattr(client, "body", None)

    def body(messages_, schema_):
        value = original(messages_, schema_)
        value["tools"][0]["strict"] = False
        value["reasoning"] = {"effort": "none"}
        return value

    if callable(original):
        client.body = body
    try:
        out, _ = client.complete(messages, schema, timeout=timeout)
        return out or {}
    finally:
        if callable(original):
            del client.body


def call_problem(error, limit=160):
    """Why a structuring call failed, cut to ``limit`` characters for the run's record. An empty account is
    read on the whole error first (the provider's code sits deep in the body) and keeps its status and code, so
    ``model_failure`` still makes Smart unavailable and ``plain_error`` still says why."""
    text = f"{type(error).__name__}: {error}"
    if out_of_credit(text):
        status = PROVIDER_ERROR.search(text)
        code = next((code for code in CREDIT_CODES + ANTHROPIC_CREDIT if code in text), None)
        return ": ".join(part for part in (type(error).__name__, status and status.group(0), code) if part)
    return text[:limit]


def structure_note(output_format, problem):
    """The run view's line for an automatic structure that didn't happen (``output_note``), with the curly
    apostrophes every dashboard string uses."""
    name = AUTOMATIC_FORMATS[output_format]
    if out_of_credit(problem):
        return (f"Your {account_name(credit_provider(problem))} account ran out of credit before Mobster could turn this answer "
                f"into {name}, so it’s shown as text.")
    return f"Mobster couldn’t turn this answer into {name}, so it’s shown as text."


def structure(client, request, answer, notes, schema, *, timeout=60):
    """(data, None) with the answer as data matching the user's JSON schema, validated, or (None, why). One
    small call (``_loose_call``)."""
    from jsonschema import Draft202012Validator
    wrapped = {"type": "object", "additionalProperties": False, "required": ["data"], "properties": {"data": schema}}
    try:
        value = _loose_call(client, _findings(request, answer, notes, "Put the phone agent's findings into the "
                                              "requested structure."), wrapped, timeout).get("data")
    except Exception as error:
        return None, call_problem(error)
    if any(True for _ in Draft202012Validator(schema).iter_errors(value)):
        return None, "The answer did not fit the requested schema."
    return value, None


# Result formats that take a structure the user didn't write (Result: CSV, JSON or YAML, Schema: Automatic).
AUTOMATIC_FORMATS = {"csv": "CSV", "json": "JSON", "yaml": "YAML"}
# The automatic structure's one call: the findings as named columns and rows. Code turns them into records with a
# schema, the shapes Fast's automatic output takes (output_contract.validate_automatic_schema): one flat record, or
# a list of them, which is also what CSV needs.
TABLE = {"type": "object", "additionalProperties": False, "required": ["columns", "rows", "single"], "properties": {
    "columns": {"type": "array", "maxItems": 40, "items": {"type": "string"}},
    "rows": {"type": "array", "maxItems": 100,
             "items": {"type": "array", "items": {"type": ["string", "number", "boolean", "null"]}}},
    "single": {"type": "boolean"}}}
CELL = ("string", "number", "integer", "boolean", "null")


def structure_auto(client, request, answer, notes, output_format, *, timeout=60):
    """(data, schema, None) with the answer as records for an automatic ``output_format`` (a key of
    AUTOMATIC_FORMATS), or (None, None, why). One small call (``_loose_call``) names the columns and fills the
    rows; code builds the records and their schema and checks both."""
    from jsonschema import Draft202012Validator
    from .output_contract import validate_automatic_schema
    instruction = (f"Put the phone agent's findings into a table for a {AUTOMATIC_FORMATS[output_format]} export. "
                   "columns: one short, readable name per field, lower case, like title or due_date. rows: one row "
                   "per item the request asks about, one value per column in the same order. single: true when the "
                   "answer is about one thing, false for a list of things.")
    try:
        out = _loose_call(client, _findings(request, answer, notes, instruction), TABLE, timeout)
    except Exception as error:
        return None, None, call_problem(error)
    columns, rows, single = out.get("columns"), out.get("rows"), out.get("single") is True
    if not isinstance(columns, list) or not isinstance(rows, list) or not all(isinstance(row, list) for row in rows):
        return None, None, "The answer did not come back as a table."
    columns = [" ".join(str(name).split())[:64] for name in columns]
    if not columns or not all(columns) or len(set(columns)) != len(columns) \
            or any(len(row) != len(columns) for row in rows) or single and len(rows) != 1:
        return None, None, "The table's columns and rows did not line up."
    record = {"type": "object", "additionalProperties": False, "required": list(columns),
              "properties": {name: {"type": list(CELL)} for name in columns}}
    schema = record if single else {"type": "array", "maxItems": 100, "items": record}
    records = [dict(zip(columns, row)) for row in rows]
    data = records[0] if single else records
    try:
        validate_automatic_schema(output_format, schema)  # its copy sorts the keys: the columns keep their order here
    except ValueError as error:
        return None, None, str(error)
    if any(True for _ in Draft202012Validator(schema).iter_errors(data)):
        return None, None, "The table's values did not fit its columns."
    return data, schema, None


def shaped_answer(text, schema):
    """The DONE turn's data_json parsed, when it validates against the user's ``schema``; else None."""
    if schema is None or not isinstance(text, str) or not text.strip():
        return None
    try:
        value = json.loads(text)
        from jsonschema import Draft202012Validator
        if any(True for _ in Draft202012Validator(schema).iter_errors(value)):
            return None
    except Exception:
        return None
    return value


def untouched_writes(contract):
    """WRITE items the contract counts as done though nothing was typed or picked in their app: the model
    judged the text already there (a reminder from an earlier task, 27 Sep), so Done would overstate it. A
    search's typing counts when the WRITE is the query (B6: "Enter 'pasta carbonara recipe' in YouTube" was typed
    into YouTube's search and still read "Already done, so Mobster changed nothing")."""
    from . import contract as K
    if contract is None:
        return []

    def touched(item, typed):
        if not contract._app_ok(item, typed["app"]):
            return False
        if not typed["search"]:
            return True
        return (bool(item.payload) and K._overlaps(item.payload, typed["text"])
                or bool(K.SEARCHY.search(item.what)) or K._overlaps(item.what, typed["text"]))
    return [item for item in contract.items if item.kind == "WRITE" and not item.unproven and not any(
        touched(item, t) for t in contract.typed)]


def summarize(outcome, agent, events, *, request, output_format="auto", output_schema=None, cost_usd=None):
    """The result of a Smart run: legacy fields the dashboard already reads (status, data, reason, actions)
    plus the contract's outcome, answer, proof, engine and costUsd. ``events`` is the run's SmartEvents."""
    contract = getattr(agent, "_contract", None)
    raw_status = outcome.get("status") or "error"
    status = STATUS.get(raw_status, "error")
    full = (outcome.get("answer") or "").strip() or None
    kind = outcome_of(status, contract, full)
    untouched = untouched_writes(contract) if kind == "done" else []
    if untouched:
        kind = "check"
    if status == "completed" and kind == "check":
        status = "completion_not_confirmed"
    model = getattr(getattr(agent, "client", None), "model", None) or smart_model()
    reason = reason_of(status, kind, outcome.get("reason"), contract,
                       bool(getattr(getattr(agent, "client", None), "model_failed", False)) and status == "error", model)
    if untouched:
        reason = "Already done, so Mobster changed nothing: " + "; ".join(item_text(i) for i in untouched[:3]) + "."
    proof = events.final_proof() if events is not None else []
    # The answer is its first sentence, a headline (the dashboard shows the rest of ``data`` under it). Only a
    # sentence about what the agent did loses its "I"; words read on the phone stay as they were.
    said = without_first_person(full) if full and tells_what_it_did(first_sentence(full), contract, proof) else full
    summary = {"event": "result", "status": status, "engine": SMART, "outcome": kind,
               "answer": first_sentence(said), "reason": reason, "independently_verified": kind == "done",
               "actions": outcome.get("actions"), "decisions": outcome.get("steps"),
               "elapsed_ms": round(outcome["elapsed"] * 1000) if outcome.get("elapsed") is not None else None,
               "costUsd": cost_usd if cost_usd is not None else outcome.get("cost_usd"),
               "model": model, "contract": outcome.get("contract"),
               "timing": outcome.get("timing"), "proof": proof}
    if outcome.get("code"):
        summary["code"] = outcome["code"]  # a phone-guard stop: phone_locked, unplugged, face_id ...
    automatic = output_schema is None and output_format in AUTOMATIC_FORMATS
    if full and status in ("completed", "completion_not_confirmed", "spend_cap", "max_steps", "timeout", "blocked"):
        if (output_schema is not None or automatic) and status != "blocked":
            client = getattr(agent, "client", None)
            given = shaped_answer(outcome.get("data_json"), output_schema) if not automatic else None
            if given is not None:
                # The DONE turn's own data_json fits the schema: no structuring call (T2.10).
                data, schema, problem = given, output_schema, None
            elif client is None:
                data, schema, problem = None, None, "No model client"
            elif automatic:
                data, schema, problem = structure_auto(client, request, full, outcome.get("notes"), output_format)
            else:
                (data, problem), schema = structure(client, request, full, outcome.get("notes"), output_schema), output_schema
            source = "automatic" if automatic else "custom"
            if data is not None:
                summary.update(data=data, resolved_output_format=output_format, schema_validated=True,
                               data_status="validated", output_schema=schema, schema_source=source, full_answer=full)
            elif automatic:
                # The answer stands without a structure: its text, with a line that says why it isn't the format.
                summary.update(data=full, resolved_output_format="text", data_status="observed", full_answer=full,
                               structure_error=problem, output_note=structure_note(output_format, problem))
            else:
                summary.update(data_status="schema_validation_failed", resolved_output_format=output_format,
                               full_answer=full, structure_error=problem, schema_source=source)
                if out_of_credit(problem):
                    summary["output_note"] = (f"Your {account_name(credit_provider(problem))} account ran out of credit before "
                                              "Mobster could fit this answer to your schema.")
        else:
            summary.update(data=full, resolved_output_format="markdown" if output_format == "markdown" else "text",
                           data_status="observed")
    else:
        summary.update(resolved_output_format=None, output_intent="action_only" if full is None and kind == "done" else None)
    if not full and kind == "done":
        summary["answer"] = "Done."
    return summary
