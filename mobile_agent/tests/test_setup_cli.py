import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from mobile_agent import setup_cli
from mobile_agent.style import SPINNER, Palette

STATE = {"steps": [
    {"id": "tools", "title": "Install the tools", "state": "done"},
    {"id": "api_key", "title": "Add your key", "state": "todo", "detail": "Missing."},
    {"id": "phone", "title": "Plug in your iPhone", "state": "working", "detail": "Waiting for it."},
    {"id": "video", "title": "Not shown here", "state": "todo"},
]}


class RenderTests(unittest.TestCase):
    def test_the_checklist_counts_done_steps_and_shows_only_the_terminals_steps(self):
        lines = setup_cli.render(STATE, Palette(False), frame=3)
        self.assertIn("1 of 3 done", lines[0])
        text = "\n".join(lines)
        self.assertIn("✓ 1. Install the tools", text)
        self.assertIn(f"{SPINNER[3]} 3. Plug in your iPhone", text)
        self.assertIn("Run `mobster login` in another terminal", text)  # not the service's "Missing."
        self.assertNotIn("Not shown here", text)


class Terminal(io.StringIO):
    def isatty(self):
        return True


class RunTests(unittest.TestCase):
    def run_with(self, states):
        runtime = self.runtime = mock.Mock()
        runtime.setup.state.side_effect = states
        args = mock.Mock(json=True, device=None)
        with mock.patch.object(setup_cli, "open_runtime", return_value=runtime), \
                mock.patch.object(setup_cli.time, "sleep") as sleep, redirect_stdout(io.StringIO()) as out:
            code = setup_cli.run(args)
        return code, out.getvalue(), sleep

    def test_a_failed_build_ends_the_run_with_1(self):
        failed = {"steps": STATE["steps"], "build": {"state": "failed", "error": "Xcode said no."}}
        code, out, sleep = self.run_with([failed])
        self.assertEqual(code, 1)
        sleep.assert_not_called()

    ASKING = {"steps": [{"id": "phone", "state": "done"}, {"id": "live", "state": "done"}],
              "device": {"udid": "U", "name": "Sam's iPhone"}, "wifi": {"state": "asking"}}

    def test_with_no_terminal_wifi_is_never_turned_on_and_the_yes_to_give_is_named(self):
        code, out, sleep = self.run_with([self.ASKING])
        self.assertEqual(code, 1)
        waiting = json.loads(out.splitlines()[-1])
        self.assertEqual(waiting["waiting"], "wifi")
        self.assertIn("run mobster setup on a terminal to answer", waiting["hint"])
        self.assertIn('mobster wifi on --device "Sam\'s iPhone"', waiting["hint"])
        self.runtime.setup.answer_wifi.assert_not_called()

    def test_on_a_terminal_it_asks_and_enter_means_no(self):
        ready = {"steps": [{"id": name, "state": "done"} for name in ("phone", "wda_build", "wda_running", "live")]}
        for typed, yes in (("", False), ("n", False), ("y", True)):
            with self.subTest(typed=typed):
                runtime = mock.Mock()
                runtime.setup.state.side_effect = [self.ASKING, ready]
                with mock.patch.object(setup_cli, "open_runtime", return_value=runtime), \
                        mock.patch.object(setup_cli.time, "sleep"), \
                        mock.patch("builtins.input", return_value=typed) as asked, \
                        mock.patch.object(setup_cli, "_width", return_value=80), \
                        redirect_stdout(Terminal()):
                    code = setup_cli.run(mock.Mock(json=False, device=None))
                self.assertEqual(code, 0)
                self.assertIn("[y/N]", asked.call_args.args[0])
                runtime.setup.answer_wifi.assert_called_once_with(self.ASKING["device"], yes)

    def test_a_wifi_pairing_under_way_ends_before_setup_does(self):
        ready = {"steps": [{"id": name, "state": "done"} for name in ("phone", "wda_build", "wda_running", "live")]}
        code, out, sleep = self.run_with(
            [{**ready, "wifi": {"state": "pairing"}}, {**ready, "wifi": {"state": "paired"}}])
        self.assertEqual(code, 0)
        sleep.assert_called_once_with(setup_cli.POLL)

    def test_the_loop_waits_one_poll_between_reads(self):
        pending = {"steps": [{"id": "phone", "state": "working"}]}
        ready = {"steps": [{"id": name, "state": "done"} for name in ("phone", "wda_build", "wda_running", "live")]}
        code, out, sleep = self.run_with([pending, ready])
        self.assertEqual(code, 0)
        sleep.assert_called_once_with(setup_cli.POLL)


if __name__ == "__main__":
    unittest.main()
