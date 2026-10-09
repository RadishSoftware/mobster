"""`mobster screen`: the phone's current screen, printed in the terminal.

kitty and Ghostty get the kitty graphics protocol, iTerm2 and WezTerm the
iTerm2 inline-image escape, and every other truecolor terminal a drawing made
of half-block characters (two pixels per cell). The screenshot comes from
WebDriverAgent's sessionless GET /screenshot: nothing is tapped.
"""

import base64
import io
import os
import sys


def protocol(env=None):
    """"kitty", "iterm" or "blocks" for the terminal described by ``env``."""
    env = os.environ if env is None else env
    term, program = env.get("TERM", ""), env.get("TERM_PROGRAM", "")
    if env.get("KITTY_WINDOW_ID") or term == "xterm-kitty" or program.lower() == "ghostty" or term == "xterm-ghostty":
        return "kitty"
    if program in {"iTerm.app", "WezTerm"} or env.get("LC_TERMINAL") == "iTerm2":
        return "iterm"
    return "blocks"


def kitty(png, columns):
    """Kitty graphics protocol: base64 PNG in 4096-byte chunks, scaled to ``columns`` cells wide."""
    data = base64.b64encode(png).decode()
    chunks = [data[i:i + 4096] for i in range(0, len(data), 4096)] or [""]
    out = []
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        head = f"a=T,f=100,c={columns},q=2,m={more}" if index == 0 else f"m={more}"
        out.append(f"\033_G{head};{chunk}\033\\")
    return "".join(out) + "\n"


def iterm(png, columns):
    data = base64.b64encode(png).decode()
    return f"\033]1337;File=inline=1;size={len(png)};width={columns};preserveAspectRatio=1:{data}\a\n"


def blocks(png, columns):
    """Half-block characters with 24-bit color, ``columns`` wide."""
    from PIL import Image
    image = Image.open(io.BytesIO(png)).convert("RGB")
    width, height = image.size
    rows = max(2, round(height * columns / width))
    rows += rows % 2
    image = image.resize((columns, rows), Image.LANCZOS)
    pixels = image.load()
    lines = []
    for y in range(0, rows, 2):
        cells = []
        for x in range(columns):
            top, bottom = pixels[x, y], pixels[x, y + 1]
            cells.append(f"\033[38;2;{top[0]};{top[1]};{top[2]}m\033[48;2;{bottom[0]};{bottom[1]};{bottom[2]}m▀")
        lines.append("".join(cells) + "\033[0m")
    return "\n".join(lines) + "\n"


def show_screen(wda_url, out=None, width=None, stream=None):
    from .tui.session import wda_screenshot
    stream = stream or sys.stdout
    image = wda_screenshot(wda_url, timeout=5)
    if image is None:
        print(f"mobster: no screen from WebDriverAgent at {wda_url}. Is it running? (`mobster doctor`)",
              file=sys.stderr)
        return 3
    png = base64.b64decode(image.split(",", 1)[1])
    if out is not None:
        out.write_bytes(png)
        print(str(out), file=stream)
        return 0
    if not stream.isatty():
        print("mobster: stdout is not a terminal; pass --out FILE to save the PNG.", file=sys.stderr)
        return 2
    try:
        columns = os.get_terminal_size().columns
    except OSError:
        columns = 80
    kind = protocol()
    cells = width or (min(40, columns - 2) if kind != "blocks" else min(36, columns - 2))
    render = {"kitty": kitty, "iterm": iterm, "blocks": blocks}[kind]
    stream.write(render(png, cells))
    stream.flush()
    return 0
