import argparse
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import chat_here
from mobile_agent.api_errors import APIError
from mobile_agent.cli_run import NoDevice
from mobile_agent.threads import cli as served_chat


class LocalChatTests(unittest.TestCase):
    def args(self):
        return SimpleNamespace(message=["Read my calendar"], json=True, thread=None, device=None)

    def run_local(self, args, session):
        out, err = io.StringIO(), io.StringIO()
        with patch("mobile_agent.engines.smart_key", return_value="scripted"), \
                patch("mobile_agent.tui.session.Session", return_value=session) as opened, \
                redirect_stdout(out), redirect_stderr(err):
            code = chat_here.run_here(args, "Read my calendar", args.json)
        return code, json.loads(out.getvalue()), err.getvalue(), opened

    def test_an_unknown_thread_or_no_conversations_reports_one_json_error_and_sends_nothing(self):
        gone = Mock()
        gone.threads.return_value.thread.side_effect = APIError("That conversation was deleted.", 404,
                                                               "thread_not_found")
        off = Mock()
        off.threads.return_value = None
        for session, thread, error in ((gone, "a" * 12, "That conversation was deleted."),
                                       (off, None, chat_here.NOT_RUNNING_HERE)):
            with self.subTest(error=error):
                args = self.args()
                args.thread = thread
                code, data, err, _ = self.run_local(args, session)
                self.assertEqual((code, data["status"], data["error"], err), (3, "couldnt_run", error, ""))
                session.send.assert_not_called()
                session.close.assert_called_once()

    def test_a_named_phone_sets_the_local_sessions_address(self):
        args = self.args()
        args.device = "Studio iPhone"
        with patch.object(chat_here, "service_running", return_value=False), \
                patch("mobile_agent.cli_run.device_target", return_value={"wdaUrl": "http://127.0.0.1:8103"}), \
                patch.object(chat_here, "run_here", return_value=0) as run:
            self.assertEqual(chat_here.maybe_here(args), 0)
        self.assertEqual(run.call_args.args[0].wda_url, "http://127.0.0.1:8103")
        session = Mock()
        session.threads.return_value = None
        _, _, _, opened = self.run_local(args, session)
        opened.assert_called_once_with(wda_url="http://127.0.0.1:8103", env_file=None, origin="cli")

    def test_an_unknown_phone_does_not_fall_back_to_the_default_phone(self):
        args = self.args()
        args.device = "Missing iPhone"
        out = io.StringIO()
        with patch.object(chat_here, "service_running", return_value=False), \
                patch("mobile_agent.cli_run.device_target", side_effect=NoDevice("No device has that name.")), \
                patch.object(chat_here, "run_here") as run, redirect_stdout(out):
            self.assertEqual(chat_here.maybe_here(args), 3)
        self.assertEqual(json.loads(out.getvalue())["code"], "no_device")
        run.assert_not_called()

    def test_a_named_phone_is_kept_when_chat_opens_the_terminal_ui(self):
        args = self.args()
        args.message, args.json, args.device = [], False, "Studio iPhone"
        terminal = SimpleNamespace(isatty=lambda: True)
        with patch.object(chat_here, "service_running", return_value=False), \
                patch("mobile_agent.cli_run.device_target", return_value={"wdaUrl": "http://127.0.0.1:8103"}), \
                patch("mobile_agent.tui.main", return_value=0) as tui, \
                patch.object(chat_here.sys, "stdin", terminal), patch.object(chat_here.sys, "stdout", terminal):
            self.assertEqual(chat_here.maybe_here(args), 0)
        self.assertEqual(tui.call_args.args[0].wda_url, "http://127.0.0.1:8103")

    def parsed(self, flags):
        parser = argparse.ArgumentParser()
        served_chat.add_arguments(parser, {})
        return parser.parse_args(["--new", "--json", *flags, "Read my calendar"])

    def test_standalone_chat_rejects_explicit_wait_flags_before_starting_a_task(self):
        for flags in (["--wait", "0"], ["--wait", "300"], ["--keep-waiting"]):
            with self.subTest(flags=flags):
                args, out = self.parsed(flags), io.StringIO()
                with patch.object(chat_here, "service_running", return_value=False), \
                        patch.object(chat_here, "run_here") as run, redirect_stdout(out):
                    self.assertEqual(chat_here.maybe_here(args), 3)
                self.assertIn("need the Mobster app", json.loads(out.getvalue())["error"])
                run.assert_not_called()

    def test_a_served_runtime_keeps_wait_flags_and_the_default_wait(self):
        for flags, wait in (([], 300.0), (["--wait", "300"], 300.0),
                            (["--wait", "0", "--keep-waiting"], 0.0)):
            with self.subTest(flags=flags):
                args = self.parsed(flags)
                with patch.object(chat_here, "service_running", return_value=True):
                    self.assertIsNone(chat_here.maybe_here(args))
                client = Mock(has_threads=True)
                with patch.object(served_chat, "once", return_value=0) as once:
                    self.assertEqual(served_chat.run(args, client=client, stdin=io.StringIO(),
                                                     stdout=io.StringIO(), stderr=io.StringIO()), 0)
                self.assertEqual(once.call_args.kwargs["wait"], wait)
                self.assertEqual(once.call_args.kwargs["keep"], bool(args.keep_waiting))


if __name__ == "__main__":
    unittest.main()
