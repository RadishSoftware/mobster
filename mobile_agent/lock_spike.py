"""Lock-screen spike (15 Oct 2026): can Mobster tap a passcode on this iOS version?

A lever for one session on a spare phone with a throwaway passcode, with its owner at the Mac. It is not unlock
code: no command, no setting, no stored passcode, and nothing calls it. The answer decides whether an opt-in unlock
gets built at all.

    python -m mobile_agent.lock_spike STEP --wda-url URL --out DIR

``record``     Read only. Saves SpringBoard's keypad tree, with every word that is not a key scrubbed, and says what
               shape it has.
``tap-digit``  One tap on one digit key (``--digit``, default 5). A partial entry never submits. You say whether a
               dot filled in on the phone.
``enter``      A full entry. You type the passcode into a hidden prompt on this Mac; it is never an argument, never
               echoed, never written down. One attempt: a marker in DIR blocks a second until you delete it.
``survive``    Polls the runner for ``--seconds`` while you lock the phone, and says whether it kept answering.

Every step prints one JSON row, adds it to DIR/results.jsonl and writes "about to tap" there before a tap goes out.
Each step refuses unless the phone shows SpringBoard's own passcode keypad (``lockscreen.classify``), shows no
failed-attempt text, and answers. Screen text only ever makes a step refuse; it never makes one act.
"""

import argparse
import getpass
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from . import lockscreen

DIGITS = "0123456789"
# The one word on a key that is not its digit: the letters under it. Everything else on a lock screen (the date,
# a notification, a name) is the phone's owner's, so a saved tree keeps only keypad vocabulary.
KEY_LETTERS = frozenset({"ABC", "DEF", "GHI", "JKL", "MNO", "PQRS", "TUV", "WXYZ", "+"})
FAILED_ATTEMPT = re.compile(r"incorrect|try again|attempt|unavailable|disabled|erase", re.I)
PASSCODE = re.compile(r"\d{4}|\d{6}")
KEYS_PER_PASSCODE_TAP_S = 0.12
MARKER = "attempt-started"


class SpikeError(Exception):
    """A step refused or could not finish. The message never holds a passcode or a digit."""


def _text(node):
    return next((node[key] for key in ("label", "name", "value") if isinstance(node.get(key), str) and node[key]), "")


def _walk(tree):
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(child for child in node.get("children") or () if isinstance(child, dict))


def keypad_keys(tree):
    """{digit: (x, y)} of SpringBoard's ten keys; SpikeError unless each digit is there once, with a rect, enabled."""
    found = {}
    for node in _walk(tree):
        text = _text(node)
        rect = node.get("rect") if isinstance(node.get("rect"), dict) else {}
        if not str(node.get("type", "")).endswith("Button") or len(text) != 1 or text not in DIGITS:
            continue
        try:
            x, y, w, h = (float(rect[key]) for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            raise SpikeError("a digit key has no rect") from None
        if w <= 0 or h <= 0 or node.get("isEnabled") is False or text in found:
            raise SpikeError("the digit keys aren't ten enabled keys with one rect each")
        found[text] = (x + w / 2, y + h / 2)
    if set(found) != set(DIGITS):
        raise SpikeError("the keypad doesn't have all ten digit keys")
    return found


def scrub(tree):
    """``tree`` as a fixture: every word but the keypad's own vocabulary becomes ``<text>``."""
    def keep(text):
        if not text or text in DIGITS or text in KEY_LETTERS or text == lockscreen.SPRINGBOARD:
            return text
        return text if lockscreen._KEYPAD_TITLE.match(text) or lockscreen._LOCK_EXTRAS.match(text) else "<text>"

    def clean(node):
        text = keep(_text(node))
        result = {"type": node.get("type"), "label": text, "name": text, "rect": node.get("rect"),
                  "isEnabled": node.get("isEnabled"), "isVisible": node.get("isVisible")}
        result["children"] = [clean(child) for child in node.get("children") or () if isinstance(child, dict)]
        return result
    return clean(tree)


def shape(tree):
    """What a recorded tree looks like, for the per-version table."""
    nodes = list(_walk(tree))
    texts = [_text(node) for node in nodes]
    return {"nodes": len(nodes), "buttons": sum(str(n.get("type", "")).endswith("Button") for n in nodes),
            "digitKeys": sum(text in tuple(DIGITS) for text in texts),
            "hasTitle": any(lockscreen._KEYPAD_TITLE.match(text) for text in texts),
            "extras": sorted({text.title() for text in texts if lockscreen._LOCK_EXTRAS.match(text)})}


class Spike:
    """One session. ``prompt`` asks a yes or no question, ``secret`` reads a hidden line; both are the owner's."""

    def __init__(self, driver, out, *, prompt=input, secret=getpass.getpass, sleep=time.sleep, clock=time.monotonic,
                 interactive=None):
        self.driver, self.out = driver, Path(out)
        self.prompt, self.secret, self.sleep, self.clock = prompt, secret, sleep, clock
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.out.mkdir(parents=True, exist_ok=True)

    def ios(self):
        try:
            value = self.driver.http.request("GET", "/status", None, lockscreen.READ_TIMEOUT).get("value") or {}
            return str((value.get("os") or {}).get("version") or "unknown")
        except Exception:
            return "unknown"

    def look(self, *, acting):
        """(tree, front, state); with ``acting``, SpikeError unless SpringBoard's keypad shows no failed attempt."""
        locked = lockscreen.read_locked(self.driver)
        front = lockscreen.read_front(self.driver) if locked is not None else None
        tree = lockscreen.read_springboard_tree(self.driver) if locked is not None else None
        labels = lockscreen.tree_labels(tree) if tree is not None else None
        state, _ = lockscreen.classify(locked, front, labels)
        if acting:
            if locked is not True or tree is None:
                raise SpikeError(f"the phone isn't answering as locked (locked={locked}); lock it, wake it and retry")
            if state != "passcode_keypad" or front != lockscreen.SPRINGBOARD:
                raise SpikeError(f"the screen is {state}, front app {front}: not SpringBoard's passcode keypad")
            if any(FAILED_ATTEMPT.search(text) for _, text in labels):
                raise SpikeError("the phone shows a failed-attempt message; wait it out, and check the phone")
        return tree, front, state

    def note(self, event, **fields):
        with open(self.out / "results.jsonl", "a") as handle:
            handle.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}) + "\n")

    def tap(self, point, tree):
        try:
            size = tuple(float(tree["rect"][key]) for key in ("width", "height"))
        except (KeyError, TypeError, ValueError):
            raise SpikeError("the keypad tree has no screen size") from None
        screen = SimpleNamespace(width=size[0], height=size[1], captured_at=time.monotonic())
        self.driver.tap_point(point[0] / size[0], point[1] / size[1], screen)

    def ask(self, question):
        return str(self.prompt(question + " ")).strip().lower() in ("y", "yes")

    def record(self):
        tree, front, state = self.look(acting=False)
        row = {"step": "record", "ios": self.ios(), "state": state, "front": front}
        if tree is not None:
            row["shape"] = shape(tree)
            try:
                keypad_keys(tree)
                row["tenKeysWithRects"] = True
            except SpikeError:
                row["tenKeysWithRects"] = False
            name = f"{state}-ios{row['ios']}.json"
            (self.out / name).write_text(json.dumps(scrub(tree), indent=1) + "\n")
            row["saved"] = name
        return row

    def tap_digit(self, digit="5"):
        if digit not in tuple(DIGITS):
            raise SpikeError("--digit is one digit, 0 to 9")
        tree, _front, _state = self.look(acting=True)
        point = keypad_keys(tree)[digit]
        self.confirm()
        self.note("about_to_tap", step="tap-digit")
        self.tap(point, tree)
        self.sleep(0.6)
        after, _, _ = self.look(acting=False)
        row = {"step": "tap-digit", "ios": self.ios(),
               "treeChanged": json.dumps(tree, sort_keys=True) != json.dumps(after, sort_keys=True),
               "dotFilled": self.ask("Did one dot fill in on the phone? [y/n]")}
        return row

    def enter(self):
        if (self.out / MARKER).exists():
            raise SpikeError(f"one attempt per session, and one was started ({self.out / MARKER}). Check the phone, "
                             "then delete that file to run it again")
        if not self.interactive:
            raise SpikeError("the passcode is typed at a prompt on this Mac; run this from a terminal")
        tree, _front, _state = self.look(acting=True)
        points = keypad_keys(tree)
        code = self.secret("Throwaway passcode (hidden, never shown or saved): ")
        if not PASSCODE.fullmatch(code):
            raise SpikeError("a passcode is 4 or 6 digits")
        self.confirm()
        (self.out / MARKER).write_text("started\n")
        self.note("about_to_tap", step="enter")
        try:
            for digit in code:
                self.tap(points[digit], tree)
                self.sleep(KEYS_PER_PASSCODE_TAP_S)
        except Exception:
            self.note("tap_failed", step="enter")
            raise SpikeError("a tap ended without an answer, so the entry's outcome is unknown. Mobster doesn't "
                             "retry it: look at the phone") from None
        finally:
            del code
        self.sleep(1.5)
        locked = lockscreen.read_locked(self.driver)
        return {"step": "enter", "ios": self.ios(), "unlockedAfter": locked is False, "lockedAfter": locked}

    def survive(self, seconds=120, interval=5.0):
        print("Lock the phone now (side button). Polling.", file=sys.stderr)
        started, samples = self.clock(), []
        while self.clock() - started < seconds:
            answers = bool(self.ios() != "unknown")
            samples.append((lockscreen.read_locked(self.driver), answers))
            self.sleep(interval)
        locked = [answers for state, answers in samples if state is True]
        return {"step": "survive", "ios": self.ios(), "seconds": int(seconds), "samples": len(samples),
                "lockedSamples": len(locked), "answeredWhileLocked": bool(locked) and all(locked),
                "stillAnswersAfter": bool(samples) and samples[-1][1]}

    def confirm(self):
        if not self.ask("This is the spare phone and its passcode is a throwaway? [y/n]"):
            raise SpikeError("not confirmed; nothing was sent")


@contextmanager
def held_phone(url):
    """The driver for the phone at ``url``, under the device lease so no task runs on it meanwhile."""
    from .compose import close_all
    from .drivers import build_driver, resolve_wda_session
    from .journal import Lease, LeaseHeld
    from .transport import TransportError
    try:
        lease = Lease.device(url)
    except LeaseHeld:
        raise SpikeError("another Mobster process is using this phone; finish or stop its task") from None
    driver = None
    try:
        try:
            session = resolve_wda_session(url)
        except (TransportError, OSError) as error:
            raise SpikeError(f"nothing answers at {url} ({type(error).__name__})") from None
        driver = build_driver("wda", url=url, session=session)
        yield driver
    finally:
        close_all(driver)
        lease.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.lock_spike", description=__doc__.split("\n\n")[0])
    parser.add_argument("step", choices=("record", "tap-digit", "enter", "survive"))
    parser.add_argument("--wda-url", default=os.environ.get("MOBSTER_WDA_URL") or "http://127.0.0.1:8100")
    parser.add_argument("--out", required=True, help="a folder outside the repository for the rows and trees")
    parser.add_argument("--digit", default="5")
    parser.add_argument("--seconds", type=int, default=120)
    args = parser.parse_args(argv)
    try:
        with held_phone(args.wda_url.rstrip("/")) as driver:
            spike = Spike(driver, args.out)
            row = {"record": spike.record, "enter": spike.enter,
                   "tap-digit": lambda: spike.tap_digit(args.digit),
                   "survive": lambda: spike.survive(args.seconds)}[args.step]()
            spike.note("result", **row)
    except SpikeError as error:
        print(f"lock_spike: {error}", file=sys.stderr)
        return 1
    print(json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
