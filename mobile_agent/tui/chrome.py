"""The terminal UI's header, notice line, welcome screen and status line."""

import time

from rich.padding import Padding
from rich.text import Text
from textual import work
from textual.widgets import Input, Static

from .. import __version__
from ..catalog import APPS
from ..narrate import usd
from .render import ACCENT, AMBER, FAINT, GREEN, MUTED, RED, TEXT
from .theme import mark_text
from .widgets import clock

NARROW = 90            # below this the welcome screen drops its right-hand hints
SHORT = 30             # below this many rows the welcome screen drops the mark, so set-up and Try fit
PLUG_IN = "plug it in with a cable and unlock it"

EXAMPLES = [
    ("settings", "Turn on Dark Mode"),
    ("messages", "Find my last message from Kate Bell"),
    ("calendar", "What's on my calendar tomorrow?"),
]


class ChromeMixin:
    """What the app draws around the transcript from the state it holds: header, notice, welcome screen, status line."""

    @work(thread=True, group="update")
    def check_for_update(self):
        import argparse

        from .. import update_check
        args = argparse.Namespace(json=False)
        if not update_check.enabled("chat", args):
            return
        update_check.start("chat", args)
        line = update_check.pending_notice(__version__, 5)
        if line:
            self.call_from_thread(self.show_update, line)

    def show_update(self, line):
        self.update_line = line
        self.refresh_notice()

    def refresh_notice(self):
        notice = self.query_one("#notice", Static)
        text = Text()
        if self.attachments:
            names = ", ".join(item["name"] for item in self.attachments)
            text.append("+ ", style=f"bold {ACCENT}")
            text.append(names, style=TEXT)
            text.append("  goes with your next task", style=FAINT)
        if self.update_line:
            if text:
                text.append("\n")
            text.append("↑ ", style=f"bold {AMBER}")
            text.append(self.update_line, style=MUTED)
        notice.update(text)
        notice.set_class(bool(text), "open")

    def phone_name(self):
        if self.phone and self.phone[2]:
            return self.phone[2]
        return "Your iPhone"

    def device_line(self):
        """(dot, colour, words) for the header, in Mobster for Mac's phone words."""
        if self.connect_error:
            return "●", RED, "Mobster couldn't start"
        if self.session is None:
            return "◌", FAINT, "Starting…"
        if self.demo:
            return "●", ACCENT, "Scripted demo phone"
        status = self.status
        if status.get("device_ready"):
            return "●", GREEN, f"{self.phone_name()}, ready"
        if not status:
            return "◌", FAINT, "Looking for your iPhone…"
        if self.phone and self.phone[0] != "unplugged":
            words = {"trust": "tap Trust on it", "setup": "needs setup", "locked": "locked",
                     "starting": "starting"}.get(self.phone[0], "not ready")
            return "○", AMBER if self.phone[0] in ("trust", "locked") else FAINT, f"{self.phone_name()}, {words}"
        return "○", FAINT, "No iPhone yet"

    def refresh_chrome(self):
        self.refresh_header()
        self.refresh_status()
        self.refresh_chip()

    def refresh_header(self):
        """The top line: Mobster, its version and the phone. The demo's note joins it only when it fits whole."""
        header = Text()
        header.append("Mobster", style=f"bold {TEXT}")
        header.append(f" {__version__}", style=FAINT)
        dot, color, words = self.device_line()
        header.append(f"   {dot} ", style=color)
        header.append(words, style=MUTED)
        if self.demo:
            header.append("   DEMO", style=f"bold {ACCENT}")
            note = " no iPhone, no model calls"
            if not self.size.width or header.cell_len + len(note) + 2 <= self.size.width:
                header.append(note, style=FAINT)
        self.query_one("#header", Static).update(header)

    def refresh_status(self):
        text = Text()
        ask = self.settings.get("askBeforeActing", True)
        mode = self.settings.get("permissionMode") or ("ask_important" if ask else "dont_ask")
        if mode == "ask_all":
            text.append("⏸ ask before every change", style=ACCENT)
        elif mode == "dont_ask":
            text.append("⏵⏵ full access", style=AMBER)
        else:
            text.append("⏸ ask before acting", style=ACCENT)
        text.append("  (shift+tab)", style=FAINT)
        right = Text()
        narrator = self.run_view.narrator if self.run_view else None
        if self.running is not None and narrator is not None:
            right.append(f"step {self.run_view.steps:>2}", style=MUTED)
            right.append(f"  {clock(narrator.elapsed_ms(time.time() * 1000))}", style=MUTED)
            if narrator.plan_calls and not narrator.cost_nanodollars:
                right.append("   ", style=FAINT)
                right.append("On your plan", style=TEXT)
            elif narrator.priced_calls or narrator.unpriced_calls:
                right.append("   ", style=FAINT)
                right.append(usd(narrator.cost_nanodollars), style=TEXT)
        elif not self.welcome_checklist_shown():
            right.append(self.model_words(), style=MUTED)
        cap = self.session.spend_cap_usd if self.session else None
        if cap is not None:
            right.append(f"   cap ${cap:.2f}", style=FAINT)
        sep = "   " if right.plain else ""
        if self.running is not None:
            # An open approval card takes esc as its Don't, so the stop key it names is ctrl+c.
            right.append(f"{sep}{'ctrl+c' if self.approval_open() else 'esc'} to stop", style=FAINT)
        else:
            right.append(f"{sep}/ commands   ? keys", style=FAINT)
        width = self.size.width - 4
        gap = max(2, width - len(text.plain) - len(right.plain))
        if len(text.plain) + len(right.plain) + 2 > width:
            text = Text("⏸ ask" if ask else "⏵⏵ act", style=ACCENT if ask else AMBER)
            gap = max(2, width - len(text.plain) - len(right.plain))
        text.append(" " * gap)
        text.append_text(right)
        self.query_one("#status", Static).update(text)

    def welcome_checklist_shown(self):
        """Whether the welcome screen's checklist is on screen (it already says when there is no key)."""
        try:
            return bool(self.query_one("#welcome", Static).display and self.checklist())
        except Exception:  # noqa: BLE001 -- before the welcome is mounted
            return False

    def engine_state(self):
        """{available, reason, model} for Mobster's agent, from the runtime's status."""
        return (self.status.get("engines") or {}).get("smart") or {}

    def key_ready(self):
        from ..engines import smart_key
        return bool(smart_key())

    def model_words(self):
        if self.demo:
            return "scripted demo"
        if not self.status:
            return "checking…"  # which engine runs depends on the keys the session loads
        if not self.key_ready():
            return "no key yet"
        from ..engines import model_words
        model = self.engine_state().get("model")
        return model_words(model) if model else "Mobster's agent"

    def refresh_chip(self):
        chip = self.query_one("#chip", Static)
        value = self.query_one("#prompt", Input).value
        if self.running is not None:
            chip.update(Text("↳", style=f"bold {ACCENT}"))
            return
        if self.awaiting_requested_app(value):
            chip.update(Text(f"{self.requested_app} …", style=MUTED))  # looked up when the phone answers
            return
        app = self.target_app(value)
        chip.update(Text(f"{app['name']} ›" if app else "Any app ›", style=f"bold {ACCENT}" if app else FAINT))

    def awaiting_requested_app(self, value):
        """Whether a task typed now would wait for --app's app: nothing chosen since, and no @app in it."""
        return bool(self.requested_app) and self.app_choice is None and self.split_mentions(value)[1] is None

    def show_welcome(self, visible):
        """The welcome screen reads from the top; a conversation sits at the foot, as a chat does."""
        self.query_one("#welcome", Static).display = visible
        self.query_one("#transcript").set_class(visible, "idle")

    def refresh_welcome(self):
        welcome = self.query_one("#welcome", Static)
        welcome.update(self.welcome())

    def checklist(self):
        """[(done, label, value, hint)] for the three things a first task needs, or [] when all are done."""
        if self.demo or self.session is None or not self.status:
            return []
        from ..engines import model_provider, model_words, on_plan, smart_key, smart_model
        rows = []
        if smart_key() and on_plan(smart_model()):
            rows.append((True, "Model key", f"none needed: {model_words(smart_model())}", ""))
        elif smart_key():
            from ..login import source_words
            claude = model_provider(smart_model()) == "anthropic"
            name, variable = ("Claude", "ANTHROPIC_API_KEY") if claude else ("OpenAI", "OPENAI_API_KEY")
            rows.append((True, "Model key", f"{name}, {source_words(variable)}", ""))
        else:
            rows.append((False, "Model key", "add your Claude or OpenAI key", "/login"))
        if self.status.get("device_ready"):
            rows.append((True, "iPhone", f"{self.phone_name()}, ready", ""))
        else:
            words = self.phone[1] if self.phone else PLUG_IN
            rows.append((False, "iPhone", words, "/devices"))
        if self.tasks_done:
            rows.append((True, "First task", "done", ""))
        else:
            rows.append((False, "First task", "pick one below with ↑ and enter, or watch one", "mobster --demo"))
        if all(row[0] for row in rows):
            return []
        return rows

    def welcome(self):
        from rich.console import Group
        from rich.table import Table
        width = max(40, self.size.width - 6) if self.size.width else 100
        narrow = width < NARROW
        brand = Table.grid(padding=(0, 2))
        brand.add_column()
        brand.add_column()
        title = Text()
        title.append("Mobster\n", style=f"bold {TEXT}")  # the header above carries the version
        title.append("Test and automate apps on real iPhones.", style=MUTED)
        if 0 < self.size.height < SHORT:
            brand.add_row(title)  # a short terminal keeps its rows for set-up and Try
        else:
            brand.add_row(mark_text(), title)
        parts = [brand, Text("")]
        if self.connect_error:
            text = Text()
            text.append("Mobster couldn't start. ", style=f"bold {RED}")
            text.append(self.connect_error + "\n", style=TEXT)
            parts.append(text)
        rows = self.checklist()
        if rows:
            done = sum(1 for row in rows if row[0])
            grid = Table.grid(expand=True, padding=(0, 2))
            grid.add_column(width=1)
            grid.add_column(width=10, no_wrap=True)
            grid.add_column(ratio=1)
            head = Text("Get set up", style=f"bold {TEXT}")
            head.append(f"   {done} of 3 done", style=FAINT)
            room = width - 1 - 1 - 10 - 2 * 2  # the value column: the padding, the dot and label columns, the gaps
            for ok, label, value, hint in rows:
                words = Text(value, style=FAINT if ok else MUTED)
                if hint and not ok:
                    # Beside its row, as the error screen has it; on a line of its own when it would wrap, so a
                    # command (`mobster --demo`) is never cut in two.
                    words.append(("  " if len(value) + 2 + len(hint) <= room else "\n") + hint, style=ACCENT)
                grid.add_row(Text("✓", style=GREEN) if ok else Text("○", style=ACCENT),
                             Text(label, style=FAINT if ok else f"bold {TEXT}"), words)
            parts += [head, Padding(grid, (0, 0, 0, 1)), Text("")]
        try_head = Text("Try", style=f"bold {TEXT}")
        examples = Text()
        for app_id, goal in EXAMPLES:
            name = next((a["name"] for a in APPS if a["id"] == app_id), app_id)
            examples.append("❯ ", style=ACCENT)
            examples.append(goal, style=TEXT)
            if not narrow:
                examples.append(f"  {name}", style=FAINT)
            examples.append("\n")
        examples.rstrip()
        parts += [try_head, Padding(examples, (0, 0, 0, 1)), Text("")]
        footer = Text()
        if not self.demo:
            footer.append("Building an iOS app? ", style=MUTED)
            footer.append("mobster test", style=ACCENT)
            footer.append(" and ", style=MUTED)
            footer.append("mobster mcp", style=ACCENT)
            footer.append(" need no iPhone.", style=MUTED)
        else:
            footer.append("This is a scripted phone: nothing reaches a real iPhone or a model. ", style=MUTED)
            footer.append("mobster", style=ACCENT)
            footer.append(" uses yours.", style=MUTED)
        parts.append(footer)
        return Group(*parts)

    def engine_ready(self):
        """Whether Mobster's agent can run: its key is set (and reaches its model)."""
        return bool(self.engine_state().get("available"))

    def needs_setup(self):
        return (self.session is not None and not self.demo and bool(self.status)
                and not (self.status.get("device_ready") and self.engine_ready()))

    def setup_hints(self):
        """What's missing, one line each with the thing that fixes it, under a task that couldn't start."""
        text = Text()
        for ok, label, value, hint in self.checklist():
            if ok or label == "First task":
                continue
            text.append("  ○ ", style=ACCENT)
            text.append(f"{label}: ", style=f"bold {TEXT}")
            text.append(value, style=MUTED)
            if hint:
                text.append(f"  {hint}", style=ACCENT)
            text.append("\n")
        if not self.key_ready() or self.status.get("device_ready"):
            return text
        if self.status and not self.engine_ready():
            reason = self.engine_state().get("reason")
            if reason:
                text.append("  ○ ", style=ACCENT)
                text.append(reason + "\n", style=MUTED)
        return text
