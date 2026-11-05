"""CI tokens and the hosted runner files: the token's scope, the workflows, the templates and the doctor's rows."""

import io
import json
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from mobile_agent import plan_gate
from mobile_agent.devkit import DevError, ci, ci_cli, ci_hosts
from mobile_agent.tests.test_plan_gate import KEY, OTHER_SEED, SEED, b64, sign

DAY = 86_400


def ci_token(seed=SEED, **over):
    now = int(time.time())
    claims = {"v": 4, "kind": "ci", "scope": "simulator", "sub": "user-1", "plan": "monthly", "status": "active",
              "iat": now - 60, "exp": now + 30 * DAY, **over}
    claims = {name: value for name, value in claims.items() if value is not None}
    payload = json.dumps(claims, separators=(",", ":")).encode()
    return f"{b64(payload)}.{b64(sign(seed, payload))}"


def env(token=None, flag="on"):
    """An environment whose license key is the test key (the loopback development setup)."""
    key = b64(KEY)
    out = {"MOBSTER_LICENSE_PUBLIC_KEY": key, "MOBSTER_ACCOUNT_API": "http://127.0.0.1:9"}
    if flag:
        out["MOBSTER_CI_TOKENS"] = flag
    if token:
        out["MOBSTER_CI_TOKEN"] = token
    return out


OFFLINE = lambda token: None  # noqa: E731 -- the license API couldn't be asked
SIMS = SimpleNamespace(device=None, testflight=None)
PHONE = lambda name: {"kind": "usb"}  # noqa: E731
SIMULATOR = lambda name: {"kind": "simulator"}  # noqa: E731


class TokenTests(unittest.TestCase):
    def tearDown(self):
        plan_gate._ci_scope.clear()

    def problem(self, token, online=lambda token: None, **kw):
        return plan_gate.ci_token_problem(env(token, **kw), online=online)

    def test_a_signed_live_simulator_token_is_accepted(self):
        self.assertIsNone(self.problem(ci_token()))
        self.assertIsNone(self.problem(ci_token(plan="team")))

    def test_the_flag_is_off_by_default(self):
        self.assertFalse(plan_gate.CI_TOKENS_ENABLED)
        self.assertEqual(self.problem(ci_token(), flag=None), "off")
        self.assertFalse(plan_gate.grant_ci_token("ci", SIMS, env(ci_token(), flag=None), online=OFFLINE))

    def test_everything_unsure_refuses(self):
        cases = {"no_token": None, "bad_signature": ci_token(OTHER_SEED), "expired": ci_token(exp=int(time.time()) - 1),
                 "inactive": ci_token(status="canceled"), "malformed": ci_token(exp=None),
                 "unsupported_version": ci_token(v=2)}
        for expected, token in cases.items():
            with self.subTest(expected):
                self.assertEqual(self.problem(token), expected)
        for name, over in (("a station kind", {"kind": "station"}), ("a phone scope", {"scope": "phone"}),
                           ("no scope", {"scope": None}), ("an unknown plan", {"plan": "free"})):
            with self.subTest(name):
                self.assertEqual(self.problem(ci_token(**over)), "malformed")
        self.assertEqual(self.problem("not-a-token"), "malformed")

    def test_a_personal_token_is_not_a_ci_token(self):
        payload = json.dumps({"v": 2, "sub": "u", "plan": "monthly", "status": "active", "machine": "m",
                              "iat": 1, "exp": int(time.time()) + DAY}).encode()
        self.assertEqual(self.problem(f"{b64(payload)}.{b64(sign(SEED, payload))}"), "unsupported_version")

    def test_it_licenses_ci_and_test_on_simulators_only(self):
        e = env(ci_token())
        for command in ("ci", "test"):
            with self.subTest(command):
                self.assertTrue(plan_gate.grant_ci_token(command, SIMS, e, online=OFFLINE))
        self.assertTrue(plan_gate.grant_ci_token("ci", SimpleNamespace(device="Sim", testflight=None), e,
                                                 named_device=SIMULATOR, online=OFFLINE))
        for name, args, named in (("a phone", SimpleNamespace(device="Sam", testflight=None), PHONE),
                                  ("a testflight build", SimpleNamespace(device=None, testflight=["com.x"]), None),
                                  ("a device it can't look up", SimpleNamespace(device="Ghost", testflight=None),
                                   lambda name: 1 / 0)):
            with self.subTest(name):
                self.assertFalse(plan_gate.grant_ci_token("ci", args, e, named_device=named, online=OFFLINE))
                self.assertEqual(plan_gate._ci_scope, [])
        for command in ("run", "verify", "phone", "serve", "xcode"):
            with self.subTest(command):
                self.assertFalse(plan_gate.grant_ci_token(command, SIMS, e, online=OFFLINE))

    def test_ci_run_with_a_phone_among_its_devices_is_not_a_simulator_run(self):
        from mobile_agent.cli_parser import build_parser
        e = env(ci_token())
        kinds = {"Sim": {"kind": "simulator"}, "Sim 2": {"kind": "simulator"}, "Sam": {"kind": "usb"}}.get
        sims = build_parser().parse_args(["ci", "run", "--device", "Sim", "--device", "Sim 2"])
        self.assertTrue(plan_gate.grant_ci_token("ci", sims, e, named_device=kinds, online=OFFLINE))
        phone = build_parser().parse_args(["ci", "run", "--device", "Sim", "--device", "Sam"])
        self.assertFalse(plan_gate.grant_ci_token("ci", phone, e, named_device=kinds, online=OFFLINE))
        self.assertEqual(plan_gate._ci_scope, [])

    def test_a_granted_token_opens_the_gate_with_no_account_and_nothing_else_does(self):
        with tempfile.TemporaryDirectory() as folder:
            gate = plan_gate.PlanGate(Path(folder) / "account.json", keys=[KEY], machine="m")
            self.assertEqual(gate.problem(), "no_account")
            self.assertTrue(plan_gate.grant_ci_token("ci", SIMS, env(ci_token()), online=OFFLINE))
            self.assertIsNone(gate.problem())
            gate.require()
            plan_gate._ci_scope[:] = [ci_token(OTHER_SEED)]  # a token this gate's keys don't trust
            self.assertEqual(gate.problem(), "no_account")

    def test_the_cli_grants_before_it_checks_the_plan(self):
        from mobile_agent import __main__ as main_cli
        gate = plan_gate.PlanGate(Path(tempfile.gettempdir()) / "mobster-none" / "account.json", keys=[KEY],
                                  machine="m")
        args = SimpleNamespace(ci_command="run", device=None, testflight=None, json=False)
        with mock.patch.object(plan_gate, "bundled_gate", return_value=gate), \
                mock.patch.object(plan_gate, "ci_token_online", OFFLINE), \
                mock.patch.dict("os.environ", env(ci_token()), clear=False):
            self.assertIsNone(main_cli.plan_refusal(args, "ci"))
            phone = SimpleNamespace(ci_command="run", device="Sam", testflight=None, json=True)
            with mock.patch("mobile_agent.device_targets.named_device", PHONE), \
                    mock.patch("builtins.print") as printed:
                self.assertEqual(main_cli.plan_refusal(phone, "ci"), main_cli.EXIT_COULDNT)
            self.assertIn("plan_required", printed.call_args[0][0])


class OnlineCheckTests(unittest.TestCase):
    """The license API is asked before a run; what it can't answer falls back to the signature and expiry."""

    def tearDown(self):
        plan_gate._ci_scope.clear()

    def asked(self, status, value=None, raises=None):
        from mobile_agent import share
        patch = mock.patch.object(share, "call", side_effect=raises) if raises else \
            mock.patch.object(share, "call", return_value=(status, value or {}))
        with patch as call:
            result = plan_gate.ci_token_online("the-token")
        return result, call

    def test_the_token_is_sent_to_the_check_route_and_a_200_goes_ahead(self):
        result, call = self.asked(200, {"active": True})
        self.assertIsNone(result)
        method, path, token = call.call_args[0][:3]
        self.assertEqual((method, path, token), ("POST", "/api/ci-tokens/check", "the-token"))
        self.assertLessEqual(call.call_args.kwargs["timeout"], 5)

    def test_a_revocation_stops_the_run(self):
        for status, value, expected in ((403, {"error": "revoked"}, "revoked"), (403, {"error": "expired"}, "expired"),
                                        (403, {"error": "plan_required"}, "plan_required"), (401, {}, "revoked"),
                                        (403, {"error": "something new"}, "revoked")):
            with self.subTest(status=status, value=value):
                self.assertEqual(self.asked(status, value)[0], expected)

    def test_no_answer_falls_back_to_the_offline_check(self):
        from mobile_agent import share
        for name, outcome in (("no network", dict(status=0, raises=share.ShareError("unreachable", 503, "x"))),
                              ("busy", dict(status=503)), ("rate limited", dict(status=429)),
                              ("a server error", dict(status=500, value={"error": "internal"}))):
            with self.subTest(name):
                self.assertIsNone(self.asked(**outcome)[0])

    def test_a_revoked_token_is_not_granted_and_an_unreachable_check_is(self):
        e = env(ci_token())
        self.assertFalse(plan_gate.grant_ci_token("ci", SIMS, e, online=lambda token: "revoked"))
        self.assertEqual(plan_gate._ci_scope, [])
        self.assertEqual(plan_gate.ci_token_problem(e, online=lambda token: "revoked"), "revoked")
        self.assertTrue(plan_gate.grant_ci_token("ci", SIMS, e, online=OFFLINE))

    def test_a_bad_signature_never_reaches_the_network(self):
        asked = []
        self.assertEqual(plan_gate.ci_token_problem(env(ci_token(OTHER_SEED)), online=asked.append), "bad_signature")
        self.assertEqual(asked, [])


class AccountCliTests(unittest.TestCase):
    def run_cli(self, *argv, environ=None, answer=(200, {})):
        from mobile_agent.cli_parser import build_parser
        from mobile_agent.devkit import account_cli
        calls = []

        def call(method, path, token, body=None):
            calls.append((method, path, token, body))
            return answer
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = account_cli.run(build_parser().parse_args(["account", "ci-token", *argv]),
                                   environ={"MOBSTER_ACCOUNT_TOKEN": "access-token"} if environ is None else environ,
                                   call=call)
        return code, out.getvalue(), err.getvalue(), calls

    def test_create_posts_the_name_and_days_and_prints_the_token_once(self):
        answer = (201, {"token": "SECRET.TOKEN", "id": "i1", "name": "app repo", "expires_at": "2027-01-22T00:00:00Z"})
        code, out, err, calls = self.run_cli("create", "app repo", "--days", "30", answer=answer)
        self.assertEqual((code, calls), (0, [("POST", "/api/account/ci-tokens", "access-token",
                                              {"name": "app repo", "days": 30})]))
        self.assertIn("SECRET.TOKEN", out)
        self.assertIn("works until 2027-01-22", out)
        self.assertNotIn("SECRET", err)
        self.assertEqual(self.run_cli("create", "x", answer=answer)[3][0][3], {"name": "x"})

    def test_list_and_revoke(self):
        tokens = {"tokens": [{"id": "i1", "name": "app repo", "state": "active", "expires_at": "2027-01-22T00:00:00Z",
                              "last_used_at": None}]}
        code, out, _, calls = self.run_cli("list", answer=(200, tokens))
        self.assertEqual((code, calls[0][:2]), (0, ("GET", "/api/account/ci-tokens")))
        self.assertIn("i1  active    until 2027-01-22  used never  app repo", out)
        self.assertNotIn("token\"", out)
        self.assertIn("no CI tokens", self.run_cli("list", answer=(200, {"tokens": []}))[1])
        code, out, _, calls = self.run_cli("revoke", "i1", answer=(200, {"token": {"name": "app repo"}}))
        self.assertEqual((code, calls[0][:2]), (0, ("DELETE", "/api/account/ci-tokens/i1")))
        self.assertIn("Revoked", out)

    def test_it_says_what_to_do_without_a_session_and_passes_the_servers_words_on(self):
        code, out, err, calls = self.run_cli("list", environ={})
        self.assertEqual((code, calls), (3, []))
        self.assertIn("MOBSTER_ACCOUNT_TOKEN", err)
        code, _, err, _ = self.run_cli("create", "x", answer=(403, {"message": "A CI token needs a Mobster plan."}))
        self.assertEqual(code, 3)
        self.assertIn("A CI token needs a Mobster plan.", err)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_hosted_github_workflow_installs_caches_prepares_runs_and_comments(self):
        path, notes = ci.init(self.root, host="github", hosted=True)
        self.assertEqual((path, notes), (self.root / ".github/workflows/mobster.yml", []))
        flow = yaml.safe_load(path.read_text())
        job = flow["jobs"]["checks"]
        self.assertEqual(job["runs-on"], "macos-15")
        self.assertEqual(flow["permissions"], {"contents": "read", "pull-requests": "write"})
        names = [step.get("name") or step.get("uses") for step in job["steps"]]
        self.assertEqual(names, ["actions/checkout@v4", "Install Mobster", "Cache WebDriverAgent",
                                 "Prepare a simulator", "Test what this PR changed", "Upload the report",
                                 "Comment on the PR"])
        self.assertIn("brew install --cask radishsoftware/tap/mobster-app", job["steps"][1]["run"])
        cache = job["steps"][2]
        self.assertEqual(cache["uses"], "actions/cache@v4")
        self.assertIn("Application Support/app.mobster.desktop/wda", cache["with"]["path"])
        self.assertIn("steps.mobster.outputs.xcode", cache["with"]["key"])
        self.assertIn('"$MOBSTER" sim doctor --fix', job["steps"][3]["run"])
        run = job["steps"][4]
        self.assertEqual(run["env"]["MOBSTER_CI_TOKEN"], "${{ secrets.MOBSTER_CI_TOKEN }}")
        self.assertNotIn("--device", run["run"])
        self.assertEqual(job["steps"][5]["if"], "always()")
        self.assertEqual(flow["env"]["MOBSTER"], ci.APP_AGENT)

    def test_a_hosted_runner_has_no_phone_to_name(self):
        for kw in ({"devices": ("Sam",)}, {"testflight": ("com.x",)}):
            with self.subTest(kw), self.assertRaises(DevError):
                ci.init(self.root, host="github", hosted=True, **kw)
        with self.assertRaises(DevError):
            ci.init(self.root, host="circleci", devices=("Sam",))
        with self.assertRaises(DevError):
            ci.init(self.root, host="gitlab", hosted=True)
        self.assertFalse((self.root / ".github").exists())

    def test_the_self_hosted_workflow_is_unchanged(self):
        path, _ = ci.init(self.root, host="github")
        self.assertEqual(yaml.safe_load(path.read_text())["jobs"]["checks"]["runs-on"],
                         ["self-hosted", "macOS", "mobster"])

    def test_every_template_installs_prepares_and_runs_with_the_token(self):
        for host in ("github", *ci.HOSTED_HOSTS):
            with self.subTest(host):
                text = ci_hosts.render(host)
                self.assertNotIn("@", text.replace("@v4", "").replace("@mobster", "").replace("@8", "")
                                 .replace("@1", "").replace("@2", ""), text)
                self.assertIn(ci_hosts.INSTALL, text)
                self.assertIn("sim doctor --fix", text)
                self.assertIn("ci run", text)
                self.assertNotIn("--device", text)
                self.assertIn("MOBSTER_CI_TOKEN", text)

    def test_xcode_cloud_script(self):
        path, _ = ci.init(self.root, host="xcode-cloud")
        self.assertEqual(path, self.root / "ci_scripts/ci_post_xcodebuild.sh")
        self.assertTrue(path.stat().st_mode & stat.S_IXUSR)
        text = path.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("CI_PULL_REQUEST_NUMBER", text)
        self.assertIn('exit "$status"', text)

    def test_codemagic_bitrise_and_circleci_files_parse_and_hold_the_steps(self):
        flow = yaml.safe_load(ci.init(self.root, host="codemagic")[0].read_text())["workflows"]["mobster"]
        self.assertEqual(flow["triggering"]["events"], ["pull_request"])
        self.assertEqual(flow["environment"]["groups"], ["mobster"])
        self.assertEqual([step["script"] for step in flow["scripts"]][1:], ["$MOBSTER sim doctor --fix",
                                                                           "$MOBSTER ci run"])
        self.assertEqual(flow["artifacts"], [".mobster/ci/**"])
        bitrise = yaml.safe_load(ci.init(self.root, host="bitrise")[0].read_text())["workflows"]["mobster"]
        self.assertEqual(len(bitrise["steps"]), 4)
        self.assertTrue(bitrise["steps"][-1]["deploy-to-bitrise-io@2"]["is_always_run"])
        circle = yaml.safe_load(ci.init(self.root, host="circleci")[0].read_text())
        self.assertIn("macos", circle["jobs"]["mobster"])
        self.assertEqual(circle["workflows"]["mobster"]["jobs"], ["mobster"])

    def test_a_project_file_that_exists_is_left_alone_and_the_job_goes_beside_it(self):
        (self.root / "codemagic.yaml").write_text("workflows: {}\n")
        path, notes = ci.init(self.root, host="codemagic")
        self.assertEqual(path, self.root / ".mobster/hosts/codemagic.yaml")
        self.assertEqual((self.root / "codemagic.yaml").read_text(), "workflows: {}\n")
        self.assertIn("Merge it into codemagic.yaml", notes[0])
        with self.assertRaises(DevError):
            ci.init(self.root, host="codemagic")
        (self.root / "ci_scripts").mkdir()
        (self.root / "ci_scripts/ci_post_xcodebuild.sh").write_text("#!/bin/sh\n")
        with self.assertRaises(DevError):
            ci.init(self.root, host="xcode-cloud")


class HostTests(unittest.TestCase):
    def test_each_host_is_told_by_its_own_variable(self):
        for host, variables in (("github", {"GITHUB_ACTIONS": "true"}), ("gitlab", {"GITLAB_CI": "true"}),
                                ("xcode-cloud", {"CI_XCODE_CLOUD": "TRUE"}), ("codemagic", {"CM_BUILD_ID": "1"}),
                                ("bitrise", {"BITRISE_IO": "true"}), ("circleci", {"CIRCLECI": "true"})):
            self.assertEqual(ci.host_of(variables), host)
        self.assertIsNone(ci.host_of({}))
        self.assertTrue(ci.is_hosted({"CIRCLECI": "true"}))
        self.assertTrue(ci.is_hosted({"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}))
        self.assertFalse(ci.is_hosted({"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"}))
        self.assertFalse(ci.is_hosted({}))

    def test_the_base_branch_comes_from_the_host(self):
        for variables, expected in (({"CI_PULL_REQUEST_TARGET_BRANCH": "develop"}, "origin/develop"),
                                    ({"BITRISEIO_GIT_BRANCH_DEST": "main"}, "origin/main"),
                                    ({"MOBSTER_BASE_BRANCH": "trunk"}, "origin/trunk"),
                                    ({"GITHUB_BASE_REF": "a", "MOBSTER_BASE_BRANCH": "b"}, "origin/a")):
            self.assertEqual(ci.base_ref(variables)[0], expected)
        self.assertEqual(ci.base_ref({}), (None, None))

    def test_a_phone_is_refused_on_a_hosted_host_unless_the_runner_lists_it(self):
        with self.assertRaises(DevError):
            ci.guard(("Sam",), (), {"CIRCLECI": "true"})
        self.assertIsNone(ci.guard(("Sam",), (), {"CIRCLECI": "true", "MOBSTER_RUNNER_DEVICES": "Sam"}))


class DoctorTests(unittest.TestCase):
    def rows(self, hosted=True, token=lambda: None, **kw):
        with tempfile.TemporaryDirectory() as home:
            agent = Path(home) / "mobster-agent"
            agent.write_text("")
            answers = {"xcodebuild": (0, "Xcode 16.4\n"),
                       "xcrun": (0, json.dumps({"runtimes": [{"name": "iOS 18.5", "isAvailable": True}]})),
                       "pmset": (0, " sleep 0\n autorestart 1\n"), "defaults": (0, "ci\n"), "launchctl": (0, "")}
            return {row.name: row for row in ci.doctor(
                run=lambda argv: answers[argv[0]], home=home, environ={}, machine="arm64", plan=lambda: "no_account",
                agent=str(agent), disk=lambda path: 80e9, hosted=hosted, ci_token=token, **kw)}

    def test_a_hosted_runner_skips_the_rows_that_keep_a_mac_running_and_checks_the_token(self):
        rows = self.rows()
        for name in ("System sleep", "Restart after a power cut", "Automatic login", "Runner service",
                     "Phones this runner allows"):
            self.assertEqual(rows[name].status, "skip", name)
        self.assertNotIn("Mobster plan", rows)
        self.assertEqual(rows["CI token"].status, "ok")
        self.assertFalse([row for row in rows.values() if row.status == "fail"])

    def test_a_missing_or_expired_token_fails_with_the_fix(self):
        for problem, words in (("no_token", "MOBSTER_CI_TOKEN"), ("expired", "new CI token"),
                               ("off", "Update Mobster")):
            row = self.rows(token=lambda problem=problem: problem)["CI token"]
            self.assertEqual(row.status, "fail")
            self.assertIn(words, row.fix)

    def test_a_mac_of_yours_still_checks_the_plan_and_the_sleep_rows(self):
        rows = self.rows(hosted=False)
        self.assertEqual(rows["Mobster plan"].status, "fail")
        self.assertNotIn("CI token", rows)
        self.assertNotEqual(rows["System sleep"].status, "skip")

    def test_the_cli_prints_the_skipped_rows_with_a_dash(self):
        out = io.StringIO()
        parser_args = SimpleNamespace(json=False, hosted=True)
        rows = self.rows()
        with mock.patch.object(ci, "doctor", return_value=list(rows.values())), mock.patch("sys.stdout", out):
            code = ci_cli._doctor(parser_args, Path("."), {})
        self.assertEqual(code, 0)
        self.assertIn("- System sleep", out.getvalue())
        self.assertIn("This runner is ready", out.getvalue())


class InitCliTests(unittest.TestCase):
    def test_init_hosted_prints_the_hosted_next_steps(self):
        from mobile_agent.cli_parser import build_parser
        with tempfile.TemporaryDirectory() as folder:
            out = io.StringIO()
            args = build_parser().parse_args(["ci", "init", "--host", "github", "--hosted"])
            with mock.patch("sys.stdout", out):
                self.assertEqual(ci_cli.run(args, cwd=Path(folder), environ={}), 0)
            self.assertIn("Wrote .github/workflows/mobster.yml.", out.getvalue())
            self.assertIn("MOBSTER_CI_TOKEN", out.getvalue())
            self.assertNotIn("self-hosted", out.getvalue())
            printed = io.StringIO()
            args = build_parser().parse_args(["ci", "init", "--host", "circleci", "--print"])
            with mock.patch("sys.stdout", printed):
                self.assertEqual(ci_cli.run(args, cwd=Path(folder), environ={}), 0)
            self.assertIn("resource_class", printed.getvalue())
            self.assertFalse((Path(folder) / ".circleci").exists())


if __name__ == "__main__":
    unittest.main()
