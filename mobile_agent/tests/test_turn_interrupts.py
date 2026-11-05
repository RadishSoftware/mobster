"""Every interrupt at every boundary, in one table: Stop, "stop after this step", pause and a message, arriving before
the first turn, while the model decides, between chained actions and while an approval waits; and a reply that comes
back after the deadline. What is pinned: the run's status, which actions reached the phone, and that nothing is done
while paused."""

import unittest
from unittest import mock

from mobile_agent import frontier
from mobile_agent.frontier import STOPPED_AFTER, FrontierAgent, prompt_text
from mobile_agent.harness import control
from mobile_agent.state import Element
from mobile_agent.steering import SteeringQueue
from mobile_agent.tests.test_frontier import Driver, Script, screen


def rows(n=6):
    """One row a screen, a different one each time, so a chained tap finds its next target on the screen it left."""
    return [screen(Element("1", f"Row {i}", "Button", (.1, .2, .8, .05)),
                   Element("2", "Send", "Button", (.6, .9, .3, .05))) for i in range(n)]


class Phone(Driver):
    """The scripted phone; ``on_act`` runs after its first action (the mid-chain trigger)."""

    def __init__(self, screens, on_act=None):
        super().__init__(screens)
        self.on_act = on_act

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        super().execute(operation, target, snapshot, text, timeout)
        if self.on_act is not None and len(self.actions) == 1:
            self.on_act()


class Model(Script):
    """The scripted model; ``on_decide`` runs inside its first decision (the during-the-model-call trigger)."""

    def __init__(self, steps, on_decide=None):
        super().__init__(steps)
        self.on_decide, self.prompts = on_decide, []

    def complete(self, messages, schema, timeout=60):
        out = super().complete(messages, schema, timeout)
        if "actions" in schema["properties"]:
            self.prompts.append(prompt_text(messages))
            if self.on_decide is not None and self.usage["calls"] == 1:
                self.on_decide()
        return out


def stop(ctx):
    ctx["stopped"] = True


def stop_after(ctx):
    control.request_stop_after(ctx["queue"])


def pause(ctx):
    control.set_paused(ctx["queue"], True)


def steer(ctx):
    ctx["queue"].put("Use the other thread", source="app")


# interrupt -> (how it fires, the status it owes, the reason it owes or None)
INTERRUPTS = {"stop": (stop, "stopped", "Stopped by the user"),
              "stop_after": (stop_after, "stopped", STOPPED_AFTER["reason"]),
              "pause": (pause, "completed", None),
              "steer": (steer, "completed", None)}
# turn_top: there before the first turn. model_call: lands while the model decides the first tap. mid_chain: the model
# chains two taps and it lands after the first. approval: lands while the person is asked about Send, who approves.
WHERE = ("turn_top", "model_call", "mid_chain", "approval")
# The actions the phone receives. An approved action is the step in progress: it finishes, nothing new starts. An
# action chosen while a pause came in does not run: the model decides again after Continue.
EXPECTED = {
    ("stop", "turn_top"): [], ("stop", "model_call"): [], ("stop", "mid_chain"): ["Row 0"], ("stop", "approval"): [],
    ("stop_after", "turn_top"): [], ("stop_after", "model_call"): [], ("stop_after", "mid_chain"): ["Row 0"],
    ("stop_after", "approval"): ["Send"],
    ("pause", "turn_top"): ["Row 0"], ("pause", "model_call"): [], ("pause", "mid_chain"): ["Row 0"],
    ("pause", "approval"): ["Send"],
    ("steer", "turn_top"): ["Row 0"], ("steer", "model_call"): ["Row 0"], ("steer", "mid_chain"): ["Row 0"],
    ("steer", "approval"): ["Send"],
}


def continue_at_once(queue, *, cancelled=None, **_):
    """Stands in for control.wait_while_paused: the loop is holding, so the person lets it go on."""
    control.set_paused(queue, False)
    return "continued"


def run(name, where):
    fire, _, _ = INTERRUPTS[name]
    ctx = {"queue": SteeringQueue(), "stopped": False}
    chain = {"mid_chain": [("TAP", "Row 0", None), ("TAP", "Row 1", None)],
             "approval": [("TAP", "Send", None)]}.get(where, [("TAP", "Row 0", None)])
    model = Model([chain] + [("DONE", None, None)] * 3,
                  on_decide=(lambda: fire(ctx)) if where == "model_call" else None)
    phone = Phone(rows(), on_act=(lambda: fire(ctx)) if where == "mid_chain" else None)
    if where == "turn_top":
        fire(ctx)

    def approve(request):
        if where == "approval":
            fire(ctx)
        return "approved"
    events = []
    agent = FrontierAgent(phone, model, emit=events.append, screenshots=False, settle_seconds=0, skills=(),
                          steering=ctx["queue"], cancelled=lambda: ctx["stopped"], approve=approve)
    with mock.patch.object(control, "wait_while_paused", continue_at_once):
        result = agent.run("Send the message to Sam" if where == "approval" else "Tap the rows")
    return result, [e["event"] for e in events], [label for _, label, _ in phone.actions], model


class InterruptMatrixTests(unittest.TestCase):
    def test_every_interrupt_at_every_boundary(self):
        for name, (_, status, reason) in INTERRUPTS.items():
            for where in WHERE:
                with self.subTest(interrupt=name, where=where):
                    result, kinds, acted, model = run(name, where)
                    self.assertEqual(result["status"], status)
                    if reason is not None:
                        self.assertEqual(result["reason"], reason)
                    self.assertEqual(acted, EXPECTED[(name, where)])
                    if name == "pause":
                        # Held at a boundary, nothing done while held, and the model decided again after.
                        paused, continued = kinds.index("paused"), kinds.index("continued")
                        self.assertLess(paused, continued)
                        self.assertNotIn("frontier_action", kinds[paused:continued])
                        self.assertIn("frontier_decision", kinds[continued:])
                    if name == "steer":
                        self.assertIn("steer_applied", kinds)
                        self.assertTrue(any("Use the other thread" in prompt for prompt in model.prompts))

    def test_a_reply_that_arrives_after_the_deadline_never_acts(self):
        clock = {"t": 1000.0}
        model = Model([[("TAP", "Row 0", None)], ("DONE", None, None)],
                      on_decide=lambda: clock.__setitem__("t", clock["t"] + 10.0))
        phone = Phone(rows())
        events = []
        with mock.patch("time.monotonic", lambda: clock["t"]), \
                mock.patch.object(frontier.phases, "FINAL_CALL_SECONDS", 0):
            result = FrontierAgent(
                phone, model, emit=events.append, screenshots=False, settle_seconds=0, skills=(), max_seconds=5,
            ).run("Tap the rows")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(phone.actions, [])
        self.assertIn("deadline", [event["reason"] for event in events if event["event"] == "frontier_chunk_stop"])
