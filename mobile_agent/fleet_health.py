"""The health of every phone: the alerts and pre-run checks the Phones page shows (Mobster for Mac › Phones)."""

import contextlib
import errno
import fcntl
import functools
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .api_errors import APIError
from .devices import REASONS

_Thread = threading.Thread

CODES = ("unplugged", "locked", "signed_out", "challenge", "region_mismatch", "owner_active")
ATTENTION, INFO = "attention", "info"
SEVERITY = {code: ATTENTION for code in CODES} | {"owner_active": INFO}
STORE = "fleet-health.json"
DEBOUNCE_S = 20.0
# How long a phone's region, language and time zone stand before they are read again. The pre-run check reads again
# after PRE_RUN_TTL, so a person who just changed a setting isn't refused on the old value for long.
LOCALE_TTL = 300.0
PRE_RUN_TTL = 5.0
READ_TIMEOUT = 2.0
GOAL_CHARS = 140
NOT_TAKEN = "No action was taken."


# -- reading the screen ------------------------------------------------------------------------------------------

def _clean(text):
    text = (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().casefold().strip(" .!?:")


# A security check or a block, in the apps' own words (English UI). Each pattern has to match one whole label (a title
# or a button, never a row or a field's value), so a message or a post that merely contains the words is
# not a challenge.
CHALLENGE = tuple(re.compile(pattern) for pattern in (
    r"we suspect automated behavior( on your account)?",
    r"suspicious (login|activity)( attempt| detected)?",
    r"we detected unusual (login )?activity( on your account)?",
    r"(please )?confirm it'?s you",
    r"verify it'?s you",
    r"help us confirm (you own this account|it'?s you)",
    r"confirm your identity",
    r"verify your identity",
    r"(take|record|upload) a (video )?selfie",
    r"upload a photo of your (id|government id)",
    r"captcha",
    r"(i'?m|i am) not a robot",
    r"(confirm|prove|verify) (that )?you'?re (a )?(human|not a robot)",
    r"verify you are (a )?human",
    r"select all (the )?(images|squares|pictures) (with|containing) .{1,40}",
    r"your account (has been|is|was) (temporarily )?(locked|suspended|disabled|restricted)",
    r"we (have )?(temporarily )?(locked|restricted) your account",
))
# One of these alone says the session ended.
SIGNED_OUT_STRONG = tuple(re.compile(pattern) for pattern in (
    r"you'?ve been logged out",
    r"you have been logged out",
    r"your session (has )?expired",
    r"session expired",
    r"(please )?(log|sign) in again( to .{1,40})?",
))
# A sign-in page shows at least two of these together (a feed never does).
SIGN_IN_PARTS = (
    re.compile(r"(log ?in|sign in)( to .{1,30})?"),
    re.compile(r"forgot (your )?password"),
    re.compile(r"create (new |a )?account|sign up|get started"),
    re.compile(r"(phone number|mobile number|username|email)(,| or ).{0,40}"),
    re.compile(r"log into another account|use another profile|switch accounts"),
)
# What each kind of element can say. A title says what the screen is; a button is what the person can press; a field's
# label is its prompt ("Phone number, username or email address"). Cells and text views hold content (a post, a message,
# a row), and no element's value is read: those can carry anything another person wrote.
TITLES = frozenset({"StaticText", "NavigationBar", "Other", "Link"})
PROMPTS = TITLES | {"Button", "TextField"}
MAX_LABEL = 100
MAX_LABELS = 400


def _whole(patterns, text):
    return any(pattern.fullmatch(text) for pattern in patterns)


def classify_screen(labels):
    # screen text is data: only whole, short titles and buttons match, and nothing read is passed on as an instruction
    pairs = []
    for pair in list(labels or ())[:MAX_LABELS]:
        if (isinstance(pair, (tuple, list)) and len(pair) == 2 and all(isinstance(part, str) for part in pair)
                and len(pair[1]) <= 3 * MAX_LABEL):
            text = _clean(pair[1])
            if text and len(text) <= MAX_LABEL:
                pairs.append((pair[0], text))
    buttons = [text for role, text in pairs if role == "Button"]
    titles = [text for role, text in pairs if role in TITLES]
    if buttons and any(_whole(CHALLENGE, text) for text in titles):
        return "challenge"
    if any(_whole(SIGNED_OUT_STRONG, text) for role, text in pairs if role in TITLES or role == "Button"):
        return "signed_out"
    seen = {index for role, text in pairs if role in PROMPTS
            for index, part in enumerate(SIGN_IN_PARTS) if part.fullmatch(text)}
    return "signed_out" if len(seen) >= 2 else None


def snapshot_labels(snapshot):
    out = []
    for element in list(getattr(snapshot, "elements", None) or ())[:MAX_LABELS]:
        role, label = getattr(element, "role", None), getattr(element, "label", None)
        if isinstance(role, str) and isinstance(label, str) and label:
            out.append((role, label))
    return out


# -- region, language and time zone ------------------------------------------------------------------------------

REGION = re.compile(r"[A-Z]{2}")
SETTING = {"region": "Settings › General › Language & Region › Region",
           "language": "Settings › General › Language & Region › Preferred Languages",
           "timezone": "Settings › General › Date & Time"}
WORD = {"region": "region", "language": "language", "timezone": "time zone"}


def expected_of(profile):
    """The values a profile expects: ``{region, language, timezone}``, each a string or None."""
    out = {}
    for key in ("region", "language", "timezone"):
        value = (profile or {}).get(key)
        out[key] = value if isinstance(value, str) and value else None
    return out


@functools.lru_cache(maxsize=1)
def _iana_names():
    try:
        from zoneinfo import available_timezones
        return frozenset(available_timezones())
    except Exception:  # noqa: BLE001 -- no tz data on this Mac: no zone can be told from an abbreviation
        return frozenset()


def iana_zone(name):
    # an abbreviation such as CST names no zone, so it is never compared with the zone a profile expects
    if not isinstance(name, str):
        return None
    name = name.strip()
    return name if (("/" in name or name in ("UTC", "GMT")) and name in _iana_names()) else None


def parse_locale(info):
    # /wda/device/info answers for the runner: its locale has the phone's Region but the runner's own language
    info = info if isinstance(info, dict) else {}
    region = None
    identifier = info.get("currentLocale")
    if isinstance(identifier, str):
        parts = re.split(r"[_-]", identifier.split("@")[0].split(".")[0].strip())
        found = [part for part in parts[1:] if REGION.fullmatch(part.upper()) and len(part) == 2]
        region = found[-1].upper() if found else None
    return {"region": region, "language": None, "timezone": iana_zone(info.get("timeZone"))}


def _offsets(name):
    """The UTC offsets of an IANA zone on the first of four months this year, or None for a name that isn't one."""
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(name)
    except Exception:  # noqa: BLE001 -- an unknown name or no tz data: compared by name only
        return None
    year = datetime.now(timezone.utc).year
    return tuple(datetime(year, month, 1, 12, tzinfo=zone).utcoffset() for month in (1, 4, 7, 10))


def zones_match(expected, actual):
    if expected == actual:
        return True
    left, right = _offsets(expected), _offsets(actual)
    return left is not None and left == right


def compare(profile, locale):
    wanted = expected_of(profile)
    locale = locale or {}
    checks = []
    for key in ("region", "language", "timezone"):
        if wanted[key] is None:
            continue
        actual = locale.get(key)
        if actual is None:
            state = "unknown"
        elif key == "timezone":
            state = "match" if zones_match(wanted[key], actual) else "mismatch"
        else:
            state = "match" if wanted[key].casefold() == actual.casefold() else "mismatch"
        checks.append({"field": key, "expected": wanted[key], "actual": actual, "state": state})
    return checks


def region_state(checks):
    if any(check["state"] == "mismatch" for check in checks):
        return "mismatch"
    if any(check["state"] == "unknown" for check in checks):
        return "unknown"
    return "ok" if checks else "none"


def mismatch_message(profile_name, phone_name, checks):
    """The refusal: what differs (both values), what to change, and that nothing was done."""
    bad = [check for check in checks if check["state"] == "mismatch"]
    parts = [f"{WORD[c['field']]} {c['actual']} where {c['expected']} is expected" for c in bad]
    changes = " and ".join(dict.fromkeys(SETTING[c["field"]] for c in bad))
    return (f"{phone_name} doesn't match {profile_name}: it is set to {_join(parts)}. Change {changes} on the phone, "
            f"or change what the profile expects in Settings › Profiles, then run the task again. {NOT_TAKEN}")


def _join(parts):
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def read_locale(wda_url, *, timeout=READ_TIMEOUT):
    from . import native_helpers
    if not wda_url or not native_helpers.devices_allowed():
        return None
    from .transport import HTTP
    client = None
    try:
        client = HTTP(wda_url)
        info = client.request("GET", "/wda/device/info", timeout=timeout).get("value")
    except Exception:  # noqa: BLE001 -- unknown, not blocking
        return None
    finally:
        if client is not None:
            client.close()
    locale = parse_locale(info)
    return locale if any(locale.values()) else None


# -- the alerts of one phone -------------------------------------------------------------------------------------

def _alert(code, message, action, since=None, profile=None):
    return {"code": code, "severity": SEVERITY[code], "message": message, "action": action, "since": since,
            **({"profile": profile} if profile else {})}


def state_alerts(item, name):
    """The alerts the device list's own state gives: unplugged, locked, owner active."""
    alerts = []
    state, reason = item.get("state"), item.get("reason")
    if state == "disconnected" and item.get("kind") == "usb" and not item.get("disconnected"):
        away = reason == REASONS["wifi_unreachable"]
        alerts.append(_alert("unplugged", f"{name} isn't reachable over Wi-Fi." if away else f"{name} is unplugged.",
                             "Keep it unlocked and on the same Wi-Fi as this Mac, or plug it in." if away
                             else "Plug it into this Mac with a cable."))
    elif state == "connected" and reason == REASONS["locked"]:
        alerts.append(_alert("locked", f"{name} is locked.", "Unlock it to run tasks. Mobster never unlocks a phone."))
    owner = item.get("owner")
    if isinstance(owner, dict) and owner.get("state") == "owner_active":
        alerts.append(_alert("owner_active", f"You're using {name}.", "Mobster waits until you're done with it.",
                             since=owner.get("since") if isinstance(owner.get("since"), (int, float)) else None))
    return alerts


def _app_name(profile):
    app = (profile or {}).get("app")
    name = app.get("name") if isinstance(app, dict) else None
    return name if isinstance(name, str) and name else "the app"


def _profile_name(profile):
    app = (profile or {}).get("app")
    handle = (profile or {}).get("handle") or "profile"
    return f"@{handle} on {_app_name(profile)}" if isinstance(app, dict) and app.get("name") else f"@{handle}"


def signal_alert(signal, profile_name, app_name, phone_name, since, profile_id):
    if signal == "challenge":
        return _alert("challenge", f"{app_name} asked for a security check for {profile_name} on {phone_name}.",
                      "Finish the check on the phone yourself, then choose Mark as handled. Tasks for this profile "
                      "wait until you do.", since, profile_id)
    return _alert("signed_out", f"{profile_name} is signed out on {phone_name}.",
                  f"Open {app_name} on the phone and sign in yourself. Mobster never signs in for you.", since,
                  profile_id)


def region_alert(profile_name, phone_name, checks, since, profile_id):
    bad = [check for check in checks if check["state"] == "mismatch"]
    parts = [f"{WORD[c['field']]} {c['actual']} instead of {c['expected']}" for c in bad]
    changes = " and ".join(dict.fromkeys(SETTING[c["field"]] for c in bad))
    return _alert("region_mismatch", f"{phone_name} is set to {_join(parts)}, which {profile_name} doesn't expect.",
                  f"Change {changes} on the phone, or what the profile expects in Settings › Profiles.", since,
                  profile_id)


# -- persisted signals -------------------------------------------------------------------------------------------

LOCK_TIMEOUT = 5.0
BLANK = {"signal": None, "since": None, "device": None, "confirmedAt": None}


class Signals:
    """fleet-health.json: per profile id, ``{signal, since, device, confirmedAt}``."""

    def __init__(self, folder, clock=time.time):
        self.path = Path(folder) / STORE if folder is not None else None
        self.lock_path = self.path.with_suffix(".lock") if self.path is not None else None
        self.clock = clock
        self.lock = threading.Lock()   # the in-memory copy, used only without a folder
        self.memory = {}

    def _load(self):
        if self.path is None:
            return {key: dict(entry) for key, entry in self.memory.items()}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        profiles = raw.get("profiles") if isinstance(raw, dict) else None
        out = {}
        for key, entry in (profiles.items() if isinstance(profiles, dict) else ()):
            if isinstance(key, str) and isinstance(entry, dict):
                signal = entry.get("signal") if entry.get("signal") in ("challenge", "signed_out") else None
                out[key] = {"signal": signal,
                            "since": entry.get("since") if isinstance(entry.get("since"), int) else None,
                            "device": entry.get("device") if isinstance(entry.get("device"), str) else None,
                            "confirmedAt": (entry.get("confirmedAt") if isinstance(entry.get("confirmedAt"), int)
                                            else None)}
        return out

    def _save(self, data):
        if self.path is None:
            self.memory = data
            return
        fd, temporary = tempfile.mkstemp(prefix=".fleet-health.", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps({"version": 1, "profiles": data}, indent=1) + "\n")
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    @contextlib.contextmanager
    def _edit(self):
        # the Mac app's server and `mobster mcp` both write: every edit locks, reads again and writes atomically
        if self.path is None:
            with self.lock:
                data = self._load()
                before = json.dumps(data, sort_keys=True)
                yield data
                if json.dumps(data, sort_keys=True) != before:
                    self._save(data)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            end = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) or time.monotonic() >= end:
                        raise OSError("Another Mobster process is holding the phone health file") from None
                    time.sleep(.02)
            data = self._load()
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                self._save(data)
        finally:
            os.close(fd)

    def all(self):
        """Every profile's entry, read from the file now."""
        if self.path is None:
            with self.lock:
                return self._load()
        return self._load()

    def get(self, profile_id):
        return dict(self.all().get(profile_id) or {})

    def mark(self, profile_id, signal, device):
        """Record ``signal``; a challenge is never replaced by the milder signed_out."""
        now = round(self.clock() * 1000)
        with self._edit() as data:
            entry = data.setdefault(profile_id, dict(BLANK))
            if entry["signal"] == "challenge" and signal == "signed_out":
                return
            if entry["signal"] != signal:
                entry.update(signal=signal, since=now)
            entry["device"] = device

    def confirm(self, profile_id, device, clear=True):
        now = round(self.clock() * 1000)
        with self._edit() as data:
            entry = data.setdefault(profile_id, dict(BLANK))
            entry["confirmedAt"], entry["device"] = now, device
            if clear and entry["signal"] != "challenge":
                entry["signal"], entry["since"] = None, None

    def clear(self, profile_id, signal=None):
        with self._edit() as data:
            entry = data.get(profile_id)
            if not entry or not entry["signal"] or (signal and entry["signal"] != signal):
                return False
            entry["signal"], entry["since"] = None, None
            return True

    def forget(self, profile_id):
        with self._edit() as data:
            data.pop(profile_id, None)


# -- the service object -------------------------------------------------------------------------------------------

def _goal(text):
    text = " ".join(str(text or "").split())
    return text if len(text) <= GOAL_CHARS else text[:GOAL_CHARS - 1].rstrip() + "…"


class FleetHealth:
    """Computes ``health`` for the fleet's phones and fires the edge-triggered alerts. One per Fleet."""

    def __init__(self, fleet, clock=time.time, monotonic=time.monotonic, read=read_locale, alerts=None, publish=None,
                 background=True, debounce=DEBOUNCE_S):
        self.fleet = fleet
        self.clock, self.monotonic, self.read = clock, monotonic, read
        self._alerts, self._publish, self.background, self.debounce = alerts, publish, background, debounce
        self.signals = Signals(getattr(fleet, "data_dir", None), clock)
        self.lock = threading.Lock()
        self.locales = {}      # phone id -> (monotonic time read, locale or None)
        self.reading = set()   # phone ids being read now
        self.active = {}       # (phone id, code, profile id) -> {first, sent, gone}
        self.baselined = False
        self.sent = []         # (phone id, code, profile id) of every alert delivered, for tests and the log

    # -- inputs --------------------------------------------------------------------------------------------------

    @property
    def runtime(self):
        return self.fleet.runtime

    def profiles(self):
        try:
            from .accounts.store import store_for
            return store_for(self.runtime).all()
        except Exception:  # noqa: BLE001 -- Profiles off: no profile checks
            return []

    def profiles_on(self, phone, item, profiles=None):
        profiles = self.profiles() if profiles is None else profiles
        return [p for p in profiles if p.get("device") == phone.id or (not p.get("device") and item.get("default"))]

    def locale(self, phone, ttl=LOCALE_TTL, read_now=False):
        """The phone's cached locale, or None. ``read_now`` reads again when the cache is older than ``ttl``."""
        with self.lock:
            cached = self.locales.get(phone.id)
        fresh = cached is not None and self.monotonic() - cached[0] < ttl
        if fresh or not read_now:
            return cached[1] if cached else None
        locale = self.read(phone.wda_url)
        with self.lock:
            self.locales[phone.id] = (self.monotonic(), locale)
        return locale

    def readable(self, phone):
        try:
            disconnected = getattr(self.fleet, "is_disconnected", None)
            if callable(disconnected) and disconnected(phone):
                return False
            in_use = getattr(self.fleet, "in_use", None)
            if callable(in_use):
                return not in_use(phone)
            busy = getattr(self.fleet, "busy", None)
            return not (callable(busy) and busy(phone) is not None)
        except Exception:  # noqa: BLE001 -- fail closed: nothing is read from a phone whose state can't be told
            return False

    def _refresh(self, phone, item, wanted):
        """Read an idle ready phone's locale in the background when a profile on it expects one and the cache is old."""
        if (not wanted or item.get("state") != "ready" or item.get("activeRunId") or item.get("disconnected")
                or not phone.wda_url):
            return
        with self.lock:
            cached = self.locales.get(phone.id)
            if phone.id in self.reading or (cached is not None and self.monotonic() - cached[0] < LOCALE_TTL):
                return
            self.reading.add(phone.id)
        if not self.readable(phone):
            with self.lock:
                self.reading.discard(phone.id)
            return

        def work():
            try:
                self.locale(phone, ttl=0, read_now=True)
            finally:
                with self.lock:
                    self.reading.discard(phone.id)
        if self.background:
            _Thread(target=work, name="mobster-locale", daemon=True).start()
        else:
            work()

    def last_good(self, phone):
        runs = getattr(self.runtime, "runs", None) or {}
        best = None
        for run in list(runs.values()):
            if getattr(run, "status", None) != "completed" or getattr(run, "finished_at", None) is None:
                continue
            if getattr(run, "mode", "live") != "live":
                continue
            device = getattr(run, "device_id", None)
            if device != phone.id and not (device is None and phone.primary):
                continue
            if best is None or run.finished_at > best.finished_at:
                best = run
        if best is None:
            return None
        return {"runId": best.id, "at": round(best.finished_at * 1000), "summary": _goal(best.goal)}

    def current_task(self, item):
        run_id = item.get("activeRunId")
        run = (getattr(self.runtime, "runs", None) or {}).get(run_id) if run_id else None
        if run is not None:
            return {"runId": run_id, "goal": _goal(run.goal), "since": round(run.created_at * 1000)}
        holder = item.get("holder")
        if isinstance(holder, dict) and isinstance(holder.get("label"), str):
            since = holder.get("since")
            return {"runId": holder.get("runId") if isinstance(holder.get("runId"), str) else None,
                    "goal": _goal(holder["label"]), "kind": holder.get("kind") if isinstance(holder.get("kind"), str)
                    else None, "since": since if isinstance(since, (int, float)) else None}
        return None

    # -- one phone's health --------------------------------------------------------------------------------------

    def health_of(self, phone, item, profiles=None, signals=None):
        """``health`` for one phone's record. Reads nothing from the phone: its locale comes from the cache."""
        signals = self.signals.all() if signals is None else signals
        name = item.get("name") or "This iPhone"
        mine = self.profiles_on(phone, item, profiles)
        wanted = any(any(expected_of(p).values()) for p in mine)
        self._refresh(phone, item, wanted)
        locale = self.locale(phone)
        alerts = state_alerts(item, name)
        report, rows = [], []
        for profile in mine:
            label = _profile_name(profile)
            stored = signals.get(profile["id"]) or {}
            signal = stored.get("signal")
            checks = compare(profile, locale)
            report.append({"profile": profile["id"], "name": label, "state": region_state(checks), "checks": checks})
            if signal:
                alerts.append(signal_alert(signal, label, _app_name(profile), name, stored.get("since"),
                                           profile["id"]))
            if any(check["state"] == "mismatch" for check in checks):
                alerts.append(region_alert(label, name, checks, None, profile["id"]))
            expected = expected_of(profile)
            rows.append({"id": profile["id"], "name": label, "handle": profile.get("handle"), "app": _app_name(profile),
                         "signedIn": False if signal else (True if stored.get("confirmedAt") else None), **expected})
        overall = "none"
        for entry in report:
            if entry["state"] == "mismatch":
                overall = "mismatch"
            elif entry["state"] == "unknown" and overall != "mismatch":
                overall = "unknown"
            elif entry["state"] == "ok" and overall == "none":
                overall = "ok"
        region = {"state": overall, "profiles": report, "phone": {**{k: (locale or {}).get(k) for k in
                  ("region", "language", "timezone")}, "readAt": self._read_at(phone)}}
        return {"alerts": alerts, "lastGoodRun": self.last_good(phone), "profiles": rows, "region": region,
                "currentTask": self.current_task(item)}

    def _read_at(self, phone):
        with self.lock:
            cached = self.locales.get(phone.id)
        if cached is None or cached[1] is None:
            return None
        return round((self.clock() - (self.monotonic() - cached[0])) * 1000)

    def apply(self, rows, complete=True):
        profiles, signals = self.profiles(), self.signals.all()
        current = {}
        for phone, item in rows:
            if not phone.id:
                continue
            health = self.health_of(phone, item, profiles, signals)
            for alert in health["alerts"]:
                current[(phone.id, alert["code"], alert.get("profile"))] = (item, alert)
            item["health"] = health
        self.observe(current, None if complete else {phone.id for phone, _ in rows if phone.id})
        # The alerts' ``since``: when each condition was first seen, unless the source knew better.
        for phone, item in rows:
            for alert in (item.get("health") or {}).get("alerts", ()):
                if alert["since"] is None:
                    seen = self.active.get((phone.id, alert["code"], alert.get("profile")))
                    alert["since"] = round(seen["first_wall"] * 1000) if seen else round(self.clock() * 1000)

    # -- edge-triggered alerts -----------------------------------------------------------------------------------

    def observe(self, current, phones=None):
        now, wall = self.monotonic(), self.clock()
        delivered = []
        with self.lock:
            first_pass = not self.baselined
            if first_pass:
                if phones is not None:  # the baseline is a read of every phone: one phone's can't tell what held
                    return []
                self.baselined = True
            for key, (item, alert) in current.items():
                entry = self.active.get(key)
                if entry is None:
                    entry = self.active[key] = {"first": now, "first_wall": wall, "sent": first_pass, "gone": None}
                elif entry["gone"] is not None and not entry["sent"]:
                    entry["first"], entry["first_wall"] = now, wall
                entry["gone"] = None
                if not entry["sent"] and now - entry["first"] >= self.debounce:
                    entry["sent"] = True
                    if alert["severity"] == ATTENTION:
                        delivered.append((key, item, alert))
            for key in [k for k in self.active if k not in current and (phones is None or k[0] in phones)]:
                entry = self.active[key]
                if entry["gone"] is None:
                    entry["gone"] = now
                elif now - entry["gone"] >= self.debounce:
                    del self.active[key]
        for key, item, alert in delivered:
            self._deliver(key, item, alert)
        return [key for key, _, _ in delivered]

    def _deliver(self, key, item, alert):
        self.sent.append(key)
        name = item.get("name") or "iPhone"
        body = f"{alert['message']} {alert['action']}"
        event = {"event": "phone_alert", "device": key[0], "deviceName": name, "code": alert["code"],
                 "profile": key[2], "title": f"Mobster: {name}", "body": body}
        with contextlib.suppress(Exception):
            if self._publish is not None:
                self._publish("devices", event)
            else:
                self.runtime.publish("devices", event)
        with contextlib.suppress(Exception):
            alerts = self._alerts
            if alerts is None:
                from .alerts import Alerts
                alerts = self._alerts = Alerts()
            alerts.phone_alert(name, alert["code"], alert["message"], alert["action"])

    # -- the runs ------------------------------------------------------------------------------------------------

    def note_screen(self, account, device, signal):
        if signal in ("challenge", "signed_out") and isinstance(account, dict) and account.get("id"):
            with contextlib.suppress(Exception):
                self.signals.mark(account["id"], signal, device)

    def note_run(self, run):
        """A finished run: keep what it learned about its profile. Never raises."""
        try:
            self._note_run(run)
        except Exception:  # noqa: BLE001 -- health is never part of a run's outcome
            pass

    def _note_run(self, run):
        account = (getattr(run, "extras", None) or {}).get("account")
        if not isinstance(account, dict) or not account.get("id"):
            return
        device = getattr(run, "device_id", None)
        summary = run.summary if isinstance(run.summary, dict) else {}
        latest = None
        for event in list(run.events):
            if event.get("event") == "account_signal" and event.get("account") == account["id"]:
                if event.get("signal") in ("challenge", "signed_out"):
                    latest = "challenge" if event["signal"] == "challenge" or latest == "challenge" else "signed_out"
            elif (event.get("event") == "account_checked" and event.get("account") == account["id"]
                  and event.get("state") == "confirmed" and latest != "challenge"):
                latest = "confirmed"
        if (summary.get("stop_code") or summary.get("code")) in ("account_unconfirmed", "account_mismatch") \
                and latest not in ("challenge", "signed_out"):
            latest = "signed_out"
        if latest in ("challenge", "signed_out"):
            self.signals.mark(account["id"], latest, device)
        elif latest == "confirmed":
            self.signals.confirm(account["id"], device)

    def acknowledge(self, profile_id, code=None):
        """The person says they handled a challenge or a sign-in. True when an alert was cleared."""
        if code is not None and code not in ("challenge", "signed_out"):
            raise ValueError("code is challenge or signed_out")
        return self.signals.clear(profile_id, code)

    # -- the pre-run check ---------------------------------------------------------------------------------------

    def preflight(self, account, phone):
        stored = next((p for p in self.profiles() if p["id"] == account.get("id")), None) or account
        name = phone.name or "This iPhone"
        label = _profile_name(stored)
        signal = self.signals.get(stored["id"]).get("signal") if stored.get("id") else None
        if signal == "challenge":
            raise APIError(f"{_app_name(stored)} asked for a security check for {label}. Finish it on the phone "
                           f"yourself, then choose Mark as handled on the Phones page. {NOT_TAKEN}", 409,
                           "profile_challenge", device=phone.id, profile=stored["id"])
        wanted = expected_of(stored)
        if not any(wanted.values()):
            return
        if not self.readable(phone):
            return  # disconnected, or a task runs there: nothing is read, and the run waits or is refused on its own
        checks = compare(stored, self.locale(phone, ttl=PRE_RUN_TTL, read_now=True))
        if any(check["state"] == "mismatch" for check in checks):
            raise APIError(mismatch_message(label, name, checks), 409, "profile_region_mismatch", device=phone.id,
                           profile=stored["id"], checks=checks)

    # -- the API -------------------------------------------------------------------------------------------------

    def report(self):
        """GET /api/fleet/health: ``{devices: [{id, name, state, health}], checkedAt}``, cheap (Fleet.entries)."""
        rows = self.fleet.entries()
        return {"devices": [{"id": item["id"], "name": item.get("name"), "state": item.get("state"),
                             "health": item.get("health")} for _, item in rows],
                "checkedAt": round(self.clock() * 1000)}

