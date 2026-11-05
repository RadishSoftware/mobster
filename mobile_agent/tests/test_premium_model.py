import importlib.util

"""`premiumModel`, on and off: what it opens, and what it never touches."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .. import plan_gate, premium_model
from ..journal import Journal

ROOT = Path(__file__).resolve().parents[2]
ON = {premium_model.DEV_ENV: "1"}


class FlagTests(unittest.TestCase):
    def test_off_unless_a_source_checkout_asks(self):
        self.assertFalse(premium_model.enabled({}))
        self.assertFalse(premium_model.enabled({premium_model.DEV_ENV: "0"}))
        self.assertTrue(premium_model.enabled(ON))

    def test_the_frozen_agent_ignores_the_variable(self):
        with patch.object(premium_model.sys, "frozen", True, create=True):
            self.assertFalse(premium_model.enabled(ON))

    def test_the_constant_turns_it_on_everywhere(self):
        with patch.object(premium_model, "ON", True), patch.object(premium_model.sys, "frozen", True, create=True):
            self.assertTrue(premium_model.enabled({}))

    @unittest.skipUnless((ROOT / "desktop/src-tauri/src/premium_model.rs").is_file(),
                         "the app shell source is not part of this repository")
    def test_the_shell_and_the_agent_flip_together(self):
        shell = (ROOT / "desktop/src-tauri/src/premium_model.rs").read_text()
        found = re.search(r"^const ON: bool = (true|false);$", shell, re.M)
        self.assertIsNotNone(found)
        self.assertEqual(found.group(1) == "true", premium_model.ON)

    @unittest.skipUnless((ROOT / "desktop/src-tauri/src/premium_model.rs").is_file(),
                         "the app shell source is not part of this repository")
    def test_a_release_build_ignores_the_variable(self):
        # The one line that keeps MOBSTER_PREMIUM_MODEL out of a release; premium_model.rs tests the rule beside it.
        shell = (ROOT / "desktop/src-tauri/src/premium_model.rs").read_text()
        self.assertRegex(shell, r"enabled_for\(\s*ON,\s*cfg!\(debug_assertions\),")

    def test_nothing_new_reads_the_flag(self):
        """Every reader of the flag or the gate's off switch is named here, with why: anything else is a new one, and
        safety code (approvals, the journal, the device lock, the secret filter) must never become one."""
        readers = {
            "mobile_agent/premium_model.py": "the flag itself",
            "mobile_agent/genuine.py": "the agent's own seal check, which runs only with the flag on",
            "mobile_agent/plan_gate.py": "the plan gate it opens",
            "mobile_agent/devkit/ci.py": "asks the gate whether a CI token is needed",
            "mobile_agent/teams/service.py": "passes turned_off=False: the team's gate stays on",
            "desktop/src-tauri/src/premium_model.rs": "the flag itself",
            "desktop/src-tauri/src/main.rs": "declares it, and says when the signature check passed",
            "desktop/src-tauri/src/agent_access.rs": "what opens the app",
            "desktop/src-tauri/src/genuine.rs": "what a finished signature check does: a failed copy's agent stops",
            "desktop/src-tauri/src/account.rs": "the status it adds",
            "desktop/src-tauri/src/account/model.rs": "the status field",
        }
        word = re.compile(r"premium_?model|gate_turned_off|\bturned_off\b", re.I)
        found = set()
        for folder, suffix in (("mobile_agent", ".py"), ("desktop/src-tauri/src", ".rs")):
            for path in (ROOT / folder).rglob(f"*{suffix}"):
                name = path.relative_to(ROOT).as_posix()
                if "/tests/" in name or "/.venv/" in name or name.endswith("/tests.rs"):
                    continue
                if word.search(path.read_text(errors="replace")):
                    found.add(name)
        expected = {name for name in readers
                    if not name.startswith(("desktop/", "mobile_agent/teams/")) or (ROOT / name).is_file()}
        self.assertEqual(found, expected, "a new reader of premiumModel: name it here, and never in safety code")


class GateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.missing = Path(self.directory.name) / "missing.json"

    def gate(self, environ):
        with patch.dict(plan_gate.os.environ, environ, clear=True):
            return plan_gate.PlanGate(self.missing, keys=(), machine="m")

    def test_on_a_task_starts_with_no_account(self):
        gate = self.gate(ON)
        self.assertIsNone(gate.problem())
        self.assertIsNone(gate.refusal())
        gate.require()

    def test_off_a_task_with_no_account_is_refused_as_before(self):
        gate = self.gate({})
        self.assertEqual(gate.problem(), "no_account")
        self.assertEqual(gate.refusal(), plan_gate.NO_PLAN)

    def test_a_gate_built_to_stay_on_stays_on(self):
        with patch.dict(plan_gate.os.environ, ON, clear=True):
            gate = plan_gate.PlanGate(self.missing, keys=(), machine="m", turned_off=False)
        self.assertEqual(gate.problem(), "no_account")

    @unittest.skipUnless(importlib.util.find_spec("mobile_agent.teams"), "requires the app teams extension")
    def test_a_lapsed_team_stays_shut_with_the_flag_on(self):
        # teams/service.py's own check: the station's token alone, so the flag doesn't open a team's console.
        from ..teams.service import TeamService
        runtime = SimpleNamespace(journal=Journal(None), plan_gate=None)
        with patch.dict(plan_gate.os.environ, ON, clear=True):
            service = TeamService(runtime, keychain=SimpleNamespace(), keys=(), docs=SimpleNamespace(),
                                  account_file=self.missing, machine="m", poll=False)
            self.assertTrue(premium_model.enabled())
            self.assertFalse(service.gate.turned_off)
            self.assertEqual(service.gate.station_status()[0], "no_token")
            self.assertEqual(service.gate.problem(), "no_account")

    def test_the_frozen_agent_refuses_with_the_variable_set(self):
        with patch.object(premium_model.sys, "frozen", True, create=True):
            self.assertEqual(self.gate(ON).problem(), "no_account")


REFUSALS = plan_gate.CI_TOKEN_REFUSALS


def doctor(fix=False, apps=False):
    """`mobster doctor`'s parsed arguments: --fix and --apps are always there, False unless given."""
    return SimpleNamespace(fix=fix, apps=apps, json=True)


class CiTokenTests(unittest.TestCase):
    """With the flag on no run needs a CI token, and everything under one that's there refuses unless the license API
    vouched for it. Off, a CI token answers as before (test_ci_hosted)."""

    def setUp(self):
        plan_gate._ci_token_verdicts.clear()
        self.addCleanup(plan_gate._ci_token_verdicts.clear)

    def stop(self, command="ci", args=None, environ=None, answer="revoked"):
        return plan_gate.ci_token_stop(command, args, {**ON, plan_gate.CI_TOKEN_ENV: "tok", **(environ or {})},
                                       ask=lambda token: answer)

    def test_a_revoked_token_stops_every_command_under_it(self):
        run = SimpleNamespace(ci_command="run")
        for command, args in (("ci", run), ("test", None), ("verify", None), ("run", None), (plan_gate.TOOL, None),
                              ("serve", None), ("chat", None)):
            plan_gate._ci_token_verdicts.clear()
            self.assertEqual(self.stop(command, args), REFUSALS["revoked"], command)

    def test_the_mcp_server_starts_and_each_tool_refuses(self):
        environ = {**ON, plan_gate.CI_TOKEN_ENV: "tok"}
        with patch.dict(plan_gate.os.environ, environ, clear=True), \
                patch.object(plan_gate, "ci_token_answer", lambda token: "revoked"):
            self.assertIsNone(plan_gate.stopped("mcp"))
            self.assertEqual(plan_gate.stopped(plan_gate.TOOL), REFUSALS["revoked"])

    def test_what_says_whats_wrong_still_runs(self):
        for command, args in (("doctor", doctor()), ("ci", SimpleNamespace(ci_command="doctor")),
                              ("ci", SimpleNamespace(ci_command="doctor", hosted=True, json=True)),
                              ("ci", SimpleNamespace(ci_command="init"))):
            for answer in ("revoked", None):
                plan_gate._ci_token_verdicts.clear()
                self.assertIsNone(self.stop(command, args, answer=answer), command)

    def test_the_doctor_that_acts_stops(self):
        # --fix installs the phone's helper and stops what holds Mobster's ports; --apps opens every app on the phone.
        for args in (doctor(fix=True), doctor(apps=True), doctor(fix=True, apps=True), None,
                     SimpleNamespace(json=True)):  # a flag it can't read counts as given
            for answer in ("revoked", None):
                plan_gate._ci_token_verdicts.clear()
                self.assertEqual(self.stop("doctor", args, answer=answer), REFUSALS[answer or "unchecked"], args)

    def test_the_check_is_asked_even_while_ci_tokens_are_dark(self):
        self.assertFalse(plan_gate.ci_tokens_enabled({**ON}))
        self.assertEqual(self.stop("verify"), REFUSALS["revoked"])

    def test_no_token_needs_no_check(self):
        # A free path asks nothing, so being offline costs it nothing.
        def never(token):
            raise AssertionError("asked with no token")
        self.assertIsNone(plan_gate.ci_token_stop("run", None, dict(ON), ask=never))
        self.assertIsNone(plan_gate.ci_token_stop("run", None, {**ON, plan_gate.CI_TOKEN_ENV: "  "}, ask=never))

    def test_a_token_the_license_api_vouched_for_goes_ahead(self):
        self.assertIsNone(self.stop(answer="ok"))

    def test_a_verdict_no_sentence_names_refuses_as_unchecked(self):
        self.assertEqual(self.stop(answer="nobody-maps-this"), REFUSALS["unchecked"])
        for verdict in ("nobody-maps-this", "", "OK"):
            self.assertEqual(plan_gate.ci_token_refusal(verdict), REFUSALS["unchecked"], verdict)
        self.assertEqual((plan_gate.ci_token_refusal(None), plan_gate.ci_token_refusal("ok")), (None, None))

    def test_no_answer_from_the_license_api_refuses(self):
        self.assertEqual(self.stop(answer=None), REFUSALS["unchecked"])
        self.assertIn("network", REFUSALS["unchecked"])
        self.assertIn(plan_gate.CI_TOKEN_ENV, REFUSALS["unchecked"])

    def test_every_refusal_from_the_license_api_refuses_with_its_own_sentence(self):
        for answer in ("revoked", "expired", "plan_required"):
            plan_gate._ci_token_verdicts.clear()
            self.assertEqual(self.stop(answer=answer), REFUSALS[answer], answer)
        self.assertEqual(len(set(REFUSALS.values())), len(REFUSALS))

    def test_the_license_apis_answers(self):
        from .. import share
        for status, value, answer in ((200, {}, "ok"), (403, {"error": "expired"}, "expired"),
                                      (403, {"error": "plan_required"}, "plan_required"), (401, {}, "revoked"),
                                      (403, {"error": "something new"}, "revoked"), (429, {}, None), (500, {}, None),
                                      (503, {}, None)):
            with patch.object(share, "call", return_value=(status, value)):
                self.assertEqual(plan_gate.ci_token_answer("tok"), answer, status)
        with patch.object(share, "call", side_effect=share.ShareError("unreachable", 503, "x")):
            self.assertIsNone(plan_gate.ci_token_answer("tok"))

    def test_with_the_flag_off_it_asks_nothing(self):
        def never(token):
            raise AssertionError("asked with the flag off")
        self.assertIsNone(plan_gate.ci_token_stop("run", None, {plan_gate.CI_TOKEN_ENV: "tok"}, ask=never))

    def test_a_token_no_header_can_carry_refuses_cleanly(self):
        def unsendable(token):
            raise ValueError("Invalid header value")
        self.assertEqual(plan_gate.ci_token_stop("run", None, {**ON, plan_gate.CI_TOKEN_ENV: "bad\ntok"},
                                                 ask=unsendable), REFUSALS["unsendable"])

    def test_only_an_unsendable_token_is_caught(self):
        # Any other fault propagates, and leaves no verdict behind to pass for an answer.
        def broken(token):
            raise RuntimeError("a fault, not an unsendable token")
        with self.assertRaises(RuntimeError):
            plan_gate.ci_token_verdict({**ON, plan_gate.CI_TOKEN_ENV: "tok"}, broken)
        self.assertEqual(plan_gate._ci_token_verdicts, {})

    def verdicts(self, answers):
        asked, clock = [], [0.0]

        def ask(token):
            asked.append(token)
            return answers[len(asked) - 1]
        environ = {**ON, plan_gate.CI_TOKEN_ENV: "tok"}
        return (lambda: plan_gate.ci_token_verdict(environ, ask, clock=lambda: clock[0])), clock, asked

    def test_a_long_lived_process_asks_again_and_a_revocation_stays(self):
        read, clock, asked = self.verdicts(["ok", "revoked", "ok"])
        self.assertEqual(read(), "ok")
        clock[0] = 10
        self.assertEqual(read(), "ok")
        self.assertEqual(len(asked), 1)
        clock[0] = plan_gate.CI_TOKEN_STANDS["ok"] + 1
        self.assertEqual(read(), "revoked")
        clock[0] *= 10
        self.assertEqual(read(), "revoked")
        self.assertEqual(len(asked), 2)

    def test_every_refusal_stands_for_the_life_of_the_process(self):
        for refusal in ("revoked", "expired", "plan_required"):
            plan_gate._ci_token_verdicts.clear()
            read, clock, asked = self.verdicts([refusal, "ok"])
            self.assertEqual(read(), refusal)
            clock[0] = 10 ** 9
            self.assertEqual(read(), refusal)
            self.assertEqual(len(asked), 1, refusal)

    def test_a_token_checked_within_5_minutes_stays_valid_and_then_needs_an_answer(self):
        self.assertEqual(plan_gate.CI_TOKEN_STANDS["ok"], 300)
        read, clock, asked = self.verdicts(["ok", None])
        self.assertEqual(read(), "ok")
        clock[0] = 299
        self.assertEqual(read(), "ok")
        self.assertEqual(len(asked), 1)
        clock[0] = 301
        self.assertEqual(read(), "unchecked")
        self.assertEqual(len(asked), 2)

    def test_an_unanswered_check_is_asked_again_after_30_s_and_not_before(self):
        self.assertEqual(plan_gate.CI_TOKEN_STANDS["unchecked"], 30)
        read, clock, asked = self.verdicts([None, "ok"])
        self.assertEqual(read(), "unchecked")
        clock[0] = 29
        self.assertEqual(read(), "unchecked")
        self.assertEqual(len(asked), 1)
        clock[0] = 31
        self.assertEqual(read(), "ok")
        self.assertEqual(len(asked), 2)

    def test_a_refusal_another_call_heard_first_stands(self):
        def ask(token):
            plan_gate._ci_token_verdicts[token] = (0.0, "revoked")  # another tool call's answer lands meanwhile
            return "ok"
        environ = {**ON, plan_gate.CI_TOKEN_ENV: "tok"}
        self.assertEqual(plan_gate.ci_token_verdict(environ, ask, clock=lambda: 0.0), "revoked")
        self.assertEqual(plan_gate._ci_token_verdicts["tok"][1], "revoked")

    def test_a_hosted_runners_doctor_says_what_each_answer_means(self):
        from ..devkit import ci
        environ = {**ON, plan_gate.CI_TOKEN_ENV: "tok"}
        for answer, status, detail in (("ok", "ok", "checked with Mobster"), ("revoked", "fail", "revoked"),
                                       ("expired", "fail", "expired"), ("plan_required", "fail", "plan inactive"),
                                       (None, "fail", "couldn't be checked"),
                                       ("nobody-maps-this", "fail", "nobody-maps-this")):
            plan_gate._ci_token_verdicts.clear()
            with patch.object(plan_gate, "ci_token_answer", lambda token, answer=answer: answer):
                row = ci.hosted_token_row(environ)
            fix = "" if answer == "ok" else REFUSALS.get(answer or "unchecked", REFUSALS["unchecked"])
            self.assertEqual((row.status, row.detail, row.fix), (status, detail, fix), answer)
        self.assertEqual(ci.hosted_token_row(dict(ON)).status, "skip")
        # Off, it's today's row: a token is needed, and CI tokens being dark says so.
        self.assertEqual(ci.hosted_token_row({}).status, "fail")

    def real_gate(self, environ, **kwargs):
        with tempfile.TemporaryDirectory() as folder, patch.dict(plan_gate.os.environ, environ, clear=True):
            return plan_gate.PlanGate(Path(folder) / "account.json", keys=(), machine="m", **kwargs)

    def cli(self, command, args, environ, gate, answer="revoked"):
        from .. import __main__ as main_cli
        with patch.dict(plan_gate.os.environ, environ, clear=True), \
                patch.object(plan_gate, "bundled_gate", return_value=gate), \
                patch.object(plan_gate, "ci_token_answer", lambda token: answer), \
                patch("builtins.print") as printed:
            return main_cli.plan_refusal(args, command), printed, main_cli

    def test_the_shipped_cli_with_a_real_gate_refuses_and_says_so(self):
        # The frozen agent has a real gate, so this path is the one that ships.
        environ, gate = {**ON, plan_gate.CI_TOKEN_ENV: "tok"}, self.real_gate(ON)
        for answer in ("revoked", None):
            for command, args in (("ci", SimpleNamespace(ci_command="run", device=None, testflight=None, json=True)),
                                  ("verify", SimpleNamespace(device=None, testflight=None, json=True)),
                                  ("run", SimpleNamespace(demo=False, json=True)),
                                  ("screen", SimpleNamespace(json=True))):
                plan_gate._ci_token_verdicts.clear()
                code, printed, main_cli = self.cli(command, args, environ, gate, answer)
                self.assertEqual(code, main_cli.EXIT_COULDNT, command)
                self.assertEqual(json.loads(printed.call_args[0][0])["error"], REFUSALS[answer or "unchecked"])

    def test_the_shipped_cli_still_runs_what_says_whats_wrong(self):
        environ, gate = {**ON, plan_gate.CI_TOKEN_ENV: "tok"}, self.real_gate(ON)
        for command, args in (("doctor", doctor()),
                              ("ci", SimpleNamespace(ci_command="doctor", json=True)),
                              ("ci", SimpleNamespace(ci_command="init", json=True))):
            for answer in ("revoked", None):
                plan_gate._ci_token_verdicts.clear()
                self.assertIsNone(self.cli(command, args, environ, gate, answer)[0], command)

    def test_the_shipped_cli_stops_the_doctor_that_acts(self):
        environ, gate = {**ON, plan_gate.CI_TOKEN_ENV: "tok"}, self.real_gate(ON)
        for args in (doctor(fix=True), doctor(apps=True)):
            for answer in ("revoked", None, "nobody-maps-this"):
                plan_gate._ci_token_verdicts.clear()
                code, printed, main_cli = self.cli("doctor", args, environ, gate, answer)
                self.assertEqual(code, main_cli.EXIT_COULDNT, args)
                self.assertEqual(json.loads(printed.call_args[0][0])["error"],
                                 REFUSALS["revoked" if answer == "revoked" else "unchecked"])
        # With the license API's word, or no token, it runs as before.
        for environ, answer in (({**ON, plan_gate.CI_TOKEN_ENV: "tok"}, "ok"), (dict(ON), "revoked")):
            plan_gate._ci_token_verdicts.clear()
            self.assertIsNone(self.cli("doctor", doctor(fix=True), environ, gate, answer)[0])

    def test_the_shipped_cli_goes_ahead_with_no_token_or_a_vouched_one(self):
        gate = self.real_gate(ON)
        args = SimpleNamespace(ci_command="run", device=None, testflight=None, json=True)
        self.assertIsNone(self.cli("ci", args, dict(ON), gate)[0])
        self.assertIsNone(self.cli("ci", args, {**ON, plan_gate.CI_TOKEN_ENV: "tok"}, gate, "ok")[0])

    def test_with_the_flag_off_the_cli_answers_by_the_plan_as_before(self):
        gate = self.real_gate({}, turned_off=False)
        args = SimpleNamespace(demo=False, json=True)
        code, printed, main_cli = self.cli("run", args, {plan_gate.CI_TOKEN_ENV: "tok"}, gate)
        self.assertEqual(code, main_cli.EXIT_COULDNT)
        self.assertEqual(json.loads(printed.call_args[0][0])["error_type"], "plan_required")
        self.assertNotIn("revoked", printed.call_args[0][0])

    def tools(self, gate=None):
        from ..mcp_server.tools import ToolSet
        tools = object.__new__(ToolSet)
        tools.plan_gate = gate
        return tools

    def mcp(self, tools, environ, answer, names=("tap", "screen", "read_notifications", "listen", "status", "stop")):
        from ..mcp_server.catalog import ANNOTATIONS
        from ..mcp_server.protocol import ACTS
        with patch.dict(plan_gate.os.environ, environ, clear=True), \
                patch.object(plan_gate, "ci_token_answer", lambda token: answer):
            return {name: tools.plan_refusal(name, ANNOTATIONS.get(name, ACTS)) for name in names}

    def assert_only_stop_answers(self, answers, sentence):
        for name, refused in answers.items():
            if name == "stop":
                self.assertIsNone(refused)
            else:
                self.assertTrue(refused.is_error, name)
                self.assertEqual(refused.text, sentence, name)

    def test_every_mcp_tool_but_stop_stops_reads_included(self):
        from ..mcp_server.catalog import ANNOTATIONS
        for name in ("screen", "read_notifications", "listen"):
            self.assertTrue(ANNOTATIONS[name]["readOnlyHint"], name)
        self.assert_only_stop_answers(self.mcp(self.tools(), {**ON, plan_gate.CI_TOKEN_ENV: "tok"}, "revoked"),
                                      REFUSALS["revoked"])

    def test_the_shipped_mcp_server_with_a_real_gate_stops_every_tool_but_stop(self):
        # The frozen agent's MCP server holds a real gate, so this path is the one that ships.
        tools = self.tools(self.real_gate(ON))
        for answer in ("revoked", None):
            plan_gate._ci_token_verdicts.clear()
            self.assert_only_stop_answers(self.mcp(tools, {**ON, plan_gate.CI_TOKEN_ENV: "tok"}, answer),
                                          REFUSALS[answer or "unchecked"])

    def test_the_shipped_mcp_server_with_a_real_gate_goes_ahead_with_no_token_or_a_vouched_one(self):
        tools = self.tools(self.real_gate(ON))
        for environ, answer in ((dict(ON), "revoked"), ({**ON, plan_gate.CI_TOKEN_ENV: "tok"}, "ok")):
            self.assertEqual(set(self.mcp(tools, environ, answer).values()), {None})

    def test_the_shipped_mcp_server_with_the_flag_off_answers_by_the_plan_as_before(self):
        tools = self.tools(self.real_gate({}, turned_off=False))
        tools._drives_real_phone = lambda *args, **kwargs: True
        answers = self.mcp(tools, {plan_gate.CI_TOKEN_ENV: "tok"}, "revoked", names=("tap", "screen", "stop"))
        self.assertTrue(answers["tap"].is_error)
        self.assertNotIn("revoked", answers["tap"].text)
        self.assertIsNone(answers["screen"])
        self.assertIsNone(answers["stop"])

    def test_an_mcp_tool_goes_ahead_with_the_flag_off_or_no_token(self):
        def never(token):
            raise AssertionError("asked")
        tools = self.tools()
        with patch.object(plan_gate, "ci_token_answer", never):
            with patch.dict(plan_gate.os.environ, {plan_gate.CI_TOKEN_ENV: "tok"}, clear=True):
                self.assertIsNone(tools.plan_refusal("tap", {}))
            with patch.dict(plan_gate.os.environ, dict(ON), clear=True):
                self.assertIsNone(tools.plan_refusal("tap", {}))
                self.assertIsNone(tools.plan_refusal("screen", {"readOnlyHint": True}))


if __name__ == "__main__":
    unittest.main()
