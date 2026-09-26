"""Explicit helper selection and model-specific thinking, never process-env mutation.

Google model capabilities checked 2026-09-19:
https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-5-flash-lite
https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/guides/gemini-3-7-flash
https://ai.google.dev/gemini-api/docs/thinking
"""

import os
import re


# Single home for Gemini model IDs this codebase names. Product selection,
# eval candidates, and thinking configs must not drift across modules.
GEMINI_MODELS = ("gemini-3.5-flash-lite", "gemini-3.7-flash", "gemini-3.1-pro-preview")
EVAL_MODELS = ("gemini-2.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.5-flash")
THINKING_CONFIGS = {
    "gemini-3.5-flash-lite": {"thinkingLevel": "MINIMAL"},
    "gemini-3.7-flash": {"thinkingLevel": "LOW"},
    "gemini-3.1-pro-preview": {"thinkingLevel": "LOW"},
    "gemini-2.5-flash-lite": {"thinkingBudget": 0},
    "gemini-2.5-flash": {"thinkingBudget": 0},
}


def validate_model_request(model):
    if model is not None and (not isinstance(model, str) or len(model) > 128
                              or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", model)):
        raise ValueError("Choose a supported helper model or the configured default")


def configured_model():
    model = os.environ.get("TEXT_MODEL") or None
    validate_model_request(model)
    return model


def model_options():
    default = configured_model()
    choices = [default] if default is not None else []
    # Do not offer bare Vertex model IDs to a differently configured provider.
    if os.environ.get("TEXT_MODEL_PROVIDER") == "vertex":
        choices.extend(model for model in GEMINI_MODELS if model not in choices)
    return choices


def resolve_helper_model(model=None):
    validate_model_request(model)
    if model is None:
        return configured_model()
    if model not in model_options():
        raise ValueError("This helper model is not available in the configured model list")
    return model


def thinking_config(model):
    # Unknown configured models use provider defaults. Never infer capabilities
    # from a version prefix (2.5 Pro cannot disable thinking; 3.7 has no MINIMAL).
    config = THINKING_CONFIGS.get(model)
    return {"thinkingConfig": dict(config)} if config is not None else {}
