import contextlib
import getpass
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.request
import urllib.response
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent.phone_io import PhoneIOError, cli, secrets
from mobile_agent.tests.test_phone_io import PHONE
from mobile_agent.wda import patches as wda_patches
from mobile_agent.wda import source as wda_source


class SecretTests(unittest.TestCase):
    def attempt(self, answers=None, read=None, journal=None):
        answers = answers or [{"ok": True, "version": 1}, {"ok": True, "code": "sent"}]
        calls = []

        def call(method, path, body, timeout):
            calls.append((method, path, body))
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        return calls, lambda: secrets.enter(call, app="example.test", field="password",
                                            read=read or (lambda: "test-only-secret"), journal=journal
                                            or (lambda e: None))

    def test_one_entry_after_preflight_and_durable_intent_without_secret_metadata(self):
        events = []
        calls, run = self.attempt(journal=lambda event: events.append((len(calls), event)))
        self.assertEqual(run(), {"ok": True, "sent": True, "submitted": False})
        self.assertEqual([call[1] for call in calls], ["/mobster/secret/check", "/mobster/secret"])
        self.assertNotIn("secret", calls[0][2])
        self.assertEqual(calls[1][2]["secret"], "test-only-secret")
        self.assertEqual([count for count, _ in events], [1, 2])
        self.assertEqual(events[0][1]["attempt"], events[1][1]["attempt"])
        self.assertNotIn("test-only-secret", json.dumps(events))
        self.assertEqual(set(events[0][1]), {"event", "attempt"})

    def test_unverified_preflight_does_not_read_or_send_password(self):
        for answer in (None, {"ok": True}, {"ok": True, "version": 2}, {"ok": False, "code": "field"},
                       {"ok": True, "version": True}, {"ok": True, "version": 1.0},
                       {"ok": False, "code": "foreground"}, PhoneIOError("old runner", "unknown_route")):
            with self.subTest(answer=answer):
                read = mock.Mock()
                calls, run = self.attempt([answer], read=read)
                with self.assertRaises(PhoneIOError):
                    run()
                read.assert_not_called()
                self.assertEqual(len(calls), 1)

    def test_unknown_outcome_never_retries_or_exposes_remote_exception(self):
        for answer in (RuntimeError("test-only-secret"), KeyboardInterrupt(), None, {"ok": True},
                       {"ok": False, "code": "unknown"}):
            with self.subTest(answer=answer):
                calls, run = self.attempt([{"ok": True, "version": 1}, answer])
                with self.assertRaises(PhoneIOError) as caught:
                    run()
                self.assertEqual(caught.exception.code, "unknown_outcome")
                self.assertNotIn("test-only-secret", str(caught.exception))
                self.assertEqual(len(calls), 2)

    def test_journal_failure_prevents_mutation_and_completion_failure_does_not_retry(self):
        for fail_at in (1, 2):
            count = 0

            def journal(event):
                nonlocal count
                count += 1
                if count == fail_at:
                    raise OSError("disk full")

            calls, run = self.attempt(journal=journal)
            with self.assertRaises(Exception):
                run()
            self.assertEqual(len(calls), fail_at)

    def test_interrupted_completion_is_reported_without_another_entry(self):
        def journal(event):
            if event["event"] == "password_entry_sent":
                raise KeyboardInterrupt

        calls, run = self.attempt(journal=journal)
        with self.assertRaises(PhoneIOError) as caught:
            run()
        self.assertEqual(caught.exception.code, "unknown_outcome")
        self.assertEqual(len(calls), 2)

    def test_springboard_passcode_target_is_refused_without_device_call(self):
        call = mock.Mock()
        with self.assertRaises(PhoneIOError):
            secrets.enter(call, app="com.apple.springboard", field="passcode", read=mock.Mock(), journal=mock.Mock())
        call.assert_not_called()

    def test_private_file_preserves_spaces_and_rejects_public_symlink_binary_and_controls(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret"
            path.write_text("  test-only secret  \r\n")
            path.chmod(0o600)
            self.assertEqual(secrets.read_secret(path), "  test-only secret  ")
            path.chmod(0o644)
            with self.assertRaises(PhoneIOError):
                secrets.read_secret(path)
            path.chmod(0o600)
            link = Path(folder) / "link"
            link.symlink_to(path)
            with self.assertRaises(PhoneIOError):
                secrets.read_secret(link)
            for content in (b"", b"\xff", b"a\nb", b"a\x00b", b"a" * 257,
                            "a\u2028b".encode(), "a\u2029b".encode(), "a\u200db".encode()):
                path.write_bytes(content)
                with self.subTest(content=content[:10]), self.assertRaises(PhoneIOError):
                    secrets.read_secret(path)

    def test_getpass_echo_fallback_is_refused_before_password_read(self):
        def prompt(message):
            warnings.warn("cannot hide", getpass.GetPassWarning)
            self.fail("continued into echoed input")

        with mock.patch.object(getpass, "getpass", side_effect=prompt), self.assertRaises(PhoneIOError):
            secrets.read_secret()

    def test_only_loopback_transport_is_accepted(self):
        for url in ("http://127.0.0.1:8100", "http://[::1]:8100"):
            secrets._local_transport({"wdaUrl": url})
        for url in ("http://192.168.1.3:8100", "http://127.0.0.1.evil:8100", "http://user@127.0.0.1:8100",
                    "http://127.0.0.1:8100/path", "http://127.0.0.1:0", "http://127.0.0.1:wrong",
                    "http://127.0.0.1:65536", "", "file:///tmp/wda"):
            with self.subTest(url=url), self.assertRaises(PhoneIOError):
                secrets._local_transport({"wdaUrl": url})

    def test_cli_holds_lease_and_outputs_only_status(self):
        from mobile_agent.cli_parser import build_parser
        held = False

        @contextlib.contextmanager
        def wda(record, *, opener):
            nonlocal held
            held = True
            try:
                yield lambda *args: {"ok": True, "version": 1, "code": "sent"}
            finally:
                held = False

        def read(path):
            self.assertTrue(held)
            return "test-only-secret"

        args = build_parser().parse_args(["phone", "password", "--app", "example.test", "--field", "password",
                                          "--json"])
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(secrets, "choose_device", return_value=PHONE), \
                mock.patch.object(secrets, "wda", side_effect=wda), mock.patch.object(secrets, "read_secret",
                                                                                      side_effect=read), \
                mock.patch("mobile_agent.paths.user_data_dir", return_value=Path(folder)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.run(args), 0)
            self.assertEqual(json.loads(output.getvalue()), {"ok": True, "sent": True, "submitted": False})
            self.assertNotIn("test-only-secret", (Path(folder) / "private-input/attempts.jsonl").read_text())
        self.assertFalse(held)

    def test_cli_stays_local_with_proxy_settings_and_never_follows_a_redirect(self):
        real_build, real_wda = urllib.request.build_opener, secrets.wda
        for redirect in (None, "check", "send"):
            requests = []

            class FakeHTTP(urllib.request.HTTPHandler):
                def http_open(self, request):
                    requests.append(request)
                    checking = request.full_url.endswith("/check")
                    status = 302 if redirect == ("check" if checking else "send") and request.data else 200
                    value = {"ok": True, "version": 1} if checking else {"ok": True, "code": "sent"}
                    if request.full_url.endswith("/status"):
                        value = {"ready": True}
                    body = json.dumps({"sessionId": "S1", "value": value}).encode()
                    response = urllib.response.addinfourl(io.BytesIO(body), {"Location": "http://remote.invalid/"},
                                                         request.full_url, status)
                    response.msg = "Found" if status == 302 else "OK"
                    return response

            def leased(record, *, opener):
                return real_wda(record, opener=opener, lease=lambda url: SimpleNamespace(close=lambda: None))

            read = mock.Mock(return_value="test-only-secret")
            args = SimpleNamespace(device=None, app="example.test", field="password", file=None, json=True)
            with self.subTest(redirect=redirect), tempfile.TemporaryDirectory() as folder, \
                    mock.patch.object(secrets, "choose_device", return_value=PHONE), \
                    mock.patch.object(secrets, "wda", side_effect=leased), \
                    mock.patch.object(secrets, "read_secret", read), \
                    mock.patch("mobile_agent.paths.user_data_dir", return_value=Path(folder)), \
                    mock.patch.dict(os.environ, {"http_proxy": "http://proxy.invalid:8080", "no_proxy": ""}), \
                    mock.patch("urllib.request.proxy_bypass", return_value=False), \
                    mock.patch("urllib.request.build_opener",
                               side_effect=lambda *handlers: real_build(*handlers, FakeHTTP())), \
                    contextlib.redirect_stdout(io.StringIO()):
                if redirect is None:
                    self.assertEqual(secrets.run(args), 0)
                else:
                    with self.assertRaises(PhoneIOError) as caught:
                        secrets.run(args)
                    self.assertEqual(caught.exception.code, "unsupported" if redirect == "check" else "unknown_outcome")
            self.assertTrue(all(request.host == "127.0.0.1:8101" for request in requests))
            self.assertEqual(sum(request.full_url.endswith(secrets.ROUTE) for request in requests),
                             0 if redirect == "check" else 1)
            self.assertEqual(read.call_count, 0 if redirect == "check" else 1)


class NativePatchTests(unittest.TestCase):
    def test_secret_patches_are_idempotent_and_part_of_managed_build(self):
        self.assertTrue(all(patch in wda_patches.WDA_PATCHES for patch in wda_patches.SECRET_PATCHES))
        fixture = json.loads((Path(__file__).parent / "fixtures/wda-private-input.json").read_text())
        self.assertEqual(fixture["commit"], wda_source.COMMIT)
        files = fixture["files"]
        patches = tuple(patch for patch in wda_patches.WDA_PATCHES if patch[0] in files)
        with tempfile.TemporaryDirectory() as folder:
            for name, content in files.items():
                path = Path(folder) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            with mock.patch.object(wda_patches, "WDA_PATCHES", patches):
                wda_patches.patch_wda(folder)
                first = {name: (Path(folder) / name).read_text() for name in files}
                wda_patches.patch_wda(folder)
                self.assertEqual(first, {name: (Path(folder) / name).read_text() for name in files})
            commands = first["WebDriverAgentLib/Commands/FBCustomCommands.m"]
            self.assertEqual(commands.count("+ (id<FBResponsePayload>)handleMobsterSecret:"), 1)
            self.assertIn("shouldRedact:YES", commands)
            snapshot = commands.index("id<FBXCElementSnapshot> snapshot")
            foreground = commands.index("XCUIApplication.fb_activeApplication.bundleID", snapshot)
            self.assertLess(foreground, commands.index("synthesizeEvent:event"))
            server = first["WebDriverAgentLib/Routing/FBWebServer.m"]
            self.assertIn('routeParams.URL.path.lowercaseString containsString:@"/mobster/secret"', server)

    def test_a_changed_logging_anchor_refuses_and_a_repaired_partial_patch_converges(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/wda-private-input.json").read_text())
        files = fixture["files"]
        patches = tuple(patch for patch in wda_patches.WDA_PATCHES if patch[0] in files)
        with tempfile.TemporaryDirectory() as folder:
            for name, content in files.items():
                path = Path(folder) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            server = Path(folder) / "WebDriverAgentLib/Routing/FBWebServer.m"
            original = server.read_text()
            server.write_text(original.replace("[FBLogger verboseLog:routeParams.description];", "// changed logger"))
            with mock.patch.object(wda_patches, "WDA_PATCHES", patches):
                with self.assertRaises(RuntimeError):
                    wda_patches.patch_wda(folder)
                server.write_text(original)
                wda_patches.patch_wda(folder)
                first = {name: (Path(folder) / name).read_text() for name in files}
                wda_patches.patch_wda(folder)
            self.assertEqual(first, {name: (Path(folder) / name).read_text() for name in files})


class RepatchTests(unittest.TestCase):
    def files(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/wda-private-input.json").read_text())
        return fixture["files"]

    def older(self, patches):
        out = []
        for name, anchor, patched in patches:
            if "handleMobsterSecret:(FBRouteRequest" in patched:
                patched = patched.replace("Never include request arguments", "An older version of this route")
            out.append((name, anchor, patched))
        return tuple(out)

    def write(self, folder, files):
        for name, content in files.items():
            path = Path(folder) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def git(self, folder, *args):
        subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True)

    def test_a_git_checkout_patched_by_an_older_mobster_is_patched_again_from_the_release(self):
        files = self.files()
        patches = tuple(patch for patch in wda_patches.WDA_PATCHES if patch[0] in files)
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as clean:
            self.write(folder, files)
            self.git(folder, "init", "-q")
            self.git(folder, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
            self.git(folder, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "release")
            with mock.patch.object(wda_patches, "WDA_PATCHES", self.older(patches)):
                wda_patches.patch_wda(folder)
            commands = Path(folder) / "WebDriverAgentLib/Commands/FBCustomCommands.m"
            self.assertIn("An older version of this route", commands.read_text())
            self.write(clean, files)
            with mock.patch.object(wda_patches, "WDA_PATCHES", patches):
                wda_patches.patch_wda(folder)
                wda_patches.patch_wda(clean)
                again = {name: (Path(folder) / name).read_text() for name in files}
                wda_patches.patch_wda(folder)
            text = commands.read_text()
            self.assertEqual(text.count("+ (id<FBResponsePayload>)handleMobsterSecret:"), 1)
            self.assertNotIn("An older version of this route", text)
            self.assertEqual(again, {name: (Path(clean) / name).read_text() for name in files})
            self.assertEqual(again, {name: (Path(folder) / name).read_text() for name in files})

    def test_a_tree_without_git_that_an_older_mobster_patched_is_refused_and_left_as_it_is(self):
        files = self.files()
        patches = tuple(patch for patch in wda_patches.WDA_PATCHES if patch[0] in files)
        with tempfile.TemporaryDirectory() as folder:
            self.write(folder, files)
            with mock.patch.object(wda_patches, "WDA_PATCHES", self.older(patches)):
                wda_patches.patch_wda(folder)
            before = {name: (Path(folder) / name).read_text() for name in files}
            with mock.patch.object(wda_patches, "WDA_PATCHES", patches), \
                    self.assertRaisesRegex(RuntimeError, "Delete .* and build again"):
                wda_patches.patch_wda(folder)
            self.assertEqual(before, {name: (Path(folder) / name).read_text() for name in files})

    def test_a_patch_that_does_not_fit_writes_nothing(self):
        files = self.files()
        patches = tuple(patch for patch in wda_patches.WDA_PATCHES if patch[0] in files)
        with tempfile.TemporaryDirectory() as folder:
            self.write(folder, files)
            broken = patches + ((patches[0][0], "// an anchor this release does not have\n",
                                 "// Mobster: an edit that has nowhere to go\n"),)
            with mock.patch.object(wda_patches, "WDA_PATCHES", broken), self.assertRaises(RuntimeError):
                wda_patches.patch_wda(folder)
            self.assertEqual(files, {name: (Path(folder) / name).read_text() for name in files})


if __name__ == "__main__":
    unittest.main()
