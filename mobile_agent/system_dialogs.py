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
- Saving a password, signing in to an Apple Account, Trust This Computer and anything with a password field are
  never pressed and never answered for the user.
- Allow Paste is pressed only within PASTE_WINDOW_S of Mobster putting text on the clipboard itself
  (``note_paste``). The clipboard's contents aren't read back: the window stands for "the text is Mobster's". Any
  other paste prompt is refused ("Don't Allow Paste").
- Open in "App"? is pressed only right after Mobster opened that link itself (``note_open``).

The mode is the user's permission mode (``permission_mode``): ask_all, ask_important (default) or dont_ask, read
the way ``permissions.policy`` reads it (MOBSTER_PERMISSION_MODE; MOBSTER_ASK_BEFORE_ACTING=0 as
dont_ask), and through that package when it is present. Runs an outside agent started (MCP, Remote) never go below
ask_important.
"""

from dataclasses import dataclass, field
import os
import re
import threading
import time
import unicodedata

ASK_ALL, ASK_IMPORTANT, DONT_ASK = "ask_all", "ask_important", "dont_ask"
MODES = (ASK_ALL, ASK_IMPORTANT, DONT_ASK)
OUTSIDE_ORIGINS = frozenset({"mcp", "remote"})
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
    # (kind, regex on the folded text). Order matters: the more precise first.
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
    ("save_password", re.compile(r"save (?:this )?password|save to passwords|icloud keychain|update password\?",
                                 re.I)),
    ("apple_account", re.compile(r"apple account|apple id|itunes store|sign in to icloud|enter (?:the|your) password",
                                 re.I)),
    ("trust_computer", re.compile(r"trust this computer", re.I)),
    ("rating", re.compile(r"^enjoying .+\?|rate (?:this app|us)|tap a star to rate", re.I)),
    ("low_battery", re.compile(r"^low battery\b", re.I)),
)
ALLOW = re.compile(r"^(?:allow(?: once| while using(?: the)? app| full access| paste)?|ok|open|continue|"
                   r"change to always allow|save password)$", re.I)
DENY = re.compile(r"^(?:don['’]?t allow(?: paste)?|ask app not to track|cancel|not now|keep only while using|"
                  r"close|later|never for this website)$", re.I)


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
                            "Allow", "Allow Full Access", "OK", "Continue", "Allow Paste", "Open")
                or next((b for b in self.buttons if ALLOW.match(fold(b))), None))

    def deny_button(self):
        return (self.button("Don't Allow", "Ask App Not to Track", "Don't Allow Paste", "Not Now", "Cancel",
                            "Keep Only While Using", "Close", "Later")
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
    return Dialog(text=plain[:400], buttons=buttons, kind=kind, app=app_named(text))


def read(driver, timeout=READ_TIMEOUT):
    """The dialog in front as a Dialog, or None. Never raises: a runner that doesn't answer reads as no dialog."""
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
    return classify(text, buttons if isinstance(buttons, list) else [])


# -- what Mobster did just before (paste, open a link) -----------------------------------------------------------

@dataclass
class Recent:
    """What Mobster itself just did that makes a following dialog its own: a clipboard it set, a link it opened."""
    pasted_at: float = 0.0
    opened_at: float = 0.0
    opened_url: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


_recent = {}
_recent_lock = threading.Lock()


def recent(driver):
    """The Recent record for ``driver`` (kept per driver object, for the life of the process)."""
    key = id(driver)
    with _recent_lock:
        found = _recent.get(key)
        if found is None:
            found = _recent[key] = Recent()
        return found


def note_paste(driver, clock=time.monotonic):
    recent(driver).pasted_at = clock()


def note_open(driver, url, clock=time.monotonic):
    record = recent(driver)
    record.opened_at, record.opened_url = clock(), str(url or "")[:2000]


# -- the user's permission mode (PR #143's hook) ----------------------------------------------------------------

def permission_mode(origin=None, env=None):
    """ask_all | ask_important | dont_ask: the user's permission mode for a run of ``origin``.

    Through PR #143's ``permissions.policy`` when it is in this build; else the same settings it reads:
    MOBSTER_PERMISSION_MODE, then MOBSTER_ASK_BEFORE_ACTING=0 as dont_ask, else ask_important. A run an outside
    agent started (MCP, Remote) never goes below ask_important."""
    if env is None:
        try:
            from .permissions import policy  # noqa: F401  (PR #143; absent on builds without it)
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


# -- the decision ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    action: str                  # "press" | "ask" | "stop" | "leave"
    dialog: Dialog
    button: str = ""             # press: the button; ask: the one Mobster suggests (the narrowest grant)
    deny: str = ""               # ask: the button a decline presses ("" when there is none to press)
    reason: str = ""             # one plain sentence for the thread and the model
    title: str = ""              # ask: the approval card's question

    def public(self):
        return {"action": self.action, "kind": self.dialog.kind, "app": self.dialog.app or None,
                "button": self.button or None, "reason": self.reason}


def _who(dialog):
    return f"“{dialog.app}”" if dialog.app else "An app"


def decide(dialog, mode=ASK_IMPORTANT, *, can_ask=True, recent_=None, in_app=False, now=None, clock=time.monotonic):
    """What to do about ``dialog`` under ``mode``. ``can_ask``: a person can answer an approval now. ``recent_``:
    the driver's Recent (what Mobster just did). ``in_app``: the dialog is in the app's own tree (its alert, not
    iOS's), which the agent answers like any screen unless it is one of iOS's known prompts."""
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
        mine = recent_ is not None and now - recent_.pasted_at <= PASTE_WINDOW_S
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
        mine = recent_ is not None and now - recent_.opened_at <= OPEN_WINDOW_S
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
    """The button to press for an approval ``answer`` ("approved", "denied", "choice:b1"...); None: press nothing
    (the run stops: timeout, Stop, or a decline with no Don't Allow to press)."""
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


def press(driver, decision, timeout=10):
    """Press the decision's button (WebDriverAgent finds it by its real name). Raises when the runner refuses."""
    driver.call("POST", "/alert/accept", {"name": decision.button}, timeout=timeout)


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
          "focus": "Focus status"}


def thread_text(kind, app, button, by="mobster"):
    """The thread's step line for an answered dialog: "iOS asked about notifications for Duolingo: Mobster chose
    “Don’t Allow”" / "...: you chose “Allow Once”"."""
    topic = TOPICS.get(kind, "a prompt")
    where = f" for {app}" if app else ""
    who = "you" if by == "you" else "Mobster"
    return f"iOS asked about {topic}{where}: {who} chose “{button}”"
