import importlib.util
import inspect
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import harness_api
from mobile_agent.harness_api import EngineAlone, EngineHooks


def members(cls):
    # get_annotations: from Python 3.14 a class's annotations are made on first use and aren't in vars(cls).
    return {name for name in vars(cls) if not name.startswith("_")} | set(inspect.get_annotations(cls))


def implemented(cls):
    return {name for name in dir(cls()) if not name.startswith("_")}


def parameters(function):
    return list(inspect.signature(function).parameters)


class ProtocolTests(unittest.TestCase):
    def check(self, cls):
        self.assertEqual(implemented(cls), members(EngineHooks))
        for name in members(EngineHooks):
            if callable(getattr(EngineHooks, name, None)):
                with self.subTest(hook=name):
                    self.assertEqual(parameters(getattr(cls, name)), parameters(getattr(EngineHooks, name)))

    def test_the_engine_alone_implements_every_hook(self):
        self.check(EngineAlone)

    @unittest.skipUnless(importlib.util.find_spec(harness_api.HOOKS_MODULE), "the harness is not in this tree")
    def test_the_harness_implements_every_hook(self):
        from mobile_agent.engine_hooks import Harness
        self.check(Harness)
        self.assertIsInstance(harness_api.hooks(), Harness)


class LoadTests(unittest.TestCase):
    def load(self, package, *, hooks_module=harness_api.HOOKS_MODULE):
        """hooks() as on a fresh start, with ``package`` in place of mobile_agent.private."""
        with mock.patch.object(harness_api, "PRIVATE_PACKAGE", package), \
                mock.patch.object(harness_api, "HOOKS_MODULE", hooks_module), \
                mock.patch.object(harness_api, "_loaded", []), mock.patch.object(harness_api, "_load_error", []):
            return harness_api.hooks()

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

    def test_without_the_harness_the_engine_is_alone_and_the_private_slots_are_empty(self):
        hooks = self.load("mobster_no_such_extension", hooks_module="mobster_no_such_harness")
        self.assertIs(type(hooks), EngineAlone)
        self.assertEqual((hooks.target_driver, hooks.cli, hooks.runtime_for), (None, [], None))

    def test_a_folder_left_behind_is_no_private_package(self):
        # After the package's files are deleted, a checkout can keep its __pycache__: Python imports the folder
        # as an empty namespace package, which has no register().
        name = self.folder("mobster_leftover_extension", {"__pycache__/old.cpython-312.pyc": ""})
        hooks = self.load(name)
        self.assertEqual((hooks.target_driver, hooks.cli), (None, []))

    def test_the_private_package_registers_on_the_same_object(self):
        name = self.folder("mobster_real_extension",
                           {"__init__.py": "def register(hooks):\n    hooks.cli.append(hooks)\n"})
        hooks = self.load(name)
        self.assertEqual(len(hooks.cli), 1)
        self.assertIs(hooks.cli[0], hooks)

    def test_a_failed_registration_raises_on_every_call_and_never_runs_again(self):
        name = self.folder("mobster_broken_extension", {"__init__.py": (
            "calls = []\n"
            "def register(hooks):\n"
            "    calls.append(1)\n"
            "    raise RuntimeError(\"extension didn't initialize\")\n")})
        with mock.patch.object(harness_api, "PRIVATE_PACKAGE", name), mock.patch.object(harness_api, "_loaded", []), \
                mock.patch.object(harness_api, "_load_error", []):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "didn't initialize"):
                    harness_api.hooks()
            self.assertEqual(harness_api._loaded, [])
        self.assertEqual(sys.modules[name].calls, [1])

    def test_a_missing_dependency_is_not_an_absent_package(self):
        name = self.folder("mobster_needy_extension", {"__init__.py": "import mobster_no_such_dependency\n"})
        with self.assertRaises(ModuleNotFoundError):
            self.load(name)


if __name__ == "__main__":
    unittest.main()
