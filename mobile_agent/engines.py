"""Mobster's one engine (``smart``, Mobster's agent) and the frozen config the benchmark shares.

``smart`` is frontier.FrontierAgent, one model planning every step (Claude Sonnet 5.5 on a Claude key, gpt-5.6-sol on
an OpenAI key alone, a plan's own model). SMART is the only place its model, reasoning, limits and switches are set;
the harness imports them from here, and tests/test_engines.py proves both paths build the same agent. The model is the
one thing a user may change (``smart_model``): MOBSTER_SMART_MODEL or MOBSTER_SMART_PROVIDER. ``read_engine`` reads a
saved or sent engine: a ``fast`` or ``quick`` from before Quick mode was retired (13 Oct 2026) is ``smart``.

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

from . import harness_api
from .api_errors import PHONE_STOP_CODES

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
    # The environment switches the loop reads, at the values it was measured with: none since the pixel settle became
    # plain code (13 Oct 2026). Compact replies, READ_LIST and SCROLL_TO, Bet 1's task contract (declared commits and
    # the DONE gate), Bet 2's exact actuation and the screen finish (frontier.screen_goal) are plain code too.
    switches: tuple = ()


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


def switches(config=SMART_CONFIG):
    return dict(config.switches)


def apply_switches(config=SMART_CONFIG, *, pin=True):
    """Set the loop's switches in this process. The app pins them (``pin``), so a stray variable in the user's env
    file cannot change Smart; the harness only fills in what is unset, so a research ablation still works there. With a
    clean environment both are identical."""
    for name, value in config.switches:
        if pin:
            os.environ[name] = value
        else:
            os.environ.setdefault(name, value)


def frontier_kwargs(config=SMART_CONFIG, *, max_cost_usd=None):
    """FrontierAgent's keyword arguments for ``config``. ``max_cost_usd`` is the one deliberate difference
    between the paths: the app stops at the user's per-task limit, the harness at its per-task cap."""
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
                   max_seconds=None, run_context=None, commit_check=None, dialog_approve=None, longform=None,
                   forced_approve=None, permissions=None, floor_approve=None,
                   permission_mode=None, saved_program=None, program_parts=None):
    """The Smart agent: FrontierAgent with ``config``'s arguments and switches. ``cancelled`` is the app's
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
    ``program_parts``: a pre-run hook's parts of saved programs (learned/programs.Parts)."""
    from .frontier import FrontierAgent
    apply_switches(config, pin=pin)
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
    unreachable; None when the provider wasn't the cause (the phone, the screen, a step). The same order as
    ``plain_error``, so the code and the sentence always agree."""
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
# The fit is sol's (OPENAI_SMART_MODEL), not SMART_CONFIG.model's; another model's tasks cost this multiple of sol's
# on the same tasks, both at list prices (sol at $4/$20). Paired iOSWorld A/B, 16 never-run tasks, 28 Sep: Sonnet 5.5
# $0.197 against sol's $0.291 a task (32 runs and 21; 95% CI of the difference -$0.143 to -$0.044), Opus 5.5 $0.297
# (16 runs).
SMART_COST_RATIO = {OPENAI_SMART_MODEL: 1.0, "claude-sonnet-5-5": 0.68, "claude-opus-5-5": 1.02,
                    # Haiku 5.5 planning with code-checked steps cost 0.059-0.066 of Sonnet 5.5 on the same 16 tasks
                    # (paired A/Bs on the phone, 9 Oct): 0.68 x 0.065. gpt-6.1-sol's list price is half sol's, and a
                    # recorded turn cost 0.46 of sol's (9 Oct).
                    "claude-haiku-5-5": 0.044, "gpt-6.1-sol": 0.46}


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
    if on_plan(model):
        # Nothing per call: the run uses the person's plan allowance (``billing`` says so to the UI).
        return {"available": available, "reason": reason, "model": model, "provider": provider_name(model),
                "lowUsd": 0.0, "highUsd": 0.0, "billing": "plan"}
    point = smart_point(goal, app_names) * SMART_COST_RATIO.get(model, 1.0) + (SMART_STRUCTURE_USD if structured else 0)
    return {"available": available, "reason": reason, "model": model, "provider": provider_name(model),
            "lowUsd": round(point / SMART_SPREAD, 4), "highUsd": round(point * SMART_SPREAD, 4)}


def midpoint(estimate):
    """The estimate's single figure: the geometric mean of its range."""
    low, high = estimate["lowUsd"], estimate["highUsd"]
    return round(math.sqrt(low * high), 4) if low > 0 and high > 0 else high


def meter(client, emit, *, provider=None, purpose=None):
    """Wrap ``client.complete`` so every model call emits inference_started / inference_finished with its
    list-price cost (the usage ledger and the spend limits read those). The loop's own accounting
    (client.usage, frontier.cost_usd) is untouched. ``client.model_failed`` says whether the last call failed.
    A client that hedges (frontier.AnthropicChat) reports a losing twin's tokens through ``on_hedge_usage``
    when it finishes: it is billed, so it is recorded as a call of its own (purpose "hedge"). ``purpose``: a label
    for every call of this client (a long task's "extract", "digest", "synthesis" or "followup"), so the usage
    ledger and History show where the money went; None reads it from the schema as before."""
    from .costs import NANODOLLARS
    from .frontier import cost_usd
    inner = client.complete
    client.model_failed = False
    provider = provider or getattr(client, "provider", None) or "openai"

    def complete(messages, schema, **kwargs):
        call_id = uuid.uuid4().hex[:16]
        model = getattr(client, "model", "") or "unknown"
        kind = purpose or ("planning" if "items" in (schema.get("properties") or {}) and "actions" not in (
            schema.get("properties") or {}) else "decision")
        meta = {"provider": provider, "call_id": call_id, "model": model, "purpose": kind}
        if on_plan(model):
            providers = harness_api.model_routes()
            meta["plan"] = providers.plan_name(providers.get(providers.split(model)[0]))  # "On your Kimi Code plan"
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
        if kind != "planning":
            # The task contract compiles alongside the first decision (the early decision): its success, which
            # may land later, never hides that decision's failure.
            client.model_failed = False
        usage = usage or {}
        finished(meta, usage, started)
        return out, usage

    def finished(meta, usage, started):
        prompt, completion = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        usd = cost_usd(meta["model"], usage)
        basis = "plan" if on_plan(meta["model"]) else "published_rate"
        emit({"event": "inference_finished", **meta, "success": True,
              "latency_ms": round((time.monotonic() - started) * 1000, 2),
              "usage": {"input_tokens": prompt, "output_tokens": completion, "total_tokens": prompt + completion,
                        "cached_input_tokens": int(usage.get("cached_tokens", 0))},
              "estimated_usd": usd, "cost_nanodollars": None if usd is None else round(usd * NANODOLLARS),
              "cost_basis": None if usd is None else basis})

    def hedged(usage):
        meta = {"provider": provider, "call_id": uuid.uuid4().hex[:16],
                "model": getattr(client, "model", "") or "unknown", "purpose": "hedge" if purpose is None
                else f"{purpose}_hedge"}
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
        "ASK_USER": "Asked you a question", "OPEN_URL": "Opened a link", "WATCH_FEED": "Watched the feed",
        "WATCH": "Watched the screen",
        "READ_EACH": "Read each item", "RECALL": "Looked back through earlier results",
    }.get(op, op.replace("_", " ").capitalize())


def connector_step(call):
    """A CALL_TOOL step in plain words, from its last connector_call event: "Used Linear: create_issue"."""
    if not isinstance(call, dict) or not call.get("connector"):
        return "Didn't use a connected service"
    name = " ".join(str(call["connector"]).split())
    name = shorten(name, 40) + (f": {shorten(str(call['tool']), 60)}" if call.get("tool") else "")
    return {"unknown": f"{name} didn't answer", "not_sent": f"Couldn't use {name}", "refused": f"Didn't use {name}",
            "error": f"{name} returned an error"}.get(call.get("status"), f"Used {name}")


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

    def __init__(self, emit, driver=None, apps=None, frame=None, ledger=None):
        self.out, self.driver, self.apps = emit, driver, dict(apps or {})
        # A long task's SpendLedger: its cost events report the run's whole spend (every metered call), not only
        # the navigation client's.
        self.ledger = ledger
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
        self.watched = None      # WATCH's last ``watched`` event: its words, picture and timeline make its step
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
        private_rects = harness_api.hook("private_rects")
        if private_rects is None:
            return None
        try:
            return private_rects(screen.elements, getattr(screen, "bundle_id", None))
        except Exception:
            return None

    @property
    def contract(self):
        return getattr(self.agent, "_contract", None)

    def __call__(self, event):
        kind = event.get("event")
        with self.lock:
            # WATCH's timeline is kept once, in its step (below), not in the event as well.
            media = event.pop("media", None) if kind == "watched" else None
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
                self._thought(event)
            elif kind == "watched" and event.get("ok"):
                # After out(): the server put the contact sheet beside the run and left its id here.
                self.watched = {"text": event.get("text"), "frameId": event.get("frameId"), "media": media}
            elif kind == "connector_call":
                self.connector_call = event  # its latest status names the CALL_TOOL step that follows
                if event.get("status") == "done" and event.get("excerpt"):
                    # What a connected service answered is proof as a screen is: quoted, with where it came from.
                    self.tool_proof = [*getattr(self, "tool_proof", [])[-2:], {
                        "quote": shorten(str(event["excerpt"]), 300), "app": event.get("connector"),
                        "screen": event.get("tool"), "frameId": None}]
            elif kind == "frontier_action":
                self._action(event)
            elif kind == "system_dialog_shown":
                self.dialog_frame = self._grab()  # the prompt itself, before anything is pressed
            elif kind == "system_dialog" and event.get("ok") and event.get("button"):
                # iOS's own prompt, answered: a step in the thread, with the frame that shows the prompt.
                from .system_dialogs import thread_text
                self._flush()
                frame, self.dialog_frame = getattr(self, "dialog_frame", None), None
                self._step(thread_text(event.get("kind"), event.get("app"), event["button"], event.get("by")),
                           event.get("step"), frame)
            if kind in ("frontier_decision", "frontier_action", "frontier_contract_update", "frontier_failed",
                        "frontier_chunk_stop"):
                self._receipts(event.get("step"))

    def _thought(self, event):
        """The turn's ``thought`` (docs/engine-contract.md): the model's own short sentence for this turn and how long
        it took, for the run view's "Thought for 2s". A saved program's turn made no model call and has none. The text is
        plain words: the loop masked the user's secrets in it, and ``secret_filter.redact`` masks anything else that
        reads as a key, a card or a code. It is shown, never sent to a model again."""
        if event.get("source") == "program":
            return
        from .secret_filter import redact
        text = event.get("thought")
        text = " ".join(redact(text).split()) if isinstance(text, str) else ""
        # The model names elements by the short ids it was shown ("e32"); a person reads "on screen".
        text = re.sub(r"\s*\((?:e\d{1,4}(?:,\s*)?)+\)", "", text)
        text = re.sub(r"\b(?:(?:in|at|on) )?(?:the )?(?:element|button|field|row) e\d{1,4}\b", "on screen", text)
        text = re.sub(r"\be\d{1,4}\b", "it", text)[:300]
        ms = event.get("latency_ms")
        self.out({"event": "thought", "step": event.get("step"), "text": text or None,
                  "ms": round(ms) if isinstance(ms, (int, float)) and ms >= 0 else None})

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
        if op == "WATCH" and self.watched is not None:
            # The step shows the frames it kept (one picture) and keeps the timeline it read as evidence.
            watched, self.watched = self.watched, None
            self.steps += 1
            step = {"event": "step", "n": self.steps, "step": event.get("step"),
                    "text": watched.get("text") or step_text(op, label, self.apps)}
            if watched.get("frameId"):
                step["frameId"], step["sheet"] = watched["frameId"], True
            if isinstance(watched.get("media"), dict):
                step["media"] = watched["media"]
            self.out(step)
            return
        if op == "CALL_TOOL":
            # No phone picture: the step happened at a connected service, not on the screen.
            self._step(connector_step(getattr(self, "connector_call", None)), event.get("step"), data=b"")
            self.connector_call = None
            return
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
        if self.ledger is not None:
            from .costs import NANODOLLARS
            usd = round(self.ledger.cost_nanodollars / NANODOLLARS, 6)
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
        for entry in getattr(self, "tool_proof", ()):
            if K.plain(entry["quote"]) not in quotes:
                quotes.add(K.plain(entry["quote"]))
                kept.append(dict(entry))
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


def _answer_values(data):
    """The scalar values of a structured answer, as plain text to look for (numbers without grouping commas)."""
    values = []
    for value in (data.values() if isinstance(data, dict) else ()):
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
            values.append(str(value).strip())
    return values


def _mentions(sentence, value):
    plain = " ".join(str(sentence or "").casefold().replace(",", "").split())
    return " ".join(value.casefold().replace(",", "").split()) in plain


def lead_sentence(full, data):
    """The headline for a structured answer: the first sentence when it states one of the answer's values, else
    the answer's first sentence that does ("The locker code is 4817. Calculator shows 4817 minus 800 = 4017."
    put the code, not the result, on top: 9 Oct). None when no sentence states one (the first one stands)."""
    values = _answer_values(data)
    if not values or not full:
        return None
    rest = str(full).strip()
    first = first_sentence(rest)
    if first and any(_mentions(first, value) for value in values):
        return None
    while rest:
        end = sentence_end(rest)
        sentence = " ".join((rest[:end] if end else rest).split())
        if sentence and any(_mentions(sentence, value) for value in values):
            return sentence if len(sentence) <= 300 else sentence[:299].rstrip() + "…"
        rest = rest[end:].strip() if end else ""
    return None


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
        actions = re.match(r"(\d+) actions used", str(raw_reason or ""))
        if actions:
            return f"The task took {actions.group(1)} actions, the most one task can take, before finishing."
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


def summarize(outcome, agent, events, *, request, output_format="auto", output_schema=None, cost_usd=None,
              report=None, markdown=None):
    """The result of a Smart run: legacy fields the dashboard already reads (status, data, reason, actions)
    plus the contract's outcome, answer, proof, engine and costUsd. ``events`` is the run's SmartEvents.

    A long task's ``report`` (longform/report.py) and its ``markdown`` make the result: ``answer`` is the TL;DR's
    first sentence, ``data`` the Markdown, the outcome ``done`` (``check`` when the report is partial or a commit
    went unproven), the proof the report's first sources, and ``report`` and ``markdown`` ride along. A run with an
    output schema or CSV, JSON or YAML gets its structure from the Markdown, as an answer's would."""
    if report is not None:
        return _summarize_report(outcome, agent, events, request=request, output_format=output_format,
                                 output_schema=output_schema, cost_usd=cost_usd, report=report, markdown=markdown)
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
    program = outcome.get("program")
    if isinstance(program, dict) and program.get("replayed"):
        # A saved or taught program ran steps with no model: the Mac app says "Learned" (lib/replay.ts runProgram).
        summary["program"] = {key: program.get(key) for key in ("steps", "replayed", "status", "taught", "name",
                                                                  "trusted", "helped", "practice", "corrections")}
    if outcome.get("code"):
        summary["code"] = outcome["code"]  # a phone-guard stop: phone_locked, unplugged, face_id ...
        if outcome["code"] in PHONE_STOP_CODES:
            # The field a Quick run and a refused preflight carry too: the reason is the phone's own sentence.
            summary["stop_code"] = outcome["code"]
    if status == "error":
        failed = model_error(outcome.get("reason"),
                             bool(getattr(getattr(agent, "client", None), "model_failed", False)))
        if failed:
            summary["model_error"] = failed  # the reason is plain_error's sentence for it
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
                lead = lead_sentence(said, data)
                if lead:
                    summary["answer"] = lead  # the headline states the value asked for
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


def _summarize_report(outcome, agent, events, *, request, output_format, output_schema, cost_usd, report, markdown):
    """``summarize`` for a long task with a report."""
    tldr = " ".join(str(report.get("tldr") or "").split())
    full = (markdown or "").strip() or None
    partial = bool(report.get("partial"))
    raw = {**outcome, "answer": full}
    if outcome.get("status") in ("max_steps", "timeout", "budget") and not partial:
        raw["status"] = "completed"
    summary = summarize(raw, agent, events, request=request, output_format=output_format, output_schema=output_schema,
                        cost_usd=cost_usd)
    headline = first_sentence(tldr) if tldr else first_sentence(report.get("title"))
    contract = getattr(agent, "_contract", None)
    unproven = [i for i in (contract.unproven() if contract else ()) if i.kind in ("COMMIT", "WRITE")]
    summary.update(answer=headline or summary.get("answer"), report=report, markdown=markdown,
                   outcome="check" if partial or unproven else "done")
    # A report comes from a long task (longform/report.py), so its helpers are there whenever this runs.
    long_report = harness_api.hook("long_report")
    helpers = long_report() if long_report is not None else None
    if not summary.get("proof") and helpers is not None:
        # What the report rests on, quoted as the screens showed it (the Mac app's proof).
        summary["proof"] = helpers.proof(report, getattr(events, "apps", None))
    if summary.get("status") == "completion_not_confirmed" and not partial and not unproven:
        summary["status"] = "completed"
    if partial:
        summary["reason"] = (helpers.partial_line(report, None) if helpers else None) or summary.get("reason")
    elif summary.get("reason") and summary.get("status") == "completed" and not unproven:
        summary["reason"] = None
    if full and output_schema is None and output_format not in AUTOMATIC_FORMATS:
        summary.update(data=markdown, resolved_output_format="markdown", data_status="observed")
    summary["independently_verified"] = False
    return summary
