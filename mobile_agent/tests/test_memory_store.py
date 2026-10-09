"""Track memory: the store (SPEC §3.3 data model). Facts and suggestions, their limits and the secret filter, the
settings, clearing, export, the file's permissions and a newer schema, and two connections at once. Offline: a
temporary folder per test, never the real data folder."""

import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent.api_errors import APIError
from mobile_agent.memory import store as memory_store
from mobile_agent.memory.store import MemoryStore


class Clock:
    def __init__(self, now=1_790_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make(testcase, clock=None):
    folder = Path(tempfile.mkdtemp(prefix="mobster-memory-")) / "memory"
    return MemoryStore(folder, clock=clock or Clock())


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = make(self, self.clock)

    def refused(self, code, fn, *args, **kwargs):
        with self.assertRaises(APIError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception


class FactTests(Base):
    def test_reading_never_creates_the_store(self):
        self.assertEqual(self.store.list_facts(), [])
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.all_facts(), [])
        self.assertEqual(self.store.list_proposals(), [])
        self.assertEqual(self.store.settings(), {"useInTasks": True, "suggest": True})
        self.assertEqual(self.store.clear(), {"facts": 0, "proposals": 0})
        self.assertFalse(self.store.folder.exists())

    def test_add_edit_pin_delete(self):
        fact = self.store.add_fact("  My gym is   the one on 5th Street ")
        self.assertEqual(fact["text"], "My gym is the one on 5th Street")
        self.assertEqual((fact["scope"], fact["app"], fact["source"], fact["pinned"], fact["uses"]),
                         ("global", None, "user", False, 0))
        self.assertEqual(fact["createdAt"], 1_790_000_000_000)
        self.assertRegex(fact["id"], r"^[a-f0-9]{12}$")
        texts = self.store.add_fact("Sign my texts with – Sam", "app:com.apple.MobileSMS", pinned=True)
        self.assertEqual(texts["app"], {"bundleId": "com.apple.MobileSMS", "name": "Messages"})
        self.clock.now += 10
        edited = self.store.update_fact(fact["id"], text="My gym is the one on 6th Street", pinned=True)
        self.assertEqual((edited["text"], edited["pinned"], edited["updatedAt"]),
                         ("My gym is the one on 6th Street", True, 1_790_000_010_000))
        listed = self.store.list_facts()
        self.assertEqual([f["id"] for f in listed], [fact["id"], texts["id"]])   # pinned, then the newest
        self.assertEqual([f["id"] for f in self.store.list_facts(scope="app:com.apple.MobileSMS")], [texts["id"]])
        self.assertEqual([f["id"] for f in self.store.list_facts(q="6TH gym")], [fact["id"]])
        self.assertEqual([f["id"] for f in self.store.list_facts(q="messages")], [texts["id"]])  # the app's name
        self.assertTrue(self.store.delete_fact(fact["id"]))
        self.refused("not_found", self.store.delete_fact, fact["id"])
        self.refused("not_found", self.store.update_fact, fact["id"], text="x")
        self.refused("not_found", self.store.get_fact, fact["id"])
        self.assertEqual(self.store.count(), 1)

    def test_limits_say_so_and_never_truncate(self):
        self.refused("invalid_text", self.store.add_fact, "   ")
        self.refused("invalid_text", self.store.add_fact, None)
        error = self.refused("too_long", self.store.add_fact, "x" * 301)
        self.assertEqual(str(error), "Keep it to 300 characters.")
        self.assertEqual(self.store.add_fact("y" * 300)["text"], "y" * 300)
        self.refused("invalid_scope", self.store.add_fact, "Fine", "app:not a bundle")
        self.refused("invalid_scope", self.store.add_fact, "Fine", "everywhere")
        self.refused("duplicate", self.store.add_fact, "Y" * 300)
        with patch.object(memory_store, "MAX_FACTS", 3):
            self.store.add_fact("one thing")
            self.store.add_fact("two things")
            self.refused("memory_full", self.store.add_fact, "three things")

    def test_the_same_words_in_another_app_are_another_fact(self):
        self.store.add_fact("Use my work account")
        self.store.add_fact("Use my work account", "app:com.apple.mobilemail")
        self.assertEqual(self.store.count(), 2)
        other = self.store.add_fact("Something else")
        self.refused("duplicate", self.store.update_fact, other["id"], text="use my work account!")

    def test_pins_have_a_count_and_a_size(self):
        with patch.object(memory_store, "MAX_PINNED", 2), patch.object(memory_store, "PINNED_CHARS", 30):
            a = self.store.add_fact("a" * 10, pinned=True)
            b = self.store.add_fact("b" * 10, pinned=True)
            error = self.refused("too_many_pinned", self.store.add_fact, "c" * 5, pinned=True)
            self.assertEqual(str(error), "You can pin up to 2 things. Unpin one first.")
            self.store.update_fact(b["id"], pinned=False)
            self.refused("pinned_too_long", self.store.add_fact, "d" * 21, pinned=True)
            self.refused("pinned_too_long", self.store.update_fact, a["id"], text="a" * 31)
            self.store.update_fact(a["id"], text="a" * 30)   # it alone may fill the room
            c = self.store.add_fact("c" * 5)
            self.refused("pinned_too_long", self.store.update_fact, c["id"], pinned=True)

    def test_secrets_are_refused_with_one_sentence(self):
        for text in ("My bank password is hunter2!", "the gate code is 4821", "my PIN is 1234", "OTP 551203",
                     "Card 4242 4242 4242 4242", "SSN 123-45-6789", "IBAN GB82WEST12345698765432",
                     "key " + "sk-" + "ant-" + "A" * 24, "sam@example.com : hunter2", "Code ••••••"):
            with self.subTest(text=text):
                error = self.refused("secret", self.store.add_fact, text)
                self.assertEqual(str(error), "Mobster doesn't remember passwords, codes or card numbers.")
                self.assertEqual(error.status, 400)
        fact = self.store.add_fact("My gym is on 5th Street")
        self.refused("secret", self.store.update_fact, fact["id"], text="My locker code is 4821")
        self.assertEqual(self.store.count(), 1)
        for text in ("My gym is on 5th Street, open until 10", "Kate Bell is my sister", "My zip is 94110"):
            with self.subTest(text=text):
                self.assertFalse(memory_store.is_secret(text))

    def test_the_seams_whole_secret_table_is_refused_on_every_write_path(self):
        """Acceptance 2 (SPEC §3.3): every string in the seam's table (test_seam_safety.SECRETS) that is_secret
        flags is refused as a fact, as an edit, as a suggestion and as an edited answer; every other one isn't
        refused as a secret."""
        from mobile_agent.tests.test_seam_safety import SECRETS
        fact = self.store.add_fact("My gym is on 5th Street")
        pending = self.store.propose("Kate Bell is my sister")
        secrets = 0
        for text, secret, _redacted in SECRETS:
            if len(" ".join(text.split())) > memory_store.FACT_CHARS or not text.strip():
                continue
            with self.subTest(text=text):
                if secret:
                    secrets += 1
                    self.refused("secret", self.store.add_fact, text)
                    self.refused("secret", self.store.update_fact, fact["id"], text=text)
                    self.assertIsNone(self.store.propose(text))
                    self.refused("secret", self.store.resolve_proposal, pending["id"], True, text=text)
                else:
                    try:
                        self.store.add_fact(text)
                    except APIError as error:
                        self.assertNotEqual(error.code, "secret")
        self.assertGreaterEqual(secrets, 20)
        self.assertEqual(self.store.get_fact(fact["id"])["text"], "My gym is on 5th Street")
        self.assertEqual(self.store.list_proposals()[0]["status"], "pending")
        stored = " ".join(f["text"] for f in self.store.list_facts())
        self.assertFalse(memory_store.is_secret(stored))

    def test_usage_is_recorded_and_never_raises(self):
        fact = self.store.add_fact("Kate Bell is my sister")
        self.clock.now += 5
        self.store.mark_used([fact["id"]])
        self.store.mark_used([])
        again = self.store.get_fact(fact["id"])
        self.assertEqual((again["uses"], again["lastUsedAt"]), (1, 1_790_000_005_000))
        with patch.object(MemoryStore, "_write", side_effect=APIError("busy", 503, "memory_unavailable")):
            self.store.mark_used([fact["id"]])


class ProposalTests(Base):
    def test_a_suggestion_is_never_a_fact_until_accepted(self):
        proposal = self.store.propose("My gym is the one on 5th Street", thread_id="3f9c2a1b7d0e",
                                      run_id="a1b2c3d4e5f6")
        self.assertEqual((proposal["status"], proposal["threadId"], proposal["runId"], proposal["pin"]),
                         ("pending", "3f9c2a1b7d0e", "a1b2c3d4e5f6", False))
        self.assertEqual(proposal["expiresAt"], (1_790_000_000 + 14 * 86400) * 1000)
        self.assertEqual(self.store.count(), 0)
        self.assertEqual([p["id"] for p in self.store.list_proposals(thread_id="3f9c2a1b7d0e")], [proposal["id"]])
        self.assertEqual(self.store.list_proposals(thread_id="000000000000"), [])
        answered, fact = self.store.resolve_proposal(proposal["id"], True, text="My gym is the one on 6th Street")
        self.assertEqual((answered["status"], answered["text"], answered["factId"]), ("accepted", "", fact["id"]))
        self.assertEqual((fact["text"], fact["source"]), ("My gym is the one on 6th Street", "proposal"))
        self.refused("not_pending", self.store.resolve_proposal, proposal["id"], True)
        self.refused("not_found", self.store.resolve_proposal, "000000000000", True)
        self.refused("invalid_answer", self.store.resolve_proposal, proposal["id"], "yes")

    def test_not_now_keeps_only_a_digest_and_is_not_asked_again(self):
        proposal = self.store.propose("I always take the window seat", pin=True)
        answered, fact = self.store.resolve_proposal(proposal["id"], False)
        self.assertIsNone(fact)
        self.assertEqual((answered["status"], answered["text"]), ("dismissed", ""))
        self.assertEqual(self.store.count(), 0)
        with sqlite3.connect(self.store.path) as conn:
            self.assertNotIn("window", " ".join(str(v) for row in conn.execute("SELECT * FROM proposals")
                                                for v in row))
        self.assertIsNone(self.store.propose("i always take the window seat."))
        self.clock.now += 91 * 86400                       # after the quiet period it may be suggested again
        self.assertIsNotNone(self.store.propose("I always take the window seat"))

    def test_what_is_never_suggested(self):
        self.store.add_fact("Kate Bell is my sister")
        self.assertIsNone(self.store.propose("kate bell is my sister"))      # already remembered
        self.assertIsNone(self.store.propose("My gate code is 4821"))        # a secret
        self.assertIsNone(self.store.propose("x" * 301))
        first = self.store.propose("My gym is on 5th")
        self.assertIsNone(self.store.propose("My gym is on 5th"))             # already waiting
        self.assertIsNotNone(first)

    def test_suggestions_expire_after_14_days_and_the_queue_is_capped(self):
        old = self.store.propose("My gym is on 5th")
        self.clock.now += 14 * 86400 + 1
        self.assertEqual(self.store.list_proposals(), [])
        expired = self.store.list_proposals(status="expired")
        self.assertEqual(([p["id"] for p in expired], expired[0]["text"]), ([old["id"]], ""))
        self.refused("not_pending", self.store.resolve_proposal, old["id"], True)
        with patch.object(memory_store, "MAX_PENDING", 3):
            made = [self.store.propose(f"My {n} is fine") for n in ("dog", "cat", "fish", "bird")]
            pending = self.store.list_proposals()
            self.assertEqual({p["id"] for p in pending}, {p["id"] for p in made[1:]})
        self.refused("invalid_status", self.store.list_proposals, status="maybe")
        self.assertEqual(len(self.store.list_proposals(status="all")), 5)

    def test_accepting_a_pinned_suggestion_with_no_room_keeps_it_unpinned(self):
        with patch.object(memory_store, "MAX_PINNED", 1):
            self.store.add_fact("Call me Sam", pinned=True)
            proposal = self.store.propose("I prefer aisle seats", pin=True)
            _, fact = self.store.resolve_proposal(proposal["id"], True)
            self.assertFalse(fact["pinned"])
            other = self.store.propose("I never eat shellfish", pin=True)
            self.refused("too_many_pinned", self.store.resolve_proposal, other["id"], True, pinned=True)

    def test_accepting_what_is_already_remembered_links_to_it(self):
        proposal = self.store.propose("My gym is on 5th")
        fact = self.store.add_fact("My gym is on 5th")
        answered, linked = self.store.resolve_proposal(proposal["id"], True)
        self.assertEqual((answered["status"], linked["id"]), ("accepted", fact["id"]))
        self.assertEqual(self.store.count(), 1)
        self.refused("secret", self.store.resolve_proposal, self.store.propose("My dog is Rex")["id"], True,
                     text="my PIN is 1234")

    def test_the_thread_item_is_kept_for_the_card(self):
        proposal = self.store.propose("My gym is on 5th", thread_id="3f9c2a1b7d0e")
        self.assertEqual(self.store.proposal_item(proposal["id"]), ("3f9c2a1b7d0e", None))
        self.store.set_proposal_item(proposal["id"], "b2c3d4e5f6a1")
        self.assertEqual(self.store.proposal_item(proposal["id"]), ("3f9c2a1b7d0e", "b2c3d4e5f6a1"))
        self.assertEqual(self.store.proposal_item("000000000000"), (None, None))


class FileTests(Base):
    def test_settings_default_on_and_change(self):
        self.assertEqual(self.store.settings(), {"useInTasks": True, "suggest": True})
        self.assertEqual(self.store.update_settings({"suggest": False}), {"useInTasks": True, "suggest": False})
        self.refused("invalid_settings", self.store.update_settings, {"useRoutines": True})
        self.refused("invalid_settings", self.store.update_settings, {"suggest": "no"})
        self.refused("invalid_settings", self.store.update_settings, {})

    def test_clear_deletes_facts_and_suggestions_and_keeps_settings(self):
        self.store.add_fact("Kate Bell is my sister")
        self.store.add_fact("My gym is on 5th", pinned=True)
        self.store.propose("I prefer aisle seats")
        self.store.update_settings({"useInTasks": False})
        self.assertEqual(self.store.clear(), {"facts": 2, "proposals": 1})
        self.assertEqual((self.store.count(), self.store.list_proposals(status="all")), (0, []))
        self.assertEqual(self.store.settings()["useInTasks"], False)
        raw = self.store.path.read_bytes()
        self.assertNotIn(b"Kate Bell", raw)                       # vacuumed: the words are gone from the file

    def test_forgotten_words_are_overwritten_in_the_file(self):
        """Delete one fact or answer "Not now": the words go from the file too, whatever SQLite's own default."""
        for n in range(4):
            self.store.add_fact(f"Ordinary fact number {n}")
        locker = self.store.add_fact("My locker is number seventeen by the zebra mural")
        self.store.add_fact("Another fact after it")
        quiet = self.store.propose("I always order the quokka special")
        self.store.delete_fact(locker["id"])
        self.store.resolve_proposal(quiet["id"], False)
        raw = b"".join(path.read_bytes() for path in self.store.folder.iterdir())
        self.assertFalse(b"zebra mural" in raw, "a deleted fact's words are still in the file")
        self.assertFalse(b"quokka special" in raw, "a dismissed suggestion's words are still in the file")
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("PRAGMA secure_delete").fetchone()[0], 1)

    def test_export_reads_like_a_page(self):
        self.assertIn("Nothing yet.", self.store.export_markdown())
        self.store.add_fact("Kate Bell is my sister")
        self.store.add_fact("Call me Sam", pinned=True)
        self.store.add_fact("Sign my texts with – Sam", "app:com.apple.MobileSMS")
        self.store.add_fact("Use the shared calendar", "app:com.example.planner")
        text = self.store.export_markdown()
        self.assertTrue(text.startswith("# What Mobster remembers\n"))
        self.assertIn("## In every app\n\n- Call me Sam (pinned)\n- Kate Bell is my sister\n", text)
        self.assertIn("## In Messages\n\n- Sign my texts with – Sam\n", text)
        self.assertIn("## In com.example.planner\n\n- Use the shared calendar\n", text)

    def test_folder_and_file_are_private(self):
        self.store.add_fact("Kate Bell is my sister")
        self.assertEqual(stat.S_IMODE(os.stat(self.store.folder).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.path).st_mode), 0o600)
        with sqlite3.connect(self.store.path) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"facts", "proposals", "routines", "routine_versions", "settings"} <= tables)

    def test_a_newer_file_is_left_alone(self):
        self.store.add_fact("Kate Bell is my sister")
        with sqlite3.connect(self.store.path) as conn:
            conn.execute("PRAGMA user_version = 2")
        for call in (self.store.list_facts, self.store.count, lambda: self.store.add_fact("Hi there")):
            error = self.refused("newer_data", call)
            self.assertEqual((error.status, str(error)),
                             (503, "This was saved by a newer Mobster. Update Mobster to use it."))

    def test_two_connections_at_once(self):
        """The Mac app's agent and `mobster memory` share the file: writes from two stores never lose each other."""
        other = MemoryStore(self.store.folder, clock=self.clock)
        errors = []

        def write(store, prefix):
            try:
                for n in range(25):
                    store.add_fact(f"{prefix} fact number {n}")
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        threads = [threading.Thread(target=write, args=(s, p)) for s, p in ((self.store, "one"), (other, "two"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(other.count(), 50)


class LocationTests(unittest.TestCase):
    def test_where_the_file_lives(self):
        with patch.dict(os.environ, {"MOBSTER_MEMORY_DIR": "/tmp/x-memory", "HOME": "/srv/example-home"}):
            self.assertEqual(memory_store.memory_dir(), Path("/tmp/x-memory"))
        with patch.dict(os.environ, {"HOME": "/srv/example-home"}, clear=True):
            self.assertEqual(memory_store.memory_dir(),
                             Path("/srv/example-home/Library/Application Support/app.mobster.desktop/memory"))
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(memory_store.memory_dir())       # a test that cleared the environment: no store
            self.assertIsNone(memory_store.default_store())


if __name__ == "__main__":
    unittest.main()
