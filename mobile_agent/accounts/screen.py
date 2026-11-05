"""Which profile is signed in, read off the screen, and the check before every commit as that profile."""

import re
import threading
import time

from .store import display

TOP = .45                      # the top part of the screen a profile's handle sits in (screen fractions)
MAX_REFUSALS = 3
# Own-profile controls in the apps' own words (English UI): "Edit profile", "Edit Profile", "Share profile".
OWN_PROFILE = re.compile(r"^\s*(?:edit|share)\s+profile\b", re.I)
# Controls only someone else's profile shows.
OTHER_PROFILE = re.compile(r"^\s*(?:follow|following|follow back|requested|message|subscribe)\s*$", re.I)
HANDLE_TOKEN = re.compile(r"(?<![A-Za-z0-9_.@-])@([A-Za-z0-9._-]{1,64}?)(?=[^A-Za-z0-9_.-]|\.(?![A-Za-z0-9_-])|$)")
TITLE_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,64}")

VERBS = {"post": ("post", "posting"), "comment": ("comment", "commenting"), "reply": ("reply", "replying"),
         "send_message": ("send", "sending"), "react": ("react", "reacting"), "follow": ("follow", "following"),
         "unfollow": ("unfollow", "unfollowing"), "share": ("share", "sharing"), "delete": ("delete", "deleting")}


def _texts(element):
    return [t for t in (getattr(element, "label", "") or "", getattr(element, "value", "") or "") if t]


def _top(element):
    x, y, w, h = element.rect
    return y + h / 2 <= TOP


def shows(text, handle):
    """Whether ``text`` names ``handle`` as a whole token, with or without its @ ("acme", "@acme", "acme ✓")."""
    pattern = r"(?<![A-Za-z0-9_.@-])@?" + re.escape(handle) + r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])"
    return re.search(pattern, text or "", re.I) is not None


def own_profile(snapshot):
    """Whether ``snapshot`` is the signed-in profile's own page: an own-profile control, and no Follow or Message."""
    elements = list(getattr(snapshot, "elements", ()) or ())
    labels = [(e.label or "").strip() for e in elements]
    return any(OWN_PROFILE.search(l) for l in labels) and not any(OTHER_PROFILE.fullmatch(l) for l in labels if l)


def read(snapshot, handle, known=()):
    """("confirmed", "@acme") | ("mismatch", "@other" or None) | ("unknown", None) for ``handle`` on ``snapshot``."""
    if not own_profile(snapshot):
        return "unknown", None
    top = [e for e in getattr(snapshot, "elements", ()) or () if _top(e)]
    if any(shows(text, handle) for e in top for text in _texts(e)):
        return "confirmed", "@" + handle
    for other in known:
        if other.casefold() != handle.casefold() and any(shows(t, other) for e in top for t in _texts(e)):
            return "mismatch", "@" + other
    for e in top:
        for text in _texts(e):
            found = HANDLE_TOKEN.search(text)
            if found and found.group(1).casefold() != handle.casefold():
                return "mismatch", "@" + found.group(1)
    # The navigation bar's title on an own profile is the username (Instagram, Threads).
    bar = next((e for e in top if e.role == "NavigationBar" and (e.label or "").strip()), None)
    if bar is not None:
        title = " ".join(bar.label.split()).split(",")[0].strip()
        if TITLE_TOKEN.fullmatch(title):
            return "mismatch", "@" + title
    return "mismatch", None


class AccountCheck:
    """One run's profile: what was read in its app, and the commit check."""

    def __init__(self, account, known=(), device_name=None, emit=None):
        self.account = account
        self.handle = account["handle"]
        self.bundle = account["app"]["bundleId"]
        self.app_name = account["app"]["name"]
        self.known = tuple(known)
        self.device_name = device_name
        self.emit = emit or (lambda event: None)
        self.state, self.shown = "unknown", None
        # Pacing (accounts/pacing.py) and the run's stop, set by check_for; None in a run without them.
        self.pacing = None
        self.cancelled = lambda: False
        self.on_signal = None   # called with "challenge" or "signed_out" when the screen shows one
        self.media = None       # content_queue.media_check.MediaCheck for a queued post's task (the queue checks on)
        self.refusals = 0
        self.lock = threading.Lock()

    @property
    def acting_as(self):
        return display(self.account)

    def look(self, snapshot):
        """Read ``snapshot`` (in the profile's app only) and keep what it says. Returns (state, shown)."""
        if getattr(snapshot, "bundle_id", None) != self.bundle:
            return "unknown", None
        state, shown = read(snapshot, self.handle, self.known)
        if state != "confirmed":
            # A sign-in page or a security check in the profile's app, on a screen that doesn't show the profile: kept
            # at once for the Phones page and the pre-run check (fleet_health.py), and as an event. The screen's text is
            # only matched, never followed.
            from ..fleet_health import classify_screen, snapshot_labels
            signal = classify_screen(snapshot_labels(snapshot))
            if signal is not None:
                self.emit({"event": "account_signal", "account": self.account["id"], "signal": signal})
                if self.on_signal is not None:
                    try:
                        self.on_signal(signal)
                    except Exception:  # noqa: BLE001 -- keeping a sighting is never part of the run's outcome
                        pass
        with self.lock:
            if state != "unknown":
                self.state, self.shown = state, shown
        self.emit({"event": "account_checked", "account": self.account["id"], "state": state})
        return state, shown

    def commit(self, act, snapshot):
        """The verdict before a commit ``act`` on ``snapshot``: None (not in the profile's app), {"acting_as"},
        {"refuse": feedback} or {"stop": reason, "code"}."""
        if getattr(snapshot, "bundle_id", None) != self.bundle:
            return None
        # A later own-profile screen may show a different account. Other screens retain the last check.
        self.look(snapshot)
        with self.lock:
            state, shown = self.state, self.shown
        if state == "mismatch":
            return {"stop": self.mismatch_line(act, shown), "code": "account_mismatch"}
        if state == "confirmed":
            paced = self.paced(act)
            if paced is not None:
                return paced
            if self.media is not None and act in self.media.POST_ACTS:
                # A queued post: the app's limited-photos notice ends the task here; otherwise the frontier hands
                # the screen to ``preview``, and the batch approval answers this Post only when it holds the media.
                return self.media.stop(snapshot) or {"acting_as": self.acting_as, "preview": self.media.preview}
            return {"acting_as": self.acting_as}
        with self.lock:
            self.refusals += 1
            if self.refusals >= MAX_REFUSALS:
                return {"stop": self.unconfirmed_line(act), "code": "account_unconfirmed"}
        return {"refuse": (f"Refused: confirm the profile first. Open {self.app_name}'s own profile screen (the one "
                           f"with Edit profile), use CHECK_PROFILE there, and once it confirms @{self.handle}, come "
                           "back and do this.")}

    def paced(self, act):
        """The profile's pacing before ``act`` (accounts/pacing.py): None to go on."""
        if self.pacing is None:
            return None
        from .pacing import WAIT_MAX_S
        try:
            verdict = self.pacing.check(self.account["id"], act)
        except Exception:  # noqa: BLE001
            return {"stop": "Mobster couldn't read this profile's pacing, so it stopped before acting.",
                    "code": "pacing_unreadable"}
        if verdict is None:
            return None
        if "wait" in verdict and verdict["wait"] <= WAIT_MAX_S:
            self.emit({"event": "pacing_wait", "account": self.account["id"], "seconds": round(verdict["wait"])})
            end = time.monotonic() + verdict["wait"]
            while time.monotonic() < end:
                if self.cancelled():
                    return {"stop": "Stopped while waiting for this profile's pace.", "code": "pacing_wait"}
                time.sleep(min(1.0, end - time.monotonic()))
            return None
        if "wait" in verdict:
            minutes = round(verdict["wait"] / 60)
            stop = {"stop": f"@{self.handle} {verdict['reason']}; the next can go in {minutes} minutes. Mobster "
                    "stopped before acting.", "code": "pacing_gap"}
        else:
            stop = {"stop": f"@{self.handle}: {verdict['stop']}", "code": verdict.get("code") or "pacing"}
        if isinstance(verdict.get("until"), (int, float)):
            stop["until"] = verdict["until"]
        # The task's record of why it stopped and when the profile may act again (a workflow waits for it).
        self.emit({"event": "pacing_stop", "account": self.account["id"], "code": stop["code"],
                   **({"until": stop["until"]} if "until" in stop else {})})
        return stop

    def mismatch_line(self, act, shown):
        verb = VERBS.get(act, ("do this", "acting"))
        who = shown or "another profile"
        return f"This would {verb[0]} as {who}, not @{self.handle}. Mobster stopped before {verb[1]}."

    def unconfirmed_line(self, act):
        verb = VERBS.get(act, ("do this", "acting"))[1]
        where = f" on {self.device_name}" if self.device_name else ""
        return (f"Mobster couldn't confirm on screen that @{self.handle} is signed in to {self.app_name}, so it "
                f"stopped before {verb}. Open {self.app_name}{where}, make sure @{self.handle} is the profile signed "
                f"in, and run the task again.")


class CheckAccount:
    """The CHECK_PROFILE tool (agent_hooks.Skill): reads the screen at hand and says what it shows."""

    op = "CHECK_PROFILE"
    needs_target = False

    def __init__(self, check):
        self.check = check
        self.prompt = (f"- CHECK_PROFILE: confirm the signed-in profile is @{check.handle} in {check.app_name}. Use it "
                       "on the app's own profile screen (the one with Edit profile) before you post, reply, comment "
                       "or send a message.")

    def available(self, context):
        return True

    def prepare(self, context, act, snapshot):
        from ..agent_hooks import Prepared
        return Prepared(title=None)

    def perform(self, context, prepared, act, snapshot):
        from ..agent_hooks import GuardVerdict, SkillResult
        if getattr(snapshot, "bundle_id", None) != self.check.bundle:
            return SkillResult(f"This screen isn't {self.check.app_name}. Open {self.check.app_name}'s own profile "
                               "screen, then use CHECK_PROFILE.")
        state, shown = self.check.look(snapshot)
        if state == "confirmed":
            return SkillResult(f"Confirmed: @{self.check.handle} is the profile signed in to {self.check.app_name}. "
                               "Go on with the task.")
        if state == "mismatch":
            line = (f"{shown or 'Another profile'} is signed in to {self.check.app_name}, not @{self.check.handle}. "
                    "Mobster stopped before acting as the wrong profile. Switch to "
                    f"@{self.check.handle} in {self.check.app_name}, then run the task again.")
            return SkillResult(line, stop=GuardVerdict("stop", "account_mismatch", line))
        return SkillResult(f"This isn't {self.check.app_name}'s own profile screen (it has no Edit profile, or it "
                           f"shows Follow or Message). Open the profile tab, then use CHECK_PROFILE.")


_checks = {}
_checks_lock = threading.Lock()


def check_for(run_context):
    """The run's AccountCheck, made once per run from its ``account`` field; None for a run with no profile."""
    extras = getattr(run_context, "extras", None) or {}
    account = extras.get("account")
    if not isinstance(account, dict) or not account.get("id"):
        return None
    with _checks_lock:
        check = _checks.get(run_context.run_id)
        if check is None:
            from .store import store_for
            known = ()
            try:
                known = tuple(a["handle"] for a in store_for(run_context.runtime).all()
                              if a["app"]["bundleId"] == account["app"]["bundleId"] and a["id"] != account["id"])
            except Exception:  # noqa: BLE001 -- the other handles only name a mismatch better
                pass
            check = _checks[run_context.run_id] = AccountCheck(account, known, account.get("deviceName"),
                                                               emit=run_context.emit)
            try:
                from .pacing import pacing_for
                check.pacing = pacing_for(run_context.runtime)
            except Exception:  # noqa: BLE001 - a runtime with no data folder (tests): no pacing to keep
                check.pacing = None
            check.cancelled = getattr(run_context, "cancelled", None) or (lambda: False)
            from ..content_queue.media_check import for_run
            check.media = for_run(run_context)
            health = getattr(getattr(run_context.runtime, "fleet", None), "health", None)
            if health is not None:
                device = run_context.device_id
                check.on_signal = lambda signal: health.note_screen(account, device, signal)
        return check


def forget(run_id):
    with _checks_lock:
        _checks.pop(run_id, None)


def commit_check_for(run_context):
    """The frontier's ``commit_check`` for this run, or None (no profile: the frontier is as before)."""
    check = check_for(run_context)
    return check.commit if check is not None else None
