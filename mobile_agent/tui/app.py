"""The Mobster terminal UI: a Textual app over ``session.Session``."""

import asyncio
import threading
import time

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Input, OptionList, Static

from ..app_choice import find_app
from ..catalog import APPS
from ..narrate import Approval, Launch, Outcome, Step, facts, status_glyph
from .chrome import PLUG_IN, ChromeMixin
from .commands import CommandMixin
from .prompt import PromptMixin
from .render import APPROVALS
from .tasks import TaskMixin
from .theme import APP_CSS, THEME
from .widgets import ApprovalCard, Composer, PhonePanel, RunView

PHONE_MIN_WIDTH = 110  # the phone panel shows by default from this terminal width
TALL = 30  # from this many rows the header sits one row under the top edge


class MobsterApp(ChromeMixin, PromptMixin, TaskMixin, CommandMixin, App):
    TITLE = "Mobster"
    ENABLE_COMMAND_PALETTE = False
    CSS = APP_CSS

    BINDINGS = [
        Binding("ctrl+c", "interrupt", show=False, priority=True),
        Binding("ctrl+d", "quit_now", show=False, priority=True),
        Binding("escape", "escape", show=False),
        Binding("shift+tab", "toggle_ask", show=False, priority=True),
        Binding("ctrl+o", "toggle_details", show=False, priority=True),
        Binding("ctrl+s", "toggle_screen", show=False, priority=True),
        Binding("ctrl+r", "history", show=False, priority=True),
        Binding("ctrl+l", "clear", show=False, priority=True),
        Binding("ctrl+n", "new_thread", show=False, priority=True),
        Binding("ctrl+t", "threads", show=False, priority=True),
    ]

    def __init__(self, session_factory, *, demo=False, resume=None, app_id=None, bell=True,
                 image_class=None, task=None, continue_=False, thread_id=None):
        super().__init__()
        self.session_factory = session_factory
        # A textual-image widget class when this terminal shows images (tui.images), else None: text.
        self.image_class = image_class
        self.session = None
        self.demo = demo
        self.resume_id = resume
        self.bell_enabled = bell
        self.apps = [dict(app) for app in APPS]
        self.app_choice = find_app(app_id, self.apps) if app_id else None
        # An --app the catalog does not know (an app installed on the phone, or a typo) waits for the phone's
        # own list (apps_ready), and is reported if nothing there matches: never dropped without a word. A list
        # without the phone's apps (WDA still starting) rules nothing out: it is read again once the phone is ready.
        self.requested_app = app_id if app_id and self.app_choice is None else None
        self.requested_app_waiting = self.requested_app_reread = False
        self.last_app = self.app_choice
        self.status = {}
        from ..permissions import from_env as permission_settings
        mode = permission_settings().mode
        self.settings = {"askBeforeActing": mode != "dont_ask", "permissionMode": mode}
        self.settings_writer = asyncio.Lock()
        self.setting_requests = {}
        self.output_format = "auto"
        self.expanded = False
        self.frame = 0
        self.run_view = None
        self.finished_views = []  # this session's tasks, for the summary printed after the UI closes
        self.running = None
        self.follow_stop = threading.Event()
        self.recalls = []
        self.recall_index = None
        self.quit_armed = 0.0
        self.phone_visible = None
        self.phone_busy = False
        self.connect_error = None
        # Conversations: the one tasks go to (None: a new one starts with the next task), -c's request, and the
        # files attached for the next task.
        self.thread_id = thread_id
        self.continue_requested = bool(continue_)
        self.attachments = []
        self.initial_task = task
        self.phone = None       # (state, words, name) from the device list: the checklist's iPhone row
        self.tasks_done = 0
        self.update_line = ""

    def call_from_thread(self, callback, *args, **kwargs):
        """Discard worker UI updates after shutdown, including callbacks queued before widgets were removed."""
        if not self.is_running:
            return None

        def deliver():
            if self.is_running:
                return callback(*args, **kwargs)
            return None

        try:
            return super().call_from_thread(deliver)
        except RuntimeError:
            if self.is_running:
                raise
            return None  # the event loop stopped between the check and dispatch

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Horizontal(id="body"):
            with VerticalScroll(id="transcript", classes="idle"):
                yield Static(self.welcome(), id="welcome")
            yield PhonePanel(self.image_class, id="phone")
        with Vertical(id="bottom"):
            yield OptionList(id="suggest")
            yield Static(id="suggest-hint")
            yield ApprovalCard()
            yield Static(id="notice")
            yield Composer(id="composer")
            yield Static(id="status")

    def on_mount(self):
        self.register_theme(THEME)
        self.theme = "mobster"
        prompt = self.query_one("#prompt", Input)
        prompt.focus()
        if self.initial_task:
            prompt.value = self.initial_task
            prompt.cursor_position = len(prompt.value)
        self.refresh_chrome()
        self.set_interval(.1, self.tick)
        self.set_interval(2, self.poll_status)
        self.set_interval(1.5, self.poll_phone)
        self.open_session()
        if not self.demo:
            self.check_for_update()

    def on_resize(self, event):
        self.screen.set_class(event.size.height >= TALL, "tall")
        if self.phone_visible is None:
            self.query_one("#phone").set_class(event.size.width < PHONE_MIN_WIDTH, "hidden")
        self.call_after_refresh(self.poll_phone)
        self.call_after_refresh(self.refresh_layout)  # after the resize lands, so self.size is the new one

    def refresh_layout(self):
        """The parts that fit themselves to the terminal: the header and the welcome screen."""
        try:
            self.refresh_header()
            self.refresh_welcome()
        except NoMatches:
            pass

    @work(thread=True, exclusive=True, group="session")
    def open_session(self):
        try:
            session = self.session_factory()
        except Exception as error:
            from .session import error_text
            self.call_from_thread(self.session_failed, error_text(error))
            return
        self.session = session
        status = settings = None
        try:
            settings = session.settings()
            status = session.status()
        except Exception:
            pass
        phone = self.read_phone()
        self.call_from_thread(self.session_ready, status, settings, phone)
        try:
            apps = session.apps()
        except Exception:
            apps = None
        self.call_from_thread(self.apps_ready, apps or self.apps)

    def session_failed(self, message):
        self.connect_error = message
        self.refresh_chrome()
        self.refresh_welcome()

    def session_ready(self, status, settings, phone=None):
        if status:
            self.status = status
        if settings:
            self.settings.update(settings)
        self.phone = phone
        history = self.session.history()
        self.recalls = [run.goal for run in reversed(history)][-200:]
        self.tasks_done = sum(1 for run in history if run.finished_at is not None)
        self.refresh_chrome()
        self.refresh_welcome()
        self.poll_phone()
        if self.continue_requested or self.thread_id:
            self.continue_thread(self.thread_id)
        if self.resume_id:
            self.resume(self.resume_id)
        if self.initial_task:
            self.start_initial_task()

    def start_initial_task(self):
        """`mobster "TASK"`: run it now when Mobster can."""
        prompt = self.query_one("#prompt", Input)
        if prompt.value.strip() != (self.initial_task or "").strip():
            return  # changed meanwhile: it's the person's now
        if self.demo or not self.needs_setup():
            self.initial_task = None
            self.submit_value(prompt.value)
        else:
            self.note("Your task is in the box. It runs when the checklist above is done: press enter then.")
            self.initial_task = None

    @work(thread=True, exclusive=True, group="apps")
    def fetch_apps(self):
        """The phone's apps, read now rather than from the cache: the phone has just become ready."""
        try:
            apps = self.session.apps(fresh=True)
        except Exception:
            apps = None
        self.call_from_thread(self.apps_ready, apps or self.apps)

    def apps_ready(self, apps):
        self.apps = apps
        if self.app_choice:
            self.app_choice = next((a for a in apps if a["id"] == self.app_choice["id"]), self.app_choice)
        if self.requested_app:
            self.resolve_requested_app()

    def phone_listed_apps(self):
        """Whether ``self.apps`` holds the phone's own list (its installed apps are marked), not the catalog alone."""
        return self.demo or any(app.get("foundOnPhone") or app.get("installed") is not None for app in self.apps)

    def resolve_requested_app(self):
        """Choose the app --app named, or say why not, once the phone's own list can tell."""
        wanted = self.requested_app
        if self.app_choice is not None:
            self.requested_app = None  # chosen since, with /app or @
            return
        found = find_app(wanted, self.apps)
        if found is None and not self.phone_listed_apps():
            if not self.status.get("device_ready"):
                if not self.requested_app_waiting:
                    self.requested_app_waiting = True
                    self.note(f"Couldn't read the phone's apps yet. Mobster looks for {wanted} when the phone "
                              "answers.")
                return  # status_ready reads them again when the phone is ready
            if not self.requested_app_reread:
                self.requested_app_reread = True
                self.fetch_apps()
                return
            self.requested_app = None
            self.note(f"Couldn't read the phone's apps to find {wanted}. Mobster picks the app from each task; "
                      "/app chooses one.", "warning")
            self.refresh_chip()
            return
        self.requested_app = None
        self.app_choice = found
        if found is None:
            self.note(f"No app called {wanted} on this phone. Mobster picks the app from each task; "
                      "/app chooses one.", "warning")
        self.refresh_chip()

    def poll_status(self):
        if self.session is not None and not self.status_polling:
            self.fetch_status()

    status_polling = False

    def read_phone(self):
        """(state, words, name) for the checklist's iPhone row, from the device list; None in the demo."""
        if self.demo or self.session is None:
            return None
        from ..native_helpers import devices_allowed
        if not devices_allowed():  # MOBSTER_NO_DEVICES=1, as in local CI: never list a phone that's plugged in
            return ("unplugged", PLUG_IN, None)
        try:
            from .. import devices
            found = devices.discover(probe=False)
        except Exception:
            return None
        url = (self.session.wda_url or "").rstrip("/")
        mine = next((item for item in found if (item.get("wdaUrl") or "").rstrip("/") == url), None)
        usb = [item for item in found if item.get("kind") == "usb"]
        item = mine or (usb[0] if usb else None)
        if item is None:
            return ("unplugged", PLUG_IN, None)
        name = item.get("name")
        state = item.get("state")
        reason = (item.get("reason") or "").lower()
        if state == "needs_setup" and "trust" in reason:
            return ("trust", "tap Trust on it", name)
        if state == "needs_setup":
            return ("setup", "run mobster setup, or set it up in Mobster for Mac", name)
        if state == "disconnected":
            return ("unplugged", PLUG_IN, name)
        if "lock" in reason:
            return ("locked", "unlock it", name)
        return ("starting", "starting Mobster's helper on it", name)

    @work(thread=True, group="status")
    def fetch_status(self):
        self.status_polling = True
        try:
            status = self.session.status()
            phone = None if status.get("device_ready") else self.read_phone()
        except Exception:
            status, phone = None, None
        finally:
            self.status_polling = False
        if status:
            self.call_from_thread(self.status_ready, status, phone)

    def status_ready(self, status, phone=None):
        was_ready = self.status.get("device_ready")
        was_setup = self.needs_setup()
        self.status = status
        self.phone = phone
        if not self.query("#header"):
            return  # the UI is closing: a status fetch still in flight lands after its widgets are gone
        self.refresh_chrome()
        if self.requested_app and status.get("device_ready") and not was_ready:
            # The phone has just answered: its apps can be read now, for the app --app named.
            self.requested_app_reread = True
            self.fetch_apps()
        if self.run_view is None:
            self.refresh_welcome()
        if was_setup and not self.needs_setup():
            prompt = self.query_one("#prompt", Input)
            if prompt.value.strip() and self.running is None:
                self.note("Everything's ready. Press enter to run your task.")

    def tick(self):
        if not self.is_running or self.running is None:
            return
        self.frame += 1
        if self.run_view is not None:
            for view in self.run_view.views.values():
                if view.live:
                    view.redraw(self.frame)
        if self.frame % 5 == 0:
            self.refresh_status()
        try:
            card = self.query_one(ApprovalCard)
        except NoMatches:  # the timer fires once more while the app closes mid-task, its widgets gone
            return
        if card.has_class("open") and self.frame % 10 == 0:
            card.redraw()

    @on(Input.Changed, "#prompt")
    def prompt_changed(self, event):
        self.refresh_chip()
        self.suggest(event.value)

    @on(OptionList.OptionSelected, "#suggest")
    def suggestion_clicked(self):
        self.accept_suggestion()
        self.query_one("#prompt", Input).focus()

    @on(Input.Submitted, "#prompt")
    def prompt_submitted(self, event):
        value = event.value.strip()
        if not value:
            return
        if value.startswith("/"):
            name, _, argument = value[1:].partition(" ")
            event.input.value = ""
            self.run_command(name.lower(), argument.strip())
            return
        self.submit_value(value)

    def submit_value(self, value):
        prompt = self.query_one("#prompt", Input)
        if self.running is not None:
            prompt.value = ""
            self.steer(value)
            return
        if self.session is None:
            self.note(self.connect_error or "Still starting. Try again in a moment.", "warning")
            return
        if self.awaiting_requested_app(value):
            # Never run it in an inferred app while the app --app named may still be on the phone.
            self.note(f"Still looking for {self.requested_app} on the phone. Try again when it is ready, or "
                      "choose an app with /app.", "warning")
            return
        app = self.target_app(value)
        goal = self.split_mentions(value)[0] or value
        prompt.value = ""
        self.recalls.append(value)
        self.recall_index = None
        self.start_task(app, goal)

    def action_interrupt(self):
        prompt = self.query_one("#prompt", Input)
        if self.running is not None:
            self.stop_task()
            return
        if prompt.value:
            prompt.value = ""
            return
        now = time.monotonic()
        if now - self.quit_armed < 2:
            self.exit()
            return
        self.quit_armed = now
        self.notify("Press ctrl+c again to quit", timeout=2)

    def action_quit_now(self):
        self.exit()

    def action_escape(self):
        if self.suggestions_open():
            self.close_suggestions()
        elif self.running is not None:
            self.stop_task()
        else:
            self.query_one("#prompt", Input).value = ""

    def stop_task(self):
        run = self.running
        if run is None or self.session is None:
            return
        self.notify("Stopping at the next safe point…", timeout=3)
        threading.Thread(target=self.session.stop, args=(run,), daemon=True).start()

    def action_toggle_ask(self):
        self.change_setting("askBeforeActing", not self.settings.get("askBeforeActing", True))

    def change_setting(self, name, value):
        self.settings[name] = value
        if name == "askBeforeActing":
            # The older switch sets the mode (the server keeps a stricter one when it goes back on).
            from ..permissions.policy import mode_for_ask
            self.settings["permissionMode"] = mode_for_ask(value, self.settings.get("permissionMode"))
        self.refresh_status()
        if self.session is not None:
            request = self.setting_requests[name] = object()
            self.save_setting(name, value, request)
        if self.running is not None:
            self.notify("Applies from the next task.", timeout=3)

    @work(group="settings")
    async def save_setting(self, name, value, request):
        async with self.settings_writer:
            if self.setting_requests.get(name) is not request:
                return  # A newer choice already replaced this queued save.
            try:
                settings = await asyncio.to_thread(self.session.set_setting, name, value)
            except Exception as error:
                from .session import error_text
                self.note(error_text(error), "error")
                try:
                    settings = await asyncio.to_thread(self.session.settings)
                except Exception:
                    return  # The save error is already shown; a closed service cannot provide a rollback.
            if self.setting_requests.get(name) is request:
                self.settings[name] = settings[name]
                if name == "askBeforeActing":
                    self.settings["permissionMode"] = settings["permissionMode"]
            self.refresh_status()

    def action_toggle_details(self):
        self.expanded = not self.expanded
        for view in self.query(RunView):
            view.redraw()

    def action_toggle_screen(self):
        phone = self.query_one("#phone")
        self.phone_visible = phone.has_class("hidden")
        phone.set_class(not self.phone_visible, "hidden")
        if self.phone_visible:
            self.poll_phone()

    def action_history(self):
        self.run_command("history", "")

    def action_clear(self):
        for view in self.query(".run"):
            if view is not self.run_view or self.running is None:
                view.remove()
        if self.running is None:
            self.run_view = None
            self.refresh_welcome()
            self.show_welcome(True)
        self.refresh_status()

    def poll_phone(self):
        """Refresh the phone pane: the outline from the latest observation, or a new screenshot."""
        try:
            phone = self.query_one("#phone")
            if phone.has_class("hidden") or self.session is None:
                return
            if not self.image_class:
                self.show_outline()
                return
        except NoMatches:
            return  # the UI is closing: its interval can fire after the widgets are gone
        if self.phone_busy:
            return
        if self.running is None and self.run_view is not None and self.frame and not self.demo and \
                not self.status.get("device_ready"):
            return
        self.phone_busy = True
        self.fetch_phone()

    def phone_caption(self):
        view = self.run_view
        if view is not None and view.narrator.screen is not None:
            screen = view.narrator.screen
            return f"{screen.get('app') or view.run.app['name']}, {len(screen['elements'])} elements read"
        return "Scripted screens, drawn locally" if self.demo and self.image_class else ""

    def show_outline(self):
        view = self.run_view
        screen = view.narrator.screen if view is not None else None
        step = view.narrator.step if view is not None else None
        target = step.target_id if step is not None and step.act in {None, "started"} and \
            step.state in {"thinking", "acting"} else None
        self.query_one(PhonePanel).show_outline(screen, target, self.phone_caption())

    @work(thread=True, group="phone")
    def fetch_phone(self):
        try:
            png = self.session.preview_image(self.running or (self.run_view.run if self.run_view else None))
        except Exception:
            png = None
        finally:
            self.phone_busy = False
        self.call_from_thread(self.phone_ready, png)

    def phone_ready(self, png):
        self.query_one(PhonePanel).show_image(png, self.phone_caption())

    def transcript_summary(self, color=False):
        """The session, printed to the terminal after the UI closes so it stays in scrollback."""
        from ..style import Palette
        paint = Palette(color)
        roles = {"success": "green", "error": "coral", "warning": "amber", "review": "accent", "neutral": "secondary"}
        lines = []
        for view in self.finished_views:
            narrator = view.narrator
            app = view.run.app.get("name") if view.run.app.get("bundleId") else None
            lines.append(paint("❯ ", "accent", "bold") + paint(view.run.goal, "bold")
                         + (paint(f"  {app}", "tertiary") if app else ""))
            outcome = None
            for item in narrator.items:
                if isinstance(item, Launch) and item.done:
                    lines.append("  " + paint("●", "green") + f" Opened {item.app}")
                elif isinstance(item, Step) and item.operation and item.operation not in {"DONE", "WAIT", "BLOCKED"}:
                    glyph = paint("●", "green" if item.state == "done" else "amber" if item.state == "unchanged"
                                  else "tertiary")
                    lines.append(f"  {glyph} {item.title}")
                elif isinstance(item, Approval) and item.decision:
                    words = APPROVALS.get(item.decision, (None, "You chose"))[1]
                    lines.append("  " + paint("◆", "accent") + f" {words}: {item.title}")
                elif isinstance(item, Outcome):
                    outcome = item
            elapsed = narrator.elapsed_ms(time.time() * 1000)
            line = facts(narrator.actions, elapsed, narrator.cost_nanodollars, bool(narrator.priced_calls),
                         plan=bool(narrator.plan_calls))
            if outcome is not None:
                glyph = status_glyph(outcome.head_tone)
                lines.append(paint(f"{glyph} {outcome.head}", roles.get(outcome.head_tone, "secondary"), "bold")
                             + "  " + paint(line, "tertiary"))
                if outcome.hero:
                    lines.append("  " + outcome.hero)
                detail = outcome.proof or outcome.detail
                if detail:
                    lines.append("  " + paint(detail, "tertiary"))
            elif narrator.finished_at is None:
                # Still running when the UI closed: `tui.main` stops it at its next safe point.
                lines.append(paint("■ Stopped when you quit", "secondary", "bold") + "  " + paint(line, "tertiary"))
            if not self.demo:
                lines.append("  " + paint(f"mobster --resume {view.run.id}", "tertiary"))
            lines.append("")
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def on_unmount(self):
        self.follow_stop.set()
