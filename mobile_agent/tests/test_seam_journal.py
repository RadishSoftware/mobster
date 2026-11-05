import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import costs, harness_api, journal, smart_events, tracks
from mobile_agent.api_errors import APIError
from mobile_agent.frontier import FrontierAgent
from mobile_agent.journal import Journal
from mobile_agent.run import Run
from mobile_agent.server import Runtime
from mobile_agent.state import Element
from mobile_agent.tests.seam_support import isolate
from mobile_agent.tests.test_frontier import Driver, Script, screen
from mobile_agent.tests.test_server_engine import MESSAGES, READY, Base
from mobile_agent.tests.test_server_hardening import APP


def tables(path):
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


class TransactionTests(unittest.TestCase):
    def test_interruption_rolls_back_partial_writes_and_allows_a_later_transaction(self):
        for interruption in (KeyboardInterrupt(), SystemExit(), GeneratorExit()):
            with self.subTest(interruption=type(interruption).__name__):
                db = Journal(None)
                self.addCleanup(db.close)
                with self.assertRaises(type(interruption)) as caught:
                    with db.transaction() as connection:
                        connection.execute("INSERT INTO runs(id, data) VALUES (?, ?)",
                                           ("partial", '{"id":"partial"}'))
                        raise interruption
                self.assertIs(caught.exception, interruption)
                self.assertFalse(db.connection.in_transaction)
                self.assertEqual(db.load(), [])
                db.create({"id": "complete", "finishedAt": None})
                self.assertEqual([run["id"] for run in db.load()], ["complete"])


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

    def test_close_waits_for_the_query_owner_and_can_be_repeated(self):
        db = self.open()
        lock, connection, lease = db.lock, db.connection, db.lease
        boundary = threading.Event()
        errors = []

        class ObservedLock:
            def __enter__(self):
                boundary.set()
                return lock.__enter__()

            def __exit__(self, *args):
                return lock.__exit__(*args)

        db.lock = ObservedLock()

        def close():
            try:
                db.close()
            except Exception as error:
                errors.append(error)
            finally:
                boundary.set()

        worker = threading.Thread(target=close)
        try:
            with lock:
                worker.start()
                self.assertTrue(boundary.wait(2))
                self.assertIs(db.connection, connection)
                self.assertIs(db.lease, lease)
                self.assertIsNone(db.lookup("missing"))
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        with db.lock:
            db.close()
        self.assertIsNone(db.connection)
        self.assertIsNone(db.lease)

    def test_a_schema_migrates_up_once_per_version(self):
        seen = []

        def v1(conn, version):
            seen.append(version)
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")

        self.register("threads", 1, v1)
        self.open().close()
        self.open().close()
        self.assertEqual(seen, [0])
        journal.SCHEMAS.clear()

        def v2(conn, version):
            seen.append(version)
            conn.execute("ALTER TABLE threads ADD COLUMN title TEXT")

        self.register("threads", 2, v2)
        db = self.open()
        self.assertEqual(seen, [0, 1])
        self.assertEqual(db.schema_status["threads"], "ok")
        self.assertEqual(db.connection.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_a_newer_schema_turns_off_only_its_own_track_and_the_journal_opens(self):
        self.register("threads", 3, lambda conn, version: conn.execute("CREATE TABLE threads (id TEXT)"))
        self.register("attachments", 1, lambda conn, version: conn.execute("CREATE TABLE attachments (id TEXT)"))
        self.open().close()
        journal.SCHEMAS.clear()
        tracks.STATUS.update(threads="ok", attachments="ok")
        migrate = Mock()
        self.register("threads", 2, migrate)
        self.register("attachments", 1, lambda conn, version: None)
        db = self.open()
        migrate.assert_not_called()
        self.assertEqual(db.schema_status, {"threads": "newer", "attachments": "ok"})
        self.assertEqual(tracks.STATUS["threads"], "error: newer data")
        self.assertEqual(tracks.STATUS["attachments"], "ok")
        self.assertTrue(harness_api.disabled("threads"))
        self.assertFalse(harness_api.disabled("attachments"))
        self.assertEqual(db.load(), [])

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
        db.add_expiry_listener(Mock(side_effect=RuntimeError("listener bug")))
        db.create({"id": "fffffffffff0", "finishedAt": None})
        for index in range(101):
            db.create({"id": f"{index:012x}", "finishedAt": index + 1})
        self.assertEqual(seen, [[f"{0:012x}"]])
        self.assertIn("fffffffffff0", {run["id"] for run in db.load()})
        db.delete(f"{5:012x}")
        self.assertEqual(seen[-1], [f"{5:012x}"])
        db.delete("ffffffffffff")
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
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        runtime = self.runtime()
        with self.assertRaisesRegex(ValueError, "Unsupported task field"):
            runtime.create("messages", "Hi", "live", extras={"unknownField": 1})
        run, replayed = runtime.create("messages", "Hi", "live", "key-1", extras={"threadId": "3f9c2a1b7d0e"})
        self.assertFalse(replayed)
        self.finish(runtime, run)
        with self.assertRaises(APIError) as caught:
            runtime.create("messages", "Hi", "live", "key-1", extras={"threadId": "000000000000"})
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        same, replayed = runtime.create("messages", "Hi", "live", "key-1", extras={"threadId": "3f9c2a1b7d0e"})
        self.assertTrue(replayed)
        self.assertIs(same, run)


class EventBudgetTests(unittest.TestCase):
    def test_a_200_turn_smart_run_finishes_within_the_event_budget(self):
        db = Journal(None)
        self.addCleanup(db.close)
        run = Run(dict(APP), "Tap every row", "live", journal=db, engine="smart")
        db.create(run.metadata())
        events = smart_events.SmartEvents(run.emit, frame=lambda: None)
        rows = [screen(Element("1", f"Row {i}", "Button", (.1, .2, .8, .05)), bundle="com.example.mail")
                for i in range(205)]
        steps = [("TAP", f"Row {i}", None)
                 for i in range(140)] + [("WAIT", None, None)] * 58 + [("DONE", None, None)] * 3
        client = Script(steps)
        client.model = "gpt-5.6-sol"
        costs.meter(client, events.inference)
        agent = FrontierAgent(Driver(rows), client, apps={"com.example.mail": "Mail"}, emit=events, max_steps=200,
                              max_seconds=3600, screenshots=False, settle_seconds=0, skills=())
        events.agent = agent
        outcome = agent.run("Tap every row in Mail")
        events.finish()
        run.finish({"event": "result", "status": outcome["status"] if outcome["status"] == "completed"
                    else "max_steps"})
        self.assertGreaterEqual(outcome["steps"], 199)
        self.assertNotIn("persistence_failed", run.summary)
        self.assertLessEqual(len(run.events), 2400)
        self.assertGreater(len(run.events), 1024)
        self.assertEqual(len(db.load()[0]["events"]), len(run.events))


if __name__ == "__main__":
    unittest.main()
