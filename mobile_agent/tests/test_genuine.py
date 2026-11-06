"""The frozen agent's own seal check (genuine.py) with ``premiumModel`` on: where a changed or unchecked copy refuses,
and where the check never runs. Fakes only: no real codesign and no real app."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .. import genuine, plan_gate, premium_model
from ..api_errors import APIError

TEAM = "ABCDE12345"
FAILED = "codesign: Mobster.app: a sealed resource is missing or invalid"
CHANGED = (genuine.CHANGED, FAILED)
UNCHECKED = (genuine.UNCHECKED, "codesign took over 20 s")


def never(*args, **kwargs):
    raise AssertionError("codesign ran")


class ProblemTests(unittest.TestCase):
    """problem(): what one check of the app around the agent answers."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.app = self.root / "Mobster.app"
        (self.app / "Contents" / "MacOS").mkdir(parents=True)
        self.agent = self.app / "Contents" / "MacOS" / "mobster-agent"
        self.agent.write_text("")
        self.team_file = self.root / genuine.TEAM_FILE
        self.calls = []

    def passes(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    def problem(self, team=TEAM, executable=None, run=None):
        if team is not None:
            self.team_file.write_text(team)
        return genuine.problem(self.team_file, executable or self.agent, run or self.passes)

    def signed_by(self, said, code=0):
        """A codesign that answers ``-d`` with ``said`` on stderr (where codesign prints it) and refuses to verify."""
        def run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            if argv[1] != "-d":
                raise AssertionError("verified an app whose team file is empty")
            return SimpleNamespace(returncode=code, stderr=said, stdout="")
        return run

    def signature(self, team):
        return (f"Executable={self.agent}\nIdentifier=mobster-agent\nFormat=Mach-O thin (arm64)\n"
                f"Signature size=9045\nTeamIdentifier={team}\nSealed Resources=none\n")

    def test_a_genuine_app_passes_a_strict_deep_offline_check_of_the_whole_app(self):
        self.assertIsNone(self.problem())
        (argv, kwargs), = self.calls
        self.assertEqual(argv, ["/usr/bin/codesign", "--verify", "--strict", "--deep",
                                f'-R=anchor apple generic and certificate leaf[subject.OU] = "{TEAM}"',
                                str(self.app.resolve())])
        self.assertEqual(kwargs["timeout"], genuine.CHECK_SECONDS)
        self.assertLessEqual(genuine.CHECK_SECONDS, 20)

    def test_a_check_codesign_rejects_is_a_changed_copy_and_says_why(self):
        for code in (1, 3):
            failed = self.problem(run=lambda argv, **kwargs: SimpleNamespace(
                returncode=code, stderr="Mobster.app: a sealed resource is missing or invalid\nfile modified: x\n",
                stdout=""))
            self.assertEqual(failed, CHANGED)

    def test_a_check_that_errors_or_runs_out_of_time_is_unchecked(self):
        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        def missing(argv, **kwargs):
            raise FileNotFoundError(argv[0])
        self.assertEqual(self.problem(run=slow), UNCHECKED)
        self.assertEqual(self.problem(run=missing)[0], genuine.UNCHECKED)

    def test_an_ad_hoc_build_is_never_refused_and_checks_nothing_more(self):
        # An empty team file beside an agent that carries no team, as build-sidecar.sh makes without a Developer ID.
        loose = self.root / "loose-agent"
        loose.write_text("")
        for team in ("", "\n", "  "):
            for agent in (self.agent, loose):
                self.calls.clear()
                self.assertIsNone(self.problem(team, executable=agent, run=self.signed_by(self.signature("not set"))))
                (argv, kwargs), = self.calls
                self.assertEqual(argv, ["/usr/bin/codesign", "-d", "--verbose=2", str(agent.resolve())])
                self.assertEqual(kwargs["timeout"], genuine.CHECK_SECONDS)

    def test_an_empty_team_file_beside_a_teams_signature_refuses(self):
        # A Developer ID build always writes its team (finish-release.sh checks), so this file was emptied.
        emptied = (genuine.UNCHECKED, "the build's signing team is empty, but a team signed the agent")
        for team in (TEAM, "ABCDE12345"):
            self.assertEqual(self.problem("", run=self.signed_by(self.signature(team))), emptied, team)

    def test_an_empty_team_file_with_a_signature_that_cant_be_read_refuses(self):
        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        def missing(argv, **kwargs):
            raise FileNotFoundError(argv[0])
        for run in (self.signed_by("mobster-agent: code object is not signed at all\n", code=1),
                    self.signed_by(self.signature("not set"), code=1),
                    self.signed_by("Executable=x\nIdentifier=mobster-agent\n"),
                    self.signed_by(self.signature("not set") + "TeamIdentifier=not set\n"),
                    self.signed_by(""), slow, missing):
            failed = self.problem("", run=run)
            self.assertIsNotNone(failed)
            self.assertEqual(failed[0], genuine.UNCHECKED)

    def test_a_missing_team_file_is_unchecked(self):
        self.assertEqual(genuine.problem(self.root / "missing", self.agent, never)[0], genuine.UNCHECKED)

    def test_a_team_that_isnt_ten_capitals_and_digits_is_unchecked(self):
        for team in ("abc", "65683s7zn4", "ABCDE12345X", "65683S7ZN", "ABCDE12345\nX", "6568-S7ZN4",
                     "Developer ID Application: Radish Retail, LLC (ABCDE12345)", "ABCDE1234Ä"):
            self.assertEqual(self.problem(team, run=never)[0], genuine.UNCHECKED, team)

    def test_no_app_around_the_agent_is_unchecked(self):
        loose = self.root / "binaries" / "mobster-agent"
        loose.parent.mkdir()
        loose.write_text("")
        self.assertEqual(self.problem(executable=loose, run=never), (genuine.UNCHECKED, "no app around the agent"))

    def test_the_team_file_is_read_from_the_bundle_folder_and_refuses_without_one(self):
        self.team_file.write_text(TEAM)
        with patch.object(genuine.sys, "_MEIPASS", str(self.root), create=True):
            self.assertIsNone(genuine.problem(None, self.agent, self.passes))
        self.assertEqual(len(self.calls), 1)
        with patch.object(genuine.sys, "_MEIPASS", None, create=True):
            self.assertEqual(genuine.problem(None, self.agent, never), (genuine.UNCHECKED, "no bundle folder"))

    def test_the_team_comes_from_a_developer_id_identity_only(self):
        # genuine.rs's table for team.rs: build-sidecar.sh writes the team by the same rule.
        self.assertEqual(genuine.team_of("Developer ID Application: Radish Retail, LLC (ABCDE12345)"), TEAM)
        self.assertEqual(genuine.team_of("  Developer ID Application: Sam (ABCDE12345)  "), "ABCDE12345")
        for identity in ("-", "", "Apple Development: Sam (ABCDE12345)", "Developer ID Application: Sam (abc)",
                         "Developer ID Application: Sam", "Developer ID Application: Sam (ABCDE12345) (x)"):
            self.assertEqual(genuine.team_of(identity), "", identity)


@contextlib.contextmanager
def agent(on=True, frozen=True, answer=CHANGED, asked=None, environ=None):
    """The frozen agent (or a source checkout) with the flag on or off, whose check answers ``answer``."""
    def problem():
        if asked is not None:
            asked.append(1)
        return answer
    with patch.object(premium_model, "ON", on), patch.object(genuine.sys, "frozen", frozen, create=True), \
            patch.object(genuine, "problem", problem), patch.dict(plan_gate.os.environ, environ or {}, clear=True), \
            contextlib.redirect_stderr(io.StringIO()) as stderr:
        yield stderr


# Each way a copy fails its seal, and the sentence every path says for it.
KINDS = ((CHANGED, plan_gate.MODIFIED), (UNCHECKED, plan_gate.UNCHECKED_COPY))


def doctor(fix=False, apps=False):
    """`mobster doctor`'s parsed arguments: --fix and --apps are always there, False unless given."""
    return SimpleNamespace(fix=fix, apps=apps, json=True)


class FailureTests(unittest.TestCase):
    """failure(): only in the frozen agent with the flag on, and once per process."""

    def setUp(self):
        genuine._answer.clear()
        self.addCleanup(genuine._answer.clear)
        self.asked = []

    def test_a_failed_copy_is_checked_once_per_process_and_says_which_kind(self):
        for answer, kind in ((CHANGED, genuine.CHANGED), (UNCHECKED, genuine.UNCHECKED)):
            genuine._answer.clear()
            self.asked.clear()
            with agent(answer=answer, asked=self.asked) as stderr:
                self.assertEqual([genuine.failure() for _ in range(3)], [kind] * 3)
            self.assertEqual(len(self.asked), 1)
            self.assertIn(answer[1], stderr.getvalue())

    def test_a_genuine_copy_is_checked_once_per_process_too(self):
        with agent(answer=None, asked=self.asked) as stderr:
            self.assertEqual({genuine.failure() for _ in range(3)}, {None})
        self.assertEqual((len(self.asked), stderr.getvalue()), (1, ""))

    def test_with_the_flag_off_the_frozen_agent_never_checks(self):
        with agent(on=False, asked=self.asked, environ={premium_model.DEV_ENV: "1"}):
            self.assertIsNone(genuine.failure())
        self.assertEqual(self.asked, [])

    def test_a_source_checkout_never_checks(self):
        with agent(frozen=False, asked=self.asked, environ={premium_model.DEV_ENV: "1"}):
            self.assertTrue(premium_model.enabled())
            self.assertIsNone(genuine.failure())
        self.assertEqual(self.asked, [])


class ShippedPathTests(unittest.TestCase):
    """A real PlanGate, the CLI's plan_refusal and an MCP ToolSet holding a real gate, in the frozen agent."""

    def setUp(self):
        genuine._answer.clear()
        self.addCleanup(genuine._answer.clear)
        plan_gate._ci_token_verdicts.clear()
        self.addCleanup(plan_gate._ci_token_verdicts.clear)
        self.asked = []

    def gate(self, **kwargs):
        with tempfile.TemporaryDirectory() as folder:
            return plan_gate.PlanGate(Path(folder) / "account.json", keys=(), machine="m", **kwargs)

    def cli(self, command, args, gate):
        from .. import __main__ as main_cli
        with patch.object(plan_gate, "bundled_gate", return_value=gate), patch("builtins.print") as printed:
            code = main_cli.plan_refusal(args, command)
        return code, (json.loads(printed.call_args[0][0]) if printed.called else None), main_cli

    def tools(self, gate):
        from ..mcp_server.tools import ToolSet
        tools = object.__new__(ToolSet)
        tools.plan_gate = gate
        return tools

    def mcp(self, tools, names=("tap", "screen", "read_notifications", "listen", "status", "stop")):
        from ..mcp_server.catalog import ANNOTATIONS
        from ..mcp_server.protocol import ACTS
        return {name: tools.plan_refusal(name, ANNOTATIONS.get(name, ACTS)) for name in names}

    def test_a_failed_copys_gate_refuses_every_task_and_says_which_kind(self):
        for answer, sentence in KINDS:
            genuine._answer.clear()
            with agent(answer=answer):
                for gate in (self.gate(), self.gate(turned_off=False)):  # the app's, and a team's own
                    self.assertEqual(gate.status(), ("modified", None))  # one problem code for both kinds
                    self.assertEqual(gate.refusal(), sentence)
                    with self.assertRaises(APIError) as refused:
                        gate.require()
                    self.assertEqual((refused.exception.status, refused.exception.code), (403, "plan_required"))
                    self.assertEqual(str(refused.exception), sentence)
        self.assertIn("was changed", plan_gate.MODIFIED)
        self.assertIn("couldn't check", plan_gate.UNCHECKED_COPY)
        for sentence in (plan_gate.MODIFIED, plan_gate.UNCHECKED_COPY):
            self.assertIn("Download Mobster again", sentence)

    def test_a_kind_no_sentence_names_refuses_as_unchecked(self):
        with patch.object(genuine, "failure", return_value="something new"):
            self.assertEqual(plan_gate.seal_refusal(), plan_gate.UNCHECKED_COPY)
            self.assertEqual(plan_gate.stopped("run", SimpleNamespace(demo=False)), plan_gate.UNCHECKED_COPY)
            self.assertEqual(self.gate().refusal(), plan_gate.UNCHECKED_COPY)

    def test_a_genuine_copys_gate_lets_every_task_through(self):
        with agent(answer=None):
            self.assertEqual(self.gate().status(), (None, None))

    def test_the_cli_in_a_failed_copy_refuses_every_command_but_the_plain_doctor_and_the_version(self):
        for answer, sentence in KINDS:
            genuine._answer.clear()
            self.asked.clear()
            with agent(answer=answer, asked=self.asked):
                gate = self.gate()
                for command, args in (("run", SimpleNamespace(demo=False, json=True)),
                                      ("demo", SimpleNamespace(json=True)),
                                      ("ci", SimpleNamespace(ci_command="run", device=None, testflight=None,
                                                             json=True)),
                                      ("ci", SimpleNamespace(ci_command="doctor", json=True)),
                                      ("verify", SimpleNamespace(device=None, testflight=None, json=True)),
                                      ("serve", SimpleNamespace(json=True)), ("screen", SimpleNamespace(json=True)),
                                      ("tui", SimpleNamespace(json=True))):
                    code, printed, main_cli = self.cli(command, args, gate)
                    self.assertEqual((code, printed["error"]), (main_cli.EXIT_COULDNT, sentence), command)
                for command, args in (("doctor", doctor()), ("version", SimpleNamespace(json=True))):
                    self.assertIsNone(self.cli(command, args, gate)[0], command)
                # The MCP server starts, so its client reads the sentence on each tool (the next test).
                self.assertIsNone(self.cli("mcp", SimpleNamespace(json=True), gate)[0])
            self.assertEqual(len(self.asked), 1)

    def test_the_doctor_that_acts_refuses_in_a_failed_copy(self):
        # --fix installs the phone's helper and stops what holds Mobster's ports; --apps opens every app on the phone.
        for answer, sentence in KINDS:
            genuine._answer.clear()
            with agent(answer=answer):
                gate = self.gate()
                for args in (doctor(fix=True), doctor(apps=True), doctor(fix=True, apps=True),
                             SimpleNamespace(json=True)):  # a flag it can't read counts as given
                    code, printed, main_cli = self.cli("doctor", args, gate)
                    self.assertEqual((code, printed["error"]), (main_cli.EXIT_COULDNT, sentence), args)
                    self.assertEqual(plan_gate.stopped("doctor", args), sentence)

    def test_the_cli_in_a_genuine_copy_goes_ahead(self):
        with agent(answer=None):
            for command, args in (("run", SimpleNamespace(demo=False, json=True)), ("doctor", doctor(fix=True)),
                                  ("doctor", doctor(apps=True))):
                self.assertIsNone(self.cli(command, args, self.gate())[0], command)

    def test_the_mcp_server_in_a_failed_copy_refuses_every_tool_but_stop(self):
        for answer, sentence in KINDS:
            genuine._answer.clear()
            with agent(answer=answer):
                answers = self.mcp(self.tools(self.gate()))
            self.assertIsNone(answers.pop("stop"))
            for name, refused in answers.items():
                self.assertEqual((refused.is_error, refused.text), (True, sentence), name)

    def test_the_mcp_server_in_a_genuine_copy_goes_ahead(self):
        with agent(answer=None):
            self.assertEqual(set(self.mcp(self.tools(self.gate())).values()), {None})

    def test_with_the_flag_off_nothing_is_checked_and_the_plan_answers_as_before(self):
        with agent(on=False, asked=self.asked, environ={premium_model.DEV_ENV: "1"}):
            gate = self.gate()
            self.assertEqual(gate.status(), ("no_account", None))
            code, printed, main_cli = self.cli("run", SimpleNamespace(demo=False, json=True), gate)
            self.assertEqual((code, printed["error"]), (main_cli.EXIT_COULDNT, plan_gate.NO_PLAN))
            self.assertIsNone(self.cli("screen", SimpleNamespace(json=True), gate)[0])
            self.assertIsNone(self.cli("doctor", doctor(fix=True), gate)[0])
            tools = self.tools(gate)
            tools._drives_real_phone = lambda *args, **kwargs: True
            answers = self.mcp(tools, ("tap", "screen", "stop"))
        self.assertEqual(answers["tap"].text, plan_gate.NO_PLAN)
        self.assertEqual((answers["screen"], answers["stop"]), (None, None))
        self.assertEqual(self.asked, [])


if __name__ == "__main__":
    unittest.main()
