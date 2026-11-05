import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mobile_agent import hints
from mobile_agent.harness_api import StepRecord
from mobile_agent.packs import health
from mobile_agent.state import Element, Snapshot

APP = "com.example.chirp"


def step(op="TAP", label="Inbox", role="Button", commit=None, shape=None, screen="s1"):
    return StepRecord(run_id="r1", step=3, index=0, op=op, app=APP,
                      target={"label": label, "role": role, "id": "e4", "frame": [0, 0, .1, .1]}, text=None,
                      screen=screen, changed=True, approval=None, commit=commit,
                      shape=tuple(shape if shape is not None else (hints.pair_hash(role, label), "deadbeef")))


def snapshot(*labels):
    return Snapshot([Element(str(i), label, "Button", (.1, .1 * i, .2, .05)) for i, label in enumerate(labels)],
                    "\n".join(labels), 400, 800, "synthetic_fixture", bundle_id=APP)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_a_hint_below_sure_goes_to_the_agent(self):
        ok, why = hints.Gate(self.dir).trust("pack", APP, "dms", confidence=.4)
        self.assertFalse(ok)
        self.assertIn("0.40", why)
        self.assertTrue(hints.Gate(self.dir).trust("pack", APP, "dms", confidence=.9)[0])

    def test_two_misses_in_a_run_turn_a_source_off_for_that_app_only(self):
        gate = hints.Gate(self.dir)
        self.assertFalse(gate.note(False, "pack", APP))
        self.assertTrue(gate.note(False, "pack", APP))
        self.assertFalse(gate.trust("pack", APP, "dms")[0])
        self.assertTrue(gate.trust("pack", "com.other", "dms")[0])
        self.assertTrue(gate.trust("ios", APP, "open_app")[0])

    def test_a_key_that_missed_its_last_uses_on_this_version_is_stale_there_only(self):
        for ok in (True, False, False):
            hints.record(self.dir, {"kind": "use", "source": "pack", "app": APP, "key": "dms", "ok": ok,
                                    "version": "2.0"})
        gate = hints.Gate(self.dir)
        ok, why = gate.trust("pack", APP, "dms", version="2.0")
        self.assertFalse(ok)
        self.assertIn("this version", why)
        self.assertTrue(gate.trust("pack", APP, "dms", version="2.1")[0])  # a new version gets a fresh chance
        self.assertTrue(gate.trust("pack", APP, "reels", version="2.0")[0])
        hints.record(self.dir,
                     {"kind": "use", "source": "pack", "app": APP, "key": "dms", "ok": True, "version": "2.0"})
        self.assertTrue(gate.trust("pack", APP, "dms", version="2.0")[0])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.listener = hints.HintListener()
        self.run = SimpleNamespace(id="r1", data_dir=self.dir, app={"bundleId": APP})
        self.listener.on_created(self.run, SimpleNamespace(config=None))

    def lines(self, name=hints.FILE):
        path = self.dir / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_a_replay_drift_is_logged_with_the_variant_where_the_agent_took_over_and_heals_after_a_done_run(self):
        self.listener.on_event(self.run, {"event": "replay_drift", "step": 3, "action": "tap Messages",
                                          "why": "no Messages button"})
        self.listener.on_step(self.run, step())
        self.listener.on_finished(self.run, {"status": "completed", "outcome": "done"}, {})
        use, drift = self.lines()
        self.assertEqual((use["source"], use["app"], use["key"], use["ok"]), ("replay", APP, "step 3", False))
        self.assertEqual((drift["kind"], drift["variant"]), ("drift", "s1"))
        heal, = self.lines(hints.HEALS)
        self.assertTrue(heal["checked"])
        self.assertEqual(heal["action"]["label"], "Inbox")

    def test_no_heal_from_a_run_that_did_not_finish(self):
        self.listener.on_event(self.run, {"event": "hint", "source": "ios", "key": "open_app", "ok": False})
        self.listener.on_step(self.run, step())
        self.listener.on_finished(self.run, {"status": "failed", "outcome": "failed"}, {})
        self.assertEqual(self.lines(hints.HEALS), [])
        self.assertEqual([line["kind"] for line in self.lines()], ["use", "drift"])

    def test_a_saved_program_s_steps_land_and_its_drift_handovers_miss(self):
        # Steps the loop ran by code land; a handover because the screen didn't fit is drift; a handover by design
        # (a step the person asked Mobster to choose each run) is neither.
        for event in ({"event": "frontier_program", "step": 0, "at": 0, "op": "TAP"},
                      {"event": "frontier_program", "step": 1, "at": 1, "op": "TAP"},
                      {"event": "frontier_program_handover", "at": 2, "steps": 5,
                       "why": "Its step 3 (tap Inbox) didn't fit this screen: no Inbox button"},
                      {"event": "frontier_program_handover", "at": 3, "steps": 5,
                       "why": "Its step 4 is one the person asked Mobster to choose each run", "decide": True}):
            self.listener.on_event(self.run, event)
        uses = [(line["key"], line["ok"]) for line in self.lines()]
        self.assertEqual(uses, [("step 1", True), ("step 2", True), ("step 3", False)])

    def test_pack_steps_go_to_the_same_ledger_once(self):
        event = {"event": "app_pack", "pack": "chirp", "step": "dms", "ok": False, "confidence": .3, "version": "2.0"}
        health.record(self.dir, {k: v for k, v in event.items() if k != "event"})
        self.listener.on_event(self.run, event)  # packs write their own line: the listener only follows the drift
        uses = [line for line in self.lines() if line["kind"] == "use"]
        self.assertEqual(len(uses), 1)
        self.assertEqual((uses[0]["source"], uses[0]["key"]), ("pack", "dms"))
        self.assertEqual(health.recent(self.dir, "chirp")[0]["step"], "dms")


class HealTests(unittest.TestCase):
    def test_a_heal_is_navigation_on_the_screen_it_was_recorded_on(self):
        good = {"action": {"op": "TAP", "role": "Button", "label": "Inbox", "commit": None}}
        self.assertTrue(hints.check(good, [hints.pair_hash("Button", "Inbox")]))
        self.assertFalse(hints.check(good, ["deadbeef"]))
        commit = {"action": {"op": "TAP", "role": "Button", "label": "Send", "commit": "send"}}
        self.assertFalse(hints.check(commit, [hints.pair_hash("Button", "Send")]))
        typed = {"action": {"op": "TYPE", "role": "TextField", "label": "Message", "commit": None}}
        self.assertFalse(hints.check(typed, [hints.pair_hash("TextField", "Message")]))

class HealthTests(unittest.TestCase):
    def test_each_app_by_source(self):
        folder = Path(tempfile.mkdtemp())
        for ok in (True, True, True, True, False):
            hints.record(folder, {"kind": "use", "source": "pack", "app": APP, "key": "dms", "ok": ok, "version": "2"})
        for ok in (False, False):
            hints.record(folder, {"kind": "use", "source": "replay", "app": APP, "key": "step 1", "ok": ok,
                                  "version": "2"})
        hints.record(folder, {"kind": "drift", "source": "replay", "app": APP, "key": "step 1", "version": "2",
                              "variant": "s9", "why": "no button"})
        app, = hints.health(folder)
        self.assertEqual(app["app"], APP)
        self.assertEqual(app["sources"]["pack"]["state"], "healthy")
        self.assertEqual(app["sources"]["replay"]["state"], "stale")
        self.assertEqual(app["sources"]["replay"]["lastDrift"]["variant"], "s9")
        self.assertEqual(app["sources"]["pack"]["versions"], ["2"])
        self.assertEqual(hints.health(None), [])


if __name__ == "__main__":
    unittest.main()
