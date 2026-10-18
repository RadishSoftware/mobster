"""Mobster's one engine (``smart``, Mobster's agent) and the frozen config the benchmark shares.

``smart`` is frontier.FrontierAgent, one model planning every step (Claude Sonnet 5.5 on a Claude key, gpt-5.6-sol on
an OpenAI key alone, a plan's own model). SMART is the only place its model, reasoning, and limits are set;
the harness imports them from here, and tests/test_engines.py proves both paths build the same agent. The model is the
one thing a user may change (``smart_model``): MOBSTER_SMART_MODEL or MOBSTER_SMART_PROVIDER. ``read_engine`` reads a
saved or sent engine: a ``fast`` or ``quick`` from before Quick mode was retired (13 Oct 2026) is ``smart``.

This module also holds what the app needs around the Smart loop and the benchmark does not: which key it
uses, whether that key reaches the model, and the words for a provider's failure. The cost estimate and the metering
are in costs.py, the dashboard's events in smart_events.py (docs/engine-contract.md) and the result in
smart_result.py. Nothing here changes a decision the loop makes.
"""

import dataclasses
import hashlib
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import harness_api

SMART = "smart"
ENGINES = (SMART,)
# Quick mode's names, as older settings, workflows, runs and scripts saved or send them.
RETIRED = ("fast", "quick")


def read_engine(value):
    """``value`` as an engine to run: Quick mode's retired names read as ``smart``; anything else is unchanged."""
    return SMART if isinstance(value, str) and value.strip().casefold() in RETIRED else value


@dataclass(frozen=True)
class SmartConfig:
    """The frozen Smart loop. Change it only with a benchmark run that shows why."""
    # Claude Sonnet 5.5 at low reasoning since 12 Oct 2026. Paired with gpt-5.6-sol on the same 16 never-run iOSWorld
    # tasks (28 Sep, docs/benchmarks.md "Smart on Claude"): pass +0.0 points (95% CI -25 to +25), 0.68x the cost a
    # task, agent time p50 137 s against 154 s. Since then this loop has run on Sonnet 5.5: iOSWorld dev_core 53.8%
    # official pass (the A arms of aa1, ab1, ac1 and ac2, 10-11 Oct) and MobsterBench Real 38 of 46 graded tasks on the
    # owner's iPhone (12 Oct). OPENAI_SMART_MODEL is Smart's model on an OpenAI key alone.
    model: str = "claude-sonnet-5-5"
    reasoning: str = "low"
    max_steps: int = 50          # iOSWorld's own step limit
    max_seconds: int = 900
    screenshots: bool = True
    settle_seconds: float = .6


SMART_CONFIG = SmartConfig()
# Smart's model on a Claude key, and on an OpenAI key alone (MOBSTER_SMART_MODEL picks any model outright).
ANTHROPIC_SMART_MODEL = "claude-sonnet-5-5"
OPENAI_SMART_MODEL = "gpt-5.6-sol"
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
    """Who serves ``model``: a provider from Settings › Models for "<provider>:<model>", anthropic for claude-*,
    openai otherwise. (A ChatGPT plan runs only through the person's own Codex: ai_plans/codex.py.)"""
    model = str(model or "")
    providers = harness_api.model_routes()
    routed, _ = providers.split(model)
    if routed:
        return routed  # "<provider>:<model>": a plan or a key from Settings › Models (ai_plans/providers.py)
    return "anthropic" if model.startswith("claude-") else "openai"


def on_plan(model):
    """Whether ``model`` runs on a plan the user pays for (no per-call cost): a plan in ai_plans/providers.py (Kimi
    Code, Grok ...)."""
    providers = harness_api.model_routes()
    routed, _ = providers.split(model)
    return bool(routed) and providers.get(routed).group == "plan"


def model_words(model):
    """``model`` for a status line: "kimi-for-coding, using your Kimi Code plan" for a plan model, else the id."""
    if on_plan(model):
        providers = harness_api.model_routes()
        routed, slug = providers.split(model)
        return f"{slug}, using {providers.get(routed).plan_label or 'your plan'}"
    return str(model or "")


def provider_name(model):
    provider = model_provider(model)
    if provider in PROVIDER_NAMES:
        return PROVIDER_NAMES[provider]
    providers = harness_api.model_routes()
    return providers.get(provider).name


def anthropic_key(env=None):
    """The Anthropic key Smart uses on Claude: ANTHROPIC_API_KEY, else an Anthropic helper's (``keys.anthropic_key``,
    the key hook; the engine alone reads ANTHROPIC_API_KEY only)."""
    key, _ = _find_key("anthropic", os.environ if env is None else env)
    return (key or "").strip() or None


def _find_key(provider, env):
    """(key, source) for "anthropic" or "openai": the harness's key hook (keys.py: a helper's key Settings saved counts
    too), else the provider's own environment variable."""
    find = harness_api.hook("key")
    if find is not None:
        return find(provider, env)
    name = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    key = (env.get(name) or "").strip()
    return (key, "own") if key else (None, None)


def smart_model(env=None):
    """The model Smart plans with: MOBSTER_SMART_MODEL when it is set (any OpenAI or Claude model id); else the
    provider the user chose (MOBSTER_SMART_PROVIDER, saved by Setup's "Connect your AI account", Settings › Models
    or `mobster login`) while its key is there; else, with no choice, Claude whenever a Claude key is there
    (ANTHROPIC_SMART_MODEL), even beside an OpenAI key, OPENAI_SMART_MODEL with an OpenAI key alone, and
    SMART_CONFIG.model with no key at all. Only the model is chosen here: the loop is SMART_CONFIG's."""
    env = os.environ if env is None else env
    chosen = (env.get("MOBSTER_SMART_MODEL") or "").strip()
    if chosen and MODEL_ID.fullmatch(chosen) and not chosen.startswith("gemini-"):
        return chosen
    preferred = (env.get("MOBSTER_SMART_PROVIDER") or "").strip()
    providers = harness_api.model_routes()
    if providers.get(preferred) is not None:
        # A plan or a key chosen in Settings › Models or `mobster login <id>`: it stays the choice while its key is
        # missing (the run then says so), never a silent switch to another bill.
        return providers.model_id(providers.get(preferred), env)
    if preferred == "openai" and openai_smart_key(env):
        return OPENAI_SMART_MODEL
    if anthropic_key(env):
        return ANTHROPIC_SMART_MODEL
    if openai_smart_key(env):
        return OPENAI_SMART_MODEL
    return SMART_CONFIG.model


def smart_config(env=None):
    """SMART_CONFIG on ``smart_model``: the frozen loop, only its model chosen."""
    model = smart_model(env)
    return SMART_CONFIG if model == SMART_CONFIG.model else dataclasses.replace(SMART_CONFIG, model=model)


def frontier_kwargs(config=SMART_CONFIG, *, max_cost_usd=None):
    """FrontierAgent's keyword arguments for ``config``. ``max_cost_usd`` is the one deliberate difference
    between the paths: the app stops at the user's per-task limit, the harness at its per-task cap."""
    return {"max_steps": config.max_steps, "max_seconds": config.max_seconds, "screenshots": config.screenshots,
            "settle_seconds": config.settle_seconds, "max_cost_usd": max_cost_usd}


def build_client(config=None, *, key=None):
    """The Smart model's client on the user's key: model_clients.OpenAIChat, or model_clients.AnthropicChat for a Claude
    model. ``config``: SMART_CONFIG on ``smart_model`` unless given."""
    from .model_clients import chat_client
    config = smart_config() if config is None else config
    return chat_client(config.model, reasoning=config.reasoning, key=key or None)


def build_frontier(driver, client, *, apps, emit, max_cost_usd=None, approve=None, cancelled=None, config=SMART_CONFIG,
                   guard=None, skills=None, output_schema=None, context_blocks=(), turn_context=None,
                   steering=None, clarify=None, step_observer=None, checkpoint=None, max_steps=None,
                   max_seconds=None, run_context=None, commit_check=None, dialog_approve=None, longform=None,
                   forced_approve=None, permissions=None, floor_approve=None,
                   permission_mode=None, saved_program=None, program_parts=None):
    """The Smart agent: FrontierAgent with ``config``'s arguments. ``cancelled`` is the app's
    Stop (the harness passes none). ``guard``: the phone guard (lockscreen.guard_for, agent_hooks.PhoneGuard);
    ``skills``: None for the default skills; ``output_schema``: the user's schema, asked for in the DONE turn
    (``summarize`` then skips its structuring call when the answer fits).

    Seam S7: ``context_blocks``, ``turn_context``, ``steering``, ``clarify``, ``step_observer``, ``checkpoint`` and
    ``run_context`` go to FrontierAgent as they are (none set: the prompts are exactly as before);
    ``max_steps`` and ``max_seconds`` (harness_api.frontier_options) replace ``config``'s when given.
    ``commit_check``: a task as an account (accounts/screen.py) checks the account on screen before each commit.
    ``dialog_approve``: who answers iOS's own prompts Mobster leaves to the user (system_dialogs), when ``approve`` is
    None because Ask before acting is off: a privacy grant asks in every mode.

    Long tasks (mobile_agent/longform): ``longform`` is the run's LongSession; ``forced_approve`` asks the person
    before a long run's acts that ask when Ask before acting is off. Neither set: as before.
    ``permissions``: the run's permission gate (permissions.gate_for_run); None asks as before. ``floor_approve``: who
    asks before what asks in every mode (a one-time code) when ``approve`` is None because the run has full access.
    ``permission_mode``: the run's own mode (the task's, its phone's), for iOS's prompts (system_dialogs).
    ``saved_program``: a pre-run hook's matched program (learned/programs.py), or the function that matches it.
    ``program_parts``: a pre-run hook's parts of saved programs (learned/parts.Parts)."""
    from .frontier import FrontierAgent
    kwargs = frontier_kwargs(config, max_cost_usd=max_cost_usd)
    if max_steps is not None:
        kwargs["max_steps"] = int(max_steps)
    if max_seconds is not None:
        kwargs["max_seconds"] = max_seconds
    offered = harness_api.hook("tool_policy") if skills else None
    if offered is not None:
        # The tool policy (harness/tools): the default skills as they are; registered tools only when available,
        # interactive-only ones only when a person watches, at most 8. The engine alone offers ``skills`` as given.
        skills = offered(skills, run_context=run_context, driver=driver, guard=guard)
    hooks = {name: value for name, value in (
        ("context_blocks", tuple(context_blocks or ())), ("turn_context", turn_context), ("steering", steering),
        ("clarify", clarify), ("step_observer", step_observer), ("checkpoint", checkpoint),
        ("run_context", run_context), ("commit_check", commit_check), ("dialog_approve", dialog_approve),
        ("longform", longform), ("forced_approve", forced_approve),
        ("permissions", permissions), ("floor_approve", floor_approve),
        ("permission_mode", permission_mode), ("saved_program", saved_program),
        ("program_parts", program_parts)) if value}
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
    """The key Smart uses: ANTHROPIC_API_KEY when ``smart_model`` is a Claude model, a provider's key (or sign-in) for
    one from Settings › Models, else ``openai_smart_key``."""
    env = os.environ if env is None else env
    provider = model_provider(smart_model(env))
    if provider == "anthropic":
        return anthropic_key(env)
    providers = harness_api.model_routes()
    if providers.get(provider) is not None:
        key = providers.credential(providers.get(provider), env)
        return None if key is None else (key or "local-server-no-key")
    return openai_smart_key(env)


def openai_smart_key(env=None):
    """The OpenAI key Smart uses: OPENAI_API_KEY, else the helper's key when the helper is OpenAI.

    The same rule as ``keys.openai_key``, which Setup, Settings and doctor read: an OpenAI helper counts
    whether Settings saved it (MOBSTER_HELPER_PROVIDER) or its base URL is OpenAI's, so Setup never calls
    the key step done while Smart asks for a key."""
    env = os.environ if env is None else env
    key = (env.get("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    helper, source = _find_key("openai", {name: value for name, value in env.items() if name != "OPENAI_API_KEY"})
    if source != "helper":
        return None
    return helper.strip() or None


# Setup's step for either key is "Connect your AI account"; the Mac app reads this as a missing key (keyMissing).
NO_SMART_KEY = "Connect your AI account in Setup"
NO_CREDIT = "Your OpenAI account is out of credit. Add credit at platform.openai.com, then try again."
NO_ANTHROPIC_CREDIT = "Your Claude account is out of credit. Add credit at platform.claude.com, then try again."
CREDIT_REASONS = (NO_CREDIT, NO_ANTHROPIC_CREDIT)
# The error codes OpenAI gives an account with no credit left; its 402 (Payment Required) says the same.
CREDIT_CODES = ("insufficient_quota", "credit_balance_exhausted")
# What Anthropic says to an account with no credit: a 400 whose message reads "Your credit balance is too low to
# access the Anthropic API", or a 402 billing_error.
ANTHROPIC_CREDIT = ("credit balance is too low", "billing_error")
PROVIDER_ERROR = re.compile(r"(OpenAI|Anthropic) HTTP (\d{3})")


def no_key_reason(managed=True, model=None, env=None):
    """Why Smart can't run without a key. SMART_CONFIG.model, the model with no key at all, runs on either key: the
    dashboard's words (Setup asks for a key), the env file's for a terminal user. Any other Claude model, or the one
    MOBSTER_SMART_MODEL names, needs ANTHROPIC_API_KEY and says so."""
    env = os.environ if env is None else env
    model = model or smart_model(env)
    chosen = (env.get("MOBSTER_SMART_MODEL") or "").strip() == model
    if model_provider(model) == "anthropic" and (chosen or model != SMART_CONFIG.model):
        return f"Add ANTHROPIC_API_KEY to the agent's env file to run Smart on {model}"
    return NO_SMART_KEY if managed else "Add OPENAI_API_KEY or ANTHROPIC_API_KEY to the agent's env file"


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
        if model_provider(model) not in ("openai", "anthropic"):
            return True, None  # a provider from Settings › Models: its key was checked when it was saved
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
        """(True, None), (False, why) or (None, None) when the answer is unknown (offline, a 5xx, a provider from
        Settings › Models, whose key was checked against its model list when it was saved)."""
        model = self._model(model)
        if model_provider(model) not in ("openai", "anthropic"):
            return None, None
        if model_provider(model) == "anthropic":
            from .model_clients import ANTHROPIC_VERSION
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
    return f"{account_name(provider)} didn't accept your key. Check it in Settings › Models."


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


def model_error(reason, model_failed=False):
    """Which model-provider failure ended a run, for the app to read beside ``plain_error``'s sentence (a result's
    ``model_error``): no_credit, rejected_key, model_unavailable, rate_limited, provider_error, declined or
    unreachable; None when the provider wasn't the cause (the phone, the screen, a step). ``plain_error`` words each code,
    so the code and the sentence always agree."""
    text = str(reason or "")
    found = PROVIDER_ERROR.search(text)
    status = found.group(2) if found else None
    if out_of_credit(text):
        return "no_credit"
    if status in ("401", "403"):
        return "rejected_key"
    if status == "404":
        return "model_unavailable"
    if status == "429":
        return "rate_limited"
    if status and status.startswith("5"):
        return "provider_error"
    if "Anthropic declined the request" in text:
        return "declined"
    if model_failed:
        return "unreachable"
    return None


def plain_error(reason, model_failed=False, model=None):
    """A run error in words a person can act on, or None. ``model_failed``: the task's last model call
    failed (costs.meter), so a timeout or a network error was the model provider's, not the phone's.
    ``model``: the run's model (its provider names the service), else ``smart_model``."""
    text = str(reason or "")
    model = model or smart_model()
    found = PROVIDER_ERROR.search(text)
    name = found.group(1) if found else provider_name(model)
    code = model_error(text, model_failed)
    if code:
        return {
            "no_credit": NO_ANTHROPIC_CREDIT if name == "Anthropic" else NO_CREDIT,
            "rejected_key": rejected_key(name),
            "model_unavailable": f"Your {account_name(name)} key can't use {model}.",
            "rate_limited": f"{account_name(name)} is rate limiting your key. Wait a minute, then try again.",
            "provider_error": f"{account_name(name)} had a problem answering. Try again in a moment.",
            "declined": "Claude declined this request. Try rewording it, or run it on another model.",
            "unreachable": NO_ANTHROPIC_REACH if name == "Anthropic" else NO_OPENAI_REACH,
        }[code]
    if re.search(r"TimeoutError|timed out|ConnectionRefused|ConnectionReset|RemoteDisconnected", text):
        return "Your iPhone stopped answering. Check it is unlocked and connected, then try again."
    if re.match(r"[A-Z]\w*(Error|Exception)\b", text):
        # An internal error's name and message mean nothing to the user; the steps show how far it got.
        return "Mobster couldn't read your iPhone's screen partway through. Try the task again."
    return None
