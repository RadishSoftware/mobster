"""iOS's own dialogs (permission prompts, Allow Paste, tracking, Open in "App"?, Save Password) answered under the
user's permission mode, never by a blanket accept.

``read(driver)`` asks WebDriverAgent for the dialog in front (GET /alert/text and /wda/alert/buttons: its FBAlert
looks in SpringBoard first, then the app in front, then the iOS 18 contacts prompt). ``classify`` names what iOS is
asking from the text and the buttons. ``decide`` turns that into one of four answers:

- ``press``: Mobster presses one button itself, with a reason the user reads in the thread ("Don't Allow: a task
  doesn't need notifications").
- ``ask``: the user chooses (the approval card, or the phone when nobody can be asked).
- ``stop``: nobody can be asked and the choice isn't Mobster's to make: the run ends and says what iOS asked, with
  the dialog left up for the user.
- ``leave``: not one of iOS's dialogs (an app's own alert, "Delete this post?"): the agent handles it like any
  screen, through the usual approvals.

Rules that hold in every mode:
- A grant of private data or hardware (location, photos, camera, microphone, contacts, calendars, Bluetooth, local
  network, health, motion...) is the user's call: asked, never pressed, in full access too (as a one-time code the
  agent types is: permissions.policy).
- Saving a password, signing in to an Apple Account, iOS asking for its password and Trust This Computer are never
  pressed and never answered for the user. They are known by Apple's wording: an app's own "Incorrect password"
  alert is the app's.
- Allow Paste is pressed only within PASTE_WINDOW_S of Mobster putting text on the clipboard itself
  (``note_paste``). The clipboard's contents aren't read back: the window stands for "the text is Mobster's". Any
  other paste prompt is refused ("Don't Allow Paste").
- Open in "App"? is pressed only right after Mobster opened that link itself (``note_open``).
- An iOS update nag gets "Later" (installing restarts the phone); a Screen Time limit stops the run (a limit is never
  ignored for the user); Undo Typing (a shake) gets "Cancel", never "Undo"; a website sign-in asks.
- A connection that sees only pixels reads the alert from the screen (``from_screen``) and taps its button by name;
  it recognizes only iOS's own kinds of alert, so an app's alert stays the agent's, under its approvals.

The mode is the user's permission mode (``permission_mode``): ask_all, ask_important (default) or dont_ask, read
the way ``permissions.policy`` reads it (MOBSTER_PERMISSION_MODE; MOBSTER_ASK_BEFORE_ACTING=0 as
dont_ask), and through that package when it is present. Runs an outside agent started (MCP, Remote) never go below
ask_important.
"""

import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, replace

ASK_ALL, ASK_IMPORTANT, DONT_ASK = "ask_all", "ask_important", "dont_ask"
MODES = (ASK_ALL, ASK_IMPORTANT, DONT_ASK)
OUTSIDE_ORIGINS = frozenset({"mcp", "remote", "hook", "trigger", "console"})
# How long after Mobster set the clipboard (or opened a link) the dialog that follows is taken to be its own.
PASTE_WINDOW_S = 20.0
OPEN_WINDOW_S = 15.0
READ_TIMEOUT = 3.0

# Privacy grants: what iOS asks to share, in the words the approval card uses ("Let Maps use your location?").
GRANTS = {
    "location": "use your location",
    "location_always": "use your location even when you're not using it",
    "photos": "see your photos",
    "photos_add": "add to your photos",
    "camera": "use the camera",
    "microphone": "use the microphone",
    "contacts": "see your contacts",
    "calendar": "use your calendar",
    "reminders": "use your reminders",
    "bluetooth": "use Bluetooth",
    "local_network": "find devices on your local network",
    "motion": "read your motion and fitness activity",
    "speech": "use speech recognition",
    "media": "use your Apple Music and media library",
    "home": "use your Home data",
    "health": "use your health data",
    "face_id": "use Face ID",
    "focus": "see your Focus status",
    "permission": "use something private on your iPhone",
}
NEVER = {"save_password", "apple_account", "trust_computer", "password_field"}

_Q = "[\"“”'‘’]"
PATTERNS = (
    # (kind, regex on the folded text). Order matters: the most restrictive first, then the more precise. What Mobster
    # never answers (NEVER), a Screen Time limit, a purchase and Sign in with Apple come before every grant, so a
    # password prompt or a purchase sheet that also names a grant ("an email address", "Apple Music") is never
    # pressed as one.
    ("save_password", re.compile(r"save (?:this )?password|save to passwords|icloud keychain|update password\?",
                                 re.I)),
    # iOS's own request for an Apple Account password or sign-in wins over the sheets below: a purchase or Sign in
    # with Apple sheet that asks for one is an Apple Account prompt, which Mobster never answers (NEVER). It is read
    # from Apple's wording, because classify() gets only the alert's text and buttons, not the process that shows it:
    # an app's own "Incorrect password" alert names no Apple Account and stays the app's.
    ("apple_account", re.compile(r"\b(?:apple (?:account|id)|icloud|itunes(?: store)?|app store) password\b|"
                                 r"\bpassword for (?:your )?(?:apple (?:account|id)|icloud|itunes)\b|"
                                 r"\benter (?:the |your )?(?:apple (?:account|id) )?password\b|"
                                 r"\bapple (?:account|id) sign[- ]in (?:requested|required)\b|"
                                 r"\bapple (?:account|id) e-?mail(?: address)?(?: or phone number)? and password\b|"
                                 r"sign in to (?:icloud|itunes store|the app store|(?:your )?apple (?:account|id))\b",
                                 re.I)),
    ("trust_computer", re.compile(r"trust this computer", re.I)),
    ("screen_time", re.compile(r"^time limit\b|reached your limit on|you['’]ve reached your limit", re.I)),
    # The sheets an app presents through iOS: not alerts, so a check reads them from Apple's own process (SHEETS).
    # The alert after a purchase counts only with StoreKit's environment tag: an app's own "You're all set" is the
    # app's. A sheet that only names the Apple Account it uses stays a sheet.
    ("purchase", re.compile(r"confirm your (?:in-app )?purchase|\[environment: (?:sandbox|xcode)\]|"
                            r"do you want to (?:buy|subscribe)|confirm with (?:the )?(?:side button|passcode|face id|"
                            r"touch id)|double[- ]click to pay|pay with (?:passcode|face id|touch id)", re.I)),
    ("sign_in_with_apple", re.compile(r"sign in with apple|continue with apple|"
                                      r"sign in to .{1,60} with your apple (?:account|id)", re.I)),
    # Any other mention of an Apple Account, including one beside Face ID or Touch ID ("Apple Account. Continue with
    # Face ID to sign in"), is an Apple Account prompt: it wins over every grant and over biometric below.
    ("apple_account", re.compile(r"apple account|apple id|itunes store|sign in to icloud|enter (?:the|your) password",
                                 re.I)),
    ("paste", re.compile(r"would like to paste from|wants to paste from|paste from other apps", re.I)),
    ("tracking", re.compile(r"to track your activity across other companies", re.I)),
    ("open_in_app", re.compile(rf"^open in {_Q}?.+{_Q}?\??$", re.I)),
    ("location_always", re.compile(r"also use your location even when|change to always allow", re.I)),
    ("location", re.compile(r"use your location|access your location|your current location", re.I)),
    ("notifications", re.compile(r"would like to send you notifications|send you notifications", re.I)),
    ("photos_add", re.compile(r"would like to add to your photos|add to your photos|add photos", re.I)),
    ("photos", re.compile(r"access your photos|access to your photo|photo library|your photos", re.I)),
    ("camera", re.compile(r"access the camera|use the camera|access your camera", re.I)),
    ("microphone", re.compile(r"access the microphone|use the microphone|access your microphone", re.I)),
    ("contacts", re.compile(r"access (?:to )?your contacts|your contacts", re.I)),
    ("calendar", re.compile(r"your calendar|add events to", re.I)),
    ("reminders", re.compile(r"your reminders", re.I)),
    ("bluetooth", re.compile(r"use bluetooth|bluetooth", re.I)),
    ("local_network", re.compile(r"local network", re.I)),
    ("motion", re.compile(r"motion (?:&|and) fitness", re.I)),
    ("speech", re.compile(r"speech recognition", re.I)),
    ("media", re.compile(r"apple music|media library", re.I)),
    ("home", re.compile(r"your home data|homekit", re.I)),
    ("health", re.compile(r"health data|\bhealth\b", re.I)),
    ("focus", re.compile(r"focus status", re.I)),
    ("face_id", re.compile(r"to use face id", re.I)),
    ("biometric", re.compile(r"(?:authenticate|unlock|sign in|log in|continue) with (?:face|touch) id|"
                             r"try (?:face|touch) id again|(?:face|touch) id (?:not recognized|didn['’]t recognize)|"
                             r"(?:face|touch) id is (?:locked|disabled)", re.I)),
    ("software_update", re.compile(r"\b(?:ios|software) update\b|\bupdate is (?:now )?available|is now available|"
                                   r"\binstall (?:now|tonight)\b", re.I)),
    ("web_sign_in", re.compile(r"(?:wants|would like) to use .{1,80} to sign in", re.I)),
    ("undo_typing", re.compile(r"^undo typing\b", re.I)),
    ("rating", re.compile(r"^enjoying .+\?|rate (?:this app|us)|tap a star to rate", re.I)),
    ("low_battery", re.compile(r"^low battery\b", re.I)),
)
ALLOW = re.compile(r"^(?:allow(?: once| while using(?: the)? app| full access| paste)?|ok|open|continue|"
                   r"change to always allow|save password)$", re.I)
DENY = re.compile(r"^(?:don['’]?t allow(?: paste)?|ask app not to track|cancel|not now|keep only while using|"
                  r"close|later|remind me later|don['’]?t trust|never for this website)$", re.I)
# The buttons of iOS's own alerts, as they read on screen: a connection that sees only pixels finds an alert by them.
BUTTONS = frozenset(name.casefold() for name in (
    "Allow", "Don't Allow", "Allow Once", "Allow While Using App", "Allow While Using the App", "Ask App Not to Track",
    "Allow Paste", "Don't Allow Paste", "OK", "Cancel", "Not Now", "Open", "Limit Access...", "Limit Access…",
    "Allow Full Access", "Select Photos...", "Select Photos…", "Close", "Later", "Remind Me Later", "Install Now",
    "Install Tonight", "Low Power Mode", "Trust", "Don't Trust", "Continue", "Keep Only While Using",
    "Change to Always Allow", "Undo", "Save Password", "Never for This Website", "Ignore Limit", "Settings",
    "Buy", "Subscribe", "Confirm", "Pay", "Continue with Passcode", "Use Passcode", "Enter Password",
    "Try Face ID Again", "Try Touch ID Again"))
# The kinds iOS presents as sheets over an app. A check's policy (verify/dialogs.py) reads them from the screen of
# Apple's own process; an agent task never does, so its behavior is unchanged.
SHEETS = frozenset({"sign_in_with_apple", "purchase", "biometric"})
SHEET_NOT_TEXT = frozenset({"Button", "Cell", "Link", "Switch", "TextField", "SecureTextField"})
# The processes that show iOS's own dialogs over an app, by exact bundle, never a pattern: Safari's web content
# (com.apple.SafariViewService) is a view service too, and a web page writes what it likes. SpringBoard shows the
# alerts; StoreKit, AuthKit and CoreAuthUI each show one kind of sheet and that kind's own alerts.
SPRINGBOARD = "com.apple.springboard"
STOREKIT = "com.apple.ios.StoreKitUIService"
AUTHKIT = "com.apple.AuthKitUIService"
CORE_AUTH = "com.apple.CoreAuthUI"
SHEET_FRONT = {"purchase": STOREKIT, "sign_in_with_apple": AUTHKIT, "biometric": CORE_AUTH}
APPLE_DIALOGS = frozenset({SPRINGBOARD, *SHEET_FRONT.values()})


def shows_alert(kind, bundle):
    """Whether ``bundle`` in front is the Apple process that shows a ``kind`` alert: SpringBoard, or for a purchase's
    all-set alert StoreKit and for Face ID's own alerts CoreAuthUI."""
    return bundle == SPRINGBOARD or (bundle is not None and SHEET_FRONT.get(kind) == bundle)


def shows_sheet(kind, bundle):
    """Whether ``bundle`` in front is the Apple process that shows a ``kind`` sheet. A sheet of what Mobster never
    answers counts from any of them, since it only stops the run."""
    if kind in SHEET_FRONT:
        return SHEET_FRONT[kind] == bundle
    return kind in NEVER and bundle in SHEET_FRONT.values()


def front(driver, timeout=2):
    """The bundle WebDriverAgent reports in front, or None when it can't say."""
    active = getattr(driver, "active_app", None)
    try:
        bundle = active(timeout=timeout) if callable(active) else None
    except Exception:
        return None
    return bundle if isinstance(bundle, str) and bundle else None


class DialogStop(Exception):
    """A dialog a check's run may not answer: ``str(error)`` is the sentence for the report."""

    def __init__(self, reason, dialog=None):
        super().__init__(reason)
        self.dialog = dialog


def fold(text):
    """Plain text for matching: curly quotes and dashes straight, spaces single."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = text.translate({0x2019: "'", 0x2018: "'", 0x201C: '"', 0x201D: '"', 0x2026: "...", 0x2013: "-",
                           0x2014: "-", 0xA0: " "})
    return " ".join(text.split())


def app_named(text):
    """The app a dialog names, from its quotes ("“Maps” Would Like…", "Allow “Maps” to…"); "" when none."""
    found = re.search(r"[“\"]([^”\"]{1,60})[”\"]", str(text or ""))
    return found.group(1).strip() if found else ""


@dataclass(frozen=True)
class Dialog:
    text: str
    buttons: tuple
    kind: str = "unknown"            # a PATTERNS kind, "permission" (Allow / Don't Allow), or "unknown"
    app: str = ""
    on_screen: bool = False          # read from the screen (a connection that sees only pixels), not from WDA
    lines: tuple = ()                # WDA's alert text line by line: a title and a message are separate labels

    @property
    def grant(self):
        return self.kind in GRANTS

    def button(self, *wanted):
        """The dialog's own button name for the first of ``wanted`` (plain-text compare), or None."""
        plain = {fold(b).casefold(): b for b in self.buttons}
        for name in wanted:
            if name and fold(name).casefold() in plain:
                return plain[fold(name).casefold()]
        return None

    def allow_button(self):
        """The narrowest grant on offer: Allow Once before Allow While Using App, Limit Access before Full Access."""
        return (self.button("Allow Once", "Allow While Using App", "Allow While Using the App", "Limit Access...",
                            "Select Photos...", "Select Contacts...", "Allow", "Allow Full Access", "OK", "Continue",
                            "Allow Paste", "Open")
                or next((b for b in self.buttons if ALLOW.match(fold(b))), None))

    def deny_button(self):
        return (self.button("Don't Allow", "Ask App Not to Track", "Don't Allow Paste", "Not Now", "Cancel",
                            "Keep Only While Using", "Close", "Later", "Remind Me Later", "Don't Trust")
                or next((b for b in self.buttons if DENY.match(fold(b))), None))

    def public(self):
        return {"kind": self.kind, "app": self.app or None, "text": self.text[:300], "buttons": list(self.buttons)}


def classify(text, buttons=()):
    """A Dialog for ``text`` and its ``buttons`` (strings, WDA's names kept as they are)."""
    buttons = tuple(str(b)[:120] for b in buttons or () if isinstance(b, str) and b.strip())[:8]
    plain = fold(text)
    kind = next((name for name, pattern in PATTERNS if pattern.search(plain)), None)
    if kind is None:
        names = {fold(b).casefold() for b in buttons}
        kind = "permission" if "allow" in names and any(n.startswith("don't allow") for n in names) else "unknown"
    lines = tuple(line for line in (fold(part) for part in str(text or "").splitlines()) if line)[:8]
    return Dialog(text=plain[:400], buttons=buttons, kind=kind, app=app_named(text), lines=lines)


def read(driver, timeout=READ_TIMEOUT, snapshot=None):
    """The dialog in front as a Dialog, or None. Never raises: a runner that doesn't answer reads as no dialog.
    A connection without WebDriverAgent (iPhone Mirroring, Cable + Bluetooth) reads it from ``snapshot``
    (``from_screen``)."""
    if not callable(getattr(driver, "call", None)):
        return from_screen(snapshot) if snapshot is not None else None
    try:
        text = driver.call("GET", "/alert/text", timeout=timeout)
    except Exception:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        buttons = driver.call("GET", "/wda/alert/buttons", timeout=timeout)
    except Exception:
        buttons = []
    dialog = classify(text, buttons if isinstance(buttons, list) else [])
    # WebDriverAgent's alert text has no fields: a password field in Apple's own tree makes it a password prompt.
    if getattr(snapshot, "bundle_id", "") in APPLE_DIALOGS and _secure_field(snapshot):
        return replace(dialog, kind="password_field")
    return dialog


def in_tree(dialog, snapshot):
    """Whether WebDriverAgent's alert text sits in ``snapshot``'s tree, the screen the agent reads."""
    head = fold(dialog.text)[:40].casefold()
    return not dialog.on_screen and bool(head) and any(
        head in fold(getattr(e, "label", "") or "").casefold() for e in getattr(snapshot, "elements", ()) or ())


def in_app(dialog, snapshot):
    """Whether WebDriverAgent's alert is the app's own: any of its lines (/alert/text joins a title and a message with
    a line break, and the tree has them as separate labels) sits in the tree of a process that isn't Apple's dialogs.
    Safari's web content counts as the app's: a web page's confirm() is no dialog of iOS's."""
    if dialog.on_screen or getattr(snapshot, "bundle_id", "") in APPLE_DIALOGS:
        return False
    labels = [fold(getattr(e, "label", "") or "").casefold() for e in getattr(snapshot, "elements", ()) or ()]
    wanted = {fold(dialog.text)[:40].casefold(), *(line.casefold() for line in dialog.lines)} - {""}
    return any(line in label for line in wanted for label in labels)


def _secure_field(snapshot, top=0.0, bottom=1.0):
    """Whether a password field sits between ``top`` and ``bottom`` (fractions of the screen's height)."""
    return any(top <= y + h / 2 <= bottom for _, y, _, h in getattr(snapshot, "secure_fields", ()) or ())


def from_screen(snapshot, kinds=None):
    """iOS's alert on a screen read as text, or None; only kinds iOS shows count, so an app's own OK stays the app's.
    With ``kinds``, only those kinds count."""
    elements = list(getattr(snapshot, "elements", ()) or ())
    buttons = [e for e in elements if fold(e.label).casefold() in BUTTONS and .2 <= e.center[1] <= .95]
    if not buttons:
        return None
    top = min(e.center[1] for e in buttons)
    # A sheet's words are static text: an app's own "Sign in with Apple" button above its Continue is not the sheet.
    lines = [e for e in elements if e not in buttons and top - .32 <= e.center[1] < top - .005
             and .08 <= e.center[0] <= .92 and fold(e.label) and (kinds is None or e.role not in SHEET_NOT_TEXT)]
    lines.sort(key=lambda e: (round(e.center[1], 3), e.center[0]))
    text = " ".join(fold(e.label) for e in lines)
    names = [e.label for e in sorted(buttons, key=lambda e: (round(e.center[1], 2), e.center[0]))]
    dialog = classify(text, names)
    if dialog.kind == "unknown" or (kinds is not None and dialog.kind not in kinds):
        return None
    # A password field among the dialog's lines and buttons makes it a password prompt, whatever its words say.
    kind = "password_field" if _secure_field(snapshot, top - .32, max(e.center[1] for e in buttons)) else dialog.kind
    return Dialog(text=dialog.text, buttons=dialog.buttons, kind=kind, app=dialog.app, on_screen=True)


def mentions_dialog(snapshot):
    """Whether a screen's text reads like one of iOS's own dialogs: a cheap look before asking WebDriverAgent."""
    for element in getattr(snapshot, "elements", ()) or ():
        text = fold(getattr(element, "label", "") or "")
        if 12 <= len(text) <= 300 and any(pattern.search(text) for kind, pattern in PATTERNS
                                          if kind not in ("rating", "low_battery", "health", "bluetooth")):
            return True
    return False


@dataclass
class Recent:
    """What Mobster itself just did that makes a following dialog its own: a clipboard it set, a link it opened."""
    pasted_at: float | None = None
    opened_at: float | None = None
    opened_url: str = ""


_recent_lock = threading.Lock()


def recent(driver):
    """The Recent record owned by ``driver``, for that driver's lifetime."""
    with _recent_lock:
        found = getattr(driver, "recent_dialogs", None)
        if found is None:
            found = Recent()
            driver.recent_dialogs = found
        return found


def note_paste(driver, clock=time.monotonic):
    recent(driver).pasted_at = clock()


def note_open(driver, url, clock=time.monotonic):
    record = recent(driver)
    record.opened_at, record.opened_url = clock(), str(url or "")[:2000]


def permission_mode(origin=None, env=None):
    """ask_all | ask_important | dont_ask: the user's permission mode for a run of ``origin``.

    Through PR #143's ``permissions.policy`` when it is in this build; else the same settings it reads:
    MOBSTER_PERMISSION_MODE, then MOBSTER_ASK_BEFORE_ACTING=0 as dont_ask, else ask_important. A run an outside
    agent started (MCP, Remote) never goes below ask_important."""
    if env is None:
        try:
            from ..permissions import policy  # noqa: F401  (PR #143; absent on builds without it)
            settings = policy.from_env()
            return policy.effective_mode(settings.mode, None, origin)
        except Exception:
            pass
    env = os.environ if env is None else env
    mode = (env.get("MOBSTER_PERMISSION_MODE") or "").strip()
    if mode not in MODES:
        mode = DONT_ASK if (env.get("MOBSTER_ASK_BEFORE_ACTING") or "1").strip() == "0" else ASK_IMPORTANT
    if origin in OUTSIDE_ORIGINS and mode == DONT_ASK:
        mode = ASK_IMPORTANT
    return mode


@dataclass(frozen=True)
class Decision:
    action: str                  # "press" | "ask" | "stop" | "leave"
    dialog: Dialog
    button: str = ""             # press: the button; ask: the one Mobster suggests (the narrowest grant)
    deny: str = ""               # ask: the button a decline presses ("" when there is none to press)
    reason: str = ""             # one plain sentence for the thread and the model
    title: str = ""              # ask: the approval card's question
    perform: object = None       # press: what answers the dialog when it is no button (a simulator's Face ID match)

    def public(self):
        return {"action": self.action, "kind": self.dialog.kind, "app": self.dialog.app or None,
                "button": self.button or None, "reason": self.reason}


def _who(dialog):
    return f"“{dialog.app}”" if dialog.app else "An app"


def decide(dialog, mode=ASK_IMPORTANT, *, can_ask=True, recent_=None, in_app=False, now=None, clock=time.monotonic):
    """What to do about ``dialog`` under ``mode``."""
    if dialog is None:
        return None
    mode = mode if mode in MODES else ASK_IMPORTANT
    now = clock() if now is None else now
    kind, who = dialog.kind, _who(dialog)
    allow, deny = dialog.allow_button(), dialog.deny_button()

    def ask(title, suggested=None, why=""):
        """The user chooses on the approval card; with nobody to ask, the run stops and leaves the dialog up."""
        if can_ask and dialog.buttons:
            return Decision("ask", dialog, suggested or allow or dialog.buttons[0], deny or "", why or
                            "iOS is asking you, so Mobster asks you too.", title)
        return Decision("stop", dialog, reason=f"{title.rstrip('?')}: iOS is asking, and Mobster leaves that to "
                                               "you. Answer it on your iPhone, then run the task again.")

    if kind == "sign_in_with_apple":
        return Decision("stop", dialog, reason="Your iPhone asks to sign in with your Apple Account. Mobster never "
                                               "answers that for you: answer it on your iPhone, then run the task "
                                               "again.")
    if kind == "purchase":
        return Decision("stop", dialog, reason="Your iPhone asks you to confirm a purchase. Mobster never confirms one "
                                               "for you: answer it on your iPhone, then run the task again.")
    if kind in NEVER:
        if kind == "save_password" and deny:
            return Decision("press", dialog, deny, reason="Mobster never saves a password for you.")
        if kind == "trust_computer":
            return Decision("stop", dialog, reason="Your iPhone asks whether to trust a computer. Mobster never "
                                                   "answers that: answer it on your iPhone.")
        return Decision("stop", dialog, reason="Your iPhone is asking for your Apple Account or a password. "
                                               "Mobster never answers that: answer it on your iPhone, then run "
                                               "the task again.")
    if kind == "paste":
        pasted_at = recent_.pasted_at if recent_ is not None else None
        mine = pasted_at is not None and 0 <= now - pasted_at <= PASTE_WINDOW_S
        if mine and allow:
            return Decision("press", dialog, allow, reason="the text being pasted is what Mobster just typed.")
        if mode == ASK_ALL and can_ask:
            return ask(f"Let {who} paste from your clipboard?", deny,
                       "Mobster didn't put anything on your clipboard.")
        if deny:
            return Decision("press", dialog, deny, reason="Mobster didn't put anything on the clipboard, so it "
                                                          "stays private.")
        return ask(f"Let {who} paste from your clipboard?")
    if kind == "open_in_app":
        opened_at = recent_.opened_at if recent_ is not None else None
        mine = opened_at is not None and 0 <= now - opened_at <= OPEN_WINDOW_S
        if mine and allow and mode != ASK_ALL:
            return Decision("press", dialog, allow, reason="Mobster opened this link for the task.")
        if not mine and deny and not can_ask:
            return Decision("press", dialog, deny, reason="Mobster didn't open this link, so it stays closed.")
        return ask(f"Open the link in {who}?")
    if kind == "tracking" and deny:
        if mode == ASK_ALL and can_ask:
            return ask(f"Let {who} track you across other apps and websites?", deny)
        return Decision("press", dialog, deny, reason="apps work without tracking you, so Mobster asked it not "
                                                      "to track.")
    if kind == "software_update":
        later = dialog.button("Later", "Remind Me Later", "Not Now")
        if later and not (mode == ASK_ALL and can_ask):
            return Decision("press", dialog, later, reason="installing iOS restarts the phone and isn't part of the "
                                                           "task, so Mobster chose later.")
        return ask("Install the iOS update now?", later, "Installing restarts your iPhone.")
    if kind == "screen_time":
        return Decision("stop", dialog, reason="Screen Time's limit for this app is up. Mobster never ignores a limit "
                                               "for you: extend it on your iPhone, then run the task again.")
    if kind == "undo_typing":
        cancel = dialog.button("Cancel")
        if cancel:
            return Decision("press", dialog, cancel, reason="it keeps the text as it is.")
        return Decision("leave", dialog)
    if kind == "web_sign_in":
        return ask(f"Let {who} use the website's sign-in?", allow, "The app could see your sign-in for that site.")
    if kind in ("rating", "low_battery") and deny:
        return Decision("press", dialog, deny, reason="it changes nothing, so Mobster closed it.")
    if kind == "notifications":
        if (mode == DONT_ASK or not can_ask) and deny:
            return Decision("press", dialog, deny, reason="a task doesn't need notifications. Turn them on in "
                                                          "Settings › Notifications if you want them.")
        return ask(f"Let {who} send you notifications?", allow)
    if dialog.grant:
        return ask(f"Let {who} {GRANTS[kind]}?", allow)
    if in_app or not dialog.buttons:
        return Decision("leave", dialog)
    return ask(f"{who} asks: “{dialog.text[:120]}”", None)


def choices(decision):
    """The approval card's choices: each of iOS's buttons, in iOS's order, as {"id", "label"}."""
    return [{"id": f"b{index}", "label": button[:60]} for index, button in enumerate(decision.dialog.buttons[:5])]


def answered(decision, answer):
    """The button to press for an approval ``answer``; None presses nothing and the run stops."""
    if not isinstance(answer, str):
        return None
    if answer.startswith("choice:"):
        picked = answer.split(":", 1)[1]
        for item, button in zip(choices(decision), decision.dialog.buttons):
            if item["id"] == picked:
                return button
        return None
    if answer == "approved":
        return decision.button or None
    if answer == "denied":
        return decision.deny or None
    return None


def press(driver, decision, timeout=10, snapshot=None):
    """Press the decision's button by its name; raises when it can't, and nothing is pressed then."""
    if decision.perform is not None:
        decision.perform()
        return
    if callable(getattr(driver, "call", None)) and not decision.dialog.on_screen:
        driver.call("POST", "/alert/accept", {"name": decision.button}, timeout=timeout)
        return
    wanted = fold(decision.button).casefold()
    found = [e for e in getattr(snapshot, "elements", ()) or () if fold(e.label).casefold() == wanted]
    if len(found) != 1:
        raise ValueError(f"the alert's “{decision.button}” isn't on the screen once")
    driver.execute("TAP", found[0], snapshot, timeout=timeout)


def line(decision, pressed=None, by_user=False):
    """What happened, in one sentence for the thread and the model."""
    dialog = decision.dialog
    asked = f"iOS asked “{dialog.text[:140]}”"
    if decision.action == "press":
        return f"{asked}. Mobster chose “{decision.button}”: {decision.reason}"
    if decision.action == "ask":
        if pressed:
            return f"{asked}. You chose “{pressed}”."
        return f"{asked}. Nothing was chosen, so it is still on the screen."
    if decision.action == "stop":
        return decision.reason
    return asked


TOPICS = {"notifications": "notifications", "tracking": "tracking", "paste": "Allow Paste",
          "open_in_app": "Open in app", "save_password": "Save Password", "rating": "a rating prompt",
          "low_battery": "Low Battery", "location": "location", "location_always": "location", "photos": "Photos",
          "photos_add": "Photos", "camera": "the camera", "microphone": "the microphone", "contacts": "contacts",
          "calendar": "the calendar", "reminders": "reminders", "bluetooth": "Bluetooth",
          "local_network": "the local network", "motion": "motion and fitness", "speech": "speech recognition",
          "media": "the media library", "home": "Home data", "health": "health data", "face_id": "Face ID",
          "focus": "Focus status", "sign_in_with_apple": "Sign in with Apple", "purchase": "a purchase",
          "biometric": "Face ID or Touch ID", "software_update": "an iOS update", "screen_time": "a Screen Time limit",
          "web_sign_in": "a website sign-in", "undo_typing": "Undo Typing"}


def thread_text(kind, app, button, by="mobster"):
    """The thread's step line for an answered dialog."""
    topic = TOPICS.get(kind, "a prompt")
    where = f" for {app}" if app else ""
    who = "you" if by == "you" else "Mobster"
    return f"iOS asked about {topic}{where}: {who} chose “{button}”"
