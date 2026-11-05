import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import mobile_agent
from mobile_agent import __main__ as cli
from mobile_agent import cli_parser, config, keychain_acl, paths
from mobile_agent.accounts import store as accounts_store
from mobile_agent.content_queue import store as queue_store
from mobile_agent.memory import store as memory_store

SERVICES = {
    "mobile_agent.env_secrets": "app.mobster.desktop.settings",
    "mobile_agent.connectors.vault": "app.mobster.desktop.connectors",
    "mobile_agent.outlets.vault": "app.mobster.desktop.outlets",
    "mobile_agent.remote.keychain": "app.mobster.desktop.remote",
    "mobile_agent.teams.keychain": "app.mobster.desktop.teams",
    "mobile_agent.ai_plans.vault": "Mobster AI",
    "mobile_agent.devkit.asc": "Mobster App Store Connect",
}


SHARED_BY_DESIGN = ("device-leases", "helpers", "dev")


class Home(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name).resolve()
        self.installed = self.home / "Library" / "Application Support" / "app.mobster.desktop"
        env = {"HOME": str(self.home)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("MOBSTER_APP_DATA_DIR", "MOBSTER_DEV_SHELL", "MOBSTER_MEMORY_DIR", "MOBSTER_DATA_DIR"):
            os.environ.pop(name, None)

    def release(self):
        patcher = mock.patch.object(paths, "source_checkout", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def slot(self, name="app.mobster.desktop.dev2"):
        self.release()
        folder = self.installed.with_name(name)
        paths.use_data_dir(folder)
        return folder


class ReleaseBuild(Home):
    def test_the_installed_app_keeps_its_folder(self):
        self.release()
        self.assertEqual(paths.user_data_dir(), self.installed)
        self.assertEqual(memory_store.memory_dir(), self.installed / "memory")
        self.assertEqual(config.app_env_file(), self.installed / "agent.env")
        self.assertEqual(paths.dev_data_dir(), self.installed / "dev")

    def test_the_installed_app_keeps_its_keychain_names(self):
        self.release()
        for module, service in SERVICES.items():
            self.assertEqual(paths.keychain_service(service), service, module)
        self.assertEqual(keychain_acl.Store("app.mobster.desktop.connectors").service, "app.mobster.desktop.connectors")

    def test_serve_hands_the_installed_folder_down_unchanged(self):
        self.release()
        paths.use_data_dir(self.installed)
        self.assertEqual(paths.user_data_dir(), self.installed)
        self.assertEqual(paths.keychain_service("Mobster AI"), "Mobster AI")


class DevIdentity(Home):
    def assert_away_from_installed(self, path):
        path = Path(path)
        self.assertFalse(path == self.installed or self.installed in path.parents, f"{path} is the installed app's")

    def test_a_slot_resolves_every_store_under_its_own_folder(self):
        folder = self.slot()
        self.assertEqual(paths.user_data_dir(), folder)
        for path in (memory_store.memory_dir(), config.app_env_file(), paths.build_dir()):
            self.assertIn(folder, Path(path).parents)
        from mobile_agent import cable_bt, maps, plan_gate, update_check
        from mobile_agent.ai_plans import vault
        from mobile_agent.permissions import exceptions
        from mobile_agent.threads import recent
        for path in (cable_bt.calibration_path(), plan_gate.app_account_file(), update_check._cache_path(),
                     exceptions.default_path(), recent.path(), vault.vault_for("provider-x").path
                     if hasattr(vault.vault_for("provider-x"), "path") else folder / "ai-plans"):
            self.assertIn(folder, Path(path).parents)
        with mock.patch.object(maps.Path, "mkdir"):
            self.assertIn(folder, maps._cache_dir().parents)

    def test_stores_without_a_journal_fall_back_to_the_slot(self):
        folder = self.slot()
        runtime = mock.Mock(config=mock.Mock(state_db=None, data_dir=None))
        for module in (queue_store, accounts_store):
            with mock.patch.object(module, "_stores", {}):
                with mock.patch.object(module, "QueueStore" if module is queue_store else "AccountStore") as made:
                    module.store_for(runtime)
                    self.assertEqual(Path(made.call_args.args[0]), folder)

    def test_keychain_services_are_the_slots_own(self):
        self.slot("app.mobster.desktop.dev2")
        for module, service in SERVICES.items():
            self.assertEqual(paths.keychain_service(service), service + ".dev2", module)
            self.assertEqual(keychain_acl.Store(service).service, service + ".dev2")

    def test_the_qa_variant_and_the_shipped_app_never_share_a_keychain_service(self):
        self.release()
        shipped = {paths.keychain_service(service) for service in SERVICES.values()}
        self.assertEqual(shipped, set(SERVICES.values()))
        qa_folder = self.slot("app.mobster.desktop.qa")
        self.assertNotEqual(qa_folder, self.installed)
        qa = {module: paths.keychain_service(service) for module, service in SERVICES.items()}
        for module, service in SERVICES.items():
            self.assertEqual(qa[module], service + ".qa", module)
            self.assertEqual(keychain_acl.Store(service).service, service + ".qa")
        self.assertFalse(shipped & set(qa.values()))

    def test_a_folder_that_is_not_a_slot_still_gets_its_own_service(self):
        self.release()
        paths.use_data_dir(self.home / "elsewhere")
        service = paths.keychain_service("Mobster AI")
        self.assertNotEqual(service, "Mobster AI")
        self.assertTrue(keychain_acl.SAFE.fullmatch(service))

    def test_arbitrary_folders_with_a_slot_name_do_not_share_keychain_items(self):
        services = []
        for root in (self.installed.with_name("app.mobster.desktop.dev2"),
                     self.home / "a" / "app.mobster.desktop.dev2", self.home / "b" / "app.mobster.desktop.dev2"):
            paths.use_data_dir(root)
            services.append(paths.keychain_service("Mobster AI"))
        self.assertEqual(len(set(services)), 3)
        self.assertTrue(all(keychain_acl.SAFE.fullmatch(service) for service in services))

    def test_a_relative_data_root_is_handed_down_as_an_absolute_path(self):
        with mock.patch("os.getcwd", return_value=str(self.home)):
            paths.use_data_dir("relative-data")
            service = paths.keychain_service("Mobster AI")
        expected = self.home / "relative-data"
        self.assertEqual(os.environ[paths.DATA_DIR_ENV], str(expected))
        with mock.patch("os.getcwd", return_value=str(self.home / "child")):
            self.assertEqual(paths.user_data_dir(), expected)
            self.assertEqual(paths.keychain_service("Mobster AI"), service)

    def test_source_windows_keep_the_journal_under_their_explicit_data_root(self):
        from types import SimpleNamespace
        journals = []
        with mock.patch.object(cli, "source_checkout", return_value=True):
            for root in (self.home / "window-a", self.home / "window-b"):
                paths.use_data_dir(root)
                for argument in (root, None):
                    journal = cli.default_state_db(SimpleNamespace(data_dir=argument))
                    self.assertEqual(journal, root / "state" / "mobster.sqlite3")
                journals.append(journal)
        self.assertNotEqual(*journals)

    def test_a_source_checkout_keeps_its_data_beside_the_package(self):
        code = ("from mobile_agent import paths, keychain_acl\n"
                "from mobile_agent.memory import store\n"
                "print(paths.source_checkout(), paths.user_data_dir(), store.memory_dir(),"
                " paths.keychain_service('Mobster AI'), sep='\\n')")
        env = {"HOME": str(self.home), "PATH": os.environ["PATH"]}
        root = Path(paths.PACKAGE).parent
        out = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                             capture_output=True, text=True, check=True).stdout.split("\n")
        self.assertEqual(out[0], "True")
        self.assertEqual(Path(out[1]), paths.PACKAGE / ".state" / "data")
        self.assertEqual(Path(out[2]), paths.PACKAGE / ".state" / "data" / "memory")
        self.assertTrue(out[3].startswith("Mobster AI.dev-"))
        self.assert_away_from_installed(out[1])

    def test_a_dev_shell_without_a_folder_gets_the_dev_one(self):
        self.release()
        os.environ["MOBSTER_DEV_SHELL"] = "1"
        self.assertEqual(paths.user_data_dir(), self.installed.with_name("app.mobster.desktop.dev"))

    def test_only_the_shared_by_design_paths_stay_in_the_installed_folder(self):
        folder = self.slot()
        self.assertEqual(paths.machine_data_dir(), self.installed)
        for name in SHARED_BY_DESIGN:
            self.assertNotIn(folder, (paths.machine_data_dir() / name).parents)


class StateDbDefaultTests(unittest.TestCase):
    def parse(self, *extra):
        return cli_parser.build_parser().parse_args(["serve", "--port", "8799", *extra])

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
                             Path("/tmp/mobster-data/state/mobster.sqlite3").resolve())
            self.assertEqual(cli.default_state_db(self.parse()), paths.user_data_dir() / "state" / "mobster.sqlite3")

    def test_installed_package_never_writes_into_site_packages(self):
        with mock.patch.object(cli, "source_checkout", return_value=False):
            self.assertEqual(cli.default_state_db(self.parse()), paths.user_data_dir() / "state" / "mobster.sqlite3")
        with mock.patch.object(paths, "PACKAGE", Path("/site-packages/mobile_agent")):
            self.assertFalse(paths.source_checkout())
            self.assertEqual(paths.build_dir(), paths.user_data_dir() / "build")

    def test_source_checkout_builds_beside_the_package(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertTrue(paths.source_checkout())
            self.assertEqual(paths.build_dir(), Path(cli.__file__).resolve().parent / ".build")


class VersionTests(unittest.TestCase):
    def test_version_flag_prints_the_package_version(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as done:
            cli_parser.build_parser().parse_args(["--version"])
        self.assertEqual(done.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"mobster {mobile_agent.__version__}")
        self.assertRegex(mobile_agent.__version__, r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
