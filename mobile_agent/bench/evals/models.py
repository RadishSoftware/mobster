"""Model calls for the L1 runners: one JSON-answering interface over Claude, OpenAI and Jev, timed and priced, under a
spend cap kept in a ledger file. Keys come from env files and are never printed or written anywhere.

``ask(model, system, parts, schema)`` returns ``Reply(out, usd, seconds, usage, error)``. ``parts``: text strings or
``("image", path)`` tuples. ``Spend`` refuses a call once the ledger's total plus this run's spend reaches the cap.
"""

import dataclasses
import json
import os
import pathlib
import threading
import time

from ... import frontier


@dataclasses.dataclass
class Reply:
    out: dict = None
    usd: float = 0.0
    seconds: float = 0.0
    usage: dict = None
    error: str = None


class OverBudget(RuntimeError):
    pass


class Spend:
    """A run's spend, checked against ``cap`` together with every earlier run in ``ledger`` (JSONL of {"usd": x})."""

    def __init__(self, ledger, cap, label):
        self.ledger, self.cap, self.label = pathlib.Path(ledger).expanduser() if ledger else None, cap, label
        self.before = self._total()
        self.usd = 0.0
        self.lock = threading.Lock()

    def _total(self):
        if not self.ledger or not self.ledger.exists():
            return 0.0
        total = 0.0
        for line in self.ledger.read_text().splitlines():
            try:
                total += float(json.loads(line).get("usd") or 0)
            except ValueError:
                pass
        return total

    def check(self):
        if self.cap is not None and self.before + self.usd >= self.cap:
            raise OverBudget(f"spend cap ${self.cap:.2f} reached (${self.before + self.usd:.2f} so far)")

    def add(self, usd):
        with self.lock:
            self.usd += usd or 0.0

    def close(self, extra=None):
        if self.ledger and self.usd:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            with open(self.ledger, "a") as handle:
                handle.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "what": self.label,
                                         "usd": round(self.usd, 5), **(extra or {})}) + "\n")


def load_keys(paths):
    """Load env files into this process (values are never echoed)."""
    from ...config import load_env_file
    for path in paths or ():
        path = os.path.expanduser(path)
        if os.path.exists(path):
            load_env_file(path)


_clients, _lock = {}, threading.Lock()


def client(model, reasoning="low"):
    key = (model, reasoning)
    with _lock:
        if key not in _clients:
            _clients[key] = frontier.chat_client(model, reasoning=reasoning)
        return _clients[key]


def _image(path):
    data = pathlib.Path(path).read_bytes()
    mime = "image/png" if str(path).lower().endswith(".png") else "image/jpeg"
    return frontier.image_part(mime, data)


def messages(system, parts):
    content = []
    for part in parts:
        if isinstance(part, tuple) and part[0] == "image":
            if part[1]:
                content.append(_image(part[1]))
        elif isinstance(part, dict):
            content.append(part)
        else:
            content.append({"type": "text", "text": str(part)})
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def ask(model, system, parts, schema, spend=None, reasoning="low", timeout=120):
    """One call. The model answers JSON matching ``schema``; ``spend`` is charged its list-price cost."""
    if spend is not None:
        spend.check()
    started = time.monotonic()
    try:
        out, usage = client(model, reasoning).complete(messages(system, parts), schema, timeout=timeout)
    except Exception as exc:  # a failed call is a row, not a crash: the scorer counts it
        return Reply(error=f"{type(exc).__name__}: {str(exc)[:200]}", seconds=time.monotonic() - started)
    seconds = time.monotonic() - started
    usd = frontier.cost_usd(model, usage) or 0.0
    if spend is not None:
        spend.add(usd)
    return Reply(out=out if isinstance(out, dict) else {"value": out}, usd=usd, seconds=seconds, usage=usage)


def jev():
    from ...decision_provider import JevDecisions
    return JevDecisions()
