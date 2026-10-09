"""The threads store in the journal: its schema migrates (and a newer one turns only conversations off), items get
their order and caps, appends are idempotent where asked, titles and rules are cut by code. Offline: in-memory or a
scratch journal."""

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mobile_agent import tracks
from mobile_agent.journal import Journal
from mobile_agent.tests.seam_support import isolate
from mobile_agent.threads import register as threads_register
from mobile_agent.threads import store as thread_store
from mobile_agent.threads.service import merge_rules, prohibitions
from mobile_agent.threads.store import ThreadFull, ThreadMissing, ThreadStore, public_item, title_from


class Registered(unittest.TestCase):
    def setUp(self):
        isolate(self)
        tracks._loaded = True  # this test registers the threads track alone
        self.assertTrue(tracks.register_one("threads", threads_register.register))

    def open(self, path=None):
        db = Journal(path)
        self.addCleanup(db.close)
        return db


class SchemaTests(Registered):
    def test_the_schema_migrates_once_and_reopens(self):
        path = Path(tempfile.mkdtemp(prefix="mobster-threads-")) / "runs.sqlite3"
        db = self.open(path)
        self.assertEqual(db.schema_status["threads"], "ok")
        tables = {r[0] for r in db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"threads", "thread_items", "thread_requests"} <= tables)
        ThreadStore(db).create_thread(title="Kate's birthday")
        db.close()
        again = self.open(path)
        self.assertEqual([t["title"] for t in ThreadStore(again).list()[0]], ["Kate's birthday"])
        self.assertEqual(again.connection.execute("SELECT version FROM schema_versions WHERE name='threads'")
                         .fetchone()[0], thread_store.VERSION)
        self.assertEqual(again.connection.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_a_newer_schema_turns_only_conversations_off(self):
        path = Path(tempfile.mkdtemp(prefix="mobster-threads-")) / "runs.sqlite3"
        db = self.open(path)
        db.close()
        connection = sqlite3.connect(path)
        connection.execute("UPDATE schema_versions SET version=99 WHERE name='threads'")
        connection.commit()
        connection.close()
        again = self.open(path)
        self.assertEqual(again.schema_status["threads"], "newer")
        self.assertEqual(tracks.STATUS["threads"], "error: newer data")
        self.assertEqual(again.load(), [])  # the run journal opens anyway


class ItemTests(Registered):
    def setUp(self):
        super().setUp()
        self.store = ThreadStore(self.open())
        self.thread = self.store.create_thread(title="Find my last message from Kate Bell")

    def test_items_get_ids_seqs_and_times_and_private_keys_stay_private(self):
        added = self.store.append(self.thread["id"], [{"kind": "user", "text": "hi", "runId": "a" * 12},
                                                      {"kind": "run", "runId": "a" * 12, "_context": {"notes": []}}])
        self.assertEqual([i["seq"] for i in added], [0, 1])
        self.assertRegex(added[0]["id"], r"^[a-f0-9]{12}$")
        self.assertNotIn("_context", public_item(added[1]))
        self.assertEqual(self.store.items(self.thread["id"], after=0)[0]["kind"], "run")
        self.assertEqual(self.store.run_ids(self.thread["id"]), ["a" * 12])
        updated = self.store.update_item(self.thread["id"], added[0]["id"], {"text": "hello", "seq": 9, "kind": "x"})
        self.assertEqual((updated["text"], updated["seq"], updated["kind"]), ("hello", 0, "user"))
        self.assertIsNone(self.store.update_item(self.thread["id"], "f" * 12, {"text": "x"}))

    def test_idempotent_appends(self):
        item = {"kind": "user", "text": "only today's", "runId": "b" * 12, "messageId": "c" * 12}

        def same(found):
            return any(i.get("messageId") == "c" * 12 for i in found)
        self.assertIsNotNone(self.store.append(self.thread["id"], [item], unless=same))
        self.assertIsNone(self.store.append(self.thread["id"], [item], unless=same))
        self.assertEqual(len(self.store.items(self.thread["id"])), 1)

    def test_caps_say_so_and_store_nothing(self):
        with patch.object(thread_store, "MAX_ITEMS", 2):
            self.store.append(self.thread["id"], [{"kind": "user", "text": "a"}, {"kind": "run"}])
            with self.assertRaises(ThreadFull) as caught:
                self.store.append(self.thread["id"], [{"kind": "user", "text": "b"}])
        self.assertEqual((caught.exception.status, caught.exception.code, str(caught.exception)),
                         (409, "thread_full", "This conversation is full. Start a new one."))
        with patch.object(thread_store, "MAX_THREAD_BYTES", 100):
            with self.assertRaises(ThreadFull):
                self.store.append(self.thread["id"], [{"kind": "user", "text": "x" * 200}])
        self.assertEqual(len(self.store.items(self.thread["id"])), 2)
        with self.assertRaises(ThreadMissing):
            self.store.append("0" * 12, [{"kind": "user"}])
        with self.assertRaises(ValueError):
            first = self.store.items(self.thread["id"])[0]
            self.store.update_item(self.thread["id"], first["id"], {"blob": "x" * 9000}, max_bytes=8000)

    def test_delete_takes_the_items_and_request_keys(self):
        self.store.append(self.thread["id"], [{"kind": "user", "text": "a"}])
        self.store.remember("k1", self.thread["id"], "f", {"status": 201, "response": {}})
        self.assertTrue(self.store.delete_thread(self.thread["id"]))
        self.assertIsNone(self.store.get(self.thread["id"]))
        self.assertIsNone(self.store.request("k1"))
        self.assertFalse(self.store.delete_thread(self.thread["id"]))
        self.assertIsNone(self.store.get("not an id"))


class WordsTests(unittest.TestCase):
    def test_titles_are_one_line_cut_at_a_word(self):
        self.assertEqual(title_from("Find my\nlast message"), "Find my last message")
        long = "Find my last message from Kate Bell and then draft a reply that says I'm running about ten minutes late"
        title = title_from(long)
        self.assertLessEqual(len(title), 80)
        self.assertTrue(title.endswith("…"))
        self.assertFalse(title[:-1].endswith(" "))
        self.assertEqual(title_from(""), "")

    def test_rules_are_copied_by_code_and_capped(self):
        self.assertEqual(prohibitions("Find Kate's message. Don't reply yet"), ["Don't reply yet."])
        self.assertEqual(prohibitions("Never text Sam after 10 pm! Then check Mail."), ["Never text Sam after 10 pm!"])
        self.assertEqual(prohibitions("Text Sam I'm late"), [])
        rules = []
        for n in range(15):
            rules = merge_rules(rules, f"Don't book anything before {n} am")
        self.assertLessEqual(len(rules), 10)
        self.assertEqual(rules[-1], "Don't book anything before 14 am.")  # the newest kept
        self.assertEqual(merge_rules(["Don't reply yet."], "don't reply yet"), ["Don't reply yet."])


if __name__ == "__main__":
    unittest.main()
