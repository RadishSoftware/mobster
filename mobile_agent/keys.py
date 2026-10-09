"""Bring your own keys: OpenAI or Anthropic (Smart), Jev (Fast) and the helper model, managed from the desktop app.

Keys live in the agent's private env file (0600) and in this process's
environment, never in responses, events or logs: the API shows only whether a
key is set and its last four characters. Helpers are any OpenAI-compatible chat
endpoint (a preset or a custom base URL) or Vertex AI through the user's own
gcloud login.

Smart runs on OpenAI with OPENAI_API_KEY. A helper already set up with an OpenAI
key counts too (engines.py falls back to TEXT_MODEL_API_KEY when the helper's
provider is openai), so nobody pastes the same key twice. With only
ANTHROPIC_API_KEY, Smart runs on Claude (engines.smart_model); MOBSTER_SMART_MODEL
picks the model outright. With both keys, MOBSTER_SMART_PROVIDER (set by Setup's
"Connect your AI account" or Settings, ``smartProvider`` here) says which one Smart
uses; unset, OpenAI's, as before.
"""

import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .device_manager import update_env_file

JEV_URL = "https://api.typesafe.ai/v1"
OPENAI_URL = "https://api.openai.com/v1"
# The model Smart runs on; the key test asks OpenAI whether this key can use it.
SMART_MODEL = "gpt-5.6-sol"
OPENAI_BILLING_URL = "https://platform.openai.com/settings/organization/billing/overview"
ANTHROPIC_URL = "https://api.anthropic.com/v1"
ANTHROPIC_BILLING_URL = "https://platform.claude.com/settings/billing"
KEY_PATTERN = re.compile(r"[\x21-\x7e]{16,400}")  # printable, no spaces
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")
# The same rules the Vertex client enforces (gemini.VertexHTTP).
PROJECT_PATTERN = re.compile(r"[a-z][a-z0-9-]{4,61}[a-z0-9]|[0-9]{6,20}")
LOCATION_PATTERN = re.compile(r"global|[a-z]+-[a-z]+[0-9]")

# Every value these presets need; "custom" takes a base URL, "vertex" a project.
PROVIDERS = (
    {"id": "openrouter", "label": "OpenRouter", "baseUrl": "https://openrouter.ai/api/v1",
     "modelHint": "google/gemini-2.5-flash", "keyUrl": "https://openrouter.ai/keys"},
    {"id": "openai", "label": "OpenAI", "baseUrl": "https://api.openai.com/v1",
     "modelHint": "gpt-4.1-mini", "keyUrl": "https://platform.openai.com/api-keys"},
    {"id": "google", "label": "Google AI Studio", "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
     "modelHint": "gemini-2.5-flash", "keyUrl": "https://aistudio.google.com/apikey"},
    {"id": "anthropic", "label": "Anthropic", "baseUrl": "https://api.anthropic.com/v1",
     "modelHint": "claude-sonnet-5", "keyUrl": "https://console.anthropic.com/settings/keys"},
    {"id": "groq", "label": "Groq", "baseUrl": "https://api.groq.com/openai/v1",
     "modelHint": "llama-3.3-70b-versatile", "keyUrl": "https://console.groq.com/keys"},
    {"id": "cerebras", "label": "Cerebras", "baseUrl": "https://api.cerebras.ai/v1",
     "modelHint": "llama-3.3-70b", "keyUrl": "https://cloud.cerebras.ai"},
    {"id": "custom", "label": "Other OpenAI-compatible", "baseUrl": None, "modelHint": "model-name", "keyUrl": None},
    {"id": "vertex", "label": "Google Cloud Vertex AI", "baseUrl": None, "modelHint": "gemini-3.5-flash-lite",
     "keyUrl": None},
)
PRESETS = {provider["id"]: provider for provider in PROVIDERS}
LOCAL_KEY = "local-server-no-key"
# Which key Smart uses when both are saved (engines.smart_model); unset keeps the default (OpenAI's).
SMART_PROVIDERS = ("anthropic", "openai")
HELPER_ENV = ("MOBSTER_HELPER_PROVIDER", "TEXT_MODEL_PROVIDER", "TEXT_MODEL_BASE_URL", "TEXT_MODEL_API_KEY",
              "TEXT_MODEL", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")


def _error_code(error):
    """The ``error.code`` an OpenAI-style error body carries ("insufficient_quota", "model_not_found"), or None."""
    try:
        data = json.loads(error.read(65536) or b"{}")
    except (OSError, ValueError, AttributeError):
        return None
    detail = data.get("error") if isinstance(data, dict) else None
    code = detail.get("code") or detail.get("type") if isinstance(detail, dict) else None
    return code if isinstance(code, str) else None


def _anthropic_error(error):
    """(type, message) of an Anthropic error body ({"type": "error", "error": {"type", "message"}})."""
    try:
        data = json.loads(error.read(65536) or b"{}")
    except (OSError, ValueError, AttributeError):
        return None, ""
    detail = data.get("error") if isinstance(data, dict) else None
    if not isinstance(detail, dict):
        return None, ""
    return detail.get("type"), str(detail.get("message") or "")


def hint(key):
    if key == LOCAL_KEY:
        return "not needed"
    return f"…{key[-4:]}" if key and len(key) >= 8 else None


def base_url(value):
    parts = urlsplit(value or "")
    local = parts.hostname in {"127.0.0.1", "localhost"}
    if parts.scheme not in {"https"} | ({"http"} if local else set()) or not parts.hostname or parts.username \
            or parts.password or parts.query or parts.fragment or len(value) > 300:
        raise ValueError("Use an https:// base URL (http:// only for this Mac), like https://api.example.com/v1")
    return value.rstrip("/")


def openai_key(env=os.environ):
    """(key, "own" | "helper") for Smart, or (None, None): OPENAI_API_KEY, else an OpenAI helper's key."""
    if env.get("OPENAI_API_KEY"):
        return env["OPENAI_API_KEY"], "own"
    if current_provider(env) == "openai" and env.get("TEXT_MODEL_API_KEY") \
            and env.get("TEXT_MODEL_API_KEY") != LOCAL_KEY:
        return env["TEXT_MODEL_API_KEY"], "helper"
    return None, None


def anthropic_key(env=os.environ):
    """(key, "own" | "helper") for Smart on Claude, or (None, None): ANTHROPIC_API_KEY, else an Anthropic helper's
    key (the helper preset talks to the same API)."""
    if (env.get("ANTHROPIC_API_KEY") or "").strip():
        return env["ANTHROPIC_API_KEY"].strip(), "own"
    if current_provider(env) == "anthropic" and env.get("TEXT_MODEL_API_KEY") \
            and env.get("TEXT_MODEL_API_KEY") != LOCAL_KEY:
        return env["TEXT_MODEL_API_KEY"], "helper"
    return None, None


def current_provider(env=os.environ):
    saved = env.get("MOBSTER_HELPER_PROVIDER")
    if saved in PRESETS:
        return saved
    if env.get("TEXT_MODEL_PROVIDER") == "vertex":
        return "vertex"
    url = (env.get("TEXT_MODEL_BASE_URL") or PRESETS["openrouter"]["baseUrl"]).rstrip("/")
    return next((p["id"] for p in PROVIDERS if p["baseUrl"] == url), "custom") if env.get("TEXT_MODEL") else None


class Keys:
    def __init__(self, env_file):
        self.env_file = env_file

    def state(self):
        env = os.environ
        provider = current_provider(env)
        helper_key = env.get("TEXT_MODEL_API_KEY")
        vertex = provider == "vertex"
        configured = bool(env.get("TEXT_MODEL")) and (bool(env.get("GOOGLE_CLOUD_PROJECT")) if vertex else bool(helper_key))
        openai, source = openai_key(env)
        from .engines import ANTHROPIC_SMART_MODEL, model_provider, smart_model
        anthropic, anthropic_source = anthropic_key(env)
        claude = smart_model(env) if model_provider(smart_model(env)) == "anthropic" else ANTHROPIC_SMART_MODEL
        return {
            "openai": {"configured": bool(openai), "keyHint": hint(openai), "fromHelper": source == "helper",
                       "model": SMART_MODEL},
            "anthropic": {"configured": bool(anthropic), "keyHint": hint(anthropic),
                          "fromHelper": anthropic_source == "helper", "model": claude},
            "jev": {"configured": bool(env.get("TYPESAFE_API_KEY")), "keyHint": hint(env.get("TYPESAFE_API_KEY"))},
            # Whose account Smart runs on now (None: no key yet), and the saved choice for when both keys exist.
            "smart": {"provider": model_provider(smart_model(env)) if (openai or anthropic) else None,
                      "preference": env.get("MOBSTER_SMART_PROVIDER") if env.get("MOBSTER_SMART_PROVIDER") in SMART_PROVIDERS
                      else None},
            "helper": {"configured": configured, "provider": provider, "model": env.get("TEXT_MODEL") or None,
                       "baseUrl": None if vertex else (env.get("TEXT_MODEL_BASE_URL") or
                                                       (PRESETS["openrouter"]["baseUrl"] if provider else None)),
                       "keyHint": None if vertex else hint(helper_key),
                       "project": env.get("GOOGLE_CLOUD_PROJECT") if vertex else None,
                       "location": env.get("GOOGLE_CLOUD_LOCATION") if vertex else None},
            "providers": [dict(provider) for provider in PROVIDERS],
            "persisted": self.env_file is not None,
        }

    def _apply(self, changes):
        if self.env_file:
            update_env_file(self.env_file, changes)
        for key, value in changes.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def update(self, body):
        if not isinstance(body, dict) or not body or set(body) - {"openai", "anthropic", "jev", "helper", "smartProvider"}:
            raise ValueError("Send openai, anthropic, jev, helper and/or smartProvider settings")
        changes = {}
        if "smartProvider" in body:
            if body["smartProvider"] is not None and body["smartProvider"] not in SMART_PROVIDERS:
                raise ValueError("smartProvider must be anthropic, openai or null")
            changes["MOBSTER_SMART_PROVIDER"] = body["smartProvider"]
        if "openai" in body:
            openai = body["openai"]
            if openai is None:
                changes["OPENAI_API_KEY"] = None
            else:
                if not isinstance(openai, dict) or set(openai) != {"key"} or not isinstance(openai["key"], str) \
                        or not KEY_PATTERN.fullmatch(openai["key"].strip()):
                    raise ValueError("That doesn't look like an OpenAI key. Copy it again without spaces.")
                changes["OPENAI_API_KEY"] = openai["key"].strip()
        if "anthropic" in body:
            anthropic = body["anthropic"]
            if anthropic is None:
                changes["ANTHROPIC_API_KEY"] = None
            else:
                if not isinstance(anthropic, dict) or set(anthropic) != {"key"} or not isinstance(anthropic["key"], str) \
                        or not KEY_PATTERN.fullmatch(anthropic["key"].strip()):
                    raise ValueError("That doesn't look like a Claude key. Copy it again without spaces.")
                changes["ANTHROPIC_API_KEY"] = anthropic["key"].strip()
        if "jev" in body:
            jev = body["jev"]
            if jev is None:
                changes["TYPESAFE_API_KEY"] = None
            else:
                if not isinstance(jev, dict) or set(jev) != {"key"} or not isinstance(jev["key"], str) \
                        or not KEY_PATTERN.fullmatch(jev["key"].strip()):
                    raise ValueError("That doesn't look like a Jev key. Copy it again without spaces.")
                changes["TYPESAFE_API_KEY"] = jev["key"].strip()
        if "helper" in body:
            changes.update(self._helper_changes(body["helper"]))
        self._apply(changes)
        return self.state()

    def _helper_changes(self, helper):
        if helper is None:
            return {key: None for key in HELPER_ENV}
        allowed = {"provider", "model", "key", "baseUrl", "project", "location"}
        if not isinstance(helper, dict) or set(helper) - allowed or helper.get("provider") not in PRESETS:
            raise ValueError("Choose a helper provider")
        provider = helper["provider"]
        model = helper.get("model")
        if not isinstance(model, str) or not MODEL_PATTERN.fullmatch(model.strip()):
            raise ValueError("Enter the model name, like " + PRESETS[provider]["modelHint"])
        changes = {"MOBSTER_HELPER_PROVIDER": provider, "TEXT_MODEL": model.strip()}
        if provider == "vertex":
            project, location = helper.get("project"), helper.get("location") or "global"
            if not isinstance(project, str) or not PROJECT_PATTERN.fullmatch(project.strip()):
                raise ValueError("Enter your Google Cloud project ID")
            if not isinstance(location, str) or not LOCATION_PATTERN.fullmatch(location):
                raise ValueError("Enter a Vertex location like global or us-central1")
            return {**changes, "TEXT_MODEL_PROVIDER": "vertex", "GOOGLE_CLOUD_PROJECT": project.strip(),
                    "GOOGLE_CLOUD_LOCATION": location, "TEXT_MODEL_API_KEY": None, "TEXT_MODEL_BASE_URL": None}
        url = base_url(helper.get("baseUrl")) if provider == "custom" else PRESETS[provider]["baseUrl"]
        key = helper.get("key")
        local = urlsplit(url).hostname in {"127.0.0.1", "localhost"}
        # A saved key is only ever sent to the endpoint it was entered for.
        same_endpoint = current_provider() == provider and (
            provider != "custom" or os.environ.get("TEXT_MODEL_BASE_URL") == url)
        if key is None or key == "":
            if local and (not same_endpoint or not os.environ.get("TEXT_MODEL_API_KEY")):
                # Ollama and LM Studio need no key; the helper client still sends a bearer value.
                key = LOCAL_KEY
            elif (not os.environ.get("TEXT_MODEL_API_KEY") or not same_endpoint
                  or os.environ.get("TEXT_MODEL_API_KEY") == LOCAL_KEY):
                # Keep the saved key when only the model changes on the same endpoint.
                raise ValueError("Paste the API key for " + PRESETS[provider]["label"])
            else:
                key = None
        elif not isinstance(key, str) or not KEY_PATTERN.fullmatch(key.strip()):
            raise ValueError("That doesn't look like an API key. Copy it again without spaces.")
        changes.update({"TEXT_MODEL_PROVIDER": None, "TEXT_MODEL_BASE_URL": url,
                        "GOOGLE_CLOUD_PROJECT": None, "GOOGLE_CLOUD_LOCATION": None})
        if key is not None:
            changes["TEXT_MODEL_API_KEY"] = key.strip()
        return changes

    # -- live checks ------------------------------------------------------------------

    def test(self, target, opener=urllib.request.urlopen):
        """{"ok": bool, "message": str, "link"?: {label, url}}: one or two tiny requests with the saved key."""
        if target == "openai":
            key, _ = openai_key()
            if not key:
                return {"ok": False, "message": "Add your OpenAI key first."}
            return self._openai_check(opener, key)
        if target == "anthropic":
            key, _ = anthropic_key()
            if not key:
                return {"ok": False, "message": "Add your Claude key first."}
            return self._anthropic_check(opener, key, self.state()["anthropic"]["model"])
        if target == "jev":
            key = os.environ.get("TYPESAFE_API_KEY")
            if not key:
                return {"ok": False, "message": "Add your Jev key first."}
            body = {"model": os.environ.get("TYPESAFE_MODEL", "jev-latest"), "state": {"text": "Connection check"},
                    "questions": {"ok": {"type": "noul", "instructions": "Is this a connection check?"}}}
            return self._probe(opener, JEV_URL + "/systemone", key, body, "Jev")
        if target == "helper":
            state = self.state()["helper"]
            if not state["configured"]:
                return {"ok": False, "message": "Add a helper model first."}
            if state["provider"] == "vertex":
                return self._vertex_check()
            body = {"model": state["model"], "max_tokens": 16,
                    "messages": [{"role": "user", "content": 'Reply with exactly {"ok":true}'}]}
            return self._probe(opener, state["baseUrl"] + "/chat/completions", os.environ["TEXT_MODEL_API_KEY"],
                               body, PRESETS.get(state["provider"], {}).get("label", "The helper"))
        raise ValueError("target must be openai, anthropic, jev or helper")

    @staticmethod
    def _openai_check(opener, key):
        """Two requests: a free read of Smart's model, then the smallest possible generation.

        The read proves the key is valid and its project may use the model, but OpenAI only enforces quota
        when it generates, so a new account with no credit passes it. The generation (16 output tokens at
        low effort, a small fraction of a cent) fails with insufficient_quota there, here in setup instead
        of inside the first task."""
        headers = {"Authorization": f"Bearer {key}", "User-Agent": "Mobster"}
        body = {"model": SMART_MODEL, "input": "Reply with OK.", "max_output_tokens": 16,
                "reasoning": {"effort": "low"}, "store": False}
        requests = (
            urllib.request.Request(f"{OPENAI_URL}/models/{SMART_MODEL}", method="GET", headers=headers),
            urllib.request.Request(f"{OPENAI_URL}/responses", json.dumps(body).encode(), method="POST",
                                   headers={**headers, "Content-Type": "application/json"}),
        )
        try:
            for request in requests:
                with opener(request, timeout=30) as response:
                    response.read(65536)
            return {"ok": True, "message": f"OpenAI accepted the key, and it can run {SMART_MODEL}."}
        except urllib.error.HTTPError as error:
            code = _error_code(error)
            if error.code == 401:
                return {"ok": False, "message": "OpenAI didn't accept this key. Copy it again from the OpenAI Platform: "
                                                "it starts with sk-.", "problem": "rejected"}
            if code in ("insufficient_quota", "credit_balance_exhausted") or error.code == 402:
                # ``problem`` tells the server Smart can't run until a test passes (Runtime.check_key).
                return {"ok": False, "message": "Your OpenAI account has no credit yet. Add $5 in Billing, then test again.",
                        "link": {"label": "Add credit", "url": OPENAI_BILLING_URL}, "problem": "no_credit"}
            if error.code in (403, 404) or code == "model_not_found":
                return {"ok": False, "message": f"This key's project can't use {SMART_MODEL}, the model Mobster's agent "
                                                "runs on. Allow it in OpenAI › Settings › Project › Limits, then test again.",
                        "problem": "no_model"}
            if error.code == 429:
                return {"ok": True, "message": "OpenAI accepted the key but is rate limiting it right now."}
            return {"ok": False, "message": f"OpenAI answered with an error ({error.code}). Try again in a minute.",
                    "problem": "provider_error"}
        except (urllib.error.URLError, TimeoutError, OSError):
            return {"ok": False, "message": "Mobster couldn't reach OpenAI. Check your internet connection, then try again.",
                    "problem": "offline"}

    @staticmethod
    def _anthropic_check(opener, key, model):
        """As ``_openai_check``, on the Claude API: a free read of the model, then the smallest generation (16 output
        tokens at the least thinking the model takes), which fails there, not in the first task, on an account
        with no credit."""
        from .frontier import ANTHROPIC_VERSION, anthropic_thinking
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "User-Agent": "Mobster"}
        body = {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "Reply with OK."}]}
        thinking, effort = anthropic_thinking(model, "none")
        if thinking is not None:
            body["thinking"] = thinking
        if effort is not None:
            body["output_config"] = {"effort": effort}
        requests = (
            urllib.request.Request(f"{ANTHROPIC_URL}/models/{model}", method="GET", headers=headers),
            urllib.request.Request(f"{ANTHROPIC_URL}/messages", json.dumps(body).encode(), method="POST",
                                   headers={**headers, "Content-Type": "application/json"}),
        )
        try:
            for request in requests:
                with opener(request, timeout=30) as response:
                    response.read(65536)
            return {"ok": True, "message": f"Claude accepted the key, and it can run {model}."}
        except urllib.error.HTTPError as error:
            kind, message = _anthropic_error(error)
            if error.code == 401:
                return {"ok": False, "message": "Claude didn't accept this key. Copy it again from the Claude Console: "
                                                "it starts with sk-ant-.", "problem": "rejected"}
            if error.code == 402 or kind == "billing_error" or "credit balance is too low" in message:
                return {"ok": False, "message": "Your Claude account has no credit yet. Add $5 in Billing, then "
                                                "test again.",
                        "link": {"label": "Add credit", "url": ANTHROPIC_BILLING_URL}, "problem": "no_credit"}
            if error.code in (403, 404):
                return {"ok": False, "message": f"This key can't use {model}, the model Mobster's agent runs on. Check "
                                                "its workspace in the Claude Console, then test again.",
                        "problem": "no_model"}
            if error.code in (429, 529):
                return {"ok": True, "message": "Claude accepted the key but is busy or rate limiting it right now."}
            return {"ok": False, "message": f"Anthropic answered with an error ({error.code}). Try again in a minute.",
                    "problem": "provider_error"}
        except (urllib.error.URLError, TimeoutError, OSError):
            return {"ok": False, "message": "Mobster couldn't reach Claude. Check your internet connection, then try "
                                            "again.", "problem": "offline"}

    @staticmethod
    def _probe(opener, url, key, body, name):
        request = urllib.request.Request(url, json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "Mobster"})
        try:
            with opener(request, timeout=20) as response:
                response.read(65536)
            return {"ok": True, "message": f"{name} accepted the key."}
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                return {"ok": False, "message": f"{name} rejected the key. Check it and paste it again."}
            if error.code == 404:
                return {"ok": False, "message": f"{name} could not find that model or address. Check the model name."}
            if error.code == 429 and _error_code(error) == "insufficient_quota":
                return {"ok": False, "message": f"{name} accepted the key, but the account has no credit. Add some, then test again."}
            if error.code == 429:
                return {"ok": True, "message": f"{name} accepted the key but is rate limiting it right now."}
            return {"ok": False, "message": f"{name} answered with an error ({error.code}). Check the model name."}
        except (urllib.error.URLError, TimeoutError, OSError):
            return {"ok": False, "message": f"Could not reach {name}. Check your internet connection."}

    @staticmethod
    def _vertex_check():
        from .gemini import GCloudToken
        try:
            GCloudToken.get(timeout=15)
            return {"ok": True, "message": "Your gcloud login works for Vertex AI."}
        except Exception:
            return {"ok": False, "message": "Vertex AI uses the gcloud CLI. Install it and run gcloud auth login, then test again."}
