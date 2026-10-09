"""`mobster run` on a terminal: one readable line per step instead of JSON.

Only used when stdout is a terminal and ``--json`` is not given; scripts and
pipes keep receiving the JSON lines they always did. Lines are only ever
appended (no full-screen redraw), so the output stays in scrollback. One
transient line at the bottom shows what is happening now, and is cleared
before each permanent line.
"""

import os
import sys
import threading
import time

from .narrate import Launch, Narrator, Note, Outcome, Step, facts, seconds, status_label, status_tone, usd  # noqa: F401

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# The palette lives in style.py (help and doctor use it without loading the narrator); kept here for callers.
from .style import Palette, color_enabled, palette  # noqa: E402,F401


class ConsoleRenderer:
    """``emit(event)`` for ``Agent``: prints each step once it has an outcome."""

    TONE_ROLES = {"success": "green", "error": "red", "warning": "amber", "review": "accent", "neutral": "muted"}

    def __init__(self, stream=None, *, color=None, live=None, preview=False):
        self.stream = stream or sys.stdout
        self.paint = Palette(color_enabled(self.stream) if color is None else color)
        self.live = self.stream.isatty() if live is None else live
        self.preview = preview
        self.narrator = Narrator()
        self.printed = set()
        self.lock = threading.Lock()
        self.transient = False
        self.frame = 0
        self.stopped = threading.Event()
        self.ticker = None
        self.started = time.monotonic()
        self.result = None
        self.paused = False

    # -- output ---------------------------------------------------------------------------------

    def _clear(self):
        if self.transient:
            self.stream.write("\r\033[2K")
            self.transient = False

    def line(self, text=""):
        with self.lock:
            self._clear()
            self.stream.write(text + "\n")
            self.stream.flush()

    def start(self, goal, where=""):
        p = self.paint
        head = p("❯ ", "accent", "bold") + p(goal, "bold")
        if where:
            head += "  " + p(where, "faint")
        self.line(head)
        if self.live:
            self.ticker = threading.Thread(target=self._tick, name="mobster-console", daemon=True)
            self.ticker.start()

    def close(self):
        self.stopped.set()
        if self.ticker is not None:
            self.ticker.join(1)
        with self.lock:
            self._clear()
            self.stream.flush()

    def _tick(self):
        while not self.stopped.wait(.08):
            if self.paused:
                continue
            with self.lock:
                self.frame += 1
                step = self.narrator.step
                doing = "Starting"
                if step is not None and step.key not in self.printed:
                    doing = {"acting": f"{step.title}…", "thinking": "Deciding…" if step.elements is not None
                             else "Reading the screen…"}.get(step.state, "Reading the screen…")
                elapsed = seconds((time.monotonic() - self.started) * 1000)
                text = (self.paint(SPINNER[self.frame % len(SPINNER)], "accent") + " "
                        + self.paint(doing, "muted") + self.paint(f"  {elapsed} · ctrl+c to stop", "faint"))
                self.stream.write("\r\033[2K" + text)
                self.stream.flush()
                self.transient = True

    # -- events ---------------------------------------------------------------------------------

    def __call__(self, event):
        if not isinstance(event, dict):
            return
        event = {**event, "timestamp": time.time() * 1000}
        kind = event.get("event")
        if kind == "result":
            self.result = event
            self._flush_steps()
            self._outcome(Outcome("outcome", event.get("status") or "error", event))
            return
        for item in self.narrator.feed(event):
            if isinstance(item, Step):
                if kind in {"observation_after_action", "action_not_dispatched", "completion_check"} or (
                        kind == "decision" and item.operation in {"WAIT", "BLOCKED"}):
                    self._flush_steps(upto=item)
            elif isinstance(item, Launch) and item.done:
                self.line("  " + self.paint("● ", "green") + self.paint(f"Opened {item.app}", "bold"))
            elif isinstance(item, Note):
                role = {"warning": "amber", "error": "red"}.get(item.tone, "muted")
                self.line("  " + self.paint(f"! {item.text}" if item.tone != "neutral" else f"· {item.text}", role))
        if kind == "observation":
            # A new step began: the previous one is as finished as it will get.
            steps = [item for item in self.narrator.items if isinstance(item, Step)]
            for step in steps[:-1]:
                self._print_step(step)

    def _flush_steps(self, upto=None):
        for item in self.narrator.items:
            if isinstance(item, Step):
                self._print_step(item)
            if item is upto:
                break

    def _print_step(self, step):
        if step.key in self.printed or step.operation is None:
            return
        self.printed.add(step.key)
        p = self.paint
        state = step.state
        glyph = ("✗", "red") if state == "failed" else ("○", "faint") if state == "skipped" and not self.preview else ("✓", "green") if step.operation == "DONE" else \
            ("○", "muted") if state == "stalled" else \
            ("●", "amber") if state == "unchanged" else ("◌", "accent") if self.preview and step.act is None else \
            ("●", "green")
        self.line(f"  {p(glyph[0], glyph[1])} {p(step.title, 'bold')}")
        where = step.app or ""
        if step.elements is not None:
            where = f"{where} · {step.elements} elements" if where else f"{step.elements} elements"
        outcome = ""
        if step.act == "acknowledged":
            outcome = {True: "screen changed", False: "no visible change", None: "sent"}[step.changed]
        elif step.act == "not_dispatched":
            outcome = "not sent: the screen changed first"
        elif self.preview and step.operation not in {"DONE", "WAIT", "BLOCKED"}:
            outcome = "preview: not sent"
        elif step.note:
            outcome = step.note
        elif step.completion:
            outcome = "goal met" if step.completion == "confirmed" else "not finished yet"
        parts = [x for x in (where, outcome) if x]
        if parts:
            self.line("    " + p("⎿ " + " → ".join(parts), "faint"))

    def _outcome(self, item):
        """MESSAGING §11's result: the head (Done, Take a look, Couldn't finish…) with the facts, the answer, then
        the proof or why it ended."""
        p = self.paint
        head, tone = item.head, item.head_tone
        role = self.TONE_ROLES.get(tone, "muted")
        glyph = {"success": "✓", "error": "✗", "warning": "!", "review": "◆"}.get(tone, "■")
        if item.status == "preview":
            head = "Preview"
        elapsed = item.summary.get("elapsed_ms") or (time.monotonic() - self.started) * 1000
        line = facts(self.narrator.actions, elapsed, self.narrator.cost_nanodollars, bool(self.narrator.priced_calls))
        self.line(p(f"{glyph} {head}", role, "bold") + "  " + p(line, "faint"))
        hero = item.hero
        if hero:
            self.line("  " + hero)
        detail = item.proof or item.detail
        if item.status == "preview":
            detail = "Nothing was sent to the phone. Add --execute to act."
        if detail:
            self.line("  " + p(detail, "faint" if item.proof else "muted"))
        data = item.summary.get("data")
        if data is not None and not (isinstance(data, str) and data.strip() == hero):
            import json
            body = data if isinstance(data, str) else json.dumps(data, indent=2, ensure_ascii=False)
            self.line("")
            self.line(body)

    # -- an approval on the same terminal ---------------------------------------------------------------------

    def pause(self):
        """Stop the spinner line and clear it, so a question can be asked below the steps."""
        self.paused = True
        with self.lock:
            self._clear()
            self.stream.flush()

    def resume(self):
        self.paused = False
