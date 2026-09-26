"""Deterministic fixture replay for development; never presented as a live device run."""

from .drivers import Driver
from .models import Decision
from .state import Element, Snapshot
from .task_policy import ActionSupport, OutputIntent, OutputSupport, StopGate


def screen(name="home"):
    data = {
        "home": ([Element("0", "Search", "Button", (.8, .06, .1, .05)),
                  Element("1", "Profile", "Button", (.8, .92, .15, .05))], "For You\nSearch\nProfile"),
        "search": ([Element("0", "Search", "SearchField", (.1, .06, .7, .05), True),
                    Element("1", "Search", "Button", (.82, .06, .15, .05))], "Search TikTok"),
        "results": ([Element("0", "Coffee brewing guide", "Button", (.05, .2, .9, .3)),
                     Element("1", "@coffee.studio", "Button", (.1, .55, .5, .05))],
                    "Top results\nCoffee brewing guide\n@coffee.studio"),
    }
    elements, text = data[name]
    return Snapshot(elements, text, 402, 874, "synthetic_fixture")


class DemoDriver(Driver):
    can_type = True

    def __init__(self):
        self.stage = "home"
        self.actions = []

    def observe(self, timeout=10):
        return screen(self.stage)

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append(operation)
        if self.stage == "home" and operation == "TAP":
            self.stage = "search"
        elif self.stage == "search" and operation == "TYPE":
            self.stage = "results"

    def close(self):
        pass


class DemoModel:
    def verify_action(self, snapshot, goal, operation, target, history, **kwargs):
        # Deliberately synthetic replay only; never a live authority oracle.
        if snapshot.source != "synthetic_fixture":
            return ActionSupport.UNCLEAR
        return ActionSupport.ALLOWED

    def decide(self, snapshot, goal, history, **kwargs):
        if "Top results" in snapshot.text:
            op, target, goal_p = "DONE", None, .99
        elif snapshot.text == "Search TikTok":
            op, target, goal_p = "TYPE", "0", .01
        else:
            op, target, goal_p = "TAP", "0", .01
        # This fixed replay represents the action-only Search coffee scenario, not inference.
        intent = OutputIntent.ACTION_ONLY if kwargs.get("classify_output") else None
        return Decision(op, target, .98, goal_p, .01, "fixture-policy (not Jev)", 0, {}, StopGate.CONTINUE, intent)

    def verify_output(self, goal, candidate, evidence, **kwargs):
        # Explicit fixture-only acceptance: never masquerades as semantic inference.
        expected = "Coffee brewing guide"
        matches = candidate["data"] == expected or candidate["data"] == {"title": expected}
        observed = any(entry["text"] == expected for entry in evidence["entries"])
        return OutputSupport.SUPPORTED if matches and observed else OutputSupport.UNCLEAR


class DemoHelper:
    """Offline fixture stub; never a live helper-model adapter."""

    calls = 0

    def ask(self, *args, **kwargs):
        self.calls += 1
        return "coffee"
