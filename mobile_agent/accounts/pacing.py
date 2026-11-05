"""Pacing per profile: how often and when a profile may act, and a stop when the app challenges it."""

import contextlib
import datetime
import fcntl
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SETTINGS = "pacing.json"
LEDGER = "pacing.jsonl"
WAIT_MAX_S = 15 * 60
KEEP_DAYS = 3
COMPACT_BYTES = 128 * 1024   # a ledger bigger than this drops the entries older than KEEP_DAYS on the next record
KINDS = ("post", "message", "comment", "follow", "react")
ACT_KIND = {"post": "post", "publish": "post", "share": "post", "tweet": "post",
            "send_message": "message", "reply": "message", "send": "message",
            "comment": "comment",
            "follow": "follow", "unfollow": "follow", "subscribe": "follow",
            "react": "react", "like": "react"}
DEFAULTS = {"post": {"daily": 4, "gapMinutes": 60}, "message": {"daily": 20, "gapMinutes": 4},
            "comment": {"daily": 30, "gapMinutes": 2}, "follow": {"daily": 30, "gapMinutes": 2},
            "react": {"daily": 100, "gapMinutes": 0.5}}
CAPS = {"daily": (0, 1000), "gapMinutes": (0, 24 * 60)}
CHALLENGE_WORDS = ("captcha", "suspect", "automated", "unusual activity", "confirm it's you", "confirm it’s you",
                   "verify your account", "verify it's you", "verify it’s you", "selfie", "upload an id",
                   "temporarily blocked", "try again later", "action blocked", "we restrict", "challenge")


class PacingError(ValueError):
    pass


def kind_of(act):
    return ACT_KIND.get(act or "")


def _zone(name):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _minutes(text):
    hours, minutes = text.split(":")
    value = int(hours) * 60 + int(minutes)
    if not 0 <= value <= 24 * 60:
        raise ValueError
    return value


def clean(body):
    """A profile's pacing settings from ``body``: {timeZone, window: {start, end} | None, limits: {kind: {daily,
    gapMinutes}}}."""
    if not isinstance(body, dict) or set(body) - {"timeZone", "window", "limits"}:
        raise PacingError("Pacing takes timeZone, window and limits.")
    out = {"timeZone": None, "window": None, "limits": {}}
    zone = body.get("timeZone")
    if zone not in (None, ""):
        if not isinstance(zone, str) or _zone(zone) is None:
            raise PacingError("timeZone is a time zone such as America/New_York.")
        out["timeZone"] = zone
    window = body.get("window")
    if window is not None:
        try:
            start, end = _minutes(window["start"]), _minutes(window["end"])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise PacingError("window is {start, end} as 24-hour times, such as 09:00 and 21:00.") from None
        if start == end:
            raise PacingError("A posting window needs a start and an end that differ.")
        out["window"] = {"start": window["start"], "end": window["end"]}
    limits = body.get("limits", {})
    if not isinstance(limits, dict):
        raise PacingError("limits is an object with daily and gapMinutes values per kind.")
    for kind, limit in limits.items():
        if kind not in KINDS or not isinstance(limit, dict) or set(limit) - set(CAPS):
            raise PacingError(f"limits are per kind: {', '.join(KINDS)}, each with daily and gapMinutes.")
        cleaned = {}
        for key, value in limit.items():
            low, high = CAPS[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise PacingError(f"{kind} {key} is a number from {low} to {high}.")
            cleaned[key] = value
        out["limits"][kind] = cleaned
    return out


class Pacing:
    """Settings and ledger in ``data_dir``. ``clock`` replaces time.time in tests."""

    def __init__(self, data_dir, clock=time.time):
        self.data_dir = Path(data_dir)
        self.settings_path = self.data_dir / SETTINGS
        self.ledger_path = self.data_dir / LEDGER
        self.clock = clock
        self._lock = threading.RLock()

    def _read(self):
        try:
            data = json.loads(self.settings_path.read_text())
        except FileNotFoundError:
            return {"version": 1, "profiles": {}}
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
            raise PacingError("The saved profile pacing settings aren't readable.")
        for stored in data["profiles"].values():
            if not isinstance(stored, dict):
                raise PacingError("The saved profile pacing settings aren't readable.")
            clean({key: value for key, value in stored.items() if key != "paused"})
            paused = stored.get("paused")
            if paused is not None and (not isinstance(paused, dict) or not isinstance(paused.get("reason"), str)):
                raise PacingError("The saved profile pause isn't readable.")
        return data

    @contextlib.contextmanager
    def _exclusive(self):
        """This process's lock and the folder's file lock: the Mac app and the CLI share the folder."""
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.data_dir / "pacing.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _replace(self, path, text, prefix):
        handle, temporary = tempfile.mkstemp(prefix=prefix, suffix=path.suffix, dir=self.data_dir)
        try:
            with os.fdopen(handle, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(text)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @contextlib.contextmanager
    def _edit(self):
        with self._exclusive():
            data = self._read()
            yield data
            self._replace(self.settings_path, json.dumps(data, indent=1) + "\n", ".pacing.")

    def settings(self, profile):
        """The profile's settings with every kind's limit filled in, and whether it is paused."""
        stored = self._read()["profiles"].get(profile) or {}
        limits = {kind: {**DEFAULTS[kind], **((stored.get("limits") or {}).get(kind) or {})} for kind in KINDS}
        return {"timeZone": stored.get("timeZone"), "window": stored.get("window"), "limits": limits,
                "paused": stored.get("paused")}

    def save(self, profile, body):
        cleaned = clean(body)
        with self._edit() as data:
            current = data["profiles"].get(profile) or {}
            data["profiles"][profile] = {**cleaned, "paused": current.get("paused")}
        return self.settings(profile)

    def pause(self, profile, reason):
        with self._edit() as data:
            current = data["profiles"].setdefault(profile, {})
            current["paused"] = {"at": round(self.clock() * 1000), "reason": str(reason)[:300]}

    def resume(self, profile):
        with self._edit() as data:
            if profile in data["profiles"]:
                data["profiles"][profile]["paused"] = None
        return self.settings(profile)

    def record(self, profile, act, run_id=None):
        kind = kind_of(act)
        if kind is None:
            return
        line = json.dumps({"at": self.clock(), "profile": profile, "kind": kind, "run": run_id}) + "\n"
        with self._exclusive():
            fd = os.open(self.ledger_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, line.encode())
            finally:
                os.close(fd)
            # Preserve the ledger when maintenance cannot read or replace it.
            with contextlib.suppress(OSError, ValueError):
                self._compact()

    def _compact(self):
        """Drop the entries older than KEEP_DAYS once the ledger is big (called under ``_exclusive``)."""
        if self.ledger_path.stat().st_size <= COMPACT_BYTES:
            return
        cutoff = self.clock() - KEEP_DAYS * 86400
        kept = [json.dumps(entry) + "\n" for entry in self._ledger() if entry["at"] >= cutoff]
        self._replace(self.ledger_path, "".join(kept), ".pacing.")

    def _ledger(self):
        try:
            lines = self.ledger_path.read_text().splitlines()
        except FileNotFoundError:
            return []
        entries = []
        for line in lines:
            entry = json.loads(line)
            if (not isinstance(entry, dict) or not isinstance(entry.get("profile"), str)
                    or entry.get("kind") not in KINDS
                    or type(entry.get("at")) not in (int, float)):
                raise PacingError("The saved profile pacing history isn't readable.")
            try:
                entry["at"] = float(entry["at"])
            except OverflowError:
                raise PacingError("The saved profile pacing history isn't readable.") from None
            if not math.isfinite(entry["at"]):
                raise PacingError("The saved profile pacing history isn't readable.")
            entries.append(entry)
        return entries

    def _entries(self, profile, kind):
        cutoff = self.clock() - KEEP_DAYS * 86400
        return sorted(entry["at"] for entry in self._ledger()
                      if entry["profile"] == profile and entry["kind"] == kind and entry["at"] >= cutoff)

    def _day_start(self, settings, now):
        zone = _zone(settings.get("timeZone"))
        local = (datetime.datetime.fromtimestamp(now, zone) if zone
                 else datetime.datetime.fromtimestamp(now).astimezone())
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    def today(self, profile):
        """{kind: how many ran today} in the profile's time zone."""
        settings = self.settings(profile)
        start = self._day_start(settings, self.clock()).timestamp()
        return {kind: sum(1 for at in self._entries(profile, kind) if at >= start) for kind in KINDS}

    def check(self, profile, act):
        """None when the profile may do ``act`` now; else {"wait": seconds, "reason"} or {"stop": reason}."""
        kind = kind_of(act)
        settings = self.settings(profile)
        if settings.get("paused"):
            return {"stop": "This profile is paused: " + settings["paused"]["reason"] + " Resume it in the Queue "
                    "after you've dealt with it in the app.", "code": "profile_paused"}
        if kind is None:
            return None
        limit = settings["limits"][kind]
        now = self.clock()
        done = self._entries(profile, kind)
        start = self._day_start(settings, now)
        today = [at for at in done if at >= start.timestamp()]
        if len(today) >= limit["daily"]:
            tomorrow = (start + datetime.timedelta(days=1)).timestamp()
            return {"stop": f"This profile has reached today's limit for {kind}s ({limit['daily']}). The next can go "
                    "after midnight in its time zone.", "code": "pacing_daily", "until": tomorrow}
        if done and limit["gapMinutes"] > 0:
            ready = done[-1] + limit["gapMinutes"] * 60
            if ready > now:
                return {"wait": ready - now, "reason": f"keeps at least {limit['gapMinutes']:g} minutes between "
                        f"{kind}s", "until": ready}
        return None

    def next_post_time(self, profile, wanted=None):
        """The earliest time (epoch seconds) at or after ``wanted`` (default now) the profile may post."""
        settings = self.settings(profile)
        if settings.get("paused"):
            return None
        at = max(wanted or self.clock(), self.clock())
        verdict = self.check(profile, "post")
        if verdict and verdict.get("until"):
            at = max(at, verdict["until"])
        return self._in_window(settings, at)

    def _in_window(self, settings, at):
        window = settings.get("window")
        if not window:
            return at
        zone = _zone(settings.get("timeZone"))
        local = datetime.datetime.fromtimestamp(at, zone) if zone else datetime.datetime.fromtimestamp(at).astimezone()
        start, end = _minutes(window["start"]), _minutes(window["end"])
        minute = local.hour * 60 + local.minute
        inside = start <= minute < end if start < end else (minute >= start or minute < end)
        if inside:
            return at
        day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        opens = day + datetime.timedelta(minutes=start)
        if opens.timestamp() <= at:
            opens += datetime.timedelta(days=1)
        return opens.timestamp()


def looks_challenged(*texts):
    """Whether a task's words say an app challenged the profile (fail closed: any of the words)."""
    joined = " ".join(str(t or "") for t in texts).casefold()
    return any(word in joined for word in CHALLENGE_WORDS)


_instances = {}
_instances_lock = threading.Lock()


def pacing_for(runtime):
    """The Pacing beside ``runtime``'s profiles (accounts.json's folder)."""
    from .store import store_for
    folder = store_for(runtime).data_dir
    with _instances_lock:
        found = _instances.get(folder)
        if found is None:
            found = _instances[folder] = Pacing(folder)
        return found
