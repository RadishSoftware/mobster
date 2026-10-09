"""`mobster chat`: talk to Mobster's agent in a conversation, from Terminal.

It uses the running Mobster app or `mobster serve` (client.py) and never runs a task itself, so a conversation
here is the same one the Mac app shows, and approvals show where they always do.

- `mobster chat` on a terminal: a conversation. Type a task; type while it works to steer it; answer its questions.
  /new, /threads, /open ID, /stop, /pause, /attach PATH, /help, /quit.
- `mobster chat "message"`: one message, then the task's steps and answer. It continues the conversation this
  terminal used last; --new starts one, --thread ID picks one.

Approvals: Mobster asks before it sends, buys, posts or deletes. With the Mac app open, you choose Send there (the
app holds approvals); here [n] declines and [x] stops. With `mobster serve` and a terminal, [y] approves: one key,
with the exact text shown above it, never typed words. --json, and any run without a terminal, never answer an
approval: they print {"status": "waiting_for_approval", ...} and exit 2.

A message that only asks Mobster to remember something ("Remember that my gym is …") starts no task: the service
offers to remember it, and on a terminal [y] or [n] answers the offer. --json prints {"status": "remember", ...}.

Exit codes: 0 done, 1 the task ended without finishing, 2 it waits for you (an approval or a question),
3 couldn't run (no Mobster running, a refused message), 4 still working when --wait ran out (it goes on).
"""

import codecs
import json
import os
from pathlib import Path
import queue
import select
import socket
import sys
import threading
import time

from .client import Client, NoService, ServiceError, NOT_RUNNING, NO_THREADS

EXIT_DONE, EXIT_NOT_DONE, EXIT_NEEDS_YOU, EXIT_COULDNT, EXIT_RUNNING = 0, 1, 2, 3, 4
DONE = frozenset({"completed", "completed_unverified", "user_condition_met"})
ATTACH_MAX = 25 * 1024 * 1024
HELP = ("/new  start a new conversation      /threads  your recent conversations\n"
        "/open ID  continue one              /stop  stop the task      /pause  pause or continue it\n"
        "/attach PATH  add a file to your next message                 /quit  leave (the task goes on)")
# A route the running Mobster doesn't have (404). Worded unlike the seams' stub sentence for a missing command, which
# test_seam_cli looks for in a command's source to tell a stub from a real one.
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
    parser.add_argument("--wait", type=bounded(float, 0, 3600) if bounded else float, default=300.0,
                        metavar="SECONDS", help="with a message: how long to follow the task (default 300); "
                                                "it goes on after")
    parser.add_argument("--url", metavar="URL", help="Mobster's address (default: $MOBSTER_URL, else "
                                                     "http://127.0.0.1:8765)")


# -- the conversation this terminal used last ----------------------------------------------------------------------

def _last_file():
    from ..paths import user_data_dir
    return user_data_dir() / "state" / "chat-threads.json"


def last_thread(url):
    try:
        value = json.loads(_last_file().read_text()).get(url)
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and len(value) == 12 else None


def remember_thread(url, thread_id):
    path = _last_file()
    try:
        saved = json.loads(path.read_text()) if path.is_file() else {}
        if not isinstance(saved, dict):
            saved = {}
    except (OSError, ValueError):
        saved = {}
    saved[url] = thread_id
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w") as stream:
            stream.write(json.dumps(saved))
    except OSError:
        pass


# -- the terminal -----------------------------------------------------------------------------------------------

class Term:
    """Lines printed above a prompt that keeps what you're typing. On a terminal it reads keys one at a time
    (cbreak), so a single key can answer an approval; elsewhere it reads whole lines."""

    def __init__(self, stdin=None, stdout=None, prompt="› "):
        self.stdin, self.stdout = stdin or sys.stdin, stdout or sys.stdout
        self.prompt, self.buffer = prompt, ""
        self.tty = _isatty(self.stdin) and _isatty(self.stdout)
        self.saved = None
        self.lock = threading.RLock()
        self.shown = False
        self.prompting = True       # whether the prompt line is drawn (one message: only while something waits)
        # A character split across two reads (an emoji typed or pasted) decodes once, whole.
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def __enter__(self):
        if self.tty:
            import termios
            import tty
            try:
                fd = self.stdin.fileno()
                self.saved = termios.tcgetattr(fd)
                tty.setcbreak(fd)
                mode = termios.tcgetattr(fd)
                mode[3] &= ~termios.ECHO  # this prompt draws what you type itself
                termios.tcsetattr(fd, termios.TCSANOW, mode)
            except (termios.error, OSError, ValueError):
                self.saved = None
        return self

    def __exit__(self, *exc):
        self.clear()
        if self.saved is not None:
            import termios
            try:
                termios.tcsetattr(self.stdin.fileno(), termios.TCSADRAIN, self.saved)
            except (termios.error, OSError, ValueError):
                pass
        return False

    def write(self, text):
        try:
            self.stdout.write(text)
            self.stdout.flush()
        except (OSError, ValueError):
            pass

    def clear(self):
        with self.lock:
            if self.tty and self.shown:
                self.write("\r\033[2K")
            self.shown = False

    def show(self):
        with self.lock:
            if self.tty and self.prompting:
                self.write("\r\033[2K" + self.prompt + self.buffer)
                self.shown = True

    def print(self, text=""):
        with self.lock:
            self.clear()
            self.write(str(text) + "\n")
            self.show()

    def keys(self, timeout):
        """What was typed within ``timeout`` seconds ("" for nothing; None at the end of input)."""
        if not self.tty:
            line = self.stdin.readline()
            return None if line == "" else line if line.endswith("\n") else line + "\n"
        ready, _, _ = select.select([self.stdin], [], [], timeout)
        if not ready:
            return ""
        data = os.read(self.stdin.fileno(), 1024)
        return None if not data else self.decoder.decode(data)

    def discard_typeahead(self):
        """Forget every key typed before now that this prompt hasn't read yet (the terminal's input queue). Called
        once an approval's exact text is on screen: a y typed while Mobster worked, before anyone saw what it asks,
        must never answer it."""
        if not self.tty:
            return
        import termios
        try:
            termios.tcflush(self.stdin.fileno(), termios.TCIFLUSH)
        except (termios.error, OSError, ValueError):
            pass
        self.decoder.reset()


def _isatty(stream):
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


# -- the conversation -----------------------------------------------------------------------------------------

class Chat:
    def __init__(self, client, term, *, thread_id=None, device=None, color=None, interactive=True):
        from ..console import Palette, color_enabled
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
            renderer(event)  # Quick mode's events, one line per step (console.py)
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
            self.say("  · Mobster read your message", "faint")
        elif name == "steer_unread":
            self.note("Mobster finished before reading your message. Send it again to run it as a new task.")
        elif name == "user_message" and event.get("source") not in ("cli",):
            where = {"app": "in Mobster", "voice": "by voice", "tui": "in the terminal UI", "mcp": "from your agent"}
            self.say(f"  ↳ You wrote {where.get(event.get('source'), 'elsewhere')}: “{event.get('text')}”")

    def outcome(self, summary):
        """The task's end: its status, time and cost, then its answer (or why it stopped)."""
        from ..narrate import completion_message, seconds, status_label, status_tone
        status = summary.get("status") or "error"
        tone = status_tone(status)
        role = {"success": "green", "error": "red", "warning": "amber", "review": "accent"}.get(tone, "muted")
        glyph = {"success": "✓", "error": "✗", "warning": "!", "review": "?"}.get(tone, "■")
        facts = [seconds(summary.get("elapsed_ms"))] if summary.get("elapsed_ms") else []
        cost = summary.get("costUsd")
        if isinstance(cost, (int, float)) and cost > 0:
            facts.append(f"${cost:.4f}" if cost < 0.1 else f"${cost:.2f}")
        self.say(self.paint(glyph, role, "bold") + " " + self.paint(status_label(status), "bold")
                 + (self.paint("  " + " · ".join(facts), "faint") if facts else ""))
        data = summary.get("data")
        if isinstance(data, (dict, list)):
            body = json.dumps(data, indent=2, ensure_ascii=False)
        else:
            body = data if isinstance(data, str) and data.strip() else summary.get("answer")
        if body and status not in ("approval_denied", "stopped"):
            for line in str(body).splitlines():
                self.say("  " + line)
        else:
            message = completion_message(status, summary.get("reason"), summary.get("data_status"))
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
            if self.client.app_session(run_id):
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
        """A message that only asked Mobster to remember something: no task runs, and each suggestion shows here. On a
        terminal y or n answers them in turn; elsewhere each says where to answer it. Nothing is saved without a yes."""
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
        self.say("  " + ("It goes with every task (pinned)." if proposal.get("pin") else
                         "Mobster will use it in tasks it fits.") + " It stays on this Mac.", "faint")
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
        """A line typed while an approval or a question waits: True when it answered (or stopped). Only a line that
        is exactly y, n or x answers an approval; words never do."""
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
            if self.client.app_session(run_id):
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
        self.say(self.paint("Mobster", "accent", "bold") + self.paint("  conversation in Terminal · /help for "
                                                                      "commands · ctrl+d to leave", "faint"))
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

# The Mac app's words for the two answers (dashboard approval-prompt.tsx, approvalVerbs), so the terminal names the
# same buttons the app shows. An act this doesn't know keeps Approve and Decline.
_SENT, _POSTED = ("Send", "Don't send"), ("Post", "Don't post")
ACTS = {
    "send_message": _SENT, "send": _SENT, "reply": _SENT, "send message": _SENT,
    "post": _POSTED, "comment": _POSTED, "publish": ("Publish", "Don't publish"), "tweet": _POSTED,
    "pay": ("Pay", "Don't pay"), "transfer": ("Transfer", "Don't transfer"),
    "request_money": ("Request", "Don't request"), "donate": ("Donate", "Don't donate"), "tip": ("Tip", "Don't tip"),
    "order": ("Place order", "Don't order"), "place order": ("Place order", "Don't order"), "buy": ("Buy", "Don't buy"),
    "purchase": ("Buy", "Don't buy"), "checkout": ("Check out", "Don't check out"),
    "book": ("Book", "Don't book"), "reserve": ("Reserve", "Don't reserve"),
    "delete": ("Delete", "Don't delete"), "erase": ("Erase", "Don't erase"), "remove": ("Remove", "Don't remove"),
    "trash": ("Move to trash", "Keep it"), "clear": ("Clear", "Don't clear"),
    "follow": ("Follow", "Don't follow"), "unfollow": ("Unfollow", "Don't unfollow"), "share": ("Share", "Don't share"),
    "call": ("Call", "Don't call"), "dial": ("Call", "Don't call"), "facetime": ("Call", "Don't call"),
    "subscribe": ("Subscribe", "Don't subscribe"), "unsubscribe": ("Unsubscribe", "Don't unsubscribe"),
    "sign up": ("Sign up", "Don't sign up"), "request": ("Send request", "Don't send"),
    "react": ("React", "Don't react"),
    "add": ("Add", "Don't add"), "save": ("Save", "Don't save"), "archive": ("Archive", "Don't archive"),
    "submit": ("Submit", "Don't submit"), "confirm": ("Confirm", "Don't confirm"), "cancel": ("Cancel it", "Keep it"),
    "accept": ("Accept", "Don't accept"), "join": ("Join", "Don't join"), "leave": ("Leave", "Stay"),
    "block": ("Block", "Don't block"), "report": ("Report", "Don't report"), "install": ("Install", "Don't install"),
    "upload": ("Upload", "Don't upload"),
}
ASK = ("Approve", "Decline")


def verbs(approval):
    """(yes, no) for an approval, as the Mac app's buttons say them: the contract's act (Smart), else the first word
    of the control a Quick task would tap; a loop asks with Approve and Decline."""
    if approval.get("kind") == "loop":
        return ASK
    act = approval.get("act")
    if not act and approval.get("operation") == "TAP":
        act = (str(approval.get("label") or "").split() or [""])[0]
    raw = str(act or "").strip().lower()
    key = raw if raw in ACTS else " ".join(raw.replace("_", " ").replace("-", " ").split())
    if key in ACTS:
        return ACTS[key]
    if key == "cancel" or key.startswith("cancel "):
        return ACTS["cancel"]
    if key == "end call":
        return ("End call", "Stay on")
    return ASK


def approval_title(approval):
    """What Mobster asks to do, in its own words (the approval's title), else from the control it would tap."""
    if approval.get("title"):
        return str(approval["title"])
    from ..narrate import operation_words
    label = approval.get("label") or ""
    title = operation_words(approval.get("operation"))[0] + (f" “{label}”" if label else "")
    app = approval.get("app")
    return f"{title} in {app}" if app and app.casefold() not in title.casefold() else title


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


# -- one message ------------------------------------------------------------------------------------------------

def outcome(run, thread_id):
    summary = run.get("summary") or {}
    data = summary.get("data")
    out = {"threadId": thread_id, "runId": run.get("id"), "status": run.get("status"),
           "outcome": summary.get("outcome"), "answer": summary.get("answer"),
           "costUsd": run.get("costUsd"), "elapsedMs": summary.get("elapsed_ms")}
    if data is not None:
        out["data"] = data
    if run.get("status") not in DONE and summary.get("reason"):
        out["reason"] = summary["reason"]
    return out


def waiting(run, thread_id):
    approval = run.get("approval") or {}
    if approval.get("kind") == "clarify":
        return {"threadId": thread_id, "runId": run.get("id"), "status": "waiting_for_answer",
                "question": approval.get("question") or approval.get("label"),
                "choices": [c.get("label") for c in approval.get("choices") or ()]}
    out = {"threadId": thread_id, "runId": run.get("id"), "status": "waiting_for_approval",
           "approval": {"title": approval_title(approval), "text": approval.get("text"), "app": approval.get("app")}}
    return out


def once(client, chat, text, *, as_json, wait, out):
    """`mobster chat "message"`. Returns the exit code."""
    tty = chat.interactive
    if tty:
        response = chat.send(text)          # prints the task's first line and follows it
        if response is None:
            return EXIT_COULDNT
    else:
        body = {"text": text, **({"device": chat.device} if chat.device else {})}
        try:
            chat.thread_id, response = client.send(chat.thread_id, body)
        except ServiceError as error:
            if as_json:
                print(json.dumps({"status": "refused", "error": str(error), "code": error.code}), file=out,
                      flush=True)
            else:
                chat.say(str(error))
            return EXIT_COULDNT
        remember_thread(client.url, chat.thread_id)
    routed = routed_as(response)
    if routed == "remember":
        return offered(chat, response, tty=tty, as_json=as_json, wait=wait, out=out)
    run_id = (response.get("run") or {}).get("id") or (response.get("item") or {}).get("runId")
    if routed in ("steer", "answer"):
        if as_json:
            print(json.dumps({"threadId": chat.thread_id, "runId": run_id, "routed": routed}), file=out, flush=True)
        else:
            chat.say("  Mobster has your message; the task goes on. Follow it with: mobster chat --thread "
                     f"{chat.thread_id}", "faint")
        return EXIT_DONE
    if not tty:
        # --json, or no terminal: never answers an approval; waits until the task ends or needs someone.
        return finish(client.wait_run(run_id, wait), chat.thread_id, as_json, out, chat)
    # On a terminal: the steps as they happen; an approval waits for a typed y, n or x.
    deadline = time.monotonic() + wait
    term = chat.term
    try:
        while time.monotonic() < deadline:
            chat.pump(0.1)
            if bool(chat.pending) != term.prompting:
                term.clear()
                term.prompting = bool(chat.pending)
                term.show()
            done = chat.finished.get(run_id)
            if done is not None:
                return finish(done, chat.thread_id, False, out, chat, printed=True)
            if chat.pending and chat.pending.get("kind") == "clarify":
                chat.say(f"  Answer with: mobster chat --thread {chat.thread_id} \"your answer\"", "faint")
                return EXIT_NEEDS_YOU
            if chat.pending:
                chat.feed(chat.term.keys(0.1) or "")
    except KeyboardInterrupt:
        chat.stop()
        return 130
    chat.say(f"  Still working. Follow it with: mobster chat --thread {chat.thread_id}", "faint")
    return EXIT_RUNNING


def routed_as(response):
    """How Mobster took a message: new_run, steer, answer or remember. The item says so too, which an older
    Mobster's answer to a new conversation's first message leaves as the only sign."""
    return response.get("routed") or (response.get("item") or {}).get("routed") or "new_run"


def proposals_of(client, thread_id, response):
    """The suggestions to remember a message made, [{id, text, pin}]. Where the answer leaves them out (that older
    Mobster), the conversation's pending ones."""
    if "proposals" in response:
        return response.get("proposals") or []
    from urllib.parse import quote
    try:
        listed = client.call("GET", f"/api/memory/proposals?thread={quote(str(thread_id), safe='')}")["proposals"]
    except (ServiceError, NoService, KeyError, TypeError):
        return []
    return [{"id": p.get("id"), "text": p.get("text"), "pin": bool(p.get("pin"))} for p in listed
            if isinstance(p, dict)]


def where_to_remember(proposal):
    quoted = str(proposal.get("text") or "").replace('"', "'")
    return f'Answer it in Mobster for Mac, or save it with: mobster memory add "{quoted}"'


def offered(chat, response, *, tty, as_json, wait, out):
    """A message that only asked Mobster to remember something: no task started, so there's no run to follow.
    --json prints the offer; on a terminal y or n answers each suggestion until --wait runs out; elsewhere each one
    says where to answer it."""
    if not tty:
        proposals = proposals_of(chat.client, chat.thread_id, response)
        if as_json:
            print(json.dumps({"threadId": chat.thread_id, "status": "remember", "proposals": proposals},
                             ensure_ascii=False), file=out, flush=True)
        else:
            chat.offer(proposals)
        return EXIT_DONE
    # chat.send showed the first suggestion already.
    term, deadline = chat.term, time.monotonic() + wait
    try:
        while chat.offers and not chat.quit and time.monotonic() < deadline:
            if not term.prompting:
                term.prompting = True
                term.show()
            data = term.keys(0.1)
            if data is None:
                break
            chat.feed(data)
    except KeyboardInterrupt:
        return 130
    for proposal in chat.offers:
        chat.say("  " + where_to_remember(proposal), "faint")
    chat.offers = []
    return EXIT_DONE


def finish(run, thread_id, as_json, out, chat, printed=False):
    if run.get("finishedAt") is None:
        if run.get("approval"):
            state = waiting(run, thread_id)
            if as_json:
                print(json.dumps(state, ensure_ascii=False), file=out, flush=True)
            else:
                if state["status"] == "waiting_for_answer":
                    chat.note(chat.paint(str(state["question"]), "bold"), glyph="?", role="accent", indent="")
                    chat.say(f"  Answer with: mobster chat --thread {thread_id} \"your answer\"", "faint")
                else:
                    chat.show_approval(run["approval"])
                    chat.note("Waiting for your approval in Mobster (or in `mobster chat` on a terminal).")
            return EXIT_NEEDS_YOU
        if as_json:
            print(json.dumps({"threadId": thread_id, "runId": run.get("id"), "status": "running"}), file=out,
                  flush=True)
        else:
            chat.say(f"Still working. Follow it with: mobster chat --thread {thread_id}", "faint")
        return EXIT_RUNNING
    result = outcome(run, thread_id)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), file=out, flush=True)
    elif not printed:
        from ..narrate import completion_message, status_label
        chat.say(status_label(result["status"]), "bold")
        message = result.get("data") if isinstance(result.get("data"), str) else result.get("answer")
        if message:
            chat.say(str(message))
        elif result.get("reason"):
            chat.say(completion_message(result["status"], result["reason"]))
    return EXIT_DONE if result["status"] in DONE else EXIT_NOT_DONE


# -- entry point ------------------------------------------------------------------------------------------------

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
    if not text and not _isatty(stdin):
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
    elif not args.new:
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
    term.prompting = not text
    with term:
        chat = Chat(client, term, thread_id=thread_id, device=args.device, interactive=term.tty and not as_json)
        if text:
            if thread_id and not as_json:
                chat.say(f"Continuing your last conversation ({thread_id}); --new starts another.", "faint")
            try:
                return once(client, chat, text, as_json=as_json, wait=args.wait, out=stdout)
            except NoService as error:
                return fail(str(error), "not_running")
        if not term.tty:
            return fail("Say what Mobster should do: mobster chat \"What's on my calendar tomorrow?\"", "usage")
        return chat.repl()
