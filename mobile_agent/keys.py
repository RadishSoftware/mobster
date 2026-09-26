"""Bring your own keys: Jev and the helper model, managed from the desktop app.

Keys live in the agent's private env file (0600) and in this process's
environment, never in responses, events or logs: the API shows only whether a
key is set and its last four characters. Helpers are any OpenAI-compatible chat
endpoint (a preset or a custom base URL) or Vertex AI through the user's own
gcloud login.
"""

import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .device_manager import update_env_file

JEV_URL = "https://api.typesafe.ai/v1"
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
HELPER_ENV = ("MOBSTER_HELPER_PROVIDER", "TEXT_MODEL_PROVIDER", "TEXT_MODEL_BASE_URL", "TEXT_MODEL_API_KEY",
              "TEXT_MODEL", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")


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
        return {
            "jev": {"configured": bool(env.get("TYPESAFE_API_KEY")), "keyHint": hint(env.get("TYPESAFE_API_KEY"))},
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
        if not isinstance(body, dict) or not body or set(body) - {"jev", "helper"}:
            raise ValueError("Send jev and/or helper settings")
        changes = {}
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
        """{"ok": bool, "message": str}: one tiny request with the saved key."""
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
        raise ValueError("target must be jev or helper")

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
