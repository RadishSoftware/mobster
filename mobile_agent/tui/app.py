"""The Mobster terminal UI: a Textual app over ``session.Session``.

Layout, top to bottom: a one-line header (Mobster and your iPhone), the transcript (one block per task: its steps,
approvals and result) with the phone's screen beside it on wide terminals, the approval card when Mobster is
waiting for you, a notice line (an update, attached files), the composer with its app chip, and a one-line status
footer (mode, model, the running step and its clock).

The first screen is one checklist (your key, your iPhone, a first task) that ticks itself off, then examples to
try. Tasks are turns in a conversation (the conversations track's service, in this process), so a follow-up knows
what the last task found; while a task runs, enter sends it a message instead.

Colours are the Mac app's (dashboard/DESIGN.md §2); words are MESSAGING §11's. Every call into the session runs on a
worker thread; the event loop only draws.
"""

import os
import re
import threading
import time

from rich.padding import Padding
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import __version__, links
from ..app_choice import (APP_VERBS, APP_WORDS, COMMON_WORD_APPS, MENTION, MESSAGE_VERB_APPS,  # noqa: F401
                          find_app, infer_app, mention_rank)
from ..catalog import APPS
from ..narrate import Approval, Launch, Narrator, Outcome, Step, facts, seconds, status_label, status_tone, usd
from ..tui_keys import COMMANDS, KEY_GROUPS, KEYS, SECTIONS, Command  # noqa: F401
from . import render
from .render import (ACCENT, AMBER, CANVAS, CHROME, FAINT, GREEN, LINE, MUTED, OVERLAY, QUATERNARY, RAISED, RED,
                     TEXT, TONES, VIOLET)

THEME = Theme(
    name="mobster", primary=ACCENT, secondary=MUTED, accent=ACCENT, warning=AMBER, error=RED, success=GREEN,
    foreground=TEXT, background=CANVAS, surface=RAISED, panel=OVERLAY, boost=OVERLAY, dark=True,
    variables={"footer-key-foreground": ACCENT, "input-selection-background": "#A493FF40",
               "block-cursor-background": OVERLAY, "block-cursor-foreground": TEXT, "input-cursor-background": ACCENT,
               "scrollbar": LINE, "scrollbar-hover": "#3a3940", "scrollbar-active": "#4a4950",
               "scrollbar-background": CANVAS, "scrollbar-corner-color": CANVAS},
)

FORMATS = ("auto", "text", "json", "yaml", "csv", "markdown")
PHONE_MIN_WIDTH = 110  # the phone panel shows by default from this terminal width
NARROW = 90            # below this the welcome screen drops its right-hand hints
LOCAL_THREADS = "terminal"  # this terminal's conversations, as `mobster chat` remembers them (chat_here.LOCAL)

# MESSAGING §11 starters: one that reads, one that sends (and asks first), one that changes a setting.
EXAMPLES = [
    ("settings", "Turn on Dark Mode"),
    ("messages", "Find my last message from Kate Bell"),
    ("calendar", "What's on my calendar tomorrow?"),
]

# Mo, the mark (the brand guide's §2): a violet phone body whose two eyes look up and to the right. Drawn in half
# blocks, two rows tall, on the welcome screen only.
MARK = ("..VVVVVV..",
        ".VVVVVVVV.",
        ".VVVWVVWV.",
        ".VVVWVVWV.")
PATH_MENTION = re.compile(r"(?:^|\s)@((?:\./|~/|/)[^\s]*)$")
STEER_PREFIX = "While Mobster worked · you said "


def ago(timestamp):
    delta = max(0, time.time() - timestamp)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    return time.strftime("%b %-d", time.localtime(timestamp))


def clock(ms):
    """A fixed-width running clock: 0:05, 1:28, 12:04 (the status line never shifts as digits change)."""
    total = int((ms or 0) // 1000)
    return f"{total // 60:>2}:{total % 60:02d}"


def mark_text():
    """The mark as two lines of half blocks: violet body, white eyes, the canvas around it."""
    colours = {"V": VIOLET, "W": "#FFFFFF", ".": CANVAS}
    text = Text()
    for top, bottom in zip(MARK[0::2], MARK[1::2]):
        for a, b in zip(top, bottom):
            if a == "." and b == ".":
                text.append(" ")
            else:
                text.append("▀", style=f"{colours[a]} on {colours[b]}")
        text.append("\n")
    text.rstrip()
    return text


# -- transcript widgets ---------------------------------------------------------------------------


class ItemView(Static):
    """One narrated item (a step, an approval, a note, the outcome), redrawn when it changes."""

    def __init__(self, item, run_view, number=None):
        super().__init__(classes=f"item {type(item).__name__.lower()}")
        self.item, self.run_view, self.number = item, run_view, number

    def redraw(self, frame=0):
        app = self.app
        self.update(render.item_renderable(self.item, expanded=getattr(app, "expanded", False), frame=frame,
                                           narrator=self.run_view.narrator, number=self.number))

    def on_mount(self):
        self.redraw()

    @property
    def live(self):
        return isinstance(self.item, Step) and self.item.state in {"thinking", "acting"}


class RunView(Vertical):
    """One task in the transcript: the prompt line, then its items as they arrive."""

    def __init__(self, run, apps=None, past=False):
        super().__init__(classes="run")
        self.run = run
        self.narrator = Narrator(apps)
        self.views = {}
        self.past = past
        self.steps = 0

    def compose(self):
        yield Static(self.header(), classes="task")

    def header(self):
        text = Text()
        text.append("❯ ", style=f"bold {ACCENT}")
        text.append(self.run.goal, style=f"bold {TEXT}")
        app = self.run.app.get("name") if self.run.app.get("bundleId") else None
        meta = f"  {app}" if app else ""
        if self.run.dry_run:
            meta += " · preview"
        if self.past:
            meta += f" · {ago(self.run.created_at)} · {self.run.id}"
        text.append(meta, style=FAINT)
        return text

    def note(self, text, style=MUTED):
        """A line in this task that isn't an event: a message you sent while it worked."""
        line = Static(Text(text, style=style), classes="item note")
        self.mount(line)

    def feed(self, event):
        changed = self.narrator.feed(event)
        for item in changed:
            if getattr(item, "merged_into", None) is not None:
                # Folded into the stalled step before it: one line with a count instead of two.
                view = self.views.pop(item.key, None)
                if view is not None:
                    view.remove()
                    self.steps -= 1
                continue
            view = self.views.get(item.key)
            if view is None:
                number = None
                if isinstance(item, Step):
                    self.steps += 1
                    number = self.steps
                view = ItemView(item, self, number)
                self.views[item.key] = view
                self.mount(view)
            else:
                view.redraw(self.app.frame)
        return changed

    def redraw(self):
        for view in self.views.values():
            view.redraw(self.app.frame)


class PhonePanel(Vertical):
    """The phone beside the transcript.

    Where the terminal shows images (kitty, Ghostty and WezTerm through the kitty graphics
    protocol, iTerm2 and others through sixel; detected before the UI starts), it is the real
    screenshot. Everywhere else it is the screen as Mobster read it: the app, the title bar and
    each row of the accessibility tree, with the element the current step acts on highlighted.
    """

    def __init__(self, image_class=None, **kwargs):
        super().__init__(**kwargs)
        self.image_class = image_class
        self.add_class(*(("image",) if image_class else ("text", "empty")))

    def compose(self):
        yield Static("Phone" if self.image_class else "Screen", id="phone-title")
        if self.image_class:
            yield self.image_class(id="phone-image")
        else:
            yield Static(render.screen_outline(None, 30, 10), id="phone-outline")
        yield Static("", id="phone-caption")

    def show_image(self, png, caption=""):
        if png:
            from PIL import Image
            import io
            try:
                self.query_one("#phone-image").image = Image.open(io.BytesIO(png))
            except Exception:
                pass
        self.query_one("#phone-caption", Static).update(Text(caption, style=FAINT))

    outline = None

    def show_outline(self, screen, target_id=None, caption=""):
        # Until a task has read a screen there is nothing to show: give the room to the transcript.
        empty = not (screen and screen.get("elements"))
        was_empty = self.has_class("empty")
        self.set_class(empty, "empty")
        self.outline = (screen, target_id, caption)
        if was_empty and not empty:
            self.call_after_refresh(self.draw_outline)  # sized once it is laid out
        self.draw_outline()

    def draw_outline(self):
        if self.outline is None:
            return
        screen, target_id, caption = self.outline
        width = max(16, self.size.width - 2)
        height = max(6, self.size.height - 4)
        self.query_one("#phone-outline", Static).update(render.screen_outline(screen, width, height, target_id))
        self.query_one("#phone-caption", Static).update(Text(caption, style=FAINT))

    def on_resize(self, event):
        if not self.image_class:
            self.draw_outline()


def approval_verbs(request):
    """(yes, no) as Mobster for Mac's buttons say them (MESSAGING §11): Send / Don't send, Pay / Don't pay, …,
    else Approve / Decline."""
    from ..smart_run import verbs
    return verbs(request)


class ApprovalCard(Static, can_focus=True):
    """Mobster is waiting for you. y gives the verb (Send, Pay…); n and esc give Don't, the safe answer.
    Keys: y, n, esc, 1-6, ↑ ↓, enter."""

    BINDINGS = [
        Binding("y", "answer('yes')", show=False), Binding("n", "answer('no')", show=False),
        Binding("escape", "answer('no')", show=False), Binding("enter", "answer('enter')", show=False),
        Binding("up", "move(-1)", show=False), Binding("down", "move(1)", show=False),
    ] + [Binding(str(n), f"answer('{n}')", show=False) for n in range(1, 7)]

    def __init__(self):
        super().__init__(id="approval")
        self.request = None
        self.cursor = 0

    def options(self):
        request = self.request or {}
        if request.get("kind", "action") != "action" and request.get("choices"):
            return [(c["id"], c["label"]) for c in request["choices"]] + [("__no", "Stop the task")]
        yes, no = approval_verbs(request)
        return [("__yes", yes), ("__no", no)]

    def ask(self, request):
        self.request, self.cursor = request, 0
        if request.get("defaultChoice"):
            ids = [key for key, _ in self.options()]
            self.cursor = ids.index(request["defaultChoice"]) if request["defaultChoice"] in ids else 0
        self.redraw()

    def redraw(self):
        request = self.request or {}
        text = Text()
        what = request.get("question") if request.get("kind", "action") != "action" else None
        if not what:
            from ..threads.cli import approval_title
            what = approval_title(request)
        text.append(what + "\n", style=f"bold {TEXT}")
        if request.get("text"):
            text.append(f"“{request['text']}”\n", style=TEXT)
        if request.get("kind", "action") == "action":
            from ..smart_run import reassurance
            text.append(reassurance(request) + "\n", style=FAINT)
        elif request.get("limits"):
            text.append(f"{request['limits']}\n", style=FAINT)
        text.append("\n")
        for index, (key, label) in enumerate(self.options()):
            selected = index == self.cursor
            text.append("❯ " if selected else "  ", style=f"bold {ACCENT}")
            text.append(f"{index + 1}. {label}", style=f"bold {TEXT}" if selected else MUTED)
            hint = "   y" if key == "__yes" else "   n · esc" if key == "__no" else ""
            text.append(hint + "\n", style=FAINT)
        expires = request.get("expiresAt")
        if expires:
            left = max(0, expires / 1000 - time.time())
            no = self.options()[-1][1]
            text.append(f"{no} in {int(left // 60)}:{int(left % 60):02d} if nobody answers", style=FAINT)
        self.update(text)

    def action_move(self, delta):
        self.cursor = (self.cursor + delta) % len(self.options())
        self.redraw()

    def action_answer(self, key):
        options = self.options()
        if key == "enter":
            choice = options[self.cursor][0]
        elif key == "yes":
            choice = "__yes" if any(k == "__yes" for k, _ in options) else options[self.cursor][0]
        elif key == "no":
            choice = "__no"
        else:
            index = int(key) - 1
            if index >= len(options):
                return
            choice = options[index][0]
        self.app.answer_approval(self.request, choice)


class PromptInput(Input):
    """The task prompt. The app sees popup, recall and help keys before the text does."""

    async def _on_key(self, event: events.Key):
        if self.app.prompt_key(event.key, event.character):
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


PLACEHOLDER = "What should Mobster do on your iPhone?"
STEER_PLACEHOLDER = "Tell Mobster something while it works…"


class Composer(Horizontal):
    def compose(self):
        yield Static("", id="chip")
        yield PromptInput(placeholder=PLACEHOLDER, id="prompt", select_on_focus=False)


# -- modal screens ---------------------------------------------------------------------------------


class Picker(ModalScreen):
    """A filterable list: apps, models, history, conversations. Returns the chosen option id, or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", show=False)]

    def __init__(self, title, options, placeholder="Type to filter", empty="Nothing matches", width=76):
        super().__init__()
        self.title_text, self.all_options, self.placeholder, self.empty = title, options, placeholder, empty
        self.width = width

    def compose(self):
        with Vertical(id="picker"):
            yield Static(Text(self.title_text, style=f"bold {TEXT}"), id="picker-title")
            yield Input(placeholder=self.placeholder, id="picker-filter")
            yield OptionList(id="picker-list")
            yield Static(Text("↑↓ move · enter choose · esc close", style=FAINT), id="picker-hint")

    def on_mount(self):
        self.query_one("#picker").styles.width = self.width
        self.fill("")
        self.query_one("#picker-filter", Input).focus()

    def fill(self, query):
        options = self.query_one("#picker-list", OptionList)
        options.clear_options()
        query = query.casefold().strip()
        matches = [(key, prompt, words) for key, prompt, words in self.all_options
                   if not query or all(part in words.casefold() for part in query.split())]
        for key, prompt, _ in matches[:200]:
            options.add_option(Option(prompt, id=key))
        if not matches:
            options.add_option(Option(Text(self.empty, style=FAINT), id="__none", disabled=True))
        else:
            options.highlighted = 0

    @on(Input.Changed, "#picker-filter")
    def filter_changed(self, event):
        self.fill(event.value)

    @on(Input.Submitted, "#picker-filter")
    def filter_submitted(self):
        options = self.query_one("#picker-list", OptionList)
        if options.highlighted is not None:
            option = options.get_option_at_index(options.highlighted)
            if not option.disabled:
                self.dismiss(option.id)

    @on(OptionList.OptionSelected, "#picker-list")
    def chosen(self, event):
        self.dismiss(event.option.id)

    def on_key(self, event):
        options = self.query_one("#picker-list", OptionList)
        if event.key in {"down", "up"} and self.focused is not options:
            options.action_cursor_down() if event.key == "down" else options.action_cursor_up()
            event.stop()


class InfoScreen(ModalScreen):
    """A read-only panel (the keys sheet, your iPhone, what Mobster remembers). Any key closes it."""

    def __init__(self, renderable, title):
        super().__init__()
        self.renderable, self.title_text = renderable, title

    def compose(self):
        with VerticalScroll(id="info"):
            yield Static(Text(self.title_text, style=f"bold {TEXT}"), id="info-title")
            yield Static(self.renderable, id="info-body")
            yield Static(Text("esc close", style=FAINT), id="info-hint")

    def update_body(self, renderable):
        self.query_one("#info-body", Static).update(renderable)

    def on_key(self, event):
        if event.key in {"escape", "q", "enter", "space", "question_mark"}:
            event.stop()
            self.dismiss(None)


class AskScreen(ModalScreen):
    """A yes or no question (Remember this?), answered with y, n or esc. Returns True or False."""

    BINDINGS = [Binding("y", "dismiss(True)", show=False), Binding("n", "dismiss(False)", show=False),
                Binding("escape", "dismiss(False)", show=False), Binding("enter", "dismiss(True)", show=False)]

    def __init__(self, title, body, yes, no):
        super().__init__()
        self.title_text, self.body, self.yes, self.no = title, body, yes, no

    def compose(self):
        text = Text()
        text.append(self.body + "\n\n", style=MUTED)
        text.append("❯ ", style=f"bold {ACCENT}")
        text.append(self.yes, style=f"bold {TEXT}")
        text.append("   y", style=FAINT)
        text.append("     " + self.no, style=MUTED)
        text.append("   n · esc", style=FAINT)
        with Vertical(id="ask"):
            yield Static(Text(self.title_text, style=f"bold {TEXT}"), id="ask-title")
            yield Static(text, id="ask-body")


class LoginScreen(ModalScreen):
    """/login: Claude or OpenAI, then the key in a password field. The key is tested with one tiny request, then
    saved where Mobster for Mac keeps it (login.py). Returns the provider saved, or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", show=False)]

    def __init__(self, provider="anthropic"):
        super().__init__()
        self.provider = provider

    def compose(self):
        from ..login import PROVIDERS
        with Vertical(id="login"):
            yield Static(Text("Connect your AI account", style=f"bold {TEXT}"), id="login-title")
            yield Static(self.choice_text(), id="login-choice")
            yield Input(password=True, id="login-key",
                        placeholder=f"Paste your {PROVIDERS[self.provider]['name']} key ({PROVIDERS[self.provider]['prefix']}…)")
            yield Static(Text("Mobster's agent runs on your own Claude or OpenAI key. You pay them for what it uses, "
                              "and the key stays on this Mac.", style=FAINT), id="login-note")
            yield Static(Text("tab Claude or OpenAI · enter connect · esc close", style=FAINT), id="login-hint")

    def choice_text(self):
        text = Text()
        for provider, words in (("anthropic", "Claude, by Anthropic"), ("openai", "OpenAI")):
            chosen = provider == self.provider
            text.append("● " if chosen else "○ ", style=ACCENT if chosen else FAINT)
            text.append(words, style=f"bold {TEXT}" if chosen else MUTED)
            if provider == "anthropic":
                text.append("  recommended", style=GREEN)
            text.append("    ")
        return text

    def on_mount(self):
        self.query_one("#login-key", Input).focus()

    def on_key(self, event):
        if event.key == "tab":
            from ..login import PROVIDERS
            self.provider = "openai" if self.provider == "anthropic" else "anthropic"
            self.query_one("#login-choice", Static).update(self.choice_text())
            info = PROVIDERS[self.provider]
            self.query_one("#login-key", Input).placeholder = f"Paste your {info['name']} key ({info['prefix']}…)"
            event.stop()
            event.prevent_default()

    @on(Input.Submitted, "#login-key")
    def submitted(self, event):
        key = event.value.strip()
        if not key:
            return
        self.query_one("#login-note", Static).update(Text("Checking your key…", style=FAINT))
        self.check(key)

    @work(thread=True, exclusive=True, group="login")
    def check(self, key):
        message, ok = save_key(self.provider, key)
        self.app.call_from_thread(self.checked, ok, message)

    def checked(self, ok, message):
        if ok:
            self.dismiss(self.provider)
        else:
            self.query_one("#login-note", Static).update(Text(message, style=RED))


def save_key(provider, key):
    """(message, ok): test the key and save it as `mobster login` does. Never shows it."""
    from ..config import SOURCES, app_env_file
    from ..keys import KEY_PATTERN, Keys
    from ..login import PROVIDERS
    info = PROVIDERS[provider]
    if not KEY_PATTERN.fullmatch(key):
        return f"That doesn't look like {'an' if provider == 'openai' else 'a'} {info['name']} key. Copy it again.", \
            False
    variable = info["variable"]
    saved = {name: os.environ.get(name) for name in (variable, "MOBSTER_SMART_PROVIDER")}
    os.environ[variable] = key
    os.environ["MOBSTER_SMART_PROVIDER"] = provider
    keys = Keys(app_env_file())
    try:
        result = keys.test(provider)
    except Exception as error:  # noqa: BLE001
        result = {"ok": False, "message": f"Mobster couldn't check the key ({type(error).__name__})."}
    if not result.get("ok"):
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return (result.get("message") or "The key didn't work.") + " Nothing was saved.", False
    keys.update({provider: {"key": key}, "smartProvider": provider})
    SOURCES[variable] = "mac-app"
    return f"{info['name']} is connected.", True


# -- the app --------------------------------------------------------------------------------------


class MobsterApp(App):
    TITLE = "Mobster"
    ENABLE_COMMAND_PALETTE = False
    CSS = f"""
    Screen {{ background: {CANVAS}; color: {TEXT}; layers: base overlay; }}
    #header {{ height: 1; padding: 0 1; background: {CHROME}; }}
    #body {{ height: 1fr; }}
    #transcript {{ height: 1fr; padding: 0 1 0 2; scrollbar-size-vertical: 1; align-vertical: bottom; }}
    #welcome {{ margin: 1 0 1 0; }}
    #welcome.hidden {{ display: none; }}
    .run {{ height: auto; margin: 0 0 1 0; }}
    .task {{ margin: 0 0 1 0; }}
    .item {{ height: auto; }}
    .step {{ margin: 0 0 0 0; }}
    .outcome {{ margin: 1 0 0 0; }}
    .launch {{ margin: 0 0 0 0; }}
    .notebox {{ height: auto; margin: 0 0 1 0; }}
    #phone {{ border-left: vkey {LINE}; padding: 0 1; }}
    #phone.text {{ width: 36; }}
    #phone.image {{ width: 40; align-horizontal: center; }}
    #phone-title, #phone-caption {{ width: 100%; }}
    #phone.hidden, #phone.empty {{ display: none; }}
    #phone-title {{ color: {FAINT}; height: 1; margin: 0 0 1 0; }}
    #phone-outline {{ height: 1fr; }}
    #phone-image {{ width: auto; height: auto; max-height: 1fr; }}
    #phone-caption {{ height: auto; max-height: 3; margin: 1 0 0 0; }}
    #bottom {{ height: auto; dock: bottom; }}
    #suggest {{ height: auto; max-height: 16; border: none; background: {OVERLAY}; margin: 0 2; padding: 0 1;
               display: none; }}
    #suggest.open {{ display: block; }}
    #suggest > .option-list--option-highlighted {{ background: #2e2d35; color: {TEXT}; text-style: none; }}
    #suggest-hint {{ height: 1; margin: 0 2; padding: 0 1; background: {OVERLAY}; display: none; }}
    #suggest-hint.open {{ display: block; }}
    #approval {{ border: round {ACCENT}; padding: 0 1; margin: 0 1; height: auto; display: none;
                background: {OVERLAY}; }}
    #approval.open {{ display: block; }}
    #approval:focus {{ border: round {ACCENT}; }}
    #notice {{ height: auto; margin: 0 2; display: none; }}
    #notice.open {{ display: block; }}
    Composer {{ height: 3; background: {RAISED}; border: tall {RAISED}; margin: 0 1; padding: 0 0; }}
    Composer:focus-within {{ border-left: tall {ACCENT}; }}
    Composer.busy {{ border-left: tall {LINE}; }}
    #chip {{ width: auto; height: 1; padding: 0 1; color: {ACCENT}; text-style: bold; background: {RAISED}; }}
    #prompt {{ border: none; height: 1; padding: 0 1 0 0; background: {RAISED}; width: 1fr; }}
    #prompt:focus {{ border: none; background-tint: {RAISED} 0%; }}
    #prompt > .input--placeholder {{ color: #7A7782; }}
    #status {{ height: 1; padding: 0 2; color: {MUTED}; }}
    ModalScreen {{ background: #060508 62%; align: center middle; }}
    #picker {{ width: 76; max-width: 90%; height: auto; max-height: 80%; border: round {LINE}; background: {OVERLAY};
              padding: 0 1; }}
    #picker-title {{ margin: 0 0 1 0; }}
    #picker-filter {{ border: none; background: {RAISED}; height: 1; margin: 0 0 1 0; padding: 0 1; }}
    #picker-list {{ height: auto; max-height: 24; border: none; background: {OVERLAY}; }}
    #picker-list > .option-list--option-highlighted {{ background: #2e2d35; text-style: none; }}
    #picker-hint {{ margin: 1 0 0 0; }}
    #info {{ width: 84; max-width: 94%; height: auto; max-height: 90%; border: round {LINE}; background: {OVERLAY};
            padding: 0 2; }}
    #info-title {{ margin: 0 0 1 0; }}
    #info-hint {{ margin: 1 0 0 0; }}
    #ask, #login {{ width: 72; max-width: 94%; height: auto; border: round {ACCENT}; background: {OVERLAY};
                   padding: 0 2; }}
    #ask-title, #login-title {{ margin: 0 0 1 0; }}
    #login-key {{ border: none; background: {RAISED}; height: 1; margin: 1 0; padding: 0 1; }}
    #login-hint {{ margin: 1 0 0 0; }}
    """

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

    def __init__(self, session_factory, *, demo=False, wda_url=None, resume=None, app_id=None, bell=True,
                 image_class=None, task=None, continue_=False, thread_id=None):
        super().__init__()
        self.session_factory = session_factory
        # A textual-image widget class when this terminal shows images (tui.images), else None: text.
        self.image_class = image_class
        self.session = None
        self.demo = demo
        self.wda_url = wda_url
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
        self.settings = {"askBeforeActing": os.environ.get("MOBSTER_ASK_BEFORE_ACTING", "1") != "0",
                         "bypassChecks": os.environ.get("MOBSTER_BYPASS_CHECKS", "0") == "1"}
        self.helper_model = None
        self.helper_chosen = False
        self.output_format = "auto"
        self.preview_next = False
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
        self.started_at = time.monotonic()
        # Conversations: the one tasks go to (None: a new one starts with the next task), -c's request, and the
        # files attached for the next task.
        self.thread_id = thread_id
        self.continue_requested = bool(continue_)
        self.attachments = []
        self.initial_task = task
        self.phone = None       # (state, words, name) from the device list: the checklist's iPhone row
        self.tasks_done = 0
        self.update_line = ""

    # -- layout ---------------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Horizontal(id="body"):
            with VerticalScroll(id="transcript"):
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
        if self.phone_visible is None:
            self.query_one("#phone").set_class(event.size.width < PHONE_MIN_WIDTH, "hidden")
        self.call_after_refresh(self.poll_phone)
        try:
            self.refresh_welcome()
        except NoMatches:
            pass

    # -- the session ----------------------------------------------------------------------------

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
        """`mobster "TASK"`: run it now when Mobster can; else it waits in the box while the checklist shows what's
        missing."""
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
        """Whether ``self.apps`` holds the phone's own list (its installed apps are marked), not the catalog
        alone. The demo phone's apps are the catalog."""
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
        """(state, words, name) for the checklist's iPhone row, from the device list; None in the demo. Runs on a
        worker thread, at most once per poll (2 s)."""
        if self.demo or self.session is None:
            return None
        from ..native_helpers import devices_allowed
        if not devices_allowed():  # MOBSTER_NO_DEVICES=1, as in local CI: never list a phone that's plugged in
            return ("unplugged", "plug it in with a cable and unlock it", None)
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
            return ("unplugged", "plug it in with a cable and unlock it", None)
        name = item.get("name")
        state = item.get("state")
        reason = (item.get("reason") or "").lower()
        if state == "needs_setup" and "trust" in reason:
            return ("trust", "tap Trust on it", name)
        if state == "needs_setup":
            return ("setup", "run mobster setup, or set it up in Mobster for Mac", name)
        if state == "disconnected":
            return ("unplugged", "plug it in with a cable and unlock it", name)
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

    # -- update notice --------------------------------------------------------------------------

    @work(thread=True, group="update")
    def check_for_update(self):
        from .. import update_check
        import argparse
        if not update_check.enabled("chat", argparse.Namespace(json=False)):
            return
        update_check.start("chat", argparse.Namespace(json=False))
        if update_check._fetch is not None:
            update_check._fetch.join(5)
        line = update_check.notice(__version__, update_check._read_cache())
        if line:
            self.call_from_thread(self.show_update, line)

    def show_update(self, line):
        self.update_line = line.replace(" · mobster update", "  ·  quit, then run mobster update")
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

    # -- header, welcome, status line -----------------------------------------------------------

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
            return "●", GREEN, f"{self.phone_name()} · Ready"
        if not status:
            return "◌", FAINT, "Looking for your iPhone…"
        if self.phone and self.phone[0] != "unplugged":
            words = {"trust": "Tap Trust on it", "setup": "Needs setup", "locked": "Locked",
                     "starting": "Starting"}.get(self.phone[0], "Not ready")
            return "○", AMBER if self.phone[0] in ("trust", "locked") else FAINT, f"{self.phone_name()} · {words}"
        return "○", FAINT, "No iPhone yet"

    def refresh_chrome(self):
        header = Text()
        header.append("Mobster", style=f"bold {TEXT}")
        header.append(f" {__version__}", style=FAINT)
        dot, color, words = self.device_line()
        header.append(f"   {dot} ", style=color)
        header.append(words, style=MUTED)
        if self.demo:
            header.append("   DEMO", style=f"bold {ACCENT}")
            header.append(" no iPhone, no model calls", style=FAINT)
        self.query_one("#header", Static).update(header)
        self.refresh_status()
        self.refresh_chip()

    def refresh_status(self):
        text = Text()
        ask, bypass = self.settings.get("askBeforeActing", True), self.settings.get("bypassChecks", False)
        if ask:
            text.append("⏸ ask before acting", style=ACCENT)
        else:
            text.append("⏵⏵ acting without asking", style=AMBER)
        if bypass:
            text.append("  ⚠ bypass on", style=AMBER)
        if self.preview_next:
            text.append("  ◌ preview only", style=ACCENT)
        text.append("  (shift+tab)", style=FAINT)
        right = Text()
        narrator = self.run_view.narrator if self.run_view else None
        if self.running is not None and narrator is not None:
            right.append(f"step {self.run_view.steps:>2}", style=MUTED)
            right.append(f" · {clock(narrator.elapsed_ms(time.time() * 1000))}", style=MUTED)
            if narrator.priced_calls or narrator.unpriced_calls:
                right.append("  ·  ", style=FAINT)
                right.append(usd(narrator.cost_nanodollars), style=TEXT)
        elif not self.welcome_checklist_shown():
            right.append(self.model_words(), style=MUTED)
        cap = self.session.spend_cap_usd if self.session else None
        if cap is not None:
            right.append(f"  ·  cap ${cap:.2f}", style=FAINT)
        sep = "  ·  " if right.plain else ""
        if self.running is not None:
            right.append(f"{sep}esc to stop", style=FAINT)
        else:
            right.append(f"{sep}/ commands  ·  ? keys", style=FAINT)
        width = self.size.width - 4
        gap = max(2, width - len(text.plain) - len(right.plain))
        if len(text.plain) + len(right.plain) + 2 > width:
            text = Text("⏸ ask" if ask else "⏵⏵ act", style=ACCENT if ask else AMBER)
            if bypass:
                text.append(" ⚠ bypass", style=AMBER)
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

    def next_engine(self):
        """The engine the next task runs on: the runtime's default (Smart on a Claude or OpenAI key, Fast for a Jev
        key alone), or Fast for a preview, which only Fast makes (``Runtime.create``)."""
        if self.preview_next:
            return "fast"
        return self.status.get("defaultEngine") or "smart"

    def engine_state(self, engine=None):
        """{available, reason, model} for ``engine`` (default: the next task's), from the runtime's status."""
        return (self.status.get("engines") or {}).get(engine or self.next_engine()) or {}

    def key_ready(self):
        from ..engines import fast_ready, smart_key
        return bool(smart_key() or fast_ready())

    def model_words(self):
        if self.demo:
            return "scripted demo"
        if not self.status:
            return "checking…"  # which engine runs depends on the keys the session loads
        if not self.key_ready():
            return "no key yet"
        engine = self.next_engine()
        if engine == "smart":
            return self.engine_state("smart").get("model") or "Mobster's agent"
        jev = self.engine_state("fast").get("model") or os.environ.get("TYPESAFE_MODEL", "jev-latest")
        helper = self.current_helper()
        return f"Quick mode · {jev}" + (f" · {helper}" if helper else "")

    def current_helper(self):
        if self.helper_chosen:
            return self.helper_model
        return self.status.get("helper_model") or os.environ.get("TEXT_MODEL") or None

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

    # -- the welcome screen: the mark, the checklist, examples ---------------------------------------------

    def refresh_welcome(self):
        welcome = self.query_one("#welcome", Static)
        welcome.update(self.welcome())

    def checklist(self):
        """[(done, label, value, hint)] for the three things a first task needs, or [] when all are done."""
        if self.demo or self.session is None or not self.status:
            return []
        from ..engines import model_provider, smart_key, smart_model
        rows = []
        if smart_key():
            from ..login import source_words
            claude = model_provider(smart_model()) == "anthropic"
            name, variable = ("Claude", "ANTHROPIC_API_KEY") if claude else ("OpenAI", "OPENAI_API_KEY")
            rows.append((True, "Model key", f"{name} · {source_words(variable)}", ""))
        elif os.environ.get("TYPESAFE_API_KEY"):
            rows.append((True, "Model key", "Jev, for Quick mode", ""))
        else:
            rows.append((False, "Model key", "add your Claude or OpenAI key", "/login"))
        if self.status.get("device_ready"):
            rows.append((True, "iPhone", f"{self.phone_name()} · Ready", ""))
        else:
            words = self.phone[1] if self.phone else "plug it in with a cable and unlock it"
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
        title.append("Mobster", style=f"bold {TEXT}")
        title.append(f" {__version__}\n", style=FAINT)
        title.append("Do anything on your iPhone with agents.", style=MUTED)
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
            head.append(f" · {done} of 3 done", style=FAINT)
            for ok, label, value, hint in rows:
                words = Text(value, style=FAINT if ok else MUTED)
                if hint and not ok:
                    words.append(f"  {hint}", style=ACCENT)  # beside its row, as the error screen has it
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
        """Whether the next task's engine can run: its key is set (and reaches its model)."""
        state = self.engine_state()
        return bool(state.get("available")) if state else bool(self.status.get("jev_configured"))

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
        if self.status and self.key_ready() and not self.engine_ready():
            reason = self.engine_state().get("reason")
            if reason:
                text.append("  ○ ", style=ACCENT)
                text.append(reason + "\n", style=MUTED)
        return text

    # -- ticking --------------------------------------------------------------------------------

    def tick(self):
        if self.running is None:
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

    # -- the prompt -----------------------------------------------------------------------------

    def target_app(self, value):
        """The app the next task starts in: the one chosen, the one it names, else None (Mobster picks)."""
        if self.app_choice:
            return self.app_choice
        goal, mentioned = self.split_mentions(value)
        return mentioned or (infer_app(goal, self.apps) if goal.strip() else None)

    def split_mentions(self, value):
        """(the task without the @words that name an app, the first app they name). Any other @word, such as
        a handle ("Follow @nasa"), is part of the task and stays in it."""
        named = []

        def drop(match):
            app = find_app(match.group(1), self.apps)
            if app is None:
                return match.group(0)
            named.append(app)
            return ""
        return MENTION.sub(drop, value).strip(), (named[0] if named else None)

    @on(Input.Changed, "#prompt")
    def prompt_changed(self, event):
        self.refresh_chip()
        self.suggest(event.value)

    def suggest(self, value):
        box = self.query_one("#suggest", OptionList)
        hint = self.query_one("#suggest-hint", Static)
        options = []
        footer = ""
        if value.startswith("/") and " " not in value:
            query = value[1:].casefold()
            matches = [c for c in COMMANDS if c.name.startswith(query) or (query and query in c.name)]
            name_width = max((len(c.name) + 1 for c in matches), default=0) + 2
            help_width = max((len(c.help) for c in matches), default=0) + 2
            for section in SECTIONS:
                rows = [c for c in matches if c.section == section]
                if not rows:
                    continue
                if not query:
                    options.append(Option(Text(section, style=f"bold {FAINT}"), id=f"section:{section}",
                                          disabled=True))
                for command in rows:
                    prompt = Text()
                    prompt.append(f"/{command.name}".ljust(name_width), style=f"bold {ACCENT}")
                    prompt.append(command.help.ljust(help_width), style=TEXT)
                    prompt.append(command.key or (command.args if command.args.startswith("<") else ""),
                                  style=FAINT)
                    options.append(Option(prompt, id=f"cmd:{command.name}"))
            footer = "↑↓ move · enter choose · esc close"
        else:
            path = PATH_MENTION.search(value)
            match = re.search(r"(?:^|\s)@([\w.-]*)$", value)
            if path:
                for entry in self.path_matches(path.group(1))[:10]:
                    prompt = Text()
                    prompt.append(entry["shown"], style=f"bold {TEXT}")
                    prompt.append("  folder" if entry["dir"] else "  attach", style=FAINT)
                    options.append(Option(prompt, id=f"path:{entry['path']}"))
                footer = "enter attach · tab open a folder · esc close"
            elif match:
                query = match.group(1).casefold()
                # Best match first (tab and enter take the first row): "@fa" is FaceTime, not safari.
                ranked = sorted(((rank, index, app) for index, app in enumerate(self.apps)
                                 if (rank := mention_rank(app, query)) is not None), key=lambda item: item[:2])
                for _, _, app in ranked[:10]:
                    prompt = Text()
                    prompt.append(app["name"], style=f"bold {TEXT}")
                    prompt.append(f"  {app['bundleId']}", style=FAINT)
                    if app.get("installed"):
                        prompt.append("  installed", style=GREEN)
                    options.append(Option(prompt, id=f"app:{app['id']}"))
                footer = "enter choose · esc close"
        box.clear_options()
        if options:
            box.add_options(options)
            first = next((i for i, option in enumerate(options) if not option.disabled), 0)
            box.highlighted = first
        box.set_class(bool(options), "open")
        hint.update(Text(footer, style=FAINT))
        hint.set_class(bool(options), "open")

    def path_matches(self, typed):
        """Files and folders for an @./, @~/ or @/ path: [{path, shown, dir}]."""
        from pathlib import Path
        expanded = os.path.expanduser(typed)
        folder, _, stem = expanded.rpartition("/")
        folder = folder + "/" if folder or typed.startswith("/") else "./"
        try:
            entries = sorted(Path(folder or "/").iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        except OSError:
            return []
        out = []
        for entry in entries:
            if entry.name.startswith(".") and not stem.startswith("."):
                continue
            if not entry.name.casefold().startswith(stem.casefold()):
                continue
            shown = typed[:len(typed) - len(stem)] + entry.name + ("/" if entry.is_dir() else "")
            out.append({"path": str(entry), "shown": shown, "dir": entry.is_dir()})
        return out

    def suggestions_open(self):
        return self.query_one("#suggest", OptionList).has_class("open")

    def close_suggestions(self):
        self.query_one("#suggest", OptionList).set_class(False, "open")
        self.query_one("#suggest-hint", Static).set_class(False, "open")

    def accept_suggestion(self, key_name="enter"):
        box = self.query_one("#suggest", OptionList)
        if box.highlighted is None:
            return False
        option = box.get_option_at_index(box.highlighted)
        if option.disabled:
            return False
        prompt = self.query_one("#prompt", Input)
        kind, _, key = option.id.partition(":")
        if kind == "cmd":
            command = next(c for c in COMMANDS if c.name == key)
            if command.args and not command.args.startswith("["):
                prompt.value = f"/{key} "
            else:
                prompt.value = ""
                self.run_command(key, "")
        elif kind == "app":
            self.app_choice = next((a for a in self.apps if a["id"] == key), None)
            prompt.value = re.sub(r"(?:^|\s)@[\w.-]*$", "", prompt.value).lstrip()
        elif kind == "path":
            if os.path.isdir(key) and key_name == "tab":
                typed = PATH_MENTION.search(prompt.value).group(1)
                stem = typed.rpartition("/")[2]
                prompt.value = prompt.value[:len(prompt.value) - len(stem)] + os.path.basename(key) + "/"
                prompt.cursor_position = len(prompt.value)
                self.suggest(prompt.value)
                return True
            prompt.value = PATH_MENTION.sub("", prompt.value).rstrip()
            if not os.path.isdir(key):
                self.attach(key)
        prompt.cursor_position = len(prompt.value)
        self.close_suggestions()
        self.refresh_chip()
        return True

    def prompt_key(self, key, character):
        """Keys the prompt hands over before editing: the popup's, history recall and ``?``. True if used."""
        if self.suggestions_open():
            box = self.query_one("#suggest", OptionList)
            if key in {"down", "up"}:
                box.action_cursor_down() if key == "down" else box.action_cursor_up()
                option = box.get_option_at_index(box.highlighted) if box.highlighted is not None else None
                if option is not None and option.disabled:
                    box.action_cursor_down() if key == "down" else box.action_cursor_up()
                return True
            if key in {"tab", "enter"}:
                return self.accept_suggestion(key)
            if key == "escape":
                self.close_suggestions()
                return True
            return False
        if key in {"up", "down"}:
            self.action_recall(-1 if key == "up" else 1)
            return True
        if character == "?" and not self.query_one("#prompt", Input).value:
            self.run_command("help", "")
            return True
        return False

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

    def action_recall(self, delta):
        prompt = self.query_one("#prompt", Input)
        # With no past tasks yet, ↑ steps through the Try rows on the welcome screen, the first one first.
        recalls = self.recalls or [goal for _, goal in reversed(EXAMPLES)]
        if self.focused is not prompt or self.suggestions_open() or self.running is not None:
            return
        if self.recall_index is None:
            if delta > 0:
                return
            self.recall_index = len(recalls)
        self.recall_index = max(0, min(len(recalls), self.recall_index + delta))
        prompt.value = recalls[self.recall_index] if self.recall_index < len(recalls) else ""
        prompt.cursor_position = len(prompt.value)

    # -- running a task -------------------------------------------------------------------------

    def start_task(self, app, goal):
        if app is not None:
            self.last_app = app
        self.app_choice = None
        self.query_one("#composer", Composer).add_class("busy")
        attachments = [item["id"] for item in self.attachments]
        self.attachments = []
        self.refresh_notice()
        self.submit_task(app, goal, self.preview_next, self.helper_request(), self.output_format, attachments)

    def helper_request(self):
        if self.demo:
            return None
        return self.helper_model if self.helper_chosen else self.current_helper()

    def default_app(self):
        """Where a task that names no app starts: the Home Screen for Mobster's agent; Settings for Quick mode and
        the demo, which need an app."""
        if self.demo or self.next_engine() == "fast":
            return self.last_app or next(a for a in self.apps if a["id"] == "settings")
        return None

    @work(thread=True, group="run")
    def submit_task(self, app, goal, preview, helper_model, output_format, attachments=()):
        from .session import error_text
        app = app or self.default_app()
        app_id = app["id"] if app else None
        try:
            threads = None if (preview or (helper_model and self.helper_chosen)) else self.session.threads()
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
                run = self.session.start(app_id or next(a for a in self.apps if a["id"] == "settings")["id"], goal,
                                         preview=preview, helper_model=helper_model, output_format=output_format)
        except Exception as error:
            self.call_from_thread(self.task_refused, app, goal, error_text(error))
            return
        self.call_from_thread(self.task_started, run)
        self.session.follow(run, lambda event: self.call_from_thread(self.task_event, run, event),
                            stop=self.follow_stop)

    def remember_here(self, goal):
        """Without conversations (the demo): "Remember that …" is a note for Mobster, offered as the service offers
        it, and nothing runs. Returns the proposals, or None to run it as a task."""
        try:
            from ..memory import extract
            from ..memory.store import default_store
        except ImportError:
            return None
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
        """A message that only asked Mobster to remember something: one card per suggestion, Remember or Not now.
        Nothing is saved without a yes."""
        self.task_idle()
        self.hide_welcome()
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
                self.answer_proposal(proposal, bool(yes), box)
                ask_next()
            self.push_screen(AskScreen("Remember this?", body, "Remember", "Not now"), answered)
        ask_next()

    @work(thread=True, group="memory")
    def answer_proposal(self, proposal, yes, box):
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
        self.call_from_thread(self.proposal_answered, box, yes, message)

    def proposal_answered(self, box, yes, message):
        text = Text()
        text.append("  ✓ " if yes else "  · ", style=GREEN if yes else FAINT)
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
            self.run_view.note(STEER_PREFIX + f"“{text}”", MUTED)
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
        self.query_one("#composer", Composer).remove_class("busy")
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
        self.hide_welcome()
        transcript.mount(view)
        transcript.scroll_end(animate=False)
        self.recall_index = None

    def hide_welcome(self):
        welcome = self.query_one("#welcome", Static)
        welcome.display = False

    def task_started(self, run):
        self.hide_welcome()
        self.running = run
        self.run_view = RunView(run, self.apps)
        self.finished_views.append(self.run_view)
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.mount(self.run_view)
        transcript.scroll_end(animate=False)
        prompt = self.query_one("#prompt", Input)
        prompt.placeholder = STEER_PLACEHOLDER if run.engine == "smart" else "Mobster is working… esc stops it"
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
        self.preview_next = False
        self.query_one("#composer", Composer).remove_class("busy")
        status = run.status
        if self.bell_enabled and status_tone(status) in {"error", "warning"}:
            self.bell()
        self.refresh_status()
        self.refresh_chip()
        self.query_one("#prompt", Input).focus()
        if status == "approval_denied":
            self.query_one("#prompt", Input).placeholder = "Tell Mobster what to do instead…  (↑ for the last task)"
        else:
            self.query_one("#prompt", Input).placeholder = "What should Mobster do next?" if self.thread_id \
                else PLACEHOLDER

    def show_approval(self, request):
        card = self.query_one(ApprovalCard)
        card.ask(request)
        card.add_class("open")
        card.focus()
        if self.bell_enabled:
            self.bell()

    def hide_approval(self):
        card = self.query_one(ApprovalCard)
        if card.has_class("open"):
            card.remove_class("open")
            card.request = None
            self.query_one("#prompt", Input).focus()

    def answer_approval(self, request, choice):
        if not request or self.session is None:
            return
        run = self.running
        approve = choice != "__no"
        picked = None if choice in {"__yes", "__no"} else choice
        self.hide_approval()
        self.send_answer(run, request["id"], approve, picked)

    @work(thread=True, group="answer")
    def send_answer(self, run, approval_id, approve, choice):
        try:
            self.session.answer(approval_id, approve, choice, run=run)
        except Exception as error:
            from .session import error_text
            self.call_from_thread(self.note, error_text(error), "error")

    def note(self, message, tone="neutral"):
        color = {"warning": AMBER, "error": RED}.get(tone, MUTED)
        self.notify(message, severity={"warning": "warning", "error": "error"}.get(tone, "information"),
                    timeout=4)
        return color

    # -- conversations ----------------------------------------------------------------------------

    def remember_thread(self, thread_id):
        try:
            from ..threads.cli import remember_thread
            remember_thread(LOCAL_THREADS, thread_id)
        except Exception:  # noqa: BLE001 -- -c falls back to a new conversation
            pass

    def continue_thread(self, thread_id=None):
        """`mobster -c`, or a conversation chosen in /threads: its tasks back in the transcript, the next task in it."""
        if self.session is None or self.session.threads() is None:
            if self.continue_requested and not self.demo:
                self.note("Conversations need Mobster's task history, which this window doesn't keep.", "warning")
            return
        live = self.session.threads()
        if thread_id is None:
            try:
                from ..threads.cli import last_thread
                thread_id = last_thread(LOCAL_THREADS)
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
            self.hide_welcome()
        title = thread.get("title") or "your last conversation"
        self.notify(f"Continuing “{title}”. ctrl+n starts a new one.", timeout=4)
        for run in shown:
            view = RunView(run, self.apps, past=True)
            self.run_view = view
            self.replay(view, list(run.events))
        self.query_one("#prompt", Input).placeholder = "What should Mobster do next?"

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
        welcome = self.query_one("#welcome", Static)
        welcome.update(self.welcome())
        welcome.display = True
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
            text.append("  ● " if fact.get("pinned") else "  · ", style=ACCENT if fact.get("pinned") else FAINT)
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

    # -- keys -----------------------------------------------------------------------------------

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
        self.refresh_status()
        if self.session is not None:
            self.save_setting(name, value)
        if self.running is not None:
            self.notify("Applies from the next task.", timeout=3)

    @work(thread=True, group="settings")
    def save_setting(self, name, value):
        try:
            settings = self.session.set_setting(name, value)
        except Exception as error:
            from .session import error_text
            self.call_from_thread(self.note, error_text(error), "error")
            return
        self.settings.update(settings)
        self.call_from_thread(self.refresh_status)

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
            welcome = self.query_one("#welcome", Static)
            welcome.update(self.welcome())
            welcome.display = True
        self.refresh_status()

    # -- the phone panel ------------------------------------------------------------------------

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
            return f"{screen.get('app') or view.run.app['name']} · {len(screen['elements'])} elements read"
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

    # -- commands -------------------------------------------------------------------------------

    def run_command(self, name, argument):
        handler = getattr(self, f"command_{name}", None)
        if handler is None:
            self.notify(f"There's no /{name}. Type / to see every command.", severity="warning", timeout=3)
            return
        handler(argument)

    def command_quit(self, argument):
        self.exit()

    def command_help(self, argument):
        from rich.table import Table
        from rich.console import Group
        groups = []
        for title, pairs in KEY_GROUPS:
            grid = Table.grid(padding=(0, 2))
            grid.add_column(width=11, no_wrap=True)
            grid.add_column()
            for key, words in pairs:
                dim = title == "Task" and key in ("y / n",) and self.running is None
                grid.add_row(Text(key, style=QUATERNARY if dim else ACCENT), Text(words, style=FAINT if dim else MUTED))
            groups += [Text(title, style=f"bold {TEXT}"), grid, Text("")]
        approvals = Text()
        approvals.append("Approvals\n", style=f"bold {TEXT}")
        approvals.append("With “Ask before acting” on, Mobster stops before it sends, buys, posts or deletes, shows "
                         "you the action and the exact text, and waits for your answer. Bypass lets it act when its "
                         "own check is unsure; approvals still apply.", style=MUTED)
        docs = Text()
        docs.append("\nEvery command: type /   ·   Docs  ", style=FAINT)
        docs.append(links.TUI, style=ACCENT)
        body = Group(*groups, approvals, docs)
        self.push_screen(InfoScreen(body, "Keys"))

    def command_ask(self, argument):
        self.action_toggle_ask()
        self.notify("Ask before acting is " + ("on" if self.settings["askBeforeActing"] else "off"), timeout=2)

    def command_bypass(self, argument):
        value = not self.settings.get("bypassChecks", False)
        self.change_setting("bypassChecks", value)
        if value:
            self.notify("Bypass is on: Mobster acts when its action check is unsure. A mismatch still stops it.",
                        severity="warning", timeout=5)
        else:
            self.notify("Bypass is off.", timeout=2)

    def command_preview(self, argument):
        fast = self.engine_state("fast")
        if not self.preview_next and not self.demo and fast and not fast.get("available"):
            # Only Fast previews a decision; Smart acts from its first step, so there is nothing to show.
            self.notify("A preview needs Quick mode: run `mobster login --provider jev`. Mobster's agent acts from "
                        "its first step, and asks before it sends, buys, posts or deletes.", severity="warning",
                        timeout=6)
            return
        self.preview_next = not self.preview_next
        self.refresh_status()
        self.notify("The next task previews its first decision without acting." if self.preview_next
                    else "Preview is off: the next task acts.", timeout=3)

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

    def command_model(self, argument):
        if self.demo:
            self.notify("The demo uses a scripted policy, not a model.", timeout=3)
            return
        choices = list(self.status.get("helper_models") or [])
        if argument:
            if argument.lower() in {"none", "off"}:
                self.helper_model, self.helper_chosen = None, True
            elif argument in choices:
                self.helper_model, self.helper_chosen = argument, True
            else:
                self.notify("Helper models here: " + (", ".join(choices) or "none set (TEXT_MODEL)"),
                            severity="warning", timeout=5)
                return
            self.refresh_status()
            return
        rates = self.status.get("helper_model_rates") or {}
        options = []
        for model in choices:
            prompt = Text(f"{model:<28}", style=TEXT)
            rate = rates.get(model)
            if isinstance(rate, dict):
                prompt.append(" ".join(f"{k} {v}" for k, v in rate.items())[:40], style=FAINT)
            if model == self.current_helper():
                prompt.append("  current", style=GREEN)
            options.append((model, prompt, model))
        options.append(("__none", Text("No helper: Quick mode stops when a step needs typed text", style=MUTED),
                        "none"))

        def chosen(key):
            if key:
                self.helper_model, self.helper_chosen = (None if key == "__none" else key), True
                self.refresh_status()
        jev = os.environ.get("TYPESAFE_MODEL", "jev-latest")
        self.push_screen(Picker(f"Quick mode's helper model  (decisions: {jev})", options, "Type to filter",
                                "No helper models set. Set TEXT_MODEL in your env file."), chosen)

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
            prompt.append({"success": "✓", "error": "✗", "warning": "!", "review": "◆"}.get(
                status_tone(run.status), "○" if run.status in {"blocked", "no_progress"} else "■") + " ", style=tone)
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
        self.hide_welcome()
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
        from .. import doctor
        if self.demo:
            text = Text()
            text.append("Scripted demo phone. ", style=f"bold {ACCENT}")
            text.append("No iPhone and no model is used. Leave with ctrl+d, then run `mobster` to use yours.\n",
                        style=MUTED)
            self.call_from_thread(screen.update_body, text)
            return
        checks = run_checks(wda_url=self.session.wda_url if self.session else None)
        body = doctor.rich_report(checks)
        body.append("\n\nAddress  ", style=FAINT)
        body.append((self.session.wda_url or "") if self.session else "", style=MUTED)
        body.append("\nSet up your iPhone: ", style=FAINT)
        body.append("mobster setup", style=ACCENT)
        body.append(" in another terminal, or Mobster for Mac.", style=FAINT)
        self.call_from_thread(screen.update_body, body)

    # -- leaving --------------------------------------------------------------------------------

    def transcript_summary(self, color=False):
        """The session, printed to the terminal after the UI closes so it stays in scrollback: each task with its
        steps, its result in Mobster for Mac's words, and how to open it again."""
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
                    words = {"approved": "You approved", "denied": "You declined", "timeout": "Nobody answered",
                             "stopped": "Stopped while asking"}.get(item.decision, "You chose")
                    lines.append("  " + paint("◆", "accent") + f" {words}: {item.title}")
                elif isinstance(item, Outcome):
                    outcome = item
            elapsed = narrator.elapsed_ms(time.time() * 1000)
            line = facts(narrator.actions, elapsed, narrator.cost_nanodollars, bool(narrator.priced_calls))
            if outcome is not None:
                glyph = {"success": "✓", "error": "✗", "warning": "!", "review": "◆"}.get(outcome.head_tone, "■")
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

