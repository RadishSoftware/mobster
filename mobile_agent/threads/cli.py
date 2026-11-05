"""`mobster chat`: talk to Mobster's agent in a conversation, from Terminal."""

import json
import queue
import socket
import sys
import threading
import time
from pathlib import Path

from ..narrate import approval_title, verbs
from .chat_once import EXIT_COULDNT, EXIT_DONE, once, proposals_of, routed_as, where_to_remember, workflow_once
from .client import NO_THREADS, NOT_RUNNING, Client, NoService, ServiceError
from .recent import last_thread, remember_thread
from .term import Term, isatty

ATTACH_MAX = 25 * 1024 * 1024
HELP = ("/new  start a new conversation      /threads  your recent conversations\n"
        "/open ID  continue one              /stop  stop the task      /pause  pause or continue it\n"
        "/attach PATH  add a file to your next message                 /quit  leave (the task goes on)")
# A route the running Mobster doesn't have (404).
NO_PAUSE = "This Mobster can't pause a task yet."
NO_FILES = "This Mobster can't take files yet."
DECISIONS = {"approved": "Approved", "denied": "Declined", "redirected": "Declined; Mobster does what you said "
             "instead", "timeout": "Nobody answered in time, so Mobster didn't do it", "stopped": "Stopped",
             "answered": "Answered"}


def add_arguments(parser, helpers):
    bounded = helpers.get("bounded")
    parser.add_argument("message", nargs="*", metavar="MESSAGE",
                        help="what to ask Mobster's agent; leave it out for a conversation in this terminal")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--thread", metavar="ID", help="continue this conversation (default: the one this terminal "
                                                      "used last)")
    which.add_argument("--new", action="store_true", help="start a new conversation")
    parser.add_argument("--device", metavar="NAME",
                        help="the iPhone or simulator: an id, UDID or name (default: the conversation's)")
    parser.add_argument("--json", action="store_true",
                        help="print one JSON object when the task finishes or needs you; never answers an approval")
    parser.add_argument("--keep-waiting", action="store_true",
                        help="with --json and the app or mobster serve running: wait while the task waits for you, "
                             "until it ends or --wait runs out")
    parser.add_argument("--wait", type=bounded(float, 0, 3600) if bounded else float, default=None,
                        metavar="SECONDS", help="with the app or mobster serve running: how long to follow a task "
                                                "(default 300); it goes on after")
    parser.add_argument("--url", metavar="URL", help="Mobster's address (default: $MOBSTER_URL, else the running "
                                                     "Mac app's, else http://127.0.0.1:8765)")
    parser.add_argument("--workflow", metavar="NAME", help=(
        "run a saved workflow (its name or id) instead of a "
        "message: in a new conversation, its own one, or --thread ID"))
    parser.add_argument("--set", action="append", default=[], metavar="BLANK=VALUE",
                        help="with --workflow: a value for one of its blanks; repeat it for each")


class Chat:
    def __init__(self, client, term, *, thread_id=None, device=None, color=None, interactive=True):
        from ..style import Palette, color_enabled
        self.client, self.term = client, term
        self.thread_id, self.device = thread_id, device
        self.paint = Palette(color_enabled(term.stdout) if color is None else color)
        self.interactive = interactive
        self.events = queue.Queue()
        self.run_id = None          # the task being followed
        self.pending = None         # the run's approval or question, as GET /api/runs/{id} shows it
        self.paused = False
        self.attachments = []       # [(id, name)] for the next message
        self.renderers = {}
        self.outcomes = set()       # runs whose outcome is printed
        self.finished = {}          # run id -> the run, once finished
        self.followers = {}
        self.quit = False
        self.held = ""              # the line you were typing when an approval came; back once it's answered
        self.offers = []            # suggestions to remember that wait for y or n here, oldest first
        self.escape = None          # inside a terminal key sequence (an arrow key): None, "esc", "csi" or "ss3"

    # -- printing

    def say(self, text, *roles):
        self.term.print(self.paint(text, *roles) if roles else text)

    def note(self, text, glyph="!", role="amber", indent="  "):
        """A line whose glyph carries the colour and whose words stay the terminal's own colour, so it reads on
        light and dark profiles alike."""
        self.term.print(indent + self.paint(glyph, role, "bold") + " " + text)

    def keys(self, *pairs):
        """"[y] Send   [n] Don't send" with each key in bold."""
        return "   ".join(self.paint(f"[{key}]", "bold") + f" {label}" for key, label in pairs)

    def renderer(self, run_id):
        renderer = self.renderers.get(run_id)
        if renderer is None:
            from ..console import ConsoleRenderer
            term = self.term

            class Lines(ConsoleRenderer):
                def line(self, text=""):
                    term.print(text)
            renderer = self.renderers[run_id] = Lines(term.stdout, color=bool(self.paint.codes), live=False)
        return renderer

    # -- following a task

    def follow(self, run_id):
        """Stream the task's events into the queue (a thread of its own), from the start."""
        self.run_id = run_id
        if run_id in self.followers:
            return
        thread = threading.Thread(target=self._stream, args=(run_id,), daemon=True, name=f"mobster-chat-{run_id}")
        self.followers[run_id] = thread
        thread.start()

    def _stream(self, run_id):
        last, tries = None, 0
        while True:
            try:
                for event in self.client.events(f"/api/runs/{run_id}/events", last, timeout=30):
                    tries = 0
                    if event is None:
                        continue
                    last = event.get("seq", last)
                    self.events.put(("event", run_id, event))
                    if event.get("event") == "run_finished":
                        self.events.put(("end", run_id, None))
                        return
                return self._ended(run_id)
            except (socket.timeout, TimeoutError):
                continue
            except ServiceError as error:
                self.events.put(("lost", run_id, str(error)))
                return
            except NoService:
                tries += 1
                if tries > 5:
                    self.events.put(("lost", run_id, "Lost the connection to Mobster."))
                    return
                time.sleep(min(2.0, 0.2 * tries))

    def _ended(self, run_id):
        self.events.put(("end", run_id, None))

    def pump(self, timeout=0.0):
        """Handle what the followers queued; waits up to ``timeout`` for the first."""
        try:
            item = self.events.get(timeout=timeout) if timeout else self.events.get_nowait()
        except queue.Empty:
            return
        while True:
            self.handle(*item)
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                return

    def handle(self, kind, run_id, event):
        if kind == "lost":
            self.note(str(event), indent="")
            if run_id == self.run_id:
                self.run_id = None
            return
        if kind == "end":
            if run_id not in self.finished:
                try:
                    self.finished[run_id] = self.client.run(run_id)
                except (ServiceError, NoService):
                    self.finished[run_id] = {"status": "error"}
            if run_id == self.run_id:
                self.run_id, self.pending, self.paused = None, None, False
                self.give_back()
            return
        name = event.get("event")
        renderer = self.renderer(run_id)
        if name == "step" and isinstance(event.get("text"), str):
            # Mobster's agent says each step in a sentence ("Typed “Running late” in Message").
            self.say("  " + self.paint("●", "green") + " " + event["text"])
            return
        if name == "plan" and isinstance(event.get("line"), str) and event["line"]:
            self.say(f"  {event['line']}", "faint")
            return
        if name in ("result", "run_finished"):
            summary = event if name == "result" else (event.get("summary") if isinstance(event.get("summary"),
                                                                                          dict) else {})
            if run_id not in self.outcomes:
                self.outcomes.add(run_id)
                renderer._flush_steps()
                self.outcome({**summary, "status": summary.get("status") or event.get("status") or "error"})
            return
        if name not in ("approval_requested", "approval_resolved", "user_message", "steer_applied", "steer_unread"):
            renderer(event)  # the run's events, one line per step (console.py)
            return
        renderer.narrator.feed(event)
        if name == "approval_requested":
            self.ask(run_id, event)
        elif name == "approval_resolved":
            self.pending = None
            decision = str(event.get("decision") or "")
            text = DECISIONS.get(decision) or ("Answered" if decision.startswith("choice:") else decision)
            self.say(f"  ⎿ {text}", "faint")
            self.give_back()
        elif name == "steer_applied":
            self.say("  – Mobster read your message", "faint")
        elif name == "steer_unread":
            self.note("Mobster finished before reading your message. Send it again to run it as a new task.")
        elif name == "user_message" and event.get("source") not in ("cli",):
            where = {"app": "in Mobster", "voice": "by voice", "tui": "in the terminal UI", "mcp": "from your agent"}
            self.say(f"  ↳ You wrote {where.get(event.get('source'), 'elsewhere')}: “{event.get('text')}”")

    def outcome(self, summary):
        """The task's end: its status, time and cost, then its answer (or why it stopped)."""
        from ..narrate import completion_message, seconds, status_glyph, status_label, status_tone
        status = summary.get("status") or "error"
        tone = status_tone(status)
        role = {"success": "green", "error": "red", "warning": "amber", "review": "accent"}.get(tone, "muted")
        glyph = status_glyph(tone)
        facts = [seconds(summary.get("elapsed_ms"))] if summary.get("elapsed_ms") else []
        cost = summary.get("costUsd")
        if isinstance(cost, (int, float)) and cost > 0:
            facts.append(f"${cost:.4f}" if cost < 0.1 else f"${cost:.2f}")
        self.say(self.paint(glyph, role, "bold") + " " + self.paint(status_label(status), "bold")
                 + (self.paint("  " + ", ".join(facts), "faint") if facts else ""))
        data = summary.get("data")
        if isinstance(data, (dict, list)):
            body = json.dumps(data, indent=2, ensure_ascii=False)
        else:
            body = data if isinstance(data, str) and data.strip() else summary.get("answer")
        if body and status not in ("approval_denied", "stopped"):
            for line in str(body).splitlines():
                self.say("  " + line)
        else:
            message = completion_message(status, summary.get("reason"), summary.get("data_status"), summary)
            if message:
                self.say("  " + message)
        if self.term.prompting:
            self.say("")  # a conversation: one blank line between tasks

    def ask(self, run_id, event):
        try:
            approval = self.client.run(run_id).get("approval")
        except (ServiceError, NoService):
            approval = None
        if not approval or approval.get("id") != event.get("approval_id"):
            return
        self.pending = dict(approval, runId=run_id)
        if approval.get("kind") == "clarify":
            self.note(self.paint(str(approval.get("question") or approval.get("label") or "Mobster has a question"),
                                 "bold"), glyph="?", role="accent", indent="")
            for number, choice in enumerate(approval.get("choices") or (), 1):
                self.say("  " + self.keys((str(number), str(choice.get("label")))))
            self.say("  Type your answer" + (", or its number" if approval.get("choices") else "")
                     + ". Mobster waits 10 minutes, then stops.", "faint")
            return
        # A line half typed when the approval came (a message meant for the task) waits until it's answered, so its
        # letters never become the answer.
        with self.term.lock:
            if self.term.buffer:
                self.held, self.term.buffer = self.held or self.term.buffer, ""
        self.show_approval(approval)
        if self.interactive:
            yes, no = verbs(approval)
            choices = [c for c in approval.get("choices") or () if isinstance(c, dict) and c.get("id")]
            if self.client.app_session():
                # On its own line: the keys below it never wrap into it on a narrow terminal.
                self.say(f"  Choose {yes} or {no} in Mobster.")
                self.say("  " + self.keys(("n", no), ("x", "Stop the task")))
            else:
                self.say("  " + self.keys(("y", yes), ("n", no), ("x", "Stop the task")))
                for number, choice in enumerate(choices, 1):
                    self.say("  " + self.keys((str(number), str(choice.get("label")))))
            self.say("  Type the " + ("letter or number" if choices and not self.client._app_session else "letter")
                     + ", then Return.", "faint")
        # Only a key pressed after the exact text is on screen answers it: anything typed before is dropped.
        self.term.discard_typeahead()
        self.escape = None

    def give_back(self):
        """The line you were typing when an approval came, back at the prompt once nothing waits."""
        if self.held and not self.pending:
            with self.term.lock:
                self.term.buffer, self.held = self.held + self.term.buffer, ""
            self.term.show()

    def show_approval(self, approval):
        self.note(self.paint(approval_title(approval), "bold"), glyph="?", indent="")
        if approval.get("text"):
            self.say(f"    “{approval['text']}”")

    # -- something to remember

    def offer(self, proposals):
        """A message that only asked Mobster to remember something: no task runs, and each suggestion shows here."""
        proposals = [p for p in proposals or () if isinstance(p, dict) and p.get("id") and p.get("text")]
        if not proposals:
            self.say("Mobster already has this.")
            return
        if not self.interactive:
            for proposal in proposals:
                self.show_offer(proposal, ask=False)
                self.say("  " + where_to_remember(proposal), "faint")
            return
        asking = bool(self.offers)
        self.offers.extend(proposals)
        if not asking:
            self.show_offer(self.offers[0])

    def show_offer(self, proposal, ask=True):
        self.note(self.paint("Remember this?", "bold") + f"  “{proposal['text']}”", glyph="?", role="accent", indent="")
        scope = proposal.get("scope") or "global"
        if scope == "global":
            use = ("It goes with every task (pinned)." if proposal.get("pin") else
                   "Mobster will use it in tasks it fits.")
        else:
            where = ("tasks for this saved profile and workflow" if scope.startswith("profile:") and
                     "/workflow:" in scope else "tasks for this saved profile" if scope.startswith("profile:")
                     else "tasks in this workflow" if scope.startswith("workflow:") else "eligible tasks")
            use = (f"Pinned: it goes with {where}." if proposal.get("pin") else
                   f"Mobster will use it in {where} when it fits.")
        self.say(f"  {use} It stays on this Mac.", "faint")
        if ask:
            self.say("  " + self.keys(("y", "Remember"), ("n", "Not now")))
            self.say("  Type the letter, then Return.", "faint")
            # As with an approval, only a key pressed after the words are on screen answers.
            self.term.discard_typeahead()
            self.escape = None

    def answer_offer(self, line):
        """A line typed while a suggestion waits: exactly y or n answers it, a /command runs, and other words are
        neither sent nor taken as an answer."""
        word = line.strip().lower()
        if word.startswith("/"):
            return False
        if word not in ("y", "n"):
            self.say("  Type y or n first.")
            return True
        from urllib.parse import quote
        proposal = self.offers.pop(0)
        try:
            self.client.call("POST", f"/api/memory/proposals/{quote(str(proposal['id']), safe='')}",
                             {"accept": word == "y"})
        except ServiceError as error:
            self.note(str(error))  # answered in the Mac app meanwhile, or expired: the service's sentence
        except NoService:
            self.note(NOT_RUNNING)
        else:
            self.say("  ⎿ " + ("Mobster will remember this." if word == "y" else "Not remembered."), "faint")
        if self.offers:
            self.show_offer(self.offers[0])
        return True

    # -- your input

    def decide(self, line):
        """A line typed while an approval or a question waits: True when it answered (or stopped)."""
        pending = self.pending
        if not pending:
            return self.answer_offer(line) if self.offers else False
        run_id, word = pending["runId"], line.strip().lower()
        if pending.get("kind") == "clarify":
            choices = pending.get("choices") or []
            if word.isdigit() and 1 <= int(word) <= len(choices):
                return self.answer(run_id, {"id": pending["id"], "approve": True,
                                            "choice": choices[int(word) - 1]["id"]})
            return False
        choices = [c for c in pending.get("choices") or () if isinstance(c, dict) and c.get("id")]
        if word == "y" or (word.isdigit() and 1 <= int(word) <= len(choices)):
            if self.client.app_session():
                self.note(f"Choose {verbs(pending)[0]} in Mobster: the app holds approvals while it's open.")
                return True
            if word != "y":
                return self.answer(run_id, {"id": pending["id"], "approve": True,
                                            "choice": choices[int(word) - 1]["id"]})
            return self.answer(run_id, {"id": pending["id"], "approve": True})
        if word == "n":
            return self.answer(run_id, {"id": pending["id"], "approve": False})
        if word == "x":
            self.stop()
            return True
        return False

    def answer(self, run_id, body):
        try:
            self.client.call("POST", f"/api/runs/{run_id}/approval", body)
        except ServiceError as error:
            if error.code == "app_only":
                self.client._app_session = True
                self.note(f"Choose {verbs(self.pending or {})[0]} in Mobster: the app holds approvals while it's open.")
            else:
                self.note(str(error))
        except NoService:
            self.note(NOT_RUNNING)
        return True

    def stop(self):
        if not self.run_id:
            self.say("Nothing is running.", "faint")
            return
        try:
            self.client.call("POST", f"/api/runs/{self.run_id}/stop", {})
            self.say("  Stopping…", "faint")
        except (ServiceError, NoService) as error:
            self.note(str(error))

    def send(self, text):
        """A message: a new task, a steer, or an answer, as Mobster routes it. Returns the response, or None."""
        if self.pending and self.pending.get("kind") != "clarify":
            yes, no = verbs(self.pending)
            hint = (f"Choose {yes} or {no} in Mobster, or type n or x here."
                    if self.client._app_session else "Type y, n or x first.")
            self.note("A typed message never approves anything.")
            self.say(f"    {hint}")  # its own line: a narrow terminal never wraps it back to column 0
            return None
        body = {"text": text}
        if self.device:
            body["device"] = self.device
        if self.attachments:
            body["attachmentIds"] = [ident for ident, _name in self.attachments]
        try:
            self.thread_id, response = self.client.send(self.thread_id, body)
        except ServiceError as error:
            if error.status == 404 and error.code == "thread_not_found":
                self.note("That conversation is gone here. Your next message starts a new one.")
                self.thread_id = None
            else:
                self.note(str(error))
            return None
        except NoService:
            self.note(NOT_RUNNING)
            return None
        remember_thread(self.client.url, self.thread_id)
        self.attachments = []
        routed = routed_as(response)
        if routed == "steer":
            self.say("  ↳ Sent while Mobster was working", "faint")
            run_id = (response.get("item") or {}).get("runId")
            if run_id and run_id != self.run_id:
                self.follow(run_id)
        elif routed == "answer":
            self.pending = None
        elif routed == "remember":
            self.offer(proposals_of(self.client, self.thread_id, response))
        else:
            run = response.get("run") or {}
            if run.get("id"):
                where = run.get("deviceName")
                if not self.term.prompting:  # one message: its first line (a conversation shows what you typed)
                    self.renderer(run["id"]).start(text, f"on {where}" if where else "")
                elif where:
                    self.say(f"  on {where}", "faint")
                self.follow(run["id"])
        return response

    def command(self, line):
        name, _, rest = line.partition(" ")
        rest = rest.strip()
        if name in ("/quit", "/exit"):
            self.quit = True
        elif name == "/help":
            for row in HELP.splitlines():
                self.say(row)
        elif name == "/new":
            self.thread_id = None
            self.say("New conversation: your next message starts it.", "faint")
        elif name == "/threads":
            self.list_threads()
        elif name == "/open":
            self.open(rest)
        elif name == "/stop":
            self.stop()
        elif name == "/pause":
            self.pause()
        elif name == "/attach":
            self.attach(rest)
        else:
            self.note(f"Unknown command {name}. /help lists them.", indent="")

    def list_threads(self):
        try:
            threads = self.client.call("GET", "/api/threads?limit=10")["threads"]
        except (ServiceError, NoService) as error:
            self.note(str(error), indent="")
            return
        if not threads:
            self.say("No conversations yet.", "faint")
        for thread in threads:
            mark = "●" if thread.get("status") in ("running", "waiting") else " "
            here = " (this one)" if thread["id"] == self.thread_id else ""
            self.say(f"{mark} {self.paint(thread['id'], 'faint')}  {ago(thread.get('updatedAt')):>8}  "
                     f"{thread.get('title')}{here}")

    def open(self, thread_id):
        if not thread_id:
            self.note("Open which? /threads lists them.", indent="")
            return
        try:
            answer = self.client.thread(thread_id)
        except ServiceError as error:
            self.note(str(error), indent="")
            return
        except NoService:
            self.note(NOT_RUNNING, indent="")
            return
        thread = answer["thread"]
        self.thread_id = thread["id"]
        remember_thread(self.client.url, self.thread_id)
        self.say(f"Conversation “{thread.get('title')}”" + (f" on {thread['deviceName']}"
                                                              if thread.get("deviceName") else ""), "bold")
        for line in transcript(answer.get("items") or [], limit=6):
            self.say(line, "faint")
        running = thread.get("lastRunId")
        run = (answer.get("runs") or {}).get(running) if running else None
        if run and run.get("finishedAt") is None:
            self.follow(running)

    def pause(self):
        if not self.run_id:
            self.say("Nothing is running.", "faint")
            return
        try:
            answer = self.client.call("POST", f"/api/runs/{self.run_id}/pause", {"paused": not self.paused})
            self.paused = bool(answer.get("paused", not self.paused))
            self.say("  Paused. /pause again to continue." if self.paused else "  Continuing.", "faint")
        except ServiceError as error:
            # 404: the running Mobster has no pause yet (an older app, or `serve` without the harness).
            self.note(NO_PAUSE if error.status == 404 else str(error))
        except NoService:
            self.note(NOT_RUNNING)

    def attach(self, path):
        if not path:
            self.note("Attach which file? /attach PATH", indent="")
            return
        file = Path(path).expanduser()
        try:
            size = file.stat().st_size
            if size > ATTACH_MAX:
                self.note("A file can be at most 25 MB.", indent="")
                return
            data = file.read_bytes()
        except OSError:
            self.note(f"Can't read {path}.", indent="")
            return
        from urllib.parse import quote
        try:
            answer = self.client.call("POST", f"/api/attachments?name={quote(file.name)}", raw=data,
                                      content_type="application/octet-stream", timeout=120)
        except ServiceError as error:
            self.note(NO_FILES if error.status == 404 else str(error), indent="")
            return
        except NoService:
            self.note(NOT_RUNNING, indent="")
            return
        attachment = answer.get("attachment") or {}
        if attachment.get("id"):
            self.attachments.append((attachment["id"], attachment.get("name") or file.name))
            self.say(f"Attached {attachment.get('name') or file.name}. It goes with your next message.", "faint")

    # -- the loop

    def repl(self):
        term = self.term
        self.say(self.paint("Mobster", "accent", "bold") + self.paint("  conversation in Terminal   /help for "
                                                                      "commands   ctrl+d to leave", "faint"))
        if self.thread_id:
            self.open(self.thread_id)
        term.show()
        while not self.quit:
            try:
                self.pump()
                data = term.keys(0.1)
                if data is None:
                    break
                self.feed(data)
            except KeyboardInterrupt:
                if term.buffer:
                    term.buffer = ""
                    term.show()
                elif self.run_id:
                    self.stop()
                else:
                    break
        term.clear()
        return EXIT_DONE

    def feed(self, data):
        """What one read of the terminal brought, key by key."""
        for ch in data:
            self.type(ch)
            if self.quit:
                break
        if self.escape == "esc":
            self.escape = None  # Escape on its own, not the start of an arrow key's sequence

    def type(self, ch):
        """One key typed on the terminal (whole lines elsewhere)."""
        term = self.term
        if self.escape is not None:
            # The rest of a key's escape sequence (an arrow is ESC [ A, Option-arrow ESC [ 1 ; 3 D, F1 ESC O P):
            # dropped whole, so it never ends up in a message as "[A".
            if self.escape == "esc":
                self.escape = "csi" if ch == "[" else "ss3" if ch == "O" else None
            elif self.escape == "ss3" or "\x40" <= ch <= "\x7e":
                self.escape = None
            return
        if ch == "\x1b":
            self.escape = "esc"
            return
        if ch in ("\r", "\n"):
            line, term.buffer = term.buffer.strip(), ""
            if term.tty:
                term.clear()
                if line:
                    term.write(self.paint("› ", "accent") + line + "\n")
            self.submit(line)
            term.show()
        elif ch in ("\x7f", "\b"):
            term.buffer = term.buffer[:-1]
            term.show()
        elif ch == "\x15":  # ctrl+u
            term.buffer = ""
            term.show()
        elif ch == "\x04":  # ctrl+d
            if not term.buffer:
                self.quit = True
        elif ch < " " and ch != "\t":
            pass  # other control keys: ignored
        else:
            term.buffer += ch
            if term.tty:
                term.write(ch)
                term.shown = True

    def submit(self, line):
        if not line:
            return
        if self.decide(line):
            return
        if line.startswith("/"):
            self.command(line)
        else:
            self.send(line)

def ago(ms):
    if not ms:
        return ""
    seconds = max(0, time.time() - ms / 1000)
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{int(seconds // size)} {unit} ago"
    return "just now"


def transcript(items, limit=6):
    """The last exchanges of a thread, one line each, for /open."""
    lines = []
    for item in items:
        if item.get("kind") == "user":
            mark = {"steer": "↳ ", "answer": "↳ ", "unread": "↳ (not read) "}.get(item.get("routed"), "› ")
            lines.append(f"{mark}{item.get('text')}")
        elif item.get("kind") == "run" and item.get("result"):
            from ..narrate import status_label
            result = item["result"]
            lines.append(f"  {status_label(result.get('status'))}" + (f": {result['answer']}" if result.get("answer")
                                                                      else ""))
        elif item.get("kind") == "clarify":
            lines.append(f"  ? {item.get('question')}" + (f" → {item['answer']}" if item.get("answer") else ""))
    return lines[-limit:]


def run(args, stdin=None, stdout=None, stderr=None, client=None):
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    as_json = bool(args.json)

    def fail(message, code="couldnt_run"):
        if as_json:
            print(json.dumps({"status": "couldnt_run", "error": message, "code": code}), file=stdout, flush=True)
        else:
            print(message, file=stderr, flush=True)
        return EXIT_COULDNT

    if client is None:
        try:
            client = Client.connect(args.url, origin="cli")
        except NoService as error:
            return fail(str(error), "not_running")
        except ServiceError as error:
            return fail(str(error), error.code or "refused")
    if not client.has_threads:
        return fail(NO_THREADS, "no_threads")
    text = " ".join(args.message).strip()
    workflow = getattr(args, "workflow", None)
    if workflow and text:
        return fail("Give a message or --workflow, not both.", "usage")
    if getattr(args, "set", None) and not workflow:
        return fail("--set fills a workflow's blanks: use it with --workflow.", "usage")
    if getattr(args, "keep_waiting", False) and not as_json:
        return fail("--keep-waiting goes with --json.", "usage")
    wait = 300.0 if args.wait is None else args.wait
    if not text and not workflow and not isatty(stdin):
        text = stdin.read().strip()
        if not text:
            return fail("Say what Mobster should do: mobster chat \"What's on my calendar tomorrow?\"", "usage")
    thread_id = None
    if args.thread:
        thread_id = args.thread.strip().lower()
        try:
            client.thread(thread_id)
        except ServiceError as error:
            return fail(str(error), error.code or "thread_not_found")
        except NoService as error:
            return fail(str(error), "not_running")
    elif not args.new and not workflow:
        remembered = last_thread(client.url)
        if remembered:
            try:
                thread = client.thread(remembered)["thread"]
                thread_id = None if thread.get("archived") else remembered
            except (ServiceError, NoService):
                thread_id = None
    if text and len(text) > 4000:
        return fail("A message can be at most 4,000 characters.", "too_long")
    term = Term(stdin, stdout if not as_json else stderr)
    term.prompting = not text and not workflow
    with term:
        chat = Chat(client, term, thread_id=thread_id, device=args.device, interactive=term.tty and not as_json)
        if workflow:
            try:
                return workflow_once(client, chat, args, as_json=as_json, wait=wait, out=stdout, fail=fail)
            except NoService as error:
                return fail(str(error), "not_running")
        if text:
            if thread_id and not as_json:
                chat.say(f"Continuing your last conversation ({thread_id}); --new starts another.", "faint")
            try:
                return once(client, chat, text, as_json=as_json, wait=wait, out=stdout,
                            keep=bool(getattr(args, "keep_waiting", False)))
            except NoService as error:
                return fail(str(error), "not_running")
        if not term.tty:
            return fail("Say what Mobster should do: mobster chat \"What's on my calendar tomorrow?\"", "usage")
        return chat.repl()
