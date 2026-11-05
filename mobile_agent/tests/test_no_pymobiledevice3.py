"""Mobster never imports pymobiledevice3: it runs the program you installed as a separate process (phone_io/tunnel.py,
team_signing/tooling.py, verify/device_audit.py), and the builds stop if it would be bundled
(scripts/check_no_pymobiledevice3.py). Mentions in strings, docstrings and comments are fine."""

import ast
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "mobile_agent"
CHECK = ROOT / "scripts" / "check_no_pymobiledevice3.py"
NAME = "pymobiledevice3"
LOADERS = {"__import__", "import_module"}


def _names_it(module):
    return isinstance(module, str) and (module == NAME or module.startswith(NAME + "."))


def imports_of(source, filename="<source>"):
    """Line numbers where ``source`` imports pymobiledevice3: an import statement anywhere (functions included), or
    ``__import__`` / ``importlib.import_module`` called with its name."""
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.Import):
            hit = any(_names_it(alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            hit = node.level == 0 and _names_it(node.module)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            hit = called in LOADERS and _names_it(node.args[0].value)
        else:
            continue
        if hit:
            found.append(node.lineno)
    return found


def sources():
    for directory, folders, files in os.walk(PACKAGE):
        folders[:] = sorted(name for name in folders if not name.startswith(".") and name != "__pycache__")
        for name in sorted(files):
            if name.endswith(".py"):
                yield Path(directory) / name


class NoImportTests(unittest.TestCase):
    def test_no_module_under_mobile_agent_imports_it(self):
        found = {}
        for path in sources():
            text = path.read_text(encoding="utf-8")
            lines = imports_of(text, str(path)) if NAME in text else []  # a module that never spells it can't import it
            if lines:
                found[path.relative_to(ROOT).as_posix()] = lines
        self.assertEqual(found, {}, "run pymobiledevice3 as a separate program (phone_io/tunnel.py tool())")

    def test_the_scan_reaches_the_package_and_skips_the_venv(self):
        scanned = {path.relative_to(PACKAGE).as_posix() for path in sources()}
        self.assertIn("verify/device_audit.py", scanned)
        self.assertFalse([path for path in scanned if path.startswith(".")])

    def test_imports_are_found_wherever_they_are_and_mentions_are_not(self):
        source = '''
"""pymobiledevice3 is mentioned here: import pymobiledevice3"""
# import pymobiledevice3
import json
TOOL = "pymobiledevice3"
import pymobiledevice3
import os, pymobiledevice3.lockdown as lockdown
from pymobiledevice3.services import webinspector
def connect():
    from pymobiledevice3.lockdown import create_using_usbmux
    class Bridge:
        async def go(self):
            import pymobiledevice3.services.accessibilityaudit
    return __import__("pymobiledevice3")
importlib.import_module("pymobiledevice3.cli")
from . import pymobiledevice3_notes
import pymobiledevice3x
'''
        self.assertEqual(sorted(imports_of(source)), [6, 7, 8, 10, 13, 14, 15])


def load_check():
    spec = importlib.util.spec_from_file_location("check_no_pymobiledevice3_under_test", CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(CHECK.is_file(), "the build check is not part of this copy")
class BuildCheckTests(unittest.TestCase):
    def setUp(self):
        self.check = load_check()
        self.root = self.folder()

    def folder(self):
        folder = Path(tempfile.mkdtemp(prefix="mobster-pmd3-check-"))
        self.addCleanup(shutil.rmtree, folder, True)
        return folder

    def run_main(self, argv, **kwargs):
        err = io.StringIO()
        with redirect_stderr(err):
            code = self.check.main(argv, **kwargs)
        return code, err.getvalue()

    def test_names_that_point_into_the_package(self):
        for name in ("pymobiledevice3", "pymobiledevice3.lockdown", "pymobiledevice3-11.17.0.dist-info/METADATA",
                     "_internal/pymobiledevice3/__init__.pyc", "Frameworks/pymobiledevice3.py",
                     "lib/python3.12/site-packages/pymobiledevice3/cli/developer"):
            self.assertTrue(self.check.names_it(name), name)
        for name in ("mobile_agent.verify.device_audit", "mobile_agent/phone_io/tunnel.pyc", "pymobiledevice",
                     "pymobiledevice30", "my_pymobiledevice3", "jsonschema-4.26.0.dist-info"):
            self.assertFalse(self.check.names_it(name), name)

    def test_the_venv_check_refuses_a_python_that_can_import_it(self):
        self.assertEqual(self.run_main([], find_spec=lambda name: None), (0, ""))
        code, err = self.run_main([], find_spec=lambda name: object() if name == NAME else None)
        self.assertEqual(code, 1)
        self.assertIn("can import pymobiledevice3", err)

    def frozen(self, archive=()):
        """A frozen folder: a bootloader whose PyInstaller archive lists ``archive``, a zip and a dist-info."""
        internal = self.root / "build" / "_internal"
        internal.mkdir(parents=True)
        (self.root / "build" / "mobster").write_bytes(bytes.fromhex("cffaedfe") + b"\0" * 64)
        (internal / "libpython3.12.dylib").write_bytes(bytes.fromhex("cffaedfe") + b"\0" * 64)
        (internal / "jsonschema-4.26.0.dist-info").mkdir()
        with zipfile.ZipFile(internal / "base_library.zip", "w") as library:
            library.writestr("json/__init__.pyc", b"")
        archives = {"mobster": ["pyiboot01_bootstrap", "mobile_agent.verify.device_audit", *archive]}
        return self.root / "build", lambda path: archives.get(path.name)

    def test_a_clean_build_passes(self):
        folder, reader = self.frozen()
        self.assertEqual(self.run_main([str(folder)], reader=lambda: reader), (0, ""))

    def test_a_module_inside_the_bootloaders_archive_is_found(self):
        folder, reader = self.frozen(archive=["pymobiledevice3", "pymobiledevice3.lockdown"])
        code, err = self.run_main([str(folder)], reader=lambda: reader)
        self.assertEqual(code, 1)
        self.assertIn("mobster: pymobiledevice3.lockdown", err)

    def test_a_folder_a_dist_info_or_a_zip_member_is_found(self):
        def zipped(internal):
            with zipfile.ZipFile(internal / "extra.zip", "w") as extra:
                extra.writestr("pymobiledevice3/lockdown.pyc", b"")

        for what, add in (("folder", lambda internal: (internal / "pymobiledevice3").mkdir()),
                          ("dist-info", lambda internal: (internal / "pymobiledevice3-11.17.0.dist-info").mkdir()),
                          ("zip member", zipped)):
            with self.subTest(what):
                self.root = self.folder()
                folder, reader = self.frozen()
                add(folder / "_internal")
                code, err = self.run_main([str(folder)], reader=lambda: reader)
                self.assertEqual(code, 1, err)
                self.assertIn("pymobiledevice3", err)

    def test_no_answer_is_never_a_pass(self):
        def no_pyinstaller():
            raise ImportError("PyInstaller")

        folder, _ = self.frozen()
        code, err = self.run_main([str(folder)], reader=no_pyinstaller)
        self.assertEqual(code, 2)
        self.assertIn("needs PyInstaller", err)
        self.assertEqual(self.run_main([str(self.root / "missing")], reader=lambda: lambda path: None)[0], 2)

        def broken(path):
            raise ValueError("a damaged archive")

        self.assertEqual(self.run_main([str(folder)], reader=lambda: broken)[0], 2)


if __name__ == "__main__":
    unittest.main()
