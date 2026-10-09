"""Seam S5: track schemas in the run journal, expiry listeners, run extras and origin through a reload, and an event
budget that lets a 200-turn Smart run finish. Offline."""

from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mobile_agent import engines, harness_api, journal, tracks
from mobile_agent.frontier import FrontierAgent
from mobile_agent.journal import Journal
from mobile_agent.server import Run, Runtime
from mobile_agent.state import Element
from mobile_agent.tests.seam_support import isolate
from mobile_agent.tests.test_frontier import Driver, Script, screen
from mobile_agent.tests.test_server_engine import Base, MESSAGES, READY
from mobile_agent.tests.test_server_hardening import APP


def tables(path):
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


class SchemaTests(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.path = Path(tempfile.mkdtemp(prefix="mobster-seam-journal-")) / "runs.sqlite3"

    def open(self):
        db = Journal(self.path)
        self.addCleanup(db.close)
        return db

    def register(self, track, version, migrate):
        with harness_api.owned_by(track):
            journal.register_schema(track, version, migrate)

    def test_a_schema_migrates_up_once_per_version(self):
        seen = []

        def v1(conn, version):
            seen.append(version)
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")

        self.register("threads", 1, v1)
        self.open().close()
        self.open().close()  # stored == registered: nothing runs
        self.assertEqual(seen, [0])
        journal.SCHEMAS.clear()

        def v2(conn, version):
            seen.append(version)
            conn.execute("ALTER TABLE threads ADD COLUMN title TEXT")

        self.register("threads", 2, v2)
        db = self.open()
        self.assertEqual(seen, [0, 1])
        self.assertEqual(db.schema_status["threads"], "ok")
        self.assertEqual(db.connection.execute("PRAGMA user_version").fetchone()[0], 1)  # older builds still open it

    def test_a_newer_schema_turns_off_only_its_own_track_and_the_journal_opens(self):
        self.register("threads", 3, lambda conn, version: conn.execute("CREATE TABLE threads (id TEXT)"))
        self.register("attachments", 1, lambda conn, version: conn.execute("CREATE TABLE attachments (id TEXT)"))
        self.open().close()
        journal.SCHEMAS.clear()
        tracks.STATUS.update(threads="ok", attachments="ok")
        migrate = Mock()
        self.register("threads", 2, migrate)  # an older build meets data from a newer one
        self.register("attachments", 1, lambda conn, version: None)
        db = self.open()
        migrate.assert_not_called()
        self.assertEqual(db.schema_status, {"threads": "newer", "attachments": "ok"})
        self.assertEqual(tracks.STATUS["threads"], "error: newer data")
        self.assertEqual(tracks.STATUS["attachments"], "ok")
        self.assertTrue(harness_api.disabled("threads"))
        self.assertFalse(harness_api.disabled("attachments"))
        self.assertEqual(db.load(), [])  # the run journal works

    def test_a_failing_migration_rolls_back_and_the_journal_opens(self):
        def broken(conn, version):
            conn.execute("CREATE TABLE half_made (id TEXT)")
            raise RuntimeError("bad migration")

        self.register("memory_items", 1, broken)
        db = self.open()
        self.assertEqual(db.schema_status["memory_items"], "migration_failed")
        self.assertEqual(tracks.STATUS["memory_items"], "error: migration")
        self.assertNotIn("half_made", tables(self.path))
        self.assertIsNone(db.connection.execute("SELECT version FROM schema_versions WHERE name='memory_items'")
                          .fetchone())
        db.create({"id": "a" * 12, "finishedAt": None})
        self.assertEqual([r["id"] for r in db.load()], ["a" * 12])

    def test_names_versions_and_duplicates_are_checked(self):
        for name, version in (("X", 1), ("ok_name", 0), ("ok_name", True), ("a", 1)):
            with self.subTest(name=name, version=version), self.assertRaises(ValueError):
                journal.register_schema(name, version, lambda conn, v: None)
        journal.register_schema("threads", 1, lambda conn, v: None)
        with self.assertRaises(ValueError):
            journal.register_schema("threads", 2, lambda conn, v: None)


class ExpiryTests(unittest.TestCase):
    def test_retention_and_delete_tell_the_listeners(self):
        db = Journal(None)
        self.addCleanup(db.close)
        seen = []
        db.add_expiry_listener(seen.append)
        db.add_expiry_listener(Mock(side_effect=RuntimeError("listener bug")))  # logged, never fatal
        for index in range(101):
            db.create({"id": f"{index:012x}", "finishedAt": None})
        self.assertEqual(seen, [[f"{0:012x}"]])
        db.delete(f"{5:012x}")
        self.assertEqual(seen[-1], [f"{5:012x}"])
        db.delete("ffffffffffff")  # nothing removed: nothing said
        self.assertEqual(len(seen), 2)


class ExtrasReloadTests(Base):
    def setUp(self):
        super().setUp()
        isolate(self)
        harness_api.register_run_field("threadId", lambda value, runtime: str(value))
        harness_api.register_run_field("secretNote", lambda value, runtime: value, public=False)

    def reopen(self, state):
        config = SimpleNamespace(wda_url="http://127.0.0.1:8203", session=None, enable_live=True, port=8765,
                                 state_db=str(state / "runs.sqlite3"), env_file=str(state / "agent.env"))
        with patch("mobile_agent.server.WdaVideo"), patch("mobile_agent.server.ManualControl"):
            runtime = Runtime(config)
        runtime.target_status = Mock(return_value=dict(READY))
        runtime.apps = Mock(return_value=[dict(APP), dict(MESSAGES)])
        self.runtimes.append(runtime)
        return runtime

    def test_extras_and_origin_survive_a_reload_and_only_public_fields_show(self):
        import os
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        state = self.root / "reload"
        runtime = self.reopen(state)
        run = runtime.create("messages", "Read my messages", "live",
                             extras={"threadId": "3f9c2a1b7d0e", "secretNote": "only in memory"}, origin="tui")
        self.assertEqual(run.extras, {"threadId": "3f9c2a1b7d0e", "secretNote": "only in memory"})
        public = run.public()
        self.assertEqual((public["extras"], public["origin"]), ({"threadId": "3f9c2a1b7d0e"}, "tui"))
        self.finish(runtime, run)
        runtime.close()
        self.runtimes.remove(runtime)
        again = self.reopen(state)
        loaded = again.runs[run.id]
        self.assertEqual((loaded.extras, loaded.origin), ({"threadId": "3f9c2a1b7d0e"}, "tui"))

    def test_extras_are_validated_and_fingerprinted(self):
        import os
        from mobile_agent.server import APIError
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        with self.assertRaisesRegex(ValueError, "Unsupported task field"):
            runtime.create("messages", "Hi", "live", extras={"unknownField": 1})
        run, replayed = runtime.create("messages", "Hi", "live", "key-1", extras={"threadId": "3f9c2a1b7d0e"})
        self.assertFalse(replayed)
        self.finish(runtime, run)
        with self.assertRaises(APIError) as caught:  # same key, other extras: another task
            runtime.create("messages", "Hi", "live", "key-1", extras={"threadId": "000000000000"})
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        same, replayed = runtime.create("messages", "Hi", "live", "key-1", extras={"threadId": "3f9c2a1b7d0e"})
        self.assertTrue(replayed)
        self.assertIs(same, run)


class EventBudgetTests(unittest.TestCase):
    def test_a_200_turn_smart_run_finishes_within_the_event_budget(self):
        self.assertEqual(journal.EVENT_LIMIT, 4096)
        db = Journal(None)
        self.addCleanup(db.close)
        run = Run(dict(APP), "Tap every row", "live", journal=db, engine="smart")
        db.create(run.metadata())
        events = engines.SmartEvents(run.emit, frame=lambda: None)
        rows = [screen(Element("1", f"Row {i}", "Button", (.1, .2, .8, .05)), bundle="com.example.mail")
                for i in range(205)]
        # The frontier takes at most 150 actions (MAX_ACTIONS): 140 taps, then turns that act on nothing.
        steps = [("TAP", f"Row {i}", None) for i in range(140)] + [("WAIT", None, None)] * 58 + [("DONE", None, None)] * 3
        client = Script(steps)
        client.model = "gpt-5.6-sol"
        engines.meter(client, events.inference)
        agent = FrontierAgent(Driver(rows), client, apps={"com.example.mail": "Mail"}, emit=events, max_steps=200,
                              max_seconds=3600, screenshots=False, settle_seconds=0, skills=())
        events.agent = agent
        outcome = agent.run("Tap every row in Mail")
        events.finish()
        run.finish({"event": "result", "status": outcome["status"] if outcome["status"] == "completed" else "max_steps"})
        self.assertGreaterEqual(outcome["steps"], 199)
        self.assertNotIn("persistence_failed", run.summary)
        self.assertLessEqual(len(run.events), 2400)
        self.assertGreater(len(run.events), 1024)  # the old EVENT_LIMIT would have ended it
        self.assertEqual(len(db.load()[0]["events"]), len(run.events))


if __name__ == "__main__":
    unittest.main()
