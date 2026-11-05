"""Colour and layout for plain terminal output: help, doctor, tables, `run`'s steps."""

import os
import sys
import time

from . import brand


def _roles(palette):
    """The roles terminal text takes: violet text, the second and third ink, and the three status tones."""
    return {"accent": palette["violet-text"], "secondary": palette["ink-2"], "tertiary": palette["ink-3"],
            "green": palette["green"], "amber": palette["amber"], "coral": palette["coral"]}


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille frames for a step in progress; the terminal UI, `run` and `setup` share them

DARK, LIGHT = _roles(brand.DARK), _roles(brand.LIGHT)
# Older role names, kept so every caller reads the same.
ALIASES = {"muted": "secondary", "faint": "tertiary", "red": "coral", "dim": "tertiary"}


def xterm256(value):
    """The xterm-256 colour nearest ``value`` (#rrggbb): from the 6x6x6 cube or the grey ramp."""
    r, g, b = brand.rgb(value)
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
    """The theme, light or dark: $MOBSTER_THEME, else $COLORFGBG, else the terminal's answer."""
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
    """The terminal's background as (r, g, b) in 0..1, from OSC 11, or None."""
    # tmux and screen answer for themselves, not for the terminal behind them.
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
            # Device attributes (ESC [c) follow: every terminal answers them, so one that ignores OSC 11 ends the
            # wait at once and leaves no late reply in the shell's input.
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
    """``paint(text, *roles)``: roles accent, secondary, tertiary, green, amber, coral, bold, underline."""

    def __init__(self, enabled, light=None):
        self.codes = {}
        if not enabled:
            return
        values = LIGHT if (theme() == "light" if light is None else light) else DARK
        full = truecolor()
        for role, value in values.items():
            r, g, b = brand.rgb(value)
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
    """``text`` wrapped at ``width``, with a hanging ``indent``."""
    import re
    import textwrap
    first = indent if first is None else first
    # textwrap breaks only at ASCII whitespace, so a no-break space holds a command together.
    held = re.sub(r"\bmobster(?: -{0,2}[a-z][\w-]*){1,2}|--[a-z][\w-]* [A-Z]+\b",
                  lambda match: match[0].replace(" ", "\u00a0"), text)
    return textwrap.fill(held, width=max(24, width), initial_indent=" " * first, subsequent_indent=" " * indent,
                         break_long_words=False, break_on_hyphens=False).replace("\u00a0", " ")
