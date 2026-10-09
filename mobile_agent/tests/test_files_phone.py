"""Track files: files to and from an iPhone app's folder. The phone file service over a fake afcclient and
ideviceinstaller (names, never overwriting, the check after a copy, USB only, never -n), `mobster phone file`, and
the MCP tools behind --allow-files. Offline: nothing here reaches a phone, and under MOBSTER_NO_DEVICES=1 the real
tools refuse to run."""

import argparse
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from mobile_agent.phone_io import PhoneIOError, files as phone_files
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
        self.assertTrue(put[-2].startswith("/"))  # absolute local path: never read as an option

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

    def test_the_command_comes_after_double_dash_so_its_flags_stay_its_own(self):
        # macOS's getopt_long reorders arguments: without "--", "ls -l" and "put -f" are refused as afcclient's own
        # unknown options (exit 2). The fake reads options the same way.
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
        with patch.object(phone_files, "afc_tool", lambda: None), self.assertRaises(PhoneIOError) as caught:
            PhoneFiles(usb_record(), runner=FakePhone()).ls("com.apple.Pages")
        self.assertEqual(caught.exception.code, "tools")
        self.assertIn("brew install libimobiledevice", caught.exception.fix)

    def test_tool_errors_become_plain_sentences(self):
        for output, code in (("ERROR: No device found!", "no_device"),
                             ("ERROR: Could not connect to lockdownd: Password protected (-17)", "locked"),
                             ("Error: Failed to get file info for /x: Object not found (8)", "not_found")):
            with self.subTest(output=output):
                self.assertEqual(phone_files._explain(output, "Sam's iPhone").code, code)


class CliTests(unittest.TestCase):
    def run_cli(self, *argv, fake=None):
        from mobile_agent.phone_io import cli
        parser = argparse.ArgumentParser()
        cli.add_arguments(parser, {"path_type": str})
        args = parser.parse_args(list(argv))
        fake = fake or FakePhone(apps={"com.apple.Pages": {"notes.txt": b"hi"}})
        out, err = io.StringIO(), io.StringIO()
        real = PhoneFiles

        def factory(record, runner=None):
            return real(record, runner=fake, afc="/x/afcclient", installer="/x/ideviceinstaller")
        with patch("mobile_agent.phone_io.cli.choose_device", return_value=usb_record()), \
                patch("mobile_agent.phone_io.files.PhoneFiles", factory), redirect_stdout(out), redirect_stderr(err):
            code = cli.run(args)
        return code, out.getvalue(), err.getvalue(), fake

    def test_put_get_and_ls(self):
        path = local()
        code, out, _, fake = self.run_cli("file", "put", str(path), "--app", "com.apple.Pages")
        self.assertEqual((code, out.strip()), (0, "Put menu.pdf in com.apple.Pages's folder on Sam's iPhone."))
        self.assertIn("menu.pdf", fake.apps["com.apple.Pages"])
        code, out, _, _ = self.run_cli("file", "ls", "--json")
        self.assertEqual(json.loads(out)["apps"], [{"bundleId": "com.apple.Pages", "name": "Pages"}])
        code, out, _, _ = self.run_cli("file", "ls", "--app", "com.apple.Pages")
        self.assertEqual(out.strip(), "notes.txt  2 bytes")
        target = Path(tempfile.mkdtemp()) / "copy.txt"
        code, out, _, _ = self.run_cli("file", "get", "notes.txt", "--app", "com.apple.Pages", "-o", str(target))
        self.assertEqual((code, target.read_text()), (0, "hi"))

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
        provider._phone_files = lambda record: PhoneFiles(record, runner=fake, afc="/x/afcclient",
                                                          installer="/x/ideviceinstaller")
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
        result = tools.call("put_file", {"device": "Sam's iPhone", "path": str(local()), "app": "com.apple.Pages"},
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
