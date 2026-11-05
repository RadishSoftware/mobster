"""A project's policy, ``.mobster/policy.yaml``: where Mobster asks first in this project. It only narrows.

The policy adds questions. It never answers one: a rule that says ``allow`` removes no question that the permission
mode, a guard, a "never open" app or a floor (a one-time code, an unknown outcome) asks. The stricter of the policy and
the person's mode wins, and a policy that can't be read asks before every act (``Policy.broken``).

    version: 1
    default: allow                    # ask: an act no rule lists asks
    rules:
      - name: Work hours              # optional, shown on the approval
        profile: acme                 # optional: a profile's handle or id; left out, every task
        app: com.burbn.instagram      # optional: a bundle id; left out or *, every app
        kind: [post, comment]         # one kind or a list; any (the default) covers all of them
        action: allow                 # allow (default ask)
        window: mon-fri 09:00-18:00   # allow only then; outside it the policy asks
        timezone: America/New_York    # the window's and the day's clock; default this Mac's
        max_per_day: 5                # after this many a day, the policy asks

Off unless ``MOBSTER_POLICY=on``. The count of what ran unasked today is kept in ``policy-counts.json`` beside the
task journal, one file lock for every process on the Mac.
"""

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ENV = "MOBSTER_POLICY"
FILE = Path(".mobster") / "policy.yaml"
MAX_BYTES = 64 * 1024
MAX_RULES = 200
ACTIONS = ("allow", "ask")
TOP_KEYS = frozenset({"version", "default", "rules"})
RULE_KEYS = frozenset({"name", "profile", "app", "kind", "action", "window", "timezone", "max_per_day"})
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_BUNDLE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*[A-Za-z0-9_]")
_WINDOW = re.compile(r"(?:(?P<days>[a-z]{3}(?:-[a-z]{3})?(?:,[a-z]{3}(?:-[a-z]{3})?)*)\s+)?"
                     r"(?P<start>\d{1,2}:\d{2})\s*-\s*(?P<end>\d{1,2}:\d{2})", re.I)

# What a rule's ``kind`` can name, and the gate acts each stands for (permissions.consequence, rules.RULE_KINDS and the
# contract's acts). An act is in every kind listed for it, so a rule on any one of them reaches it. An outward act no
# kind lists is "other"; a change that stays on the phone and no kind lists is "change".
ACT_KINDS = {
    "send": ("message",), "send_message": ("message",), "reply": ("message",), "unsend": ("message", "delete"),
    "comment": ("comment",),
    "post": ("post",), "publish": ("post",), "tweet": ("post",), "share": ("post",),
    "react": ("react",), "like": ("react",), "unlike": ("react",),
    "follow": ("follow",), "unfollow": ("follow",), "subscribe": ("follow", "buy"), "unsubscribe": ("follow", "buy"),
    "call": ("call",), "dial": ("call",), "facetime": ("call",), "end call": ("call",),
    "buy": ("buy",), "purchase": ("buy",), "pay": ("buy",), "checkout": ("buy",), "order": ("buy",),
    "place order": ("buy",), "transfer": ("buy",), "donate": ("buy",), "tip": ("buy",), "redeem": ("buy",),
    "rent": ("buy",), "get": ("buy",), "install": ("buy",),
    "book": ("book",), "reserve": ("book",),
    "delete": ("delete",), "erase": ("delete",), "remove": ("delete",), "reset": ("delete",), "clear": ("delete",),
    "trash": ("delete",), "wipe": ("delete",), "unmatch": ("delete",),
    "setting": ("setting",), "turn off": ("setting",),
    "change": ("change",), "save": ("change",), "submit": ("change",), "archive": ("change",), "junk": ("change",),
}
KINDS = tuple(sorted({kind for kinds in ACT_KINDS.values() for kind in kinds} | {"other"})) + ("any",)


def enabled(env=None):
    """Whether project policies are read (``MOBSTER_POLICY=on``); off by default."""
    return ((env if env is not None else os.environ).get(ENV) or "").strip().casefold() == "on"


def kinds_of(gate_kind, act):
    """The policy kinds of one gate decision: ``gate_kind`` is "commit" or "change", ``act`` its verb."""
    act = " ".join(str(act or "").casefold().split())
    found = ACT_KINDS.get(act) or ACT_KINDS.get(act.replace("_", " "))
    return frozenset(found or (("change",) if gate_kind == "change" else ("other",)))


class PolicyError(ValueError):
    """The file can't be used; the message says what to change."""


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PolicyError(f"{name} is text of at most {limit} characters")
    return " ".join(value.split())


def _minutes(clock):
    hours, minutes = clock.split(":")
    value = int(hours) * 60 + int(minutes)
    if not 0 <= value <= 24 * 60 or int(minutes) > 59:
        raise PolicyError("a window's times are 24-hour, such as 09:00-18:00")
    return value


def _days(text):
    out = set()
    for part in text.casefold().split(","):
        first, _, last = part.partition("-")
        if first not in DAYS or last and last not in DAYS:
            raise PolicyError("a window's days are mon to sun, such as mon-fri or sat,sun")
        low, high = DAYS.index(first), DAYS.index(last or first)
        out |= set(range(low, high + 1)) if low <= high else set(range(low, 7)) | set(range(high + 1))
    return frozenset(out)


def _zone(name):
    if not name:
        return None
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise PolicyError("timezone is a time zone such as America/New_York") from None


@dataclass(frozen=True)
class Rule:
    index: int
    name: str
    profile: str | None
    app: str | None
    kinds: frozenset
    action: str
    window: str | None = None
    days: frozenset | None = None
    start: int = 0
    end: int = 0
    timezone: str | None = None
    max_per_day: int | None = None

    @property
    def key(self):
        """What the day's count is kept under: the rule's scope, so raising its cap keeps today's count."""
        scope = json.dumps([self.name, self.profile, self.app, sorted(self.kinds)])
        return hashlib.sha256(scope.encode()).hexdigest()[:16]

    @property
    def label(self):
        return f"“{self.name}”" if self.name else f"rule {self.index + 1}"

    def matches(self, kinds, app, profile):
        return ((self.app is None or self.app == app) and (self.profile is None or _profile_is(self.profile, profile))
                and ("any" in self.kinds or bool(self.kinds & kinds)))

    def local(self, now):
        zone = _zone(self.timezone)
        if zone:
            return datetime.datetime.fromtimestamp(now, zone)
        return datetime.datetime.fromtimestamp(now).astimezone()

    def day(self, now):
        return self.local(now).date().isoformat()

    def open(self, now):
        """Whether ``now`` is inside the rule's window (always, when it has none)."""
        if self.window is None:
            return True
        local = self.local(now)
        minute, weekday = local.hour * 60 + local.minute, local.weekday()
        if self.start < self.end:
            return weekday in self.days and self.start <= minute < self.end
        # A window past midnight belongs to the day it started.
        return (weekday in self.days and minute >= self.start) or ((weekday - 1) % 7 in self.days and minute < self.end)


def _profile_is(wanted, profile):
    if not profile:
        return False
    names = {str(profile.get(key) or "").casefold().lstrip("@") for key in ("id", "handle", "name")} - {""}
    return wanted.casefold().lstrip("@") in names


class Counts:
    """How many acts the policy let through unasked today, per rule. ``path`` None keeps them in memory (tests, dry
    runs). Unreadable counts read as a reached cap: the policy then asks (fail closed)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._memory = {}
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def _exclusive(self):
        with self._lock:
            if self.path is None:
                yield
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _read(self):
        if self.path is None:
            return self._memory
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or not all(isinstance(v, dict) and isinstance(v.get("day"), str)
                                                 and type(v.get("n")) is int for v in data.values()):
            raise ValueError("unreadable")
        return data

    def _write(self, data):
        if self.path is None:
            self._memory = data
            return
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".policy-counts-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def today(self, key, day):
        """How many ran today under ``key``; None when the counts can't be read."""
        try:
            with self._exclusive():
                entry = self._read().get(key)
        except (OSError, ValueError):
            return None
        return entry["n"] if entry and entry["day"] == day else 0

    def take(self, wanted):
        """Count one more under each (key, day, cap) of ``wanted``, unless one is at its cap or unreadable: then
        nothing is counted and that item is returned. All or nothing, under one lock."""
        try:
            with self._exclusive():
                data = self._read()
                for key, day, cap in wanted:
                    entry = data.get(key)
                    if (entry["n"] if entry and entry["day"] == day else 0) >= cap:
                        return (key, day, cap)
                for key, day, _ in wanted:
                    entry = data.get(key)
                    data[key] = {"day": day, "n": entry["n"] + 1 if entry and entry["day"] == day else 1}
                if len(data) > 400:
                    data = dict(sorted(data.items(), key=lambda item: item[1]["day"])[-200:])
                self._write(data)
        except (OSError, ValueError):
            return ("unreadable", "", 0)
        return None


@dataclass(frozen=True)
class Policy:
    rules: tuple = ()
    default: str = "allow"
    path: Path | None = None
    error: str | None = None
    counts: Counts = field(default_factory=Counts, compare=False)

    @classmethod
    def broken(cls, path, error, counts=None):
        return cls(default="ask", path=path, error=error, counts=counts or Counts())

    @property
    def narrows(self):
        """Whether the policy can ask at all: any rule but a plain ``allow``, a default of ask, or a broken file."""
        return self.error is not None or self.default == "ask" or any(
            r.action == "ask" or r.window is not None or r.max_per_day is not None for r in self.rules)

    def _matching(self, kinds, app, profile):
        return [rule for rule in self.rules if rule.matches(kinds, app, profile)]

    def asks(self, kinds, app, profile=None, now=None):
        """The sentence for why the policy asks about an act of ``kinds`` in ``app`` as ``profile`` now, or None.
        Reads the counts; counts nothing."""
        now = time.time() if now is None else now
        if self.error is not None:
            return f"Your project policy can't be read: {self.error}"
        matched = self._matching(kinds, app, profile)
        for rule in matched:
            if rule.action == "ask":
                return f"Your project policy {rule.label} asks before this" if rule.name else \
                    "Your project policy asks before this"
            if not rule.open(now):
                return f"Your project policy allows this only {rule.window}"
            if rule.max_per_day is not None:
                done = self.counts.today(rule.key, rule.day(now))
                if done is None or done >= rule.max_per_day:
                    return f"You reached today's limit of {rule.max_per_day} in your project policy"
        if not matched and self.default == "ask":
            return "Your project policy asks before anything it doesn't list"
        return None

    def take(self, kinds, app, profile=None, now=None):
        """An act the engine lets through unasked: count it under every capped rule it matches. None when counted,
        else the sentence for why the policy asks (a cap was reached between ``asks`` and now, or the counts are
        unreadable)."""
        now = time.time() if now is None else now
        capped = [(r.key, r.day(now), r.max_per_day) for r in self._matching(kinds, app, profile)
                  if r.max_per_day is not None]
        if not capped or self.counts.take(capped) is None:
            return None
        return "You reached today's limit in your project policy"


def parse(text, *, path=None, counts=None):
    """The ``Policy`` in ``text``. Raises ``PolicyError`` for a key it doesn't know (a typo must not pass), a value
    out of range, or a window or cap on a rule that asks."""
    import yaml
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        line = getattr(getattr(error, "problem_mark", None), "line", None)
        raise PolicyError("the file isn't valid YAML" + (f" (line {line + 1})" if line is not None else "")) from None
    if not isinstance(data, dict) or set(data) - TOP_KEYS:
        raise PolicyError("the file takes version, default and rules")
    if data.get("version", 1) != 1:
        raise PolicyError("version is 1")
    default = data.get("default", "allow")
    if default not in ACTIONS:
        raise PolicyError("default is allow or ask")
    listed = [] if data.get("rules") is None else data["rules"]
    if not isinstance(listed, list) or len(listed) > MAX_RULES:
        raise PolicyError(f"rules is a list of at most {MAX_RULES}")
    rules = []
    for index, raw in enumerate(listed):
        try:
            rules.append(_rule(index, raw))
        except PolicyError as error:
            raise PolicyError(f"rule {index + 1}: {error}") from None
    return Policy(tuple(rules), default, path, None, counts or Counts())


def _rule(index, raw):
    if not isinstance(raw, dict) or set(raw) - RULE_KEYS:
        raise PolicyError("a rule takes " + ", ".join(sorted(RULE_KEYS)))
    action = raw.get("action", "ask")
    if action not in ACTIONS:
        raise PolicyError("action is allow or ask")
    kinds = raw.get("kind", "any")
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if not isinstance(kinds, list) or not kinds or not all(isinstance(k, str) and k.casefold() in KINDS for k in kinds):
        raise PolicyError("kind is one of " + ", ".join(KINDS) + ", or a list of them")
    app = raw.get("app")
    if app is not None and app != "*" and (not isinstance(app, str) or len(app) > 255 or "." not in app
                                          or not _BUNDLE.fullmatch(app)):
        raise PolicyError("app is a bundle id such as com.burbn.instagram, or *")
    window, days, start, end = raw.get("window"), None, 0, 0
    if action == "ask" and (window is not None or raw.get("max_per_day") is not None):
        raise PolicyError("a rule that asks always asks: put window and max_per_day on an allow rule")
    if window is not None:
        found = _WINDOW.fullmatch(_text(window, "window", 40))
        if found is None:
            raise PolicyError("window is days and hours, such as mon-fri 09:00-18:00")
        days = _days(found["days"]) if found["days"] else frozenset(range(7))
        start, end = _minutes(found["start"]), _minutes(found["end"])
        if start == end:
            raise PolicyError("a window needs a start and an end that differ")
        window = " ".join(window.split())
    cap = raw.get("max_per_day")
    if cap is not None and (type(cap) is not int or not 1 <= cap <= 10_000):
        raise PolicyError("max_per_day is a whole number from 1 to 10000")
    zone = raw.get("timezone")
    if zone is not None:
        _zone(_text(zone, "timezone", 64))
    return Rule(index, _text(raw["name"], "name", 60) if "name" in raw else "",
                _text(raw["profile"], "profile", 80) if raw.get("profile") is not None else None,
                None if app == "*" else app, frozenset(k.casefold() for k in kinds), action, window, days, start, end,
                zone, cap)


def load(folder, counts_path=None):
    """The ``Policy`` in ``folder``/.mobster/policy.yaml; None when there is no file. A file that can't be read or
    checked, a link, or one too big gives a broken policy, which asks before every act."""
    path = Path(folder) / FILE
    counts = Counts(counts_path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return Policy.broken(path, "the file can't be read", counts)
    if not path.is_file() or path.is_symlink() or path.parent.is_symlink() or info.st_size > MAX_BYTES:
        return Policy.broken(path, "it must be a plain file of at most 64 KB, not a link", counts)
    try:
        return parse(path.read_text(encoding="utf-8"), path=path, counts=counts)
    except (OSError, UnicodeDecodeError):
        return Policy.broken(path, "the file can't be read as text", counts)
    except PolicyError as error:
        return Policy.broken(path, str(error), counts)


def project_folder(start=None):
    """The folder that holds ``.mobster/``, walking up from ``start`` (default the working directory); None without
    one."""
    start = Path(start or Path.cwd()).resolve()
    start = start if start.is_dir() else start.parent
    return next((folder for folder in (start, *start.parents) if (folder / ".mobster").is_dir()), None)


def counts_path(state_db=None):
    """Where today's counts live: beside the task journal. None for no journal (a scripted demo): they stay in
    memory."""
    if state_db:
        return Path(state_db).parent / "policy-counts.json"
    return None
