"""The terminal UI's slash commands and conversations."""

from rich.text import Text
from textual import work
from textual.containers import VerticalScroll
from textual.widgets import Input

from .. import chat_here, links
from ..app_choice import find_app, infer_app
from ..narrate import status_glyph, status_label, status_tone
from .keys import KEY_GROUPS
from .render import ACCENT, FAINT, GREEN, MUTED, QUATERNARY, TEXT, TONES, doctor_report
from .screens import InfoScreen, LoginScreen, Picker
from .widgets import NEXT_PLACEHOLDER, PLACEHOLDER, RunView, ago

FORMATS = ("auto", "text", "json", "yaml", "csv", "markdown")


class CommandMixin:
    """The ``/`` commands, and the conversations, history and device check they open."""

    def remember_thread(self, thread_id):
        try:
            from ..threads.recent import remember_thread
            remember_thread(chat_here.LOCAL, thread_id)
        except Exception:  # noqa: BLE001 -- -c falls back to a new conversation
            pass

    def continue_thread(self, thread_id=None):
        """`mobster -c`, or a conversation chosen in /threads: its tasks back in the transcript, the next task in it."""
        live = self.session.threads() if self.session is not None else None
        if live is None:
            if self.continue_requested and not self.demo:
                self.note("Conversations need Mobster's task history, which this window doesn't keep.", "warning")
            return
        if thread_id is None:
            try:
                from ..threads.recent import last_thread
                thread_id = last_thread(chat_here.LOCAL)
            except Exception:  # noqa: BLE001
                thread_id = None
        thread = live.store.get(thread_id) if thread_id else None
        if thread is None or thread.get("archived"):
            if self.continue_requested:
                self.note("No conversation to continue yet. Your next task starts one.")
            self.continue_requested = False
            return
        self.continue_requested = False
        self.thread_id = thread_id
        self.remember_thread(thread_id)
        for view in self.query(".run"):
            view.remove()
        self.run_view = None
        run_ids = live.store.run_ids(thread_id)
        shown = [self.session.runtime.runs.get(run_id) for run_id in run_ids][-5:]
        shown = [run for run in shown if run is not None]
        if shown:
            self.show_welcome(False)
        title = thread.get("title") or "your last conversation"
        self.notify(f"Continuing “{title}”. ctrl+n starts a new one.", timeout=4)
        for run in shown:
            view = RunView(run, self.apps, past=True)
            self.run_view = view
            self.replay(view, list(run.events))
        self.query_one("#prompt", Input).placeholder = NEXT_PLACEHOLDER

    def action_new_thread(self):
        self.command_new("")

    def action_threads(self):
        self.command_threads("")

    def command_new(self, argument):
        if self.running is not None:
            self.notify("A task is running. Stop it first (esc).", severity="warning", timeout=3)
            return
        self.thread_id = None
        for view in self.query(".run"):
            view.remove()
        self.run_view = None
        self.refresh_welcome()
        self.show_welcome(True)
        self.query_one("#prompt", Input).placeholder = PLACEHOLDER
        self.notify("New conversation: your next task starts it.", timeout=3)
        self.refresh_status()

    def command_threads(self, argument):
        if self.session is None:
            return
        if self.session.threads() is None:
            self.notify("Conversations need Mobster's task history, which this window doesn't keep.",
                        severity="warning", timeout=4)
            return
        threads = self.session.recent_threads()
        width = max(60, min(100, self.size.width - 8))
        options = []
        for thread in threads:
            prompt = Text(no_wrap=True, overflow="ellipsis")
            title = thread.get("title") or "New conversation"
            room = width - 22
            prompt.append(f"{title[:room - 1] + '…' if len(title) > room else title:<{room}}", style=TEXT)
            state = {"running": "Running", "waiting": "Waiting for you"}.get(thread.get("status") or "", "")
            prompt.append(f"{state:<16}", style=ACCENT)
            prompt.append(ago((thread.get("updatedAt") or 0) / 1000), style=FAINT)
            options.append((thread["id"], prompt, title))

        def chosen(key):
            if key:
                self.continue_thread(key)
        self.push_screen(Picker("Recent", options, "Search your conversations", "No conversations yet. Your next "
                                "task starts one.", width=width), chosen)

    def command_attach(self, argument):
        if not argument:
            self.notify("Name the file: /attach ~/Downloads/menu.pdf, or type @./ to pick one.", timeout=4)
            return
        self.attach(argument)

    @work(thread=True, group="attach")
    def attach(self, path):
        from .session import error_text
        try:
            saved = self.session.attach(path, thread_id=self.thread_id)
        except Exception as error:
            self.call_from_thread(self.note, error_text(error), "warning")
            return
        self.call_from_thread(self.attached, saved)

    def attached(self, saved):
        self.attachments.append({"id": saved["id"], "name": saved.get("name") or "file"})
        self.refresh_notice()

    def command_memory(self, argument):
        text = Text()
        try:
            from ..memory.store import default_store
            store = default_store()
            facts_list = store.list_facts() if store is not None and store.exists() else []
        except Exception:  # noqa: BLE001
            facts_list = []
        if not facts_list:
            text.append("Nothing yet. Tell Mobster something it should know, like where your gym is:\n", style=MUTED)
            text.append("  /remember My gym is the one on 5th Street\n", style=ACCENT)
        for fact in facts_list[:40]:
            text.append("  ● " if fact.get("pinned") else "  – ", style=ACCENT if fact.get("pinned") else FAINT)
            text.append(f"{fact.get('text')}\n", style=TEXT)
        if facts_list:
            text.append(f"\n{len(facts_list)} thing{'s' if len(facts_list) != 1 else ''}. ", style=MUTED)
            text.append("● pinned ones go with every task; the rest with tasks they fit. Everything stays on this "
                        "Mac.\n", style=FAINT)
        text.append("\nEdit them with ", style=FAINT)
        text.append("mobster memory", style=ACCENT)
        text.append(", or in Mobster for Mac › Settings › Memory.", style=FAINT)
        self.push_screen(InfoScreen(text, "What Mobster remembers"))

    def command_remember(self, argument):
        if not argument:
            self.notify("Say what to remember: /remember My gym is the one on 5th Street", timeout=4)
            return
        self.remember_fact(argument)

    @work(thread=True, group="memory")
    def remember_fact(self, text):
        if self.demo:
            self.call_from_thread(self.note, "The demo doesn't save anything; `mobster` would remember this.")
            return
        try:
            from ..memory.store import default_store
            default_store().add_fact(text)
        except Exception as error:  # noqa: BLE001 -- the store's own sentence
            self.call_from_thread(self.note, str(error), "warning")
            return
        self.call_from_thread(self.note, f"Mobster will remember: {text}")

    def command_login(self, argument):
        def done(provider):
            if provider:
                from ..login import PROVIDERS
                self.notify(f"{PROVIDERS[provider]['name']} is connected. Your key stays on this Mac.", timeout=4)
                self.poll_status()
        self.push_screen(LoginScreen(), done)

    def run_command(self, name, argument):
        handler = getattr(self, f"command_{name}", None)
        if handler is None:
            self.notify(f"There's no /{name}. Type / to see every command.", severity="warning", timeout=3)
            return
        handler(argument)

    def command_quit(self, argument):
        self.exit()

    def command_help(self, argument):
        from rich.console import Group
        from rich.table import Table
        groups = []
        for title, pairs in KEY_GROUPS:
            grid = Table.grid(padding=(0, 2))
            grid.add_column(width=11, no_wrap=True)
            grid.add_column()
            for key, words in pairs:
                dim = title == "Task" and key == "y / n" and self.running is None
                grid.add_row(Text(key, style=QUATERNARY if dim else ACCENT), Text(words, style=FAINT if dim else MUTED))
            groups += [Text(title, style=f"bold {TEXT}"), grid, Text("")]
        approvals = Text()
        approvals.append("Approvals\n", style=f"bold {TEXT}")
        approvals.append("With “Ask before acting” (shift+tab switches it with Full access), Mobster stops before "
                         "anything with an effect outside your iPhone, shows you the action and the exact text, and "
                         "waits for your answer. In every mode it asks before it types a one-time code. Bypass lets "
                         "it act when its own check is unsure; approvals still apply.", style=MUTED)
        docs = Text()
        docs.append("\nEvery command: type /     Docs  ", style=FAINT)
        docs.append(links.TUI, style=ACCENT)
        body = Group(*groups, approvals, docs)
        self.push_screen(InfoScreen(body, "Keys"))

    def command_ask(self, argument):
        self.action_toggle_ask()
        self.notify("Full access: Mobster doesn't ask" if not self.settings["askBeforeActing"] else
                    "Ask before every change" if self.settings.get("permissionMode") == "ask_all" else
                    "Ask before acting", timeout=2)

    def command_cap(self, argument):
        if self.session is None:
            return
        value = argument.strip().lstrip("$").lower()
        try:
            self.session.set_spend_cap(None if value in {"", "off", "none"} else float(value))
        except (ValueError, TypeError) as error:
            self.notify(f"Spend cap: {error}", severity="error", timeout=4)
            return
        cap = self.session.spend_cap_usd
        self.notify(f"Spend cap: ${cap:.2f} per task" if cap is not None else "No spend cap", timeout=3)
        self.refresh_status()

    def command_format(self, argument):
        value = argument.strip().lower()
        if value not in FORMATS:
            self.notify("Formats: " + ", ".join(FORMATS), severity="warning", timeout=4)
            return
        self.output_format = value
        self.notify(f"Answers come back as {value}.", timeout=2)

    def command_details(self, argument):
        self.action_toggle_details()

    def command_screen(self, argument):
        self.action_toggle_screen()

    def command_clear(self, argument):
        self.action_clear()

    def command_app(self, argument):
        if argument:
            app = find_app(argument, self.apps) or infer_app(argument, self.apps)
            if app is None:
                self.notify(f"No app called {argument}.", severity="warning", timeout=3)
                return
            self.app_choice = app
            self.refresh_chip()
            return
        options = []
        for app in self.apps:
            prompt = Text()
            prompt.append(f"{app['name']:<22}", style=TEXT)
            prompt.append(app["bundleId"], style=FAINT)
            if app.get("installed"):
                prompt.append("  installed", style=GREEN)
            options.append((app["id"], prompt, f"{app['name']} {app['bundleId']}"))

        def chosen(key):
            if key:
                self.app_choice = next((a for a in self.apps if a["id"] == key), None)
                self.refresh_chip()
        self.push_screen(Picker("Choose the app", options, "Type an app name or bundle id"), chosen)

    def command_history(self, argument):
        if self.session is None:
            return
        runs = self.session.history()
        width = max(60, min(110, self.size.width - 8))
        goal_width = width - 2 - 13 - 22 - 12 - 6
        options = []
        for run in runs:
            tone = TONES.get(status_tone(run.status), MUTED)
            prompt = Text(no_wrap=True, overflow="ellipsis")
            prompt.append(status_glyph(status_tone(run.status), "○" if run.status in {"blocked", "no_progress"}
                                       else "■")
                          + " ", style=tone)
            goal = run.goal if len(run.goal) <= goal_width else run.goal[:goal_width - 1] + "…"
            prompt.append(f"{goal:<{goal_width + 1}}", style=TEXT)
            app = run.app["name"] if run.app.get("bundleId") else ""
            prompt.append(f"{app[:12]:<13}", style=MUTED)
            prompt.append(f"{status_label(run.status)[:21]:<22}", style=tone)
            prompt.append(ago(run.created_at), style=FAINT)
            options.append((run.id, prompt, f"{run.goal} {app} {run.status} {run.id}"))

        def chosen(key):
            if key:
                self.resume(key)
        self.push_screen(Picker(f"History  ({len(runs)} tasks, newest first)", options, "Type to filter",
                                "No tasks yet.", width=width), chosen)

    def command_resume(self, argument):
        if not argument:
            self.command_history("")
            return
        self.resume(argument)

    def resume(self, identifier):
        """Show a past task's transcript and put its request back in the prompt to run again."""
        if self.session is None:
            return
        history = self.session.history()
        if identifier == "latest":
            run = history[0] if history else None
        else:
            run = next((r for r in history if r.id == identifier), None) or self.session.find(identifier)
        if run is None:
            self.notify(f"No task {identifier} in history.", severity="warning", timeout=3)
            return
        if self.running is not None:
            self.notify("A task is running. Stop it first.", severity="warning", timeout=3)
            return
        self.show_welcome(False)
        view = RunView(run, self.apps, past=True)
        self.run_view = view
        self.replay(view, list(run.events))
        self.app_choice = next((a for a in self.apps if a["id"] == run.app["id"]), None) if run.app.get("bundleId") \
            else None
        prompt = self.query_one("#prompt", Input)
        prompt.value = run.goal
        prompt.cursor_position = len(prompt.value)
        prompt.focus()
        self.refresh_chip()
        self.refresh_status()

    @work(group="replay")
    async def replay(self, view, events):
        """Mount a past task's view, then feed it its events, so its prompt line comes first."""
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(view)
        for event in events:
            view.feed(event)
        transcript.scroll_end(animate=False)
        self.refresh_status()
        self.poll_phone()

    def command_device(self, argument):
        """/device: the old name of /devices, kept so it still works."""
        self.command_devices(argument)

    def command_devices(self, argument):
        screen = InfoScreen(Text("Checking…", style=FAINT), "Your iPhone")
        self.push_screen(screen)
        self.check_device(screen)

    @work(thread=True, group="doctor")
    def check_device(self, screen):
        from ..doctor import run_checks
        if self.demo:
            text = Text()
            text.append("Scripted demo phone. ", style=f"bold {ACCENT}")
            text.append("No iPhone and no model is used. Leave with ctrl+d, then run `mobster` to use yours.\n",
                        style=MUTED)
            self.call_from_thread(screen.update_body, text)
            return
        checks = run_checks(wda_url=self.session.wda_url if self.session else None)
        body = doctor_report(checks)
        body.append("\n\nAddress  ", style=FAINT)
        body.append((self.session.wda_url or "") if self.session else "", style=MUTED)
        body.append("\nSet up your iPhone: ", style=FAINT)
        body.append("mobster setup", style=ACCENT)
        body.append(" in another terminal, or Mobster for Mac.", style=FAINT)
        self.call_from_thread(screen.update_body, body)
