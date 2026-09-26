"""Offline stand-ins for the phone, the probe and Vertex. No device, no network."""

import io
import time

from mobile_agent.state import Element, Snapshot

SETTINGS = "com.apple.Preferences"


def element(label, role="Cell", rect=(.05, .3, .9, .06), value="", actions=("TAP",), locator="", editable=False,
            id=None):
    return Element(id or label or role, label, role, rect, editable, locator or f"/X/{role}", value, actions)


def snapshot(elements=(), bundle=SETTINGS, title=None, text=""):
    items = list(elements)
    if title is not None:
        items.append(Element("nav", title, "NavigationBar", (0, .06, 1, .05), False,
                             "/XCUIElementTypeNavigationBar[1]", "", ("TAP",)))
    return Snapshot(items, text, 393, 852, "wda", bundle_id=bundle)


def png(width=40, height=80):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 200, 200)).save(out, format="PNG")
    return out.getvalue()


class FakePhone:
    """Records gestures; serves a scripted sequence of AX snapshots."""

    def __init__(self, snapshots=None, bundle=SETTINGS):
        self.snapshots = list(snapshots or [snapshot()])
        self.index = 0
        self.calls = []
        self.bundle = bundle

    def current(self):
        return self.snapshots[min(self.index, len(self.snapshots) - 1)]

    def advance(self):
        self.index += 1

    def observe(self, timeout=8):
        self.calls.append(("observe",))
        return self.current()

    def screenshot(self, timeout=6):
        self.calls.append(("screenshot",))
        return png(), (40, 80)

    def size(self):
        return (393, 852)

    def foreground(self, timeout=4):
        return self.current().bundle_id

    def tap(self, x, y, hold_ms=40):
        self.calls.append(("tap", round(x), round(y)))
        self.advance()

    def long_press(self, x, y, seconds=2):
        self.calls.append(("long_press", round(x), round(y)))

    def drag(self, x1, y1, x2, y2, duration=.1):
        self.calls.append(("drag", round(x1), round(y1), round(x2), round(y2)))

    def swipe(self, operation, region=None):
        self.calls.append(("swipe", operation, region))
        self.advance()

    def back_gesture(self):
        self.calls.append(("back",))

    def type_text(self, text, enter=False):
        self.calls.append(("type", text, enter))

    def key(self, value):
        self.calls.append(("key", value))

    def home(self):
        self.calls.append(("home",))

    def activate(self, bundle_id):
        self.calls.append(("activate", bundle_id))

    def wait_keyboard(self, seconds=2.0):
        return True

    def close(self):
        pass


class FakeVertex:
    """Returns scripted responses in order and keeps every request body."""

    def __init__(self, responses, cost=0.001, latency_ms=100.0):
        self.responses = list(responses)
        self.requests = []
        self.cost = cost
        self.latency_ms = latency_ms

    def generate(self, model, body, timeout=90):
        import copy
        self.requests.append(copy.deepcopy(body))
        if not self.responses:
            raise AssertionError("no scripted response left")
        response = self.responses.pop(0)
        return response, {"model": model, "latency_ms": self.latency_ms, "prompt_tokens": 1000,
                          "output_tokens": 50, "cost_usd": self.cost}

    def probe(self, model, timeout=30):
        return True, "ok"


def model_turn(*parts):
    return {"candidates": [{"content": {"role": "model", "parts": list(parts)}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 50}}


def call(name, **args):
    return {"functionCall": {"name": name, "args": args, "id": f"id-{name}"}}


def text(value):
    return {"text": value}


class FakeProbe:
    """The harness's probe: reset returns a start screen; observe returns the final screen."""

    def __init__(self, start=None, final=None, fail_reset=False):
        self.start = start or snapshot(title="Settings")
        self.final = final or self.start
        self.fail_reset = fail_reset
        self.resets = []

    def reset(self, bundle, url=None):
        from mobile_agent.evals.oracles import ProbeUnavailable
        self.resets.append((bundle, url))
        if self.fail_reset:
            raise ProbeUnavailable("reset failed")
        return self.start

    def observe(self, bundle, timeout=10):
        return self.final


class Clock:
    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def context(phone=None, task=None, seconds=60):
    from mobile_agent.bench.agents.base import Context
    from mobile_agent.bench.safety import Monitor
    return Context(phone=phone, wda_url="http://127.0.0.1:8100", monitor=Monitor(task),
                   deadline=time.monotonic() + seconds, session="s")
