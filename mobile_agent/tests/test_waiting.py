import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mobile_agent import result_blocks, share
from mobile_agent.api_errors import APIError
from mobile_agent.notify_triggers.sources import MAC_EPOCH
from mobile_agent.server import make_handler
from mobile_agent.tests.seam_support import request
from mobile_agent.tests.test_server_engine import MESSAGES, Base
from mobile_agent.waiting import contacts, drafts, job, messages, rank

NOW = 1_800_000_000.0
HOUR, DAY = 3600, 86400
SESSION = "s" * 40


def blob(text):
    raw = text.encode("utf-8")
    size = len(raw)
    length = bytes([size]) if size < 0x80 else b"\x81" + size.to_bytes(2, "little") if size < 0x10000 \
        else b"\x82" + size.to_bytes(4, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00"
            b"\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + length + raw +
            b"\x86\x84\x02iI\x01\x05\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01\x92\x84\x96\x96\x1d")


class FakeChatDb:
    def __init__(self, directory):
        self.path = Path(directory) / "chat.db"
        self.db = sqlite3.connect(self.path)
        self.db.executescript("""
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT, display_name TEXT, style INTEGER);
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB, handle_id INTEGER,
                is_from_me INTEGER, date INTEGER, item_type INTEGER DEFAULT 0, associated_message_type INTEGER DEFAULT 0,
                cache_has_attachments INTEGER DEFAULT 0);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        """)  # noqa: E501
        self.handles = self.chats = self.rows = 0

    def chat(self, handle, style=45, extra_handles=()):
        self.chats += 1
        self.db.execute("INSERT INTO chat VALUES (?,?,?,?,?)", (self.chats, f"g{self.chats}", handle, None, style))
        for one in (handle, *extra_handles):
            self.handles += 1
            self.db.execute("INSERT INTO handle VALUES (?,?,?)", (self.handles, one, "iMessage"))
            self.db.execute("INSERT INTO chat_handle_join VALUES (?,?)", (self.chats, self.handles))
        return self.chats

    def say(self, chat, text, ago, mine=False, body=None, kind=0, item=0, attached=0):
        self.rows += 1
        date = int((NOW - ago - MAC_EPOCH) * 1_000_000_000)
        self.db.execute("INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (self.rows, f"m{self.rows}", None if body else text, blob(text) if body else None, 1,
                         int(mine), date, item, kind, attached))
        self.db.execute("INSERT INTO chat_message_join VALUES (?,?)", (chat, self.rows))
        return self.rows

    def commit(self):
        self.db.commit()
        return self.path


def build(directory, spec):
    fake = FakeChatDb(directory)
    for handle, lines in spec.items():
        chat = fake.chat(handle)
        for text, ago, mine in lines:
            fake.say(chat, text, ago, mine)
    return fake.commit()


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)


class DecodeTests(unittest.TestCase):
    def test_the_string_in_an_attributed_body(self):
        for text in ("Are you free tonight?", "café \U0001F600 你好", "x" * 127, "y" * 128, "z" * 700, "a"):
            with self.subTest(length=len(text)):
                self.assertEqual(messages.decode_attributed_body(blob(text)), text)

    def test_blobs_that_do_not_hold_a_string_give_none(self):
        for value in (None, b"", b"garbage", b"NSString", b"NSString\x01\x94\x84\x01+",
                      b"NSString\x01\x94\x84\x01+\x05ab",
                      b"NSString\x01\x94\x84\x01+\x02\xff\xfe"):
            with self.subTest(value=value):
                self.assertIsNone(messages.decode_attributed_body(value))
        decoy = b"NSString-not-a-payload \x00\x00" + blob("the real words")
        self.assertEqual(messages.decode_attributed_body(decoy), "the real words")

    def test_the_attachment_placeholder_is_not_text(self):
        self.assertEqual(messages.message_text(None, blob("￼ look at this")), "look at this")
        self.assertEqual(messages.message_text("plain", blob("other")), "plain")


class ReaderTests(Temp):
    def test_reads_text_and_attributed_body_oldest_first(self):
        fake = FakeChatDb(self.dir)
        chat = fake.chat("+15550000001")
        fake.say(chat, "first one", 3 * DAY)
        fake.say(chat, "words only in the blob", 2 * DAY, body=True)
        fake.say(chat, "my answer", DAY, mine=True)
        chats = messages.read_chats(fake.commit(), NOW)
        self.assertEqual(len(chats), 1)
        self.assertEqual([(m.text, m.mine) for m in chats[0].messages],
                         [("first one", False), ("words only in the blob", False), ("my answer", True)])
        self.assertEqual(chats[0].handle, "+15550000001")
        self.assertAlmostEqual(chats[0].messages[0].when, NOW - 3 * DAY, delta=1)

    def test_a_reaction_is_not_a_message(self):
        fake = FakeChatDb(self.dir)
        chat = fake.chat("+15550000002")
        fake.say(chat, "can you send it?", 3 * HOUR)
        fake.say(chat, "Loved “can you send it?”", HOUR, mine=True, kind=2000)
        fake.say(chat, "Liked “hello”", HOUR)
        fake.say(chat, "", HOUR, kind=3001)
        chats = messages.read_chats(fake.commit(), NOW)
        self.assertEqual([m.text for m in chats[0].messages], ["can you send it?"])

    def test_system_items_and_empty_messages_are_skipped_but_an_attachment_counts(self):
        fake = FakeChatDb(self.dir)
        chat = fake.chat("+15550000003")
        fake.say(chat, "renamed the chat", HOUR, item=2)
        fake.say(chat, "", 2 * HOUR)
        fake.say(chat, "", 3 * HOUR, attached=1)
        chats = messages.read_chats(fake.commit(), NOW)
        self.assertEqual([m.text for m in chats[0].messages], [messages.ATTACHMENT_TEXT])

    def test_group_chats_are_not_read(self):
        fake = FakeChatDb(self.dir)
        group = fake.chat("chat123", style=43, extra_handles=("+15550000005", "+15550000006"))
        fake.say(group, "group plan?", 3 * HOUR)
        two_handles = fake.chat("+15550000007", extra_handles=("+15550000008",))
        fake.say(two_handles, "who is this", 3 * HOUR)
        solo = fake.chat("+15550000009")
        fake.say(solo, "hi", 3 * HOUR)
        chats = messages.read_chats(fake.commit(), NOW)
        self.assertEqual([c.handle for c in chats], ["+15550000009"])

    def test_old_chats_and_old_messages_are_left_out(self):
        fake = FakeChatDb(self.dir)
        stale = fake.chat("+15550000010")
        fake.say(stale, "ancient", 60 * DAY)
        mixed = fake.chat("+15550000011")
        fake.say(mixed, "old", 45 * DAY)
        fake.say(mixed, "recent", DAY)
        chats = messages.read_chats(fake.commit(), NOW)
        self.assertEqual([[m.text for m in c.messages] for c in chats], [["recent"]])

    def test_it_keeps_the_newest_messages_of_a_busy_chat(self):
        fake = FakeChatDb(self.dir)
        chat = fake.chat("+15550000012")
        for index in range(60):
            fake.say(chat, f"m{index}", (60 - index) * HOUR)
        (one,) = messages.read_chats(fake.commit(), NOW, per_chat=10)
        self.assertEqual([m.text for m in one.messages], [f"m{i}" for i in range(50, 60)])

    def test_the_database_opens_read_only_and_is_left_exactly_as_it_was(self):
        path = build(self.dir, {"+15550000013": [("hello", 3 * HOUR, False)]})
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        seen = []
        real = sqlite3.connect

        def spy(target, *args, **kwargs):
            seen.append(target)
            return real(target, *args, **kwargs)
        with patch("mobile_agent.waiting.messages.sqlite3.connect", side_effect=spy):
            messages.read_chats(path, NOW)
        self.assertTrue(seen and all("mode=ro" in one and one.startswith("file:") for one in seen), seen)
        connection = messages.connect(path)
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute("INSERT INTO handle VALUES (99, 'x', 'SMS')")
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute("DELETE FROM message")
        connection.close()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["chat.db"])

    def test_a_missing_file_does_not_get_created(self):
        with self.assertRaises(sqlite3.OperationalError):
            messages.read_chats(self.dir / "nope.db", NOW)
        self.assertFalse((self.dir / "nope.db").exists())


def address_book(directory, name="AddressBook-v22.abcddb"):
    path = Path(directory) / name
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZNICKNAME TEXT, ZORGANIZATION TEXT);
        CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
        CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT);
        INSERT INTO ZABCDRECORD VALUES (1, 'Sam', 'Rivera', NULL, NULL), (2, NULL, NULL, 'Mom', NULL), (3, NULL, NULL, NULL, 'Corner Bakery');
        INSERT INTO ZABCDPHONENUMBER VALUES (1, '(555) 010-0101'), (2, '+1 555-010-0909'), (3, '5550100303'), (1, '12');
        INSERT INTO ZABCDEMAILADDRESS VALUES (1, 'Sam.Rivera@example.com');
    """)  # noqa: E501
    db.commit()
    db.close()
    return path


class ContactsTests(Temp):
    def test_phone_numbers_match_on_the_last_ten_digits_and_emails_exactly(self):
        book = contacts.ContactBook.load([address_book(self.dir)])
        self.assertEqual(book.name("+15550100101"), "Sam Rivera")
        self.assertEqual(book.name("5550100101"), "Sam Rivera")
        self.assertEqual(book.name("+1 (555) 010-0909"), "Mom")
        self.assertEqual(book.name("+15550100303"), "Corner Bakery")
        self.assertEqual(book.name("sam.rivera@example.com"), "Sam Rivera")
        self.assertIsNone(book.name("sam@example.com"))
        self.assertIsNone(book.name("+15559999999"))
        self.assertIsNone(book.name("12"))
        self.assertIsNone(book.name(""))

    def test_a_missing_or_broken_database_is_skipped(self):
        (self.dir / "broken.abcddb").write_bytes(b"not a database")
        book = contacts.ContactBook.load([self.dir / "broken.abcddb", self.dir / "none.abcddb"])
        self.assertIsNone(book.name("+15550100101"))
        self.assertFalse((self.dir / "none.abcddb").exists())

    def test_sources_are_found_under_the_address_book_folder(self):
        source = self.dir / "Sources" / "ABC-123"
        source.mkdir(parents=True)
        address_book(source)
        self.assertEqual([p.parent.name for p in contacts.database_paths(self.dir)], ["ABC-123"])


def chat_of(handle, *lines, rowid=1):
    fake = [messages.Message(i + 1, mine, NOW - ago, text, text == messages.ATTACHMENT_TEXT)
            for i, (text, ago, mine) in enumerate(lines)]
    return messages.Chat(rowid, handle, fake)


def bucket_of(*lines, handle="+15550000001"):
    item = rank.classify(chat_of(handle, *lines), NOW)
    return item.bucket if item else None


class RankTests(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(bucket_of(("did you get the deck?", 5 * HOUR, False)), "asked")
        self.assertEqual(bucket_of(("Can you call me when you're free", 5 * HOUR, False)), "asked")
        self.assertEqual(bucket_of(("let me know about sunday", 5 * HOUR, False)), "asked")
        self.assertEqual(bucket_of(("the landlord says the keys are ready", 5 * HOUR, False)), "waiting")
        self.assertEqual(bucket_of(("I'll send the photos tomorrow", 2 * DAY, True)), "promised")
        self.assertEqual(bucket_of(("let me check", 2 * DAY, True)), "promised")

    def test_a_reply_means_nobody_is_waiting(self):
        self.assertIsNone(bucket_of(("are you coming?", 5 * HOUR, False), ("yes, on my way", 4 * HOUR, True)))
        self.assertIsNone(bucket_of(("thanks for dinner", 5 * HOUR, True)))

    def test_the_two_hour_floor_and_the_thirty_day_window(self):
        self.assertIsNone(bucket_of(("are you free?", HOUR + 59 * 60, False)))
        self.assertEqual(bucket_of(("are you free?", 2 * HOUR + 1, False)), "asked")
        self.assertEqual(bucket_of(("are you free?", 29 * DAY, False)), "asked")
        self.assertIsNone(bucket_of(("are you free?", 31 * DAY, False)))
        self.assertIsNone(bucket_of(("I'll send it tomorrow", HOUR, True)))
        self.assertIsNone(bucket_of(("I'll send it tomorrow", 31 * DAY, True)))

    def test_closers_are_not_waiting(self):
        for text in ("ok", "Okay!", "thanks", "Thank you so much", "thx", "lol", "haha", "\U0001F44D",
                     "\U0001F602\U0001F602",
                     "sounds good", "got it", "will do", "love you", "ok thanks!", "..."):
            with self.subTest(text=text):
                self.assertIsNone(bucket_of((text, 5 * HOUR, False)))
        for text in ("ok?", "thanks, can you also send the file", "okay but what about Sunday", "where are the keys"):
            with self.subTest(text=text):
                self.assertIsNotNone(bucket_of((text, 5 * HOUR, False)))

    def test_short_codes_and_business_senders_are_not_people(self):
        for handle in ("12345", "+1234", "22000", "no-reply@shop.example", "noreply@bank.example",
                       "donotreply@x.example",
                       "AMAZON", "urn:biz:abc123", "", "+12"):
            with self.subTest(handle=handle):
                self.assertIsNone(bucket_of(("your code is 4821, did you request it?", 5 * HOUR, False), handle=handle))
        for handle in ("+15550000001", "sam@example.com", "5550000001"):
            with self.subTest(handle=handle):
                self.assertEqual(bucket_of(("are you free?", 5 * HOUR, False), handle=handle), "asked")

    def test_a_promise_kept_with_a_file_is_not_open(self):
        self.assertIsNone(bucket_of(("I'll send the photos tomorrow", 3 * DAY, True),
                                    (messages.ATTACHMENT_TEXT, 2 * DAY, True)))
        self.assertEqual(bucket_of(("I'll send the photos tomorrow", 3 * DAY, True),
                                   ("also, how was it", 2 * DAY, True)),
                         "promised")
        self.assertEqual(bucket_of(("I'll send the photos tomorrow", 3 * DAY, True), ("great, thanks", 2 * DAY, False)),
                         None)
        self.assertIsNone(bucket_of(("let me know if you need anything", 3 * DAY, True)))

    def test_order_and_cap(self):
        chats = [chat_of("+15550000100", ("the keys are ready", 3 * DAY, False), rowid=1),
                 chat_of("+15550000101", ("are you free?", 2 * DAY, False), rowid=2),
                 chat_of("+15550000102", ("are you free?", 9 * DAY, False), rowid=3),
                 chat_of("+15550000103", ("I'll send it tomorrow", 6 * DAY, True), rowid=4),
                 chat_of("+15550000104", ("ok", 3 * DAY, False), rowid=5)]
        items = rank.rank(chats, NOW)
        self.assertEqual([(i.chat, i.bucket) for i in items],
                         [(3, "asked"), (2, "asked"), (1, "waiting"), (4, "promised")])
        many = [chat_of(f"+1555000{n:04d}", ("are you free?", (n + 3) * HOUR, False), rowid=n + 1) for n in range(40)]
        self.assertEqual(len(rank.rank(many, NOW)), rank.MAX_ITEMS)
        self.assertEqual(len(rank.rank(many, NOW, limit=3)), 3)


class FakeRunner:
    def __init__(self, available=True, reason="", answers=None):
        self.available, self.reason, self.answers, self.requests = available, reason, answers or {}, []

    def status(self):
        return {"available": self.available, "reason": self.reason}

    def run(self, request, per_draft, total):
        self.requests.append(request)
        for item in request["items"]:
            if item["id"] in self.answers:
                yield {"id": item["id"], **self.answers[item["id"]]}


class DraftTests(unittest.TestCase):
    def items(self):
        lines = [(f"line {n}", (30 - n) * HOUR, n % 2 == 0)
                 for n in range(12)] + [("can you look at the deck?", 3 * HOUR, False)]
        return [rank.classify(chat_of("+15550000001", *lines, rowid=7), NOW),
                rank.classify(chat_of("+15550000002", ("secret of another chat?", 4 * HOUR, False), rowid=8), NOW)]

    def test_each_draft_sees_only_its_chat_and_at_most_eight_messages(self):
        runner = FakeRunner(answers={"7": {"ok": True, "draft": "Let me check and get back to you."}})
        out = drafts.draft_all(self.items(), runner=runner)
        self.assertEqual(out, {"7": "Let me check and get back to you."})
        sent = {one["id"]: one for one in runner.requests[0]["items"]}
        self.assertEqual(len(sent["7"]["messages"]), 8)
        self.assertNotIn("another chat", json.dumps(sent["7"]))
        self.assertEqual(sent["7"]["messages"][-1], {"from": "them", "text": "can you look at the deck?"})
        self.assertEqual(sent["7"]["kind"], "asked")
        self.assertTrue(all(m["from"] in ("you", "them") for m in sent["7"]["messages"]))

    def test_a_draft_is_one_short_line(self):
        self.assertEqual(drafts.clean('  "Let me\ncheck."  '), "Let me check.")
        self.assertLessEqual(len(drafts.clean("word " * 200)), drafts.MAX_DRAFT)

    def test_failures_and_timeouts_leave_the_draft_empty(self):
        runner = FakeRunner(answers={"7": {"ok": False, "error": "timeout"}, "8": {"ok": True, "draft": ""}})
        self.assertEqual(drafts.draft_all(self.items(), runner=runner), {})

    def test_drafts_that_commit_the_person_are_dropped(self):
        for text in ("Yes, Friday at 8 works", "I can do 7:30", "Sure, $40 is fine", "I'll be there!", "No.",
                     "See you tomorrow",
                     "I'm in", "Sounds good on March 5"):
            with self.subTest(text=text):
                runner = FakeRunner(answers={"7": {"ok": True, "draft": text}})
                self.assertEqual(drafts.draft_all(self.items()[:1], runner=runner), {})
        for text in ("Let me check and get back to you.", "Thanks for sending this, I'll take a look soon.",
                     "Congrats, that's great news!"):
            with self.subTest(text=text):
                runner = FakeRunner(answers={"7": {"ok": True, "draft": text}})
                self.assertEqual(drafts.draft_all(self.items()[:1], runner=runner), {"7": text})

    def test_no_helper_means_no_drafts_and_no_fallback(self):
        with patch("mobile_agent.waiting.drafts._run_helper", return_value=None):
            self.assertEqual(drafts.status(), {"available": False, "reason": "no_helper"})
            self.assertEqual(drafts.draft_all(self.items()), {})

    def test_the_helper_gets_message_text_on_stdin_only(self):
        popen = Mock()
        popen.stdin = io.StringIO()
        popen.stdin.close = Mock()
        popen.stdout = io.StringIO('{"id": "7", "ok": true, "draft": "Let me check."}\n')
        popen.poll.return_value = 0
        with patch("mobile_agent.waiting.drafts._run_helper", return_value="/x/draft-reply"), \
                patch("mobile_agent.waiting.drafts.subprocess.Popen", return_value=popen) as launched, \
                patch("selectors.DefaultSelector") as selector:
            selector.return_value.select.return_value = [object()]
            out = drafts.draft_all(self.items()[:1])
        self.assertEqual(out, {"7": "Let me check."})
        self.assertEqual(launched.call_args.args[0][:2], ["/x/draft-reply", "run"])
        self.assertNotIn("deck", json.dumps(launched.call_args.args))


class NoNetwork:
    def __enter__(self):
        def refuse(*args, **kwargs):
            raise AssertionError("the job opened a network connection")
        self.patches = [patch("socket.socket.connect", refuse), patch("socket.create_connection", refuse),
                        patch("socket.getaddrinfo", refuse), patch("urllib.request.urlopen", refuse),
                        patch("http.client.HTTPConnection.connect", refuse),
                        patch("http.client.HTTPSConnection.connect", refuse)]
        for one in self.patches:
            one.start()
        return self

    def __exit__(self, *args):
        for one in self.patches:
            one.stop()


SPEC = {
    "+15550100101": [("are you free sunday?", 3 * DAY, False)],
    "+15550100202": [("the keys are ready", 2 * DAY, False)],
    "+15550100303": [("I'll send the photos tomorrow", 6 * DAY, True)],
    "+15550100404": [("ok thanks", 3 * HOUR, False)],
    "12345": [("your code is 123456, did you request it?", 3 * HOUR, False)],
}


class JobTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-000000000000"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = build(self.dir, SPEC)
        self.book = contacts.ContactBook.load([address_book(self.dir)])
        self.runtime_ = self.runtime(apps=(MESSAGES,))

    def make(self, runner=None, **kwargs):
        runner = runner or FakeRunner(answers={"1": {"ok": True, "draft": "Let me check and get back to you."},
                                               "2": {"ok": True, "draft": "Thanks for letting me know."},
                                               "3": {"ok": True, "draft": "Sorry, I haven't forgotten."}})
        with NoNetwork():
            return job.run(self.runtime_, now=NOW, path=self.path, book=self.book, runner=runner, **kwargs)

    def queue(self, run):
        (block,) = run.summary["blocks"]["blocks"]
        return block

    def test_a_finished_task_with_a_valid_review_queue(self):
        run = self.make()
        self.assertEqual((run.status, run.goal, run.app["id"], run.origin), ("completed", job.GOAL, "messages", "app"))
        self.assertIsNotNone(run.finished_at)
        self.assertNotIn("blocks_problems", run.summary)
        envelope, problems = result_blocks.validate(run.summary["blocks"])
        self.assertEqual(problems, [])
        block = self.queue(run)
        self.assertEqual((block["type"], block["action"]), ("review_queue", "send_message"))
        self.assertEqual([b["id"] for b in block["buckets"]], ["asked", "waiting", "promised"])
        by_to = {item["to"]: item for item in block["items"]}
        self.assertEqual(set(by_to), {"Sam Rivera", "+15550100202", "Corner Bakery"})
        sam = by_to["Sam Rivera"]
        self.assertEqual((sam["bucket"], sam["app"], sam["bundleId"], sam["handle"]),
                         ("asked", "Messages", "com.apple.MobileSMS", "+15550100101"))
        self.assertEqual((sam["lastMessage"], sam["context"]), ("are you free sunday?", "Asked you 3 days ago"))
        self.assertEqual(sam["draft"], "Let me check and get back to you.")
        self.assertNotIn("handle", by_to["+15550100202"])
        self.assertEqual(by_to["Corner Bakery"]["bucket"], "promised")
        self.assertTrue(by_to["Corner Bakery"]["lastMessage"].startswith("You wrote: "))
        self.assertEqual(run.summary["actions"], 0)
        self.assertIsNone(run.summary["model"])

    def test_the_answer_counts_and_names_nobody(self):
        run = self.make()
        answer = run.summary["answer"]
        self.assertIn("1 person asked you something", answer)
        self.assertIn("1 person is waiting on a reply", answer)
        self.assertIn("1 promise to keep", answer)
        self.assertIn("Nothing was sent", answer)
        for private in ("Sam", "Rivera", "Bakery", "5550100", "Sunday", "keys"):
            self.assertNotIn(private, answer)

    def test_no_model_and_no_network_are_used(self):
        runner = FakeRunner()
        with patch("mobile_agent.model_clients.chat_client") as chat, patch(
                "mobile_agent.model_clients.AnthropicChat") as anthropic, \
                patch("mobile_agent.model_clients.OpenAIChat") as openai, patch(
                    "mobile_agent.model_clients.GeminiChat") as gemini:
            run = self.make(runner)
        for client in (chat, anthropic, openai, gemini):
            client.assert_not_called()
        self.assertEqual(run.cost_usd, None)
        self.assertEqual(run.events[-1]["event"], "run_finished")
        self.assertFalse(any(e["event"].startswith("inference") for e in run.events))

    def test_the_on_device_model_is_off_so_items_have_no_draft_and_the_answer_says_so_once(self):
        run = self.make(FakeRunner(available=False, reason="apple_intelligence_off"))
        self.assertFalse(any(item.get("draft") for item in self.queue(run)["items"]))
        self.assertEqual(run.summary["answer"].count("Apple Intelligence"), 1)
        self.assertIn("you'll write your own replies", run.summary["answer"])

    def test_nobody_waiting_is_a_plain_result_without_a_queue(self):
        other = Path(tempfile.mkdtemp(dir=self.dir))
        path = build(other, {"+15550100505": [("ok", 4 * HOUR, False)]})
        with NoNetwork():
            run = job.run(self.runtime_, now=NOW, path=path, book=self.book, runner=FakeRunner())
        self.assertNotIn("blocks", run.summary)
        self.assertIn("Nobody is waiting on you", run.summary["answer"])

    def test_approve_uses_the_existing_send_path_and_sends_nothing_itself(self):
        run = self.make()
        item = next(i for i in self.queue(run)["items"] if i["to"] == "Sam Rivera")
        before = set(self.runtime_.runs)
        answer = self.runtime_.decide_block_item(run.id, "waiting", item["id"], "approve", "Sorry, can we talk later?")
        self.assertEqual(answer["state"], "approved")
        follow = self.runtime_.runs[answer["runId"]]
        self.assertEqual(set(self.runtime_.runs) - before, {follow.id})
        self.assertIn("“Sorry, can we talk later?”", follow.goal)
        self.assertIn("Sam Rivera (+15550100101)", follow.goal)
        self.assertEqual(follow.app["id"], "messages")
        self.assertEqual(follow.preapproved[0]["to"], "Sam Rivera")
        self.assertEqual(follow.preapproved[0]["text"], "Sorry, can we talk later?")
        self.assertEqual(follow.status, "queued")
        again = self.runtime_.decide_block_item(run.id, "waiting", item["id"], "approve")
        self.assertEqual(again["runId"], follow.id)
        self.assertTrue(again["replayed"])

    def test_only_an_item_the_job_left_without_a_draft_asks_for_the_persons_words(self):
        run = self.make(FakeRunner(answers={"1": {"ok": True, "draft": "Let me check and get back to you."}}))
        items = self.queue(run)["items"]
        drafted, bare = [i for i in items if i["draft"]], [i for i in items if not i["draft"]]
        self.assertTrue(drafted and bare)
        for item in drafted:
            self.assertNotIn("yourWords", item)
        for item in bare:
            self.assertIs(item["yourWords"], True)

    def test_a_your_words_item_sends_exactly_what_the_person_typed_to_that_person(self):
        run = self.make(FakeRunner(available=False, reason="apple_intelligence_off"))
        item = next(i for i in self.queue(run)["items"] if i["to"] == "Sam Rivera")
        self.assertTrue(item["yourWords"] and not item["draft"])
        answer = self.runtime_.decide_block_item(run.id, "waiting", item["id"], "approve",
                                                 "  Sorry, I got buried today.  ")
        follow = self.runtime_.runs[answer["runId"]]
        self.assertIn("“Sorry, I got buried today.”", follow.goal)
        self.assertIn("Sam Rivera (+15550100101)", follow.goal)
        self.assertEqual((follow.preapproved[0]["to"], follow.preapproved[0]["text"]),
                         ("Sam Rivera", "Sorry, I got buried today."))

    def test_a_your_words_item_still_refuses_an_empty_reply(self):
        run = self.make(FakeRunner(available=False, reason="apple_intelligence_off"))
        item = self.queue(run)["items"][0]
        before = set(self.runtime_.runs)
        for text in (None, "", "   ", "\n\t "):
            with self.subTest(text=text), self.assertRaises(APIError) as refused:
                self.runtime_.decide_block_item(run.id, "waiting", item["id"], "approve", text)
            self.assertEqual((refused.exception.status, refused.exception.code), (400, "block_item_invalid"))
        self.assertEqual(set(self.runtime_.runs), before)
        self.assertEqual(run.block_state, {})

    def test_a_share_link_hides_people(self):
        run = self.make()
        public = run.public(include_events=False)
        page = share.page(public, people="mask").split("</style>", 1)[1]
        for private in ("Sam Rivera", "Corner Bakery", "5550100", "Sam"):
            self.assertNotIn(private, page)
        shown = share.page(public, people="show").split("</style>", 1)[1]
        self.assertIn("Sam Rivera", shown)

    def test_the_stored_result_keeps_only_what_the_card_shows(self):
        run = self.make()
        saved = json.dumps({item["id"]: item for item in self.runtime_.journal.load()}[run.id])
        for text in ("are you free sunday?", "Sam Rivera"):
            self.assertIn(text, saved)
        for text in ("your code is", "ok thanks", "123456"):
            self.assertNotIn(text, saved)
        self.assertEqual(sorted(self.queue(run)), sorted({"type", "id", "title", "action", "buckets", "items"}))

    def test_nothing_it_reads_reaches_the_log(self):
        with self.assertNoLogs(level="DEBUG"):
            self.make()

    def test_errors_in_plain_words(self):
        with self.assertRaises(APIError) as missing:
            job.run(self.runtime_, now=NOW, path=self.dir / "none.db", book=self.book, runner=FakeRunner())
        self.assertEqual((missing.exception.status, missing.exception.code), (404, "messages_missing"))
        self.assertIn("Messages in iCloud", str(missing.exception))
        os.chmod(self.path, 0)
        self.addCleanup(os.chmod, self.path, 0o600)
        if os.access(self.path, os.R_OK):
            self.skipTest("this account can read a file with no permissions")
        with self.assertRaises(APIError) as denied:
            job.run(self.runtime_, now=NOW, path=self.path, book=self.book, runner=FakeRunner())
        self.assertEqual((denied.exception.status, denied.exception.code), (403, "full_disk_access"))
        old = self.dir / "old.db"
        db = sqlite3.connect(old)
        db.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY)")
        db.commit()
        db.close()
        with self.assertRaises(APIError) as odd:
            job.run(self.runtime_, now=NOW, path=old, book=self.book, runner=FakeRunner())
        self.assertEqual(odd.exception.code, "messages_unreadable")

    def test_one_job_at_a_time(self):
        job._lock.acquire()
        try:
            with self.assertRaises(APIError) as busy:
                self.make()
            self.assertEqual(busy.exception.code, "waiting_busy")
        finally:
            job._lock.release()

    def test_the_same_key_gives_the_same_task(self):
        first = self.make(key="waiting-abc")
        runner = FakeRunner()
        with NoNetwork():
            second = job.run(self.runtime_, now=NOW, path=self.dir / "gone.db", book=self.book, runner=runner,
                             key="waiting-abc")
        self.assertEqual(first.id, second.id)
        self.assertEqual(runner.requests, [])


class RouteTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-000000000000"
        self.handler = make_handler(self.runtime(apps=(MESSAGES,)), app_session=SESSION)

    def call(self, body=None, session=SESSION):
        status, data, _ = request(self.handler, "POST", "/api/waiting", body if body is not None else {},
                                  headers={"X-Mobster-App-Session": session} if session else None)
        return status, data

    def test_only_the_mac_app_starts_it_and_a_bad_key_is_refused(self):
        with patch("mobile_agent.waiting.job.run") as ran:
            for session in (None, "x" * 40):
                status, body = self.call(session=session)
                self.assertEqual((status, body["code"]), (403, "app_only"))
            self.assertEqual(self.call({"key": 5})[0], 400)
            ran.assert_not_called()

    def test_it_starts_a_finished_task(self):
        made = Mock(id="abcdef012345")
        with patch("mobile_agent.waiting.job.run", return_value=made) as ran:
            status, body = self.call({"key": "k1"})
        self.assertEqual((status, body), (201, {"runId": "abcdef012345"}))
        self.assertEqual(ran.call_args.kwargs["key"], "waiting-k1")


if __name__ == "__main__":
    unittest.main()
