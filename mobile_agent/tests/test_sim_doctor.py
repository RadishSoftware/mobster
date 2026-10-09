"""`mobster sim doctor`: each state, the fixes it prints, --fix, and a key never shown."""

import contextlib
import json
import os
import unittest
from unittest import mock

from mobile_agent.sim import doctor
from mobile_agent.sim.simctl import Result
from mobile_agent.tests.test_sim_fakes import fake_manager

XCODE = "/Applications/Xcode.app/Contents/Developer"
VERSION = "Xcode 26.4\nBuild version 17E192\n"
SECRET = "sk-test-0123456789abcdefghijklmnopqrstuvwxyz"


@contextlib.contextmanager
def doctored(xcode=(XCODE, XCODE), version=Result(0, VERSION, ""), first_launch=Result(0, "", ""),
             machine="arm64", free=200 * 1024 ** 3, git="/usr/bin/git", environ=None):
    with fake_manager(environ) as (manager, simctl, wda, leases, root):
        commands = []

        def run(argv, timeout=60, env=None):
            argv = [str(part) for part in argv]
            commands.append(argv)
            if argv[0].endswith("xcodebuild") and argv[1] == "-version":
                return version
            if argv[0].endswith("xcodebuild") and argv[1] == "-checkFirstLaunchStatus":
                return first_launch
            if argv[0] == "/usr/sbin/sysctl":
                return Result(0, "0\n", "")
            return simctl(argv, timeout, env)
        manager.run = run
        usage = mock.Mock(free=free)
        with mock.patch("mobile_agent.device_manager.developer_dir", return_value=xcode), \
                mock.patch("mobile_agent.device_manager.xcode_tool", return_value=git), \
                mock.patch("mobile_agent.sim.doctor.platform.machine", return_value=machine), \
                mock.patch("mobile_agent.sim.doctor.shutil.disk_usage", return_value=usage):
            yield manager, simctl, wda, commands


def by_key(checks):
    return {check["key"]: check for check in checks}


class DoctorTests(unittest.TestCase):
    def test_a_ready_mac(self):
        with doctored(environ={"OPENAI_API_KEY": ""}) as (manager, simctl, wda, commands):
            checks = manager.doctor()
        self.assertEqual([check["key"] for check in checks],
                         ["arch", "xcode", "first_launch", "runtime", "device_type", "git", "disk", "wda_build",
                          "simulators", "ports", "key"])
        for check in checks:
            self.assertEqual(set(check), {"key", "label", "state", "detail", "fix"})
        states = {check["key"]: check["state"] for check in checks}
        self.assertEqual(states, {"arch": "ok", "xcode": "ok", "first_launch": "ok", "runtime": "ok",
                                  "device_type": "ok", "git": "ok", "disk": "ok", "wda_build": "warn",
                                  "simulators": "skip", "ports": "ok", "key": "skip"})
        found = by_key(checks)
        self.assertEqual(found["xcode"]["detail"], "Xcode 26.4 (17E192) at /Applications/Xcode.app")
        self.assertEqual((found["runtime"]["detail"], found["device_type"]["detail"]), ("iOS 26.4", "iPhone 17 Pro"))
        self.assertEqual(doctor.problems(checks), [])
        self.assertIn("Ready for `mobster verify`.", doctor.report(checks))

    def test_no_xcode(self):
        with doctored(xcode=(None, "/Library/Developer/CommandLineTools")) as (manager, simctl, wda, commands):
            found = by_key(manager.doctor())
        self.assertEqual(found["xcode"]["state"], "fail")
        self.assertIn("App Store", found["xcode"]["fix"])
        self.assertEqual((found["runtime"]["state"], found["device_type"]["state"]), ("skip", "skip"))
        self.assertNotIn("wda_build", found)

    def test_the_command_line_tools_selected(self):
        with doctored(xcode=(XCODE, "/Library/Developer/CommandLineTools")) as (manager, simctl, wda, commands):
            found = by_key(manager.doctor())
        self.assertEqual(found["xcode"]["state"], "fail")
        self.assertEqual(found["xcode"]["fix"], "sudo xcode-select -s /Applications/Xcode.app")

    def test_license_and_first_launch(self):
        with doctored(version=Result(69, "", "You have not agreed to the Xcode license agreements.")) as \
                (manager, simctl, wda, commands):
            self.assertEqual(by_key(manager.doctor())["xcode"]["fix"], "sudo xcodebuild -license accept")
        with doctored(first_launch=Result(1, "", "")) as (manager, simctl, wda, commands):
            found = by_key(manager.doctor())
        self.assertEqual((found["first_launch"]["state"], found["first_launch"]["fix"]),
                         ("fail", "sudo xcodebuild -runFirstLaunch"))

    def test_no_runtime_prints_the_download_and_never_runs_it(self):
        with doctored() as (manager, simctl, wda, commands):
            for runtime in simctl.runtimes:
                runtime["isAvailable"] = False
            found = by_key(manager.doctor(fix=True))
        self.assertEqual((found["runtime"]["state"], found["runtime"]["fix"]),
                         ("fail", "xcodebuild -downloadPlatform iOS"))
        self.assertEqual(found["prepare"]["state"], "skip")
        self.assertEqual(simctl.simctl_calls("create"), [])
        self.assertFalse(any("sudo" in command or "-downloadPlatform" in command for command in commands))

    def test_not_apple_silicon_and_low_disk(self):
        with doctored(machine="x86_64", free=2 * 1024 ** 3) as (manager, simctl, wda, commands):
            found = by_key(manager.doctor())
        self.assertEqual(found["arch"]["state"], "fail")
        self.assertEqual(found["disk"]["state"], "fail")
        # The measured sizes (28 Sep), not an unmeasured "about that much" (review round 3).
        self.assertEqual(found["disk"]["fix"], "Free at least 5 GB: each booted simulator takes 1.6 GB, and the "
                                               "WebDriverAgent build 0.2 GB")
        self.assertNotIn("about", found["disk"]["fix"])

    def test_git_only_matters_before_the_build(self):
        with doctored(git=None) as (manager, simctl, wda, commands):
            self.assertEqual(by_key(manager.doctor())["git"]["state"], "fail")

    def test_fix_prepares_and_releases(self):
        with doctored() as (manager, simctl, wda, commands):
            checks = manager.doctor(fix=True)
            found = by_key(checks)
            self.assertEqual(found["prepare"]["state"], "ok")
            self.assertIn("127.0.0.1:8310", found["prepare"]["detail"])
            self.assertEqual(len(simctl.simctl_calls("create")), 1)
            self.assertEqual(manager.lease.held, set())
            self.assertEqual(found["simulators"]["state"], "ok")
            self.assertIn("booted, WebDriverAgent ready", found["simulators"]["detail"])
            self.assertLess(list(found).index("prepare"), list(found).index("simulators"))

    def test_a_booted_simulator_without_wda_is_a_warning(self):
        with doctored() as (manager, simctl, wda, commands):
            manager.acquire().release()
            wda.ready = lambda url: False
            found = by_key(manager.doctor())
        self.assertEqual((found["simulators"]["state"], found["simulators"]["fix"]), ("warn", "mobster sim prepare"))

    def test_a_blocked_port_is_a_warning(self):
        with doctored() as (manager, simctl, wda, commands):
            manager.acquire().release()
            wda.busy = {8310}
            found = by_key(manager.doctor())
        self.assertEqual(found["ports"]["state"], "warn")
        self.assertIn("8311", found["ports"]["detail"])

    def test_the_key_is_reported_never_shown(self):
        with doctored(environ={"OPENAI_API_KEY": SECRET}) as (manager, simctl, wda, commands):
            checks = manager.doctor(fix=True)
        self.assertEqual(by_key(checks)["key"]["state"], "ok")
        self.assertNotIn(SECRET, json.dumps(checks))
        self.assertNotIn(SECRET, doctor.report(checks))
        self.assertNotIn(SECRET[:12], json.dumps(checks))

    KEY_VARIABLES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MOBSTER_SMART_MODEL", "MOBSTER_HELPER_PROVIDER",
                     "TEXT_MODEL", "TEXT_MODEL_API_KEY", "TEXT_MODEL_BASE_URL", "TEXT_MODEL_PROVIDER")

    @contextlib.contextmanager
    def only(self, environ):
        with mock.patch.dict("os.environ"):
            for name in self.KEY_VARIABLES:
                os.environ.pop(name, None)
            os.environ.update(environ)
            yield

    def test_the_key_row_names_the_key_and_model_smart_actually_runs_on(self):
        # Since #44, Smart runs on gpt-5.6-sol with an OpenAI key or on Claude with an Anthropic key. The row
        # says which, and names the model the Smart client is built for.
        from mobile_agent import engines
        gpt, claude = engines.SMART_CONFIG.model, engines.ANTHROPIC_SMART_MODEL
        cases = [({"OPENAI_API_KEY": SECRET}, "OpenAI", gpt, "OpenAIChat"),
                 ({"ANTHROPIC_API_KEY": SECRET}, "Anthropic", claude, "AnthropicChat"),
                 ({"OPENAI_API_KEY": SECRET, "ANTHROPIC_API_KEY": SECRET + "x"}, "OpenAI", gpt, "OpenAIChat"),
                 ({"OPENAI_API_KEY": SECRET, "ANTHROPIC_API_KEY": SECRET + "x",
                   "MOBSTER_SMART_MODEL": "claude-sonnet-4-5"}, "Anthropic", "claude-sonnet-4-5", "AnthropicChat"),
                 ({"ANTHROPIC_API_KEY": SECRET, "MOBSTER_SMART_MODEL": "claude-opus-5-5"}, "Anthropic",
                  "claude-opus-5-5", "AnthropicChat")]
        for environ, provider, model, client in cases:
            with self.subTest(environ=sorted(environ)), self.only(environ):
                found = doctor.check_key()
                self.assertEqual((found["label"], found["state"]), ("Smart key", "ok"))
                self.assertEqual(found["detail"], f"your {provider} key is set: Smart runs flows on {model}")
                self.assertEqual(engines.smart_config().model, model)
                self.assertEqual(type(engines.build_client(key=engines.smart_key())).__name__, client)
                self.assertNotIn(SECRET, json.dumps(found))

    def test_no_key_says_either_provider_will_do(self):
        with self.only({}):
            found = doctor.check_key()
        self.assertEqual(found["state"], "skip")
        self.assertEqual(found["detail"], "not set: launch-only and key-less checks need none; Smart needs your "
                                          "OpenAI or Anthropic key")
        self.assertIn("OPENAI_API_KEY=… or ANTHROPIC_API_KEY=…", found["fix"])

    def test_a_chosen_model_without_any_key_names_its_provider(self):
        with self.only({"MOBSTER_SMART_MODEL": "claude-sonnet-5-5"}):
            found = doctor.check_key()
        self.assertEqual(found["state"], "skip")
        self.assertIn("Smart on claude-sonnet-5-5 needs your Anthropic key", found["detail"])
        self.assertEqual(found["fix"], "For Smart, put ANTHROPIC_API_KEY=… in an env file and pass --env-file PATH")

    def test_a_chosen_model_whose_key_is_missing_says_so_when_the_other_key_is_set(self):
        # OPENAI_API_KEY is set, but MOBSTER_SMART_MODEL names Claude and there is no Anthropic key: Smart
        # can't run, and "not set" or "Smart needs one" would both be wrong.
        from mobile_agent import engines
        with self.only({"OPENAI_API_KEY": SECRET, "MOBSTER_SMART_MODEL": "claude-sonnet-5-5"}):
            self.assertIsNone(engines.smart_key())
            found = doctor.check_key()
        self.assertEqual(found["state"], "warn")
        self.assertEqual(found["detail"], "MOBSTER_SMART_MODEL is claude-sonnet-5-5, which needs ANTHROPIC_API_KEY; "
                                          "only your OpenAI key is set, so Smart can't run")
        self.assertEqual(found["fix"], "Add ANTHROPIC_API_KEY=… to your env file, or remove MOBSTER_SMART_MODEL to "
                                       f"run Smart on {engines.SMART_CONFIG.model}")
        self.assertNotIn(SECRET, json.dumps(found))
        with self.only({"ANTHROPIC_API_KEY": SECRET, "MOBSTER_SMART_MODEL": engines.SMART_CONFIG.model}):
            found = doctor.check_key()
        self.assertEqual(found["state"], "warn")
        self.assertEqual(found["detail"], f"MOBSTER_SMART_MODEL is {engines.SMART_CONFIG.model}, which needs "
                                          "OPENAI_API_KEY; only your Anthropic key is set, so Smart can't run")
        self.assertTrue(found["fix"].endswith(f"run Smart on {engines.ANTHROPIC_SMART_MODEL}"))

    def test_the_mismatch_shows_its_fix_and_is_no_problem_for_verify(self):
        with self.only({"OPENAI_API_KEY": SECRET, "MOBSTER_SMART_MODEL": "claude-sonnet-5-5"}), \
                doctored() as (manager, simctl, wda, commands):
            checks = manager.doctor()
        self.assertEqual(by_key(checks)["key"]["state"], "warn")
        text = doctor.report(checks)
        self.assertIn("→ Add ANTHROPIC_API_KEY=… to your env file", text)
        self.assertIn("Ready for `mobster verify`.", text)
        self.assertNotIn(SECRET, text)

    def test_a_crashing_check_is_reported_not_raised(self):
        with doctored() as (manager, simctl, wda, commands):
            with mock.patch.object(doctor, "check_disk", side_effect=PermissionError("denied")):
                found = by_key(manager.doctor())
        self.assertEqual(found["disk"]["state"], "fail")
        self.assertIn("denied", found["disk"]["detail"])

    def test_report_lists_fixes_under_problems(self):
        checks = [doctor.check("xcode", "Xcode", "fail", "not installed", "Install Xcode"),
                  doctor.check("key", "Smart key", "skip", "not set", "Set it")]
        text = doctor.report(checks)
        self.assertIn("→ Install Xcode", text)
        self.assertNotIn("→ Set it", text)
        self.assertIn("1 problem to fix before `mobster verify` can run.", text)


if __name__ == "__main__":
    unittest.main()
