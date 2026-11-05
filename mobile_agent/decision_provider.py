"""Decision models behind one seam: typed closed questions answered with probabilities, no text generated."""

import base64
import http.client
import json
import os
import threading
import time
from dataclasses import dataclass, field

JEV_BASE_URL = "https://api.typesafe.ai/v1"

# USD per 1M input tokens (output is free on both), read 2026-10-09.
DECISION_PRICES = {"gpt-6-luna": 0.10, "jev-latest": 0.042}
OPENAI_HOST = "api.openai.com"
# A single choice question's option cap (Jev documents 255). OpenAI's is measured by bench/decide_gate.py.
MAX_OPTIONS = 255


@dataclass(frozen=True)
class Question:
    kind: str                     # "choice" | "yesno"
    instructions: str
    options: dict = field(default_factory=dict)   # choice: {value: description}

    def __post_init__(self):
        if self.kind not in ("choice", "yesno"):
            raise ValueError(f"unknown question kind {self.kind!r}")
        if self.kind == "choice" and not 2 <= len(self.options) <= MAX_OPTIONS:
            raise ValueError("a choice needs 2 to 255 options")


@dataclass(frozen=True)
class Answer:
    kind: str
    choice: str = None            # choice: the winning value
    probabilities: dict = field(default_factory=dict)
    yes: float = None             # yesno: P(yes)
    confidence: float = None

    @property
    def top(self):
        """The winning probability (choice) or the probability of the answer given (yesno)."""
        if self.kind == "choice":
            return self.probabilities.get(self.choice)
        return None if self.yes is None else max(self.yes, 1 - self.yes)


def _prob(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError("bad probability")
    return float(value)


def parse_choice(raw, options):
    """An Answer for a choice, or None when it is malformed or names a value that was not offered."""
    try:
        probs = raw["probabilities"]
        if isinstance(probs, list):
            mapped = {item["value"]: item["probability"] for item in probs}
            if len(mapped) != len(probs):
                return None
            probs = mapped
        probs = {str(k): _prob(v) for k, v in probs.items()}
        choice = str(raw["choice"])
        if choice not in options or not set(probs) <= set(options) or abs(sum(probs.values()) - 1) > .03:
            return None
        if probs.get(choice, 0) < max(probs.values()) - 1e-6:
            return None
        confidence = raw.get("confidence")
        return Answer("choice", choice=choice, probabilities=probs,
                      confidence=_prob(confidence) if confidence is not None else None)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def parse_yesno(raw):
    try:
        value = raw.get("probability", raw.get("noul"))
        return Answer("yesno", yes=_prob(value), confidence=_prob(raw["confidence"]) if "confidence" in raw else None)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


class DecisionProvider:
    name = "decision"
    model = ""
    sees_images = False

    def __init__(self):
        self.calls, self.input_tokens, self.latencies = 0, 0, []
        self._lock = threading.Lock()

    def ask(self, context, questions, image=None, timeout=20):
        raise NotImplementedError

    def _record(self, started, usage):
        with self._lock:
            self.calls += 1
            self.input_tokens += int((usage or {}).get("input_tokens") or 0)
            self.latencies.append(time.monotonic() - started)

    @property
    def cost_usd(self):
        return self.input_tokens * DECISION_PRICES.get(self.model, 0.0) / 1e6


class OpenAIDecisions(DecisionProvider):
    name = "openai"
    sees_images = True

    def __init__(self, model="gpt-6-luna", key=None, host=OPENAI_HOST):
        super().__init__()
        self.model, self.host = model, host
        self.key = key or os.environ.get("OPENAI_API_KEY", "")
        if not self.key:
            raise ValueError("OPENAI_API_KEY is not set")

    def body(self, context, questions, image=None):
        content = [{"type": "input_text", "text": context}]
        if image is not None:
            data = image if isinstance(image, str) else "data:image/jpeg;base64," + base64.b64encode(image).decode()
            content.append({"type": "input_image", "image_url": data})
        wire = []
        for name, q in questions.items():
            if q.kind == "choice":
                wire.append({"type": "choice", "name": name, "instructions": q.instructions,
                             "choices": [{"value": k, "description": v} for k, v in q.options.items()]})
            else:
                wire.append({"type": "predicate", "name": name, "instructions": q.instructions})
        return {"model": self.model, "input": [{"role": "user", "content": content}], "questions": wire}

    def ask(self, context, questions, image=None, timeout=20):
        payload = json.dumps(self.body(context, questions, image)).encode()
        started = time.monotonic()
        connection = http.client.HTTPSConnection(self.host, timeout=timeout)
        try:
            connection.request("POST", "/v1/decisions", payload,
                               {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        if response.status != 200:
            raise RuntimeError(f"OpenAI decisions HTTP {response.status}: {raw[:300].decode(errors='replace')}")
        data = json.loads(raw)
        self._record(started, data.get("usage"))
        by_name = {a.get("name"): a for a in data.get("answers") or () if isinstance(a, dict)}
        return {name: (parse_choice(by_name.get(name), q.options) if q.kind == "choice"
                       else parse_yesno(by_name.get(name))) if name in by_name else None
                for name, q in questions.items()}


class JevDecisions(DecisionProvider):
    name = "jev"
    sees_images = False

    def __init__(self, model=None, key=None):
        super().__init__()
        from .http_pool import pooled_http
        key = key or os.environ.get("TYPESAFE_API_KEY")
        if not key:
            raise ValueError("TYPESAFE_API_KEY is not set")
        self.model = model or os.environ.get("TYPESAFE_MODEL", "jev-latest")
        self.http = pooled_http(JEV_BASE_URL, key)

    def ask(self, context, questions, image=None, timeout=20):
        wire = {}
        for name, q in questions.items():
            if q.kind == "choice":
                wire[name] = {"type": "choice", "criteria": dict(q.options), "instructions": q.instructions}
            else:
                wire[name] = {"type": "noul", "instructions": q.instructions}
        started = time.monotonic()
        response = self.http.request("POST", "/systemone", {"model": self.model, "state": {"context": context},
                                                           "questions": wire}, timeout)
        self._record(started, (response or {}).get("usage"))
        answers = (response or {}).get("answers") or {}
        return {name: (parse_choice(answers.get(name), q.options) if q.kind == "choice"
                       else parse_yesno(answers.get(name))) if name in answers else None
                for name, q in questions.items()}


def provider(name, **kwargs):
    if name == "openai":
        return OpenAIDecisions(**kwargs)
    if name == "jev":
        return JevDecisions(**kwargs)
    raise ValueError(f"unknown decision provider {name!r}")
