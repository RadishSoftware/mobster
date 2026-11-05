"""Mobster's one engine (``smart``, Mobster's agent) and the frozen config the benchmark shares."""

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
RETIRED = ("fast", "quick")


def read_engine(value):
    """``value`` as an engine to run: Quick mode's retired names read as ``smart``; anything else is unchanged."""
    return SMART if isinstance(value, str) and value.strip().casefold() in RETIRED else value


@dataclass(frozen=True)
class SmartConfig:
    """The frozen Smart loop. Change it only with a benchmark run that shows why."""
    # Claude Sonnet 5.5 at low reasoning: paired with gpt-5.6-sol on 16 iOSWorld tasks it matched the pass rate at 0.68x
    # the cost (docs/benchmarks.md "Smart on Claude").
    model: str = "claude-sonnet-5-5"
    reasoning: str = "low"
    max_steps: int = 50          # iOSWorld's own step limit
    max_seconds: int = 900
    screenshots: bool = True
    settle_seconds: float = .6


SMART_CONFIG = SmartConfig()
ANTHROPIC_SMART_MODEL = "claude-sonnet-5-5"
OPENAI_SMART_MODEL = "gpt-5.6-sol"
PROVIDER_NAMES = {"openai": "OpenAI", "anthropic": "Anthropic"}
# People know the account as "Claude", never "Anthropic" (MESSAGING § 11); PROVIDER_NAMES stays the company for errors.
ACCOUNT_NAMES = {"OpenAI": "OpenAI", "Anthropic": "Claude"}


def account_name(provider):
    """The account's name in a sentence ("Claude" for Anthropic) from a company name or a model provider id."""
    company = PROVIDER_NAMES.get(provider, provider)
    return ACCOUNT_NAMES.get(company, company)
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def model_provider(model):
    """Who serves ``model``: a Settings › Models provider for "<provider>:<model>",
    anthropic for claude-*, else openai."""
    model = str(model or "")
    providers = harness_api.hooks().model_routes()
    routed, _ = providers.split(model)
    if routed:
        return routed  # "<provider>:<model>": a plan or a key from Settings › Models (ai_plans/providers.py)
    return "anthropic" if model.startswith("claude-") else "openai"


def on_plan(model):
    """Whether ``model`` runs on a plan the user pays for (no per-call cost)."""
    providers = harness_api.hooks().model_routes()
    routed, _ = providers.split(model)
    return bool(routed) and providers.get(routed).group == "plan"


def model_words(model):
    """``model`` for a status line: "kimi-for-coding, using your Kimi Code plan" for a plan model, else the id."""
    if on_plan(model):
        providers = harness_api.hooks().model_routes()
        routed, slug = providers.split(model)
        return f"{slug}, using {providers.get(routed).plan_label or 'your plan'}"
    return str(model or "")


def provider_name(model):
    provider = model_provider(model)
    if provider in PROVIDER_NAMES:
        return PROVIDER_NAMES[provider]
    providers = harness_api.hooks().model_routes()
    return providers.get(provider).name


def anthropic_key(env=None):
    """The Anthropic key Smart uses on Claude."""
    key, _ = harness_api.hooks().key("anthropic", os.environ if env is None else env)
    return (key or "").strip() or None


def smart_model(env=None):
    """The model Smart plans with: MOBSTER_SMART_MODEL, else the provider the user chose, else Claude on a Claude key,
    OpenAI on an OpenAI key alone, else SMART_CONFIG's."""
    env = os.environ if env is None else env
    chosen = (env.get("MOBSTER_SMART_MODEL") or "").strip()
    if chosen and MODEL_ID.fullmatch(chosen) and not chosen.startswith("gemini-"):
        return chosen
    preferred = (env.get("MOBSTER_SMART_PROVIDER") or "").strip()
    providers = harness_api.hooks().model_routes()
    if providers.get(preferred) is not None:
        # A plan or key chosen in Settings stays the choice while its key is missing,
        # never a silent switch to another bill.
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
    """FrontierAgent's keyword arguments for ``config``."""
    return {"max_steps": config.max_steps, "max_seconds": config.max_seconds, "screenshots": config.screenshots,
            "settle_seconds": config.settle_seconds, "max_cost_usd": max_cost_usd}


def build_client(config=None, *, key=None):
    """The Smart model's client on the user's key."""
    from .model_clients import chat_client
    config = smart_config() if config is None else config
    return chat_client(config.model, reasoning=config.reasoning, key=key or None)


def build_frontier(driver, client, *, apps, emit, max_cost_usd=None, approve=None, cancelled=None, config=SMART_CONFIG,
                   guard=None, skills=None, output_schema=None, context_blocks=(), turn_context=None,
                   steering=None, clarify=None, step_observer=None, checkpoint=None, max_steps=None,
                   max_seconds=None, run_context=None, commit_check=None, dialog_approve=None, longform=None,
                   forced_approve=None, permissions=None, floor_approve=None,
                   permission_mode=None, saved_program=None, program_parts=None, dialogs=None,
                   dialog_policy=None, recorder=None):
    """The Smart agent: FrontierAgent with ``config``'s arguments. ``recorder``: traces.Recording.attach, which returns
    the driver and client the agent runs on (a tape on, the pair itself off)."""
    from .frontier import FrontierAgent
    kwargs = frontier_kwargs(config, max_cost_usd=max_cost_usd)
    if max_steps is not None:
        kwargs["max_steps"] = int(max_steps)
    if max_seconds is not None:
        kwargs["max_seconds"] = max_seconds
    if skills:
        # The tool policy (harness/tools): registered tools only when available, interactive ones only when a person
        # watches, at most 8.
        skills = harness_api.hooks().tool_policy(skills, run_context=run_context, driver=driver, guard=guard)
    hooks = {name: value for name, value in (
        ("context_blocks", tuple(context_blocks or ())), ("turn_context", turn_context), ("steering", steering),
        ("clarify", clarify), ("step_observer", step_observer), ("checkpoint", checkpoint),
        ("run_context", run_context), ("commit_check", commit_check), ("dialog_approve", dialog_approve),
        ("longform", longform), ("forced_approve", forced_approve),
        ("permissions", permissions), ("floor_approve", floor_approve),
        ("permission_mode", permission_mode), ("saved_program", saved_program),
        ("program_parts", program_parts), ("dialogs", dialogs), ("dialog_policy", dialog_policy)) if value}
    if recorder is not None:
        driver, client = recorder(driver, client, apps=apps, frontier={**kwargs, "output_schema": output_schema},
                                  hooks=[*hooks, *(("approve",) if approve else ()), *(("guard",) if guard else ())])
    return FrontierAgent(driver, client, apps=apps, emit=emit, approve=approve, cancelled=cancelled, guard=guard,
                         skills=skills, output_schema=output_schema, **hooks, **kwargs)


def acquire_client(key):
    """The Smart model's client for one run (seam S6.2): today, a new one on ``key`` (``build_client``)."""
    return build_client(key=key)


def release_client(client):
    """A run is done with ``client``: close its connection."""
    connection = getattr(client, "connection", None)
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass


def smart_key(env=None):
    """The key Smart uses: the one of ``smart_model``'s provider."""
    env = os.environ if env is None else env
    provider = model_provider(smart_model(env))
    if provider == "anthropic":
        return anthropic_key(env)
    providers = harness_api.hooks().model_routes()
    if providers.get(provider) is not None:
        key = providers.credential(providers.get(provider), env)
        return None if key is None else (key or "local-server-no-key")
    return openai_smart_key(env)


def openai_smart_key(env=None):
    """The OpenAI key Smart uses: OPENAI_API_KEY, else the helper's key when the helper is OpenAI."""
    env = os.environ if env is None else env
    key = (env.get("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    helper, source = harness_api.hooks().key("openai", {name: value for name, value in env.items()
                                                       if name != "OPENAI_API_KEY"})
    if source != "helper":
        return None
    return helper.strip() or None


# The Mac app reads this sentence as a missing key (keyMissing).
NO_SMART_KEY = "Connect your AI account in Setup"
NO_CREDIT = "Your OpenAI account is out of credit. Add credit at platform.openai.com, then try again."
NO_ANTHROPIC_CREDIT = "Your Claude account is out of credit. Add credit at platform.claude.com, then try again."
CREDIT_REASONS = (NO_CREDIT, NO_ANTHROPIC_CREDIT)
CREDIT_CODES = ("insufficient_quota", "credit_balance_exhausted")
# Anthropic's no-credit answers: a 400 "credit balance is too low", or a 402 billing_error.
ANTHROPIC_CREDIT = ("credit balance is too low", "billing_error")
PROVIDER_ERROR = re.compile(r"(OpenAI|Anthropic) HTTP (\d{3})")


def no_key_reason(managed=True, model=None, env=None):
    """Why Smart can't run without a key."""
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
    """Whether a model error (its status and body, as a run's reason carries them) says the account has no credit."""
    text = str(text or "")
    return (any(code in text for code in CREDIT_CODES + ANTHROPIC_CREDIT) or "OpenAI HTTP 402" in text
            or "Anthropic HTTP 402" in text)


class ModelReach:
    """Whether the saved key can run Smart's model, cached per key and model; unknown counts as reachable."""

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
        """What a run or a key test found."""
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
        """(True, None), (False, why) or (None, None) when the answer is unknown."""
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
    """(False, why) when a run's error says Smart can't run on this key, else (None, None)."""
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
    """Which model-provider failure ended a run."""
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
    """A run error in words a person can act on, or None."""
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
