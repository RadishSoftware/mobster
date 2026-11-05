"""The one table of states that stop Mobster, each with its words and its fix.

A row is one state and one cause: what happened in a sentence, what to do in a sentence, and the fix. Every surface that
names a state reads its words here: the Mac app, ``mobster mcp``'s ``status`` and ``mobster serve`` (through
``engine_health``), and ``mobster doctor`` and ``mobster sim doctor`` (through ``doctor``). Nothing else holds a copy.
``tests/test_states.py`` fails when a surface says a state in other words, or claims a row it can't produce.

The fix is one of six kinds, so a state never shows a dead end:

- ``Auto``: Mobster does it, because it touches only Mobster's own state and nothing another program is running.
- ``Ask``: Mobster does it after it names the process or command and the person says yes. Never without a terminal.
- ``Guided``: a guided flow (``mobster login``, ``mobster setup``) that asks its own questions.
- ``Yours``: the person does it, on the iPhone or with the cable; ``picture`` names the picture of the iPhone's screen
  that shows the taps. Mobster notices and goes on.
- ``Run``: a command Mobster prints and never runs for the person (a download, opening the app).
- ``Wait``: Mobster is already on it, or it clears by itself.

``{phone}`` is the phone's name, or "your iPhone"; ``{Phone}`` is the same at the start of a sentence. A model's own
sentence (``engine_health.classify_model``) replaces a row's ``message``, never its step.

Nothing here acts on the phone. It only says what to do.
"""

from dataclasses import dataclass
from typing import NoReturn, assert_never

READY = "ready"
WAITING_FOR_PHONE = "waiting_for_phone"
HELPER_DOWN = "helper_down"
NO_PHONE = "no_phone"
MODEL_UNAVAILABLE = "model_unavailable"
LIVE_OFF = "live_off"
STOPPING = "stopping"
# Named by whoever asks, when the process doesn't answer (engine_health.from_silence).
SLOW = "slow"
FROZEN = "frozen"
DOWN = "down"
RESTARTING = "restarting"

STATES = (READY, WAITING_FOR_PHONE, HELPER_DOWN, NO_PHONE, MODEL_UNAVAILABLE, LIVE_OFF, STOPPING,
          SLOW, FROZEN, DOWN, RESTARTING)


# -- the fix ----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Auto:
    action: str


@dataclass(frozen=True)
class Ask:
    action: str


@dataclass(frozen=True)
class Guided:
    command: str


@dataclass(frozen=True)
class Yours:
    picture: str | None = None


@dataclass(frozen=True)
class Run:
    command: str


@dataclass(frozen=True)
class Wait:
    why: str


Fix = Auto | Ask | Guided | Yours | Run | Wait


def unreachable(fix) -> NoReturn:
    assert_never(fix)


def fix_public(fix):
    """The fix as a client reads it: {"kind", ...}. ``action`` names what ``Auto`` and ``Ask`` run
    (doctor_fix.ACTIONS)."""
    match fix:
        case Auto(action) | Ask(action):
            return {"kind": type(fix).__name__.lower(), "action": action}
        case Guided(command) | Run(command):
            return {"kind": type(fix).__name__.lower(), "command": command}
        case Yours(picture):
            return {"kind": "yours", **({"picture": picture} if picture else {})}
        case Wait(why):
            return {"kind": "wait", "why": why}
    unreachable(fix)


def how(fix):
    """One sentence on what Mobster does about the row, for the docs and the reports."""
    match fix:
        case Auto():
            return "`mobster doctor --fix` does this for you."
        case Ask():
            return "`mobster doctor --fix` names what it would stop and asks you first."
        case Guided(command):
            return f"`{command}` walks you through it."
        case Yours():
            return "You do this one. Mobster notices and goes on."
        case Run(command):
            return f"Run `{command}`. Mobster doesn't run it for you."
        case Wait(why):
            return why
    unreachable(fix)


# -- the rows ---------------------------------------------------------------------------------------------------------

# Where a row shows today. A surface listed here is tested to produce it (tests/test_states.py).
ENGINE = ("app", "mcp")   # engine_health: the Mac app, `mobster mcp` status, `mobster serve`
DOCTOR = ("cli",)         # `mobster doctor` and `mobster sim doctor`


@dataclass(frozen=True)
class Row:
    state: str
    cause: str
    message: str
    step: str
    need: str | None
    fix: Fix
    surfaces: tuple[str, ...] = ENGINE


# The next step of the three a task waits on is the sentence the task's caller (MCP, Mobster Remote) passes on.
ROWS = (
    Row(WAITING_FOR_PHONE, "locked", "{Phone} is locked.",
        "Unlock {phone}: the task goes on as soon as it's unlocked.", "unlock", Yours(), ENGINE + DOCTOR),
    Row(WAITING_FOR_PHONE, "unplugged", "{Phone} is unplugged.",
        "Plug {phone} back into the Mac: the task goes on when it's back.", "plug_in", Yours(), ENGINE + DOCTOR),
    Row(WAITING_FOR_PHONE, "wifi_lost", "{Phone} is out of reach over Wi-Fi.",
        "Bring {phone} back near the Mac, or plug it in: the task goes on when it's back.", "bring_back", Yours(),
        ENGINE + DOCTOR),
    Row(WAITING_FOR_PHONE, "helper_restarting", "Mobster's helper on {phone} is restarting.",
        "Keep {phone} unlocked. This takes a few seconds.", None, Wait("Mobster starts the helper again by itself.")),
    Row(WAITING_FOR_PHONE, "developer_mode_off", "Developer Mode is off on {phone}.",
        "Turn it on in Settings › Privacy & Security › Developer Mode, then restart {phone}.", "developer_mode",
        Yours("developer-mode"), ENGINE + DOCTOR),
    Row(WAITING_FOR_PHONE, "trust_needed", "{Phone} doesn't trust Mobster's helper yet.",
        "On {phone}, open Settings › General › VPN & Device Management and trust your developer app.", "trust",
        Yours("trust-developer-app"), ENGINE + DOCTOR),
    Row(WAITING_FOR_PHONE, "ui_automation_off", "UI Automation is off on {phone}.",
        "On {phone}, turn on Settings › Developer › Enable UI Automation, and keep it unlocked while the helper "
        "starts.", "ui_automation", Yours("ui-automation"), ENGINE + DOCTOR),
    Row(WAITING_FOR_PHONE, "not_responding", "{Phone} isn't responding.",
        "Unlock {phone} and close Control Center or Siri if they're open.", "clear_screen", Yours()),
    Row(WAITING_FOR_PHONE, "computer_untrusted", "{Phone} doesn't trust this Mac yet.",
        "On {phone}, tap Trust, then enter your passcode. Mobster never enters it.", "trust_computer",
        Yours("trust-this-computer"), DOCTOR),
    Row(HELPER_DOWN, "exposed",
        "Mobster stopped its helper on {phone} because it answered on the phone's network address.",
        "Build the helper again in Devices, then start it.", None, Guided("mobster setup")),
    Row(HELPER_DOWN, "signature_expired", "Mobster's helper on {phone} has expired.",
        "Refresh it in Devices.", None, Auto("rebuild_helper"), ENGINE + DOCTOR),
    Row(HELPER_DOWN, "not_built", "Mobster's helper isn't on {phone} yet.",
        "Set it up in Devices.", None, Guided("mobster setup"), ENGINE + DOCTOR),
    Row(HELPER_DOWN, "failing", "Mobster's helper on {phone} keeps stopping.",
        "Unlock {phone} and check Developer Mode, then start the helper in Devices.", None, Auto("start_helper")),
    Row(HELPER_DOWN, "stopped", "Mobster's helper on {phone} isn't running.",
        "Start it in Devices.", None, Auto("start_helper"), ENGINE + DOCTOR),
    Row(HELPER_DOWN, "not_answering", "Mobster's helper on {phone} isn't answering.",
        "Check that {phone} is on and reachable, then try again.", None, Yours()),
    Row(HELPER_DOWN, "port_taken", "Another program is using the port Mobster's helper needs on this Mac.",
        "Quit that program, or let Mobster name it and stop it with your OK.", None, Ask("free_port"), DOCTOR),
    Row(NO_PHONE, "none_chosen", "No iPhone is set up.", "Add one in Devices.", None, Guided("mobster setup"),
        ENGINE + DOCTOR),
    Row(NO_PHONE, "none_attached", "No iPhone is attached.", "Start Mobster with a device, or add one in Devices.",
        None, Guided("mobster setup")),
    Row(NO_PHONE, "no_runtime", "No iOS Simulator runtime is installed.",
        "Download one with xcodebuild -downloadPlatform iOS.", None, Run("xcodebuild -downloadPlatform iOS"), DOCTOR),
    Row(MODEL_UNAVAILABLE, "no_key", "No AI account is connected.", "Connect your AI account in Settings › Models.",
        None, Guided("mobster login"), ENGINE + DOCTOR),
    Row(MODEL_UNAVAILABLE, "no_credit", "Your AI account is out of credit.",
        "Add credit at your AI account, then try again.", None, Yours()),
    Row(MODEL_UNAVAILABLE, "key_rejected", "Your AI account didn't accept your key.",
        "Check your key in Settings › Models.", None, Guided("mobster login")),
    Row(MODEL_UNAVAILABLE, "network_gone", "This Mac has no network.", "Join a network, then try again.", None,
        Yours(), DOCTOR),
    Row(LIVE_OFF, "disabled", "Live runs are off.", "Turn them on in Settings to act on the phone.", None, Yours()),
    Row(STOPPING, "closing", "Mobster is closing.", "Open it again when you need it.", None,
        Wait("Mobster is closing. Open it again when you need it.")),
    Row(SLOW, "slow_answer", "Mobster is slow to answer.", "Wait a few seconds. Nothing is lost.", None,
        Wait("Mobster answers again by itself.")),
    Row(FROZEN, "no_answer", "Mobster stopped responding.",
        "Restart Mobster. A task in flight is kept in History and is not resent.", None, Ask("restart_service"),
        ENGINE + DOCTOR),
    Row(DOWN, "not_running", "Mobster isn't running.", "Open the Mobster app.", None, Run("open -a Mobster"),
        ENGINE + DOCTOR),
    Row(RESTARTING, "restarted", "Mobster restarted.", "Wait a moment while it reconnects.", None,
        Wait("Mobster reconnects by itself.")),
)
BY_CAUSE = {row.cause: row for row in ROWS}


def row(cause):
    """The row for a cause (KeyError for one the table doesn't have). Every cause belongs to one state."""
    return BY_CAUSE[cause]


def words(state, cause, phone=None, message=None):
    """(message, next_step, need) for a state and cause, with the phone's name in. ``message`` replaces the table's
    (a model's own sentence). KeyError for a pair the table doesn't have."""
    found = BY_CAUSE[cause]
    if found.state != state:
        raise KeyError((state, cause))
    name = (phone or "").strip() or "your iPhone"
    fields = {"phone": name, "Phone": name[:1].upper() + name[1:]}
    return (message or found.message.format(**fields)), found.step.format(**fields), found.need
