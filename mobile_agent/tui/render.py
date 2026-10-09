"""Rich renderables for the narrated items (``mobile_agent.narrate``): one look for every screen.

Color is kept to a few roles, named after the Mac app's tokens: text, muted,
faint, one accent (violet), and green, amber and red only for outcomes.
"""

import io
import json

from rich.console import Group
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from ..narrate import Approval, Launch, Note, Outcome, Step, facts, seconds, usd  # noqa: F401

# dashboard/DESIGN.md §2 (dark): the Mac app's tokens, so the terminal and Mobster for Mac read as one product.
CANVAS = "#111014"      # --bg-canvas
CHROME = "#0D0C10"      # --bg-app
RAISED = "#1A191F"      # --bg-raised: the composer
OVERLAY = "#222128"     # --bg-overlay: the palette, the keys sheet, the approval card
LINE = "#2A2930"        # --line
TEXT = "#F4F2EE"        # --text-primary
MUTED = "#B4B1BA"       # --text-secondary
FAINT = "#8F8C96"       # --text-tertiary
QUATERNARY = "#6B6873"  # --text-quaternary: decorative only, never information
ACCENT = "#A493FF"      # --accent-text
VIOLET = "#7D68FF"      # --violet: the mark's body, never text
GREEN = "#48d597"
AMBER = "#f6bd4f"
RED = "#ff806b"         # coral
TONES = {"success": GREEN, "review": ACCENT, "warning": AMBER, "error": RED, "neutral": MUTED}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PHASES = ("observe", "decide", "verify", "act", "check")


def _row(left, right=None):
    grid = Table.grid(expand=True, padding=0)
    grid.add_column(ratio=1, no_wrap=False, overflow="fold")
    grid.add_column(justify="right", no_wrap=True)
    grid.add_row(left, right or Text(""))
    return grid


def step_glyph(step, frame=0):
    state = step.state
    if state in {"thinking", "acting"}:
        return Text(SPINNER[frame % len(SPINNER)], style=ACCENT)
    if state == "failed":
        return Text("✗", style=RED)
    if state == "skipped":
        return Text("○", style=FAINT)
    if step.operation == "DONE":
        return Text("✓", style=GREEN if step.completion == "confirmed" else MUTED)
    if state == "stalled":
        return Text("○", style=MUTED)
    if state == "unchanged":
        return Text("●", style=AMBER)
    return Text("●", style=GREEN)


def step_renderable(step, *, expanded=False, frame=0, number=None):
    head = Text()
    head.append_text(step_glyph(step, frame))
    head.append(" ")
    head.append(step.title, style=f"bold {TEXT}" if step.state in {"done", "unchanged"} else
                MUTED if step.state in {"skipped", "stalled"} else TEXT)
    meta = []
    if step.tries > 1:
        meta.append(f"{step.tries} tries")
    if expanded:
        # How sure Mobster was and how long each step took are details: ctrl+o shows them (P2-4).
        if number is not None:
            meta.insert(0, f"step {number}")
        if isinstance(step.confidence, (int, float)) and step.operation not in {None, "DONE", "WAIT", "BLOCKED"}:
            meta.append(f"{step.confidence:.0%}")
        if step.duration_ms is not None:
            meta.append(seconds(step.duration_ms))
    rows = [_row(head, Text(" · ".join(meta), style=FAINT))]
    phases = step.phases()
    if expanded:
        for index, (phase, text) in enumerate(phases):
            line = Text("  ⎿ " if index == 0 else "    ", style=FAINT)
            line.append(f"{phase:<8}", style=MUTED)
            line.append(text, style=MUTED if phase != "act" else _act_style(step))
            rows.append(line)
    else:
        summary = _summary(step)
        if summary:
            line = Text("  ⎿ ", style=FAINT)
            line.append_text(summary)
            rows.append(line)
    return Group(*rows)


def _act_style(step):
    return {"failed": RED, "unchanged": AMBER, "done": MUTED}.get(step.state, MUTED)


def _summary(step):
    """One line under the step: where it looked, what the phone did."""
    text = Text()
    looked = step.app or ""
    if step.elements is not None:
        looked = f"{looked} · {step.elements} elements" if looked else f"{step.elements} elements"
    if looked:
        text.append(looked, style=FAINT)
    outcome = None
    if step.state == "skipped":
        outcome = (step.note, FAINT)
    elif step.act == "started":
        outcome = ("sending…", ACCENT)
    elif step.act == "not_dispatched":
        outcome = ("not sent: the screen changed first", RED)
    elif step.act == "acknowledged":
        outcome = {True: ("screen changed", MUTED), False: ("no visible change", AMBER),
                   None: ("sent", MUTED)}[step.changed]
    elif step.operation == "DONE" and step.completion:
        outcome = ("goal met" if step.completion == "confirmed" else "not finished yet", MUTED)
    elif step.check and step.act is None and step.operation not in {"DONE", "WAIT", "BLOCKED"}:
        outcome = (f"check: {step.check}", MUTED)
    if outcome:
        if text:
            text.append(" → ", style=FAINT)
        text.append(*outcome)
    return text


def launch_renderable(item):
    text = Text()
    text.append("● " if item.done else "… ", style=GREEN if item.done else ACCENT)
    text.append(f"Opened {item.app}" if item.done else f"Opening {item.app}", style=f"bold {TEXT}" if item.done
                else TEXT)
    return text


def note_renderable(item):
    color = {"warning": AMBER, "error": RED}.get(item.tone, MUTED)
    text = Text("  ! " if item.tone != "neutral" else "  · ", style=color)
    text.append(item.text, style=color if item.tone != "neutral" else MUTED)
    return text


def approval_renderable(item):
    decision = item.decision
    if decision is None:
        text = Text("  ◆ ", style=ACCENT)
        text.append(f"Waiting for you: {item.title}", style=ACCENT)
        return text
    glyph, words, color = {
        "approved": ("✓", "You approved", GREEN), "denied": ("✗", "You declined", MUTED),
        "timeout": ("⏱", "Nobody answered", AMBER), "stopped": ("■", "Stopped while asking", MUTED),
    }.get(decision, ("✓", f"You chose {decision.split(':', 1)[-1]}", GREEN))
    text = Text(f"  {glyph} ", style=color)
    text.append(f"{words}: {item.title}", style=MUTED)
    return text


def outcome_renderable(item, narrator=None):
    """MESSAGING §11's result: the head (Done, Here's what Mobster found, Take a look, Couldn't finish, Stopped
    safely, You declined, Stopped) with the facts, the answer as the hero, then the proof or why it ended."""
    color = TONES.get(item.head_tone, MUTED)
    glyph = {"success": "✓", "error": "✗", "warning": "!", "review": "◆"}.get(item.head_tone, "■")
    head = Text()
    head.append(f"{glyph} ", style=f"bold {color}")
    head.append(item.head if item.status != "preview" else "Preview", style=f"bold {color}")
    summary = item.summary
    line = ""
    if narrator is not None:
        line = facts(narrator.actions, narrator.elapsed_ms(), narrator.cost_nanodollars,
                     bool(narrator.priced_calls or narrator.cost_nanodollars))
    elif summary.get("decisions") is not None:
        line = facts(summary.get("actions", 0), summary.get("elapsed_ms"))
    rows = [_row(head, Text(line, style=FAINT))]
    hero = item.hero
    if hero:
        rows.append(Padding(Text(hero, style=f"bold {TEXT}"), (0, 0, 0, 2)))
    detail = item.proof or item.detail
    if item.status == "preview":
        detail = "Nothing was sent to your iPhone."
    if detail:
        rows.append(Padding(Text(detail, style=FAINT if item.proof else MUTED), (0, 0, 0, 2)))
    data = summary.get("data")
    if data is not None and not (isinstance(data, str) and data.strip() == hero):
        rows.append(Text("  Answer", style=f"bold {TEXT}"))
        rows.append(Text("  " + _format_data(data).replace("\n", "\n  "), style=TEXT))
    return Group(*rows)


def _format_data(data):
    if isinstance(data, str):
        return data
    return json.dumps(data, indent=2, ensure_ascii=False)


def item_renderable(item, *, expanded=False, frame=0, narrator=None, number=None):
    if isinstance(item, Step):
        return step_renderable(item, expanded=expanded, frame=frame, number=number)
    if isinstance(item, Launch):
        return launch_renderable(item)
    if isinstance(item, Note):
        return note_renderable(item)
    if isinstance(item, Approval):
        return approval_renderable(item)
    if isinstance(item, Outcome):
        return outcome_renderable(item, narrator)
    return Text(str(item))


def halfblocks(png, columns, rows, background=(17, 16, 20), center=False):
    """``png`` drawn with half-block characters: two pixels per cell, fit inside columns x rows.

    Every truecolor terminal shows it (and Textual's screenshots keep it), where
    kitty or iTerm2 image escapes would be swallowed by the UI's own compositor.
    """
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:
        return None
    width, height = image.size
    scale = min(columns / width, rows * 2 / height)
    size = (max(1, round(width * scale)), max(2, round(height * scale)))
    size = (size[0], size[1] + size[1] % 2)
    image = image.resize(size, Image.LANCZOS)
    pixels = image.load()
    pad = " " * ((columns - size[0]) // 2) if center else ""
    text = Text()
    for y in range(0, size[1], 2):
        text.append(pad)
        for x in range(size[0]):
            top, bottom = pixels[x, y], pixels[x, y + 1] if y + 1 < size[1] else background
            text.append("▀", style=f"#{top[0]:02x}{top[1]:02x}{top[2]:02x} on #{bottom[0]:02x}{bottom[1]:02x}{bottom[2]:02x}")
        if y + 2 < size[1]:
            text.append("\n")
    return text


# -- the screen as text: what Mobster read, laid out like the phone -------------------------------

ROW_ROLES = {"Cell", "Button", "Link", "Switch", "Toggle", "TextField", "SecureTextField", "SearchField",
             "TextView", "StaticText", "Slider", "Stepper", "Tab", "SegmentedControl", "Picker", "MenuItem",
             "Icon", "Image"}
FIELD_ROLES = {"TextField", "SecureTextField", "SearchField", "TextView"}
SWITCH_ROLES = {"Switch", "Toggle"}
CONTAINER_ROLES = {"Cell", "Button", "Link"}
HIGHLIGHT = "on #2a2347"  # violet 16% on the canvas: the step's target


def _rect(element):
    rect = element.get("rect") or (0, 0, 0, 0)
    try:
        x, y, w, h = (float(v) for v in rect[:4])
    except (TypeError, ValueError):
        x = y = w = h = 0.0
    return x, y, w, h


def _inside(inner, outer, slack=.004):
    ix, iy, iw, ih = _rect(inner)
    ox, oy, ow, oh = _rect(outer)
    return (ix >= ox - slack and iy >= oy - slack and ix + iw <= ox + ow + slack and iy + ih <= oy + oh + slack
            and (iw * ih) < (ow * oh))


def _label(element):
    return " ".join(str(element.get("label") or element.get("value") or "").split())


def _fit(text, width):
    return text if len(text) <= width else text[:max(1, width - 1)] + "…"


def screen_rows(screen):
    """[(elements on one visual row)] from an observation, top to bottom, without the navigation bar.

    An element inside a cell or button is left to its container, so a Settings row reads once.
    """
    elements = [e for e in (screen or {}).get("elements") or () if isinstance(e, dict)]
    nav = next((e for e in elements if e.get("role") == "NavigationBar"), None)
    containers = [e for e in elements if e.get("role") in CONTAINER_ROLES and _label(e)]
    chosen = []
    for element in elements:
        role = element.get("role")
        if element is nav or role not in ROW_ROLES or not _label(element):
            continue
        if nav is not None and _inside(element, nav):
            continue
        _, _, w, h = _rect(element)
        if w * h > .6:  # a screen-sized wrapper, not a row
            continue
        if any(other is not element and _inside(element, other) for other in containers):
            continue
        chosen.append(element)
    chosen.sort(key=lambda e: (_rect(e)[1] + _rect(e)[3] / 2, _rect(e)[0]))
    rows = []
    for element in chosen:
        _, y, _, h = _rect(element)
        centre = y + h / 2
        if rows and element.get("role") != "Cell" and rows[-1][0][0].get("role") != "Cell" \
                and abs(centre - rows[-1][1]) < .012:
            rows[-1][0].append(element)
        else:
            rows.append(([element], centre))
    return [row[0] for row in rows], nav, elements


def _row_parts(row):
    """[(element, text)] on the left of a row, and the text on its right (a value or a switch state)."""
    left, right = [], ""
    for element in row:
        role, label = element.get("role"), _label(element)
        if role == "Cell" and ", " in label:
            name, _, value = label.partition(", ")
            left.append((element, name))
            right = value
        elif role in SWITCH_ROLES:
            left.append((element, label))
            right = "on" if str(element.get("value")) in {"1", "true", "on"} else "off"
        elif role in FIELD_ROLES:
            value = " ".join(str(element.get("value") or "").split())
            left.append((element, f"[ {value or label} ]"))
        elif role == "Button" and str(element.get("value")) == "1":
            left.append((element, f"{label} ✓"))  # a selected option (Light / Dark)
        else:
            left.append((element, label))
    return left, right


def _row_text(row, width, target=None, targeted=False):
    """One row as it reads on the phone: labels on the left, a cell's value or a switch on the right.

    In the targeted row the target keeps its whole label and the rest gives way, so the thing
    Mobster is about to act on is always readable.
    """
    left, right = _row_parts(row)
    marker = "❯ " if targeted else "  "
    right_room = len(right) + 2 if right else 0
    room = max(4, width - len(marker) - right_room)
    texts = [text for _, text in left]
    while sum(len(t) for t in texts) + 2 * (len(texts) - 1) > room:
        # Shorten the longest part that is not the target.
        candidates = [i for i, (element, text) in enumerate(left) if element is not target and len(texts[i]) > 4]
        if not candidates:
            texts = [_fit("  ".join(texts), room)]
            left = [(None, texts[0])]
            break
        i = max(candidates, key=lambda i: len(texts[i]))
        texts[i] = _fit(texts[i], len(texts[i]) - 1)
    back = HIGHLIGHT if targeted else ""
    text = Text()
    text.append(marker, style=f"bold {ACCENT} {HIGHLIGHT}" if targeted else FAINT)
    plain = all(e.get("role") == "StaticText" for e in row)
    for index, ((element, _), part) in enumerate(zip(left, texts)):
        if index:
            text.append("  ", style=back)
        if targeted and element is target:
            style = f"bold {ACCENT} {HIGHLIGHT}"
        elif targeted:
            style = f"{TEXT} {HIGHLIGHT}"
        else:
            style = MUTED if plain else TEXT
        text.append(part, style=style)
    used = len(text.plain)
    if right:
        text.append(" " * max(2, width - used - len(right)), style=back)
        text.append(_fit(right, max(3, width - used - 2)),
                    style=f"{ACCENT} {HIGHLIGHT}" if targeted else ACCENT if right == "on" else FAINT)
    elif targeted:
        text.append(" " * max(0, width - used), style=back)
    return text


def screen_outline(screen, width, height, target_id=None):
    """The screen Mobster last read, as text: the app, the title bar, and each row, with the target marked.

    Used where the terminal cannot show images. It is exactly what Mobster sees: the
    accessibility tree, not pixels.
    """
    if not screen or not screen.get("elements"):
        return Text("The screen Mobster reads appears here while a task runs.", style=FAINT)
    rows, nav, elements = screen_rows(screen)
    by_id = {e.get("id"): e for e in elements}
    target = by_id.get(target_id)
    text = Text()
    text.append(_fit(screen.get("app") or screen.get("bundle") or "", width) + "\n", style=f"bold {TEXT}")
    app_title = screen.get("app") or ""
    if nav is not None and not (_label(nav) == app_title and not any(
            e.get("role") == "Button" and _inside(e, nav) for e in elements)):
        back = next((e for e in elements if e.get("role") == "Button" and _inside(e, nav) and _rect(e)[0] < .3), None)
        title = _label(nav)
        line = Text()
        if back is not None:
            arrow = "" if _label(back) in {"Edit", "Cancel", "Close", "Done"} else "‹ "
            line.append(arrow + _fit(_label(back), 12), style=ACCENT)
        if title and (back is None or title != _label(back)):
            used = len(line.plain)
            line.append(" " * max(2, (width - len(title)) // 2 - used) if back is not None else "", style="")
            line.append(_fit(title, width - len(line.plain)), style=f"bold {TEXT}")
        text.append_text(line)
        text.append("\n")
    text.append("─" * width + "\n", style=LINE)
    targeted = None
    if target is not None:
        for index, row in enumerate(rows):
            if any(e is target or _inside(target, e) or _inside(e, target) for e in row):
                targeted = index
                break
    room = max(1, height - len(text.plain.splitlines()) - (1 if screen.get("keyboard") == "visible" else 0))
    start = 0
    if len(rows) > room:
        room -= 1  # the "more" line
        if targeted is not None and targeted >= room:
            start = min(targeted - room // 2, len(rows) - room)
    shown = rows[start:start + room]
    if start:
        text.append(f"  ↑ {start} more\n", style=FAINT)
        shown = shown[1:] if len(shown) > 1 else shown
    for index, row in enumerate(shown, start + (1 if start else 0)):
        text.append_text(_row_text(row, width, target, index == targeted))
        text.append("\n")
    hidden = len(rows) - (start + (1 if start else 0)) - len(shown)
    if hidden > 0:
        text.append(f"  ↓ {hidden} more\n", style=FAINT)
    if screen.get("keyboard") == "visible":
        text.append("  ⌨ keyboard up\n", style=FAINT)
    text.rstrip()
    return text
