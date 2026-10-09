"""`mobster chat` against a real HTTP server on port 0 (the real handler and Runtime, a scratch HOME, never the app on
8765): one message and its answer, approvals shown with where to answer them, [y] only without the Mac app's session
and only on a terminal, --json and no terminal returning waiting_for_approval with exit 2, questions, steering, and
the conversation this terminal used last. Offline: scripted model and phone."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import pty
import re
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import __main__ as main_cli
from mobile_agent.extensions import Hooks
from mobile_agent.server import BoundedServer, Runtime, make_handler
from mobile_agent.tests.test_conversations_support import ThreadsBase, clarify
from mobile_agent.tests.test_engines import COMPOSE, Script, item, sent
from mobile_agent.tests.test_memory_api import GYM, REMEMBER, scratch_memory
from mobile_agent.tests.test_server_engine import jpeg
from mobile_agent.threads import cli as chat
from mobile_agent.threads.client import Client, NOT_RUNNING

TOKEN = "t" * 43
SESSION = "s" * 43
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def args(*message, thread=None, new=False, device=None, json_out=False, wait=20.0, url=None):
    return SimpleNamespace(message=list(message), thread=thread, new=new, device=device, json=json_out, wait=wait,
                           url=url)


class Served(ThreadsBase):
    """A Runtime behind a real HTTP server. Each task someone starts runs on its own worker thread with the next
    queued script (``scripted``), one at a time, or is only admitted when none is queued."""

    def setUp(self):
        super().setUp()
        self.scripts, self.phones = [], {}
        self.worker.stop()
        real = Runtime.work

        def work(runtime, run):
            if not self.scripts:
                return
            script, screens = self.scripts.pop(0)
            phone = self.Phone(screens or [COMPOSE] * 8)
            self.phones[run.id] = phone
            frames = iter([jpeg()] * 50)
            with patch("mobile_agent.server.build_target_driver", return_value=phone), \
                    patch("mobile_agent.server.prepare_wda_phone"), \
                    patch("mobile_agent.engines.build_client", return_value=script), \
                    patch("mobile_agent.frontier.video_frame", side_effect=lambda *a, **k: next(frames, None)):
                real(runtime, run)
            if run.lease:
                run.lease.close()
                run.lease = None
        self.worker = patch.object(Runtime, "work", work)
        self.worker.start()
        self.addCleanup(self.worker.stop)

    def serve(self, app_session=None):
        runtime = self.runtime()
        server = BoundedServer(("127.0.0.1", 0), make_handler(runtime, TOKEN, app_session=app_session))
        runtime.config.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"
        return runtime

    def client(self):
        return Client.connect(self.url, origin="cli", tokens=[TOKEN])

    def scripted(self, script, screens=None):
        """The next task someone starts runs with ``script``."""
        self.scripts.append((script, screens))

    def last_run(self, runtime):
        return max(runtime.runs.values(), key=lambda r: r.created_at)

    def wait_done(self, run, timeout=10):
        deadline = time.monotonic() + timeout
        while run.finished_at is None and time.monotonic() < deadline:
            time.sleep(.01)

    def terminal(self):
        master, slave = pty.openpty()
        stdin = io.open(slave, "r", closefd=False)
        stdout = io.open(os.dup(slave), "w")
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        self.addCleanup(stdout.close)
        seen = []

        def drain():
            while True:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    return
                if not data:
                    return
                seen.append(data.decode("utf-8", "replace"))
        threading.Thread(target=drain, daemon=True).start()
        return master, stdin, stdout, seen

    def until(self, seen, text, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in ANSI.sub("", "".join(seen)):
                return
            time.sleep(.02)
        self.fail(f"never saw {text!r} in {ANSI.sub('', ''.join(seen))!r}")

    def chat(self, argv_args, client=None, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        code = chat.run(argv_args, stdin=stdin or io.StringIO(""), stdout=out, stderr=err,
                        client=client or self.client())
        return code, out.getvalue(), err.getvalue()


class OneMessageTests(Served):
    def test_nothing_running_says_how_to_start_mobster(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(main_cli, "load_extensions", return_value=Hooks()), redirect_stdout(out), \
                redirect_stderr(err):
            code = main_cli.main(["chat", "What's on my calendar tomorrow?", "--url", f"http://127.0.0.1:{port}"])
        self.assertEqual((code, err.getvalue().strip(), out.getvalue()), (3, NOT_RUNNING, ""))
        with patch.object(main_cli, "load_extensions", return_value=Hooks()), redirect_stdout(out), \
                redirect_stderr(err):
            code = main_cli.main(["chat", "Hi", "--json", "--url", f"http://127.0.0.1:{port}"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out.getvalue())["code"], "not_running")
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(main_cli.main(["chat", "Hi", "--url", "http://192.168.1.20:8765"]), 3)

    def test_a_wrong_token_is_refused_without_printing_it(self):
        self.serve()
        with self.assertRaises(chat.ServiceError) as caught:
            Client.connect(self.url, tokens=["wrong" * 9])
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn("wrong", str(caught.exception))

    def test_a_message_prints_the_answer_and_remembers_the_conversation(self):
        runtime = self.serve()
        self.scripted(self.script(answer="Lunch with Sam at noon."))
        code, out, err = self.chat(args("What's on my calendar tomorrow?", json_out=True))
        self.assertEqual(code, 0, (out, err))
        answer = json.loads(out)
        self.assertEqual((answer["status"], answer["answer"]), ("completed", "Lunch with Sam at noon."))
        thread_id = answer["threadId"]
        self.assertEqual(chat.last_thread(self.client().url), thread_id)
        self.assertEqual(runtime.runs[answer["runId"]].origin, "cli")
        # The next message continues it; --new starts another.
        self.scripted(self.script(answer="Done."))
        code, out, _ = self.chat(args("And the day after?", json_out=True))
        self.assertEqual((code, json.loads(out)["threadId"]), (0, thread_id))
        self.scripted(self.script(answer="Done."))
        code, out, _ = self.chat(args("Something else", json_out=True, new=True))
        self.assertNotEqual(json.loads(out)["threadId"], thread_id)
        code, out, err = self.chat(args("x", thread="000000000000"))
        self.assertEqual(code, 3)
        self.assertIn("doesn't exist", err)

    def test_plain_output_without_a_terminal(self):
        self.serve()
        self.scripted(self.script(answer="Lunch with Sam at noon."))
        code, out, err = self.chat(args("What's on my calendar tomorrow?"))
        self.assertEqual(code, 0, err)
        self.assertIn("Done\nLunch with Sam at noon.", out)

    def test_an_approval_is_never_answered_from_json_or_without_a_terminal(self):
        runtime = self.serve()
        for json_out in (True, False):
            with self.subTest(json=json_out):
                script = Script([[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)],
                                 [("DONE", None, None)]], [item()], answer="Sent.")
                self.scripted(script, screens=[COMPOSE, COMPOSE, sent("I'm running late")])
                code, out, err = self.chat(args("Text Sam I'm running late", json_out=json_out, new=True))
                self.assertEqual(code, 2, (out, err))
                run = self.last_run(runtime)
                self.assertIsNotNone(run.approval)  # still waiting: nothing here answered it
                if json_out:
                    state = json.loads(out)
                    self.assertEqual(state["status"], "waiting_for_approval")
                    self.assertEqual(state["approval"]["text"], "I'm running late")
                else:
                    self.assertIn("“I'm running late”", out)
                    self.assertIn("Waiting for your approval in Mobster", out)
                run.answer_approval(run.approval["id"], False)
                self.wait_done(run)
                self.assertNotIn(("TAP", "Send", None), self.phones[run.id].actions)

    def test_a_question_waits_and_the_next_message_answers_it(self):
        runtime = self.serve()
        data = self.thread(runtime, "Text Sam the address")
        run = runtime.runs[data["run"]["id"]]
        helper, box = self.ask(run, clarify())
        code, out, _ = self.chat(args(thread=data["thread"]["id"], json_out=True, wait=1))
        self.assertEqual(code, 3)  # no message and no terminal: a usage error
        status = self.client().wait_run(run.id, 0)
        state = chat.waiting(status, data["thread"]["id"])
        self.assertEqual((state["status"], state["choices"]), ("waiting_for_answer", ["Sam Lee", "Sam Park"]))
        code, out, _ = self.chat(args("Sam Park, the one from work", thread=data["thread"]["id"], json_out=True))
        self.assertEqual((code, json.loads(out)["routed"]), (0, "answer"))
        helper.join(5)
        self.assertEqual(box["answer"], "answer:Sam Park, the one from work")

    def test_a_message_while_it_works_steers_it(self):
        runtime = self.serve()
        data = self.thread(runtime, "Find my last message from Kate Bell")
        run = runtime.runs[data["run"]["id"]]
        code, out, _ = self.chat(args("only today's", thread=data["thread"]["id"]))
        self.assertEqual(code, 0)
        self.assertIn("Mobster has your message", out)
        self.assertEqual(run.steering.pending(), 1)

    def test_conversations_missing_from_an_older_mobster(self):
        self.serve()
        client = self.client()
        client.status["extensions"] = {}
        code, _out, err = self.chat(args("Hi"), client=client)
        self.assertEqual((code, err.strip()), (3, chat.NO_THREADS))


class TerminalTests(Served):
    """A real terminal (a pty): one key answers an approval, only where it may."""

    def converse(self, app_session, keys):
        runtime = self.serve(app_session=app_session)
        script = Script([[("TYPE", "Message", "I'm running late"), ("TAP", "Send", None)], [("DONE", None, None)]],
                        [item()], answer="Sent Sam “I'm running late”.")
        self.scripted(script, screens=[COMPOSE, COMPOSE, sent("I'm running late"), sent("I'm running late")])
        master, stdin, stdout, seen = self.terminal()
        result = {}

        def talk():
            result["code"] = chat.run(args("Text Sam I'm running late"), stdin=stdin, stdout=stdout,
                                      stderr=stdout, client=self.client())
        talker = threading.Thread(target=talk, daemon=True)
        talker.start()
        for wait_for, key in keys:
            self.until(seen, wait_for)
            os.write(master, key)
        talker.join(15)
        run = self.last_run(runtime)
        self.wait_done(run)
        return result.get("code"), ANSI.sub("", "".join(seen)), {"phone": self.phones[run.id]}

    def test_with_mobster_serve_y_approves_the_exact_text_shown(self):
        code, screen, box = self.converse(None, [("[y] Send", b"y\n")])
        self.assertEqual(code, 0, screen)
        self.assertIn("“I'm running late”", screen)
        self.assertIn("Approved", screen)
        self.assertIn(("TAP", "Send", None), box["phone"].actions)

    def test_with_the_mac_app_y_never_approves_and_n_declines(self):
        code, screen, box = self.converse(SESSION, [("Choose Send or Don't send in Mobster", b"y\n"),
                                                    ("the app holds approvals", b"n\n")])
        self.assertEqual(code, 1, screen)
        self.assertNotIn(("TAP", "Send", None), box["phone"].actions)
        self.assertIn("Declined", screen)
        self.assertNotIn("[y] Send", screen)
        # Where to answer is a line of its own, so the keys never wrap into it on a narrow terminal.
        self.assertRegex(screen,
                         r"Choose Send or Don't send in Mobster\.\r?\n\s*\[n\] Don't send   \[x\] Stop the task")

    def test_typed_words_never_approve(self):
        code, screen, box = self.converse(None, [("[y] Send", b"yes send it\n"), ("A typed message never", b"n\n")])
        self.assertEqual(code, 1, screen)
        self.assertNotIn(("TAP", "Send", None), box["phone"].actions)
        self.assertIn("Declined", screen)


    def test_a_y_typed_while_it_worked_never_answers_the_approval(self):
        # A y and Return typed before Mobster showed what it asks (pressed early, or meant for something else)
        # waits in the terminal's input; it must not answer the approval that appears after it.
        runtime = self.serve()
        gate = threading.Event()
        script = Gated([[("WAIT", None, None)], [("TYPE", "Message", "I quit"), ("TAP", "Send", None)],
                        [("DONE", None, None)]], [item()], answer="Sent.", gate=gate)
        self.scripted(script, screens=[COMPOSE, COMPOSE, COMPOSE, sent("I quit"), sent("I quit")])
        master, stdin, stdout, seen = self.terminal()
        result = {}

        def talk():
            result["code"] = chat.run(args("Text Sam what my note says"), stdin=stdin, stdout=stdout, stderr=stdout,
                                      client=self.client())
        talker = threading.Thread(target=talk, daemon=True)
        talker.start()
        self.until(seen, "Text Sam what my note says")
        os.write(master, b"y\n")  # while the model still decides: nothing asks yet
        time.sleep(.2)
        gate.set()
        self.until(seen, "[y] Send")
        run = self.last_run(runtime)
        time.sleep(.5)
        self.assertIsNotNone(run.approval)  # the early y answered nothing
        self.assertNotIn(("TAP", "Send", None), self.phones[run.id].actions)
        os.write(master, b"n\n")  # a key pressed with the text on screen still answers it
        talker.join(15)
        self.wait_done(run)
        self.assertEqual(result.get("code"), 1)
        self.assertNotIn(("TAP", "Send", None), self.phones[run.id].actions)
        self.assertEqual(next(e for e in run.events if e["event"] == "approval_resolved")["decision"], "denied")


class Gated(Script):
    """A model whose second turn waits for ``gate``, so a test can type while Mobster works."""

    def __init__(self, *args, gate=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.gate = gate

    def complete(self, messages, schema, timeout=60):
        if "items" not in schema["properties"] and len(self.prompts) == 1:
            self.gate.wait(10)
        return super().complete(messages, schema, timeout)


class FakeClient:
    """Records what Chat posts; ``app`` says whether the Mac app holds approvals."""

    def __init__(self, app=False):
        self._app_session = app
        self.posts = []

    def app_session(self, run_id):
        return self._app_session

    def call(self, method, path, body=None, **kwargs):
        self.posts.append((method, path, body))
        return {}


class ApprovalWordsTests(unittest.TestCase):
    def chat(self, app=False):
        out = io.StringIO()
        term = chat.Term(io.StringIO(""), out)
        return chat.Chat(FakeClient(app), term, color=False), out

    def test_the_terminal_names_the_buttons_the_mac_app_shows(self):
        cases = [({"act": "send_message"}, ("Send", "Don't send")),
                 ({"act": "pay"}, ("Pay", "Don't pay")),
                 ({"act": "place_order"}, ("Place order", "Don't order")),
                 ({"act": "cancel_subscription"}, ("Cancel it", "Keep it")),
                 ({"act": "end_call"}, ("End call", "Stay on")),
                 ({"operation": "TAP", "label": "Buy now"}, ("Buy", "Don't buy")),
                 ({"operation": "TAP", "label": "Continue"}, ("Approve", "Decline")),
                 ({"kind": "loop", "act": "send"}, ("Approve", "Decline")),
                 ({}, ("Approve", "Decline"))]
        for approval, words in cases:
            with self.subTest(approval=approval):
                self.assertEqual(chat.verbs(approval), words)

    def test_an_approval_with_choices_takes_a_number_only_without_the_mac_app(self):
        approval = {"id": "a" * 12, "kind": "question", "label": "Can't tell if this one is a match",
                    "choices": [{"id": "skip", "label": "Skip it"}, {"id": "stop", "label": "Stop here"}]}
        talk, out = self.chat()
        talk.interactive = True
        talk.client.run = lambda run_id: {"approval": approval}
        talk.ask("b" * 12, {"approval_id": approval["id"]})
        screen = out.getvalue()
        self.assertIn("[1] Skip it", screen)
        self.assertIn("Type the letter or number, then Return.", screen)
        self.assertTrue(talk.decide("2"))
        self.assertEqual(talk.client.posts[-1][2], {"id": approval["id"], "approve": True, "choice": "stop"})
        # Words, or a number that isn't a choice, answer nothing.
        talk.pending = dict(approval, runId="b" * 12)
        self.assertFalse(talk.decide("3"))
        self.assertFalse(talk.decide("skip it"))
        self.assertEqual(len(talk.client.posts), 1)
        # With the Mac app open, a choice is made there.
        talk, out = self.chat(app=True)
        talk.pending = dict(approval, runId="b" * 12)
        self.assertTrue(talk.decide("1"))
        self.assertEqual(talk.client.posts, [])
        self.assertIn("Choose Approve in Mobster", out.getvalue())


class KeyTests(unittest.TestCase):
    """What the prompt makes of the keys a terminal sends."""

    def chat(self):
        term = chat.Term(io.StringIO(""), io.StringIO())
        return chat.Chat(FakeClient(), term, color=False), term

    def test_arrow_and_function_keys_never_end_up_in_a_message(self):
        talk, term = self.chat()
        talk.feed("hi\x1b[A\x1b[D\x1b[1;3C\x1bOP")  # up, left, Option-right, F1
        talk.feed(" there")
        self.assertEqual(term.buffer, "hi there")
        talk.feed("\x1b")  # Escape on its own: the next key is kept
        talk.feed("!")
        self.assertEqual(term.buffer, "hi there!")

    def test_a_character_split_across_reads_arrives_whole(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        stdin, stdout = io.open(slave, "r", closefd=False), io.open(os.dup(slave), "w")
        self.addCleanup(stdout.close)
        term = chat.Term(stdin, stdout)
        self.assertTrue(term.tty)
        with term:
            os.write(master, "“".encode()[:2])
            self.assertEqual(term.keys(1), "")
            os.write(master, "“".encode()[2:] + "ok".encode())
            self.assertEqual(term.keys(1), "“ok")

    def test_a_half_typed_line_waits_while_an_approval_is_answered(self):
        talk, term = self.chat()
        talk.interactive = True
        approval = {"id": "a" * 12, "operation": "TAP", "label": "Send", "text": "On my way", "act": "send_message"}
        talk.client.run = lambda run_id: {"approval": approval}
        talk.feed("only toda")
        talk.ask("b" * 12, {"approval_id": approval["id"]})
        self.assertEqual((term.buffer, talk.held), ("", "only toda"))  # its letters can't become the answer
        talk.feed("n\n")
        self.assertEqual(talk.client.posts[-1][2], {"id": approval["id"], "approve": False})
        talk.handle("event", "b" * 12, {"event": "approval_resolved", "decision": "denied"})
        self.assertEqual((term.buffer, talk.held), ("only toda", ""))


class PauseTests(unittest.TestCase):
    def test_a_mobster_without_pause_says_so(self):
        class NoPause(FakeClient):
            def call(self, method, path, body=None, **kwargs):
                super().call(method, path, body)
                raise chat.ServiceError(404, {"error": "Not found"})

        out = io.StringIO()
        talk = chat.Chat(NoPause(), chat.Term(io.StringIO(""), out), color=False)
        talk.run_id = "b" * 12
        talk.pause()
        self.assertEqual(talk.client.posts, [("POST", f"/api/runs/{'b' * 12}/pause", {"paused": True})])
        self.assertIn("This Mobster can't pause a task yet.", out.getvalue())
        self.assertFalse(talk.paused)


class ReplTests(Served):
    def test_a_conversation_in_the_terminal(self):
        runtime = self.serve()
        master, stdin, stdout, seen = self.terminal()
        result = {}

        def talk():
            result["code"] = chat.run(args(new=True), stdin=stdin, stdout=stdout, stderr=stdout, client=self.client())
        talker = threading.Thread(target=talk, daemon=True)
        self.scripted(self.script(answer="Lunch with Sam at noon."))
        talker.start()
        self.until(seen, "/help for commands")
        os.write(master, b"What's on my calendar tomorrow?\n")
        self.until(seen, "Lunch with Sam at noon.")
        # The next task stays running (no script): typing while it works steers it.
        os.write(master, b"And the day after?\n")
        deadline = time.monotonic() + 5
        while len(runtime.runs) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(runtime.runs), 2)
        os.write(master, b"only the afternoon\n")
        self.until(seen, "Sent while Mobster was working")
        running = self.last_run(runtime)
        self.assertEqual(running.steering.pending(), 1)
        os.write(master, b"/threads\n")
        self.until(seen, "What's on my calendar tomorrow? (this one)")
        os.write(master, b"/help\n")
        self.until(seen, "/attach PATH")
        os.write(master, b"/pause\n")
        self.until(seen, "Paused. /pause again to continue.")  # the harness track's pause (POST /api/runs/ID/pause)
        os.write(master, b"/stop\n")
        self.until(seen, "Stopping")
        os.write(master, b"/quit\n")
        talker.join(10)
        self.assertEqual(result.get("code"), 0)
        self.assertTrue(running.stop.is_set())
        thread_id = running.extras["threadId"]
        self.assertEqual(chat.last_thread(self.client().url), thread_id)
        kinds = [(i["kind"], i.get("routed")) for i in self.items(runtime, thread_id)]
        self.assertEqual(kinds[-3:], [("user", "new_run"), ("run", None), ("user", "steer")])


class RememberTests(Served):
    """A message that only asks Mobster to remember something starts no task: the service offers to remember it, and
    `mobster chat` shows the offer and, on a terminal, takes y or n for it. It used to wait for a task that never
    started: --json and a pipe asked for the run None, and a terminal waited out --wait and said "Still working"."""

    def setUp(self):
        super().setUp()
        self.memory = scratch_memory(self)

    def facts(self):
        return [fact["text"] for fact in self.memory.all_facts()]

    def test_json_says_what_it_offered_and_runs_nothing(self):
        runtime = self.serve()
        code, out, err = self.chat(args(REMEMBER, json_out=True))
        self.assertEqual(code, 0, (out, err))
        answer = json.loads(out)
        self.assertEqual(answer["status"], "remember")
        self.assertEqual([(p["text"], p["pin"]) for p in answer["proposals"]], [(GYM, False)])
        self.assertTrue(answer["proposals"][0]["id"])
        self.assertEqual(chat.last_thread(self.client().url), answer["threadId"])
        self.assertEqual(runtime.runs, {})
        self.assertEqual(self.facts(), [])  # an offer, never a fact
        # A new conversation's first message says how it was taken, as a message to an existing one does.
        thread_id, first = self.client().send(None, {"text": "Remember that my sister's name is Kate Bell"})
        self.assertEqual((first["routed"], first["run"]), ("remember", None))
        self.assertEqual([p["text"] for p in first["proposals"]], ["My sister's name is Kate Bell"])
        self.assertEqual(runtime.runs, {})

    def test_without_a_terminal_it_says_where_to_answer(self):
        runtime = self.serve()
        code, out, err = self.chat(args(REMEMBER))
        self.assertEqual(code, 0, (out, err))
        self.assertIn(f"Remember this?  “{GYM}”", out)
        self.assertIn("Mobster will use it in tasks it fits. It stays on this Mac.", out)
        self.assertIn(f'Answer it in Mobster for Mac, or save it with: mobster memory add "{GYM}"', out)
        self.assertEqual((runtime.runs, self.facts()), ({}, []))
        # The same words again: there's nothing new to offer.
        code, out, _ = self.chat(args(REMEMBER))
        self.assertEqual(code, 0)
        self.assertIn("Mobster already has this.", out)

    def on_a_terminal(self, *keys):
        runtime = self.serve()
        master, stdin, stdout, seen = self.terminal()
        result = {}

        def talk():
            result["code"] = chat.run(args(REMEMBER, wait=10), stdin=stdin, stdout=stdout, stderr=stdout,
                                      client=self.client())
        talker = threading.Thread(target=talk, daemon=True)
        talker.start()
        for wait_for, key in keys:
            self.until(seen, wait_for)
            os.write(master, key)
        talker.join(15)
        self.assertEqual(runtime.runs, {})
        return result.get("code"), ANSI.sub("", "".join(seen))

    def test_on_a_terminal_y_remembers_it(self):
        code, screen = self.on_a_terminal(("[y] Remember   [n] Not now", b"y\n"))
        self.assertEqual(code, 0, screen)
        self.assertIn("Mobster will remember this.", screen)
        self.assertNotIn("Still working", screen)
        self.assertEqual(self.facts(), [GYM])

    def test_on_a_terminal_words_never_answer_it_and_n_remembers_nothing(self):
        code, screen = self.on_a_terminal(("[y] Remember", b"sure, why not\n"), ("Type y or n first.", b"n\n"))
        self.assertEqual(code, 0, screen)
        self.assertIn("Not remembered.", screen)
        self.assertEqual(self.facts(), [])

    def test_in_a_conversation_it_asks_and_the_next_task_runs_as_usual(self):
        runtime = self.serve()
        master, stdin, stdout, seen = self.terminal()
        result = {}

        def talk():
            result["code"] = chat.run(args(new=True), stdin=stdin, stdout=stdout, stderr=stdout, client=self.client())
        talker = threading.Thread(target=talk, daemon=True)
        talker.start()
        self.until(seen, "/help for commands")
        os.write(master, (REMEMBER + "\n").encode())
        self.until(seen, "[y] Remember")
        os.write(master, b"y\n")
        self.until(seen, "Mobster will remember this.")
        self.assertEqual((runtime.runs, self.facts()), ({}, [GYM]))
        self.scripted(self.script(answer="Lunch with Sam at noon."))
        os.write(master, b"What's on my calendar tomorrow?\n")
        self.until(seen, "Lunch with Sam at noon.")
        os.write(master, b"/quit\n")
        talker.join(10)
        self.assertEqual(result.get("code"), 0)
        self.assertEqual(len(runtime.runs), 1)


class OlderMobsterRememberTests(unittest.TestCase):
    def test_an_answer_with_only_the_items_routed_still_shows_the_offer(self):
        """An older Mobster's answer to a new conversation's first message keeps only the item's routed: the offer
        comes from the conversation's pending suggestions."""
        class Older(FakeClient):
            def call(self, method, path, body=None, **kwargs):
                super().call(method, path, body)
                return {"proposals": [{"id": "p" * 12, "text": GYM, "pin": False, "status": "pending"}]}

        out = io.StringIO()
        talk = chat.Chat(Older(), chat.Term(io.StringIO(""), out), color=False, interactive=False)
        talk.thread_id = "a" * 12
        response = {"thread": {"id": "a" * 12}, "item": {"kind": "user", "routed": "remember"}, "run": None}
        self.assertEqual(chat.routed_as(response), "remember")
        self.assertEqual(chat.offered(talk, response, tty=False, as_json=False, wait=1, out=out), chat.EXIT_DONE)
        self.assertIn(f"Remember this?  “{GYM}”", out.getvalue())
        self.assertEqual(talk.client.posts, [("GET", f"/api/memory/proposals?thread={'a' * 12}", None)])


if __name__ == "__main__":
    unittest.main()
