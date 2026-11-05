"""A scripted sign-up app (Lumen), Smart model and simulator manager for the replay core."""

import re
import threading
import time
from types import SimpleNamespace
from xml.sax.saxutils import quoteattr

from mobile_agent.drivers import Driver
from mobile_agent.sim import AppInfo
from mobile_agent.state import Element, Snapshot
from mobile_agent.tests.fakes import SimLease
from mobile_agent.tests.sota_world import Turn, act
from mobile_agent.verify.tree import EVIDENCE_SOURCE_PATH, GLANCE_SOURCE_PATH

BUNDLE, NAME = "com.example.lumen", "Lumen"
WIDTH, HEIGHT = 402, 874
LABELS = {1: {"continue": "Continue", "email": "Email", "create": "Create account", "delete": "Delete account"},
          2: {"continue": "Next", "email": "Email", "create": "Create account", "delete": "Delete account"},
          3: {"continue": "Get started", "email": "Your email", "create": "Sign up", "delete": "Remove account"},
          4: {"continue": "Continue", "email": "Email", "create": "Create account", "delete": "Delete account"}}
# Seconds each read and action takes, times PACE["value"] (0 in tests; the benchmark sets the long-form world's
# measured USB iPhone times).
LATENCY = {"observe": 0.0, "TAP": 0.0, "TYPE": 0.0, "evidence": 0.0}
PACE = {"value": 0.0}


def wait(kind):
    seconds = LATENCY.get(kind, 0.0) * PACE["value"]
    if seconds > 0:
        time.sleep(seconds)


class LumenPhone(Driver):
    """One simulator with Lumen. ``state``: {"screen", "email", "build", "sheet", "log", "deleted"}."""

    capabilities = frozenset({"active_app", "tap_point", "clear_text", "call"})
    can_type = True

    def __init__(self, state):
        self.state = state
        self.lock = threading.RLock()

    def screen_rows(self):
        s, labels = self.state, LABELS[self.state["build"]]
        screen = s["screen"]
        if screen == "welcome":
            rows = [Element("n0", "Welcome to Lumen", "StaticText", (.1, .3, .8, .05)),
                    Element("n1", labels["continue"], "Button", (.1, .85, .8, .06))]
            if s.get("sheet"):
                rows = [Element("n5", "What's new", "StaticText", (.1, .2, .8, .05)),
                        Element("n6", "Not now", "Button", (.1, .7, .8, .06))]
        elif screen == "signup":
            rows = [Element("n0", "Create your account", "StaticText", (.1, .15, .8, .05)),
                    Element("n1", labels["email"], "TextField", (.1, .3, .8, .05), editable=True,
                            value=s["email"], actions=("TAP", "TYPE")),
                    Element("n2", labels["create"], "Button", (.1, .85, .8, .06))]
        else:
            rows = [Element("n0", f"Signed in as {s['email']}", "StaticText", (.1, .2, .8, .05)),
                    Element("n1", labels["delete"], "Button", (.1, .85, .8, .06))]
        return rows

    def observe(self, timeout=10):
        wait("observe")
        with self.lock:
            rows = self.screen_rows()
            return Snapshot(rows, "\n".join(e.label for e in rows), WIDTH, HEIGHT, "synthetic_fixture",
                            bundle_id=BUNDLE, revision=f"{self.state['screen']}-{len(self.state['log'])}")

    def active_app(self, *args, **kwargs):
        return BUNDLE

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        wait("TYPE" if operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT") else "TAP")
        s, labels = self.state, LABELS[self.state["build"]]
        label = target.label if isinstance(target, Element) else (target or "")
        with self.lock:
            s["log"].append((operation, label, text))
            if operation == "LAUNCH_APP":
                return {}
            if s["screen"] == "welcome" and s.get("sheet"):
                if operation == "TAP" and label == "Not now":
                    s["sheet"] = False
            elif s["screen"] == "welcome" and operation == "TAP" and label == labels["continue"]:
                s["screen"] = "signup"
            elif s["screen"] == "signup" and operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT") \
                    and label == labels["email"]:
                s["email"] = (text or "") if operation == "SET_TEXT" else s["email"] + (text or "")
            elif s["screen"] == "signup" and operation == "TAP" and label == labels["create"] and s["email"]:
                s["screen"] = "home"
            elif s["screen"] == "home" and operation == "TAP" and label == labels["delete"]:
                s["deleted"] = True
                s["screen"], s["email"] = "welcome", ""
        return {}

    def tap_point(self, x, y, snapshot, timeout=10, purpose=None):
        wait("TAP")

    def clear_text(self, target, timeout=10):
        self.state["email"] = ""

    def call(self, method, path, body=None, timeout=10):
        if method == "GET" and path in (EVIDENCE_SOURCE_PATH, GLANCE_SOURCE_PATH):
            wait("evidence")
            return self.xml()  # the glance shows the same screen; ``visible`` is "true" on every node either way
        if path == "/alert/text":
            raise OSError("no alert")
        if (method, path) == ("POST", "/url"):
            return None
        raise AssertionError(f"unexpected WDA call {method} {path}")

    def xml(self):
        with self.lock:
            rows = self.screen_rows()
        children = []
        for element in rows:
            x, y, w, h = element.rect
            value = f" value={quoteattr(element.value)}" if element.value else ""
            children.append(f'<XCUIElementType{element.role} type="XCUIElementType{element.role}" '
                            f'label={quoteattr(element.label)} name={quoteattr(element.label)}{value} '
                            f'enabled="true" visible="true" x="{x * WIDTH:.0f}" y="{y * HEIGHT:.0f}" '
                            f'width="{w * WIDTH:.0f}" height="{h * HEIGHT:.0f}" traits=""/>')
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<XCUIElementTypeApplication type="XCUIElementTypeApplication" '
                f'name="{NAME}" label="{NAME}" enabled="true" visible="true" x="0" y="0" width="{WIDTH}" '
                f'height="{HEIGHT}" traits="" processId="1000" bundleId="{BUNDLE}"><XCUIElementTypeWindow '
                f'type="XCUIElementTypeWindow" enabled="true" visible="true" x="0" y="0" width="{WIDTH}" '
                f'height="{HEIGHT}" traits="">' + "".join(children) + "</XCUIElementTypeWindow>"
                "</XCUIElementTypeApplication>")

    def close(self):
        pass


def new_state(build=1, sheet=False):
    return {"screen": "welcome", "email": "", "build": build, "sheet": sheet or build == 4, "log": [],
            "deleted": False}


class Manager:
    """The simulator manager for Lumen: every launch starts the app afresh on the build ``state["build"]``."""

    def __init__(self, state, *, version="1.0 (1)", runtime="iOS 26.4"):
        self.state, self.version = state, version
        self.target = SimpleNamespace(udid="00000000-0000-4000-8000-00000000LUMN", name="Mobster · iPhone 17 Pro",
                                      device_type="iPhone 17 Pro", runtime=runtime, wda_url="http://127.0.0.1:1",
                                      mjpeg_url="")
        self.pid = 1000

    def acquire(self, device=None, runtime=None, *, timeout=600, cancelled=None):
        assert cancelled is None or callable(cancelled)
        return SimLease(self.target)

    def installed_app(self, target, bundle):
        return AppInfo(bundle_id=BUNDLE, name=NAME, version=self.version, path=None)

    def reset(self, target, bundle, level, app_path=None):
        build = self.state["build"]
        self.state.clear()
        self.state.update(new_state(build))

    def launch(self, target, bundle, args=(), env=None):
        self.pid += 1

    def terminate(self, target, bundle):
        pass

    def screenshot(self, target, path, *, max_width=None, quality=75):
        from PIL import Image
        width = max_width or 402
        Image.new("RGB", (width, round(width * HEIGHT / WIDTH)), (240, 240, 245)).save(path, "JPEG", quality=quality)
        return path

    def restarter(self, target):
        return None


EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
WELCOME_BUTTONS = ("Continue", "Next", "Get started")
# The email the model makes up when the request names none: text a replay never types on its own.
DEFAULT_EMAIL = "test.user@example.com"


class LumenModel:
    """model_clients.OpenAIChat's ``complete`` for Lumen, by rules. ``calls``, ``prompt_chars``: what a real model would
    have been sent (the task contract's call included)."""

    model = "scripted-lumen"
    per_call_reasoning = False

    def __init__(self, think=0.0):
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}
        self.model_failed, self.reasoning, self.connection = False, "low", None
        self.think = think          # seconds per call (the benchmark: the measured Smart turn)
        self.calls, self.prompt_chars, self.contract_calls = 0, 0, 0

    def complete(self, messages, schema, timeout=60, reasoning=None, **kwargs):
        from mobile_agent.frontier import prompt_text
        prompt = prompt_text(messages)
        self.prompt_chars += len(prompt)
        if self.think:
            time.sleep(self.think)
        if "items" in schema.get("properties", {}):
            self.contract_calls += 1
            return {"items": []}, {}
        self.calls += 1
        self.usage["calls"] += 1
        try:
            operations = schema["properties"]["actions"]["items"]["properties"]["op"]["enum"]
        except (KeyError, TypeError):
            operations = ()
        t = Turn(prompt, operations)
        actions, answer = decide(t)
        out = {"thought": "", "plan": None, "notes_add": [], "checklist_updates": [], "actions": actions,
               "answer": answer}
        if "data_json" in schema.get("properties", {}):
            out["data_json"] = None
        return out, {}


def decide(t):
    if t.has("Not now"):
        return [act("TAP", t.find("Not now"))], None
    for name in WELCOME_BUTTONS:
        if t.find(name, "Button"):
            return [act("TAP", t.find(name, "Button"))], None
    field = next((rid for rid, role, _ in t.rows if role == "TextField"), None)
    if field:
        email = EMAIL.search(t.request)
        typed = re.search(r'TextField "[^"]*"[^\n]*value[:=]\s*"?([^"\n]+)', t.prompt)
        email = email.group(0) if email else DEFAULT_EMAIL
        if not (typed and email in typed.group(1)):
            return [act("TYPE", field, email)], None
        button = next((rid for rid, role, label in t.rows if role == "Button"), None)
        return [act("TAP", button)], None
    if any(label.startswith("Signed in as") for label in t.labels()):
        return [act("DONE")], "Signed up."
    return [act("BLOCKED", None, "I don't know this screen")], "Blocked."


def check(email="sam@example.com", **extra):
    """The sign-up check: two steps, the email literal in them, and an expectation only the flow makes true."""
    from mobile_agent.verify.checks import check_from_dict
    data = {"name": "Sign up", "bundle_id": BUNDLE,
            "steps": ["Tap Continue", f"Sign up with {email}"],
            "expect": [{"text": f"Signed in as {email}"}], **extra}
    return check_from_dict(data)
