"""Native helper sources survive standalone installation without a desktop checkout."""

import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import paths
from mobile_agent.netxray import tool
from mobile_agent.wireless import pair

ROOT = Path(__file__).resolve().parents[2]
HELPERS = (pair, tool)


class NativeHelperSourceTests(unittest.TestCase):
    def test_sources_are_adjacent_to_their_modules_and_declared_as_package_data(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]
        for helper in HELPERS:
            with self.subTest(helper=helper.NAME):
                source = Path(helper.__file__).resolve().with_name(f"{helper.NAME}.c")
                self.assertEqual(helper.SOURCE, source)
                self.assertTrue(source.is_file())
                self.assertIn(source.name, data[helper.__package__])
                self.assertIn("This file is MIT", source.read_text())

    def test_build_uses_packaged_source_and_reuses_the_result(self):
        for helper in HELPERS:
            with self.subTest(helper=helper.NAME), tempfile.TemporaryDirectory() as folder:
                calls = []

                def compile_fake(argv, **kwargs):
                    calls.append(argv)
                    if argv[0] == "pkg-config":
                        return SimpleNamespace(stdout="-I/synthetic/include -L/synthetic/lib -limobiledevice")
                    self.assertEqual(argv[:3], ["xcrun", "clang", "-O2"])
                    self.assertEqual(argv[3], str(helper.SOURCE))
                    Path(argv[argv.index("-o") + 1]).write_bytes(b"synthetic helper")
                    return SimpleNamespace(returncode=0)

                with (mock.patch.object(paths, "machine_data_dir", return_value=folder),
                      mock.patch.object(paths, "user_data_dir", return_value=folder),
                      mock.patch.object(helper.shutil, "which", return_value="pkg-config"),
                      mock.patch.object(helper.subprocess, "run", side_effect=compile_fake)):
                    target = Path(helper.build())
                    self.assertEqual(target.read_bytes(), b"synthetic helper")
                    self.assertFalse(target.with_suffix(".partial").exists())
                    self.assertEqual(helper.build(), str(target))
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0], ["pkg-config", "--cflags", "--libs", *helper.LIBRARIES])
