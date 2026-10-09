"""`mobster --help`, `mobster help --all` and the help topics (`environment`, `exit-codes`, `tui`).

The top-level help is drawn from the tables here, without building the argument parser, so `mobster --help` starts
as fast as `--version`. Each command's own help (`mobster help run`) still comes from its parser. A test checks
that every command listed here exists and that every hidden one stays out of the top-level help.
"""

import os

from . import links

HERO = "Do anything on your iPhone with agents."
# What the command does, in plain words: the task, the cable, the approval, and the agents you already have. No
# Codex or Cursor until S7 (MESSAGING §1.3). Proposed for MESSAGING §1.1 in polish/cli/HANDOFF-messaging.md.
LINE_2 = ("Type a task and Mobster's agent does it on your iPhone, over USB from your Mac. It asks before it sends, "
          "buys, posts or deletes. Claude Code and your own agents can use the phone too.")

# Every command and its one line, in the order `mobster --help` lists them. The parser uses the same lines.
HELP = {
    "login": "save your Claude or OpenAI key for Mobster on this Mac",
    "doctor": "check your iPhone, key and tools, with the fix for each",
    "setup": "set up your iPhone for Mobster, one step at a time",
    "devices": "list the iPhones and simulators Mobster can use",
    "chat": "talk to Mobster's agent; follow-ups know what earlier tasks found",
    "run": "run one task and print each step (JSON lines when piped)",
    "memory": "see and edit what Mobster remembers",
    "history": "list past tasks",
    "export": "save a past task as a captioned GIF or MP4",
    "screen": "show your iPhone's screen in the terminal",
    "test": "run your app's checks on simulators and iPhones, with reports",
    "verify": "check one screen of your app and print passed or failed",
    "mcp": "let your coding agent use the phone and check your app",
    "sim": "create and clean up Mobster's simulators",
    "serve": "start the local API that Mobster for Mac and other clients use",
    "phone": "set your iPhone's clipboard, put files on it, or install a build",
    "alerts": "test the webhook that hears about scheduled tasks",
    "demo": "replay a scripted task: no phone, no keys",
    "completion": "print a shell completion script (zsh, bash or fish)",
    "update": "update Mobster to the latest release",
    "version": "print the version",
    "logout": "remove a saved key from this Mac",
    "help": "show the help for a command or a topic",
    "wifi": "use an iPhone without the cable, over the encrypted Wi-Fi link",
    "tui": "open the terminal UI (what `mobster` does on its own)",
    "build-ocr": "compile the optional Apple Vision OCR helper (needs Xcode)",
    "decide-fixture": "ask Jev for one decision on a synthetic screen (needs a key)",
}

GROUPS = (
    ("Get started", ("login", "doctor", "setup", "devices")),
    ("Your tasks", ("chat", "run", "memory", "history", "export", "screen")),
    ("Test your app", ("test", "verify", "mcp", "sim")),
)
MORE = ("serve", "phone", "alerts", "demo", "completion", "update", "version", "logout")
# Registered, but never in the top-level help: internals, the parser's own aliases, and switches that are off.
HIDDEN = ("help", "tui", "build-ocr", "decide-fixture", "wifi")
COMMANDS = tuple(HELP)

# The More group in `mobster --help`: the commands people use most of the rest. `mobster help --all` lists every one.
MORE_ROWS = ("phone", "demo", "completion", "update", "logout")

# (the command, its task and options): the command is painted as a command, the rest as text.
EXAMPLES = (
    ("mobster", '"Turn on Dark Mode"'),
    ("mobster chat", '"Find my last message from Kate Bell"'),
    ("mobster run", """"What's my battery level?" --execute --json"""),
    ("mobster test", ""),
    ("mobster mcp install", "--all"),
)

# The options every terminal knows to look for, and the two that change what a command does.
OPTIONS = (
    ("-h, --help", "show this help; after a command, that command's help"),
    ("--version", "print the version"),
    ("--demo", "try Mobster on a pretend iPhone: no phone or key needed"),
    ("--json", "print JSON for scripts, on run, chat, doctor and devices"),
)

TOPICS = {
    "environment": "every variable Mobster reads",
    "exit-codes": "what each exit code means",
    "tui": "the terminal UI's options and keys",
}

# One table for every command's exit codes: `mobster help exit-codes` and the docs' reference render it.
EXIT_CODES = (
    ("0", "done", "verify and test: passed"),
    ("1", "the task or check didn't succeed", "verify and test: failed"),
    ("2", "usage error: a mistyped command or option", "verify and test: needs review; chat: it waits for you"),
    ("3", "couldn't run: no phone, no key, nothing serving", "verify and test: usage errors too"),
    ("4", "chat: still working when --wait ran out; the task goes on", ""),
    ("130", "stopped with ctrl+c (128 + the signal for SIGTERM and SIGHUP)", ""),
)

# (name, default, meaning) for the variables people set. Every other MOBSTER_ variable the code reads is a tuning or
# development switch, listed by name under TUNING.
ENVIRONMENT = (
    ("Keys", (
        ("ANTHROPIC_API_KEY", "", "your Claude key: Mobster's agent (Smart) runs on Claude"),
        ("OPENAI_API_KEY", "", "your OpenAI key: Smart runs on OpenAI"),
        ("TYPESAFE_API_KEY", "", "Jev's key, for Quick mode (Fast) and `mobster run --engine fast`"),
        ("MOBSTER_SMART_PROVIDER", "", "anthropic or openai: which key Smart uses when both are saved"),
        ("MOBSTER_SMART_MODEL", "", "the model Smart runs on, instead of the default for your key"),
        ("MOBSTER_DEFAULT_ENGINE", "smart", "smart or fast: the engine a task runs on"),
        ("TEXT_MODEL", "", "the helper model that types and answers for Fast (with TEXT_MODEL_API_KEY, "
                           "TEXT_MODEL_BASE_URL, TEXT_MODEL_PROVIDER)"),
        ("MOBSTER_HELPER_PROVIDER", "", "the helper's provider, as Mobster for Mac saves it"),
    )),
    ("Files and folders", (
        ("MOBSTER_ENV_FILE", "", "a KEY=VALUE file every command loads, like --env-file"),
        ("MOBSTER_DATA_DIR", "~/Library/Application Support/app.mobster.desktop/dev",
         "where verify, sim and mcp keep simulators and builds"),
        ("MOBSTER_RUNS_DIR", "./.mobster/runs", "where verify and test write their reports"),
        ("MOBSTER_MEMORY_DIR", "", "where what Mobster remembers is kept"),
    )),
    ("Your iPhone", (
        ("MOBSTER_WDA_URL", "http://127.0.0.1:8100", "the iPhone's address (the --wda-url default)"),
        ("MOBSTER_WDA_DEVICES", "", "more phone addresses for `mobster devices`, comma-separated"),
        ("MOBSTER_ENABLE_LIVE", "1", "0 stops tasks from tapping and typing in the terminal UI"),
        ("MOBSTER_ASK_BEFORE_ACTING", "1", "0 turns off Ask before acting in the terminal UI"),
        ("MOBSTER_BYPASS_CHECKS", "0", "1 lets Mobster act when its own check is unsure; approvals still apply"),
        ("MOBSTER_BLOCKED_APPS", "", "bundle IDs Mobster's agent must never open, comma-separated"),
        ("MOBSTER_ASK_USER", "on", "off stops Mobster's agent from asking you questions mid-task"),
        ("MOBSTER_PHONE_VIEW", "auto", "image, text or off: how the terminal UI shows the phone"),
        ("MOBSTER_MAX_SIMS", "", "the most simulators `mobster test` boots at once"),
    )),
    ("Mobster for Mac and `mobster serve`", (
        ("MOBSTER_URL", "http://127.0.0.1:8765", "the Mobster that `mobster chat` talks to"),
        ("MOBSTER_API_TOKEN", "", "that Mobster's API token (or MOBSTER_API_TOKEN_FILE, a file holding it)"),
        ("MOBSTER_TASK_COST_LIMIT", "", "stop a task once it has cost this many US dollars"),
        ("MOBSTER_MONTHLY_LIMIT", "", "stop starting tasks once this month has cost this many US dollars"),
        ("MOBSTER_ALERT_WEBHOOK_URL", "", "where a scheduled task's failures and approvals are posted"),
        ("MOBSTER_KEEP_AWAKE", "1", "0 lets the Mac sleep while workflows are scheduled"),
    )),
    ("Output", (
        ("NO_COLOR", "", "any value turns colour off"),
        ("MOBSTER_PLAIN", "", "1: plain text, no colour or spinners (for screen readers and logs)"),
        ("MOBSTER_THEME", "", "light or dark: the colours for your terminal's background (default: asked)"),
        ("MOBSTER_NO_UPDATE_CHECK", "", "1 turns off the daily check for a new release"),
        ("CI", "", "any value: no update check, no questions"),
    )),
)

TUNING = ("MOBSTER_API_TOKEN_FILE", "MOBSTER_APP_SESSION", "MOBSTER_DEBUG_CROPS", "MOBSTER_DECISION_MEMO",
          "MOBSTER_DIRECT_URL", "MOBSTER_EARLY_CALL", "MOBSTER_EARLY_DECISION", "MOBSTER_EXACT_TEXT",
          "MOBSTER_FLICK", "MOBSTER_FRAME_CLOCK", "MOBSTER_FRAME_CLOCK_LOG", "MOBSTER_FRAME_CLOCK_TRACE",
          "MOBSTER_FRONTIER_CONTRACT", "MOBSTER_FRONTIER_MACROS", "MOBSTER_FRONTIER_REPLY", "MOBSTER_GLIDE",
          "MOBSTER_GLIDE_ALL", "MOBSTER_GLIDE_BUNDLES", "MOBSTER_GLIDE_READ_LIST", "MOBSTER_HEDGE",
          "MOBSTER_HEDGE_RATIO", "MOBSTER_IPHONE_TOOLS", "MOBSTER_MILESTONES", "MOBSTER_NO_DEVICES",
          "MOBSTER_PAGE_PROBE", "MOBSTER_PLANS", "MOBSTER_PRINT_TIMING", "MOBSTER_RENEW_WINDOW_HOURS",
          "MOBSTER_RICH_ROWS", "MOBSTER_ROUTES", "MOBSTER_SEARCH_PATHS", "MOBSTER_SIM_PORT_BASE",
          "MOBSTER_SIM_WDA_XCTESTRUN", "MOBSTER_SKIP_SWIFT", "MOBSTER_TAP_XY", "MOBSTER_TEXT_SELECTION",
          "MOBSTER_TYPING_FREQUENCY", "MOBSTER_VIDEO_DETAIL", "MOBSTER_VIDEO_FPS", "MOBSTER_VISION_T0",
          "MOBSTER_WDA_RECOVERY", "MOBSTER_WIFI_TRANSPORT", "MOBSTER_HOME", "MOBSTER_BIN_DIR",
          "MOBSTER_INSTALL_BASE_URL")

AGENT_VARIABLES = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_MANAGED_BY_NPM", "OPENCODE",
                   "CURSOR_AGENT", "GEMINI_CLI")


def under_agent(env=None):
    """Whether a coding agent runs this command (it sets one of AGENT_VARIABLES)."""
    env = os.environ if env is None else env
    return any(env.get(name) for name in AGENT_VARIABLES)


def visible(name, env=None):
    """Whether ``name`` shows in the top-level help: hidden commands never; `wifi` only when its switch is on."""
    env = os.environ if env is None else env
    if name == "wifi":
        return str(env.get("MOBSTER_WIFI_TRANSPORT", "")).strip().lower() in {"1", "on", "true", "yes"}
    return name not in HIDDEN


def top_level(paint, width=100):
    """`mobster --help`: one description column for every section, purple only for what you type, and the
    placeholders (`"TASK"`, `<command>`) in grey."""
    def heading(text):
        return paint(text, "bold")

    def command(words):
        """``mobster <command> [options]``: the words to type in accent, the placeholders in grey."""
        return " ".join(paint(word, "tertiary" if word[0] in '"<[' else "accent") for word in words.split(" "))

    usage = (("mobster", "open Mobster in this terminal"), ('mobster "TASK"', "open it and run TASK"),
             ("mobster <command> [options]", "run one of the commands below"))
    more = [name for name in MORE_ROWS if visible(name)] + (["wifi"] if visible("wifi") else [])
    learn = [("mobster help <command>", "options and examples for one command")]
    learn += [(f"mobster help {topic}", words) for topic, words in TOPICS.items()]
    learn += [("mobster help --all", "every command, with serve, alerts and version")]
    rows = [left for left, _ in usage + tuple(learn)] + [flag for flag, _ in OPTIONS]
    for _, names in GROUPS:
        rows += list(names)
    texts = [words for _, words in usage + tuple(learn) + OPTIONS] + [HELP[name] for name in rows if name in HELP]
    # One column for every section; a terminal too narrow for it gets the short one the command names need.
    column = max(len(left) for left in rows) + 2
    if 2 + column + max(len(text) for text in texts) > width:
        column = max(len(name) for name in rows if name in HELP or name.startswith("-")) + 2

    def row(left, words, painted=None):
        pad = " " * max(2, column - len(left))
        if len(left) >= column:
            return "  " + (painted or paint(left, "accent")) + "\n" + " " * (2 + column) + words
        return "  " + (painted or paint(left, "accent")) + pad + words

    lines = [paint(HERO, "bold")]
    lines += _wrap(LINE_2, width).splitlines()
    lines += ["", heading("Usage")]
    lines += [row(left, words, command(left)) for left, words in usage]
    for title, names in GROUPS:
        lines += ["", heading(title)]
        lines += [row(name, HELP[name]) for name in names if visible(name)]
    lines += ["", heading("More")]
    lines += [row(name, HELP[name]) for name in more]
    lines += ["", heading("Options")]
    lines += [row(flag, words) for flag, words in OPTIONS]
    lines += ["", heading("Examples")]
    lines += ["  " + paint("$ ", "tertiary") + paint(cmd, "accent") + (" " + rest if rest else "")
              for cmd, rest in EXAMPLES]
    lines += ["", heading("Learn more")]
    lines += [row(left, words, command(left)) for left, words in learn]
    lines += [row("Docs", paint(links.DOCS, "accent", "underline"), "Docs")]
    if under_agent():
        lines += ["", "Running under an agent? Use " + paint("mobster mcp", "accent") + ", or "
                  + paint("--json", "accent") + " on run, chat, doctor and devices."]
    return "\n".join(lines) + "\n"


def all_commands(paint, helps):
    """`mobster help --all`: every command the parser has, the hidden ones too. ``helps``: {name: help}."""
    lines = [paint("Every command", "bold")]
    width = max(len(name) for name in helps) + 2
    for name in sorted(helps):
        note = "" if visible(name) else paint("  (not in mobster --help)", "tertiary")
        lines.append("  " + paint(name.ljust(width), "accent") + (helps[name] or HELP.get(name, "")) + note)
    lines += ["", "Options and examples for one: " + paint("mobster help <command>", "accent")]
    return "\n".join(lines)


def environment(paint, width=100):
    lines = [paint("Environment variables", "bold"),
             _wrap("Mobster reads these from the environment, an --env-file, $MOBSTER_ENV_FILE and, last, the keys "
                   "Mobster for Mac saved on this Mac. A variable already set wins.", width)]
    for title, rows in ENVIRONMENT:
        lines += ["", paint(title, "bold")]
        for name, default, meaning in rows:
            lines.append("  " + paint(name, "accent"))
            text = meaning + (f" (default: {default})" if default else "")
            lines.append(_wrap(text, width, indent=4))
    lines += ["", paint("Tuning and development", "bold"),
              _wrap("These change how Mobster's agent works and are for development: " + ", ".join(TUNING) + ".",
                    width, indent=2),
              "", "More: " + paint(links.page("configuration", "environment-variables"), "accent")]
    return "\n".join(lines)


def exit_codes(paint, width=100):
    lines = [paint("Exit codes", "bold"),
             _wrap("Every command exits with one of these. Scripts can tell a usage error (2) from a phone or key "
                   "that isn't there (3).", width), ""]
    for code, meaning, exception in EXIT_CODES:
        lines.append("  " + paint(code.ljust(5), "accent") + meaning)
        if exception:
            lines.append(" " * 7 + paint(exception, "tertiary"))
    lines += ["", "Each command's own codes are at the end of its help: " + paint("mobster help run", "accent")]
    return "\n".join(lines)


TUI_OPTIONS = (
    ('"TASK"', "open with TASK and run it (it waits in the box until your iPhone and key are ready)"),
    ("-c, --continue", "continue your last conversation"),
    ("--resume [ID]", "open a past task and put it back in the box (the latest without an ID)"),
    ("--demo", "a scripted phone and policy: no iPhone, no keys, no model calls"),
    ("--device NAME", "the iPhone or simulator to use, from `mobster devices`"),
    ("--app NAME", "start with this app chosen (a name or bundle ID)"),
    ("--env-file PATH", "load keys and settings from a KEY=VALUE file [env: MOBSTER_ENV_FILE]"),
    ("--no-bell", "don't ring the terminal bell when Mobster needs you"),
    ("--wda-url URL", "the phone's address, for a phone you set up by hand [env: MOBSTER_WDA_URL]"),
)


def tui(paint, width=100, keys=()):
    lines = [paint("The terminal UI", "bold"),
             _wrap("`mobster` on its own opens Mobster in this terminal: tell it what to do, watch each step, and "
                   "answer when it asks before it sends, buys, posts or deletes.", width), "",
             paint("Usage", "bold"), "  " + paint('mobster [options] ["TASK"]', "accent"), "",
             paint("Options", "bold")]
    for flag, words in TUI_OPTIONS:
        lines.append("  " + paint(flag.ljust(18), "accent") + words)
    if keys:
        lines += ["", paint("Keys", "bold")]
        lines += ["  " + paint(key.ljust(18), "accent") + words for key, words in keys]
    lines += ["", "More: " + paint(links.TUI, "accent")]
    return "\n".join(lines)


def _wrap(text, width, indent=0):
    from .style import wrap
    return wrap(text, min(width, 100), indent=indent)
