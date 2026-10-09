"""The terminal UI's commands and keys, without the UI library: the palette, the keys sheet (`?`) and
`mobster help tui` all read these."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    args: str
    help: str
    section: str = "Task"
    key: str = ""


COMMANDS = [
    Command("new", "", "Start a new conversation", "Conversation", "ctrl+n"),
    Command("threads", "", "Open a recent conversation", "Conversation", "ctrl+t"),
    Command("attach", "<path>", "Send a file with the next task", "Conversation", "@./"),
    Command("memory", "", "See what Mobster remembers", "Conversation"),
    Command("remember", "<text>", "Tell Mobster something to remember", "Conversation"),
    Command("app", "[name]", "Choose the app the next task starts in", "Task", "@app"),
    Command("ask", "", "Turn “Ask before acting” on or off", "Task", "shift+tab"),
    Command("preview", "", "Preview Quick mode's first decision, without acting", "Task"),
    Command("cap", "<usd>|off", "Set a spend cap per task, in US dollars", "Task"),
    Command("format", "<auto|text|json|yaml|csv|markdown>", "Choose the answer format", "Task"),
    Command("model", "[name]", "Choose Quick mode's helper model", "Task"),
    Command("bypass", "", "Act when Mobster's own check is unsure (approvals still apply)", "Task"),
    Command("history", "", "Browse past tasks", "View", "ctrl+r"),
    Command("resume", "[id]", "Open a past task and put it back in the box", "View"),
    Command("devices", "", "Your iPhone, and how to set it up", "View"),
    Command("login", "", "Save your Claude or OpenAI key", "View"),
    Command("screen", "", "Show or hide your iPhone's screen", "View", "ctrl+s"),
    Command("details", "", "Show every phase of each step", "View", "ctrl+o"),
    Command("clear", "", "Clear the transcript", "View", "ctrl+l"),
    Command("help", "", "Keys and commands", "View", "?"),
    Command("quit", "", "Leave Mobster", "View", "ctrl+d"),
]
SECTIONS = ("Conversation", "Task", "View")

KEY_GROUPS = (
    ("Task", (
        ("enter", "Run the task; while one runs, tell Mobster something"),
        ("esc", "Stop the task, or answer Don't to an approval"),
        ("y / n", "Answer an approval: its verb (Send, Pay…) or Don't"),
        ("ctrl+c", "Stop the task; press twice to quit"),
        ("shift+tab", "Ask before acting on or off"),
        ("@", "Pick the app: @messages text Sam…"),
        ("↑ ↓", "Previous tasks (the Try rows, before your first)"),
    )),
    ("Conversation", (
        ("ctrl+n", "New conversation"),
        ("ctrl+t", "Recent conversations"),
        ("@./ @~/", "Attach a file by its path"),
    )),
    ("View", (
        ("/", "Commands"),
        ("?", "This sheet"),
        ("ctrl+o", "Step details"),
        ("ctrl+s", "Your iPhone's screen"),
        ("ctrl+r", "History"),
        ("ctrl+l", "Clear"),
        ("ctrl+d", "Quit"),
    )),
)
KEYS = [pair for _, pairs in KEY_GROUPS for pair in pairs]
