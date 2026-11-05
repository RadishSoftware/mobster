import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mobile_agent.tests as suite

ROOT = Path(__file__).resolve().parents[2]
# Fakes in the shapes of real keys: never a value read from this Mac.
OPENAI = "sk-proj-" + "q" * 40 + "e3d1"
ANTHROPIC = "sk-ant-api03-" + "q" * 40 + "a7f2"
JEV = "apikey_" + "q" * 40 + "7a23"
GOOGLE = "AIza" + "q" * 35


class GuardTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        self.mac = Path(tempfile.mkdtemp(prefix="mobster-real-home-"))
        # A stand-in for the account's real home: the Mac app's agent.env with a key in it.
        app = self.mac / "Library" / "Application Support" / "app.mobster.desktop"
        app.mkdir(parents=True)
        (app / "agent.env").write_text(f"OPENAI_API_KEY={OPENAI}\nANTHROPIC_API_KEY={ANTHROPIC}\n")
        (self.mac / "keys.env").write_text(f"TYPESAFE_API_KEY={JEV}\n")
        self.addCleanup(shutil.rmtree, self.mac, True)

    def assertSeesNone(self, *fakes):
        from mobile_agent import config, keys
        from mobile_agent.ai_plans import providers, vault
        state = keys.Keys(None).state()
        seen = {name for name, value in os.environ.items() if any(fake in value for fake in fakes)}
        seen |= {name for name, (key, _) in (("openai", keys.openai_key()), ("anthropic", keys.anthropic_key()))
                 if key in fakes}
        seen |= {p.id for p in providers.ALL.values() if providers.credential(p) in fakes}
        self.assertEqual(sorted(seen), [])
        self.assertEqual([state[name]["keyHint"] for name in ("openai", "anthropic", "helper")], [None, None, None])
        # The Mac app's agent.env and the vault's files are in this run's empty HOME, never the account's.
        self.assertEqual(Path.home(), Path(suite.HOME))
        self.assertTrue(config.app_env_file().is_relative_to(suite.HOME))
        self.assertTrue(vault.vault_for("provider-openai").path.is_relative_to(suite.HOME))
        self.assertFalse(vault.on_mac())

    def test_a_key_set_in_the_environment_is_gone_once_the_guard_runs(self):
        os.environ.update({"OPENAI_API_KEY": OPENAI, "ANTHROPIC_API_KEY": ANTHROPIC, "TYPESAFE_API_KEY": JEV,
                           "TEXT_MODEL_API_KEY": OPENAI, "TEXT_MODEL": "gpt-4.1-mini",
                           "MOBSTER_HELPER_PROVIDER": "openai", "GEMINI_API_KEY": GOOGLE, "OPENROUTER_API_KEY": OPENAI,
                           "XAI_API_KEY": OPENAI,
                           # A key under a name nobody knows still goes, by its shape.
                           "MY_SCRATCH": OPENAI,
                           # The real home and a key file every command loads.
                           "HOME": str(self.mac), "MOBSTER_ENV_FILE": str(self.mac / "keys.env"),
                           "MOBSTER_VAULT": "keychain"})
        from mobile_agent import keys
        self.assertEqual(keys.openai_key(), (OPENAI, "own"))  # what a test saw before the guard
        removed = suite.guard()
        self.assertTrue({"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY", "TEXT_MODEL_API_KEY",
                         "GEMINI_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY", "MY_SCRATCH", "MOBSTER_ENV_FILE"}
                        <= set(removed))
        self.assertEqual((os.environ["HOME"], os.environ["MOBSTER_VAULT"]), (suite.HOME, "file"))
        self.assertSeesNone(OPENAI, ANTHROPIC, JEV, GOOGLE)

    def test_a_new_test_process_sees_no_key_before_its_first_test(self):
        probe = ("import os, json, mobile_agent.tests as suite\n"
                 "from mobile_agent import config, keys\n"
                 "config.load_env_layers()\n"
                 "print(json.dumps({'keys': [n for n, v in os.environ.items() if suite.KEY_SHAPE.search(v)],\n"
                 "    'openai': keys.openai_key()[0] is not None, 'anthropic': keys.anthropic_key()[0] is not None,\n"
                 "    'home': os.environ['HOME'] == suite.HOME, 'env_file': 'MOBSTER_ENV_FILE' in os.environ}))\n")
        env = {**os.environ, "OPENAI_API_KEY": OPENAI, "ANTHROPIC_API_KEY": ANTHROPIC, "TYPESAFE_API_KEY": JEV,
               "HOME": str(self.mac), "MOBSTER_ENV_FILE": str(self.mac / "keys.env"), "PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, suite.mask(done.stderr))
        self.assertEqual(json.loads(done.stdout), {"keys": [], "openai": False, "anthropic": False, "home": True,
                                                   "env_file": False})

    def test_a_test_that_clears_the_environment_still_has_no_real_home(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": OPENAI}, clear=True):
            self.assertEqual((os.path.expanduser("~"), os.path.expanduser("~/x"), os.path.expanduser(b"~/x")),
                             (suite.HOME, suite.HOME + "/x", os.fsencode(suite.HOME) + b"/x"))
            from mobile_agent import config
            from mobile_agent.ai_plans import vault
            self.assertTrue(config.app_env_file().is_relative_to(suite.HOME))
            self.assertTrue(vault.vault_for("provider-openai").path.is_relative_to(suite.HOME))
        # With HOME set, ~ is HOME as always.
        with mock.patch.dict(os.environ, {"HOME": str(self.mac)}):
            self.assertEqual(Path.home(), self.mac)

    def test_every_key_mobster_reads_by_name_is_removed(self):
        from mobile_agent import keys
        from mobile_agent.ai_plans import providers
        names = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY", *keys.HELPER_ENV,
                 *(name for provider in providers.ALL.values() for name in provider.env)}
        # A value that isn't key-shaped, so only the name can catch it.
        environ = {name: "x" for name in names}
        suite.guard(environ)
        self.assertEqual(sorted(names & set(environ)), [])

class FailureMessageTests(unittest.TestCase):
    def test_a_failed_assertion_shows_a_key_only_by_its_last_four(self):
        class Leaky(unittest.TestCase):
            def test_header(self):
                self.assertEqual(f"Bearer {OPENAI}", f"Bearer {ANTHROPIC}")

            def test_error(self):
                raise RuntimeError(f"sent {JEV} and {GOOGLE}")

        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(Leaky))
        self.assertEqual((len(result.failures), len(result.errors)), (1, 1))
        printed = stream.getvalue() + "".join(text for _, text in result.failures + result.errors)
        for key in (OPENAI, ANTHROPIC, JEV, GOOGLE):
            self.assertNotIn(key, printed)
        for tail in ("<key …e3d1>", "<key …a7f2>", "<key …7a23>", "<key …" + GOOGLE[-4:] + ">"):
            self.assertIn(tail, printed)


# Where this Mac's CLIs would be: never a real folder, and nothing is there, so a refusal that failed would show as
# FileNotFoundError, not as a run.
ELSEWHERE = "/opt/mobster-test-mac/bin"


class CodingAgentTests(unittest.TestCase):

    def setUp(self):
        from mobile_agent.ai_plans import agents
        self.mac = Path(tempfile.mkdtemp(prefix="mobster-real-bin-"))
        self.addCleanup(shutil.rmtree, self.mac, True)
        self.ran = self.mac / "ran.txt"
        for name in ("codex", "claude", "gemini", "zsh"):
            path = self.mac / name
            path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.ran}"\n')
            path.chmod(0o755)
        agents.reset_for_tests()
        self.addCleanup(agents.reset_for_tests)

    def ran_here(self):
        return self.ran.read_text().splitlines() if self.ran.exists() else []

    def test_a_test_that_clears_the_environment_finds_none_and_runs_none(self):
        from mobile_agent.ai_plans import agents, cli
        with mock.patch.dict(os.environ, {"PATH": f"{self.mac}{os.pathsep}/usr/bin{os.pathsep}/bin",
                                          "SHELL": str(self.mac / "zsh")}, clear=True):
            statuses = agents.statuses(max_age=0)
            cli.child_env(str(self.mac / "codex"))  # a task's CLI gets the login shell's PATH
        self.assertEqual({agent: status["installed"] for agent, status in statuses.items()},
                         {"codex": False, "claude_code": False, "gemini": False})
        self.assertEqual(self.ran_here(), [])

    def test_a_cli_a_test_puts_somewhere_itself_still_runs(self):
        from mobile_agent.ai_plans import cli
        from mobile_agent.ai_plans.codex import Codex
        status = Codex({**os.environ, "MOBSTER_CODEX_PATH": str(self.mac / "codex")}).status(max_age=0)
        self.assertEqual((status["installed"], status["path"]), (True, str(self.mac / "codex")))
        self.assertIn("codex login status", self.ran_here())
        with mock.patch.object(cli, "SEARCH_DIRS", (str(self.mac),)):
            self.assertEqual(cli.find_binary("claude", {"PATH": "/usr/bin"}), str(self.mac / "claude"))

    def test_this_macs_cli_or_a_login_shell_is_refused_before_it_starts(self):
        before = len(suite.REFUSED)
        for argv in ([f"{ELSEWHERE}/claude", "--version"], [f"{ELSEWHERE}/zsh", "-l", "-c", "true"],
                     [str(self.mac / "zsh"), "-lc", "true"]):
            with self.subTest(argv=argv), self.assertRaises(PermissionError):
                subprocess.run(argv, capture_output=True, timeout=10)
        refused = suite.REFUSED[before:]
        del suite.REFUSED[before:]  # this test's own refusals don't fail it
        self.assertEqual(len(refused), 3)
        self.assertEqual(refused[0], f"A test tried to start this Mac's claude ({ELSEWHERE}/claude). "
                                     "Point MOBSTER_CLAUDE_CODE_PATH at a fake (fake_agent_cli.py)")
        self.assertEqual(self.ran_here(), [])
        # A test's own fake, in the temporary folder, runs; so does a shell that isn't a login shell.
        subprocess.run([str(self.mac / "claude"), "--version"], check=True, timeout=10)
        subprocess.run([str(self.mac / "zsh"), "-c", "true"], check=True, timeout=10)
        self.assertEqual(self.ran_here(), ["claude --version", "zsh -c true"])

    def test_a_test_whose_process_tries_one_fails_with_it(self):
        class Quiet(unittest.TestCase):
            def test_status(self):
                try:  # as a status thread does: the error is swallowed, the test would pass
                    subprocess.run([f"{ELSEWHERE}/codex", "login", "status"], capture_output=True, timeout=10)
                except OSError:
                    pass

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Quiet).run(result)
        self.assertEqual(len(result.errors), 1)
        self.assertIn(f"A test tried to start this Mac's codex ({ELSEWHERE}/codex)", result.errors[0][1])
        self.assertEqual(suite.REFUSED, [])


@unittest.skipUnless(importlib.util.find_spec("mobile_agent.remote"), "requires the app remote extension")
class KeychainTests(unittest.TestCase):

    def stores(self):
        from mobile_agent.ai_plans import vault
        from mobile_agent.outlets import vault as outlets
        from mobile_agent.remote import keychain
        return {"remote": keychain.backend(), "outlets": outlets.backend(),
                "ai plans": vault.vault_for("provider-openai")}

    def test_a_test_that_clears_the_environment_keeps_its_secrets_in_files(self):
        from mobile_agent.tests.test_engines import CLEAN
        self.assertEqual([name for name in CLEAN if name.startswith("MOBSTER_")], [])
        with mock.patch.dict(os.environ, CLEAN, clear=True):
            stores = self.stores()
        self.assertEqual({name: type(store).__name__ for name, store in stores.items()},
                         {"remote": "FileKeychain", "outlets": "FileVault", "ai plans": "FileVault"})
        for name, store in stores.items():
            with self.subTest(store=name):
                self.assertTrue(Path(store.path).is_relative_to(suite.HOME))

    def test_a_file_a_test_names_is_still_used(self):
        folder = Path(tempfile.mkdtemp(prefix="mobster-keychain-"))
        self.addCleanup(shutil.rmtree, folder, True)
        with mock.patch.dict(os.environ, {"MOBSTER_REMOTE_KEYCHAIN": f"file:{folder}/remote.json",
                                          "MOBSTER_OUTLETS_KEYCHAIN": f"file:{folder}/outlets.json"}):
            stores = self.stores()
        self.assertEqual((Path(stores["remote"].path), Path(stores["outlets"].path)),
                         (folder / "remote.json", folder / "outlets.json"))

    def test_this_macs_keychain_is_refused_before_it_is_read(self):
        before = len(suite.REFUSED)
        for argv in ([f"{ELSEWHERE}/security", "find-generic-password", "-s", "app.mobster.desktop", "-w"],
                     [f"{ELSEWHERE}/security", "-i"]):
            with self.subTest(argv=argv[1]), self.assertRaises(PermissionError):
                subprocess.run(argv, input="", capture_output=True, text=True, timeout=10)
        refused = suite.REFUSED[before:]
        del suite.REFUSED[before:]  # this test's own refusals don't fail it
        self.assertEqual(len(refused), 2)
        self.assertEqual(refused[0], f"A test tried to reach this Mac's Keychain ({ELSEWHERE}/security "
                                     "find-generic-password -s). Give the code a file store, as tests/__init__.py does")
        # Decoding a provisioning profile reads no Keychain item.
        self.assertIsNone(suite.refusal(["/usr/bin/security", "cms", "-D", "-i", "profile.mobileprovision"]))


class ServiceTests(unittest.TestCase):

    def setUp(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.setblocking(False)
        self.addCleanup(self.listener.close)
        self.url = "http://127.0.0.1:%d" % self.listener.getsockname()[1]
        home = Path(tempfile.mkdtemp(prefix="mobster-service-"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / "state").mkdir()
        (home / "state" / "api-url").write_text(self.url + "\n")
        for patch in (mock.patch("mobile_agent.paths.user_data_dir", return_value=home),
                      mock.patch("mobile_agent.paths.source_checkout", return_value=None),
                      mock.patch.dict(os.environ, {})):
            patch.start()
            self.addCleanup(patch.stop)

    def reached(self):
        count = 0
        while True:
            try:
                self.listener.accept()[0].close()
            except BlockingIOError:
                return count
            count += 1

    def test_the_guard_names_a_closed_port_whatever_the_shell_set(self):
        environ = {"MOBSTER_URL": "http://127.0.0.1:8765"}
        suite.guard(environ)
        self.assertEqual(environ["MOBSTER_URL"], suite.NO_SERVICE)
        self.assertEqual(os.environ["MOBSTER_URL"], suite.NO_SERVICE)  # what this process got at import

    def test_a_running_service_is_not_reached_by_the_clients_that_look_for_it(self):
        from mobile_agent.permissions import outside
        from mobile_agent.threads import client
        self.assertEqual(client.default_url(), suite.NO_SERVICE)
        with self.assertRaises(client.NoService):
            client.Client.connect(origin="mcp")
        # The approval of an outside agent's link, as open_url asks it: refused for want of the app, nothing posted.
        with self.assertRaises(outside.Refused) as refused:
            outside.ask_app({"operation": "OPEN_URL", "label": "Open tel:+15550100"}, key="link")
        self.assertIn("the Mobster app isn't open", str(refused.exception))
        self.assertEqual(self.reached(), 0)

    def test_without_the_guard_the_same_clients_find_that_service(self):
        from mobile_agent.threads import client
        os.environ.pop("MOBSTER_URL", None)
        self.assertEqual(client.default_url(), self.url)
        self.assertGreater(self.reached(), 0)

if __name__ == "__main__":
    unittest.main()
