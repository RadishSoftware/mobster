import gzip
import io
import json
import re
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from mobile_agent import drivers, frontier
from mobile_agent.bench import tape
from mobile_agent.frontier import FrontierAgent
from mobile_agent.state import Element
from mobile_agent.tests import sota_world
from mobile_agent.tests.test_frontier import ARCHIVE, Driver, Script, screen

FRONTIER = {"screenshots": False, "settle_seconds": 0}


def record(folder, request, phone, model, apps=None, frontier_kwargs=FRONTIER):
    recorder = tape.Tape(Path(folder) / tape.NAME)
    recorder.header(request=request, apps=apps or {}, model="scripted", frontier=frontier_kwargs, driver=phone)
    agent = FrontierAgent(tape.TapeDriver(phone, recorder), tape.TapeClient(model, recorder), apps=apps or {},
                          emit=lambda event: None, **frontier_kwargs)
    outcome = agent.run(request)
    recorder.end(outcome)
    return outcome


class TapeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()

    def archive_run(self):
        row = screen(Element("1", "Delete", "Button", (.1, .1, .2, .05)),
                     Element("2", "Archive", "Button", (.4, .1, .2, .05)))
        done = screen(Element("3", "Archived", "StaticText", (.4, .1, .2, .05)))
        phone = Driver([row, row, done, done, done])
        model = Script([("TAP", "Delete", None), ("TAP", "Archive", None), ("DONE", None, None),
                        ("DONE", None, None)])
        return record(self.folder, ARCHIVE, phone, model), phone

    def test_a_recorded_run_replays_identically(self):
        outcome, phone = self.archive_run()
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(phone.actions, [("TAP", "Archive", None)])
        report = tape.replay(self.folder)
        self.assertIsNone(report.error)
        self.assertIsNone(report.divergence)
        self.assertTrue(report.identical, report.public())
        self.assertEqual((report.recorded_acts, report.replayed_acts), (1, 1))
        self.assertEqual(report.prompts_same, report.prompts_seen)

    def test_a_gate_that_now_allows_what_was_refused_diverges_at_that_turn(self):
        self.archive_run()
        with mock.patch.object(frontier.safety, "requested_by", lambda *args, **kwargs: True), \
                mock.patch.object(frontier.stages.guards, "guard", lambda *args, **kwargs: None):
            report = tape.replay(self.folder)
        self.assertFalse(report.identical)
        if report.divergence is None:
            self.skipTest(f"the gate under test refuses elsewhere: {report.public()}")
        self.assertEqual(report.divergence["index"], 0)
        self.assertIn("Delete", json.dumps(report.divergence["got"]))
        self.assertIn("Archive", json.dumps(report.divergence["expected"]))

    def test_a_prompt_change_is_counted_without_stopping_the_replay(self):
        self.archive_run()
        with mock.patch.object(frontier.prompt, "SYSTEM", frontier.SYSTEM + "\n- One more rule."):
            report = tape.replay(self.folder)
        self.assertIsNone(report.divergence)
        self.assertLess(report.prompts_same, report.prompts_seen)

    def test_scripted_phone_tasks_replay_identically(self):
        for request, acts in (("What's the address of the dinner place Alex sent me?", 2),
                              ("Text Alex that I'm running late", 4)):
            with self.subTest(request=request):
                folder = tempfile.mkdtemp()
                phone = sota_world.ScriptedPhone(sota_world.new_phone_state())
                outcome = record(folder, request, phone, sota_world.ScriptedModel(),
                                 apps={sota_world.MESSAGES: "Messages"})
                report = tape.replay(folder)
                self.assertEqual(outcome["status"], "completed")
                self.assertTrue(report.identical, report.public())
                self.assertEqual(report.recorded_acts, acts)
                self.assertTrue(report.same_answer)
                self.assertEqual(report.prompts_same, report.prompts_seen)

    def test_a_replay_declares_the_capabilities_the_recording_had_and_offers_the_same_operations(self):
        _, phone = self.archive_run()
        replayed = tape.ReplayDriver(tape.Player(tape.load(self.folder)))
        self.assertEqual(phone.capabilities, replayed.capabilities)
        offered = drivers.operations_for(phone.capabilities, frontier.OPERATIONS)
        self.assertEqual(offered, drivers.operations_for(replayed.capabilities, frontier.OPERATIONS))
        self.assertNotIn("LONG_PRESS", offered)
        self.assertIn("TAP_XY", offered)

    def test_a_replay_of_a_richer_phone_offers_its_longer_list(self):
        folder = tempfile.mkdtemp()
        phone = sota_world.ScriptedPhone(sota_world.new_phone_state())
        record(folder, "What's the address of the dinner place Alex sent me?", phone, sota_world.ScriptedModel(),
               apps={sota_world.MESSAGES: "Messages"})
        replayed = tape.ReplayDriver(tape.Player(tape.load(folder)))
        self.assertEqual(phone.capabilities, replayed.capabilities)
        self.assertLessEqual({"capture_preview", "active_app", "tap_point", "clear_text"}, replayed.capabilities)

    def test_a_replay_declares_only_names_the_contract_lists(self):
        self.archive_run()
        player = tape.Player(tape.load(self.folder))
        player.header["capabilities"] = {**player.header["capabilities"], "swipe_hard": "call"}
        self.assertNotIn("swipe_hard", tape.ReplayDriver(player).capabilities)

    def test_reading_the_capabilities_of_a_recorded_driver_writes_nothing_to_the_tape(self):
        phone = Driver([screen(Element("1", "Archive", "Button", (.4, .1, .2, .05)))])
        recorder = tape.Tape(Path(self.folder) / tape.NAME)
        self.assertEqual(phone.capabilities, tape.TapeDriver(phone, recorder).capabilities)
        recorder.close()
        self.assertFalse((Path(self.folder) / tape.NAME).exists())

    def test_the_tape_holds_screens_as_data_and_pictures_by_size_only(self):
        self.archive_run()
        entries = tape.load(self.folder)
        observed = [e for e in entries if e.get("kind") == "call" and e["name"] == "observe"]
        self.assertTrue(observed)
        self.assertEqual(observed[0]["result"]["__t"], "Snapshot")
        self.assertEqual(tape.encode(b"\xff\xd8 jpeg"), {"__t": "bytes", "n": 7})
        self.assertEqual(entries[-1]["kind"], "end")

    def test_snapshots_round_trip(self):
        snapshot = screen(Element("1", "Archive", "Button", (.4, .1, .2, .05), hit=(.5, .12)),
                          Element("2", "Search", "SearchField", (.1, .2, .8, .05), editable=True,
                                  actions=("TAP", "TYPE"), placeholder="Search"))
        back = tape.decode(json.loads(json.dumps(tape.encode(snapshot))))
        self.assertEqual(back.elements, snapshot.elements)
        self.assertEqual(back.content_fingerprint, snapshot.content_fingerprint)

    def test_a_transport_error_with_its_own_init_replays_as_its_class_and_reads_as_live(self):
        """MirroringError and CableBtError take neither status nor wda_error: they decoded to RuntimeError, so a replay
        read known where the live run read unknown."""
        from mobile_agent.bench.tape import decode_error, encode_error
        from mobile_agent.cable_bt import CableBtError
        from mobile_agent.frontier.safety import outcome_unknown
        from mobile_agent.mirroring import MirroringError
        for live in (MirroringError("the screen capture stopped"), CableBtError("the cable dropped")):
            replayed = decode_error(json.loads(json.dumps(encode_error(live))))
            self.assertIs(type(replayed), type(live))
            self.assertEqual((replayed.status, replayed.wda_error), (None, None))
            self.assertEqual(outcome_unknown(replayed, "TAP"), outcome_unknown(live, "TAP"))
            self.assertTrue(outcome_unknown(replayed, "TAP"))

    def test_a_driver_error_replays_as_the_same_kind(self):
        from mobile_agent.wda.guards import DriverRejection
        error = tape.decode_error(tape.encode_error(DriverRejection("physical_tap_outcome_unknown")))
        self.assertIsInstance(error, DriverRejection)
        self.assertEqual(error.code, "physical_tap_outcome_unknown")
        self.assertIsInstance(tape.decode_error(tape.encode_error(TimeoutError("slow"))), TimeoutError)

    def test_keys_never_go_on_the_tape(self):
        self.assertEqual(tape.switches({"MOBSTER_SMART_MODEL": "m", "MOBSTER_API_KEY": "k", "ANTHROPIC_API_KEY": "k",
                                        "MOBSTER_RELAY_TOKEN": "t"}), {"MOBSTER_SMART_MODEL": "m"})


class BundleReplayTests(unittest.TestCase):
    """A trace bundle (a zip holding tape.jsonl.gz, from `mobster export ID --trace`) replays like the tape in it, and
    each recorded action is listed as the same or the one that differs."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        row = screen(Element("1", "Delete", "Button", (.1, .1, .2, .05)),
                     Element("2", "Archive", "Button", (.4, .1, .2, .05)))
        done = screen(Element("3", "Archived", "StaticText", (.4, .1, .2, .05)))
        record(self.folder, ARCHIVE, Driver([row, row, done, done, done]),
               Script([("TAP", "Delete", None), ("TAP", "Archive", None), ("DONE", None, None), ("DONE", None, None)]))
        self.bundle = Path(self.folder) / "bundle.zip"
        self.pack(self.bundle, {"tape.jsonl.gz": (Path(self.folder) / "tape.jsonl.gz").read_bytes()})

    @staticmethod
    def pack(path, members):
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)

    def test_a_bundle_replays_like_its_tape(self):
        report = tape.replay(self.bundle)
        self.assertTrue(report.identical, report.public())
        self.assertEqual(tape.replay(Path(self.folder)).replayed_acts, report.replayed_acts)

    def test_each_recorded_action_is_listed_against_the_replay(self):
        report = tape.replay(self.bundle)
        self.assertEqual([(s["act"], s["now"]) for s in report.steps], [("execute TAP 'Archive'", "same")])
        self.assertEqual(report.steps[0]["turn"], 2)

    def test_a_change_is_listed_at_the_action_it_changes(self):
        with mock.patch.object(frontier.safety, "requested_by", lambda *args, **kwargs: True), \
                mock.patch.object(frontier.stages.guards, "guard", lambda *args, **kwargs: None):
            report = tape.replay(self.bundle)
        if report.divergence is None:
            self.skipTest("the gate under test refuses elsewhere")
        self.assertEqual(len(report.steps), 1)
        self.assertEqual(report.steps[0]["act"], "execute TAP 'Archive'")
        self.assertTrue(report.steps[0]["now"].startswith("differs: execute TAP 'Delete'"), report.steps)

    def test_steps_name_an_action_the_recording_never_sent_and_a_read_it_never_made(self):
        from types import SimpleNamespace
        acts = [{"args": ["execute", "TAP", {"label": "Open", "role": "Button"}, None], "t0": 5}]
        player = SimpleNamespace(acts=acts, acts_done=1, decisions=[1, 4])
        extra = {"why": "an action the recording never sent", "expected": None, "index": 1, "step": 2,
                 "got": ["execute", "TAP", {"label": "Save to list", "role": "Button"}, None]}
        self.assertEqual(tape.steps_of(player, extra), [
            {"index": 0, "turn": 2, "act": "execute TAP 'Open'", "now": "same"},
            {"index": 1, "turn": 3, "act": "(the recording sent none)", "now": "differs: execute TAP 'Save to list'"}])
        read = {"why": "a read the recording didn't make (driver.observe_ready)", "expected": None, "index": 0,
                "step": 1, "got": ["observe_ready"]}
        player.acts_done = 0
        self.assertEqual(tape.steps_of(player, read)[0]["now"],
                         "differs: a read the recording didn't make (driver.observe_ready)")

    def test_a_run_that_ends_early_lists_the_actions_it_never_asked_for(self):
        report = tape.replay(self.bundle, build=lambda driver, client, header, emit: type(
            "Idle", (), {"run": lambda self, request: {"status": "completed", "answer": None}})())
        self.assertEqual([s["now"] for s in report.steps], ["never asked"])
        self.assertFalse(report.identical)

    def test_a_bundle_without_a_tape_says_how_to_get_one(self):
        empty = Path(self.folder) / "empty.zip"
        self.pack(empty, {"manifest.json": "{}", "events.jsonl": ""})
        with self.assertRaisesRegex(ValueError, "MOBSTER_EXPERIMENTAL_TRACES"):
            tape.load(empty)
        broken = Path(self.folder) / "broken.zip"
        broken.write_bytes(b"not a zip")
        with self.assertRaisesRegex(ValueError, "holds no tape"):
            tape.load(broken)

    def test_the_command_takes_bundles_and_folders_of_them(self):
        from mobile_agent.bench import tape_replay
        shelf = Path(self.folder) / "shelf"
        shelf.mkdir()
        (shelf / "one.zip").write_bytes(self.bundle.read_bytes())
        self.assertEqual(tape_replay.tapes([str(self.bundle)]), [self.bundle])
        self.assertEqual(tape_replay.tapes([str(shelf)]), [shelf / "one.zip"])
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = tape_replay.main([str(self.bundle), "--steps"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("identical", text)
        self.assertIn("execute TAP 'Archive'", text)
        self.assertIn("1 of 1 runs identical", text)

    def test_the_hooks_a_live_run_had_are_named_beside_its_steps(self):
        from mobile_agent.bench import tape_replay
        entries = tape.load(self.bundle)
        entries[0]["hooks"] = ["approve", "guard"]
        path = Path(self.folder) / "hooked.jsonl.gz"
        path.write_bytes(gzip.compress("".join(json.dumps(e) + "\n" for e in entries).encode()))
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            tape_replay.main([str(path), "--steps"])
        self.assertIn("the live run also had approve, guard", out.getvalue())


FIXTURES = Path(__file__).parent / "fixtures" / "tapes"


class RecordedRunsTests(unittest.TestCase):
    def test_every_recorded_run_replays_with_every_action_the_same(self):
        tapes = sorted(FIXTURES.glob("*.jsonl.gz"))
        self.assertGreaterEqual(len(tapes), 3)
        for path in tapes:
            with self.subTest(path.name):
                report = tape.replay(path)
                self.assertIsNone(report.error, report.error)
                self.assertTrue(report.identical, (
                    f"{path.name}: this change makes the loop act differently on a recorded run "
                    f"({report.divergence or (report.recorded_status, report.replayed_status)}). If it's meant to, "
                    f"measure it with a paired simulator A/B, record the task again (bench/iosworld.py writes the "
                    f"tape), replace the file here and say so in the pull request."))
                self.assertEqual(report.replayed_acts, report.recorded_acts)

    def test_a_replay_keeps_to_the_recordings_clock_whatever_the_hosts_says(self):
        real = time.monotonic
        start = real()
        for host in (1.0, 1e7):  # a host just booted and one awake for months: before and after every recording
            for path in sorted(FIXTURES.glob("*.jsonl.gz")):
                with self.subTest(path.name, host=host), \
                        mock.patch("time.monotonic", lambda host=host: host + real() - start):
                    report = tape.replay(path)
                    self.assertTrue(report.identical, (
                        f"{path.name} replays differently with the host's clock at {host:.0f} s "
                        f"({report.divergence}): code the loop calls reads the host's clock, so put its module in "
                        f"tape.TIMED"))

    def test_recorded_runs_hold_no_session_ids_paths_or_keys(self):
        private = re.compile(r"(?i)(?!00000000-)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                             r"|/Users/|/private/|sk-ant-|sk-[A-Za-z0-9]{20}|127\.0\.0\.1:(?!8100\b)\d+")
        for path in sorted(FIXTURES.glob("*.jsonl.gz")):
            with self.subTest(path.name):
                self.assertEqual(private.findall(gzip.decompress(path.read_bytes()).decode()), [])


if __name__ == "__main__":
    unittest.main()
