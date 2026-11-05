"""The terminal UI's tasks: starting one, steering it, answering its approvals."""

from rich.text import Text
from textual import work
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Input, Static

from ..narrate import status_tone
from .render import ACCENT, FAINT, GREEN, MUTED, RED, TEXT
from .screens import AskScreen
from .widgets import NEXT_PLACEHOLDER, PLACEHOLDER, STEER_PLACEHOLDER, ApprovalCard, Composer, RunView


class TaskMixin:
    """Running a task for the app: submit it on a worker, show its events, steer it, answer its approvals."""

    def start_task(self, app, goal):
        if app is not None:
            self.last_app = app
        self.app_choice = None
        self.query_one("#composer", Composer).add_class("busy")
        attachments = [item["id"] for item in self.attachments]
        self.attachments = []
        self.refresh_notice()
        self.submit_task(app, goal, self.output_format, attachments)

    def default_app(self):
        """Where a task that names no app starts: the Home Screen; Settings for the demo, which needs an app."""
        if self.demo:
            return self.last_app or next(a for a in self.apps if a["id"] == "settings")
        return None

    @work(thread=True, group="run")
    def submit_task(self, app, goal, output_format, attachments=()):
        from .session import error_text
        app = app or self.default_app()
        app_id = app["id"] if app else None
        try:
            threads = self.session.threads()
            if threads is not None:
                thread_id = self.thread_id or self.session.new_thread()["id"]
                if thread_id != self.thread_id:
                    self.thread_id = thread_id
                    self.remember_thread(thread_id)
                routed, run, response = self.session.send(thread_id, goal, app_id=app_id, attachments=attachments,
                                                           output_format=output_format)
                if routed == "remember":
                    self.call_from_thread(self.offer_to_remember, goal, response.get("proposals") or [])
                    return
                if run is None:
                    self.call_from_thread(self.task_idle)
                    return
            else:
                if attachments:
                    raise ValueError("Attaching files needs Mobster's task history, which this window doesn't keep.")
                offered = self.remember_here(goal)
                if offered is not None:
                    self.call_from_thread(self.offer_to_remember, goal, offered)
                    return
                run = self.session.start(app_id, goal, output_format=output_format)
        except Exception as error:
            self.call_from_thread(self.task_refused, app, goal, error_text(error))
            return
        self.call_from_thread(self.task_started, run)
        self.session.follow(run, lambda event: self.call_from_thread(self.task_event, run, event),
                            stop=self.follow_stop)

    def remember_here(self, goal):
        """Without conversations (the demo): a "Remember that …" message becomes proposals,
        else None to run it as a task."""
        from ..memory import extract
        from ..memory.store import default_store
        found = extract.remember_only(goal)
        if not found:
            return None
        if self.demo:
            return [{"id": None, "text": candidate.text, "pin": candidate.pin} for candidate in found]
        store = default_store()
        if store is None:
            return None
        made = []
        for candidate in found:
            proposal = store.propose(candidate.text, "global", pin=candidate.pin)
            if proposal is not None:
                made.append({"id": proposal["id"], "text": proposal["text"], "pin": proposal["pin"]})
        return made

    def task_idle(self):
        self.query_one("#composer", Composer).remove_class("busy")

    def offer_to_remember(self, goal, proposals):
        """A message that only asked Mobster to remember something: one card per suggestion, Remember or Not now."""
        self.task_idle()
        self.show_welcome(False)
        transcript = self.query_one("#transcript", VerticalScroll)
        line = Text()
        line.append("❯ ", style=f"bold {ACCENT}")
        line.append(goal, style=f"bold {TEXT}")
        box = Static(line, classes="run")
        transcript.mount(box)
        if not proposals:
            self.note("Mobster already has this.")
            return
        pending = list(proposals)

        def ask_next():
            if not pending:
                return
            proposal = pending.pop(0)
            body = (f"“{proposal['text']}”\n\n" + ("It goes with every task (pinned). " if proposal.get("pin") else
                                                    "Mobster will use it in tasks it fits. ")
                    + "It stays on this Mac.")

            def answered(yes):
                self.answer_proposal(proposal, bool(yes))
                ask_next()
            self.push_screen(AskScreen("Remember this?", body, "Remember", "Not now"), answered)
        ask_next()

    @work(thread=True, group="memory")
    def answer_proposal(self, proposal, yes):
        message = "Mobster will remember this." if yes else "Not remembered."
        try:
            from ..memory.store import default_store
            store = default_store()
            if proposal.get("id"):
                store.resolve_proposal(proposal["id"], yes)
            elif yes and not self.demo:
                store.add_fact(proposal["text"], pinned=bool(proposal.get("pin")))
            elif yes and self.demo:
                message = "The demo doesn't save anything; `mobster` would remember this."
        except Exception as error:  # noqa: BLE001 -- the store's own sentence
            message = str(error)
        self.call_from_thread(self.proposal_answered, yes, message)

    def proposal_answered(self, yes, message):
        text = Text()
        text.append("  ✓ " if yes else "  – ", style=GREEN if yes else FAINT)
        text.append(message, style=MUTED)
        line = Static(text, classes="item")
        self.query_one("#transcript", VerticalScroll).mount(line)
        self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)

    def steer(self, text):
        """A message to the running task: it reads it before its next step (Mobster's agent only)."""
        run = self.running
        if run is None or self.session is None:
            return
        if self.run_view is not None:
            self.run_view.note(f"While Mobster worked, you said “{text}”", MUTED)
        self.send_steer(run, text)

    @work(thread=True, group="steer")
    def send_steer(self, run, text):
        from .session import error_text
        try:
            if self.thread_id and self.session.threads() is not None:
                self.session.send(self.thread_id, text)
            else:
                run.steer(text, source="tui")
        except Exception as error:
            self.call_from_thread(self.note, error_text(error), "warning")

    def task_refused(self, app, goal, message):
        self.task_idle()
        transcript = self.query_one("#transcript", VerticalScroll)
        view = Static(classes="run")
        text = Text()
        text.append("❯ ", style=f"bold {ACCENT}")
        text.append(goal, style=f"bold {TEXT}")
        if app:
            text.append(f"  {app['name']}", style=FAINT)
        text.append("\n\n")
        text.append("✗ Couldn't start  ", style=f"bold {RED}")
        text.append(message + "\n", style=TEXT)
        if self.needs_setup():
            text.append("\n")
            text.append_text(self.setup_hints())
        prompt = self.query_one("#prompt", Input)
        if not prompt.value.strip():
            # The task waits in the box, so enter runs it once what was missing is there.
            prompt.value = goal
            prompt.cursor_position = len(goal)
            text.append("\nYour task is back in the box: press enter when you're set.\n", style=FAINT)
        view.update(text)
        self.show_welcome(False)
        transcript.mount(view)
        transcript.scroll_end(animate=False)
        self.recall_index = None

    def task_started(self, run):
        self.show_welcome(False)
        self.running = run
        self.run_view = RunView(run, self.apps)
        self.finished_views.append(self.run_view)
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.mount(self.run_view)
        transcript.scroll_end(animate=False)
        prompt = self.query_one("#prompt", Input)
        prompt.placeholder = STEER_PLACEHOLDER if run.steerable else "Mobster is working… esc stops it"
        self.refresh_chip()
        self.refresh_status()
        self.poll_phone()

    def task_event(self, run, event):
        view = self.run_view
        if view is None or view.run is not run:
            return
        changed = view.feed(event)
        kind = event.get("event")
        if kind == "approval_requested":
            request = self.session.pending_approval(run)
            if request:
                self.show_approval(request)
        elif kind == "approval_resolved":
            self.hide_approval()
        elif kind == "run_finished":
            self.task_finished(run)
        if changed:
            transcript = self.query_one("#transcript", VerticalScroll)
            if transcript.max_scroll_y - transcript.scroll_y < 6 or kind == "run_finished":
                transcript.scroll_end(animate=False)
        if kind in {"observation", "observation_after_action", "run_finished"} or (
                not self.image_class and kind in {"decision", "action_started", "action_acknowledged"}):
            self.poll_phone()
        self.refresh_status()

    def task_finished(self, run):
        self.running = None
        self.tasks_done += 1
        self.hide_approval()
        self.task_idle()
        status = run.status
        if self.bell_enabled and status_tone(status) in {"error", "warning"}:
            self.bell()
        self.refresh_status()
        self.refresh_chip()
        self.query_one("#prompt", Input).focus()
        if status == "approval_denied":
            self.query_one("#prompt", Input).placeholder = "Tell Mobster what to do instead…  (↑ for the last task)"
        else:
            self.query_one("#prompt", Input).placeholder = NEXT_PLACEHOLDER if self.thread_id else PLACEHOLDER

    def approval_open(self):
        try:
            return self.query_one(ApprovalCard).has_class("open")
        except NoMatches:
            return False

    def show_approval(self, request):
        card = self.query_one(ApprovalCard)
        card.ask(request)
        card.add_class("open")
        card.focus()
        self.refresh_status()
        if self.bell_enabled:
            self.bell()

    def hide_approval(self):
        card = self.query_one(ApprovalCard)
        if card.has_class("open"):
            card.remove_class("open")
            card.request = None
            self.query_one("#prompt", Input).focus()
            self.refresh_status()

    def answer_approval(self, request, choice):
        if not request or self.session is None:
            return
        run = self.running
        approve = choice != "__no"
        picked = None if choice in {"__yes", "__always", "__no"} else choice
        self.hide_approval()
        self.send_answer(run, request["id"], approve, picked, remember=choice == "__always")

    @work(thread=True, group="answer")
    def send_answer(self, run, approval_id, approve, choice, remember=False):
        try:
            self.session.answer(approval_id, approve, choice, run=run, remember=remember)
        except Exception as error:
            from .session import error_text
            self.call_from_thread(self.note, error_text(error), "error")

    def note(self, message, tone="neutral"):
        self.notify(message, severity={"warning": "warning", "error": "error"}.get(tone, "information"),
                    timeout=4)
