"""Track files: attachments on this Mac. The magic-byte allowlist, names, the store and its caps, what is derived
from each kind of file, clean-up, the run field, and the HTTP routes. Offline: the doc-text helper and the phone's
tools are fakes (test_files_support)."""

import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mobile_agent import harness_api
from mobile_agent.attachments import extract, service, sniff, store as store_module
from mobile_agent.attachments.store import AttachmentStore, StoreError
from mobile_agent.journal import Journal
from mobile_agent.server import make_handler
from mobile_agent.tests.seam_support import isolate, request
from mobile_agent.tests.test_files_support import (FakeHelper, FakePhone, MENU_PAGES, make_jpeg, make_pdf, make_png,
                                                   usb_record)
from mobile_agent.tests.test_server_engine import Base

HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64
EXE = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff" + b"\x00" * 64 + b"This program cannot be run in DOS mode"
ELF = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64


def helper(fake):
    """Patches extract's helper with ``fake`` (a FakeHelper)."""
    return patch.multiple(extract, helper_path=lambda: Path("/fake/doc-text"), run_helper=fake)


class SniffTests(unittest.TestCase):
    def test_the_allowlist_reads_the_bytes_not_the_name(self):
        cases = [
            (make_png(), "x.png", "image/png", "image"),
            (make_jpeg(40, 30), "x.jpg", "image/jpeg", "image"),
            (b"GIF89a" + b"\x00" * 20, "x.gif", "image/gif", "image"),
            (WEBP, "x.webp", "image/webp", "image"),
            (HEIC, "IMG_0001.HEIC", "image/heic", "image"),
            (make_pdf(["Hello"]), "menu.pdf", "application/pdf", "pdf"),
            (make_pdf(["Hello"]), "menu.txt", "application/pdf", "pdf"),          # the bytes win over the name
            (make_png(), "menu.pdf", "image/png", "image"),
            (b"a,b\n1,2\n", "data.csv", "text/csv", "csv"),
            (b"a\tb\n1\t2\n", "data.tsv", "text/tab-separated-values", "csv"),
            (b'{"a": 1}', "x.json", "application/json", "text"),
            (b'{"a": ', "x.json", "text/plain", "text"),                           # not JSON: plain text
            (b"# Notes\n- milk", "notes.md", "text/markdown", "text"),
            ("Café crème ☕".encode(), "notes", "text/plain", "text"),
            (b"\xef\xbb\xbfwith a BOM", "bom.txt", "text/plain", "text"),
        ]
        for data, name, mime, kind in cases:
            with self.subTest(name=name, mime=mime):
                found = sniff.sniff(data, name)
                self.assertEqual((found.mime, found.kind), (mime, kind))

    def test_programs_and_binary_files_are_refused_whatever_their_name(self):
        for data, name in ((EXE, "menu.pdf"), (EXE, "setup.exe"), (ELF, "notes.txt"), (b"\x00\x01\x02", "a.csv"),
                           ("hello".encode("utf-16"), "utf16.txt"), (b"\xff\xfe\xfd\xfc" * 10, "x.jpg"),
                           (bytes(range(1, 32)) * 10, "controls.txt")):
            with self.subTest(name=name, head=data[:4]), self.assertRaises(sniff.Unsupported):
                sniff.sniff(data, name)

    def test_names_stay_inside_their_folder(self):
        cases = {"../../etc/passwd": "passwd", "/Users/you/Desktop/menu.pdf": "menu.pdf",
                 "C:\\Users\\you\\menu.pdf": "menu.pdf", "a\x00b\x07c.pdf": "abc.pdf", "..": "file", "": "file",
                 ".": "file", "-rf.pdf": "rf.pdf", "  spaced   out .txt ": "spaced out .txt",
                 "evil\u202efdp.exe": "evilfdp.exe", ".hidden": "hidden", "nul\x00": "nul",
                 "Cafe\u0301.pdf": "Café.pdf"}
        for raw, clean in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sniff.clean_name(raw), clean)
        long = sniff.clean_name("x" * 300 + ".pdf")
        self.assertEqual((len(long), long[-4:]), (sniff.NAME_LIMIT, ".pdf"))

    def test_a_name_gets_the_extension_its_type_needs(self):
        self.assertEqual(sniff.with_extension("scan", sniff.PDF), "scan.pdf")
        self.assertEqual(sniff.with_extension("photo.jpeg", sniff.IMAGE_TYPES["image/jpeg"]), "photo.jpeg")
        self.assertEqual(sniff.with_extension("notes", sniff.PLAIN), "notes")
        self.assertEqual(sniff.with_extension("server.log", sniff.PLAIN), "server.log")
        self.assertEqual(sniff.size_words(412_000), "412 KB")
        self.assertEqual(sniff.size_words(3_400_000), "3.4 MB")
        self.assertEqual(sniff.size_words(1), "1 byte")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-files-"))
        self.journal = Journal(self.root / "runs.sqlite3")
        self.addCleanup(self.journal.close)
        with self.journal.transaction() as connection:
            store_module.migrate(connection, 0)
        self.events = []
        self.now = [1_800_000_000.0]
        self.store = AttachmentStore(self.journal, self.root / "attachments", publish=self.events.append,
                                     clock=lambda: self.now[0])

    def add_run(self, run_id):
        with self.journal.transaction() as connection:
            connection.execute("INSERT INTO runs(id, data) VALUES (?, ?)", (run_id, json.dumps({"id": run_id})))

    def test_a_pdf_is_kept_with_its_text_and_thumbnail_private_to_this_user(self):
        with helper(FakeHelper()):
            attachment = self.store.create(make_pdf(MENU_PAGES), "Menu.pdf")
        self.assertEqual((attachment.kind, attachment.data["pages"], attachment.name), ("pdf", 2, "Menu.pdf"))
        public = attachment.public()
        self.assertEqual(public["thumbUrl"], f"/api/attachments/{attachment.id}/thumb")
        self.assertTrue(public["textAvailable"])
        self.assertEqual(set(public) >= {"id", "name", "mime", "bytes", "kind", "pages", "createdAt"}, True)
        folder = self.root / "attachments" / attachment.id
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "attachments").stat().st_mode), 0o700)
        for name in ("original.pdf", "text.txt", "thumb.jpg"):
            self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode) & 0o077, 0, name)
        self.assertIn("Mushroom risotto", self.store.text(attachment))
        self.assertEqual(self.events[-1]["event"], "attachment_created")
        self.assertEqual(self.store.get(attachment.id).data["sha256"], attachment.data["sha256"])

    def test_without_the_helper_a_pdf_is_kept_but_says_its_text_couldnt_be_read(self):
        with patch.object(extract, "helper_path", lambda: None):
            attachment = self.store.create(make_pdf(["Hi"]), "scan.pdf")
        self.assertFalse(attachment.data["textAvailable"])
        self.assertIsNone(attachment.public()["thumbUrl"])

    def test_refusals_say_why_with_a_status(self):
        with helper(FakeHelper(locked=True)), self.assertRaises(StoreError) as locked:
            self.store.create(make_pdf(["secret"]), "locked.pdf")
        self.assertEqual((locked.exception.status, locked.exception.code), (415, "locked_pdf"))
        for data, status, code in ((b"", 400, "empty_file"), (EXE, 415, "unsupported_type"),
                                   (b"a" * (store_module.FILE_MAX + 1), 413, "too_large")):
            with self.subTest(code=code), self.assertRaises(StoreError) as caught:
                self.store.create(data, "x.txt")
            self.assertEqual((caught.exception.status, caught.exception.code), (status, code))
        self.assertEqual(list((self.root / "attachments").iterdir()) if (self.root / "attachments").exists() else [],
                         [])  # a refused file leaves nothing behind

    def test_a_conversation_and_the_mac_each_have_a_cap(self):
        thread = "3f9c2a1b7d0e"
        with patch.object(store_module, "THREAD_MAX", 50):
            self.store.create(b"x" * 20, "a.txt", thread_id=thread)
            with self.assertRaises(StoreError) as full:
                self.store.create(b"y" * 20, "b.txt", thread_id=thread)
            self.assertEqual((full.exception.status, full.exception.code), (413, "thread_full"))
            self.store.create(b"y" * 20, "b.txt", thread_id="aaaaaaaaaaaa")  # another conversation has room
        with patch.object(store_module, "TOTAL_MAX", 100), self.assertRaises(StoreError) as mac:
            self.store.create(b"z" * 40, "c.txt")
        self.assertEqual(mac.exception.code, "storage_full")
        self.assertIn("1 GB", str(mac.exception))

    def test_csv_keeps_the_header_and_200_rows(self):
        rows = "name;price\n" + "".join(f"Dish {i};{i}\n" for i in range(450))
        attachment = self.store.create(rows.encode(), "menu.csv")
        self.assertEqual((attachment.kind, attachment.data["rows"], attachment.data["columns"]), ("csv", 450, 2))
        text = self.store.text(attachment)
        self.assertTrue(text.startswith("name;price\nDish 0;0\n"))
        self.assertIn("Dish 199;199", text)
        self.assertNotIn("Dish 200;200", text)
        self.assertIn("250 more rows", text)

    def test_images_get_a_thumbnail_a_picture_for_the_model_and_their_text(self):
        from PIL import Image
        transparent = Image.new("RGBA", (3000, 2000), (0, 0, 0, 0))
        import io
        out = io.BytesIO()
        transparent.save(out, "PNG")
        with helper(FakeHelper(ocr="Table for 4 at 7pm")):
            attachment = self.store.create(out.getvalue(), "note.png")
        model = self.store.path(attachment, "model")
        thumb = self.store.path(attachment, "thumb")
        with Image.open(model) as image:
            self.assertEqual(image.size, (1024, 683))
            self.assertEqual(image.getpixel((10, 10)), (255, 255, 255))  # transparency on white, not black
        with Image.open(thumb) as image:
            self.assertEqual(max(image.size), 480)
        self.assertEqual((attachment.data["width"], attachment.data["height"]), (3000, 2000))
        self.assertEqual(self.store.text(attachment), "Table for 4 at 7pm")

    def test_an_enormous_image_is_refused(self):
        with patch.object(extract, "MAX_PIXELS", 1000), self.assertRaises(StoreError) as caught:
            self.store.create(make_png(50, 50), "big.png")
        self.assertEqual((caught.exception.status, caught.exception.code), (413, "too_large"))

    def test_heic_goes_through_sips_then_upright_and_without_its_location(self):
        """sips keeps a photo's metadata (its location too) and an orientation tag; the pictures Mobster keeps, and
        the one the agent sends to the AI account, are upright and carry no metadata."""
        import io
        from PIL import Image
        calls = []

        def sips(argv, timeout=60):
            calls.append(argv)
            exif = Image.Exif()
            exif[0x0112] = 6                                          # rotate 90° to show it upright
            exif[0x010F] = "Apple"
            exif[0x8825] = {1: "N", 2: (37.0, 46.0, 30.0), 3: "W", 4: (122.0, 25.0, 10.0)}  # where it was taken
            out = io.BytesIO()
            Image.new("RGB", (1600, 1200), (30, 160, 90)).save(out, "JPEG", exif=exif.tobytes())
            Path(argv[-1]).write_bytes(out.getvalue())
            return 0
        with patch.object(extract, "run_sips", sips), patch.object(extract, "sips_path", lambda: "/usr/bin/sips"), \
                helper(FakeHelper(ocr="")):
            attachment = self.store.create(HEIC, "IMG_0001.HEIC")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index("format") + 1], "jpeg")
        self.assertTrue(attachment.data["thumb"] and attachment.data["model"])
        self.assertEqual(attachment.data["mime"], "image/heic")
        self.assertEqual((attachment.data["width"], attachment.data["height"]), (1200, 1600))
        for which, longest in (("model", 1024), ("thumb", 480)):
            with self.subTest(which=which), Image.open(self.store.path(attachment, which)) as image:
                self.assertEqual(image.size, (longest * 3 // 4, longest))   # upright: taller than wide
                self.assertEqual(len(image.getexif()), 0)                  # no location, no camera, no tag
                self.assertNotIn("exif", image.info)
        folder = self.root / "attachments" / attachment.id
        self.assertNotIn("heic-converted.jpg", [p.name for p in folder.iterdir()])  # the full-size copy is gone

    def test_a_heic_sips_cant_read_is_kept_without_pictures(self):
        with patch.object(extract, "run_sips", lambda argv, timeout=60: 1), \
                patch.object(extract, "sips_path", lambda: "/usr/bin/sips"), helper(FakeHelper(ocr="")):
            attachment = self.store.create(HEIC, "IMG_0002.HEIC")
        self.assertFalse(attachment.data["thumb"] or attachment.data["model"])

    def test_a_file_used_by_tasks_goes_with_the_last_of_them(self):
        a = self.store.create(b"first", "a.txt")
        b = self.store.create(b"second", "b.txt", thread_id="3f9c2a1b7d0e")
        self.add_run("aaaaaaaaaaa1")
        self.add_run("aaaaaaaaaaa2")
        self.store.attach([a.id, b.id], "aaaaaaaaaaa1")
        self.store.attach([a.id], "aaaaaaaaaaa2")
        self.assertEqual(self.store.get(a.id).run_ids, ("aaaaaaaaaaa1", "aaaaaaaaaaa2"))
        self.assertEqual(self.events[-1]["event"], "attachments_used")
        self.journal.delete("aaaaaaaaaaa1")
        self.store.expired(["aaaaaaaaaaa1"])
        self.assertIsNotNone(self.store.get(a.id))           # task 2 still uses it
        self.journal.delete("aaaaaaaaaaa2")
        self.store.expired(["aaaaaaaaaaa2"])
        self.assertIsNone(self.store.get(a.id))
        self.assertFalse((self.root / "attachments" / a.id).exists())
        self.assertIsNotNone(self.store.get(b.id))           # a conversation's file outlives its tasks

    def test_a_task_joins_its_unattached_files_to_its_conversation(self):
        a = self.store.create(b"first", "a.txt")
        self.add_run("aaaaaaaaaaa1")
        self.store.attach([a.id, "ffffffffffff", "not-an-id"], "aaaaaaaaaaa1", "3f9c2a1b7d0e")
        found = self.store.get(a.id)
        self.assertEqual((found.thread_id, found.run_id), ("3f9c2a1b7d0e", "aaaaaaaaaaa1"))

    def test_clean_up_deletes_unused_files_after_a_day_and_files_of_deleted_conversations(self):
        old = self.store.create(b"old", "old.txt")
        held = self.store.create(b"held", "held.txt")
        thread_file = self.store.create(b"t", "t.txt", thread_id="3f9c2a1b7d0e")
        kept_thread = self.store.create(b"k", "k.txt", thread_id="bbbbbbbbbbbb")
        self.now[0] += 3600
        fresh = self.store.create(b"fresh", "fresh.txt")
        orphan = self.root / "attachments" / "cccccccccccc"
        orphan.mkdir()
        os.utime(orphan, (self.now[0] - 7200, self.now[0] - 7200))
        self.now[0] += store_module.UNUSED_TTL - 60
        # No conversations here yet: their files stay.
        self.assertEqual(sorted(self.store.gc(referenced={held.id})), [old.id])
        with self.journal.transaction() as connection:
            connection.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, data TEXT, updated_at REAL)")
            connection.execute("INSERT INTO threads VALUES ('bbbbbbbbbbbb', '{}', 0)")
        self.assertEqual(self.store.gc(referenced={held.id}), [thread_file.id])
        self.assertIsNotNone(self.store.get(kept_thread.id))
        self.assertIsNotNone(self.store.get(fresh.id))
        self.assertIsNotNone(self.store.get(held.id))
        self.assertFalse(orphan.exists())

    def test_deleting_a_conversation_deletes_its_files(self):
        a = self.store.create(b"a", "a.txt", thread_id="3f9c2a1b7d0e")
        b = self.store.create(b"b", "b.txt", thread_id="3f9c2a1b7d0e")
        other = self.store.create(b"c", "c.txt", thread_id="bbbbbbbbbbbb")
        self.assertEqual(sorted(self.store.delete_thread("3f9c2a1b7d0e")), sorted([a.id, b.id]))
        self.assertEqual([x.id for x in self.store.for_thread("bbbbbbbbbbbb")], [other.id])


class ServiceTests(unittest.TestCase):
    def test_a_runtime_without_a_journal_on_disk_has_no_store(self):
        from types import SimpleNamespace

        class Runtime:
            config = SimpleNamespace(state_db=None)
            journal = Journal(None)
        self.assertIsNone(service.store_for(Runtime()))


class HttpTests(Base):
    SESSION = "s" * 40

    def setUp(self):
        super().setUp()
        isolate(self)
        self.helper = helper(FakeHelper())
        self.helper.start()
        self.addCleanup(self.helper.stop)
        self.app = self.runtime()
        self.handler = make_handler(self.app)
        self.store = service.store_for(self.app)
        self.phone = FakePhone(apps={"com.apple.Pages": {"notes.txt": b"hello"}})
        patcher = patch.object(service, "phone_files", lambda record: __import__(
            "mobile_agent.phone_io.files", fromlist=["PhoneFiles"]).PhoneFiles(record, runner=self.phone,
                                                                             afc="/x/afcclient",
                                                                             installer="/x/ideviceinstaller"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.record = usb_record()
        device = patch.object(service, "device_record", lambda runtime, device=None: dict(self.record))
        device.start()
        self.addCleanup(device.stop)
        os.environ["OPENAI_API_KEY"] = "sk-test-000000000000"

    def call(self, method, path, body=None, handler=None, **kwargs):
        return request(handler or self.handler, method, path, body, **kwargs)

    def upload(self, data, name="menu.pdf", query=""):
        return self.call("POST", f"/api/attachments?name={name}{query}", raw=data,
                         content_type="application/octet-stream")

    def test_the_track_is_registered(self):
        status, data, _ = self.call("GET", "/api/status")
        self.assertEqual(data["extensions"]["attachments"], "ok")

    def test_upload_read_and_delete(self):
        status, data, _ = self.upload(make_pdf(MENU_PAGES), "Luigi%27s%20menu.pdf")
        self.assertEqual(status, 201, data)
        attachment = data["attachment"]
        self.assertEqual((attachment["name"], attachment["kind"], attachment["pages"]),
                         ("Luigi's menu.pdf", "pdf", 2))
        status, data, _ = self.call("GET", f"/api/attachments/{attachment['id']}")
        self.assertEqual((status, data["attachment"]["id"]), (200, attachment["id"]))
        status, body, headers = self.call("GET", attachment["contentUrl"])
        self.assertEqual((status, headers["Content-Type"], body[:5]), (200, "application/pdf", b"%PDF-"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, body, headers = self.call("GET", attachment["thumbUrl"])
        self.assertEqual((status, headers["Content-Type"], body[:3]), (200, "image/jpeg", b"\xff\xd8\xff"))
        status, _, _ = self.call("DELETE", f"/api/attachments/{attachment['id']}")
        self.assertEqual(status, 200)
        status, data, _ = self.call("GET", f"/api/attachments/{attachment['id']}")
        self.assertEqual((status, data["code"]), (404, "attachment_not_found"))

    def test_text_is_never_served_as_html_or_a_script(self):
        _, data, _ = self.upload(b"<script>alert(1)</script>", "page.html")
        _, _, headers = self.call("GET", data["attachment"]["contentUrl"])
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_a_renamed_program_is_refused_and_an_empty_file_too(self):
        status, data, _ = self.upload(EXE, "menu.pdf")
        self.assertEqual((status, data["code"]), (415, "unsupported_type"))
        self.assertIn("images, PDFs, text and CSV", data["error"])
        status, data, _ = self.call("POST", "/api/attachments?name=x.txt", raw=b"", content_type="text/plain")
        self.assertEqual(status, 400)

    def test_25_mb_goes_through_the_handler_and_one_byte_more_is_413(self):
        status, data, _ = self.upload(b"a" * 26_214_400, "big.txt")
        self.assertEqual(status, 201, data.get("error") if isinstance(data, dict) else data)
        status, data, _ = self.upload(b"a" * 26_214_401, "bigger.txt")
        self.assertEqual((status, data["code"]), (413, "too_large"))

    def test_bad_queries_are_400(self):
        status, _, _ = self.upload(b"hi", "x.txt", "&thread=nope")
        self.assertEqual(status, 400)
        status, _, _ = self.upload(b"hi", "x.txt", "&name=twice.txt")
        self.assertEqual(status, 400)

    def test_a_run_takes_attachment_ids_and_keeps_its_files(self):
        _, data, _ = self.upload(b"Kate Bell: 555-0100", "contacts.txt")
        identifier = data["attachment"]["id"]
        for value, words in ((["ffffffffffff"], "gone"), (["uploading"], "still uploading"),
                             (["not-an-id"], "file ids"), ([identifier] * 2 + [f"{i:012x}" for i in range(10)],
                                                          "at most 10")):
            with self.subTest(value=value):
                status, refused, _ = self.call("POST", "/api/runs", {"appId": "messages", "goal": "Text Kate",
                                                                    "mode": "live", "attachmentIds": value})
                self.assertEqual(status, 400, refused)
                self.assertIn(words, refused["error"])
        status, created, _ = self.call("POST", "/api/runs", {"appId": "messages", "goal": "Text Kate the number",
                                                             "mode": "live", "attachmentIds": [identifier]})
        self.assertEqual(status, 201, created)
        run = created if "id" in created else created["run"]
        self.assertEqual(run["extras"], {"attachmentIds": [identifier]})
        # The task names it: it shows in the file's runIds at once (the composer drops the chip), and the
        # clean-up keeps it while the task is in history.
        self.assertEqual(self.store.get(identifier).run_ids, (run["id"],))
        status, data, _ = self.call("GET", f"/api/attachments/{identifier}")
        self.assertEqual(data["attachment"]["runIds"], [run["id"]])
        self.assertEqual(self.store.gc(now=time.time() + 2 * 86400), [])
        self.assertIsNotNone(self.store.get(identifier))

    def test_deleting_a_task_never_waits_on_files_and_its_files_follow(self):
        """The journal tells listeners under the runtime's lock: the files are cleaned on the clean-up thread."""
        _, data, _ = self.upload(b"Kate Bell: 555-0100", "contacts.txt")
        identifier = data["attachment"]["id"]
        status, created, _ = self.call("POST", "/api/runs", {"appId": "messages", "goal": "Text Kate", "mode": "live",
                                                             "attachmentIds": [identifier]})
        run = self.app.runs[(created.get("run") or created)["id"]]
        self.store.attach([identifier], run.id)
        self.finish(self.app, run)
        with patch.object(service, "BUS_WAIT", .02):
            service.close(self.app)          # restart the clean-up with a short wait
            service.ensure_cleaner(self.app)
            done = []
            worker = threading.Thread(target=lambda: done.append(self.call("DELETE", f"/api/runs/{run.id}")))
            worker.start()
            worker.join(5)
            self.assertTrue(done, "deleting the task waited on the files track")
            self.assertEqual(done[0][0], 200)
            for _ in range(250):
                if self.store.get(identifier) is None:
                    break
                time.sleep(.02)
        self.assertIsNone(self.store.get(identifier))

    def test_listing_the_phones_files(self):
        status, data, _ = self.call("GET", "/api/phone/files")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["apps"], [{"bundleId": "com.apple.Pages", "name": "Pages"}])
        status, data, _ = self.call("GET", "/api/phone/files?app=com.apple.Pages")
        self.assertEqual([f["name"] for f in data["files"]], ["notes.txt"])

    def test_putting_a_file_on_the_phone_needs_the_app_session_in_the_mac_app(self):
        _, data, _ = self.upload(make_pdf(MENU_PAGES), "menu.pdf")
        body = {"attachmentId": data["attachment"]["id"], "destination": "app:com.apple.Pages"}
        with_session = make_handler(self.app, app_session=self.SESSION)
        status, refused, _ = self.call("POST", "/api/phone/files/put", body, handler=with_session)
        self.assertEqual((status, refused["code"]), (403, "app_only"))
        self.assertNotIn("menu.pdf", self.phone.apps["com.apple.Pages"])
        headers = {"X-Mobster-App-Session": self.SESSION, "Idempotency-Key": "put-1"}
        status, done, _ = self.call("POST", "/api/phone/files/put", body, handler=with_session, headers=headers)
        self.assertEqual((status, done["status"], done["name"]), (200, "copied", "menu.pdf"), done)
        self.assertEqual(self.phone.apps["com.apple.Pages"]["menu.pdf"][:5], b"%PDF-")
        puts = len([c for c in self.phone.calls if "put" in c])
        status, again, _ = self.call("POST", "/api/phone/files/put", body, handler=with_session, headers=headers)
        self.assertEqual(again, done)  # the same key: no second copy
        self.assertEqual(len([c for c in self.phone.calls if "put" in c]), puts)
        status, second, _ = self.call("POST", "/api/phone/files/put", body, handler=with_session,
                                      headers={"X-Mobster-App-Session": self.SESSION})
        self.assertEqual(second["name"], "menu 2.pdf")  # never overwrites the user's file

    def test_files_move_only_over_the_cable(self):
        self.record["transport"] = "wifi"
        _, data, _ = self.upload(b"hello", "a.txt")
        status, refused, _ = self.call("POST", "/api/phone/files/put", {"attachmentId": data["attachment"]["id"],
                                                                       "destination": "app:com.apple.Pages"})
        self.assertEqual((status, refused["code"]), (409, "needs_cable"))
        status, refused, _ = self.call("GET", "/api/phone/files")
        self.assertEqual(status, 409)
        self.record.update(transport=None, kind="simulator", name="iPhone 17 Pro")
        status, refused, _ = self.call("GET", "/api/phone/files")
        self.assertEqual((status, refused["code"]), (400, "usage"))
        self.assertIn("plugged into this Mac", refused["error"])

    def test_an_app_without_file_sharing_says_so(self):
        _, data, _ = self.upload(b"hello", "a.txt")
        status, refused, _ = self.call("POST", "/api/phone/files/put", {"attachmentId": data["attachment"]["id"],
                                                                       "destination": "app:com.apple.MobileSMS"})
        self.assertEqual((status, refused["code"]), (409, "no_file_sharing"))

    def test_bringing_a_file_back_makes_it_an_attachment(self):
        posted = []
        with patch.object(harness_api, "post_thread_item", lambda thread, item: posted.append((thread, item))):
            status, data, _ = self.call("POST", "/api/phone/files/get",
                                        {"app": "com.apple.Pages", "name": "notes.txt", "threadId": "3f9c2a1b7d0e"})
        self.assertEqual(status, 201, data)
        self.assertEqual((data["attachment"]["origin"], data["attachment"]["threadId"]), ("phone", "3f9c2a1b7d0e"))
        self.assertEqual(posted[0][1]["kind"], "file_saved")
        self.assertEqual(posted[0][1]["attachment"]["name"], "notes.txt")
        status, data, _ = self.call("POST", "/api/phone/files/get", {"app": "com.apple.Pages", "name": "nope.txt"})
        self.assertEqual((status, data["code"]), (404, "not_found"))

    def test_the_clipboard_takes_text_files_only_and_never_during_a_task(self):
        _, pdf, _ = self.upload(make_pdf(["x"]), "menu.pdf")
        status, refused, _ = self.call("POST", "/api/phone/files/put", {"attachmentId": pdf["attachment"]["id"],
                                                                       "destination": "clipboard"})
        self.assertEqual((status, refused["code"]), (400, "not_text"))
        _, text, _ = self.upload(b"hello", "a.txt")
        with patch.object(type(self.app), "device_busy", lambda runtime, device: True):
            status, refused, _ = self.call("POST", "/api/phone/files/put", {"attachmentId": text["attachment"]["id"],
                                                                           "destination": "clipboard"})
        self.assertEqual((status, refused["code"]), (409, "device_busy"))

    def test_the_clipboard_takes_the_whole_text_file_through_webdriveragent(self):
        import base64
        import io
        from types import SimpleNamespace
        rows = "dish;price\r\n" + "".join(f"Dish {i};{i}\r\n" for i in range(400))  # past the 200 rows read
        _, table, _ = self.upload(rows.encode(), "menu.csv")
        sent = []

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(req, timeout=None):
            sent.append((req.full_url, json.loads(req.data)))
            return Answer(b'{"value": null}')
        phone = SimpleNamespace(id="sams-iphone", name="Sam's iPhone", wda_url="http://127.0.0.1:8100",
                                wda_session=lambda: "s1")
        with patch.object(type(self.app), "resolve_device", lambda runtime, device: phone), \
                patch.object(type(self.app), "device_busy", lambda runtime, device: False), \
                patch("urllib.request.urlopen", urlopen):
            status, done, _ = self.call("POST", "/api/phone/files/put", {"attachmentId": table["attachment"]["id"],
                                                                         "destination": "clipboard"})
        self.assertEqual((status, done["destination"]), (200, "clipboard"), done)
        self.assertEqual(sent[0][0], "http://127.0.0.1:8100/session/s1/wda/setPasteboard")
        self.assertEqual(base64.b64decode(sent[0][1]["content"]).decode(), rows.replace("\r\n", "\n"))


if __name__ == "__main__":
    unittest.main()
