"""Colour and layout for plain terminal output: help, doctor, tables, `run`'s steps.

The colours are the Mac app's (dashboard/DESIGN.md §2), so the terminal and Mobster for Mac read as one product:
dark values on a dark terminal, the light ones on a light terminal, so information text keeps 4.5:1 or better on
either. Primary text is the terminal's own foreground, right on every background. Truecolor when the terminal
says it has it ($COLORTERM), else the nearest of the 256 colours. NO_COLOR, TERM=dumb, MOBSTER_PLAIN=1 and
anything that isn't a terminal get plain text.

This module imports nothing heavy: `mobster --help` uses it.
"""

import os
import sys
import time

# DESIGN.md §2: --accent-text, --text-secondary, --text-tertiary and the status tones (dark), and the light values
# beside them (violet text, ink secondary and tertiary, green, amber, coral).
DARK = {"accent": "#A493FF", "secondary": "#B4B1BA", "tertiary": "#8F8C96", "green": "#48d597",
        "amber": "#f6bd4f", "coral": "#ff806b"}
LIGHT = {"accent": "#5B41E8", "secondary": "#5C5A63", "tertiary": "#6F6C76", "green": "#137A4C",
         "amber": "#9A5D00", "coral": "#C93B26"}
# Older role names, kept so every caller reads the same.
ALIASES = {"muted": "secondary", "faint": "tertiary", "red": "coral", "dim": "tertiary"}


def _rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def xterm256(value):
    """The xterm-256 colour nearest ``value`` (#rrggbb): from the 6x6x6 cube or the grey ramp."""
    r, g, b = _rgb(value)
    steps = (0, 95, 135, 175, 215, 255)

    def nearest(channel):
        return min(range(6), key=lambda i: abs(steps[i] - channel))
    cube = (nearest(r), nearest(g), nearest(b))
    cube_rgb = tuple(steps[i] for i in cube)
    grey = max(0, min(23, round(((r + g + b) / 3 - 8) / 10)))
    grey_rgb = (8 + grey * 10,) * 3

    def distance(other):
        return sum((a - b) ** 2 for a, b in zip((r, g, b), other))
    if distance(grey_rgb) < distance(cube_rgb):
        return 232 + grey
    return 16 + 36 * cube[0] + 6 * cube[1] + cube[2]


def truecolor():
    return os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}


_theme = None


def theme():
    """"light" or "dark": $MOBSTER_THEME, else $COLORFGBG (iTerm2, rxvt), else the terminal's own answer to the
    background-colour query (most terminals answer in a few milliseconds), else dark."""
    global _theme
    chosen = os.environ.get("MOBSTER_THEME", "").strip().lower()
    if chosen in {"light", "dark"}:
        return chosen
    if _theme is None:
        _theme = _detect_theme()
    return _theme


def _detect_theme():
    colorfgbg = os.environ.get("COLORFGBG", "")
    if colorfgbg:
        background = colorfgbg.split(";")[-1]
        if background.isdigit():
            return "light" if int(background) in {7, 9, 10, 11, 12, 13, 14, 15} else "dark"
    rgb = _query_background()
    if rgb is None:
        return "dark"
    luminance = sum(weight * channel for weight, channel in zip((.2126, .7152, .0722), rgb))
    return "light" if luminance > .5 else "dark"


def _query_background(timeout=.2):
    """The terminal's background as (r, g, b) in 0..1, from OSC 11, or None.

    A device-attributes query follows it: every terminal answers that one, so a terminal that ignores OSC 11 is
    known at once, and no late answer is left in the shell's input. Only on an interactive terminal, never under
    tmux or screen (they answer for themselves) or CI."""
    if os.environ.get("TMUX") or os.environ.get("STY") or os.environ.get("TERM", "").startswith("screen") \
            or os.environ.get("CI"):
        return None
    try:
        import select
        import termios
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except (OSError, ImportError, ValueError, AttributeError):
        return None
    answer = b""
    try:
        old = termios.tcgetattr(fd)
        raw = termios.tcgetattr(fd)
        raw[3] &= ~(termios.ECHO | termios.ICANON)
        raw[6][termios.VMIN], raw[6][termios.VTIME] = 0, 0
        termios.tcsetattr(fd, termios.TCSANOW, raw)
        try:
            os.write(fd, b"\033]11;?\033\\\033[c")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
                if not ready:
                    break
                chunk = os.read(fd, 256)
                if not chunk:
                    break
                answer += chunk
                if b"\033[?" in answer and answer.endswith(b"c"):
                    break  # the device attributes came: any OSC 11 answer came before them
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
    except Exception:  # noqa: BLE001 -- a terminal that can't be asked is a dark one
        return None
    finally:
        os.close(fd)
    import re
    found = re.search(rb"\]11;rgba?:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", answer)
    if not found:
        return None
    return tuple(int(part, 16) / (16 ** len(part) - 1) for part in found.groups())


class Palette:
    """``paint(text, *roles)``: roles accent, secondary, tertiary, green, amber, coral, bold, underline (and the
    older muted, faint, red, text). Plain text when disabled."""

    def __init__(self, enabled, light=None):
        self.codes = {}
        if not enabled:
            return
        values = LIGHT if (theme() == "light" if light is None else light) else DARK
        full = truecolor()
        for role, value in values.items():
            r, g, b = _rgb(value)
            self.codes[role] = f"38;2;{r};{g};{b}" if full else f"38;5;{xterm256(value)}"
        self.codes.update(bold="1", text="39", underline="4")
        for old, new in ALIASES.items():
            self.codes[old] = self.codes[new]

    @property
    def enabled(self):
        return bool(self.codes)

    def __call__(self, text, *roles):
        if not self.codes or not text:
            return text
        return "".join(f"\033[{self.codes[role]}m" for role in roles) + text + "\033[0m"


def color_enabled(stream):
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        return False
    return bool(tty) and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb" \
        and os.environ.get("MOBSTER_PLAIN", "") not in {"1", "true", "yes"}


def palette(stream=None):
    """The palette for ``stream`` (stdout by default): colour on an interactive terminal that allows it."""
    return Palette(color_enabled(sys.stdout if stream is None else stream))


def columns(default=100):
    try:
        return int(os.environ.get("COLUMNS") or os.get_terminal_size().columns)
    except (OSError, ValueError):
        return default


def wrap(text, width, indent=0, first=None):
    """``text`` wrapped at ``width``, with a hanging ``indent``; ``first`` is the first line's indent (default
    ``indent``). Never breaks inside a word, so a URL or a command stays whole."""
    import textwrap
    first = indent if first is None else first
    return textwrap.fill(text, width=max(24, width), initial_indent=" " * first, subsequent_indent=" " * indent,
                         break_long_words=False, break_on_hyphens=False)


def visible_len(text):
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", text))
