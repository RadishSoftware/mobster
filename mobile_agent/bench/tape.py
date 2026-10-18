"""A run's tape: every call the agent made to the phone and to its model, in order, with what came back, so the run
can be replayed on today's code with no phone and no model.

The rule is DeepSeek Harness's, applied to a phone: everything the agent saw is on the tape. Recording wraps the
driver (``TapeDriver``) and the model client (``TapeClient``) at the two boundaries the loop already has. ``replay``
serves the tape back (``ReplayDriver``, ``ReplayClient``) on a virtual clock and runs FrontierAgent on the same
request. Code that would act differently on a recorded run stops the replay at the first action the recording didn't
send (a ``Divergence``), with the turn it happened in. Code that acts the same replays the whole run in seconds, for
free. That makes a recorded run a regression test for the loop, its gates and its target resolution.

Format: ``tape.jsonl``, one JSON object a line.

- ``{"kind": "header", "version", "request", "apps", "model", "frontier", "env", "capabilities", "t"}``
- ``{"kind": "call", "on", "name", "args", "t0", "t1", "result" | "error"}``: a driver method (``on`` is ``driver``, or
  ``driver.frame_clock`` and so on for an object the driver hands out)
- ``{"kind": "value", "on", "name", "value"}``: a driver attribute the agent read
- ``{"kind": "model", "purpose", "prompt", "t0", "t1", "reply" | "error", "usage"}``: ``prompt`` is a hash of the
  prompt's text (images left out), so a replay also says whether the model would have been asked the same thing
- ``{"kind": "end", "status", "answer", "steps", "actions"}``

What it holds: the request and the screens as data (labels, values, frames), the model's replies. A picture is kept
as its size only: a replayed model doesn't look. Benchmarks on simulators write it (bench/iosworld.py). The Mac app
doesn't.
"""

import dataclasses
import gzip
import hashlib
import importlib
import inspect
import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from .. import state as state_module
from ..drivers import Driver
from ..driver_guards import DriverRejection

VERSION = 1
NAME = "tape.jsonl"           # gzipped to tape.jsonl.gz when the run ends
# Calls that change the phone: a replay sends these only in the recorded order, with the same arguments. Every other
# call (a read, a settle, tune, recover) is served from the tape by name.
ACTS = frozenset({"execute", "tap_point", "long_press", "set_value", "write_text", "replace_text", "clear_text",
                  "relaunch_app", "drag_next", "dismiss_at", "open_url", "launch"})
BASE = ("observe", "execute", "close", "can_type", "last_image")
_PIXEL = []


def pixel():
    """A small grey JPEG: what a replay hands over for any picture the recording took."""
    if not _PIXEL:
        import io
        from PIL import Image
        out = io.BytesIO()
        Image.new("RGB", (8, 8), (128, 128, 128)).save(out, "JPEG")
        _PIXEL.append(out.getvalue())
    return _PIXEL[0]


DATACLASSES = {cls.__name__: cls for cls in (state_module.Element, state_module.Snapshot)}
# Environment kept with the header: the switches that steer the loop, never a key.
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


class Divergence(BaseException):
    """The replay asked for something the tape doesn't have: an action the recording didn't send, or a read past its
    end. A BaseException, so the loop's own error handling can't turn it into a run that "failed"."""

    def __init__(self, why, expected=None, got=None, index=None):
        self.why, self.expected, self.got, self.index = why, expected, got, index
        super().__init__(why)


# -- encoding -----------------------------------------------------------------------------------------------------

def encode(value, depth=0):
    """``value`` as JSON: dataclasses by name, bytes by size, an exception by type and message."""
    if depth > 12:
        return None
    if isinstance(value, str) and value.startswith("data:image/"):
        return {"__t": "bytes", "n": len(value)}  # a picture as a data URL: its size only, like bytes
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, (bytes, bytearray)):
        return {"__t": "bytes", "n": len(value)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type) and type(value).__name__ in DATACLASSES:
        return {"__t": type(value).__name__,
                **{f.name: encode(getattr(value, f.name), depth + 1) for f in dataclasses.fields(value)}}
    if isinstance(value, dict):
        return {str(k): encode(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode(v, depth + 1) for v in value]
    return {"__t": "object", "type": type(value).__name__}


def _tupled(value):
    return tuple(_tupled(v) for v in value) if isinstance(value, list) else value


def decode(value):
    """What ``encode`` wrote, back as the objects the loop reads (lists stay lists outside dataclasses)."""
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("__t")
    if kind == "bytes":
        return pixel()
    if kind == "object":
        return None
    if kind in DATACLASSES:
        cls = DATACLASSES[kind]
        fields = {}
        for f in dataclasses.fields(cls):
            if f.name not in value:
                continue
            item = decode(value[f.name])
            fields[f.name] = item if f.name == "elements" or not isinstance(item, list) else _tupled(item)
        return cls(**fields)
    return {k: decode(v) for k, v in value.items()}


def encode_error(error):
    out = {"type": type(error).__name__, "module": type(error).__module__, "message": str(error)[:500]}
    if isinstance(error, DriverRejection):
        out["code"] = error.code
    return out


def decode_error(data):
    """The recorded exception, as close to its class as can be rebuilt; else a RuntimeError naming it."""
    if data.get("type") == "DriverRejection":
        return DriverRejection(data.get("code"))
    module, name, message = data.get("module") or "", data.get("type") or "", data.get("message") or ""
    if module == "builtins" or module.startswith("mobile_agent."):
        try:
            cls = getattr(importlib.import_module(module), name)
            if isinstance(cls, type) and issubclass(cls, Exception):
                return cls(message)
        except Exception:
            pass
    return RuntimeError(f"{name}: {message}")


def _element_key(value):
    return {"label": value.label, "role": value.role} if isinstance(value, state_module.Element) else value


def act_key(name, args, kwargs):
    """What makes two calls the same action: the method, its operation and target (by label and role), its text and
    flags. Snapshots and timeouts are left out: the loop's own reads decide those."""
    parts = [name]
    for value in list(args) + [kwargs[k] for k in sorted(kwargs) if k not in ("timeout", "purpose")]:
        if isinstance(value, state_module.Snapshot):
            continue
        if isinstance(value, float):
            value = round(value, 3)
        parts.append(encode(_element_key(value)))
    return parts


def prompt_hash(messages):
    """A hash of the prompt's text parts, in order (images left out)."""
    digest = hashlib.sha256()
    for message in messages or ():
        content = message.get("content")
        parts = [content] if isinstance(content, str) else content or ()
        for part in parts:
            text = part if isinstance(part, str) else part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str):
                digest.update(text.encode("utf-8", "replace"))
    return digest.hexdigest()[:16]


def purpose_of(schema):
    """A model call's purpose: its reply schema's property names (a decision has ``actions``, a contract ``items``)."""
    return ",".join(sorted((schema or {}).get("properties") or {}))


def switches(env=None):
    env = os.environ if env is None else env
    return {k: v for k, v in sorted(env.items())
            if k.startswith("MOBSTER_") and not any(word in k for word in SECRET_WORDS)}


# -- recording ----------------------------------------------------------------------------------------------------

class Tape:
    """The writer: one JSONL file, appended under a lock (the verify read and the contract compile run on their own
    threads). A write that fails stops the tape, never the run."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._file = None
        self._screens = set()
        self.broken = None

    def _shared(self, result):
        """A screen read again unchanged is written once: later reads refer to it by hash, with their own times."""
        if not (isinstance(result, dict) and result.get("__t") == "Snapshot"):
            return result
        times = {k: result.get(k) for k in ("captured_at", "read_started")}
        body = {k: v for k, v in result.items() if k not in times}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
        with self._lock:
            seen = digest in self._screens
            self._screens.add(digest)
        return {"__t": "ref", "h": digest, **times} if seen else {**result, "__h": digest}

    def write(self, entry):
        if self.broken:
            return
        try:
            if entry.get("kind") == "call" and "result" in entry:
                entry = {**entry, "result": self._shared(entry["result"])}
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            with self._lock:
                if self._file is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._file = open(self.path, "a", encoding="utf-8", buffering=1)  # a line a write: a crash keeps the tape
                self._file.write(line + "\n")
        except Exception as error:  # noqa: BLE001
            self.broken = f"{type(error).__name__}: {error}"

    def header(self, *, request, apps, model, frontier, driver, env=None):
        kinds = capabilities(driver)
        values = {name: encode(getattr(driver, name, None)) for name, kind in kinds.items() if kind == "value"}
        self.write({"kind": "header", "version": VERSION, "request": request, "apps": dict(apps or {}),
                    "model": model, "frontier": encode(frontier), "env": switches(env), "capabilities": kinds,
                    "values": values, "t": time.monotonic()})

    def end(self, outcome):
        """The run's end, then the tape gzipped (tape.jsonl.gz): a tape that never ends stays plain."""
        outcome = outcome or {}
        self.write({"kind": "end", "status": outcome.get("status"), "answer": encode(outcome.get("answer")),
                    "steps": outcome.get("steps"), "actions": outcome.get("actions")})
        self.close()
        if self.broken or not self.path.exists():
            return
        try:
            with open(self.path, "rb") as plain, gzip.open(str(self.path) + ".gz", "wb", compresslevel=6) as packed:
                shutil.copyfileobj(plain, packed)
            self.path.unlink()
        except OSError as error:
            self.broken = f"{type(error).__name__}: {error}"

    def close(self):
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


def capabilities(driver):
    """{name: "call" | "value" | "object"} for the base contract and every Driver.OPTIONAL name ``driver`` has."""
    out = {}
    for name in (*BASE, *Driver.OPTIONAL):
        try:
            value = getattr(driver, name)
        except Exception:  # noqa: BLE001
            continue
        # Present means the attribute exists, set or not: settled_by is None until the first settle.
        out[name] = ("call" if callable(value) else "object" if name == "frame_clock" and value is not None
                     else "value")
    return out


class _Recorded:
    """A proxy that writes each call and attribute read of ``inner`` to the tape. Objects the agent gets from it
    (the frame clock, its video) are proxied too, under a dotted ``on``."""

    _OBJECTS = frozenset({"frame_clock", "video"})

    def __init__(self, inner, tape, on):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_tape", tape)
        object.__setattr__(self, "_on", on)

    def __getattr__(self, name):
        value = getattr(self._inner, name)
        if name.startswith("__"):
            return value
        if callable(value) and not isinstance(value, type):
            return self._call(name, value)
        if name in self._OBJECTS and value is not None:
            return _Recorded(value, self._tape, f"{self._on}.{name}")
        self._tape.write({"kind": "value", "on": self._on, "name": name, "value": encode(value)})
        return value

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)

    def _call(self, name, method):
        tape, on = self._tape, self._on

        def call(*args, **kwargs):
            entry = {"kind": "call", "on": on, "name": name, "args": act_key(name, args, kwargs),
                     "thread": threading.current_thread().name, "t0": time.monotonic()}
            try:
                result = method(*args, **kwargs)
            except BaseException as error:
                entry.update(t1=time.monotonic(), error=encode_error(error))
                tape.write(entry)
                raise
            entry.update(t1=time.monotonic(), result=encode(result))
            tape.write(entry)
            return result

        return call


class TapeDriver(_Recorded):
    """``driver`` with every call and read on ``tape``."""

    def __init__(self, driver, tape):
        super().__init__(driver, tape, "driver")

    # The abstract three, spelled out so the proxy is a complete driver even to code that looks them up on the class.
    def observe(self, timeout=10):
        return self.__getattr__("observe")(timeout=timeout)

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        return self.__getattr__("execute")(operation, target, snapshot, text=text, timeout=timeout)

    def close(self):
        return self._inner.close()


class TapeClient:
    """The model client with every call on ``tape``: its purpose, a hash of the prompt's text, the reply or the
    error, and the client's usage after it (hedges included), so a replay's cost is the recording's."""

    def __init__(self, client, tape):
        object.__setattr__(self, "_inner", client)
        object.__setattr__(self, "_tape", tape)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)

    def complete(self, messages, schema, **kwargs):
        entry = {"kind": "model", "purpose": purpose_of(schema), "prompt": prompt_hash(messages),
                 "t0": time.monotonic()}
        try:
            reply, usage = self._inner.complete(messages, schema, **kwargs)
        except BaseException as error:
            entry.update(t1=time.monotonic(), error=encode_error(error),
                         usage=encode(dict(getattr(self._inner, "usage", None) or {})))
            self._tape.write(entry)
            raise
        entry.update(t1=time.monotonic(), reply=encode(reply), call_usage=encode(usage),
                     usage=encode(dict(getattr(self._inner, "usage", None) or {})))
        self._tape.write(entry)
        return reply, usage


# -- replay -------------------------------------------------------------------------------------------------------

def load(path):
    """The tape's entries; ValueError when it has no header."""
    path = Path(path)
    if path.is_dir():
        path = path / NAME if (path / NAME).exists() else path / (NAME + ".gz")
    text = (gzip.decompress(path.read_bytes()).decode("utf-8") if path.suffix == ".gz"
            else path.read_text(encoding="utf-8"))
    entries = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not entries or entries[0].get("kind") != "header":
        raise ValueError(f"{path} is not a tape (no header)")
    screens = {}
    for entry in entries:
        result = entry.get("result")
        if isinstance(result, dict) and result.get("__t") == "Snapshot" and "__h" in result:
            screens[result.pop("__h")] = result
        elif isinstance(result, dict) and result.get("__t") == "ref":
            shared = screens.get(result.get("h"))
            if shared is None:
                raise ValueError(f"{path}: a screen refers to one the tape doesn't hold")
            entry["result"] = {**shared, "captured_at": result.get("captured_at"),
                               "read_started": result.get("read_started")}
    return entries


class Clock:
    """The replay's time: the recording's monotonic clock, moved to each served call's end; sleeping moves it."""

    def __init__(self, start):
        self.now = float(start)
        self._lock = threading.Lock()

    def reach(self, t):
        if isinstance(t, (int, float)):
            with self._lock:
                self.now = max(self.now, float(t))

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        with self._lock:
            self.now += max(0.0, float(seconds or 0))


class _VirtualTime:
    """The ``time`` module as the loop sees it in a replay: monotonic and sleep from the Clock, the rest real."""

    def __init__(self, clock):
        self._clock = clock

    def monotonic(self):
        return self._clock.monotonic()

    def sleep(self, seconds):
        self._clock.sleep(seconds)

    def __getattr__(self, name):
        return getattr(time, name)


class Player:
    """The tape served back: acts in order and compared, reads by name in order, values by name (the last one again
    once they run out), model replies by purpose in order."""

    def __init__(self, entries):
        self.header = entries[0]
        self.clock = Clock(self.header.get("t") or 0.0)
        self.acts, self.reads, self.values, self.models = [], {}, {}, {}
        self.end = next((e for e in entries if e.get("kind") == "end"), None)
        self.recorded_kinds = {}
        for entry in entries[1:]:
            kind = entry.get("kind")
            if kind == "call":
                key = (entry["on"], entry["name"])
                self.recorded_kinds[key] = "call"
                if self._is_act(entry["name"], entry.get("args")):
                    self.acts.append(entry)
                else:
                    self.reads.setdefault(key, []).append(entry)
            elif kind == "value":
                key = (entry["on"], entry["name"])
                self.recorded_kinds.setdefault(key, "value")
                self.values.setdefault(key, []).append(entry)
            elif kind == "model":
                self.models.setdefault(entry.get("purpose") or "", []).append(entry)
        self.acts_done = 0
        self.prompts_same = self.prompts_seen = 0
        self._served_values = {}
        self._lock = threading.Lock()

    @staticmethod
    def _is_act(name, args):
        if name in ACTS:
            return True
        # A raw WebDriverAgent request acts unless it's a GET.
        return name == "call" and bool(args) and len(args) > 1 and str(args[1]).upper() != "GET"

    def _answer(self, entry):
        self.clock.reach(entry.get("t1"))
        if "error" in entry:
            raise decode_error(entry["error"])
        return decode(entry.get("result"))

    def call(self, on, name, args, kwargs):
        key = act_key(name, args, kwargs)
        with self._lock:
            if self._is_act(name, key):
                index = self.acts_done
                if index >= len(self.acts):
                    raise Divergence("an action the recording never sent", None, key, index)
                expected = self.acts[index]
                if expected["on"] != on or expected["args"] != json.loads(json.dumps(key)):
                    raise Divergence("a different action", expected["args"], key, index)
                self.acts_done += 1
                entry = expected
            else:
                queue = self.reads.get((on, name))
                if not queue:
                    raise Divergence(f"a read the recording didn't make ({on}.{name})", None, key, self.acts_done)
                entry = queue.pop(0)
        return self._answer(entry)

    def value(self, on, name):
        with self._lock:
            queue = self.values.get((on, name))
            if queue:
                self._served_values[(on, name)] = queue.pop(0)
            entry = self._served_values.get((on, name))
        if entry is None:
            raise AttributeError(name)
        return decode(entry.get("value"))

    def model(self, messages, schema):
        purpose = purpose_of(schema)
        with self._lock:
            queue = self.models.get(purpose)
            if not queue:
                raise Divergence(f"a model call the recording didn't make ({purpose or 'no schema'})", None,
                                 purpose, self.acts_done)
            entry = queue.pop(0)
            self.prompts_seen += 1
            self.prompts_same += prompt_hash(messages) == entry.get("prompt")
        self.clock.reach(entry.get("t1"))
        if "error" in entry:
            raise decode_error(entry["error"])
        return decode(entry.get("reply")), decode(entry.get("call_usage") or {}), decode(entry.get("usage") or {})


class _Served:
    """The replayed side of an object the driver handed out (``on`` names it)."""

    def __init__(self, player, on):
        object.__setattr__(self, "_player", player)
        object.__setattr__(self, "_on", on)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        on, player = self._on, self._player
        kind = player.recorded_kinds.get((on, name))
        if kind == "call":
            return lambda *args, **kwargs: player.call(on, name, args, kwargs)
        if (on, name) in player.values or (on, name) in player._served_values:
            return player.value(on, name)
        if any(key[0] == f"{on}.{name}" for key in player.recorded_kinds):
            return _Served(player, f"{on}.{name}")
        raise AttributeError(name)

    def __setattr__(self, name, value):
        pass  # what the loop sets on a driver changes nothing a replay serves


class ReplayDriver(_Served):
    """The recorded driver: the capabilities it had, its reads and its answers."""

    def __init__(self, player):
        super().__init__(player, "driver")
        object.__setattr__(self, "_capabilities", dict(player.header.get("capabilities") or {}))

    def __getattr__(self, name):
        kind = self._capabilities.get(name)
        if kind is None and not name.startswith("__"):
            raise AttributeError(name)
        if kind == "call":
            player = self._player
            return lambda *args, **kwargs: player.call("driver", name, args, kwargs)
        if kind == "object":
            return _Served(self._player, f"driver.{name}")
        try:
            return self._player.value("driver", name)
        except AttributeError:
            return decode((self._player.header.get("values") or {}).get(name))

    def observe(self, timeout=10):
        return self._player.call("driver", "observe", (), {"timeout": timeout})

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        return self._player.call("driver", "execute", (operation, target, snapshot), {"text": text,
                                                                                     "timeout": timeout})

    def close(self):
        pass


class ReplayClient:
    """The recorded model: each call answered with the recorded reply for its purpose, usage as it was after it."""

    per_call_reasoning = True
    cancelled = None
    on_hedge_usage = None

    def __init__(self, player):
        self.player = player
        self.model = player.header.get("model") or ""
        self.reasoning = "low"
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
                      "calls": 0}

    def complete(self, messages, schema, **kwargs):
        reply, call_usage, usage = self.player.model(messages, schema)
        if usage:
            self.usage.clear()
            self.usage.update(usage)
        return reply, call_usage


@dataclasses.dataclass
class Report:
    tape: str
    request: str
    recorded_acts: int
    replayed_acts: int
    divergence: dict = None          # {"why", "expected", "got", "index", "step"} or None
    recorded_status: str = None
    replayed_status: str = None
    same_answer: bool = None
    prompts_same: int = 0
    prompts_seen: int = 0
    seconds: float = 0.0
    error: str = None

    @property
    def identical(self):
        return (self.divergence is None and self.error is None and self.replayed_acts == self.recorded_acts
                and self.recorded_status == self.replayed_status)

    def public(self):
        return {**dataclasses.asdict(self), "identical": self.identical}


# Modules whose ``time`` a replay swaps for the virtual one: the loop and the code it calls between two driver calls.
TIMED = ("frontier", "contract", "effect_ledger", "system_dialogs")


@contextmanager
def _virtual_time(clock):
    shim = _VirtualTime(clock)
    patches = []
    for name in TIMED:
        try:
            module = importlib.import_module(f"mobile_agent.{name}")
        except Exception:  # noqa: BLE001
            continue
        if getattr(module, "time", None) is time:
            patches.append(mock.patch.object(module, "time", shim))
    for patch in patches:
        patch.start()
    try:
        yield
    finally:
        for patch in reversed(patches):
            patch.stop()


def build_agent(driver, client, header, emit):
    """FrontierAgent as the recording built it: the same apps and limits, the engine's switches as they were."""
    from ..frontier import FrontierAgent
    frontier = dict(decode(header.get("frontier")) or {})
    accepted = set(inspect.signature(FrontierAgent.__init__).parameters)
    return FrontierAgent(driver, client, apps=dict(header.get("apps") or {}), emit=emit,
                         **{k: v for k, v in frontier.items() if k in accepted})


def replay(path, *, build=build_agent):
    """Replay the tape at ``path`` (a file, or a folder holding tape.jsonl) on today's code; a ``Report``."""
    entries = load(path)
    player = Player(entries)
    header = player.header
    events = []
    report = Report(str(path), header.get("request") or "", recorded_acts=len(player.acts), replayed_acts=0,
                    recorded_status=(player.end or {}).get("status"))
    started = time.monotonic()
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("MOBSTER_")}, **(header.get("env") or {})}
    with mock.patch.dict(os.environ, env, clear=True), _virtual_time(player.clock):
        agent = build(ReplayDriver(player), ReplayClient(player), header, events.append)
        try:
            outcome = agent.run(header.get("request") or "")
            report.replayed_status = outcome.get("status")
            recorded_answer = decode((player.end or {}).get("answer"))
            report.same_answer = outcome.get("answer") == recorded_answer
        except Divergence as divergence:
            step = next((e.get("step") for e in reversed(events) if e.get("event") == "frontier_decision"), None)
            report.divergence = {"why": divergence.why, "expected": divergence.expected, "got": divergence.got,
                                 "index": divergence.index, "step": step}
        except Exception as error:  # noqa: BLE001
            report.error = f"{type(error).__name__}: {error}"[:300]
    report.replayed_acts = player.acts_done
    report.prompts_same, report.prompts_seen = player.prompts_same, player.prompts_seen
    report.seconds = round(time.monotonic() - started, 3)
    return report
