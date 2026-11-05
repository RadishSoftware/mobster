"""Traces (traces.py): a run records a tape while it runs, `mobster export ID --trace` packs it into a bundle, and no
secret rides along. Offline: scripted phones and models, temporary folders and journals."""

import argparse
import gzip
import io
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from mobile_agent import cli_parser, harness_api, run_export, traces
from mobile_agent.agent_hooks import MASK
from mobile_agent.bench import tape
from mobile_agent.frontier import FrontierAgent
from mobile_agent.journal import Journal
from mobile_agent.state import Element
from mobile_agent.tests.test_frontier import ARCHIVE, Driver, Script, screen
from mobile_agent.tests.test_sota_e2e import Service
from mobile_agent.wda.frame_store import FrameStore

FRONTIER = {"screenshots": False, "settle_seconds": 0}
RUN_ID = "0123456789ab"
# Values the secret filter must catch wherever the run shows or says them.
KEY = "sk-ant-fake3-ZXCVBNMASDFGHJKL1234"
TOKEN = "ghp_" + "abcdefghijklmnopqrstuvwx1234"
PASSWORD = "hunter2xyz"
CARD = "4111 1111 1111 1111"
CODE = "551203"
BEARER = "abc123DEF456ghi789jkl"
PLANTED = (KEY, TOKEN, PASSWORD, CARD, CODE, BEARER)


class Thinking(Script):
    """A scripted model whose thought repeats a secret it read on the screen."""

    def complete(self, messages, schema, timeout=60):
        reply, usage = super().complete(messages, schema, timeout)
        if "actions" in reply:
            reply["thought"] = f"The page shows Bearer {BEARER} and the password: {PASSWORD}."
        return reply, usage


def archive_screens(with_secrets=False):
    delete = Element("1", "Delete", "Button", (.1, .1, .2, .05))
    archive = Element("2", "Archive", "Button", (.4, .1, .2, .05))
    extra = [Element("4", f"API key {KEY}", "StaticText", (.1, .3, .8, .05)),
             Element("5", "Card", "StaticText", (.1, .4, .8, .05), value=f"Visa {CARD}"),
             Element("6", f"Your code is {CODE}", "StaticText", (.1, .5, .8, .05)),
             Element("7", f"token {TOKEN} password: {PASSWORD}", "StaticText", (.1, .6, .8, .05))] \
        if with_secrets else []
    row = screen(delete, archive, *extra)
    done = screen(Element("3", "Archived", "StaticText", (.4, .1, .2, .05)))
    return [row, row, done, done, done]


def record(test, folder, *, with_secrets=False, frames=None, env=None):
    """Run FrontierAgent on a scripted phone with traces on; (outcome, recording, events)."""
    frames = frames if frames is not None else FrameStore(Path(folder) / "frames")
    test.addCleanup(frames.close)
    recording = traces.Recording(frames, RUN_ID, ARCHIVE)
    model = (Thinking if with_secrets else Script)([("TAP", "Delete", None), ("TAP", "Archive", None),
                                                    ("DONE", None, None), ("DONE", None, None)])
    with mock.patch.dict(os.environ, {traces.ENV_FLAG: "1", **(env or {})}):
        driver, client = recording.attach(Driver(archive_screens(with_secrets)), model, apps={}, frontier=FRONTIER)
    events = []
    outcome = FrontierAgent(driver, client, apps={}, emit=events.append, **FRONTIER).run(ARCHIVE)
    recording.finish(outcome)
    return outcome, recording, events


def members(data):
    """{name: text} for every member of a bundle, a gzip member unpacked."""
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            raw = archive.read(name)
            out[name] = gzip.decompress(raw).decode("utf-8") if name.endswith(".gz") else raw.decode("utf-8", "replace")
    return out


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))

    def tape_path(self):
        return self.folder / "frames" / RUN_ID / "tape.jsonl.gz"

    def test_a_run_records_a_private_tape_with_no_secret_in_it(self):
        outcome, _, _ = record(self, self.folder, with_secrets=True)
        self.assertEqual(outcome["status"], "completed")
        path = self.tape_path()
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertFalse((path.parent / "tape.jsonl").exists())
        text = gzip.decompress(path.read_bytes()).decode("utf-8")
        for value in PLANTED:
            self.assertNotIn(value, text)
        self.assertIn(MASK, text)

    def test_the_tape_is_masked_as_it_is_written_not_when_it_is_packed(self):
        scrubbed = []

        def scrub(entry):
            scrubbed.append(entry["kind"])
            return traces.mask(entry)

        written = self.folder / "t" / "tape.jsonl"
        recorder = tape.Tape(written, scrub=scrub)
        recorder.write({"kind": "model", "reply": {"thought": f"password: {PASSWORD}"}})
        recorder.close()
        self.assertEqual(scrubbed, ["model"])
        self.assertNotIn(PASSWORD, written.read_text())

    def test_recording_costs_the_agent_no_masking_time(self):
        # The secret filter takes tens of milliseconds a screen: it runs on the tape's own thread.
        threads = []

        def scrub(entry):
            threads.append(__import__("threading").current_thread().name)
            return entry

        recorder = tape.Tape(self.folder / "t.jsonl", scrub=scrub)
        recorder.write({"kind": "value", "name": "x", "value": 1})
        recorder.close()
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0].startswith("mobster-tape"), threads)

    def test_the_tape_in_a_bundle_replays_the_run(self):
        outcome, _, _ = record(self, self.folder)
        folder = self.folder / "frames" / RUN_ID
        data, manifest = traces.bundle({"id": RUN_ID, "goal": ARCHIVE, "events": [], "summary": {}}, folder)
        self.assertTrue(manifest["tape"]["included"])
        unpacked = self.folder / "unpacked" / "tape.jsonl.gz"
        unpacked.parent.mkdir()
        unpacked.write_bytes(zipfile.ZipFile(io.BytesIO(data)).read("tape.jsonl.gz"))
        report = tape.replay(unpacked)
        self.assertTrue(report.identical, report.public())
        self.assertEqual(report.recorded_status, outcome["status"])

    def test_nothing_is_recorded_unless_traces_are_on(self):
        phone, model = Driver(archive_screens()), Script([("DONE", None, None)])
        recording = traces.Recording(FrameStore(self.folder / "frames"), RUN_ID, ARCHIVE)
        self.addCleanup(recording.frames.close)
        with mock.patch.dict(os.environ, {traces.ENV_FLAG: ""}):
            pair = recording.attach(phone, model, apps={}, frontier=FRONTIER)
        self.assertEqual(pair, (phone, model))
        self.assertFalse((self.folder / "frames").exists())
        recording.finish({"status": "completed"})

    def test_a_run_with_nowhere_to_keep_a_tape_records_none(self):
        phone, model = Driver(archive_screens()), Script([("DONE", None, None)])
        with mock.patch.dict(os.environ, {traces.ENV_FLAG: "1"}):
            for frames, run_id in ((None, RUN_ID), (FrameStore(None), RUN_ID),
                                   (FrameStore(self.folder / "frames"), "../escape")):
                with self.subTest(frames=frames, run_id=run_id):
                    pair = traces.Recording(frames, run_id, ARCHIVE).attach(phone, model, apps={}, frontier=FRONTIER)
                    self.assertEqual(pair, (phone, model))
                    if frames is not None:
                        frames.close()
        self.assertFalse((self.folder / "frames").exists())

    def test_a_tape_past_its_size_is_dropped_whole(self):
        with mock.patch.object(tape.Tape, "MAX_BYTES", 2000):
            record(self, self.folder)
        self.assertEqual(list((self.folder / "frames" / RUN_ID).glob("tape.jsonl*")), [])

    def test_finishing_twice_ends_the_tape_once(self):
        outcome, recording, _ = record(self, self.folder)
        self.assertIsNone(recording.tape)
        recording.finish()  # a second finish, as the run's finally does after the normal one, changes nothing
        end = [e for e in tape.load(self.tape_path()) if e["kind"] == "end"]
        self.assertEqual([e["status"] for e in end], [outcome["status"]])

    def test_frames_keep_a_runs_files_in_a_folder_only_for_a_real_run_id(self):
        store = FrameStore(self.folder / "frames")
        self.addCleanup(store.close)
        self.assertEqual(store.folder(RUN_ID), self.folder / "frames" / RUN_ID)
        for bad in (None, "", "../x", "0123456789AB", "0123"):
            self.assertIsNone(store.folder(bad), bad)
        self.assertIsNone(FrameStore(None).folder(RUN_ID))


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))

    def row(self):
        planted = (f"The page shows Bearer {BEARER}. Card {CARD}, code {CODE}, token {TOKEN}, "
                   f"key {KEY}, password: {PASSWORD}")
        events = [{"event": "run_started", "seq": 0, "timestamp": 1},
                  {"event": "frontier_decision", "seq": 1, "timestamp": 2, "step": 1, "thought": planted,
                   "target_label": planted, "latency_ms": 1800, "frameId": "f0123456789"},
                  {"event": "frontier_action", "seq": 2, "timestamp": 3, "step": 1, "operation": "TAP",
                   "target_label": f"Archive {planted}", "changed": True, "exec_ms": 400, "settle_ms": 900},
                  {"event": "result", "seq": 3, "timestamp": 4, "status": "completed", "answer": planted,
                   "data": {"rows": [planted] * 20}, "blocks": {"blocks": [{"type": "table", "rows": [planted]}]},
                   "costUsd": 0.04}]
        return {"id": RUN_ID, "goal": f"Archive it. My password is {PASSWORD}", "app": {"bundleId": "com.example.mail"},
                "appName": "Mail", "status": "completed", "createdAt": 1000, "finishedAt": 5000, "costUsd": 0.04,
                "summary": {"status": "completed", "answer": planted, "data": {"rows": [planted]},
                            "blocks": {"blocks": [planted]}, "costUsd": 0.04, "model": "claude-sonnet-5-5"},
                "events": events}

    def test_no_planted_secret_is_in_any_member_of_a_bundle(self):
        record(self, self.folder, with_secrets=True)
        shots = {"f0123456789": b"\xff\xd8 screenshot"}
        data, manifest = traces.bundle(self.row(), self.folder / "frames" / RUN_ID, read_frame=shots.get)
        every = members(data)
        self.assertEqual(set(every), {"manifest.json", "events.jsonl", "tape.jsonl.gz", "screens/f0123456789.jpg"})
        for name, text in every.items():
            if name.startswith("screens/"):
                continue  # a picture can't be masked: it is only there when asked for
            for value in PLANTED:
                self.assertNotIn(value, text, f"{value!r} in {name}")
        self.assertIn(MASK, every["events.jsonl"])
        self.assertEqual(manifest["screens"], {"included": True, "count": 1})

    def test_a_bundle_masks_a_tape_written_before_the_filter_widened(self):
        record(self, self.folder)
        path = self.folder / "frames" / RUN_ID / "tape.jsonl.gz"
        old = gzip.decompress(path.read_bytes()).decode("utf-8").replace("Archive", f"Archive {KEY}", 1)
        path.write_bytes(gzip.compress(old.encode("utf-8")))
        data, _ = traces.bundle(self.row(), self.folder / "frames" / RUN_ID)
        self.assertNotIn(KEY, members(data)["tape.jsonl.gz"])

    def test_a_bundle_has_the_facts_and_leaves_out_the_answer_data_and_the_screens_by_default(self):
        data, manifest = traces.bundle(self.row(), None)
        every = members(data)
        self.assertEqual(set(every), {"manifest.json", "events.jsonl"})
        self.assertEqual(manifest["screens"], {"included": False})
        self.assertFalse(manifest["tape"]["included"])
        self.assertIn(traces.ENV_FLAG, manifest["tape"]["why"])
        summary = manifest["run"]["summary"]
        self.assertEqual((summary["status"], summary["costUsd"], summary["model"]),
                         ("completed", 0.04, "claude-sonnet-5-5"))
        self.assertNotIn("data", summary)
        self.assertNotIn("blocks", summary)
        result = json.loads(every["events.jsonl"].splitlines()[-1])
        self.assertNotIn("data", result)
        self.assertNotIn("blocks", result)
        self.assertEqual(manifest["run"]["app"], "com.example.mail")
        decision = json.loads(every["events.jsonl"].splitlines()[1])
        self.assertEqual((decision["latency_ms"], decision["step"]), (1800, 1))

    def test_an_unreadable_tape_is_left_out_and_said(self):
        folder = self.folder / "frames" / RUN_ID
        folder.mkdir(parents=True)
        (folder / "tape.jsonl.gz").write_bytes(b"not gzip")
        data, manifest = traces.bundle(self.row(), folder)
        self.assertEqual(set(members(data)), {"manifest.json", "events.jsonl"})
        self.assertIn("could not be read", manifest["tape"]["why"])

    def test_mask_keeps_the_shape_and_drops_blobs(self):
        value = {"a": [f"password: {PASSWORD}", 3, None, True], f"key {KEY}": {"b": "x" * 5000}}
        masked = traces.mask(value)
        self.assertEqual(masked["a"][1:], [3, None, True])
        self.assertNotIn(PASSWORD, json.dumps(masked))
        self.assertNotIn(KEY, json.dumps(masked))
        self.assertEqual(list(masked.values())[1]["b"], "[5000 characters left out]")
        self.assertEqual(traces.mask(traces.mask(value)), masked)  # idempotent, like the filter
        deep = current = {}
        for _ in range(80):
            current["x"] = {}
            current = current["x"]
        self.assertLess(len(json.dumps(traces.mask(deep))), 2000)

    def test_save_never_replaces_a_file_and_the_file_is_private(self):
        first = traces.save(b"one", self.folder, "mobster-trace-x")
        second = traces.save(b"two", self.folder, "mobster-trace-x")
        self.assertEqual((first.name, second.name), ("mobster-trace-x.zip", "mobster-trace-x (1).zip"))
        self.assertEqual(first.read_bytes(), b"one")
        self.assertEqual(stat.S_IMODE(second.stat().st_mode), 0o600)


class ExportTraceTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))
        state = self.folder / "state"
        self.journal_path = state / "mobster.sqlite3"
        meta = {"id": RUN_ID, "app": {"bundleId": "com.example.mail"}, "appName": "Mail", "goal": ARCHIVE,
                "status": "running", "createdAt": 1000, "finishedAt": None, "summary": None}
        journal = Journal(self.journal_path)
        journal.create(meta)
        journal.append(meta, {"event": "frontier_decision", "seq": 0, "timestamp": 1, "step": 1,
                              "thought": f"password: {PASSWORD}", "frameId": "f0123456789"})
        self.meta = {**meta, "status": "stopped", "finishedAt": 5000, "summary": {"status": "stopped"}}
        journal.finish(self.meta, {"event": "run_finished", "status": "stopped", "seq": 1, "timestamp": 2,
                                   "summary": {"status": "stopped", "answer": "x", "data": [PASSWORD]}})
        journal.close()
        run_folder = state / "frames" / RUN_ID
        run_folder.mkdir(parents=True)
        (run_folder / "f0123456789.jpg").write_bytes(b"\xff\xd8 jpeg")
        record(self, state.parent, frames=FrameStore(state / "frames"))
        patcher = mock.patch("mobile_agent.run_export.journals", return_value=[self.journal_path])
        patcher.start()
        self.addCleanup(patcher.stop)

    def export(self, **overrides):
        values = {"run_id": "latest", "gif": False, "mp4": False, "trace": True, "screens": False, "redact": False,
                  "theme": "paper", "output": self.folder / "out", "json": False}
        values.update(overrides)
        out, err = io.StringIO(), io.StringIO()
        code = run_export.export(argparse.Namespace(**values), out, err)
        return code, out.getvalue(), err.getvalue()

    def test_a_finished_run_is_saved_as_a_bundle_with_its_tape(self):
        (self.folder / "out").mkdir()
        code, out, err = self.export(json=True)
        self.assertEqual(code, 0, err)
        saved = json.loads(out)["trace"]
        self.assertEqual(Path(saved["path"]).name, f"mobster-trace-{RUN_ID}.zip")
        self.assertTrue(saved["tape"]["included"])
        every = members(Path(saved["path"]).read_bytes())
        self.assertEqual(set(every), {"manifest.json", "events.jsonl", "tape.jsonl.gz"})
        self.assertNotIn(PASSWORD, "".join(every.values()))

    def test_the_screenshots_come_only_with_screens_and_the_words_say_they_cant_be_masked(self):
        (self.folder / "out").mkdir()
        code, out, _ = self.export(screens=True)
        self.assertEqual(code, 0)
        self.assertIn("which can't be masked", out)
        saved = next((self.folder / "out").glob("*.zip"))
        self.assertIn("screens/f0123456789.jpg", zipfile.ZipFile(saved).namelist())
        code, out, _ = self.export()
        self.assertEqual(code, 0)
        self.assertNotIn("screenshots", out)

    def test_a_named_file_is_written_there_and_a_folder_never_loses_a_file(self):
        target = self.folder / "mine" / "bug.zip"
        self.assertEqual(self.export(output=target)[0], 0)
        self.assertTrue(zipfile.is_zipfile(target))
        (self.folder / "out").mkdir()
        self.export()
        self.export()
        self.assertEqual(sorted(p.name for p in (self.folder / "out").glob("*.zip")),
                         [f"mobster-trace-{RUN_ID} (1).zip", f"mobster-trace-{RUN_ID}.zip"])

    def test_what_it_refuses(self):
        code, _, err = self.export(run_id="ffff")
        self.assertEqual(code, 1)
        self.assertIn("No task ffff in your history", err)
        self.assertEqual(self.export(gif=True)[0], 2)
        self.assertEqual(self.export(trace=False, screens=True)[0], 2)
        self.assertEqual(self.export(trace=False)[0], 2)

    def test_the_options_stay_out_of_the_help_until_traces_are_on(self):
        def help_of(env):
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"COLUMNS": "100", "NO_COLOR": "1", traces.ENV_FLAG: env}), \
                    mock.patch.object(harness_api.hooks(), "cli", []), \
                    mock.patch("sys.stdout", out), mock.patch("argparse.ArgumentParser.exit", side_effect=SystemExit):
                try:
                    cli_parser.build_parser().parse_args(["export", "--help"])
                except SystemExit:
                    pass
            return out.getvalue()

        self.assertNotIn("--trace", help_of(""))
        self.assertNotIn("--screens", help_of(""))
        self.assertIn("--trace", help_of("1"))
        self.assertIn("--screens", help_of("1"))
        with mock.patch.object(harness_api.hooks(), "cli", []):
            args = cli_parser.build_parser().parse_args(["export", "0123", "--trace", "--screens"])
        self.assertEqual((args.trace, args.screens), (True, True))


class ServiceTraceTests(Service):
    """The real service, runtime and frontier loop over the scripted phone (test_sota_e2e): a task records its tape
    when traces are on, `mobster export --trace` can pack it, and a task with traces off leaves none."""

    ASK = "What's the address of the dinner place Alex sent me in Messages?"

    def frames_of(self, run_id):
        return self.state_db.parent / "frames" / run_id

    def test_a_task_records_a_tape_that_replays_and_names_what_the_live_run_had(self):
        env = mock.patch.dict(os.environ, {traces.ENV_FLAG: "1"})
        env.start()
        self.addCleanup(env.stop)
        _, run_id = self.start(self.ASK)
        run = self.finished(run_id)
        self.assertIn("214 Pine St", run["summary"]["answer"])
        path = self.frames_of(run_id) / "tape.jsonl.gz"
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        header = tape.load(path)[0]
        self.assertEqual(header["request"], self.ASK)
        self.assertTrue({"guard", "approve", "steering"} <= set(header["hooks"]), header["hooks"])
        report = tape.replay(path)
        self.assertTrue(report.identical, report.public())
        self.assertGreaterEqual(report.recorded_acts, 2)
        self.assertEqual(report.recorded_status, run["summary"]["status"])

    def test_a_task_records_nothing_with_traces_off(self):
        _, run_id = self.start(self.ASK)
        self.finished(run_id)
        folder = self.frames_of(run_id)
        self.assertEqual(list(folder.glob("tape.jsonl*")) if folder.exists() else [], [])

    def test_a_deleted_task_takes_its_tape_with_it(self):
        env = mock.patch.dict(os.environ, {traces.ENV_FLAG: "1"})
        env.start()
        self.addCleanup(env.stop)
        _, run_id = self.start(self.ASK)
        self.finished(run_id)
        self.assertTrue((self.frames_of(run_id) / "tape.jsonl.gz").is_file())
        self.runtime.frames.delete(run_id)
        self.assertFalse(self.frames_of(run_id).exists())


if __name__ == "__main__":
    unittest.main()
