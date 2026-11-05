import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mobile_agent.attachments import service
from mobile_agent.phone_io import PhoneIOError
from mobile_agent.phone_io import files as phone_files
from mobile_agent.phone_io.files import PhoneFiles
from mobile_agent.tests.test_files_support import FakePhone, usb_record


def phone(fake, record=None):
    return PhoneFiles(record or usb_record(), runner=fake, afc="/opt/homebrew/bin/afcclient",
                      installer="/opt/homebrew/bin/ideviceinstaller")


def local(data=b"%PDF-1.4 menu", name="menu.pdf"):
    folder = Path(tempfile.mkdtemp(prefix="mobster-files-phone-"))
    path = folder / name
    path.write_bytes(data)
    return path


class PhoneFilesTests(unittest.TestCase):
    def test_put_uses_a_bounded_private_snapshot_across_reservation(self):
        original = b"original file"
        source = local(original)
        fake = FakePhone(apps={"com.apple.Pages": {}})
        staged = []

        def transfer(argv, timeout):
            if "put" in argv:
                staged.append(Path(argv[-2]))
                self.assertNotEqual(staged[-1], source.resolve())
                self.assertEqual(staged[-1].stat().st_mode & 0o777, 0o600)
            return fake(argv, timeout)

        with patch.object(phone_files, "MAX_BYTES", len(original)):
            result = phone(transfer).put("com.apple.Pages", source,
                                         before_copy=lambda: source.write_bytes(b"replacement" * 20))
        self.assertEqual(fake.apps["com.apple.Pages"]["menu.pdf"], original)
        self.assertEqual(result["bytes"], len(original))
        self.assertEqual(len(staged), 1)
        self.assertFalse(staged[0].exists())

    def test_put_rejects_actual_oversize_before_reservation_or_device_work(self):
        source = local(b"original")
        stale = source.stat()
        source.write_bytes(b"original plus more")
        fake = FakePhone(apps={"com.apple.Pages": {}})
        reserved = []
        with patch.object(phone_files, "MAX_BYTES", stale.st_size), patch.object(Path, "stat", return_value=stale), \
                self.assertRaises(PhoneIOError) as caught:
            phone(fake).put("com.apple.Pages", source, before_copy=lambda: reserved.append(True))
        self.assertEqual(caught.exception.code, "usage")
        self.assertEqual(reserved, [])
        self.assertEqual(fake.calls, [])

    def test_get_keeps_a_concurrent_output_and_a_dangling_symlink(self):
        fake = FakePhone(apps={"com.apple.Pages": {"notes.txt": b"from the phone"}})
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            original = folder / "notes.txt"
            link, replace = os.link, os.replace

            def competing_writer(operation):
                def publish(source, target, *args, **kwargs):
                    if Path(target) == original and not original.exists():
                        original.write_bytes(b"other writer")
                    return operation(source, target, *args, **kwargs)
                return publish

            with patch.object(os, "link", side_effect=competing_writer(link)), \
                    patch.object(os, "replace", side_effect=competing_writer(replace)):
                saved = phone(fake).get("com.apple.Pages", "notes.txt", folder)
            self.assertEqual(original.read_bytes(), b"other writer")
            self.assertEqual((saved.name, saved.read_bytes()), ("notes 2.txt", b"from the phone"))
            original.unlink()
            original.symlink_to(folder / "absent")
            saved = phone(fake).get("com.apple.Pages", "notes.txt", folder)
            self.assertTrue(original.is_symlink())
            self.assertEqual((saved.name, saved.read_bytes()), ("notes 3.txt", b"from the phone"))

    def test_only_apps_that_share_files_are_listed(self):
        fake = FakePhone(apps={"com.apple.Pages": {}, "com.apple.Numbers": {}}, others=["com.apple.MobileSMS"])
        self.assertEqual(phone(fake).apps(), [{"bundleId": "com.apple.Numbers", "name": "Numbers"},
                                              {"bundleId": "com.apple.Pages", "name": "Pages"}])
        listing = fake.calls[0]
        self.assertEqual(listing[1:4], ["-u", FakePhone.UDID, "list"])
        self.assertIn("UIFileSharingEnabled", listing)

    def test_ls_reads_names_sizes_and_times_even_with_spaces(self):
        fake = FakePhone(apps={"com.apple.Pages": {"Trip plan (final).pages": b"x" * 1234, "a.txt": b"hi"}})
        entries = phone(fake).ls("com.apple.Pages")
        self.assertEqual([(e["name"], e["bytes"], e["folder"]) for e in entries],
                         [("a.txt", 2, False), ("Trip plan (final).pages", 1234, False)])
        self.assertIsInstance(entries[0]["modifiedAt"], int)
        self.assertEqual(fake.calls[0][:7], ["/opt/homebrew/bin/afcclient", "-u", FakePhone.UDID, "--documents",
                                             "com.apple.Pages", "--", "ls"])

    def test_put_never_overwrites_and_checks_the_copy_arrived(self):
        fake = FakePhone(apps={"com.apple.Pages": {"menu.pdf": b"older"}})
        result = phone(fake).put("com.apple.Pages", local())
        self.assertEqual(result, {"name": "menu 2.pdf", "path": "/menu 2.pdf", "bytes": 13, "app": "com.apple.Pages"})
        self.assertEqual(fake.apps["com.apple.Pages"]["menu.pdf"], b"older")
        result = phone(fake).put("com.apple.Pages", local(), replace=True)
        self.assertEqual(result["name"], "menu.pdf")
        self.assertEqual(fake.apps["com.apple.Pages"]["menu.pdf"], b"%PDF-1.4 menu")
        put = next(c for c in fake.calls if "put" in c and "-f" in c)
        self.assertTrue(put[-2].startswith("/"))

    def test_a_copy_that_doesnt_arrive_whole_is_an_error(self):
        fake = FakePhone(apps={"com.apple.Pages": {}})
        original = fake.__call__

        def truncating(argv, timeout=None):
            code, out = original(argv, timeout)
            if "put" in argv:
                fake.apps["com.apple.Pages"]["menu.pdf"] = b"%PDF"
            return code, out
        with self.assertRaises(PhoneIOError) as caught:
            phone(truncating).put("com.apple.Pages", local())
        self.assertIn("didn't arrive whole", str(caught.exception))

    def test_copy_reservation_follows_preflight_and_precedes_the_only_put(self):
        source = local()
        for app, path in (("com.apple.Pages", source.with_name("missing.pdf")), ("com.apple.MobileSMS", source)):
            with self.subTest(app=app, path=path):
                fake = FakePhone(apps={"com.apple.Pages": {}})
                reserved = []
                with self.assertRaises(PhoneIOError):
                    phone(fake).put(app, path, before_copy=lambda: reserved.append(True))
                self.assertEqual(reserved, [])
                self.assertFalse(any("put" in call for call in fake.calls))

        fake = FakePhone(apps={"com.apple.Pages": {"menu.pdf": b"older"}})
        reserved = []
        result = phone(fake).put("com.apple.Pages", source,
                                 before_copy=lambda: reserved.append([call[-3:] for call in fake.calls]))
        self.assertEqual(result["name"], "menu 2.pdf")
        self.assertEqual(len(reserved), 1)
        self.assertEqual(len(reserved[0]), 1)
        self.assertFalse(any("put" in call for call in reserved[0]))
        self.assertEqual(sum("put" in call for call in fake.calls), 1)

    def test_a_copy_reservation_refusal_never_dispatches(self):
        fake = FakePhone(apps={"com.apple.Pages": {}})

        def refuse():
            raise RuntimeError("journal unavailable")

        with self.assertRaisesRegex(RuntimeError, "journal unavailable"):
            phone(fake).put("com.apple.Pages", local(), before_copy=refuse)
        self.assertFalse(any("put" in call for call in fake.calls))

    def test_names_never_leave_the_apps_folder(self):
        fake = FakePhone(apps={"com.apple.Pages": {}})
        result = phone(fake).put("com.apple.Pages", local(), name="../../../private/var/evil.pdf")
        self.assertEqual(result["path"], "/evil.pdf")
        with self.assertRaises(PhoneIOError):
            phone(fake).get("com.apple.Pages", "../evil.pdf", tempfile.mkdtemp())
        with self.assertRaises(PhoneIOError):
            phone(fake).ls("com.apple.Pages; rm -rf /")

    def test_get_copies_a_file_back_without_clobbering(self):
        fake = FakePhone(apps={"com.apple.Pages": {"notes.txt": b"from the phone"}})
        folder = Path(tempfile.mkdtemp())
        (folder / "notes.txt").write_text("already here")
        saved = phone(fake).get("com.apple.Pages", "notes.txt", folder)
        self.assertEqual((saved.name, saved.read_bytes()), ("notes 2.txt", b"from the phone"))
        self.assertEqual((folder / "notes.txt").read_text(), "already here")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["notes 2.txt", "notes.txt"])
        with self.assertRaises(PhoneIOError) as missing:
            phone(fake).get("com.apple.Pages", "nope.txt", folder)
        self.assertEqual(missing.exception.code, "not_found")

    def test_land_never_replaces_a_file_that_appears_first(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "a.txt").write_text("first")
        (folder / "a 2.txt").write_text("second")
        partial = folder / "partial"
        partial.write_text("new")
        landed = phone_files.land(partial, folder, "a.txt")
        self.assertEqual((landed.name, landed.read_text(), partial.exists()), ("a 3.txt", "new", False))
        self.assertEqual(((folder / "a.txt").read_text(), (folder / "a 2.txt").read_text()), ("first", "second"))

    def test_land_claims_the_name_with_a_hard_link_not_an_exists_check(self):
        folder = Path(tempfile.mkdtemp())
        partial = folder / "partial"
        partial.write_text("new")
        with patch("os.path.exists", side_effect=AssertionError("checked before claiming")), \
                patch("pathlib.Path.exists", side_effect=AssertionError("checked before claiming")):
            self.assertEqual(phone_files.land(partial, folder, "a.txt").read_text(), "new")

    def test_land_without_hard_links_still_refuses_to_replace(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "a.txt").write_text("first")
        partial = folder / "partial"
        partial.write_text("new")
        with patch("os.link", side_effect=PermissionError("no links here")):
            self.assertEqual(phone_files.land(partial, folder, "a.txt").name, "a 2.txt")
            partial.write_text("again")
            with self.assertRaises(FileExistsError):
                phone_files.land(partial, folder, "a.txt", exact=True)
        self.assertEqual((folder / "a.txt").read_text(), "first")

    def test_the_command_comes_after_double_dash_so_its_flags_stay_its_own(self):
        fake = FakePhone(apps={"com.apple.Pages": {"menu.pdf": b"older"}})
        files = phone(fake)
        files.ls("com.apple.Pages")
        files.put("com.apple.Pages", local(), replace=True)
        for argv in fake.calls:
            if Path(argv[0]).name == "afcclient":
                command = argv[argv.index("--") + 1:]
                self.assertIn(command[0], ("ls", "put", "get"))
                self.assertEqual(argv[:argv.index("--")], ["/opt/homebrew/bin/afcclient", "-u", FakePhone.UDID,
                                                           "--documents", "com.apple.Pages"])
        self.assertEqual(FakePhone()(["/x/afcclient", "-u", FakePhone.UDID, "--documents", "com.apple.Pages", "ls",
                                      "-l", "/"])[0], 2)

    def test_an_app_without_file_sharing_says_so(self):
        with self.assertRaises(PhoneIOError) as caught:
            phone(FakePhone()).ls("com.apple.MobileSMS")
        self.assertEqual(caught.exception.code, "no_file_sharing")
        self.assertIn("Pages, Numbers or Keynote", caught.exception.fix)

    def test_usb_only_and_never_a_network_connection(self):
        for record, code in ((usb_record(transport="wifi"), "needs_cable"), (usb_record(kind="simulator"), "usage"),
                             (usb_record(kind="wda"), "usage"), (usb_record(udid=None), "no_device")):
            with self.subTest(record=record), self.assertRaises(PhoneIOError) as caught:
                phone(FakePhone(), record)
            self.assertEqual(caught.exception.code, code)
        fake = FakePhone(apps={"com.apple.Pages": {"a.txt": b"a"}})
        files = phone(fake)
        files.ls("com.apple.Pages")
        files.put("com.apple.Pages", local())
        files.get("com.apple.Pages", "a.txt", tempfile.mkdtemp())
        for argv in fake.calls:
            self.assertNotIn("-n", argv)
            self.assertNotIn("--network", argv)

    def test_under_no_devices_the_real_tools_never_run(self):
        with patch.dict(os.environ, {"MOBSTER_NO_DEVICES": "1"}), patch("subprocess.run") as run:
            with self.assertRaises(RuntimeError):
                PhoneFiles(usb_record(), afc="/x/afcclient", installer="/x/ideviceinstaller").ls("com.apple.Pages")
            run.assert_not_called()

    def test_missing_tools_say_how_to_get_them(self):
        with patch("mobile_agent.mac_tools.tool", lambda name: None), self.assertRaises(PhoneIOError) as caught:
            PhoneFiles(usb_record(), runner=FakePhone()).ls("com.apple.Pages")
        self.assertEqual(caught.exception.code, "tools")
        self.assertIn("brew install libimobiledevice", caught.exception.fix)

    def test_tool_errors_become_plain_sentences(self):
        for output, code in (("ERROR: No device found!", "no_device"),
                             ("ERROR: Could not connect to lockdownd: Password protected (-17)", "locked"),
                             ("Error: Failed to get file info for /x: Object not found (8)", "not_found")):
            with self.subTest(output=output):
                self.assertEqual(phone_files._explain(output, "Studio iPhone").code, code)


class CliTests(unittest.TestCase):
    def test_get_explicit_filename_keeps_a_dangling_destination_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "chosen.txt"
            target.symlink_to(Path(tmp) / "absent")
            code, _, _, _ = self.run_cli("file", "get", "notes.txt", "--app", "com.apple.Pages", "-o", str(target))
            self.assertEqual(code, 2)
            self.assertTrue(target.is_symlink())
            self.assertEqual(list(Path(tmp).iterdir()), [target])

    def run_cli(self, *argv, fake=None):
        from mobile_agent.phone_io import cli
        parser = argparse.ArgumentParser()
        cli.add_arguments(parser, {"path_type": str})
        args = parser.parse_args(list(argv))
        fake = fake or FakePhone(apps={"com.apple.Pages": {"notes.txt": b"hi"}})
        out, err = io.StringIO(), io.StringIO()
        real = PhoneFiles

        def factory(record, runner=None, container=False):
            return real(record, runner=fake, afc="/x/afcclient", installer="/x/ideviceinstaller", container=container)
        with patch("mobile_agent.phone_io.cli.choose_device", return_value=usb_record()), \
                patch("mobile_agent.phone_io.files.PhoneFiles", factory), redirect_stdout(out), redirect_stderr(err):
            code = cli.run(args)
        return code, out.getvalue(), err.getvalue(), fake

    def test_put_get_and_ls(self):
        path = local()
        code, out, _, fake = self.run_cli("file", "put", str(path), "--app", "com.apple.Pages")
        self.assertEqual((code, out.strip()), (0, "Put menu.pdf in com.apple.Pages's folder on Studio iPhone."))
        self.assertIn("menu.pdf", fake.apps["com.apple.Pages"])
        code, out, _, _ = self.run_cli("file", "ls", "--json")
        self.assertEqual(json.loads(out)["apps"], [{"bundleId": "com.apple.Pages", "name": "Pages"}])
        code, out, _, _ = self.run_cli("file", "ls", "--app", "com.apple.Pages")
        self.assertEqual(out.strip(), "notes.txt  2 bytes")
        target = Path(tempfile.mkdtemp()) / "copy.txt"
        code, out, _, _ = self.run_cli("file", "get", "notes.txt", "--app", "com.apple.Pages", "-o", str(target))
        self.assertEqual((code, target.read_text()), (0, "hi"))

    def test_get_to_a_name_that_exists_refuses_and_keeps_the_file(self):
        target = Path(tempfile.mkdtemp()) / "copy.txt"
        target.write_text("mine")
        code, _, err, _ = self.run_cli("file", "get", "notes.txt", "--app", "com.apple.Pages", "-o", str(target))
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["copy.txt"])
        self.assertEqual(target.read_text(), "mine")

    def test_failures_exit_with_their_code_and_a_sentence(self):
        code, _, err, _ = self.run_cli("file", "ls", "--app", "com.apple.MobileSMS")
        self.assertEqual(code, 1)
        self.assertIn("doesn't keep files you can see in Files", err)
        code, _, err, _ = self.run_cli("file", "put", "/nope/missing.pdf", "--app", "com.apple.Pages")
        self.assertEqual(code, 2)
        self.assertIn("There is no file at", err)


class McpTests(unittest.TestCase):
    def setUp(self):
        from mobile_agent.tests.seam_support import isolate
        isolate(self)

    def toolset(self, **kwargs):
        from mobile_agent import tracks
        from mobile_agent.mcp_server.tools import ToolSet
        tracks.load()
        tools = ToolSet(runs_dir=tempfile.mkdtemp(prefix="mobster-files-mcp-"), keyless=True,
                        devices=lambda: [usb_record()], **kwargs)
        self.addCleanup(tools.shutdown)
        return tools

    def provider(self, fake, cwd=None):
        from mobile_agent.attachments.mcp import FilesTools
        from mobile_agent.mcp_server import registry
        provider = next(p for p in registry.providers() if p.name == "files")
        patcher = patch.object(service, "phone_files", lambda record: PhoneFiles(
            record, runner=fake, afc="/x/afcclient", installer="/x/ideviceinstaller"))
        patcher.start()
        self.addCleanup(patcher.stop)
        provider._cwd = cwd
        self.assertIsInstance(provider, FilesTools)
        return provider

    def test_the_file_tools_are_listed_only_with_allow_files(self):
        names = {tool["name"] for tool in self.toolset().list_tools()}
        self.assertFalse(names & {"put_file", "get_file", "list_files"})
        listed = {tool["name"]: tool for tool in self.toolset(allow_files=True).list_tools()}
        self.assertEqual(listed["put_file"]["annotations"]["readOnlyHint"], False)
        self.assertTrue(listed["put_file"]["annotations"]["destructiveHint"])
        self.assertTrue(listed["list_files"]["annotations"]["readOnlyHint"])
        self.assertIn("never the camera roll", listed["put_file"]["description"])

    def test_without_allow_files_the_file_tools_can_not_be_called(self):
        from mobile_agent.mcp_server.protocol import ProtocolError
        tools = self.toolset()
        fake = FakePhone(apps={"com.apple.Pages": {"notes.txt": b"hello"}})
        self.provider(fake)
        for name, arguments in (("put_file", {"device": "sams-iphone", "path": str(local()), "app": "com.apple.Pages"}),
                                ("get_file", {"device": "sams-iphone", "app": "com.apple.Pages", "name": "notes.txt"}),
                                ("list_files", {"device": "sams-iphone"})):
            with self.subTest(tool=name), self.assertRaises(ProtocolError):
                tools.call(name, arguments, None)
        self.assertEqual(fake.calls, [])

    def test_put_list_and_get(self):
        tools = self.toolset(allow_files=True)
        fake = FakePhone(apps={"com.apple.Pages": {"notes.txt": b"hello"}})
        cwd = tempfile.mkdtemp()
        self.provider(fake, cwd)
        result = tools.call("put_file", {"device": "Studio iPhone", "path": str(local()), "app": "com.apple.Pages"},
                            None)
        self.assertFalse(result.is_error, result.text)
        self.assertIn("Put menu.pdf in com.apple.Pages's folder", result.text)
        result = tools.call("list_files", {"device": "sams-iphone"}, None)
        self.assertIn("Pages (com.apple.Pages)", result.text)
        result = tools.call("get_file", {"device": "sams-iphone", "app": "com.apple.Pages", "name": "notes.txt"},
                            None)
        self.assertEqual((Path(cwd) / "notes.txt").read_bytes(), b"hello")
        self.assertFalse(result.is_error, result.text)

    def test_put_needs_one_destination_and_an_absolute_path(self):
        tools = self.toolset(allow_files=True)
        self.provider(FakePhone())
        for arguments, words in (({"path": str(local())}, "not both"),
                                 ({"path": str(local()), "app": "com.apple.Pages", "clipboard": True}, "not both"),
                                 ({"path": "menu.pdf", "app": "com.apple.Pages"}, "absolute path")):
            with self.subTest(arguments=arguments):
                result = tools.call("put_file", {"device": "sams-iphone", **arguments}, None)
                self.assertTrue(result.is_error)
                self.assertIn(words, result.text)

    def test_a_device_the_server_may_not_drive_is_refused(self):
        tools = self.toolset(allow_files=True, allow_devices=["Work iPhone"])
        fake = FakePhone()
        self.provider(fake)
        result = tools.call("list_files", {"device": "sams-iphone"}, None)
        self.assertTrue(result.is_error)
        self.assertIn("--allow-device", result.text)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
