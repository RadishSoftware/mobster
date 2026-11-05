import base64
import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent.phone_io import PhoneIOError, handoff, photos, transfer, tunnel
from mobile_agent.phone_io.transfer import Phone, Transfer, Transfers

USB = {"id": "alex", "kind": "usb", "udid": "00008130-00000000ABCD0007", "name": "alex", "wdaUrl": "http://x"}
WIFI = {**USB, "transport": "wifi"}
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 3000
HEIC = b"\x00\x00\x00\x18ftypheic" + b"h" * 5000
MOV = b"\x00\x00\x00\x14ftypqt  " + b"m" * 7000
MP4 = b"\x00\x00\x00\x18ftypisom" + b"4" * 9000


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeRunner:
    def __init__(self, transfers=True, status="authorized", corrupt=False, lose_save=False):
        self.transfers, self.status_value, self.corrupt, self.lose_save = transfers, status, corrupt, lose_save
        self.library = []
        self.uploads = {}
        self.calls = []
        self.alerts = []

    def __call__(self, method, path, body=None, timeout=15):
        self.calls.append((method, path))
        if path == "/mobster/photos/status":
            return {"status": self.status_value, "bundleId": "runner"}
        if path.startswith("/mobster/photos/") and not self.transfers:
            raise PhoneIOError("WebDriverAgent doesn't know that command.", "unknown_route")
        if path == "/mobster/photos/count":
            photos_ = sum(1 for i in self.library if i["kind"] == "photo")
            return {"status": self.status_value, "photos": photos_, "videos": len(self.library) - photos_}
        if path == "/mobster/photos/chunk":
            have = self.uploads.get(body["upload"], b"") if body["offset"] else b""
            if body["offset"] != len(have):
                return {"ok": False, "bytes": len(have), "error": "offset"}
            self.uploads[body["upload"]] = have + base64.b64decode(body["data"])
            return {"ok": True, "bytes": len(self.uploads[body["upload"]])}
        if path == "/mobster/photos/save":
            data = self.uploads.pop(body["upload"])
            if self.corrupt:
                data = data[:-1] + b"!"
            if sha(data) != body["sha256"]:
                return {"saved": False, "error": "checksum", "sha256": sha(data)}
            if self.lose_save:
                raise PhoneIOError("WebDriverAgent didn't answer (TimeoutError).", "failed")
            return self._add(data, body["filename"], body["kind"], body.get("album"))
        if path == "/mobster/photos":
            data = base64.b64decode(body["data"])
            return self._add(data, body["filename"], body["kind"], None)
        if path == "/mobster/photos/recent":
            items = list(reversed(self.library))[:body["limit"]]
            return {"status": self.status_value, "items": [
                {"id": i["id"], "filename": i["filename"], "kind": i["kind"], "created": 1_790_000_000 + n}
                for n, i in enumerate(items)]}
        if path == "/mobster/photos/export":
            item = next((i for i in self.library if i["id"] == body["id"]), None)
            if item is None:
                return {"error": "not_found", "status": self.status_value}
            data = item["data"]
            piece = data[body["offset"]:body["offset"] + body["length"]] if body["length"] else b""
            return {"status": self.status_value, "filename": item["filename"], "bytes": len(data),
                    "sha256": sha(data), "offset": body["offset"], "data": base64.b64encode(piece).decode(),
                    "done": not body["length"] or body["offset"] + len(piece) >= len(data)}
        if path == "/alert/text":
            if self.alerts:
                return self.alerts[0]
            raise PhoneIOError("WebDriverAgent doesn't know that command.", "unknown_route")
        if path == "/wda/alert/buttons":
            return ["Decline", "Accept"]
        if path == "/alert/accept":
            self.alerts.clear()
            self.accepted = body["name"]
            return None
        if path == "/wda/apps/activate":
            return None
        raise AssertionError(f"unexpected {method} {path}")

    def _add(self, data, filename, kind, album):
        identifier = f"ID{len(self.library) + 1}/L0/001"
        self.library.append({"id": identifier, "data": data, "filename": filename, "kind": kind, "album": album})
        return {"saved": True, "status": self.status_value, "localIdentifier": identifier, "sha256": sha(data),
                "bytes": len(data), "albumError": ""}

    def context(self):
        @contextlib.contextmanager
        def runner():
            yield self
        return runner


class FakeRoll:
    def __init__(self, files=None):
        self.files = dict(files or {})

    def __call__(self, record):
        return self

    def media(self):
        return {path: len(data) for path, data in self.files.items()}

    def fetch(self, remote, folder, size=None):
        target = Path(folder) / remote.rsplit("/", 1)[-1]
        target.write_bytes(self.files[remote])
        return target

    def pull(self, folder, count=1, since=None, convert_heic=False):
        out = []
        for path in sorted(self.files, reverse=True)[:count]:
            target = Path(folder) / path.rsplit("/", 1)[-1]
            target.write_bytes(self.files[path])
            out.append({"path": str(target), "name": target.name, "bytes": target.stat().st_size, "kind": "photo",
                        "sha256": sha(self.files[path])})
        return out


class FakeHandoff:
    def __init__(self, roll=None, runner=None, error=None, mangle=False):
        self.roll, self.runner, self.error, self.mangle, self.sent = roll, runner, error, mangle, []

    def airdrop(self, paths, to):
        self.sent.append(("airdrop", to, list(paths)))
        if self.error:
            raise self.error
        self._arrive(paths)
        return {"sent": len(paths), "seconds": 1.0}

    def mirroring_drop(self, paths, x, y):
        self.sent.append(("drop", x, y, list(paths)))
        self._arrive(paths)
        return {"operation": 1}

    def _arrive(self, paths):
        for n, path in enumerate(paths):
            data = Path(path).read_bytes()
            if self.mangle:
                data = data + b"x"
            if self.roll is not None:
                self.roll.files[f"/DCIM/137APPLE/IMG_{9000 + len(self.roll.files) + n}.JPG"] = data
            if self.runner is not None:
                self.runner._add(data, Path(path).name, "photo", None)


def no_sleep(_):
    return None


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.files = {}
        for name, data in (("a.jpg", JPEG), ("b.heic", HEIC), ("c.mov", MOV), ("d.mp4", MP4)):
            (self.dir / name).write_bytes(data)
            self.files[name] = str(self.dir / name)
        patcher = mock.patch.dict(os.environ, {"MOBSTER_NO_DEVICES": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)


class SendInPiecesTests(Base):
    def test_actual_upload_limit_and_nonregular_sources_refuse_before_preflight(self):
        source = Path(self.files["a.jpg"])
        runner = FakeRunner()
        reserved = []
        with mock.patch.object(photos, "MAX_CHUNKED_BYTES", len(JPEG) - 1), \
                self.assertRaises(PhoneIOError) as caught:
            photos.send(runner, source, before_send=lambda: reserved.append(True))
        self.assertEqual(caught.exception.code, "usage")
        source.unlink()
        os.mkfifo(source)
        with self.assertRaises(PhoneIOError) as caught:
            photos.send(runner, source, before_send=lambda: reserved.append(True))
        self.assertEqual(caught.exception.code, "usage")
        self.assertEqual((runner.calls, runner.library, reserved), ([], [], []))

    def test_upload_snapshot_is_private_and_removed_after_an_unknown_save(self):
        runner = FakeRunner(lose_save=True)
        staged = []
        original = photos._send_pieces

        def send(request, file, *args):
            staged.append(file)
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(file, Path(self.files["a.jpg"]))
            return original(request, file, *args)

        with mock.patch.object(photos, "_send_pieces", side_effect=send), self.assertRaises(PhoneIOError) as caught:
            photos.send(runner, self.files["a.jpg"])
        self.assertEqual(caught.exception.code, "unknown")
        self.assertEqual(len(staged), 1)
        self.assertFalse(staged[0].exists())
        self.assertEqual(len([c for c in runner.calls if c[1] == "/mobster/photos/save"]), 1)

    def test_source_replacement_after_preflight_does_not_change_either_upload_route(self):
        for transfers in (False, True):
            with self.subTest(transfers=transfers):
                source = Path(self.files["a.jpg"])
                source.write_bytes(JPEG)
                runner = FakeRunner(transfers=transfers)
                result = photos.send(runner, source, album="Spring", chunk=17,
                                     before_send=lambda: source.write_bytes(b"!" * len(JPEG)))
                self.assertEqual(runner.library[0]["data"], JPEG)
                self.assertEqual((result["name"], result["bytes"], result["sha256"]),
                                 ("a.jpg", len(JPEG), sha(JPEG)))

    def test_changed_source_checksum_refuses_before_any_phone_request_or_reservation(self):
        runner = FakeRunner()
        reserved = []
        with self.assertRaises(PhoneIOError) as caught:
            photos.send(runner, self.files["a.jpg"], sha256=sha(b"different"),
                        before_send=lambda: reserved.append(True))
        self.assertEqual(caught.exception.code, "checksum")
        self.assertEqual((runner.calls, runner.library, reserved), ([], [], []))

    def test_reservation_precedes_chunked_transfer_without_changing_album_or_checksum(self):
        runner = FakeRunner()
        reserved = []
        result = photos.send(runner, self.files["c.mov"], album="Spring", chunk=1000,
                             before_send=lambda: reserved.append(list(runner.calls)))
        self.assertEqual(reserved, [[("GET", "/mobster/photos/status")]])
        self.assertTrue(result["verified"])
        self.assertEqual((result["album"], result["sha256"]), ("Spring", sha(MOV)))
        self.assertEqual(runner.library[0]["data"], MOV)

    def test_pieces_are_checked_by_sha256_on_the_phone_and_go_into_the_album(self):
        runner = FakeRunner()
        seen = []
        result = photos.send(runner, self.files["c.mov"], name="alex", album="Spring", chunk=1000,
                             progress=lambda sent, total: seen.append((sent, total)))
        self.assertTrue(result["verified"])
        self.assertEqual((result["kind"], result["album"], result["sha256"]), ("video", "Spring", sha(MOV)))
        self.assertEqual(runner.library[0]["data"], MOV)
        self.assertEqual(runner.library[0]["album"], "Spring")
        self.assertEqual(seen[-1], (len(MOV), len(MOV)))
        self.assertEqual(len([c for c in runner.calls if c[1] == "/mobster/photos/chunk"]), 8)

    def test_a_changed_byte_on_the_way_is_refused_and_never_reaches_photos(self):
        runner = FakeRunner(corrupt=True)
        with self.assertRaises(PhoneIOError) as caught:
            photos.send(runner, self.files["a.jpg"], name="alex", chunk=1000)
        self.assertEqual(caught.exception.code, "checksum")
        self.assertEqual(runner.library, [])

    def test_an_old_runner_takes_one_piece_up_to_100_mb_and_says_albums_need_a_rebuild(self):
        runner = FakeRunner(transfers=False)
        result = photos.send(runner, self.files["a.jpg"], name="alex", album="Spring")
        self.assertFalse(result["verified"])
        self.assertIsNone(result["album"])
        self.assertIn("Rebuild", result["albumError"])
        self.assertEqual(runner.library[0]["data"], JPEG)
        big = self.dir / "big.mp4"
        with open(big, "wb") as stream:
            stream.write(MP4[:16])
            stream.truncate(photos.MAX_SEND_BYTES + 1)
        with self.assertRaises(PhoneIOError) as caught:
            photos.send(FakeRunner(transfers=False), big, name="alex")
        self.assertEqual(caught.exception.code, "needs_rebuild")

    def test_a_save_whose_answer_is_lost_is_unknown_and_not_sent_again(self):
        runner = FakeRunner(lose_save=True)
        with self.assertRaises(PhoneIOError) as caught:
            photos.send(runner, self.files["a.jpg"], name="alex")
        self.assertEqual(caught.exception.code, "unknown")
        self.assertEqual(len([c for c in runner.calls if c[1] == "/mobster/photos/save"]), 1)

    def test_heic_mov_and_mp4_are_told_apart_by_their_bytes(self):
        self.assertEqual(photos.sniff(self.files["b.heic"]), ("photo", "heic"))
        self.assertEqual(photos.sniff(self.files["c.mov"]), ("video", "mov"))
        self.assertEqual(photos.sniff(self.files["d.mp4"]), ("video", "mp4"))


class ExportTests(Base):
    def test_items_come_back_in_pieces_checked_against_the_phones_checksum(self):
        runner = FakeRunner()
        runner._add(MP4, "IMG_0001.MP4", "video", None)
        item = photos.recent(runner, 1)[0]
        got = photos.export(runner, item, self.dir / "out", chunk=1000)
        self.assertEqual(Path(got["path"]).read_bytes(), MP4)
        self.assertEqual((got["sha256"], got["name"]), (sha(MP4), "IMG_0001.MP4"))
        self.assertEqual(photos.checksum(runner, item["id"])["sha256"], sha(MP4))

    def test_a_mismatch_is_refused_and_leaves_nothing(self):
        runner = FakeRunner()
        runner._add(JPEG, "IMG_0002.JPG", "photo", None)
        original = runner.__call__

        def lying(method, path, body=None, timeout=15):
            answer = original(method, path, body, timeout)
            if path == "/mobster/photos/export" and answer.get("data"):
                answer["data"] = base64.b64encode(b"z" * len(base64.b64decode(answer["data"]))).decode()
            return answer
        with self.assertRaises(PhoneIOError) as caught:
            photos.export(lying, photos.recent(runner, 1)[0], self.dir / "out")
        self.assertEqual(caught.exception.code, "checksum")
        self.assertEqual([p.name for p in (self.dir / "out").iterdir()], [])


class ToPhoneTests(Base):
    def phone(self, record=USB, runner=None, roll=None, handoff_=None):
        return Phone(record, runner=runner.context() if runner else None, roll=roll, handoff=handoff_)

    def test_over_the_cable_each_is_verified_and_the_library_count_is_kept(self):
        runner = FakeRunner()
        runner._add(b"old", "old.jpg", "photo", None)
        moved = transfer.run(Transfer("to_phone", "photos", self.phone(runner=runner),
                                      paths=[self.files["a.jpg"], self.files["c.mov"], self.files["d.mp4"]]))
        self.assertEqual(moved.state, "done")
        self.assertEqual(moved.route, "cable")
        self.assertEqual([i.state for i in moved.items], ["verified"] * 3)
        self.assertEqual((moved.before, moved.after), (1, 4))
        self.assertEqual({i["album"] for i in runner.library[1:]}, {"Mobster"})
        self.assertTrue(moved.public()["verified"])
        self.assertIn("3 checked by SHA-256", transfer.summary(moved))

    def test_airdrop_is_checked_on_the_cable_by_count_and_checksum(self):
        roll = FakeRoll({"/DCIM/137APPLE/IMG_0001.HEIC": b"old"})
        hand = FakeHandoff(roll=roll)
        moved = transfer.run(Transfer("to_phone", "photos", self.phone(roll=roll, handoff_=hand), via="airdrop",
                                      paths=[self.files["a.jpg"], self.files["b.heic"]]), sleep=no_sleep)
        self.assertEqual(hand.sent[0][:2], ("airdrop", "alex"))
        self.assertEqual((moved.before, moved.after), (1, 3))
        self.assertEqual([i.state for i in moved.items], ["verified", "verified"])

    def test_what_arrived_changed_is_counted_but_not_verified(self):
        roll = FakeRoll()
        moved = transfer.run(Transfer("to_phone", "photos", self.phone(roll=roll, handoff_=FakeHandoff(roll=roll,
                                                                                                       mangle=True)),
                                      via="airdrop", paths=[self.files["a.jpg"]]), sleep=no_sleep)
        self.assertEqual(moved.items[0].state, "counted")

    def test_nothing_arrived_is_unknown_and_nothing_is_sent_again(self):
        roll = FakeRoll()

        class Lost(FakeHandoff):
            def _arrive(self, paths):
                return None
        hand = Lost(roll=roll)
        clock = iter(range(0, 10_000, 30)).__next__
        moved = transfer.run(Transfer("to_phone", "photos", self.phone(roll=roll, handoff_=hand), via="airdrop",
                                      paths=[self.files["a.jpg"]]), clock=clock, sleep=no_sleep)
        self.assertEqual(moved.state, "unknown")
        self.assertEqual(len(hand.sent), 1)
        self.assertIn("Look in Photos", moved.items[0].error)

    def test_auto_moves_on_only_when_a_route_refused_before_sending(self):
        roll = FakeRoll()
        refused = FakeHandoff(roll=roll, error=PhoneIOError("AirDrop doesn't see alex.", "airdrop_not_found"))
        record = {**USB, "transport": "wifi", "wdaUrl": None}
        moved = transfer.run(Transfer("to_phone", "photos", Phone(record, roll=roll, handoff=refused),
                                      paths=[self.files["a.jpg"]]), sleep=no_sleep)
        self.assertEqual(moved.state, "failed")
        self.assertEqual([t["via"] for t in moved.tried], ["cable", "wifi", "airdrop", "mirroring"])
        unknown = FakeHandoff(roll=roll, error=PhoneIOError("AirDrop stopped answering", "unknown"))
        moved = transfer.run(Transfer("to_phone", "photos", Phone(record, roll=roll, handoff=unknown),
                                      paths=[self.files["a.jpg"]]), sleep=no_sleep)
        self.assertEqual(moved.state, "unknown")
        self.assertEqual([t["via"] for t in moved.tried], ["cable", "wifi"])

    def test_mirroring_only_reaches_the_phone_this_mac_is_set_to_mirror(self):
        roll = FakeRoll()
        hand = FakeHandoff(roll=roll)
        with mock.patch.dict(os.environ, {"MOBSTER_MIRRORS": "another phone"}):
            moved = transfer.run(Transfer("to_phone", "photos", self.phone(roll=roll, handoff_=hand), via="mirroring",
                                          paths=[self.files["a.jpg"]]), sleep=no_sleep)
        self.assertEqual(moved.state, "failed")
        self.assertEqual(hand.sent, [])
        opened = []
        with mock.patch.dict(os.environ, {"MOBSTER_MIRRORS": "alex"}), \
                mock.patch.object(transfer, "open_on_mirroring", opened.append):
            moved = transfer.run(Transfer("to_phone", "photos", self.phone(roll=roll, handoff_=hand), via="mirroring",
                                          paths=[self.files["a.jpg"]]), sleep=no_sleep)
        self.assertEqual(opened, ["Photos"])
        self.assertEqual(hand.sent[0][0], "drop")
        self.assertEqual(moved.items[0].state, "verified")

    def test_airdrop_accept_is_tapped_only_on_an_airdrop_alert(self):
        runner = FakeRunner()
        runner.alerts.append("“Alex’s MacBook Pro” would like to share a photo. AirDrop")
        done = threading.Event()
        threading.Timer(0.2, done.set).start()
        transfer._accept_airdrop(self.phone(runner=runner), done, sleep=lambda s: time.sleep(0.01))
        self.assertEqual(runner.accepted, "Accept")
        other = FakeRunner()
        other.alerts.append("Allow “Photos” to use your location?")
        done = threading.Event()
        threading.Timer(0.1, done.set).start()
        transfer._accept_airdrop(self.phone(runner=other), done, sleep=lambda s: time.sleep(0.01))
        self.assertFalse(hasattr(other, "accepted"))

    def test_files_into_an_apps_folder_are_read_back_and_compared(self):
        stored = {}

        class Files:
            def __init__(self, record, container=False):
                self.container = container

            def put(self, app, local, name=None, replace=False):
                stored[Path(local).name] = Path(local).read_bytes()
                return {"name": Path(local).name, "path": "/"
                        + Path(local).name, "bytes": len(stored[Path(local).name])}

            def get(self, app, name, folder):
                target = Path(folder) / name
                target.write_bytes(stored[name])
                return target
        moved = transfer.run(Transfer("to_phone", "files", Phone(USB, files=Files), paths=[self.files["d.mp4"]],
                                      app="com.apple.Pages", album=None))
        self.assertEqual(moved.items[0].state, "verified")


class ToMacTests(Base):
    def test_an_app_file_pull_only_compares_size_and_keeps_its_local_checksum(self):
        class Files:
            def __init__(self, record, container=False):
                pass

            def ls(self, app):
                return [{"name": "notes.txt", "bytes": 5}]

            def get(self, app, name, folder):
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / name
                target.write_bytes(b"hello")
                return target

        moved = transfer.run(Transfer("to_mac", "files", Phone(USB, files=Files), app="com.apple.Pages",
                                      names=["notes.txt"], folder=str(self.dir / "files")))
        self.assertEqual((moved.state, moved.items[0].state), ("done", "counted"))
        self.assertEqual(moved.items[0].sha256, sha(b"hello"))
        self.assertFalse(moved.public()["verified"])
        self.assertIn("matched by size", transfer.summary(moved))
        self.assertNotIn("SHA-256", transfer.summary(moved))

    def test_a_cable_pull_is_counted_by_size_and_never_claims_a_checksum(self):
        roll = FakeRoll({"/DCIM/100APPLE/IMG_0001.HEIC": HEIC})
        moved = transfer.run(Transfer("to_mac", "photos", Phone(USB, roll=roll), folder=str(self.dir / "in"),
                                      count=1))
        self.assertEqual([i.state for i in moved.items], ["counted"])
        self.assertFalse(moved.public()["verified"])
        self.assertNotIn("SHA-256", transfer.summary(moved))
        self.assertIn("matched by size", transfer.summary(moved))

    def test_an_old_runner_on_wifi_says_to_rebuild_it_and_names_the_phone(self):
        moved = transfer.run(Transfer("to_mac", "photos", Phone(WIFI, runner=FakeRunner(transfers=False).context()),
                                      via="wifi", folder=str(self.dir / "in"), count=1))
        self.assertEqual((moved.state, moved.error["code"]), ("failed", "needs_rebuild"))
        self.assertIn("alex", moved.error["message"])
        self.assertIn("Rebuild", moved.error["message"])
        self.assertNotIn("old_runner", json.dumps(moved.public()))


class ReadFallbackTests(Base):
    def test_a_copy_to_the_mac_tries_the_next_route_after_any_failure(self):
        class Down:
            def pull(self, folder, count=1, since=None, convert_heic=False):
                raise PhoneIOError("The tunnel to alex failed.", "failed")
        runner = FakeRunner()
        runner._add(JPEG, "IMG_0004.JPG", "photo", None)
        record = {**USB, "transport": "wifi"}
        phone = Phone(record, runner=runner.context(), tunnel=lambda rec: Down())
        moved = transfer.run(Transfer("to_mac", "photos", phone, via="auto", folder=str(self.dir / "in")))
        self.assertEqual((moved.route, moved.state), ("wifi", "done"))
        broken = Phone(record, tunnel=lambda rec: Down())
        moved = transfer.run(Transfer("to_mac", "photos", broken, folder=str(self.dir / "in2")))
        self.assertEqual(moved.state, "failed")
        self.assertEqual([t["via"] for t in moved.tried], ["cable", "wifi", "tunnel"])


class TunnelTests(unittest.TestCase):
    def test_newest_by_folder_and_number_and_a_down_tunnel_is_read_again(self):
        listing = "\n".join(["/DCIM", "/DCIM/101APPLE", "/DCIM/101APPLE/IMG_0010.HEIC", "/DCIM/100APPLE/IMG_0999.JPG",
                             "/DCIM/101APPLE/IMG_0011.AAE", "/DCIM/101APPLE/IMG_0012.MOV"])
        answers = [(1, "ERROR could not find remoted's RSD connection"), (0, listing)]
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return answers.pop(0)
        roll = tunnel.TunnelRoll({"udid": USB["udid"], "kind": "usb", "name": "alex"}, runner=runner, program="p")
        self.assertEqual(roll.media()[-2:], ["/DCIM/101APPLE/IMG_0010.HEIC", "/DCIM/101APPLE/IMG_0012.MOV"])
        self.assertEqual(len(calls), 2)
        self.assertIn("--native", calls[0])

    def test_the_tool_is_found_in_pipxs_own_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            program = Path(folder) / tunnel.TOOL
            program.write_text("#!/bin/sh\n")
            program.chmod(0o755)
            with mock.patch.object(tunnel.shutil, "which", return_value=None):
                with mock.patch.dict(os.environ, {"PIPX_BIN_DIR": folder}):
                    self.assertEqual(tunnel.tool(), str(program))
                program.chmod(0o644)
                with mock.patch.dict(os.environ, {"PIPX_BIN_DIR": folder}):
                    self.assertNotEqual(tunnel.tool(), str(program))


class HandoffTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.file = Path(folder.name) / "a.jpg"
        self.file.write_bytes(JPEG)

    def answer(self, value):
        return lambda argv, timeout: json.dumps(value) + "\n"

    def test_airdrop_answers_become_codes(self):
        with mock.patch.dict(os.environ, {"MOBSTER_NO_DEVICES": ""}):
            self.assertEqual(handoff.airdrop([self.file], "alex", tool="t", runner=self.answer(
                {"ok": True, "sent": 1, "seconds": 16.9}))["sent"], 1)
            for value, code in (({"ok": False, "code": "not_found", "seen": ["Studio iPhone"]}, "airdrop_not_found"),
                                ({"ok": False, "code": "ambiguous"}, "airdrop_ambiguous"),
                                ({"ok": False, "code": "permission"}, "permission"),
                                ({"ok": False, "code": "failed", "error": "Declined"}, "airdrop_declined")):
                with self.subTest(code=code), self.assertRaises(PhoneIOError) as caught:
                    handoff.airdrop([self.file], "alex", tool="t", runner=self.answer(value))
                self.assertEqual(caught.exception.code, code)
            with self.assertRaises(PhoneIOError) as caught:
                handoff.airdrop([self.file], "alex", tool="t", runner=lambda argv, timeout: None)
            self.assertEqual(caught.exception.code, "unknown")

    def test_the_helper_gets_the_exact_name_and_the_files(self):
        seen = []

        def runner(argv, timeout):
            seen.append(argv)
            return json.dumps({"ok": True, "sent": 1}) + "\n"
        with mock.patch.dict(os.environ, {"MOBSTER_NO_DEVICES": ""}):
            handoff.airdrop([self.file], "🔗 example.sh", tool="t", runner=runner)
            handoff.mirroring_drop([self.file], 0.5, 0.55, tool="t", runner=runner)
        self.assertEqual(seen[0][:4], ["t", "airdrop", "--to", "🔗 example.sh"])
        self.assertEqual(seen[0][-1], str(self.file.resolve()))
        self.assertEqual(seen[1][:6], ["t", "drop", "--x", "0.5000", "--y", "0.5500"])


class SeveralPhonesTests(Base):
    def test_phones_run_at_once_and_one_phone_one_at_a_time(self):
        active, peak, lock = {}, {"all": 0, "same": 0}, threading.Lock()

        def slow(t):
            with lock:
                active[t.phone.id] = active.get(t.phone.id, 0) + 1
                peak["all"] = max(peak["all"], sum(active.values()))
                peak["same"] = max(peak["same"], max(active.values()))
            time.sleep(0.1)
            with lock:
                active[t.phone.id] -= 1
            t.state = "done"
        manager = Transfers(runner=slow)
        phones = [Phone({**USB, "id": name, "name": name}) for name in ("one", "two", "one")]
        started = [manager.start(Transfer("to_phone", "photos", p, paths=[self.files["a.jpg"]])) for p in phones]
        deadline = time.time() + 5
        while any(t.state != "done" for t in started) and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(peak, {"all": 2, "same": 1})
        self.assertEqual(manager.get(started[0].id), started[0])

    def test_a_profile_names_its_phone(self):
        from mobile_agent.accounts import store as accounts
        fake = [{"id": "aaaaaaaaaaaa", "handle": "getmobster",
                 "app": {"bundleId": "com.burbn.instagram", "name": "Instagram"},
                 "device": "alex", "deviceName": "alex"}]
        with mock.patch.object(accounts, "store_for", return_value=mock.Mock(all=lambda: fake)):
            phone = transfer.target(profile="@getmobster", records=[USB, {**USB, "id": "other", "name": "other"}])
            self.assertEqual(phone.id, "alex")
            with self.assertRaises(PhoneIOError):
                transfer.target(profile="@getmobster", device="other", records=[USB])


class RuntimeOwnershipTests(Base):
    def runtime_phone(self):
        fleet_phone = mock.Mock(id=USB["id"], kind="usb", udid=USB["udid"], name=USB["name"],
                                transport=None, wda_url=USB["wdaUrl"])
        runtime = mock.Mock(closing=False)
        runtime.resolve_device.return_value = fleet_phone
        runtime.device_busy.return_value = False
        return runtime, transfer.phone_from_runtime(runtime, USB["id"])

    def test_runtime_media_refuses_an_owned_phone_without_airdrop_fallback(self):
        from mobile_agent import journal

        runtime, phone = self.runtime_phone()
        handoff_ = FakeHandoff()
        phone.handoff = handoff_
        with mock.patch.object(journal, "_LEASE_ROOT", self.dir), \
                mock.patch("urllib.request.urlopen") as opener:
            owner = journal.Lease.device(USB["wdaUrl"], wait=0, udid=USB["udid"])
            try:
                moved = transfer.run(Transfer("to_phone", "photos", phone, paths=[self.files["a.jpg"]]))
            finally:
                owner.close()
        self.assertEqual((moved.state, moved.error["code"], moved.route), ("failed", "device_busy", "cable"))
        self.assertEqual((moved.tried, handoff_.sent), ([], []))
        opener.assert_not_called()

    def test_cli_media_also_treats_an_owned_phone_as_terminal(self):
        from mobile_agent import journal

        phone = transfer.phone_from_record(USB)
        handoff_ = FakeHandoff()
        phone.handoff = handoff_
        with mock.patch.object(journal, "_LEASE_ROOT", self.dir), \
                mock.patch("urllib.request.urlopen") as opener:
            owner = journal.Lease.device(USB["wdaUrl"], wait=0, udid=USB["udid"])
            try:
                moved = transfer.run(Transfer("to_phone", "photos", phone, paths=[self.files["a.jpg"]]))
            finally:
                owner.close()
        self.assertEqual((moved.state, moved.error["code"]), ("failed", "device_busy"))
        self.assertEqual(handoff_.sent, [])
        opener.assert_not_called()

    def test_a_busy_wifi_export_does_not_fall_through_to_a_tunnel(self):
        @contextlib.contextmanager
        def busy_runner():
            raise PhoneIOError("The phone is busy.", "device_busy")
            yield

        tunnel = mock.Mock()
        phone = Phone(WIFI, runner=busy_runner, tunnel=lambda record: tunnel)
        moved = transfer.run(Transfer("to_mac", "photos", phone, folder=str(self.dir / "out")))
        self.assertEqual((moved.state, moved.error["code"], moved.route), ("failed", "device_busy", "wifi"))
        tunnel.assert_not_called()

    def test_runtime_media_keeps_the_lease_through_its_runner_and_rechecks_admission(self):
        import io

        from mobile_agent import journal

        runtime, phone = self.runtime_phone()
        fleet_phone = runtime.resolve_device.return_value
        fleet_phone.wda_session.return_value = "s1"
        with mock.patch.object(journal, "_LEASE_ROOT", self.dir), \
                mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: io.BytesIO(
                    b'{"value":{"ready":true},"sessionId":"s1"}')) as opener:
            with phone.runner() as request:
                self.assertEqual(request("GET", "/status", None, 5), {"ready": True})
                with self.assertRaises(journal.LeaseHeld):
                    journal.Lease.device(USB["wdaUrl"], wait=0, udid=USB["udid"])
            self.assertIn("/session/s1/status", opener.call_args.args[0].full_url)
            fleet_phone.wda_session.assert_called_once()
            free = journal.Lease.device(USB["wdaUrl"], wait=0, udid=USB["udid"])
            free.close()
            runtime.device_busy.side_effect = [False, True]
            with self.assertRaises(PhoneIOError) as caught:
                with phone.runner():
                    pass
            self.assertEqual(caught.exception.code, "device_busy")
            free = journal.Lease.device(USB["wdaUrl"], wait=0, udid=USB["udid"])
            free.close()
            runtime.device_busy.side_effect = None
            runtime.device_busy.return_value = False
            with self.assertRaisesRegex(RuntimeError, "transfer body failed"):
                with phone.runner():
                    raise RuntimeError("transfer body failed")
            free = journal.Lease.device(USB["wdaUrl"], wait=0, udid=USB["udid"])
            free.close()

    def test_simulator_media_owns_its_device_and_stops_after_an_uncertain_import(self):
        from mobile_agent import journal, native_helpers

        sim = {"id": "SIM-1", "udid": "SIM-1", "kind": "simulator", "name": "Test simulator",
               "wdaUrl": "http://127.0.0.1:8310"}
        phone = Phone(sim)
        paths = [self.files["a.jpg"], self.files["b.heic"]]
        with mock.patch.object(journal, "_LEASE_ROOT", self.dir), \
                mock.patch.object(native_helpers, "require_devices"), \
                mock.patch("subprocess.run") as add:
            def simctl_calls():
                return [call for call in add.call_args_list
                        if call.args[0][:3] == ["xcrun", "simctl", "addmedia"]]

            def owner_lookup_only(*args, **kwargs):
                if args[0][:3] == ["xcrun", "simctl", "addmedia"]:
                    raise AssertionError("busy simulator must not import media")
                return subprocess.CompletedProcess(args[0], 0, "", "")

            add.side_effect = owner_lookup_only
            owner = journal.Lease.device(sim["wdaUrl"], wait=0, udid=sim["udid"])
            try:
                busy = transfer.run(Transfer("to_phone", "photos", phone, paths=paths))
            finally:
                owner.close()
            self.assertEqual((busy.state, busy.error["code"]), ("failed", "device_busy"))
            self.assertEqual(simctl_calls(), [])

            def import_once(*args, **kwargs):
                if args[0][:3] != ["xcrun", "simctl", "addmedia"]:
                    return subprocess.CompletedProcess(args[0], 0, "", "")
                with self.assertRaises(journal.LeaseHeld):
                    journal.Lease.device(sim["wdaUrl"], wait=0, udid=sim["udid"])
                return subprocess.CompletedProcess(args[0], 1, "", "the answer was lost")

            add.side_effect = import_once
            uncertain = transfer.run(Transfer("to_phone", "photos", phone, paths=paths))
            self.assertEqual((uncertain.state, uncertain.error["code"], len(simctl_calls())),
                             ("unknown", "unknown", 1))
            add.reset_mock()
            add.side_effect = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", "")
            sent = transfer.run(Transfer("to_phone", "photos", phone, paths=[paths[0]]))
            self.assertEqual((sent.state, sent.items[0].state, len(simctl_calls())), ("done", "sent", 1))
            free = journal.Lease.device(sim["wdaUrl"], wait=0, udid=sim["udid"])
            free.close()


class RunnerPatchTests(unittest.TestCase):
    def test_a_clone_patched_for_photos_takes_the_transfer_routes_once(self):
        from mobile_agent.wda import patches as wda_patches
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "WebDriverAgentLib" / "Commands" / "FBCustomCommands.m"
            source.parent.mkdir(parents=True)
            fixture = json.loads((Path(__file__).parent / "fixtures/wda-private-input.json").read_text())
            source.write_text(fixture["files"]["WebDriverAgentLib/Commands/FBCustomCommands.m"])
            for args in (["init", "-q"], ["add", "-A"],
                         ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "release"]):
                subprocess.run(["git", "-C", folder, *args], check=True, stdin=subprocess.DEVNULL)
            patches = [p for p in wda_patches.WDA_PATCHES if p[0].endswith("FBCustomCommands.m")]
            with mock.patch.object(wda_patches, "WDA_PATCHES", tuple(patches[:3])):
                wda_patches.patch_wda(folder)
            with mock.patch.object(wda_patches, "WDA_PATCHES", tuple(patches)):
                wda_patches.patch_wda(folder)
                once = source.read_text()
                wda_patches.patch_wda(folder)
            self.assertEqual(source.read_text(), once)
            for route in ("count", "chunk", "save", "recent", "export"):
                self.assertEqual(once.count(f'@"/mobster/photos/{route}"'), 2)
            self.assertEqual(once.count("handleMobsterPhotosAdd:(FBRouteRequest"), 1)
            self.assertIn("CommonCrypto/CommonDigest.h", once)
            self.assertLess(once.index("static NSString *FBMobsterPhotosStatus"),
                            once.index("FBMobsterSHA256(NSString"))


if __name__ == "__main__":
    unittest.main()
