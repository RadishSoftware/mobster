"""A check's ``dialogs:`` block: how it answers the screens that aren't the developer's.

iOS puts permission alerts, Allow Paste, Sign in with Apple, Face ID and the purchase sheet over an app. A check names
each answer once, and a run gives it with no model: Smart's turns (``Policy`` through the frontier) and a replay
(``Responder`` between steps) both read the dialog off WebDriverAgent or the screen, name it with ``system_dialogs``
and press the button the policy names.

Only iOS's own dialogs are answered, on a simulator and an iPhone alike. An alert needs SpringBoard in front, as
WebDriverAgent reports it, or the Apple process for its own kind (``system_dialogs.SHEET_FRONT``), and none of its
lines in the app's own tree. A sheet needs the one Apple process that shows its kind in front, and its own tree read:
StoreKit for a purchase, AuthKit for Sign in with Apple, CoreAuthUI for Face ID. Bundles are compared exactly, so
Safari's web content, a view service too, is never taken for iOS. Anything else is left to the step, which meets it
like any screen, under the check's approver. The run's policy remembers the last dialog it answered, so the same one
again stops the run instead of a second answer.

What a simulator may do that an iPhone may not lives here, in one table:

- On a simulator every key has a default (DEFAULTS). Money and people don't exist there.
- On an iPhone nothing is answered the check doesn't name, and some answers are never given: a purchase is never
  confirmed, a paste is allowed only for text Mobster put on the clipboard, a Face ID match can't be sent, and Open
  in another app goes only to a link Mobster opened. A dialog with no answer falls to ``system_dialogs.decide`` with
  nobody to ask, which stops the run or declines. A permission alert the check doesn't name is left to the step, as
  it was before this block existed.

The module imports no simulator code: ``biometric`` is handed in by the run that holds the simulator.
"""

import difflib
import re
import time
from dataclasses import dataclass, field

from ..wda import system_dialogs
from ..wda.system_dialogs import DialogStop
from .assertions import CheckError

CHOICES = {
    "permissions": ("allow", "deny", "ask"),
    "paste": ("allow", "deny", "ask"),
    "sign_in_with_apple": ("continue", "cancel"),
    "biometric": ("match", "nonmatch", "cancel"),
    "purchase": ("confirm", "cancel"),
}
DEFAULTS = {"permissions": "allow", "paste": "allow", "sign_in_with_apple": "continue", "biometric": "match",
            "purchase": "confirm"}
# The check key a dialog kind answers to. Grants, notifications and tracking are all "permissions".
KEY_OF = {**{kind: "permissions" for kind in system_dialogs.GRANTS}, "notifications": "permissions",
          "tracking": "permissions", "open_in_app": "permissions", "paste": "paste",
          "sign_in_with_apple": "sign_in_with_apple", "biometric": "biometric", "purchase": "purchase"}
CONFIRM_BUTTONS = ("Buy", "Subscribe")
# StoreKit's alert after a purchase: OK acknowledges what already happened, so a simulator presses it either way.
ALL_SET = re.compile(r"you're all set|your purchase was successful", re.I)
CANCEL_BUTTONS = ("Cancel", "Not Now", "Close")
CONTINUE_BUTTONS = ("Continue", "Continue with Passcode")
PER_SCREEN = 4        # dialogs a replay answers before looking at its next step again
BIOMETRIC_ACTIONS = ("match", "nonmatch")


def parse(value):
    """The normalized ``dialogs`` block of a check: {} when absent. Raises CheckError."""
    if value is None:
        return {}
    if not isinstance(value, dict) or not value:
        raise CheckError("dialogs must name an answer, such as {permissions: allow, purchase: cancel}.")
    out = {}
    for key, answer in value.items():
        if key not in CHOICES:
            close = difflib.get_close_matches(str(key), list(CHOICES), n=1)
            raise CheckError(f"dialogs.{str(key)[:40]} isn't a dialog Mobster answers; "
                             + (f"did you mean {close[0]}? " if close else "") + f"Use {', '.join(CHOICES)}.")
        if answer not in CHOICES[key]:
            raise CheckError(f"dialogs.{key} {str(answer)[:40]!r} isn't an answer; use {', '.join(CHOICES[key])}.")
        out[key] = answer
    return {key: out[key] for key in CHOICES if key in out}


def merge(*blocks):
    """Later blocks win key by key: fixtures in order, then the check's own."""
    out = {}
    for block in blocks:
        out.update(block or {})
    return {key: out[key] for key in CHOICES if key in out}


@dataclass(frozen=True)
class Policy:
    """The answers one run gives. ``simulator`` is False for anything that might be someone's phone."""
    named: dict = field(default_factory=dict)
    simulator: bool = False
    biometric: object = None      # biometric(action) sends a Face ID match or nonmatch to the run's simulator
    # The last dialog this run answered, as [(kind, text, button)]: shared by Smart's steps and the replay, so the same
    # dialog again stops the run instead of a second answer. A list, since the rest of the policy never changes.
    answered: list = field(default_factory=list, compare=False, repr=False)

    def answer(self, key):
        """The check's answer for ``key``, else the simulator's default; None on a phone that didn't name one."""
        return self.named.get(key) or (DEFAULTS.get(key) if self.simulator else None)

    def sheet(self, snapshot, front):
        """The sheet iOS presents over an app (Sign in with Apple, the purchase sheet, Face ID, or an Apple Account
        prompt it never answers), or None. Only while ``front`` is the Apple process that shows that kind and the
        screen read is that process's own tree: an app's or a web page's own "Continue with Apple", "Subscribe" or
        "Log in with Face ID" is theirs, and so is the app's screen behind a real sheet."""
        found = system_dialogs.from_screen(snapshot, kinds=system_dialogs.SHEETS | system_dialogs.NEVER)
        tree = getattr(snapshot, "bundle_id", "")
        return found if found is not None and system_dialogs.shows_sheet(found.kind, front) \
            and system_dialogs.shows_sheet(found.kind, tree) else None

    def look(self, driver, snapshot, front=None):
        """(dialog, in_app, front) for what is over ``snapshot``: WebDriverAgent's alert, else a sheet; dialog None
        when there is none. ``front`` is the bundle WebDriverAgent reports in front, read here when not given."""
        front = front or system_dialogs.front(driver)
        dialog = system_dialogs.read(driver, snapshot=snapshot) or self.sheet(snapshot, front)
        return dialog, (dialog is not None and system_dialogs.in_app(dialog, snapshot)), front

    def decide(self, dialog, *, recent_=None, in_app=False, front=None, now=None, clock=time.monotonic):
        """A press, stop or leave Decision for ``dialog``. Never asks: a check has nobody to ask. ``in_app``: the
        alert's text sits in the app's own tree. ``front``: the bundle WebDriverAgent reports in front, None when
        unknown. A press goes only to a dialog iOS shows; anything else is the step's."""
        made = self._decide(dialog, recent_, in_app, now, clock)
        if made.action == "press" and not self._shown_by_ios(dialog, in_app, front):
            return system_dialogs.Decision("leave", dialog)
        if made.action == "press" and self.answered and self.answered[-1][:2] == (dialog.kind, dialog.text):
            # The same dialog after Mobster answered it: the answer didn't take, or iOS asks again. Never twice.
            return system_dialogs.Decision(
                "stop", dialog, reason=f"iOS still shows “{dialog.text[:100]}” after Mobster pressed "
                                       f"“{self.answered[-1][2]}”, so Mobster stopped rather than press it again. Look "
                                       "at the screen before you run the check again.")
        return made

    def pressed(self, dialog, button):
        """Remember ``dialog`` as the last one this run answered, pressed or perhaps pressed: never again."""
        self.answered[:] = [(dialog.kind, dialog.text, button)]

    @staticmethod
    def _shown_by_ios(dialog, in_app, front):
        if dialog.on_screen:
            return system_dialogs.shows_sheet(dialog.kind, front)   # a sheet: read off the process that shows it
        # An alert: the app's own (a web page's confirm() among them) is never iOS's, whatever is in front.
        return not in_app and system_dialogs.shows_alert(dialog.kind, front)

    def _decide(self, dialog, recent_, in_app, now, clock):
        if dialog.kind == "unknown":
            return system_dialogs.Decision("leave", dialog)   # an app's own alert is the step's, under the usual rules
        key = KEY_OF.get(dialog.kind)
        answer = self.answer(key) if key else None
        made = self._by_policy(dialog, key, answer, recent_, now, clock) if answer and answer != "ask" else None
        if made is not None:
            return made
        if key == "permissions" and answer is None:
            # An iPhone permission alert the check doesn't name stays as it was: the step meets it like any screen.
            return system_dialogs.Decision("leave", dialog)
        return system_dialogs.decide(dialog, system_dialogs.ASK_IMPORTANT, can_ask=False, recent_=recent_,
                                     in_app=in_app, now=now, clock=clock)

    def _by_policy(self, dialog, key, answer, recent_, now, clock):
        press = system_dialogs.Decision
        why = "the check says so" if key in self.named else "a simulator's default for a check"
        if key == "permissions":
            if dialog.kind == "open_in_app" and answer == "allow" and not self.simulator \
                    and not _opened_by_mobster(recent_, now, clock):
                return press("leave", dialog)   # on a phone, another app opens only for a link Mobster opened
            button = dialog.allow_button() if answer == "allow" else dialog.deny_button()
            return press("press", dialog, button, reason=f"dialogs.permissions is {answer} ({why}).") if button \
                else press("stop", dialog, reason=_stop(dialog, f"it has no button to {answer}"))
        if key == "paste":
            if answer == "allow" and not self.simulator:
                return None    # on a phone Allow Paste stays for text Mobster put on the clipboard itself
            button = dialog.button("Allow Paste", "Allow") if answer == "allow" else dialog.deny_button()
            return press("press", dialog, button, reason=f"dialogs.paste is {answer} ({why}).") if button else None
        if key == "sign_in_with_apple":
            names = CONTINUE_BUTTONS if answer == "continue" else CANCEL_BUTTONS
            button = dialog.button(*names)
            return press("press", dialog, button, reason=f"dialogs.sign_in_with_apple is {answer} ({why}).") \
                if button else press("stop", dialog, reason=_stop(dialog, f"it has no {answer} button"))
        if key == "purchase":
            if self.simulator and ALL_SET.search(dialog.text) and dialog.button("OK"):
                return press("press", dialog, dialog.button("OK"), reason="the purchase already went through on the "
                                                                          "simulator; OK only closes its alert.")
            if answer == "confirm" and not self.simulator:
                return press("stop", dialog, reason="A purchase sheet is up on an iPhone. Mobster never confirms a "
                                                    "purchase there: answer it on your iPhone, or name "
                                                    "dialogs.purchase: cancel.")
            names = CONFIRM_BUTTONS if answer == "confirm" else CANCEL_BUTTONS
            button = dialog.button(*names)
            return press("press", dialog, button, reason=f"dialogs.purchase is {answer} ({why}).") \
                if button else press("stop", dialog, reason=_stop(dialog, f"it has no {answer} button"))
        if key == "biometric":
            if answer == "cancel":
                button = dialog.button(*CANCEL_BUTTONS)
                return press("press", dialog, button, reason=f"dialogs.biometric is cancel ({why}).") \
                    if button else press("stop", dialog, reason=_stop(dialog, "it has no cancel button"))
            if not self.simulator:
                return press("stop", dialog, reason="Face ID or Touch ID is asking on an iPhone. Mobster can't send "
                                                    "a match to a phone: look at the phone, or name "
                                                    "dialogs.biometric: cancel.")
            if self.biometric is None:
                return press("stop", dialog, reason=_stop(dialog, "Mobster can't send a Face ID event here"))
            send = self.biometric
            return press("press", dialog, answer, reason=f"dialogs.biometric is {answer} ({why}).",
                         perform=lambda: send(answer))
        return None


def _opened_by_mobster(recent_, now, clock):
    opened_at = recent_.opened_at if recent_ is not None else None
    now = clock() if now is None else now
    return opened_at is not None and 0 <= now - opened_at <= system_dialogs.OPEN_WINDOW_S


def for_run(check, manager, target, actions=None):
    """A run's policy: the check's ``dialogs`` answers; a simulator also gets the defaults and, with the run's action
    journal, can send a Face ID match, journaled before it is sent like every other device action."""
    from .smart import is_real_device
    simulator = not is_real_device(manager)
    send = None
    if simulator and actions is not None:
        from ..sim import biometrics

        def send(action):
            with actions.action("BIOMETRIC"):
                biometrics.deliver(manager, target.udid, action)
    return Policy(check.dialogs, simulator=simulator, biometric=send)


def _stop(dialog, why):
    return f"iOS is asking “{dialog.text[:100]}” and {why}, so Mobster stopped. Answer it by hand, or name an answer " \
           "for it in the check's dialogs."


class Responder:
    """Answers the dialogs in front of a replay with the policy: between steps, never as a step or a heal."""

    def __init__(self, policy, driver, *, emit=None, log=None, clock=time.monotonic):
        self.policy, self.driver, self.clock = policy, driver, clock
        self.emit = emit or (lambda event: None)
        self.log = log or (lambda line: None)
        self.answered = []

    def _read(self, snapshot, *, thorough):
        """(dialog, in_app, front), dialog None when there is none. The cheap look first; WebDriverAgent is asked only
        when it finds a reason."""
        front = None
        suspect = (system_dialogs.mentions_dialog(snapshot)
                   or getattr(snapshot, "bundle_id", "") in system_dialogs.APPLE_DIALOGS)
        if not suspect and thorough:
            front = system_dialogs.front(self.driver)
            suspect = front in system_dialogs.APPLE_DIALOGS
        if not suspect:
            return None, False, front
        return self.policy.look(self.driver, snapshot, front)

    def clear(self, snapshot, *, thorough=False, read=None):
        """(snapshot, number answered): the dialogs in front are answered, then the screen is read again.
        Raises DialogStop when the policy won't answer one."""
        count = 0
        while count < PER_SCREEN:
            dialog, in_app, front = self._read(snapshot, thorough=thorough)
            if dialog is None:
                break
            decision = self.policy.decide(dialog, recent_=system_dialogs.recent(self.driver), in_app=in_app,
                                          front=front)
            if decision is None or decision.action == "leave":
                break
            event = {"event": "system_dialog", "kind": dialog.kind, "app": dialog.app or None, "by": "check",
                     "action": decision.action, "reason": decision.reason}
            if decision.action != "press":
                self.emit({**event, "button": None})
                raise DialogStop(decision.reason, dialog)
            # The intent goes out before the press, as Smart's does: a press that never returns still has its line.
            self.emit({"event": "system_dialog_shown", "kind": dialog.kind, "app": dialog.app or None, "by": "check",
                       "button": decision.button})
            self.policy.pressed(dialog, decision.button)   # before the press: once sent, it may have landed
            try:
                system_dialogs.press(self.driver, decision, snapshot=snapshot)
            except Exception as error:
                # Pressing may have landed: say what is known, and never press again.
                self.emit({**event, "button": decision.button, "ok": False})
                raise DialogStop(f"iOS asked “{dialog.text[:100]}” and Mobster couldn't confirm its answer "
                                 f"“{decision.button}” ({type(error).__name__}). Look at the screen before you run "
                                 "the check again.", dialog) from None
            self.emit({**event, "button": decision.button, "ok": True})
            self.log(system_dialogs.line(decision))
            self.answered.append(dialog.kind)
            count += 1
            snapshot = read() if read is not None else snapshot
        return snapshot, count
