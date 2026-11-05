import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent import alerts, config, env_secrets, login
from mobile_agent.env_secrets import update_env_file
from mobile_agent.keys import Keys
from mobile_agent.tests.fake_keychain import FakeBridge

KEY = "sk-ant-api03-" + "k" * 40
OTHER = "sk-proj-" + "o" * 40


class FakeKeychain:
    def __init__(self, record=None, refuse_save=False, refuse_load=False):
        self.record, self.refuse_save, self.refuse_load = dict(record or {}), refuse_save, refuse_load
        self.loads = self.saves = self.clears = 0

    def load(self):
        self.loads += 1
        if self.refuse_load:
            raise env_secrets.KeychainError("The Keychain didn't give Mobster its settings")
        return dict(self.record)

    def save(self, record):
        self.saves += 1
        if self.refuse_save:
            raise env_secrets.KeychainError("The Keychain didn't take Mobster's settings")
        self.record = dict(record)

    def clear(self):
        self.clears += 1
        self.record = {}


class Base(unittest.TestCase):
    def setUp(self):
        self.path = config.app_env_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.path.unlink, True)
        self.keychain = FakeKeychain()
        backend = patch.object(env_secrets, "backend", lambda: self.keychain)
        backend.start()
        self.addCleanup(backend.stop)
        names = env_secrets.SECRET_NAMES + (env_secrets.MARKER, "MOBSTER_SMART_PROVIDER", "TEXT_MODEL")
        environ = patch.dict(os.environ, {})
        environ.start()
        self.addCleanup(environ.stop)
        for name in names:
            os.environ.pop(name, None)

    def text(self):
        return self.path.read_text() if self.path.exists() else ""

    def fresh_process(self):
        for name in env_secrets.SECRET_NAMES + (env_secrets.MARKER, "MOBSTER_SMART_PROVIDER"):
            os.environ.pop(name, None)
        return config.load_env_file(self.path, missing_ok=True)


class SaveTests(Base):
    def test_concurrent_key_saves_preserve_both_credentials(self):
        import fcntl
        import threading
        first_read, first_done, release = threading.Event(), threading.Event(), threading.Event()
        load, flock = self.keychain.load, fcntl.flock
        errors = []

        def read():
            record = load()
            if threading.current_thread().name == "first":
                first_read.set()
                release.wait(2)
            else:
                release.set()
                first_done.wait(2)
            return record

        def acquire(file, operation):
            if threading.current_thread().name == "second":
                release.set()
            return flock(file, operation)

        def save(name, value):
            try:
                update_env_file(self.path, {name: value})
            except Exception as error:
                errors.append(error)
            finally:
                if threading.current_thread().name == "first":
                    first_done.set()

        with patch.object(self.keychain, "load", read), patch("fcntl.flock", acquire):
            first = threading.Thread(target=save, args=("ANTHROPIC_API_KEY", KEY), name="first")
            second = threading.Thread(target=save, args=("OPENAI_API_KEY", OTHER), name="second")
            first.start()
            self.assertTrue(first_read.wait(1))
            second.start()
            first.join(3)
            second.join(3)
            self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.keychain.record, {"ANTHROPIC_API_KEY": KEY, "OPENAI_API_KEY": OTHER})
        self.assertNotIn(KEY, self.text())
        self.assertNotIn(OTHER, self.text())

    def test_a_saved_key_is_in_the_keychain_and_never_in_the_file(self):
        Keys(self.path).update({"anthropic": {"key": KEY}, "smartProvider": "anthropic"})
        self.assertEqual(self.keychain.record, {"ANTHROPIC_API_KEY": KEY})
        self.assertNotIn(KEY, self.text())
        self.assertIn("MOBSTER_SMART_PROVIDER=anthropic", self.text())
        self.assertIn(f"{env_secrets.MARKER}=1", self.text())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], KEY)
        loaded = self.fresh_process()
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], KEY)
        self.assertIn("ANTHROPIC_API_KEY", loaded.loaded)

    def test_removing_the_last_key_clears_the_item_and_the_marker(self):
        Keys(self.path).update({"anthropic": {"key": KEY}, "openai": {"key": OTHER}})
        Keys(self.path).update({"anthropic": None})
        self.assertEqual(self.keychain.record, {"OPENAI_API_KEY": OTHER})
        Keys(self.path).update({"openai": None})
        self.assertEqual((self.keychain.record, self.keychain.clears), ({}, 1))
        self.assertNotIn(env_secrets.MARKER, self.text())
        self.fresh_process()
        self.assertNotIn("OPENAI_API_KEY", os.environ)

    def test_a_refusal_saves_nothing_anywhere(self):
        self.keychain.refuse_save = True
        update_env_file(self.path, {"MOBSTER_SMART_PROVIDER": "openai"})
        with self.assertRaises(env_secrets.KeychainError):
            Keys(self.path).update({"anthropic": {"key": KEY}, "smartProvider": "anthropic"})
        self.assertNotIn(KEY, self.text())
        self.assertIn("MOBSTER_SMART_PROVIDER=openai", self.text(), "the file is as it was")
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)

    def test_a_setting_that_isnt_secret_never_touches_the_keychain(self):
        update_env_file(self.path, {"MOBSTER_ENABLE_LIVE": "1"})
        self.fresh_process()
        self.assertEqual((self.keychain.loads, self.keychain.saves), (0, 0))

    def test_the_alert_address_is_a_secret_too(self):
        url = "https://hooks.slack.com/services/T000/B000/" + "x" * 24
        update_env_file(self.path, {"MOBSTER_ALERT_WEBHOOK_URL": url})
        self.assertNotIn(url, self.text())
        self.assertEqual(alerts.configured_url(env={}, data_dir=self.path.parent, read_file=True), url)


class LoadTests(Base):
    def test_loading_a_stale_plaintext_read_uses_the_current_secret_and_preserves_shell_values(self):
        read_secrets = env_secrets.secrets_for
        for replacement in (OTHER, None):
            for shell in (None, "shell-synthetic-key"):
                with self.subTest(replacement=replacement is not None, shell=shell is not None):
                    os.environ.pop("ANTHROPIC_API_KEY", None)
                    if shell is not None:
                        os.environ["ANTHROPIC_API_KEY"] = shell
                    self.path.write_text(f"ANTHROPIC_API_KEY={KEY}\n")

                    def current_secret(path, values):
                        update_env_file(path, {"ANTHROPIC_API_KEY": replacement})
                        return read_secrets(path, values)

                    with patch.object(env_secrets, "secrets_for", current_secret):
                        loaded = config.load_env_file(self.path)
                    self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), shell or replacement)
                    self.assertEqual("ANTHROPIC_API_KEY" in loaded.loaded,
                                     shell is None and replacement is not None)
                    self.assertEqual(self.keychain.record,
                                     {"ANTHROPIC_API_KEY": replacement} if replacement else {})
                    self.assertEqual(loaded.warnings, [])

    def test_migrating_an_old_plaintext_snapshot_preserves_a_concurrent_saved_key(self):
        self.path.write_text(f"ANTHROPIC_API_KEY={KEY}\n")
        stale = config.read_env_values(self.path)
        update_env_file(self.path, {"OPENAI_API_KEY": OTHER})
        secrets, warnings = env_secrets.secrets_for(self.path, stale)
        self.assertEqual(secrets, {"ANTHROPIC_API_KEY": KEY, "OPENAI_API_KEY": OTHER})
        self.assertEqual(self.keychain.record, secrets)
        self.assertEqual(warnings, [])
        self.assertNotIn(KEY, self.text())
        self.assertNotIn(OTHER, self.text())

    def test_stale_plaintext_cannot_replace_a_new_key_or_resurrect_a_removed_key(self):
        for value in (OTHER, None):
            with self.subTest(value="replacement" if value else "logout"):
                self.path.write_text(f"ANTHROPIC_API_KEY={KEY}\n")
                stale = config.read_env_values(self.path)
                update_env_file(self.path, {"ANTHROPIC_API_KEY": value})
                expected = {"ANTHROPIC_API_KEY": value} if value else {}
                secrets, warnings = env_secrets.secrets_for(self.path, stale)
                self.assertEqual(secrets, expected)
                self.assertEqual(self.keychain.record, expected)
                self.assertEqual(warnings, [])
                self.assertNotIn(KEY, self.text())
                self.assertNotIn(OTHER, self.text())

    def test_a_refused_writer_lock_warns_without_using_plaintext_or_keychain(self):
        self.path.write_text(f"ANTHROPIC_API_KEY={KEY}\n{env_secrets.MARKER}=1\n")
        lock = self.path.with_name(self.path.name + ".lock")
        lock.unlink(missing_ok=True)
        lock.symlink_to(self.path.parent / "missing-lock-target")
        self.addCleanup(lock.unlink, True)
        secrets, warnings = env_secrets.secrets_for(self.path, config.read_env_values(self.path))
        self.assertEqual(secrets, {})
        self.assertEqual(self.keychain.loads, 0)
        self.assertEqual(len(warnings), 1)
        self.assertNotIn(KEY, warnings[0])
        loaded = self.fresh_process()
        self.assertEqual(len(loaded.warnings), 1)
        self.assertEqual(self.keychain.loads, 0)
        self.assertEqual(self.keychain.record, {})
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)

    def test_stale_plaintext_is_not_a_fallback_after_keychain_refusal(self):
        for value in (OTHER, None):
            with self.subTest(value="replacement" if value else "logout"):
                self.keychain.refuse_load = False
                self.path.write_text(f"ANTHROPIC_API_KEY={KEY}\n")
                stale = config.read_env_values(self.path)
                update_env_file(self.path, {"ANTHROPIC_API_KEY": value})
                self.keychain.refuse_load = True
                secrets, _warnings = env_secrets.secrets_for(self.path, stale)
                self.assertEqual(secrets, {})
                self.assertEqual(self.keychain.record, {"ANTHROPIC_API_KEY": value} if value else {})
                self.assertNotIn(KEY, self.text())

    def test_a_key_an_older_version_left_in_the_file_moves_to_the_keychain(self):
        self.path.write_text(f"ANTHROPIC_API_KEY={KEY}\nMOBSTER_SMART_PROVIDER=anthropic\n")
        os.chmod(self.path, 0o600)
        loaded = self.fresh_process()
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], KEY)
        self.assertIn("ANTHROPIC_API_KEY", loaded.loaded)
        self.assertEqual(self.keychain.record, {"ANTHROPIC_API_KEY": KEY})
        self.assertNotIn(KEY, self.text())
        self.assertIn("MOBSTER_SMART_PROVIDER=anthropic", self.text())
        self.assertEqual(loaded.warnings, [])

    def test_a_key_the_keychain_refuses_stays_in_the_file_and_still_works(self):
        self.keychain.refuse_save = True
        self.path.write_text(f"ANTHROPIC_API_KEY={KEY}\n")
        loaded = self.fresh_process()
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], KEY)
        self.assertIn(KEY, self.text(), "never lost")
        self.assertEqual(len(loaded.warnings), 1)
        self.assertNotIn(KEY, loaded.warnings[0])

    def test_an_unreadable_keychain_is_a_warning_not_a_crash(self):
        self.keychain.record = {"ANTHROPIC_API_KEY": KEY}
        self.path.write_text(f"{env_secrets.MARKER}=1\n")
        self.keychain.refuse_load = True
        loaded = self.fresh_process()
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
        self.assertIn("Keychain", loaded.warnings[0])

    def test_the_keychain_is_read_only_when_the_file_says_it_holds_keys(self):
        self.path.write_text("MOBSTER_ENABLE_LIVE=1\n")
        self.fresh_process()
        self.assertEqual(self.keychain.loads, 0)

    def test_the_shell_still_wins(self):
        self.keychain.record = {"ANTHROPIC_API_KEY": KEY}
        self.path.write_text(f"{env_secrets.MARKER}=1\n")
        os.environ["ANTHROPIC_API_KEY"] = OTHER
        config.load_env_file(self.path)
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], OTHER)

    def test_logout_finds_the_key_in_the_keychain(self):
        Keys(self.path).update({"anthropic": {"key": KEY}, "smartProvider": "anthropic"})
        self.assertEqual(login._saved().get("ANTHROPIC_API_KEY"), KEY)


class OtherFilesTests(Base):
    def test_any_other_env_file_is_the_persons_own(self):
        folder = Path(tempfile.mkdtemp(prefix="mobster-own-env-"))
        self.addCleanup(shutil.rmtree, folder, True)
        own = folder / "agent.env"
        own.write_text(f"ANTHROPIC_API_KEY={KEY}\n")
        config.load_env_file(own)
        update_env_file(own, {"OPENAI_API_KEY": OTHER})
        self.assertEqual(own.read_text(), f"ANTHROPIC_API_KEY={KEY}\nOPENAI_API_KEY={OTHER}\n")
        self.assertEqual((self.keychain.loads, self.keychain.saves), (0, 0))

    def test_without_a_keychain_the_file_keeps_them_as_before(self):
        with patch.object(env_secrets, "backend", lambda: None):
            update_env_file(self.path, {"ANTHROPIC_API_KEY": KEY})
            self.assertIn(f"ANTHROPIC_API_KEY={KEY}", self.text())
            self.assertNotIn(env_secrets.MARKER, self.text())

    def test_the_file_can_opt_out_of_the_keychain_before_its_secret_is_loaded(self):
        self.path.write_text(f"ANTHROPIC_API_KEY={KEY}\nMOBSTER_VAULT=file\n")
        with patch.dict(os.environ, {}, clear=True), patch.object(
                env_secrets, "backend", lambda: None if os.environ.get("MOBSTER_VAULT") == "file" else self.keychain):
            config.load_env_file(self.path)
            self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), KEY)
            self.assertEqual(self.keychain.loads, 0)

    def test_the_suite_never_reaches_the_real_keychain(self):
        self.assertFalse(env_secrets.on_mac())


class KeychainCommandTests(unittest.TestCase):
    def test_the_secret_goes_through_the_framework_never_a_command_line(self):
        bridge = FakeBridge()
        store = env_secrets.Keychain(runner=lambda argv, **kw: bridge.runner(argv, kw.get("input")), bridge=bridge)
        self.assertEqual(store.load(), {}, "no item yet")
        store.save({"ANTHROPIC_API_KEY": KEY})
        self.assertEqual(bridge.cli, [])
        self.assertTrue(bridge.items[(env_secrets.SERVICE, env_secrets.ACCOUNT)]["marked"])
        self.assertEqual(store.load(), {"ANTHROPIC_API_KEY": KEY})

    def test_a_failed_read_is_an_error_not_an_empty_record(self):
        bridge = FakeBridge()
        bridge.refuse.add("find")
        with self.assertRaises(env_secrets.KeychainError):
            env_secrets.Keychain(bridge=bridge).load()


if __name__ == "__main__":
    unittest.main()
