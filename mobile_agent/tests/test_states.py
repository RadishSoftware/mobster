"""The table of states (states.py): one row per cause, the fix each carries, and every surface saying a row in the same
words. Offline: fakes for the phone, the service and the clock."""

import inspect
import re
import unittest

from mobile_agent import doctor, doctor_fix, runner_hints, states
from mobile_agent import engine_health as eh
from mobile_agent.tests.test_mcp_tools import ToolsHarness

BANNED = re.compile(r"\b(simply|just|easily|seamlessly|powerful|robust|crucial|delve|pivotal|showcase)\b|[—·]",
                    re.IGNORECASE)


class TableTests(unittest.TestCase):
    def test_a_cause_is_one_row_of_a_known_state(self):
        self.assertEqual(len(states.BY_CAUSE), len(states.ROWS))
        for row in states.ROWS:
            self.assertIn(row.state, states.STATES, row.cause)
            self.assertNotEqual(row.state, states.READY, row.cause)

    def test_a_pair_the_table_does_not_have_is_an_error_not_a_default(self):
        with self.assertRaises(KeyError):
            states.words(states.HELPER_DOWN, "locked")
        with self.assertRaises(KeyError):
            doctor.finding("x", "X", doctor.WARN, "not_a_cause")

    def test_every_row_says_what_happened_and_what_to_do_in_the_writing_rules(self):
        for row in states.ROWS:
            message, step, _need = states.words(row.state, row.cause, "Studio iPhone")
            for sentence in (message, step):
                self.assertTrue(sentence.endswith("."), (row.cause, sentence))
                self.assertNotIn("{", sentence, row.cause)
                self.assertIsNone(BANNED.search(sentence), (row.cause, sentence))
        self.assertEqual(states.words(states.WAITING_FOR_PHONE, "locked", "sam")[0], "Sam is locked.")

    def test_every_fix_has_something_to_run_and_every_action_a_row(self):
        used, commands = set(), set()
        for row in states.ROWS:
            match row.fix:
                case states.Auto(action) | states.Ask(action):
                    used.add(action)
                case states.Guided(command):
                    commands.add(command)
                case states.Run(command):
                    self.assertTrue(command, row.cause)
                case states.Wait(why):
                    self.assertTrue(why.endswith("."), row.cause)
        self.assertEqual(used, set(doctor_fix.ACTIONS))
        self.assertEqual(set(doctor_fix.ORDER), used)
        self.assertEqual(commands, {"mobster login", "mobster setup"})   # the flows doctor_fix.guided runs

    def test_a_picture_names_the_screen_it_shows_once(self):
        pictures = [row.fix.picture for row in states.ROWS if isinstance(row.fix, states.Yours) and row.fix.picture]
        self.assertEqual(len(pictures), len(set(pictures)))
        for name in pictures:
            self.assertRegex(name, r"^[a-z]+(-[a-z]+)*$")
        # Every state the person fixes with a tap on the iPhone's own screen has its picture.
        taps = {"developer_mode_off", "trust_needed", "ui_automation_off", "computer_untrusted"}
        self.assertEqual({row.cause for row in states.ROWS if getattr(row.fix, "picture", None)}, taps)

    def test_the_fix_a_client_reads(self):
        self.assertEqual(states.fix_public(states.row("stopped").fix), {"kind": "auto", "action": "start_helper"})
        self.assertEqual(states.fix_public(states.row("port_taken").fix), {"kind": "ask", "action": "free_port"})
        self.assertEqual(states.fix_public(states.row("no_key").fix), {"kind": "guided", "command": "mobster login"})
        self.assertEqual(states.fix_public(states.row("developer_mode_off").fix),
                         {"kind": "yours", "picture": "developer-mode"})
        self.assertEqual(states.fix_public(states.row("locked").fix), {"kind": "yours"})
        self.assertEqual(states.fix_public(states.row("restarted").fix)["kind"], "wait")
        for row in states.ROWS:
            self.assertTrue(states.how(row.fix), row.cause)


class EmittedTests(unittest.TestCase):
    """A row marked for the app is one the engine says, and nothing the engine says is missing from the table."""

    def emitted(self):
        names = {name for name in dir(states) if name.isupper() and getattr(states, name) in states.STATES}
        names.discard("READY")
        named = {cause for name, cause in re.findall(r'\b([A-Z][A-Z_]+), "([a-z_]+)"', inspect.getsource(eh))
                 if name in names}
        # Reached by name, not as a literal pair: the model's three causes, the helper log's and a task's wait.
        named |= {eh.classify_model(False, "x", ("credit",), has_key=False)[1],
                  eh.classify_model(False, "credit", ("credit",))[1],
                  eh.classify_model(False, "x", ("credit",))[1]}
        named |= {cause for _needle, cause, _hint in runner_hints.RUNNER_HINTS if cause}
        named |= set(eh.TASK_WAIT_CAUSES.values())
        return named

    def test_the_engine_says_exactly_the_rows_marked_app(self):
        marked = {row.cause for row in states.ROWS if "app" in row.surfaces}
        self.assertEqual(self.emitted(), marked)

    def test_every_phone_row_marked_app_can_come_from_the_phone(self):
        for row in states.ROWS:
            if row.state == states.WAITING_FOR_PHONE and "app" in row.surfaces:
                self.assertIn(row.cause, eh.PHONE_CAUSES)
        self.assertNotIn("computer_untrusted", eh.PHONE_CAUSES)


class OneVoiceTests(ToolsHarness):
    """Every state in the table reads the same on each surface that shows it: the engine's snapshot (the Mac app and
    `mobster serve`), `mobster mcp`'s status and `mobster doctor`."""

    def test_the_app_mcp_status_and_doctor_say_a_row_in_the_same_words(self):
        tools = self.make()
        seen = set()
        for row in states.ROWS:
            if "app" not in row.surfaces and "cli" not in row.surfaces:
                continue
            message, step, need = states.words(row.state, row.cause, "Sam")
            if "app" in row.surfaces:
                health = eh.EngineHealth(clock=lambda: 5.0)
                health.report("phone:primary", row.state, row.cause, phone="Sam")
                snapshot = health.snapshot()
                tools._engine_source = lambda snapshot=snapshot: snapshot
                status = self.call("status")
                issue = status.structured["engine"]
                self.assertEqual((issue["message"], issue["next_step"]), (message, step), row.cause)
                self.assertEqual(issue["fix"], states.fix_public(row.fix), row.cause)
                self.assertIn(f"Mobster app: {message} {step}", status.text, row.cause)
                seen.add("mcp")
            if "cli" in row.surfaces:
                check = doctor.finding("x", "X", doctor.WARN, row.cause, phone="Sam")
                self.assertTrue(check.detail.startswith(message), (row.cause, check.detail))
                self.assertEqual(check.fix.split("\n")[0], step, row.cause)
                self.assertEqual(check.public()["action"], states.fix_public(row.fix), row.cause)
                seen.add("cli")
        self.assertEqual(seen, {"mcp", "cli"})

    def test_the_asker_names_a_silent_service_in_the_table_words(self):
        for age, refused, cause in ((4.0, False, "slow_answer"), (9.0, False, "no_answer"), (0.0, True, "not_running")):
            said = eh.from_silence(age, refused=refused)
            row = states.row(cause)
            self.assertEqual((said["message"], said["next_step"], said["fix"]),
                             (row.message, row.step, states.fix_public(row.fix)))

    def test_a_task_waiting_on_the_phone_passes_on_the_rows_next_step(self):
        for kind, cause in eh.TASK_WAIT_CAUSES.items():
            need, line = eh.task_wait(kind, "Sam")
            _message, step, row_need = states.words(states.WAITING_FOR_PHONE, cause, "Sam")
            self.assertEqual((need, line), (row_need, step))


if __name__ == "__main__":
    unittest.main()
