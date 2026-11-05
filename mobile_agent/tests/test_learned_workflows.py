import json
import os
import re
import unittest
from unittest import mock

from mobile_agent.learned.workflows import match_template, template_from
from mobile_agent.tests.sota_world import decide as ORIGINAL_DECIDE
from mobile_agent.tests.test_sota_e2e import Service

NOTE = "Luca, 214 Pine St (dinner Friday 7:30)"


def note_rule(t):
    from mobile_agent.tests import sota_world as world
    found = re.match(r"save a new note: (.+)$", t.r)
    if not found:
        return ORIGINAL_DECIDE(t)
    body = t.request.split(": ", 2)[-1].strip()
    if not t.in_app(world.NOTES):
        return [world.launch(world.NOTES)], None, []
    if t.has("New Note"):
        if any(label.startswith(body[:60]) for label in t.labels()):
            return world.done(f"Saved a new note: “{body}”.")
        return [world.act("TAP", t.find("New Note"))], None, []
    note = t.find("Note", "TextView")
    if note and body not in t.prompt.split("Screen elements:", 1)[1]:
        return [world.act("TYPE", note, body)], None, []
    return [world.act("TAP", t.find("Done"))], None, []


class LearnedBase(Service):
    def setUp(self):
        super().setUp()
        from mobile_agent.tests import sota_world as world
        patcher = mock.patch.object(world, "decide", note_rule)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, goal, app):
        status, data = self.call("POST", "/api/workflows", {"name": "Saved", "appId": app, "goal": goal})
        self.assertEqual(status, 201, data)
        return data["workflow"]["id"]

    def start(self, workflow):
        status, data = self.call("POST", f"/api/workflows/{workflow}/run", {})
        self.assertIn(status, (200, 201), data)
        return data["run"]["id"]

    def finished(self, run_id, timeout=20):
        run = super().finished(run_id)

        def free():
            with self.runtime.lock:
                return run_id not in self.runtime.active_runs
        self.until(free, f"task {run_id} to free the phone", timeout)
        return run

    def learned(self):
        status, data = self.call("GET", "/api/replay/workflows")
        self.assertEqual(status, 200, data)
        return data["learned"]

    def replay_events(self, run):
        return [e for e in run["events"] if str(e.get("event", "")).startswith("replay_")]


class LearnedWorkflowTests(LearnedBase):
    def test_a_learned_workflow_runs_again_with_no_model(self):
        workflow = self.save(f"Save a new note: {NOTE}", "notes")
        first = self.finished(self.start(workflow))
        self.assertEqual(first["summary"]["outcome"], "done", first["summary"])
        calls = len(self.calls)
        self.assertGreater(calls, 0)
        learned = self.until(lambda: self.learned().get(workflow), "the workflow to be learned")
        self.assertEqual((learned["steps"], learned["replays"]), (3, 0))
        second = self.finished(self.start(workflow))
        self.assertEqual(len(self.calls), calls, "a learned workflow calls no model")
        self.assertEqual(second["summary"]["outcome"], "done")
        self.assertIn(second["summary"]["costUsd"], (None, 0, 0.0))
        self.assertEqual((second["summary"]["replay"]["mode"], second["summary"]["replay"]["costUsd"]), ("replayed", 0))
        self.assertEqual(self.phone["notes"][:2], [NOTE[:60], NOTE[:60]])
        self.assertEqual(self.learned()[workflow]["last"]["costUsd"], 0.0)
        took = second["summary"]["replay"]["first"]
        self.assertEqual(took["runId"], first["id"])
        self.assertEqual(took["seconds"], round(first["summary"]["elapsed_ms"] / 1000, 2))
        self.assertEqual(took["costUsd"], first["summary"]["costUsd"])
        self.assertEqual(self.learned()[workflow]["first"]["runId"], first["id"])
        steps = [e["text"] for e in second["events"] if e.get("event") == "step"]
        self.assertEqual(len(steps), 3, steps)
        self.assertTrue(steps[1].startswith("Typed"), steps)
        other = self.save("Save a new note: Trip ideas for May", "notes")
        self.assertIn(other, self.learned())
        self.finished(self.start(other))
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.phone["notes"][0], "Trip ideas for May")

    def test_a_send_still_asks_and_a_no_sends_nothing(self):
        workflow = self.save("Text Alex: Running 10 minutes late, sorry!", "messages")
        run = self.start(workflow)
        approval = self.asked(run, "commit")
        self.call("POST", f"/api/runs/{run}/approval", {"id": approval["id"], "approve": True}, origin=None)
        self.assertEqual(self.finished(run)["summary"]["outcome"], "done")
        self.until(lambda: self.learned().get(workflow), "the workflow to be learned")
        calls = len(self.calls)
        for approve, sent in ((False, 1), (True, 2)):
            run = self.start(workflow)
            approval = self.asked(run, "commit")
            self.assertEqual(approval.get("act"), "send_message", approval)
            self.call("POST", f"/api/runs/{run}/approval", {"id": approval["id"], "approve": approve}, origin=None)
            result = self.finished(run)["summary"]
            self.assertEqual(result["outcome"], "done" if approve else "declined", result)
            self.assertEqual(len(self.phone["sent"]["Alex Rivera"]), sent)
        self.assertEqual(len(self.calls), calls, "the replayed send asked a person, not a model")

    def test_a_screen_that_changed_hands_over_to_the_model_and_relearns(self):
        workflow = self.save(f"Save a new note: {NOTE}", "notes")
        original = self.finished(self.start(workflow))
        self.until(lambda: self.learned().get(workflow), "the workflow to be learned")
        calls = len(self.calls)
        from mobile_agent.learned import workflows as learned
        folder = learned.learned_dir(self.state_db.parent)
        file = next(folder.glob("*.json"))
        data = json.loads(file.read_text())
        data["steps"][0]["target"]["label"] = "Compose"  # as if the app renamed New Note
        file.write_text(json.dumps(data))
        self.phone["notes"] = ["Groceries"]  # so the model has a note to write
        run = self.finished(self.start(workflow))
        self.assertEqual(run["summary"]["outcome"], "done", run["summary"])
        self.assertGreater(len(self.calls), calls, "the model took over at the changed step")
        handoff = [e for e in run["events"] if e.get("event") == "replay_handoff"]
        self.assertEqual(handoff[0]["step"], 1)
        relearned = self.until(lambda: (self.learned().get(workflow) or {}).get("relearnedAt"), "a relearn")
        self.assertTrue(relearned)
        self.assertEqual(self.learned()[workflow]["first"]["runId"], original["id"])

    def test_runs_from_the_composer_are_never_learned(self):
        status, data = self.call("POST", "/api/runs", {"appId": "notes", "goal": f"Save a new note: {NOTE}",
                                                      "mode": "live"})
        self.assertIn(status, (200, 201), data)
        self.finished(data["run"]["id"])
        from mobile_agent.learned import workflows as learned
        folder = learned.learned_dir(self.state_db.parent)
        self.assertFalse(folder.exists() and any(folder.iterdir()))

    def test_replay_off_learns_nothing(self):
        with mock.patch.dict(os.environ, {"MOBSTER_REPLAY": "off"}):
            workflow = self.save(f"Save a new note: {NOTE}", "notes")
            self.finished(self.start(workflow))
            calls = len(self.calls)
            self.finished(self.start(workflow))
            self.assertGreater(len(self.calls), calls)
            self.assertEqual(self.learned(), {})


class ReplayConfirmTests(LearnedBase):

    def no_proof(self):
        from mobile_agent.learned import workflows as learned
        from mobile_agent.tests import sota_world as world
        written = world.contract_items

        def items(prompt):
            found = re.search(r"^Request: .*?Save a new note: (.*)$", prompt, re.M)
            if not found:
                return written(prompt)
            return [{"kind": "WRITE", "app": "Notes", "what": "a new note", "payload": found.group(1).strip(),
                     "act": "none", "count": "", "condition": "", "quote": ""}]
        for patcher in (mock.patch.object(learned, "proof_template", lambda proof, values: []),
                        mock.patch.object(world, "contract_items", items)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def path_file(self):
        from mobile_agent.learned import workflows as learned
        return next(learned.learned_dir(self.state_db.parent).glob("*.json"))

    def test_a_three_row_loop_runs_every_round_after_learning_the_first(self):
        self.no_proof()
        status, data = self.call("POST", "/api/workflows", {
            "name": "QA loop notes", "steps": [{"id": "s1", "kind": "task", "appId": "notes",
                                                "text": "Save a new note: Mobster QA loop {item}"}],
            "repeat": {"kind": "each", "rows": [{"item": "A"}, {"item": "B"}, {"item": "C"}]}})
        self.assertEqual(status, 201, data)
        workflow = data["workflow"]["id"]
        self.start(workflow)

        def ended():
            batches = self.call("GET", f"/api/workflows/{workflow}/runs")[1]["batches"]
            return batches[0] if batches and batches[0]["status"] != "running" else None
        batch = self.until(ended, "the loop to end", timeout=40)
        self.assertEqual(batch["status"], "completed", batch)
        self.assertEqual([r["status"] for r in batch["rounds"]], ["completed"] * 3, batch["rounds"])
        self.assertEqual(self.phone["notes"][:3], ["Mobster QA loop C", "Mobster QA loop B", "Mobster QA loop A"])
        runs = [self.run_of(r["runIds"][0]) for r in batch["rounds"]]
        self.assertNotIn("replay", runs[0]["summary"])
        for run in runs[1:]:
            summary = run["summary"]
            self.assertEqual((summary["replay"]["mode"], summary["outcome"]), ("replayed", "done"), summary)
            self.assertEqual(summary["replay"]["confirmed"], "screen")
            self.assertIn(summary["costUsd"], (None, 0, 0.0))
        self.assertEqual(json.loads(self.path_file().read_text())["replays"], 2)
        self.assertEqual(len([call for call in self.calls if call.get("look")]), 0, "the code check needed no look")

    def test_another_final_screen_is_not_confirmed_by_code(self):
        self.no_proof()
        workflow = self.save(f"Save a new note: {NOTE}", "notes")
        self.finished(self.start(workflow))
        self.until(lambda: self.learned().get(workflow), "the workflow to be learned")
        file = self.path_file()
        data = json.loads(file.read_text())
        self.assertTrue(data["final"]["shape"], "the learned run's last screen is kept, as hashes")
        data["final"]["shape"] = ["0000000" + str(n) for n in range(8)]  # as if the run had ended elsewhere
        data["final"]["hash"] = "0" * 16
        file.write_text(json.dumps(data))
        from mobile_agent.learned import workflows as learned
        with mock.patch.object(learned, "look_client", lambda ctx: None):  # no model to look: fails closed
            run = self.finished(self.start(workflow))
        self.assertEqual(run["summary"]["outcome"], "check", run["summary"])
        self.assertEqual(run["summary"]["status"], "completion_not_confirmed")

    def test_with_no_last_screen_kept_one_look_confirms_and_the_next_replay_needs_none(self):
        self.no_proof()
        workflow = self.save(f"Save a new note: {NOTE}", "notes")
        self.finished(self.start(workflow))
        self.until(lambda: self.learned().get(workflow), "the workflow to be learned")
        file = self.path_file()
        data = json.loads(file.read_text())
        data["final"] = None
        file.write_text(json.dumps(data))
        looks = lambda: [call for call in self.calls if call.get("look")]  # noqa: E731
        decisions = len(self.calls)
        run = self.finished(self.start(workflow))
        self.assertEqual((run["summary"]["outcome"], run["summary"]["replay"]["confirmed"]), ("done", "look"),
                         run["summary"])
        self.assertEqual(len(looks()), 1, "one look-only call, no decisions")
        self.assertEqual(len(self.calls), decisions + 1)
        self.assertEqual(run["summary"]["proof"][0]["quote"], NOTE)
        self.assertTrue(json.loads(file.read_text())["final"]["shape"], "the screen it saw is kept")
        again = self.finished(self.start(workflow))
        self.assertEqual((again["summary"]["outcome"], again["summary"]["replay"]["confirmed"]), ("done", "screen"))
        self.assertEqual(len(self.calls), decisions + 1, "the next replay confirms by code")

    def test_a_look_that_doesnt_see_it_done_is_not_confirmed(self):
        self.no_proof()
        workflow = self.save(f"Save a new note: {NOTE}", "notes")
        self.finished(self.start(workflow))
        self.until(lambda: self.learned().get(workflow), "the workflow to be learned")
        file = self.path_file()
        data = json.loads(file.read_text())
        data["final"] = None
        file.write_text(json.dumps(data))
        from mobile_agent.tests import sota_world as world
        model = world.ScriptedModel.complete

        def blind(client, messages, schema, **kwargs):
            if set(schema.get("properties", {})) == {"finished", "seen"}:
                return {"finished": True, "seen": "Saved, trust me"}, {}  # words that aren't on the screen
            return model(client, messages, schema, **kwargs)
        with mock.patch.object(world.ScriptedModel, "complete", blind):
            run = self.finished(self.start(workflow))
        self.assertEqual((run["summary"]["outcome"], run["summary"]["status"]), ("check", "completion_not_confirmed"))
        self.assertIsNone(json.loads(file.read_text())["final"], "nothing unconfirmed is kept as the last screen")


class LearnedSafetyTests(LearnedBase):
    def learn_send(self):
        workflow = self.save("Text Alex: Running 10 minutes late, sorry!", "messages")
        run = self.start(workflow)
        approval = self.asked(run, "commit")
        self.call("POST", f"/api/runs/{run}/approval", {"id": approval["id"], "approve": True}, origin=None)
        self.finished(run)
        self.until(lambda: self.learned().get(workflow), "the workflow to be learned")
        from mobile_agent.learned import workflows as learned
        file = next(learned.learned_dir(self.state_db.parent).glob("*.json"))
        return workflow, file

    def test_after_a_replayed_send_the_model_never_takes_over(self):
        workflow, file = self.learn_send()
        data = json.loads(file.read_text())
        after = dict(data["steps"][0], target={"label": "Info", "role": "Button", "id": None, "frame": None})
        data["steps"].append(after)  # a step after the send that no screen has
        file.write_text(json.dumps(data))
        calls = len(self.calls)
        run = self.start(workflow)
        approval = self.asked(run, "commit")
        self.call("POST", f"/api/runs/{run}/approval", {"id": approval["id"], "approve": True}, origin=None)
        result = self.finished(run)["summary"]
        self.assertEqual(result["outcome"], "check", result)
        self.assertIn("nothing is done twice", result["reason"])
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(len(self.phone["sent"]["Alex Rivera"]), 2, "sent once by the learned run, once now")

    def test_a_dynamically_classified_send_never_hands_later_drift_to_the_model(self):
        workflow, file = self.learn_send()
        data = json.loads(file.read_text())
        send = next(step for step in data["steps"] if (step.get("target") or {}).get("label") == "Send")
        send["commit"] = None  # older saved paths may lack the act today's gate recognizes
        data["steps"].append({**send, "target": {"label": "Info", "role": "Button", "id": None,
                                                   "frame": None}})
        file.write_text(json.dumps(data))
        calls = len(self.calls)
        run = self.start(workflow)
        approval = self.asked(run, "commit")
        self.assertEqual(approval.get("act"), "send_message")
        self.call("POST", f"/api/runs/{run}/approval", {"id": approval["id"], "approve": True}, origin=None)
        result = self.finished(run)["summary"]
        self.assertEqual(result["outcome"], "check", result)
        self.assertIn("nothing is done twice", result["reason"])
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(len(self.phone["sent"]["Alex Rivera"]), 2)

    def break_phone(self, *, after, on="observe"):
        from mobile_agent.tests import sota_world as world
        original, failed = getattr(world.ScriptedPhone, on), []

        def broken(phone, *args, **kwargs):
            if not failed and after(args):
                failed.append(True)  # once: the phone answers again after the timeout
                raise OSError("WebDriverAgent timed out")
            return original(phone, *args, **kwargs)
        patcher = mock.patch.object(world.ScriptedPhone, on, broken)
        patcher.start()
        self.addCleanup(patcher.stop)

    def replay_send_and_break(self, *, on):
        workflow, file = self.learn_send()
        before = file.read_text()
        sent = len(self.phone["sent"]["Alex Rivera"])
        calls = len(self.calls)
        if on == "observe":
            self.break_phone(after=lambda args: len(self.phone["sent"]["Alex Rivera"]) > sent)
        else:
            self.break_phone(on="execute", after=lambda args: args[0] == "TAP"
                             and getattr(args[1], "label", "") == "Send")
        run = self.start(workflow)
        approval = self.asked(run, "commit")
        self.call("POST", f"/api/runs/{run}/approval", {"id": approval["id"], "approve": True}, origin=None)
        result = self.finished(run)["summary"]
        self.assertEqual(result["status"], "completion_not_confirmed", result)
        self.assertIn("nothing is done twice", result["reason"])
        self.assertEqual(len(self.calls), calls, "the model never takes over after a send")
        self.assertEqual(json.loads(file.read_text())["steps"], json.loads(before)["steps"])

    def test_a_screen_read_that_fails_right_after_a_replayed_send_never_hands_to_the_model(self):
        self.replay_send_and_break(on="observe")

    def test_a_send_that_raises_is_unknown_and_never_handed_to_the_model(self):
        self.replay_send_and_break(on="execute")

    def test_a_screen_read_that_fails_after_a_plain_step_never_hands_to_the_model(self):
        workflow = self.save(f"Save a new note: {NOTE}", "notes")
        self.finished(self.start(workflow))
        self.until(lambda: self.learned().get(workflow), "the workflow to be learned")
        calls = len(self.calls)
        notes = list(self.phone["notes"])
        self.break_phone(after=lambda args: self.phone["app"] == "com.apple.mobilenotes"
                         and self.phone["stage"] == "editor"
                         and not self.phone["typed"])
        run = self.finished(self.start(workflow))
        self.assertEqual((run["summary"]["outcome"], run["summary"]["status"]),
                         ("check", "completion_not_confirmed"), run["summary"])
        self.assertNotIn("replay_handoff", [e["event"] for e in self.replay_events(run)])
        self.assertEqual(len(self.calls), calls, "the model cannot repeat a step whose outcome is unknown")
        self.assertEqual(self.learned()[workflow]["steps"], 3)
        self.assertEqual(self.phone["notes"], notes)

    def test_a_plain_tap_that_raises_after_dispatch_never_hands_to_the_model(self):
        from mobile_agent.tests import sota_world as world
        workflow = self.save(f"Save a new note: {NOTE}", "notes")
        self.finished(self.start(workflow))
        self.until(lambda: self.learned().get(workflow), "the workflow to be learned")
        calls = len(self.calls)
        sent = []
        execute = world.ScriptedPhone.execute

        def dispatch_then_fail(phone, operation, target, snapshot, text=None, timeout=10):
            result = execute(phone, operation, target, snapshot, text=text, timeout=timeout)
            if operation == "TAP" and getattr(target, "label", "") == "New Note":
                sent.append(operation)
                raise TimeoutError("the tap response was lost")
            return result

        with mock.patch.object(world.ScriptedPhone, "execute", dispatch_then_fail):
            run = self.finished(self.start(workflow))
        self.assertEqual((run["summary"]["outcome"], run["summary"]["status"]),
                         ("check", "completion_not_confirmed"), run["summary"])
        self.assertEqual(sent, ["TAP"])
        self.assertEqual(self.phone["stage"], "editor", "the tap landed")
        self.assertEqual(len(self.calls), calls)
        self.assertNotIn("replay_handoff", [e["event"] for e in self.replay_events(run)])

    def test_done_only_on_the_learned_runs_proof(self):
        workflow, file = self.learn_send()
        data = json.loads(file.read_text())
        self.assertEqual(data["proof"][0]["quote"], "{p1}", "the proof is the typed words, as a blank")
        data["proof"][0]["quote"] = "Delivered at noon"
        file.write_text(json.dumps(data))
        from mobile_agent.learned import workflows as learned
        patcher = mock.patch.object(learned, "look_client", lambda ctx: None)  # no model to look: fails closed
        patcher.start()
        self.addCleanup(patcher.stop)
        run = self.start(workflow)
        approval = self.asked(run, "commit")
        self.call("POST", f"/api/runs/{run}/approval", {"id": approval["id"], "approve": True}, origin=None)
        result = self.finished(run)["summary"]
        self.assertEqual((result["outcome"], result["proof"]), ("check", []))
        self.assertIn("didn't show what the learned run showed", result["reason"])


class FinalScreenTests(unittest.TestCase):

    @staticmethod
    def screen(text, app="com.apple.mobilenotes", extra=()):
        from mobile_agent.state import Element, Snapshot
        rows = [("Back", "Button"), ("Share", "Button"), ("More", "Button"), ("Notes", "StaticText"),
                (text, "TextView"), *extra]
        elements = [Element(id=str(n), label=label, role=role, rect=(0, n / 20, 1, .04))
                    for n, (label, role) in enumerate(rows)]
        return Snapshot(elements=elements, text="", width=390, height=844, source="wda", bundle_id=app)

    def test_this_rounds_words_on_the_learned_last_screen(self):
        from mobile_agent.learned.workflows import final_holds, final_of
        from mobile_agent.replay_core.path import screen_hashes as screen_of
        final = final_of(screen_of(self.screen("Mobster QA loop A")))
        self.assertEqual(final_holds(final, self.screen("Mobster QA loop B"), ["Mobster QA loop B"]), (True, ""))
        ok, why = final_holds(final, self.screen("Groceries"), ["Mobster QA loop B"])
        self.assertEqual((ok, why), (False, '"Mobster QA loop B" isn\'t on the screen'))
        ok, why = final_holds(final, self.screen("Mobster QA loop B", app="com.apple.MobileSMS"), ["Mobster QA loop B"])
        self.assertFalse(ok)
        self.assertIn("is in front", why)
        other = self.screen("Mobster QA loop B", extra=[(f"Row {n}", "Cell") for n in range(6)])
        ok, why = final_holds(final, other, ["Mobster QA loop B"])
        self.assertFalse(ok)
        self.assertIn("alike", why)
        self.assertFalse(final_holds(None, self.screen("Mobster QA loop B"), ["Mobster QA loop B"])[0])


class TemplateTests(unittest.TestCase):
    def test_typed_text_becomes_a_blank_only_when_the_workflow_says_it(self):
        steps = [{"op": "TYPE", "text": "running late", "live": False}, {"op": "TYPE", "text": "made up",
                                                                         "live": False}]
        template, out = template_from("Text Alex: running late", steps)
        self.assertEqual(template, "Text Alex: {p1}")
        self.assertEqual((out[0]["param"], out[0]["text"]), ("p1", None))
        self.assertTrue(out[1]["live"])
        self.assertEqual(match_template(template, "Text Alex: on my way"), {"p1": "on my way"})
        self.assertIsNone(match_template(template, "Text Sam: on my way"))
        self.assertIsNone(match_template("{p1}", "anything at all"), "a template needs words of its own")
        search = {"op": "TYPE", "text": "ramen near me", "live": False, "search": True}
        template, out = template_from("Find a ramen place open late", [search])
        self.assertEqual((template, out[0]["text"], out[0]["live"]), ("Find a ramen place open late", "ramen near me",
                                                                       False), "a search query replays as typed")
        template = "Text Alex: {p1}"
        self.assertEqual(match_template(template, "Text Alex: {message}"), {"p1": "{message}"})


if __name__ == "__main__":
    unittest.main()
