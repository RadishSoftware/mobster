"""Offline stand-ins for the screen and the probe. No device, no network."""

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


def context(task=None, seconds=60):
    from mobile_agent.bench.agents.base import Context
    from mobile_agent.bench.safety import Monitor
    return Context(wda_url="http://127.0.0.1:8100", monitor=Monitor(task),
                   deadline=time.monotonic() + seconds, session="s")
