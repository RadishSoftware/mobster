import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import extensions


class LoadTests(unittest.TestCase):
    """extensions.load() imports the optional package once; without it, every hook stays empty."""

    def load(self, package):
        """load() as on a fresh start, with ``package`` in place of mobile_agent.private."""
        fresh = extensions.Hooks()
        with mock.patch.object(extensions, "PACKAGE", package), mock.patch.object(extensions, "hooks", fresh), \
                mock.patch.object(extensions, "_loaded", False), mock.patch.object(extensions, "_load_error", None):
            return extensions.load()

    def folder(self, name, files):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        for path, text in files.items():
            (root / name / path).parent.mkdir(parents=True, exist_ok=True)
            (root / name / path).write_text(text)
        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        self.addCleanup(sys.modules.pop, name, None)
        return name

    def test_no_package_leaves_every_hook_empty(self):
        hooks = self.load("mobster_no_such_extension")
        self.assertIsNone(hooks.target_driver)
        self.assertEqual((hooks.cli, hooks.eval_targets), ([], []))

    def test_a_folder_left_behind_is_no_extension(self):
        # After the package's files are deleted, a checkout can keep its __pycache__: Python imports the folder
        # as an empty namespace package, which has no register().
        name = self.folder("mobster_leftover_extension", {"__pycache__/old.cpython-312.pyc": ""})
        hooks = self.load(name)
        self.assertIsNone(hooks.target_driver)
        self.assertEqual(hooks.cli, [])

    def test_a_package_registers_its_hooks(self):
        name = self.folder("mobster_real_extension", {"__init__.py": "def register(hooks):\n    hooks.cli.append('extra')\n"})
        self.assertEqual(self.load(name).cli, ["extra"])

    def test_failed_registration_never_becomes_a_successful_cached_load(self):
        module = mock.Mock()
        module.register.side_effect = RuntimeError("extension didn't initialize")
        with mock.patch.object(extensions, "_loaded", False), mock.patch.object(extensions, "_load_error", None), \
                mock.patch.object(extensions.importlib, "import_module", return_value=module):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "didn't initialize"):
                    extensions.load()
                self.assertFalse(extensions._loaded)
            module.register.assert_called_once()

    def test_missing_dependency_is_not_treated_as_an_absent_extension(self):
        error = ModuleNotFoundError("missing dependency", name="extension_dependency")
        with mock.patch.object(extensions, "_loaded", False), mock.patch.object(extensions, "_load_error", None), \
                mock.patch.object(extensions.importlib, "import_module", side_effect=error):
            with self.assertRaises(ModuleNotFoundError):
                extensions.load()
            self.assertFalse(extensions._loaded)


if __name__ == "__main__":
    unittest.main()
