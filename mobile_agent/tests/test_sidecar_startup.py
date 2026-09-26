"""Frozen desktop sidecar: where `serve` keeps state it must not write into the app bundle."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

import mobile_agent
from mobile_agent import __main__ as cli, paths
from mobile_agent.server import default_data_dir


class StateDbDefaultTests(unittest.TestCase):
    def parse(self, *extra):
        return cli.build_parser().parse_args(["serve", "--port", "8799", *extra])

    def test_explicit_state_db_is_kept(self):
        self.assertEqual(self.parse("--state-db", "/tmp/x.sqlite3").state_db, Path("/tmp/x.sqlite3"))

    def test_source_checkout_keeps_the_journal_beside_the_package(self):
        args = self.parse()
        self.assertIsNone(args.state_db)
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(cli.default_state_db(args), Path(cli.__file__).parent / ".state" / "mobster.sqlite3")

    def test_frozen_sidecar_uses_the_data_dir(self):
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(cli.default_state_db(self.parse("--data-dir", "/tmp/mobster-data")),
                             Path("/tmp/mobster-data/state/mobster.sqlite3"))
            self.assertEqual(cli.default_state_db(self.parse()), default_data_dir() / "state" / "mobster.sqlite3")

    def test_installed_package_never_writes_into_site_packages(self):
        with mock.patch.object(cli, "source_checkout", return_value=False):
            self.assertEqual(cli.default_state_db(self.parse()), default_data_dir() / "state" / "mobster.sqlite3")
        with mock.patch.object(paths, "PACKAGE", Path("/site-packages/mobile_agent")):
            self.assertFalse(paths.source_checkout())
            self.assertEqual(paths.build_dir(), default_data_dir() / "build")

    def test_source_checkout_builds_beside_the_package(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertTrue(paths.source_checkout())
            self.assertEqual(paths.build_dir(), Path(cli.__file__).resolve().parent / ".build")


class VersionTests(unittest.TestCase):
    def test_version_flag_prints_the_package_version(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as done:
            cli.build_parser().parse_args(["--version"])
        self.assertEqual(done.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"mobster {mobile_agent.__version__}")
        self.assertRegex(mobile_agent.__version__, r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
