import contextlib
import datetime
import os
import plistlib
import re
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from mobile_agent.notify_triggers import rules, sources, watch
from mobile_agent.notify_triggers.sources import MAC_EPOCH, Notification, SourceError

SCHEMA = """
CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier VARCHAR, badge INTEGER NULL);
CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB, request_date REAL,
                     request_last_date REAL, delivered_date REAL, presented Bool, style INTEGER, snooze_fire_date REAL);
"""


class MacDB:
    def __init__(self, path):
        self.path = Path(path)
        self.writer = sqlite3.connect(self.path, isolation_level=None)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.executescript(SCHEMA)
        self.apps = {}

    def post(self, app, title, body="", subtitle="", *, when=None, rec_id=None, uuid=None):
        if app not in self.apps:
            self.writer.execute("INSERT INTO app (identifier) VALUES (?)", (app,))
            self.apps[app] = self.writer.execute("SELECT max(app_id) FROM app").fetchone()[0]
        when = time.time() if when is None else when
        uuid = uuid or os.urandom(16)
        data = plistlib.dumps({"app": app, "date": when - MAC_EPOCH, "orig": 4, "srce": os.urandom(16), "uuid": uuid,
                               "styl": 1, "intl": True,
                               "req": {"titl": title, "subt": subtitle, "body": body, "thre": "t1", "cate": "c1",
                                       "dest": 7}}, fmt=plistlib.FMT_BINARY)
        self.writer.execute("INSERT INTO record (rec_id, app_id, uuid, data, request_date, delivered_date, presented, "
                            "style) VALUES (?, ?, ?, ?, ?, ?, 0, 1)",
                            (rec_id, self.apps[app], uuid, data, when - MAC_EPOCH, when - MAC_EPOCH))
        return uuid

    def close(self):
        self.writer.close()


def archive(root):
    objects = ["$null"]
    classes = {}

    def klass(name):
        if name not in classes:
            objects.append({"$classname": name, "$classes": [name, "NSObject"]})
            classes[name] = plistlib.UID(len(objects) - 1)
        return classes[name]

    def add(value):
        if value is None:
            return plistlib.UID(0)
        index = len(objects)
        objects.append(None)
        if isinstance(value, dict):
            keys, values = [add(k) for k in value], [add(v) for v in value.values()]
            objects[index] = {"NS.keys": keys, "NS.objects": values, "$class": klass("NSDictionary")}
        elif isinstance(value, list):
            objects[index] = {"NS.objects": [add(v) for v in value], "$class": klass("NSArray")}
        elif isinstance(value, datetime.datetime):
            objects[index] = {"NS.time": value.timestamp() - MAC_EPOCH, "$class": klass("NSDate")}
        else:
            objects[index] = value
        return plistlib.UID(index)

    top = {"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": add(root)}, "$objects": objects}
    return plistlib.dumps(top, fmt=plistlib.FMT_BINARY)


def phone_entry(identifier, title, body, when=None):
    when = datetime.datetime.fromtimestamp(time.time() if when is None else when, datetime.timezone.utc)
    return {"AppNotificationIdentifier": identifier, "AppNotificationTitle": title, "AppNotificationCreationDate": when,
            "SBSPushStoreNotificationThreadKey": "thread-1", "SBSPushStoreNotificationCategoryKey": "MESSAGE",
            "ShouldPlaySound": True,
            "EventBehavior": {"eventDetails": {"body": body, "subtitle": "", "bundleIdentifier": "ignored.when.mapped",
                                               "title": title, "identifier": identifier}}}


class PhoneStore:
    FOLDER = "11111111-2222-3333-4444-555555555555"

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True)
        (self.root / "RemoteDeviceInfo.plist").write_bytes(plistlib.dumps({"RemoteDeviceIdentifier": "<UDID>"}))
        self.map({"com.example.delivery": self.FOLDER})

    def map(self, mapping):
        (self.root / "Library.plist").write_bytes(archive(mapping))
        for folder in mapping.values():
            (self.root / folder).mkdir(exist_ok=True)

    def deliver(self, entries, folder=FOLDER):
        path = self.root / folder / "DeliveredNotifications.plist"
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(archive(entries))
        os.replace(temporary, path)


def note(app="com.example.delivery", title="Courier", body="Your order is here", source="iphone", ident="n1",
         date=None):
    return Notification(app=app, title=title, subtitle="", body=body,
                        date=time.time() if date is None else date, source=source, id=ident)


class FakeSource:
    name = "fake"

    def __init__(self, batches=()):
        self.batches = list(batches)
        self.primed = False

    def prime(self):
        self.primed = True

    def poll(self):
        return self.batches.pop(0) if self.batches else []

    def close(self):
        pass


def rule(name="Order is here", app="com.example.*", match="is here", per_hour=6, enabled=True):
    return rules.Rule(name=name, app=app, pattern=re.compile(match, re.IGNORECASE) if match else None, source="any",
                      per_hour=per_hour, enabled=enabled)


class Actor:
    def __init__(self):
        self.acted = []

    def act(self, rule, notification, masked=False):
        self.acted.append((rule.name, notification, masked))


def run_watcher(batches, rule_list, per_hour=30, subscriber=None):
    events, actor = [], Actor()
    watcher = watch.Watcher([FakeSource(batches)], rules.RuleSet(tuple(rule_list), per_hour), actor,
                            listener=lambda event, fields: events.append((event, fields)), subscriber=subscriber)
    watcher.start()
    for _ in batches:
        watcher.poll()
    watcher.close()
    return events, actor.acted


class TempCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="notify-triggers-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class MacSourceTests(TempCase):
    def setUp(self):
        super().setUp()
        self.db = MacDB(self.dir / "db")
        self.addCleanup(self.db.close)

    def test_it_starts_from_now_and_reads_what_lands_after(self):
        self.db.post("com.example.old", "Old", "history")
        source = sources.MacSource(self.dir / "db")
        source.prime()
        self.assertEqual(source.poll(), [])
        self.db.post("com.apple.scripteditor2", "Courier", "Your order is here", "spike")
        found = source.poll()
        self.assertEqual([(n.app, n.title, n.subtitle, n.body, n.source) for n in found],
                         [("com.apple.scripteditor2", "Courier", "spike", "Your order is here", "mac")])
        self.assertAlmostEqual(found[0].date, time.time(), delta=5)
        self.assertEqual(source.poll(), [])
        source.close()

    def test_a_row_dated_before_the_watcher_never_fires(self):
        source = sources.MacSource(self.dir / "db")
        source.prime()
        self.db.post("com.example.app", "Late write", "an hour old", when=time.time() - 3600)
        self.assertEqual(source.poll(), [])
        source.close()

    def test_a_reused_rec_id_is_still_a_new_notification(self):
        source = sources.MacSource(self.dir / "db")
        source.prime()
        self.db.post("com.example.app", "One", rec_id=7)
        self.assertEqual([n.title for n in source.poll()], ["One"])
        self.db.writer.execute("DELETE FROM record WHERE rec_id = 7")
        self.db.post("com.example.app", "Two", rec_id=7)
        self.assertEqual([n.title for n in source.poll()], ["Two"])
        source.close()

    def test_it_never_writes_the_database(self):
        source = sources.MacSource(self.dir / "db")
        source.prime()
        with self.assertRaises(sqlite3.OperationalError):
            source.connection.execute("DELETE FROM record")
        source.close()

    def test_a_corrupt_or_recreated_database_stops_reads_then_resumes(self):
        source = sources.MacSource(self.dir / "db")
        source.prime()
        self.db.close()
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(f"{self.dir / 'db'}{suffix}")
        (self.dir / "db").write_bytes(b"not a database" * 100)
        with self.assertRaises(SourceError):
            source.poll()
        os.remove(self.dir / "db")
        with self.assertRaises(SourceError):
            source.poll()
        self.db = MacDB(self.dir / "db")
        self.db.post("com.example.app", "Before the new start", when=time.time() - 3600)
        self.db.post("com.example.app", "After")
        self.assertEqual([n.title for n in source.poll()], ["After"])
        source.close()


class IPhoneSourceTests(TempCase):
    def setUp(self):
        super().setUp()
        self.store = PhoneStore(self.dir / "Remote" / "default")

    def test_it_starts_from_now_and_reads_new_forwarded_notifications(self):
        self.store.deliver([phone_entry("a", "Courier", "Yesterday's order")])
        source = sources.IPhoneSource(self.store.root)
        source.prime()
        self.assertEqual(source.poll(), [])
        self.store.deliver([phone_entry("a", "Courier", "Yesterday's order"),
                            phone_entry("b", "Courier", "Your order is here")])
        found = source.poll()
        self.assertEqual([(n.app, n.title, n.body, n.source, n.id) for n in found],
                         [("com.example.delivery", "Courier", "Your order is here", "iphone", "b")])
        self.assertEqual(source.poll(), [])

    def test_a_new_app_folder_and_a_cleared_one(self):
        source = sources.IPhoneSource(self.store.root)
        source.prime()
        folder = "99999999-2222-3333-4444-555555555555"
        self.store.map({"com.example.delivery": PhoneStore.FOLDER, "com.example.chat": folder})
        self.store.deliver([phone_entry("c", "Sam", "Are you around?")], folder)
        self.assertEqual([(n.app, n.title) for n in source.poll()], [("com.example.chat", "Sam")])
        os.remove(self.store.root / folder / "DeliveredNotifications.plist")
        self.assertEqual(source.poll(), [])
        self.store.deliver([phone_entry("c", "Sam", "Are you around?")], folder)
        self.assertEqual([n.id for n in source.poll()], ["c"])

    def test_old_and_unreadable_entries_are_skipped(self):
        source = sources.IPhoneSource(self.store.root)
        source.prime()
        self.store.deliver([phone_entry("old", "Courier", "last week", when=time.time() - 7 * 86400), {"junk": 1}])
        self.assertEqual(source.poll(), [])
        (self.store.root / PhoneStore.FOLDER / "DeliveredNotifications.plist").write_bytes(b"half written")
        self.assertEqual(source.poll(), [])

    def test_a_missing_store_is_a_source_error(self):
        with self.assertRaises(SourceError) as caught:
            sources.IPhoneSource(self.dir / "nowhere").prime()
        self.assertIn("iPhone", caught.exception.message)


class WatcherTests(unittest.TestCase):
    def test_a_matching_rule_acts_once_per_notification(self):
        _events, acted = run_watcher([[note()], [note()]], [rule()])
        self.assertEqual([name for name, _n, _masked in acted], ["Order is here"])

    def test_a_subscriber_hears_each_new_notification_once_masked_and_its_failure_stops_nothing(self):
        heard = []

        def subscriber(notification):
            heard.append(notification)
            if len(heard) == 1:
                raise RuntimeError("down")
        events, _acted = run_watcher([[note(body="Code 551203", ident="a"), note(body="Again", ident="a"),
                                       note(body="Next", ident="b")]], [rule()], subscriber=subscriber)
        self.assertEqual([n.body for n in heard], ["Code ••••••", "Next"])
        self.assertIn("subscriber_failed", [fields.get("reason") for _event, fields in events])

    def test_the_rate_limit_refuses_past_the_rules_hourly_limit_and_the_global_one(self):
        events, acted = run_watcher([[note(ident=f"n{i}") for i in range(8)]], [rule(per_hour=6)])
        self.assertEqual(len(acted), 6)
        self.assertEqual([fields["reason"] for event, fields in events if event == "refused"], ["rate_limited"] * 2)
        events, acted = run_watcher([[note(ident=f"m{i}") for i in range(3)]], [rule(), rule(name="Second")],
                                    per_hour=4)
        self.assertEqual(len(acted), 4)
        self.assertEqual([event for event, _fields in events], ["refused"] * 2)

    def test_the_limiter_frees_up_after_an_hour(self):
        limiter = watch.Limiter(per_hour=30)
        one = rule(per_hour=1)
        self.assertTrue(limiter.allow(one, 1000.0))
        self.assertFalse(limiter.allow(one, 1000.0 + 3599))
        self.assertTrue(limiter.allow(one, 1000.0 + 3600))

    def test_a_one_time_code_is_masked_before_any_rule_sees_it(self):
        events, acted = run_watcher([[note(title="Bank", body="Your code is 551203. Don't share it.")]],
                                    [rule(name="Code", match="551203"), rule(name="Bank", match="code")])
        self.assertEqual([(name, masked) for name, _n, masked in acted], [("Bank", True)])
        self.assertNotIn("551203", acted[0][1].text())
        self.assertIn("Your code is ••••••", acted[0][1].body)

    def test_the_loop_guard_refuses_mobsters_own_notifications(self):
        events, acted = run_watcher([[note(app="app.mobster.desktop", body="Task done: order is here", source="mac"),
                                      note(app="app.mobster.desktop.qaflows", body="is here", source="mac",
                                           ident="2")]], [rule(app="*")])
        self.assertEqual(acted, [])
        self.assertEqual([(event, fields["reason"]) for event, fields in events], [("refused", "loop_guard")] * 2)

    def test_an_action_that_raises_stops_nothing(self):
        class Broken(Actor):
            def act(self, rule, notification, masked=False):
                super().act(rule, notification, masked)
                raise RuntimeError("down")
        actor = Broken()
        watcher = watch.Watcher([FakeSource([[note(ident="a"), note(ident="b")]])], rules.RuleSet((rule(),)), actor,
                                listener=lambda *_: None)
        watcher.start()
        watcher.poll()
        watcher.close()
        self.assertEqual(len(actor.acted), 2)

    def test_a_source_error_is_said_once_and_its_recovery_too(self):
        class Flaky(FakeSource):
            def __init__(self):
                super().__init__()
                self.results = [SourceError("gone"), SourceError("gone"), [note()]]

            def poll(self):
                result = self.results.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
        events = []
        watcher = watch.Watcher([Flaky()], rules.RuleSet(()), Actor(),
                                listener=lambda event, fields: events.append(event))
        watcher.start()
        for _ in range(3):
            watcher.poll()
        watcher.close()
        self.assertEqual(events, ["source_error", "source_ok"])
