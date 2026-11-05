"""Maestro flows as checks; the YAML is read with safe_load only and never reaches a model as free text."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..verify.checks import LIMITS, CheckError, check_from_dict, check_to_dict

FILE_LIMIT = 1_000_000
MAX_DEPTH = 8
TEXT_LIMIT = 120          # a quoted value in a step
INPUT_LIMIT = 300         # typed text
MAX_FLOWS = 500
REGEX_CHARS = set("*+?[](){}|^$\\")
ENV_REF = re.compile(r"(?<!\\)\$\{([^}]*)\}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PERCENT_POINT = re.compile(r"^\s*(\d{1,3}(?:\.\d+)?)%\s*,\s*(\d{1,3}(?:\.\d+)?)%\s*$")
ABS_POINT = re.compile(r"^\s*(\d{1,5}(?:\.\d+)?)\s*,\s*(\d{1,5}(?:\.\d+)?)\s*$")
HEADER_KEYS = ("appId", "name", "tags", "env", "onFlowStart", "onFlowComplete", "properties", "jsEngine")
DIRECTIONS = ("UP", "DOWN", "LEFT", "RIGHT")
# Options that only tune timing or retries: Mobster waits for the screen itself and verifies each action.
TIMING = ("retryTapIfNoChange", "waitToSettleTimeoutMs", "duration", "speed", "timeout", "label",
          "visibilityPercentage", "centerElement", "delay", "waitUntilVisible")
RELATIVE = {"below": "below", "above": "above", "leftOf": "to the left of", "rightOf": "to the right of",
            "childOf": "inside", "containsChild": "that contains", "containsDescendants": "that contains"}
KEYS = {"home": "Press the Home button", "enter": "Press the Return key on the keyboard",
        "backspace": "Press the delete key on the keyboard"}

# Commands Mobster doesn't run: (why, what to do instead). An unsupported assertion refuses the whole flow.
UNSUPPORTED = {
    "runScript": ("runs JavaScript, which Mobster doesn't run",
                  "move the logic into the app or the launch environment, "
                  "or write that part as a Mobster check by hand"),
    "evalScript": ("runs JavaScript, which Mobster doesn't run", "set the value in the flow's env instead"),
    "assertTrue": ("judges a JavaScript condition, which Mobster doesn't run",
                   "write the condition as assertVisible or assertNotVisible on what the screen shows"),
    "assertWithAI": ("asks a model to judge the screen, and Mobster's verdicts never come from a model",
                     "write the assertion as assertVisible or assertNotVisible"),
    "assertNoDefectsWithAI": ("asks a model to judge the screen, and Mobster's verdicts never come from a model",
                              "write the assertion as assertVisible or assertNotVisible"),
    "assertScreenshot": ("compares pixels, and Mobster checks the accessibility tree",
                         "assert the text or element the screenshot proves with assertVisible"),
    "repeat": ("loops, and a check is a fixed list of steps", "write the loop out as steps, or use scrollUntilVisible"),
    "retry": ("retries commands, and a check is a fixed list of steps",
              "remove the retry; `mobster test --retries N` retries a whole check"),
    "takeScreenshot": ("Mobster records a frame for every step of every run", "read the frames in the report"),
    "startRecording": ("Mobster records video itself", "run `mobster test --video`"),
    "stopRecording": ("Mobster records video itself", "run `mobster test --video`"),
    "setLocation": ("changing the simulated location isn't a check step",
                    "set it before the run with `xcrun simctl location`"),
    "setPermissions": ("granting permissions isn't a check step",
                       "grant them before the run with `xcrun simctl privacy`"),
    "travel": ("moving the simulated location isn't a check step",
               "set it before the run with `xcrun simctl location`"),
    "setAirplaneMode": ("changing network state isn't a check step", "set it on the device before the run"),
    "toggleAirplaneMode": ("changing network state isn't a check step", "set it on the device before the run"),
    "setOrientation": ("rotating the device isn't a check step", "rotate the device before the run"),
    "setClipboard": ("the clipboard isn't a check step", "run `mobster phone clipboard set`"),
    "copyTextFrom": ("copies screen text into a variable, and a check has no variables",
                     "assert the text with assertVisible"),
    "pasteText": ("pastes a variable, and a check has no variables", "type the text with inputText"),
    "addMedia": ("adding photos isn't a check step", "add them before the run with `xcrun simctl addmedia`"),
    "clearKeychain": ("clearing the keychain isn't a check step", "run `mobster sim erase` before the run"),
    "clearState": ("clearing app data mid-flow isn't a check step",
                   "put `clearState: true` on the first launchApp, or set `reset: data` in the check"),
    "stopApp": ("stopping the app mid-flow isn't a check step", "use launchApp to start it again"),
    "killApp": ("killing the app mid-flow isn't a check step", "use launchApp to start it again"),
    "openBrowser": ("Android only", "use openLink"),
    "inputRandomText": ("types random text, so the run isn't repeatable", "use inputText with fixed text"),
    "inputRandomNumber": ("types random text, so the run isn't repeatable", "use inputText with fixed text"),
    "inputRandomEmail": ("types random text, so the run isn't repeatable", "use inputText with fixed text"),
    "inputRandomPersonName": ("types random text, so the run isn't repeatable", "use inputText with fixed text"),
    "defineVariables": ("sets variables, and a check has none", "put the values in the flow's env"),
    "pauseTest": ("pauses for a person", "remove it"),
    "setDarkMode": ("changing appearance isn't a check step", "set it before the run with `xcrun simctl ui`"),
}
ASSERTIONS = ("assertTrue", "assertWithAI", "assertNoDefectsWithAI", "assertScreenshot")


class MaestroError(Exception):
    """The file can't be read as a flow at all."""


@dataclass
class Problem:
    where: str                    # "command 4 (runScript)", with the file for a subflow
    command: str
    why: str
    instead: str = ""
    assertion: bool = False       # an assertion is never dropped


@dataclass
class FlowResult:
    source: str                   # path shown to the user, relative to the import root
    outcome: str = "imported"     # imported | refused | skipped
    checks: list = field(default_factory=list)      # of (file stem, check dict)
    problems: list = field(default_factory=list)    # of Problem: why a flow was refused
    placeholders: list = field(default_factory=list)  # of Problem: became a marked step for the first run
    same: list = field(default_factory=list)        # of file stems: already on disk exactly as imported
    converted: int = 0            # commands that became steps or expectations as written
    dropped: list = field(default_factory=list)     # of Problem: dropped on request
    notes: list = field(default_factory=list)       # of str: kept, with a caveat
    reason: str = ""              # for skipped, or a flow that can't be read

    def to_dict(self):
        return {"source": self.source, "outcome": self.outcome, "reason": self.reason or None,
                "checks": [{"file": f"{stem}.yaml", "steps": len(data.get("steps") or ()),
                            "expect": len(data["expect"])} for stem, data in self.checks],
                "refused": [vars(p) for p in self.problems] if self.outcome == "refused" else [],
                "converted": self.converted, "needsFirstRun": [vars(p) for p in self.placeholders],
                "dropped": [vars(p) for p in self.dropped], "notes": list(self.notes)}


class _Ctx:
    def __init__(self, root, skip_unsupported):
        self.root, self.skip = Path(root), skip_unsupported
        self.problems, self.dropped, self.notes, self.placeholders = [], [], [], []
        self.converted = 0
        self.ops = []             # ("act", text) | ("assert", dict, name) | ("launch", {...}) | ("url", url)
        self.launch = {}
        self.loaded = None

    def problem(self, where, command, why, instead="", assertion=False):
        assertion = assertion or command.startswith("assert") or command == "extendedWaitUntil"
        item = Problem(where, command, why, instead, assertion)
        if self.skip and not assertion:
            self.dropped.append(item)
        else:
            self.problems.append(item)

    def unsupported(self, where, command, why, instead, assertion=False):
        """A command Mobster can't run: refused when it is an assertion, dropped on request, else a marked step."""
        if assertion or self.skip:
            return self.problem(where, command, why, instead, assertion)
        self.placeholders.append(Problem(where, command, why, instead))
        name = command if ENV_NAME.fullmatch(command) and len(command) <= 40 else "unknown"
        self.ops.append(("act", f"TODO (Maestro {name}, not imported): {why}. Look at the screen and do what this "
                                "point in the flow needs, or nothing if it needs nothing"))

    def started(self):
        return any(op[0] in ("act", "assert") for op in self.ops)


def read_flow(path):
    """(config dict, commands list) from a Maestro flow file. Raises MaestroError on a file that isn't one."""
    import yaml
    path = Path(path)
    try:
        if path.stat().st_size > FILE_LIMIT:
            raise MaestroError("larger than 1 MB")
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise MaestroError(f"can't be read ({error.strerror or type(error).__name__})") from None
    except UnicodeDecodeError:
        raise MaestroError("isn't UTF-8 text") from None
    try:
        docs = [doc for doc in yaml.safe_load_all(text)]
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        raise MaestroError(f"line {mark.line + 1}: {getattr(error, 'problem', None) or 'not valid YAML'}"
                           if mark is not None else "isn't valid YAML") from None
    except (RecursionError, ValueError):
        raise MaestroError("is nested too deeply") from None
    docs = [doc for doc in docs if doc is not None]
    if len(docs) == 1 and isinstance(docs[0], list):
        return {}, docs[0]
    if len(docs) == 1 and isinstance(docs[0], dict):
        return docs[0], []
    if len(docs) == 2 and isinstance(docs[0], dict) and isinstance(docs[1], list):
        return docs[0], docs[1]
    if not docs:
        raise MaestroError("is empty")
    raise MaestroError("isn't a Maestro flow: expected a config, then `---`, then a list of commands")


def discover(paths):
    """The flow files in ``paths`` as [(file, root)], sorted and de-duplicated."""
    found = {}
    for path in paths:
        path = Path(path).expanduser()
        if path.is_dir():
            root = path.resolve()
            for file in sorted(path.iterdir()):
                if file.suffix.lower() in (".yaml", ".yml") and file.is_file() and not file.is_symlink() \
                        and not file.name.startswith("."):
                    found.setdefault(file.resolve(), root)
        elif path.is_file():
            found.setdefault(path.resolve(), path.resolve().parent)
        else:
            raise MaestroError(f"{path} doesn't exist")
    return sorted(found.items())[:MAX_FLOWS]


def _substitute(value, env, ctx, where, command):
    """``${NAME}`` from the flow's env. A name that isn't set, and any JavaScript expression, is a problem."""
    if not isinstance(value, str):
        return value

    def sub(found):
        name = found.group(1).strip()
        if not ENV_NAME.fullmatch(name):
            ctx.problem(where, command, f"uses the JavaScript expression ${{{name[:40]}}}, which Mobster doesn't run",
                        "write the value out, or set it in the flow's env")
            failed.append(name)
            return ""
        if name not in env:
            ctx.problem(where, command, f"uses ${{{name}}}, which the flow's env doesn't set",
                        f"add {name} to the flow's env, or write the value out")
            failed.append(name)
            return ""
        return str(env[name])
    failed = []
    out = ENV_REF.sub(sub, value).replace("\\${", "${")
    return None if failed else out


def _quote(text, limit=TEXT_LIMIT):
    """A value inside a step: JSON-quoted on one line, so it reads as data."""
    text = str(text)
    if len(text) > limit:
        raise ValueError(f"is longer than {limit} characters")
    return json.dumps(text, ensure_ascii=False)


def _scalar(value):
    if isinstance(value, bool) or value is None:
        return None
    return str(value) if isinstance(value, (str, int, float)) else None


def _regex(text):
    return any(char in REGEX_CHARS for char in text)


def _literal(text):
    """``text`` with every regex character escaped and nothing else touched."""
    return re.sub(r"([\\.^$*+?()\[\]{}|/])", r"\\\1", text)


def _matcher(text, *, fold=True):
    # Ids stay exact (fold=False): that can fail a flow Maestro would pass, never pass one Maestro would fail.
    if _regex(text):
        return f"/^(?:{text})$/i"
    if fold and text.lower() != text.upper():
        return f"/^{_literal(text)}$/i"
    return text


@dataclass
class Sel:
    text: str = None
    id: str = None
    enabled: bool = None
    selected: bool = None
    index: int = None
    point: str = None
    optional: bool = False
    relative: list = field(default_factory=list)      # of (word, Sel)

    def describe(self):
        """The selector in plain English, every value quoted."""
        if self.point is not None:
            return f"the screen point {self.point}"
        parts = [] if self.text is not None or self.id is not None else ["the element"]
        if self.text is not None:
            parts.append(f"the element whose text matches the pattern {_quote(self.text)}" if _regex(self.text)
                         else _quote(self.text))
        if self.id is not None:
            kind = "an accessibility id that matches the pattern" if _regex(self.id) else "accessibility id"
            parts.append(f"{'with' if parts else 'the element with'} {kind} {_quote(self.id)}")
        out = " ".join(parts)
        for state, name in ((self.enabled, "enabled"), (self.selected, "selected")):
            if state is not None:
                out += f" that is {'' if state else 'not '}{name}"
        for word, other in self.relative:
            out += f" {RELATIVE[word]} {other.describe()}"
        if self.index is not None:
            out += f" (match number {self.index + 1}, counting from the top left)"
        return out


def _selector(value, where, command, ctx, env, *, tap=False, nested=False):
    """A Sel from a Maestro selector (a string, or an object), or None after naming the problem."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str):
        value = {"text": value}
    if not isinstance(value, dict):
        ctx.problem(where, command, "has a selector Mobster can't read", "give it as text, id or point")
        return None
    sel, bad = Sel(), False
    for key, item in value.items():
        if key in TIMING:
            continue
        if key in ("text", "id"):
            if _scalar(item) is None or not _scalar(item).strip():
                ctx.problem(where, command, f"has an empty or non-text {key}", "give the text or id to match")
                bad = True
            else:
                item = _substitute(_scalar(item), env, ctx, where, command)
                if item is None:
                    bad = True
                else:
                    setattr(sel, key, item)
        elif key in ("enabled", "selected") and isinstance(item, bool):
            setattr(sel, key, item)
        elif key == "index" and isinstance(item, int) and not isinstance(item, bool) and item >= 0 and tap:
            sel.index = item
        elif key == "optional" and isinstance(item, bool):
            sel.optional = item
        elif key == "longPress" and tap and isinstance(item, bool):
            continue
        elif key == "repeat" and tap:
            ctx.problem(where, command, "repeats the tap", "write the taps out as steps")
            bad = True
        elif key == "point" and tap and not nested:
            point = _substitute(_scalar(item), env, ctx, where, command)
            if point is None:
                bad = True
            elif point and (PERCENT_POINT.match(point) or ABS_POINT.match(point)):
                sel.point = point.replace(" ", "")
            else:
                ctx.problem(where, command, f"has the point {str(item)[:30]!r}, which isn't x,y or x%,y%",
                            "write it as 50%,90%")
                bad = True
        elif key in RELATIVE and tap and not nested:
            other = _selector(item, where, command, ctx, env, nested=True)
            if other is None:
                bad = True
            elif other.relative or other.index is not None:
                ctx.problem(where, command, f"nests {key} more than one level",
                            "describe the element by its own text or id")
                bad = True
            else:
                sel.relative.append((key, other))
        else:
            ctx.problem(where, command, f"uses the selector option {key}, which Mobster can't match",
                        "select by text or id" if tap else "assert on text or id; Mobster can't judge this option",
                        assertion=not tap)
            bad = True
    if not bad and sel.point is None and sel.text is None and sel.id is None and not (tap and sel.relative):
        ctx.problem(where, command, "has no text, id or point", "give the text or id to match", assertion=not tap)
        bad = True
    if not bad and sel.point is not None and (sel.text or sel.id or sel.relative):
        ctx.problem(where, command, "mixes a point with a text or id", "use one or the other")
        bad = True
    return None if bad else sel


def _expect(sel, present, name, ctx, where, command):
    """The Mobster assertion for a Maestro assertVisible / assertNotVisible selector."""
    if sel.index is not None or sel.point is not None or sel.relative or sel.optional:
        what = "optional" if sel.optional else "a relative position, index or point"
        ctx.problem(where, command, f"asserts with {what}, which an assertion on the accessibility tree can't carry",
                    "assert on the element's text or id", assertion=True)
        return None
    selector = {}
    if sel.text is not None:
        selector["label"] = _matcher(sel.text)
    if sel.id is not None:
        selector["id"] = _matcher(sel.id, fold=False)
    if sel.enabled is not None:
        selector["enabled"] = sel.enabled
    if sel.selected is not None:
        selector["selected"] = sel.selected
    verb = "visible" if present else "not visible"
    shown = sel.text if sel.text is not None else f"id {sel.id}"
    label = f"{shown} is {verb}"
    return {"name": label[:120], ("visible" if present else "absent"): selector}


def _commands(commands, env, ctx, base, label, depth, chain):
    if not isinstance(commands, list):
        ctx.problem(label, "flow", "has no list of commands", "write the commands as a YAML list after `---`")
        return
    for number, command in enumerate(commands, 1):
        name, arg = _split(command)
        where = f"{label}command {number} ({name or '?'})"
        if name is None:
            ctx.problem(where, "?", "isn't a command Mobster can read", "write one command per list item")
            continue
        _command(name, arg, env, ctx, base, where, depth, chain)


def _split(command):
    if isinstance(command, str):
        return command, None
    if isinstance(command, dict) and len(command) == 1:
        name, arg = next(iter(command.items()))
        return (name, arg) if isinstance(name, str) else (None, None)
    return None, None


def _act(ctx, text):
    ctx.ops.append(("act", text))


def _command(name, arg, env, ctx, base, where, depth, chain):
    def sub(value):
        return _substitute(value, env, ctx, where, name)

    before = len(ctx.problems) + len(ctx.placeholders) + len(ctx.dropped)
    try:
        _dispatch(name, arg, env, ctx, base, where, depth, chain, sub)
    except ValueError as error:
        ctx.problem(where, name, f"has a value that {error}", "shorten it, or split the flow")
    if name != "runFlow" and before == len(ctx.problems) + len(ctx.placeholders) + len(ctx.dropped):
        ctx.converted += 1


def _dispatch(name, arg, env, ctx, base, where, depth, chain, sub):
    if name == "launchApp":
        return _launch(arg, env, ctx, where, sub)
    if name in ("tapOn", "doubleTapOn", "longPressOn"):
        sel = _selector(arg, where, name, ctx, env, tap=True)
        if sel is None:
            return
        verb = {"tapOn": "Tap", "doubleTapOn": "Double-tap", "longPressOn": "Press and hold"}[name]
        if isinstance(arg, dict) and (arg.get("longPress") is True):
            verb = "Press and hold"
        target = sel.describe()
        if sel.optional:
            return _act(ctx, f"If {target} is on screen, {verb[0].lower() + verb[1:]} it; otherwise skip this step")
        if sel.point is not None and ABS_POINT.match(sel.point):
            ctx.notes.append(f"{where}: the point {sel.point} is in screen points, so it depends on the device; "
                             "write it as 50%,90% to work on any size")
        return _act(ctx, f"{verb} {target}")
    if name in ("assertVisible", "assertNotVisible"):
        sel = _selector(arg, where, name, ctx, env)
        if sel is not None:
            found = _expect(sel, name == "assertVisible", name, ctx, where, name)
            if found is not None:
                ctx.ops.append(("assert", found))
        return
    if name == "extendedWaitUntil":
        return _wait_until(arg, env, ctx, where)
    if name == "inputText":
        if not _scalar(arg):
            return ctx.problem(where, name, "has no text to type", "give the text as a string")
        text = sub(_scalar(arg))
        if text is None:
            return
        return _act(ctx, f"Type {_quote(text, INPUT_LIMIT)} into the focused text field")
    if name == "eraseText":
        count = arg.get("charactersToErase") if isinstance(arg, dict) else arg
        if count is None:
            return _act(ctx, "Erase the text in the focused text field")
        if isinstance(count, int) and not isinstance(count, bool) and 0 < count <= 1000:
            return _act(ctx, f"Delete {count} characters from the focused text field")
        return ctx.problem(where, name, "has a character count Mobster can't read", "give a whole number")
    if name == "hideKeyboard":
        return _act(ctx, "Dismiss the keyboard")
    if name == "scroll":
        return _act(ctx, "Scroll down")
    if name == "scrollUntilVisible":
        return _scroll_until(arg, env, ctx, where)
    if name == "swipe":
        return _swipe(arg, env, ctx, where)
    if name == "back":
        ctx.notes.append(f"{where}: iOS has no back key, so the step taps the Back button on screen or swipes from "
                         "the left edge")
        return _act(ctx, "Go back one screen with the Back button on screen, or by swiping from the left edge")
    if name == "pressKey":
        key = str(arg).strip().lower() if _scalar(arg) else ""
        if key in KEYS:
            return _act(ctx, KEYS[key])
        return ctx.problem(where, name, f"presses {str(arg)[:30]!r}, a key iOS doesn't have",
                           "use Home, Enter or Backspace")
    if name == "openLink":
        link = sub(_scalar(arg.get("link")) if isinstance(arg, dict) else _scalar(arg))
        if link is None and "${" in str(arg):
            return
        if not link or not re.match(r"[A-Za-z][A-Za-z0-9+.\-]*:", link) or len(link) > LIMITS["url"]:
            return ctx.problem(where, name, "has a link Mobster can't read",
                               "write the full link, such as yourapp://home")
        if not ctx.started() and "url" not in ctx.launch:
            ctx.launch["url"] = link
            return
        return _act(ctx, f"Open the link {_quote(link, LIMITS['url'])}")
    if name == "waitForAnimationToEnd":
        return _act(ctx, "Wait for animations on screen to finish")
    if name == "runFlow":
        return _run_flow(arg, env, ctx, base, where, depth, chain)
    if name in UNSUPPORTED:
        why, instead = UNSUPPORTED[name]
        return ctx.unsupported(where, name, why, instead, assertion=name in ASSERTIONS)
    ctx.unsupported(where, name, "isn't a Maestro command Mobster knows",
                    "check the spelling, or run this step by hand", assertion=name.startswith("assert"))


def _launch(arg, env, ctx, where, sub):
    options = {} if arg is None else arg
    if not isinstance(options, dict):
        return ctx.problem(where, "launchApp", "has options Mobster can't read", "use clearState or nothing")
    initial = not ctx.started() and not ctx.launch.get("seen")
    app = options.get("appId")
    resolved = sub(_scalar(app)) if app is not None else None
    if app is not None and resolved is None:
        return
    if app is not None and resolved != ctx.launch.get("appId"):
        return ctx.problem(where, "launchApp", "launches a different app", "write one flow per app")
    for key in options:
        if key in ("appId", "stopApp", "clearState"):
            continue
        why = {"arguments": "passes launch arguments, whose iOS form Mobster can't confirm",
               "permissions": "sets permissions, which isn't a check step"}.get(key, f"uses the option {key}")
        ctx.problem(where, "launchApp", why, "set it before the run, or in the check's launch.args by hand")
    clear = options.get("clearState") is True
    if initial:
        ctx.launch["seen"] = True
        ctx.launch["reset"] = "data" if clear else "none"
        return
    if clear:
        return ctx.problem(where, "launchApp", "clears app data in the middle of the flow",
                           "put clearState on the first launchApp, or split the flow")
    _act(ctx, "Bring the app to the front" if options.get("stopApp") is False
         else "Force-quit the app, then open it again")


def _wait_until(arg, env, ctx, where):
    if not isinstance(arg, dict) or ("visible" in arg) == ("notVisible" in arg):
        return ctx.problem(where, "extendedWaitUntil", "doesn't have exactly one of visible and notVisible",
                           "give one", assertion=True)
    present = "visible" in arg
    sel = _selector(arg["visible" if present else "notVisible"], where, "extendedWaitUntil", ctx, env)
    if sel is None or arg.get("optional") is True:
        if sel is not None:
            ctx.problem(where, "extendedWaitUntil", "is optional, so it asserts nothing", "remove optional",
                        assertion=True)
        return
    found = _expect(sel, present, "extendedWaitUntil", ctx, where, "extendedWaitUntil")
    if found is None:
        return
    timeout = arg.get("timeout")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 5000:
        ctx.notes.append(f"{where}: Maestro waits up to {timeout / 1000:g} s; Mobster waits --assert-timeout "
                         "(default 5) for the screen to settle, then judges")
    ctx.ops.append(("assert", found))


def _scroll_until(arg, env, ctx, where):
    if not isinstance(arg, dict) or "element" not in arg:
        return ctx.problem(where, "scrollUntilVisible", "has no element to scroll to", "give element: with text or id")
    sel = _selector(arg["element"], where, "scrollUntilVisible", ctx, env, tap=True)
    direction = str(arg.get("direction", "DOWN")).upper()
    if direction not in DIRECTIONS:
        return ctx.problem(where, "scrollUntilVisible", f"scrolls {direction[:20]!r}, not UP, DOWN, LEFT or RIGHT",
                           "fix the direction")
    if sel is not None:
        tail = "; continue if it never appears" if arg.get("optional") is True else ""
        _act(ctx, f"Scroll {direction.lower()} until {sel.describe()} is visible{tail}")


def _swipe(arg, env, ctx, where):
    arg = arg if isinstance(arg, dict) else {}
    if "direction" in arg and "from" not in arg:
        direction = str(arg["direction"]).upper()
        if direction in DIRECTIONS:
            return _act(ctx, f"Swipe {direction.lower()} across the screen")
    elif "direction" in arg and "from" in arg:
        sel = _selector(arg["from"], where, "swipe", ctx, env, tap=True)
        direction = str(arg["direction"]).upper()
        if direction in DIRECTIONS and sel is not None:
            return _act(ctx, f"Swipe {direction.lower()} starting on {sel.describe()}")
        if sel is not None:
            return ctx.problem(where, "swipe", "has a direction Mobster can't read", "use UP, DOWN, LEFT or RIGHT")
        return
    elif "start" in arg and "end" in arg:
        points = []
        for key in ("start", "end"):
            text = _scalar(arg[key])
            if text and (PERCENT_POINT.match(text) or ABS_POINT.match(text)):
                points.append(text.replace(" ", ""))
        if len(points) == 2:
            return _act(ctx, f"Swipe from the screen point {points[0]} to {points[1]}")
    ctx.problem(where, "swipe", "isn't a swipe Mobster can read",
                "use direction, or start and end as 50%,80%")


def _run_flow(arg, env, ctx, base, where, depth, chain):
    if isinstance(arg, str):
        arg = {"file": arg}
    if not isinstance(arg, dict):
        return ctx.problem(where, "runFlow", "has options Mobster can't read", "give a file or commands")
    if "when" in arg:
        return ctx.problem(where, "runFlow", "runs only when a condition holds, and a check is a fixed list of steps",
                           "write the branch you want as its own flow")
    inner = dict(env)
    given = arg.get("env") or {}
    if not isinstance(given, dict):
        return ctx.problem(where, "runFlow", "has an env Mobster can't read", "write env as KEY: value")
    for key, value in given.items():
        inner[str(key)] = _substitute(str(value), env, ctx, where, "runFlow") or ""
    if depth >= MAX_DEPTH:
        return ctx.problem(where, "runFlow", f"nests more than {MAX_DEPTH} flows deep", "flatten the flows")
    if "commands" in arg:
        return _commands(arg["commands"], inner, ctx, base, f"{where} > ", depth + 1, chain)
    target = _scalar(arg.get("file"))
    if not target:
        return ctx.problem(where, "runFlow", "has neither a file nor commands", "give one")
    target = _substitute(target, env, ctx, where, "runFlow")
    if target is None:
        return
    path = (base / target).resolve()
    try:
        path.relative_to(ctx.root)
    except ValueError:
        return ctx.problem(where, "runFlow", f"reads {target[:60]}, outside the folder being imported",
                           "keep subflows inside the folder you import")
    if path in chain:
        return ctx.problem(where, "runFlow", "runs a flow that runs it again", "remove the loop")
    try:
        config, commands = read_flow(path)
    except MaestroError as error:
        return ctx.problem(where, "runFlow", f"{target[:60]} {error}", "fix the file or the path")
    for key, value in (config.get("env") or {}).items() if isinstance(config.get("env"), dict) else ():
        if str(key) in inner and inner[str(key)] != str(value):
            return ctx.problem(where, "runFlow", f"{target[:60]} sets {key} to a different value than the flow that "
                               "runs it, and Mobster can't tell which wins", "set it in one place")
        inner[str(key)] = str(value)
    app = config.get("appId")
    if app is not None and _substitute(str(app), inner, ctx, where, "runFlow") not in (None, ctx.launch.get("appId")):
        return ctx.problem(where, "runFlow", f"{target[:60]} is for a different app", "write one flow per app")
    _commands(commands, inner, ctx, path.parent, f"{path.relative_to(ctx.root)} ", depth + 1, chain | {path})


def _pack(steps):
    """At most LIMITS['steps'] steps: join neighbours with ", then" when there are more."""
    cap, limit = LIMITS["steps"], LIMITS["step"]
    if len(steps) <= cap:
        return steps
    size = -(-len(steps) // cap)
    out = [", then ".join(s[0].lower() + s[1:] if i else s for i, s in enumerate(steps[i:i + size]))
           for i in range(0, len(steps), size)]
    if any(len(step) > limit for step in out):
        raise ValueError("long")
    return out


def _segments(ops):
    """Cumulative segments: [(steps so far, assertions of this group)], one per assertion group that more actions
    follow, then the last."""
    steps, group, out = [], [], []
    for op in ops:
        if op[0] == "act":
            if group:
                out.append((list(steps), group))
                group = []
            steps.append(op[1])
        elif op[0] == "assert":
            group.append(op[1])
    trailing = 0
    if group or not out:
        out.append((list(steps), group))
    else:
        trailing = len(steps) - len(out[-1][0])
    return out, trailing


def convert_flow(file, root, *, bundle=None, app_path=None, skip_unsupported=False, stem=None, shown=None):
    """The FlowResult for one Maestro flow file."""
    file, root = Path(file).resolve(), Path(root).resolve()
    result = FlowResult(shown or str(file.relative_to(root)))
    stem = stem or file.stem
    try:
        config, commands = read_flow(file)
    except MaestroError as error:
        result.outcome, result.reason = "refused", f"{file.name} {error}"
        return result
    ctx = _Ctx(root, skip_unsupported)
    unknown = [key for key in config if key not in HEADER_KEYS]
    for key in unknown:
        ctx.problem("config", str(key), "isn't a flow setting Mobster reads",
                    "remove it, or it belongs to a workspace config, not a flow")
    env = {}
    raw_env = config.get("env") or {}
    if isinstance(raw_env, dict):
        env = {str(key): str(value) for key, value in raw_env.items()}
    for hook in ("onFlowStart", "onFlowComplete"):
        if hook in config:
            ctx.problem("config", hook, "runs commands before or after the flow",
                        "put those commands at the start or end of the flow")
    app_id = _substitute(_scalar(config.get("appId")), env, ctx, "config", "appId") if config.get("appId") else None
    app_id = bundle or app_id
    if not app_id and ctx.problems:
        result.outcome, result.problems = "refused", ctx.problems
        return result
    if not app_id:
        result.outcome = "skipped"
        result.reason = "no appId, so it is a subflow or can't name its app; pass --bundle to import it as a flow"
        return result
    ctx.launch["appId"] = app_id
    if not commands:
        result.outcome, result.reason = "refused", "has no commands"
        return result
    _commands(commands, env, ctx, file.parent, "", 0, frozenset({file}))
    result.notes, result.dropped = ctx.notes, ctx.dropped
    result.placeholders, result.converted = ctx.placeholders, ctx.converted
    if ctx.placeholders:
        result.notes.append(f"{len(ctx.placeholders)} command{'' if len(ctx.placeholders) == 1 else 's'} became a "
                            "step marked TODO: the first run (Smart) fills it, then replay keeps what it did; read "
                            "that run's report before you trust the check")
    if ctx.problems:
        result.outcome, result.problems = "refused", ctx.problems
        return result
    if not any(op[0] == "assert" for op in ctx.ops):
        result.notes.append("the flow asserts nothing, so the check ends needs_review; add an assertVisible")
    name = _text_name(config.get("name"), stem)
    segments, trailing = _segments(ctx.ops)
    if trailing:
        result.notes.append(f"the last {trailing} step{'' if trailing == 1 else 's'} come after the final "
                            "assertion, so no check runs them; nothing judges them")
    tags = _tags(config.get("tags"), result)
    for index, (steps, asserts) in enumerate(segments, 1):
        try:
            steps = _pack(steps)
        except ValueError:
            result.outcome = "refused"
            result.problems.append(Problem("flow", "steps", f"has more than {LIMITS['steps']} steps that can't be "
                                           "joined into one check", "split the flow with runFlow into shorter flows"))
            return result
        data = {"version": 1, "name": name if len(segments) == 1 else f"{name} (part {index} of {len(segments)})",
                "app": {"bundle": app_id}}
        data["reset"] = ctx.launch.get("reset", "none")
        data["tags"] = tags
        if ctx.launch.get("url"):
            data["launch"] = {"url": ctx.launch["url"]}
        if steps:
            data["steps"] = steps
        data["expect"] = asserts
        try:
            canonical = check_to_dict(check_from_dict(data, base_dir=root))
        except CheckError as error:
            result.outcome = "refused"
            result.problems.append(Problem("flow", "check", f"makes a check Mobster rejects: {error}",
                                           "fix the flow, or write the check by hand"))
            return result
        if app_path:
            canonical["app"]["path"] = str(app_path)
        result.checks.append((stem if len(segments) == 1 else f"{stem}-part-{index}", canonical))
    if len(segments) > 1:
        result.notes.append(f"assertions in the middle of the flow became {len(segments)} checks; each one replays "
                            "the steps up to its assertions")
    return result


def _text_name(value, stem):
    text = value.strip() if isinstance(value, str) and value.strip() else stem.replace("_", " ").replace("-", " ")
    return text[:LIMITS["name"]]


def _tags(value, result):
    out = ["maestro"]
    for tag in value if isinstance(value, list) else ():
        word = re.sub(r"[^a-z0-9_-]+", "-", str(tag).lower()).strip("-")[:40]
        if word and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", word):
            if word not in out and len(out) < LIMITS["tags"]:
                out.append(word)
        else:
            result.notes.append(f"the tag {str(tag)[:30]!r} isn't a Mobster tag (a-z, 0-9, - and _), "
                                "so it was left out")
    return out


def convert_tree(paths, *, bundle=None, app_path=None, skip_unsupported=False):
    """[FlowResult] for every flow under ``paths``."""
    files = discover(paths)
    if not files:
        raise MaestroError("no .yaml or .yml files found")
    shown_names = _unique_stems(files)
    results = []
    for (file, root), (stem, shown) in zip(files, shown_names):
        if file.name.lower() in ("config.yaml", "config.yml"):
            item = FlowResult(shown, "skipped", reason="a workspace config, not a flow; its includeTags, excludeTags "
                                                      "and executionOrder are not applied")
        else:
            item = convert_flow(file, root, bundle=bundle, app_path=app_path, skip_unsupported=skip_unsupported,
                                stem=stem, shown=shown)
        results.append(item)
    return results


def _unique_stems(files):
    """(stem, shown path) per file: the file name, or the folder path joined with - when two share a name."""
    out, seen = [], {}
    for file, root in files:
        rel = file.relative_to(root)
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", rel.with_suffix("").as_posix().replace("/", "-")).strip("-.") or "flow"
        count = seen.get(stem.lower(), 0)
        seen[stem.lower()] = count + 1
        out.append((stem if not count else f"{stem}-{count + 1}", rel.as_posix()))
    return out


def render(check, source):
    """A check dict as YAML text with a header that names the Maestro flow it came from."""
    import yaml
    header = (f"# Imported from the Maestro flow {source}.\n"
              "# Run it with: mobster test <this file>\n")
    return header + yaml.safe_dump(check, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100)
