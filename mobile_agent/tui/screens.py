"""The terminal UI's modal screens: each returns a value (or None) and knows nothing of the session."""

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .render import ACCENT, FAINT, GREEN, MUTED, RED, TEXT


class Picker(ModalScreen):
    """A filterable list: apps, history, conversations. Returns the chosen option id, or None."""

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
            yield Static(Text("↑↓ move   enter choose   esc close", style=FAINT), id="picker-hint")

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
        text.append("   n or esc", style=FAINT)
        with Vertical(id="ask"):
            yield Static(Text(self.title_text, style=f"bold {TEXT}"), id="ask-title")
            yield Static(text, id="ask-body")


class LoginScreen(ModalScreen):
    """/login: Claude or OpenAI, then the key in a password field."""

    BINDINGS = [Binding("escape", "dismiss(None)", show=False)]

    def __init__(self):
        super().__init__()
        self.provider = "anthropic"

    def compose(self):
        from ..login import PROVIDERS
        with Vertical(id="login"):
            yield Static(Text("Connect your AI account", style=f"bold {TEXT}"), id="login-title")
            yield Static(self.choice_text(), id="login-choice")
            yield Input(password=True, id="login-key",
                        placeholder=f"Paste your {PROVIDERS[self.provider]['name']} key "
                        f"({PROVIDERS[self.provider]['prefix']}…)")
            yield Static(Text("Mobster's agent runs on your own Claude or OpenAI key. You pay them for what it uses, "
                              "and the key stays on this Mac.", style=FAINT), id="login-note")
            yield Static(Text("tab Claude or OpenAI   enter connect   esc close", style=FAINT), id="login-hint")

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
        from ..login import save_key
        result = save_key(self.provider, key)
        self.app.call_from_thread(self.checked, result["ok"], result["message"])

    def checked(self, ok, message):
        if ok:
            self.dismiss(self.provider)
        else:
            self.query_one("#login-note", Static).update(Text(message, style=RED))
