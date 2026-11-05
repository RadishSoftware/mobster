import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import harness_api
from mobile_agent.api_errors import APIError
from mobile_agent.memory import provider as memory_provider
from mobile_agent.memory import retrieval
from mobile_agent.memory import store as memory_store
from mobile_agent.memory.extract import Candidate, candidates
from mobile_agent.memory.store import MemoryStore
from mobile_agent.secret_filter import is_secret
from mobile_agent.server import make_handler
from mobile_agent.tests.fakes import Clock
from mobile_agent.tests.seam_support import isolate, request
from mobile_agent.tests.seam_tasks import SmartBase
from mobile_agent.tests.test_server_engine import Base


def make(testcase, clock=None):
    folder = Path(tempfile.mkdtemp(prefix="mobster-memory-")) / "memory"
    return MemoryStore(folder, clock=clock or Clock(1_790_000_000.0))


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(1_790_000_000.0)
        self.store = make(self, self.clock)

    def refused(self, code, fn, *args, **kwargs):
        with self.assertRaises(APIError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception


class FactTests(StoreBase):
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
        self.refused("invalid_scope", self.store.add_fact, "Fine", "the world")
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
            self.store.update_fact(a["id"], text="a" * 30)
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
                self.assertFalse(is_secret(text))

    def test_the_seams_whole_secret_table_is_refused_on_every_write_path(self):
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
        self.assertFalse(is_secret(stored))


class ProposalTests(StoreBase):
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
        self.assertEqual((fact["text"], fact["source"]), ("My gym is the one on 6th Street", "user"))
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


class FileTests(StoreBase):
    def test_a_folder_that_cannot_be_created_reports_memory_unavailable(self):
        self.store.folder.parent.mkdir(parents=True, exist_ok=True)
        self.store.folder.write_text("a file is in the way")
        error = self.refused("memory_unavailable", self.store.add_fact, "My gym is on 5th")
        self.assertEqual((error.status, str(error)), (503, memory_store.UNAVAILABLE))

    def test_clear_deletes_facts_and_suggestions_and_keeps_settings(self):
        self.store.add_fact("Kate Bell is my sister")
        self.store.add_fact("My gym is on 5th", pinned=True)
        self.store.propose("I prefer aisle seats")
        self.store.update_settings({"useInTasks": False})
        with sqlite3.connect(self.store.path) as conn:
            conn.executescript("CREATE TABLE routines(id TEXT PRIMARY KEY, name TEXT);"
                               "CREATE TABLE routine_versions(routine_id TEXT REFERENCES routines(id), "
                               "trajectory TEXT);"
                               "INSERT INTO routines VALUES ('old', 'Old routine words');"
                               "INSERT INTO routine_versions VALUES ('old', 'Old trajectory words');")
        self.assertEqual(self.store.clear(), {"facts": 2, "proposals": 1})
        self.assertEqual((self.store.count(), self.store.list_proposals(status="all")), (0, []))
        self.assertEqual(self.store.settings()["useInTasks"], False)
        with sqlite3.connect(self.store.path) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables, {"facts", "proposals", "settings", "forgotten", "staged", "run_use", "projects"})
        raw = self.store.path.read_bytes()
        self.assertNotIn(b"Kate Bell", raw)                       # vacuumed: the words are gone from the file
        self.assertNotIn(b"Old routine words", raw)
        self.assertNotIn(b"Old trajectory words", raw)

    def test_forgotten_words_are_overwritten_in_the_file(self):
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

    def test_a_store_made_with_a_looser_mode_tightens(self):
        self.store.add_fact("Kate Bell is my sister")
        os.chmod(self.store.path, 0o644)
        os.chmod(self.store.folder, 0o755)
        reopened = MemoryStore(self.store.folder)
        self.assertEqual(len(reopened.list_facts()), 1)
        self.assertEqual(stat.S_IMODE(os.stat(reopened.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(reopened.folder).st_mode), 0o700)

    def test_an_existing_database_symlink_refuses_reads_and_writes(self):
        self.store.add_fact("Use plain words")
        linked = MemoryStore(self.store.folder.parent / "linked")
        linked.folder.mkdir()
        linked.path.symlink_to(self.store.path)
        for operation in (linked.list_facts, lambda: linked.add_fact("Use paragraphs")):
            with self.subTest(operation=operation):
                self.refused("memory_unavailable", operation)
        self.assertEqual([fact["text"] for fact in self.store.list_facts()], ["Use plain words"])
        self.assertTrue(linked.path.is_symlink())

    def test_folder_and_file_are_private(self):
        self.store.add_fact("Kate Bell is my sister")
        self.assertEqual(stat.S_IMODE(os.stat(self.store.folder).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.path).st_mode), 0o600)
        with sqlite3.connect(self.store.path) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables, {"facts", "proposals", "settings", "forgotten", "staged", "run_use", "projects"})

    def test_a_newer_file_is_left_alone(self):
        self.store.add_fact("Kate Bell is my sister")
        with sqlite3.connect(self.store.path) as conn:
            conn.execute("PRAGMA user_version = 3")
        for call in (self.store.list_facts, self.store.count, lambda: self.store.add_fact("Hi there")):
            error = self.refused("newer_data", call)
            self.assertEqual((error.status, str(error)),
                             (503, "This was saved by a newer Mobster. Update Mobster to use it."))

    def test_two_connections_at_once(self):
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


GYM = "My gym is the one on 5th Street"
REMEMBER = "Remember that my gym is the one on 5th Street"


def scratch_memory(testcase):
    folder = Path(tempfile.mkdtemp(prefix="mobster-memory-")) / "memory"
    os.environ["MOBSTER_MEMORY_DIR"] = str(folder)   # the test's patch.dict of the environment restores it
    return MemoryStore(folder)


class Sink:

    def __init__(self):
        self.items, self.updates = [], []

    def append(self, thread_id, item):
        stored = {**item, "id": f"{len(self.items) + 1:012x}", "seq": len(self.items)}
        self.items.append((thread_id, stored))
        return stored

    def update(self, thread_id, item_id, changes):
        self.updates.append((thread_id, item_id, changes))
        return {"id": item_id, **changes}


class ApiBase(Base):
    def setUp(self):
        super().setUp()
        isolate(self)
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"
        self.store = scratch_memory(self)
        self.sink = Sink()
        sink = patch.object(harness_api, "_thread_sink", [(self.sink.append, self.sink.update, None)])
        sink.start()
        self.addCleanup(sink.stop)
        self.app = self.runtime()
        self.handler = make_handler(self.app)

    def call(self, method, path, body=None, **kwargs):
        return request(self.handler, method, path, body, **kwargs)

    def bus(self, start):
        return [e for e in self.app.bus.since({"memory"}, start)]


class RouteTests(ApiBase):
    def test_learned_facts_are_kept_forgotten_previewed_and_read_from_the_run(self):
        learned = self.store.learn("Insights is under Professional dashboard", "path", "app:com.burbn.instagram", "run",
                                   run_id="r1")["fact"]
        status, data, _ = self.call("GET", "/api/memory/new")
        self.assertEqual((status, [f["id"] for f in data["facts"]]), (200, [learned["id"]]))
        status, data, _ = self.call("GET", "/api/memory/preview?goal=read+insights&app=com.burbn.instagram")
        self.assertEqual((status, [f["id"] for f in data["facts"]]), (200, [learned["id"]]))
        self.assertEqual(self.store.get_fact(learned["id"])["uses"], 0)
        status, data, _ = self.call("GET", "/api/memory/run?runId=r1")
        self.assertEqual((status, data["learnedCount"], data["used"]), (200, 1, []))
        status, data, _ = self.call("POST", f"/api/memory/facts/{learned['id']}/keep")
        self.assertEqual((status, data["fact"]["kept"], data["fact"]["new"]), (200, True, False))
        status, data, _ = self.call("POST", f"/api/memory/facts/{learned['id']}/forget")
        self.assertEqual((status, data["forgotten"]), (200, True))
        self.assertEqual(self.store.learn(learned["text"], "path", learned["scope"], "run")["status"], "forgotten")
        self.assertEqual(self.call("POST", f"/api/memory/facts/{learned['id']}/forget")[0], 404)
        status, data, _ = self.call("POST", "/api/memory/looked")
        self.assertEqual(status, 200)

    def test_a_project_is_linked_and_its_file_read(self):
        folder = Path(tempfile.mkdtemp(prefix="mobster-project-")) / "firefox"
        (folder / ".mobster").mkdir(parents=True)
        (folder / ".mobster" / "memory.yaml").write_text("version: 1\nfacts:\n"
                                                         "  - {kind: repo, text: The scheme is Firefox}\n")
        status, data, _ = self.call("POST", "/api/memory/projects", {"id": "abc123def456", "path": str(folder)})
        self.assertEqual((status, data["imported"]), (200, 1))
        status, data, _ = self.call("GET", "/api/memory/facts?scope=project:abc123def456")
        self.assertEqual([f["text"] for f in data["facts"]], ["The scheme is Firefox"])
        self.assertEqual(self.call("POST", "/api/memory/projects", {"id": "abc123def456", "path": ""})[0], 400)

    def test_facts_round_trip(self):
        start = self.app.bus.position()
        status, data, _ = self.call("GET", "/api/memory/facts")
        self.assertEqual((status, data["facts"], data["count"]), (200, [], 0))
        self.assertEqual(data["limits"]["perTask"], 12)
        self.assertFalse(self.store.exists())                     # reading never creates the file
        status, data, _ = self.call("POST", "/api/memory/facts", {"text": GYM})
        self.assertEqual(status, 201)
        gym = data["fact"]
        status, data, _ = self.call("POST", "/v1/memory/facts", {"text": "Sign my texts with – Sam",
                                                                  "scope": "app:com.apple.MobileSMS", "pinned": True})
        texts = data["fact"]
        self.assertEqual((status, texts["app"]["name"], texts["pinned"]), (201, "Messages", True))
        status, data, _ = self.call("GET", "/api/memory/facts?scope=global")
        self.assertEqual([f["id"] for f in data["facts"]], [gym["id"]])
        status, data, _ = self.call("GET", "/api/memory/facts?q=texts")
        self.assertEqual([f["id"] for f in data["facts"]], [texts["id"]])
        status, data, _ = self.call("POST", f"/api/memory/facts/{gym['id']}",
                                    {"pinned": True, "text": "My gym is on 6th"})
        self.assertEqual((status, data["fact"]["text"], data["fact"]["pinned"]), (200, "My gym is on 6th", True))
        status, data, _ = self.call("DELETE", f"/api/memory/facts/{gym['id']}")
        self.assertEqual((status, data), (200, {"deleted": True}))
        status, data, _ = self.call("DELETE", f"/api/memory/facts/{gym['id']}")
        self.assertEqual((status, data["code"]), (404, "not_found"))
        self.assertEqual([(e["event"], e["op"]) for e in self.bus(start)],
                         [("fact_changed", "added"), ("fact_changed", "added"), ("fact_changed", "updated"),
                          ("fact_changed", "deleted")])

    def test_refusals_are_sentences_with_codes(self):
        cases = [({"text": "My bank password is hunter2!"}, 400, "secret",
                  "Mobster doesn't remember passwords, codes or card numbers."),
                 ({"text": "x" * 301}, 400, "too_long", "Keep it to 300 characters."),
                 ({"text": ""}, 400, "invalid_text", "Write what Mobster should remember."),
                 ({"text": "Fine", "scope": "app:nope"}, 400, "invalid_scope", None),
                 ({"text": "Fine", "pinned": "yes"}, 400, "invalid_field", None),
                 ({"text": "Fine", "id": "aaaaaaaaaaaa"}, 400, "invalid_field", "Unsupported field id.")]
        for body, status, code, message in cases:
            with self.subTest(body=body):
                got, data, _ = self.call("POST", "/api/memory/facts", body)
                self.assertEqual((got, data["code"]), (status, code))
                if message:
                    self.assertEqual(data["error"], message)
        self.call("POST", "/api/memory/facts", {"text": GYM})
        status, data, _ = self.call("POST", "/api/memory/facts", {"text": GYM.lower() + "."})
        self.assertEqual((status, data["code"], data["fact"]["text"]), (409, "duplicate", GYM))
        status, _, _ = self.call("POST", "/api/memory/facts", {"text": "x" * 5000})
        self.assertEqual(status, 413)
        self.assertEqual(self.store.count(), 1)

    def test_settings_clear_and_export(self):
        status, data, _ = self.call("GET", "/api/memory/settings")
        self.assertEqual((status, data["settings"], data["count"]), (200, {"useInTasks": True, "suggest": True}, 0))
        status, data, _ = self.call("POST", "/api/memory/settings", {"suggest": False})
        self.assertEqual(data["settings"], {"useInTasks": True, "suggest": False})
        status, data, _ = self.call("POST", "/api/memory/settings", {"useRoutines": True})
        self.assertEqual((status, data["code"]), (400, "invalid_field"))
        self.call("POST", "/api/memory/facts", {"text": GYM})
        status, data, headers = self.call("GET", "/api/memory/export")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/markdown; charset=utf-8"))
        self.assertIn(f"- {GYM}", data.decode())
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, data, _ = self.call("POST", "/api/memory/clear", {"confirm": "yes"})
        self.assertEqual((status, data["code"]), (400, "confirm_required"))
        self.assertEqual(self.store.count(), 1)
        status, data, _ = self.call("POST", "/api/memory/clear", {"confirm": "delete everything"})
        self.assertEqual((status, data["deleted"]), (200, {"facts": 1, "proposals": 0}))
        self.assertEqual(self.store.count(), 0)

    def test_no_home_folder_means_memory_is_unavailable(self):
        with patch.dict(os.environ, {}, clear=True):
            status, data, _ = self.call("GET", "/api/memory/facts")
        self.assertEqual((status, data["code"]), (503, "memory_unavailable"))


class SuggestionTests(ApiBase):
    def start(self, goal, origin="app", thread=None):
        field = {"threadId": (lambda value, runtime: value, True, None, True)}
        with patch.dict(harness_api._run_fields, field):
            run = self.app.create("messages", goal, "live", origin=origin,
                                  extras={"threadId": thread} if thread else None)
        self.app.listeners.flush(5)
        return run

    def test_a_goal_a_person_typed_becomes_a_suggestion_never_a_fact(self):
        start = self.app.bus.position()
        run = self.start(REMEMBER, thread="3f9c2a1b7d0e")
        (proposal,) = self.store.list_proposals()
        self.assertEqual((proposal["text"], proposal["runId"], proposal["threadId"]), (GYM, run.id, "3f9c2a1b7d0e"))
        self.assertEqual(self.store.count(), 0)                        # nothing remembered yet
        ((thread, item),) = self.sink.items
        self.assertEqual((thread, item["kind"], item["proposalId"], item["text"], item["status"]),
                         ("3f9c2a1b7d0e", "memory_proposal", proposal["id"], GYM, "pending"))
        self.assertEqual([e["event"] for e in self.bus(start)], ["proposal_created"])
        status, data, _ = self.call("GET", "/api/memory/proposals?thread=3f9c2a1b7d0e")
        self.assertEqual([p["id"] for p in data["proposals"]], [proposal["id"]])
        status, data, _ = self.call("POST", f"/api/memory/proposals/{proposal['id']}", {})
        self.assertEqual((status, data["code"]), (400, "invalid_answer"))
        status, data, _ = self.call("POST", f"/api/memory/proposals/{proposal['id']}", {"accept": True})
        self.assertEqual((status, data["proposal"]["status"], data["fact"]["text"]), (200, "accepted", GYM))
        self.assertEqual(self.sink.updates, [("3f9c2a1b7d0e", item["id"], {"status": "accepted", "text": GYM,
                                                                           "factId": data["fact"]["id"]})])
        self.assertEqual(self.store.count(), 1)
        status, data, _ = self.call("POST", f"/api/memory/proposals/{proposal['id']}", {"accept": True})
        self.assertEqual((status, data["code"]), (409, "not_pending"))

    def test_another_agents_words_are_never_suggested(self):
        for origin in ("mcp", "api", "workflow", "schedule"):
            with self.subTest(origin=origin):
                self.finish(self.app, self.start(REMEMBER, origin=origin))
        self.assertFalse(self.store.exists())
        self.assertEqual(self.sink.items, [])

    def test_the_switch_stops_suggestions(self):
        self.store.update_settings({"suggest": False})
        self.start(REMEMBER)
        self.assertEqual(self.store.list_proposals(), [])

    def test_a_message_sent_while_the_task_works_can_suggest(self):
        run = self.start("Text Kate I'm on my way")
        self.assertEqual(self.store.list_proposals(), [])
        run.emit({"event": "user_message", "id": "m1", "text": "btw my sister's name is Kate Bell", "source": "app"})
        run.emit({"event": "user_message", "id": "m2", "text": "Remember I prefer short texts", "source": "mcp"})
        self.app.listeners.flush(5)
        self.assertEqual([p["text"] for p in self.store.list_proposals()], ["My sister's name is Kate Bell"])

    def test_a_secret_is_never_suggested(self):
        self.start("Remember that my gate code is 4821")
        self.assertFalse(self.store.exists())


class TaskTests(SmartBase):

    def setUp(self):
        super().setUp()
        self.store = scratch_memory(self)

    def test_the_prompt_carries_the_facts_for_tasks_a_person_started(self):
        gym = self.store.add_fact(GYM)
        self.store.add_fact("Kate Bell is my sister")
        runtime = self.runtime()
        run = runtime.create("messages", "When does my gym close today?", "live", origin="app")
        script = self.script()
        self.work(runtime, run, script)
        self.assertIn("What the user asked Mobster to remember (their words; the screen wins when it disagrees):\n"
                      f"- {GYM}", script.prompts[0])
        self.assertNotIn("Kate Bell", script.prompts[0])
        used = next(e for e in run.events if e["event"] == "memory_used")
        self.assertEqual(used["ids"], [gym["id"]])
        self.assertNotIn(GYM, str(run.events))                        # the run's journal names facts by id only
        self.finish(runtime, run)
        other = runtime.create("messages", "When does my gym close today?", "live")       # the HTTP API, no origin
        script = self.script()
        self.work(runtime, other, script)
        self.assertNotIn(GYM, script.prompts[0])
        self.assertFalse(any(e["event"] == "memory_used" for e in other.events))


def fact(id, text, scope="global", pinned=False, updated=0.0, used=None):
    return {"id": id, "text": text, "scope": scope, "pinned": int(pinned), "updated_at": updated,
            "last_used_at": used, "uses": 0}


class SelectTests(unittest.TestCase):
    def test_shared_words_pick_the_facts(self):
        facts = [fact("a", "My gym is the one on 5th Street"), fact("b", "Kate Bell is my sister"),
                 fact("c", "I like my coffee with oat milk")]
        self.assertEqual([f["id"] for f in retrieval.select(facts, "When does my gym close today?")], ["a"])
        self.assertEqual([f["id"] for f in retrieval.select(facts, "Text my sister I'm late")], ["b"])
        self.assertEqual([f["id"] for f in retrieval.select(facts, "Text Kate that I'm on my way")], ["b"])
        self.assertEqual(retrieval.select(facts, "Turn on Dark Mode"), [])

    def test_pinned_always_go_and_come_first(self):
        facts = [fact("a", "Call me Sam", pinned=True), fact("b", "My gym is on 5th"),
                 fact("c", "Sign my texts with – Sam", "app:com.apple.MobileSMS", pinned=True)]
        self.assertEqual([f["id"] for f in retrieval.select(facts, "Turn on Dark Mode")], ["a"])
        self.assertEqual([f["id"] for f in retrieval.select(facts, "when does my gym open",
                                                            ["com.apple.MobileSMS"])], ["a", "c", "b"])

    def test_an_apps_facts_go_only_when_that_app_is_in_play(self):
        facts = [fact("m", "Sign my texts with – Sam", "app:com.apple.MobileSMS"),
                 fact("x", "Use the shared calendar", "app:com.example.planner")]
        self.assertEqual(retrieval.select(facts, "Text Sam hello"), [])
        self.assertEqual([f["id"] for f in retrieval.select(facts, "Text Sam hello", ["com.apple.MobileSMS"])],
                         ["m"])
        self.assertEqual([f["id"] for f in retrieval.select(facts, "Open Messages and text Sam")], ["m"])
        self.assertEqual(retrieval.select(facts, "Plan my week on the shared calendar"), [])  # name unknown
        from mobile_agent import catalog
        with patch.dict(catalog._EXTRA_NAMES, {"com.example.planner": "Planner"}):
            self.assertEqual([f["id"] for f in retrieval.select(facts, "In Planner, plan my week")], ["x"])


class Runs(dict):
    pass


def context(origin="app", goal="When does my gym close?", app=None, thread=None, runtime=None, run_id="aaaaaaaaaaaa"):
    events = []
    ctx = harness_api.RunContext(run_id=run_id, goal=goal, engine="smart", origin=origin, app_bundle=app,
                                 device_id=None, device_kind=None,
                                 extras=harness_api.frozen_mapping({"threadId": thread} if thread else {}),
                                 data_dir=None, emit=events.append, clarify=lambda r: "", cancelled=lambda: False,
                                 runtime=runtime)
    return ctx, events


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mobster-memory-")) / "memory"
        env = patch.dict(os.environ, {"MOBSTER_MEMORY_DIR": str(self.folder)})
        env.start()
        self.addCleanup(env.stop)
        self.store = MemoryStore(self.folder)

    def test_nothing_remembered_means_no_provider_and_no_file(self):
        ctx, _ = context()
        self.assertIsNone(memory_provider.factory(ctx))
        self.assertFalse(self.folder.exists())

    def test_only_tasks_a_person_started_or_scheduled_get_memory(self):
        self.store.add_fact("My gym is the one on 5th Street")
        for origin, expected in (("app", True), ("tui", True), ("cli", True), ("workflow", True),
                                 ("schedule", True), ("mcp", False), ("api", False)):
            with self.subTest(origin=origin):
                ctx, _ = context(origin)
                self.assertEqual(memory_provider.factory(ctx) is not None, expected)

    def test_the_block_names_its_facts_by_id_and_records_their_use(self):
        gym = self.store.add_fact("My gym is the one on 5th Street")
        self.store.add_fact("Kate Bell is my sister")
        ctx, events = context()
        provider = memory_provider.factory(ctx)
        self.assertEqual((provider.key, provider.max_chars), ("memory", 1200))
        (block,) = provider.blocks(ctx)
        self.assertEqual((block.key, block.stable, block.untrusted), ("memory", True, False))
        self.assertEqual(block.title, "What the user asked Mobster to remember (their words; the screen wins when it "
                                      "disagrees)")
        self.assertEqual(block.text, "- My gym is the one on 5th Street")
        self.assertEqual(events, [{"event": "memory_used", "ids": [gym["id"]]}])
        self.assertEqual(self.store.get_fact(gym["id"])["uses"], 1)
        self.assertEqual(provider.turn_blocks(ctx, None), ())
        ctx, events = context(goal="Turn on Dark Mode")
        self.assertEqual(provider.blocks(ctx), ())
        self.assertEqual(events, [])

    def test_the_switch_turns_it_off(self):
        self.store.add_fact("Call me Sam", pinned=True)
        self.store.update_settings({"useInTasks": False})
        ctx, events = context()
        self.assertEqual(memory_provider.factory(ctx).blocks(ctx), ())
        self.assertEqual(events, [])

    def test_the_conversations_last_app_is_in_play(self):
        self.store.add_fact("Sign my texts with – Sam", "app:com.apple.MobileSMS")
        earlier = SimpleNamespace(id="bbbbbbbbbbbb", created_at=10.0, extras={"threadId": "3f9c2a1b7d0e"},
                                  app={"bundleId": None}, events=[{"event": "plan", "apps": ["Messages"]}])
        older = SimpleNamespace(id="cccccccccccc", created_at=5.0, extras={"threadId": "3f9c2a1b7d0e"},
                                app={"bundleId": "com.apple.mobilecal"}, events=[])
        other = SimpleNamespace(id="dddddddddddd", created_at=20.0, extras={"threadId": "000000000000"},
                                app={"bundleId": "com.apple.Maps"}, events=[])
        runtime = SimpleNamespace(runs={r.id: r for r in (earlier, older, other)}, lock=None)
        ctx, _ = context(goal="Now reply to her: see you at 7", thread="3f9c2a1b7d0e", runtime=runtime)
        self.assertEqual(memory_provider.thread_apps(ctx), ("com.apple.MobileSMS",))
        (block,) = memory_provider.factory(ctx).blocks(ctx)
        self.assertEqual(block.text, "- In Messages: Sign my texts with – Sam")
        ctx, _ = context(goal="Now reply to her: see you at 7", runtime=runtime)
        self.assertEqual(memory_provider.factory(ctx).blocks(ctx), ())


SUGGESTS = [
    ("Remember that my gym is the one on 5th Street", [Candidate("My gym is the one on 5th Street")]),
    ("remember my gym is the one on 5th Street.", [Candidate("My gym is the one on 5th Street")]),
    ("Please remember: my dentist is Dr. Lee on Valencia St. near the park.",
     [Candidate("My dentist is Dr. Lee on Valencia St. near the park")]),
    ("Can you remember that Kate Bell is my sister", [Candidate("Kate Bell is my sister")]),
    ("Don't forget that I'm vegetarian", [Candidate("I'm vegetarian")]),
    ("keep in mind I always fly from SFO", [Candidate("I always fly from SFO", pin=True)]),
    ("my sister's name is Kate Bell", [Candidate("My sister's name is Kate Bell")]),
    ("Hey, my favorite coffee is an oat flat white", [Candidate("My favorite coffee is an oat flat white")]),
    ("My kids are Sam and Alex", [Candidate("My kids are Sam and Alex")]),
    ("I always take the window seat", [Candidate("I always take the window seat", pin=True)]),
    ("I prefer short replies.", [Candidate("I prefer short replies", pin=True)]),
    ("I'd rather take the train", [Candidate("I'd rather take the train", pin=True)]),
    ("Call me Sam", [Candidate("Call me Sam", pin=True)]),
    ("Book a table for 2. I prefer quiet places. My partner is vegetarian.",
     [Candidate("I prefer quiet places", pin=True), Candidate("My partner is vegetarian")]),
    ("I prefer aisle seats. I never eat shellfish. My gym is on 5th.",       # at most two per message
     [Candidate("I prefer aisle seats", pin=True), Candidate("I never eat shellfish", pin=True)]),
]

SUGGESTS_NOTHING = [
    "",
    "Text Sam that I'm 10 minutes late",
    "What time does my gym close today?",
    "Is my package here yet",
    "My battery is low, can you check",
    "my order is late",
    "my coffee is cold",
    "my phone is at 20%",
    "Text Sam that my train is late",
    "Open Messages and tell her I always run late",
    "I never got your text",
    "Tell Kate to call me Monday",
    "call me when you are done",
    "Remember to buy milk",
    "Can you remember what my gym is called?",
    "Find my keys",
    "my PIN is 4821",
    "remember the gate code is 4821",
    "my wifi password is hunter2!",
    "Remember my card 4242 4242 4242 4242",
    "Remember x",                                   # too short to be worth asking
    "my problem is that it won't open",
]


class ExtractTests(unittest.TestCase):
    def test_what_each_sentence_suggests(self):
        for text, expected in SUGGESTS:
            with self.subTest(text=text):
                self.assertEqual(candidates(text), expected)

    def test_what_suggests_nothing(self):
        for text in SUGGESTS_NOTHING:
            with self.subTest(text=text):
                self.assertEqual(candidates(text), [])

    def test_a_long_message_full_of_abbreviations_stays_fast(self):
        import time
        for text in ("Dr. " * 1000, "St. " * 990 + "Remember that my gym is on 5th"):
            started = time.perf_counter()
            candidates(text)
            self.assertLess(time.perf_counter() - started, 0.15)
        self.assertEqual(candidates("I saw Dr. Lee. My dentist is Dr. Lee on Valencia St. by the park"),
                         [Candidate("My dentist is Dr. Lee on Valencia St. by the park")])


if __name__ == "__main__":
    unittest.main()
