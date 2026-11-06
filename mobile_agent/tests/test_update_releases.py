import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import build_identity, links, update_check

PUBLISHED = "2026-10-20T00:00:00Z"
NOW = 1792670400
CLI_ASSETS = ("mobster-macos-arm64.tar.gz", "mobster-macos-arm64.tar.gz.sha256")


def release(tag="v0.5.0", assets=CLI_ASSETS, **extra):
    return {"tag_name": tag, "published_at": PUBLISHED, "draft": False, "prerelease": False,
            "assets": [{"name": name} for name in assets], **extra}


def package(version="0.5.0", *, yanked=False):
    return {"info": {"name": "mobster-cli", "version": version, "yanked": yanked},
            "urls": [{"packagetype": "bdist_wheel", "yanked": yanked, "upload_time_iso_8601": PUBLISHED}]}


class ReleasesFeedTests(unittest.TestCase):
    def fetch(self, body, *, frozen=False):
        response = io.BytesIO(json.dumps(body).encode())
        response.read = Mock(wraps=response.read)
        opener = Mock(return_value=response)
        with patch.object(update_check, "inside_app", return_value=False), patch.object(sys, "frozen", frozen,
                                                                                       create=True):
            found = update_check.latest_release(timeout=0.5, opener=opener)
        request = opener.call_args.args[0]
        self.assertIsNone(request.data)
        self.assertEqual(request.header_items(), [("Accept", "application/json"), ("User-agent", "mobster-cli")])
        self.assertEqual(opener.call_args.kwargs, {"timeout": 0.5})
        response.read.assert_called_once_with(200_000)
        self.assertTrue(response.closed)
        return found, request.full_url

    def test_python_checks_pypi_without_account_or_device_data(self):
        found, url = self.fetch(package())
        self.assertEqual(url, "https://pypi.org/pypi/mobster-cli/json")
        self.assertEqual(found, {"version": "0.5.0", "publishedAt": PUBLISHED})

    def test_python_requires_a_stable_non_yanked_distribution(self):
        cases = [package("0.5.0rc1"), package("0.5.0.dev1"), package(yanked=True),
                 package() | {"urls": []}, package() | {"urls": "bad"}, package() | {"info": []},
                 package() | {"info": {"name": "another-package", "version": "0.5.0"}},
                 package() | {"urls": [{"packagetype": "bdist_wheel", "yanked": True}]},
                 package() | {"urls": [{"packagetype": "bdist_wheel"}]},
                 package() | {"urls": [{"packagetype": "bdist_egg", "yanked": False}]}]
        for body in cases:
            with self.subTest(body=body):
                self.assertIsNone(self.fetch(body)[0])
        body = package() | {"urls": [{"packagetype": "sdist", "yanked": False}]}
        self.assertEqual(self.fetch(body)[0]["version"], "0.5.0")

    def test_frozen_checks_the_archive_release_with_both_required_assets(self):
        found, url = self.fetch(release(), frozen=True)
        self.assertEqual(url, "https://api.github.com/repos/RadishSoftware/mobster-releases/releases/latest")
        self.assertEqual(found, {"version": "0.5.0", "publishedAt": PUBLISHED})
        for body in (release(assets=()), release(assets=("Mobster.dmg",)), release(assets=CLI_ASSETS[:1]),
                     release(assets=CLI_ASSETS[1:]), release() | {"assets": "bad"},
                     release() | {"assets": [{"name": []}]}, release(draft=True), release(prerelease=True),
                     release(tag="v0.5.0rc1")):
            with self.subTest(body=body):
                self.assertIsNone(self.fetch(body, frozen=True)[0])

    def test_offline_and_invalid_responses_do_not_fail_commands(self):
        for frozen in (False, True):
            for body in ([], None, "not an object"):
                with self.subTest(frozen=frozen, body=body):
                    self.assertIsNone(self.fetch(body, frozen=frozen)[0])
        with patch.object(update_check, "inside_app", return_value=False):
            self.assertIsNone(update_check.latest_release(opener=Mock(side_effect=OSError("offline"))))

    def test_notices_offer_the_original_distribution_without_an_app_download(self):
        for frozen, location in ((False, links.PYPI_PROJECT), (True, links.CLI_RELEASES)):
            with self.subTest(frozen=frozen), patch.object(sys, "frozen", frozen, create=True):
                line = update_check.notice("0.3.0", {"latest": {"version": "0.4.0", "publishedAt": PUBLISHED}},
                                           now=NOW)
                self.assertEqual(line, "Mobster 0.4.0 is out, released 2 days ago (you have 0.3.0). "
                                       f"Upgrade with the installer you used: {location}")
                self.assertNotIn("Mobster for Mac", line)
                self.assertEqual(update_check.notice("0.3.0", {"minimum": "99.0.0"}), "")


class InstallationBoundaryTests(unittest.TestCase):
    def test_app_owned_agents_defer_even_when_moved_outside_the_bundle(self):
        for standalone, executable in ((False, "/tmp/mobster-agent"),
                                       (True, "/Applications/Mobster.app/Contents/MacOS/mobster")):
            with self.subTest(standalone=standalone, executable=executable), \
                    patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", executable), \
                    patch.object(build_identity, "STANDALONE_CLI", standalone), \
                    patch.object(update_check, "latest_release") as latest, redirect_stdout(io.StringIO()) as out:
                self.assertTrue(update_check.inside_app())
                self.assertFalse(update_check.enabled("doctor", SimpleNamespace(json=False)))
                self.assertEqual(update_check.run_update(SimpleNamespace(check=True)), 0)
                latest.assert_not_called()
                self.assertIn("belongs to Mobster for Mac", out.getvalue())
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", "/tmp/mobster"), \
                patch.object(build_identity, "STANDALONE_CLI", True):
            self.assertFalse(update_check.inside_app())

    def test_python_and_frozen_caches_ignore_the_old_app_cache(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch("mobile_agent.paths.user_data_dir", return_value=Path(folder)):
            old = Path(folder, "cli", "update-check.json")
            old.parent.mkdir()
            old.write_text(json.dumps({"latest": {"version": "99.0.0"}, "minimum": "99.0.0"}))
            for frozen, kind, version in ((False, "python", "0.5.0"), (True, "frozen", "0.4.0")):
                with patch.object(sys, "frozen", frozen, create=True):
                    self.assertEqual(update_check._cache_path().name, f"update-check-{kind}.json")
                    self.assertEqual(update_check._read_cache(), {})
                    update_check._write_cache({"checkedAt": 1, "latest": {"version": version}})
            with patch.object(sys, "frozen", False, create=True):
                self.assertEqual(update_check._read_cache()["latest"]["version"], "0.5.0")
            self.assertEqual(json.loads(old.read_text())["minimum"], "99.0.0")

    def test_daily_quiet_controls_do_not_start_a_fetch(self):
        tty = SimpleNamespace(isatty=lambda: True)
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "stdout", tty), \
                patch.object(sys, "stderr", tty), patch.object(update_check, "inside_app", return_value=False), \
                patch.object(update_check.threading, "Thread") as thread:
            for command in update_check.QUIET_COMMANDS:
                update_check.start(command, SimpleNamespace(json=False))
            update_check.start("doctor", SimpleNamespace(json=True))
            for env in ({"CI": "1"}, {"MOBSTER_NO_UPDATE_CHECK": "1"}):
                with patch.dict(os.environ, env):
                    update_check.start("doctor", SimpleNamespace(json=False))
            with patch.object(sys, "stdout", io.StringIO()):
                update_check.start("doctor", SimpleNamespace(json=False))
            thread.assert_not_called()

    def test_a_recent_check_is_reused_and_explicit_checks_still_work_when_daily_checks_are_disabled(self):
        args = SimpleNamespace(json=False, check=True)
        with patch.object(update_check, "enabled", return_value=True), patch.object(update_check, "_read_cache",
                                                                                   return_value={"checkedAt": 50}), \
                patch.object(update_check.time, "time", return_value=60), \
                patch.object(update_check.threading, "Thread") as thread:
            update_check.start("doctor", args)
            thread.assert_not_called()
        with patch.dict(os.environ, {"MOBSTER_NO_UPDATE_CHECK": "1"}), \
                patch.object(update_check, "inside_app", return_value=False), \
                patch.object(update_check, "latest_release", return_value=None) as latest, \
                patch.object(update_check, "_write_cache"), redirect_stdout(io.StringIO()):
            self.assertEqual(update_check.run_update(args), 1)
            latest.assert_called_once_with(timeout=5)


if __name__ == "__main__":
    unittest.main()
