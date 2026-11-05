"""Rich renderables for the narrated items (``mobile_agent.narrate``): one look for every screen."""

import json

from rich.console import Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from .. import brand, doctor
from ..narrate import Approval, Launch, Note, Outcome, Step, facts, seconds, status_glyph
from ..style import SPINNER

# The brand's dark tokens (docs/brand/tokens.json, through brand.py), the ones Mobster for Mac and mobster.dev draw
# with, so the terminal and the app read as one product. A terminal draws no see-through fill, so the washes are
# mixed over what they sit on.
CANVAS = brand.DARK["paper"]           # the page
CHROME = brand.DARK["sunken"]          # the header band: the app's chrome is the sunken tone
RAISED = brand.DARK["surface"]         # the composer
OVERLAY = brand.DARK["surface"]        # the palette, the keys sheet, the approval card: the app's overlays
LINE = brand.DARK["line"]
LINE_STRONG = brand.DARK["line-strong"]  # a dialog's or the approval card's edge (never a violet ring)
TEXT = brand.DARK["ink"]
MUTED = brand.DARK["ink-2"]
FAINT = brand.DARK["ink-3"]
QUATERNARY = brand.DARK["ink-4"]       # decorative only, never information
ACCENT = brand.DARK["violet-text"]
GREEN = brand.DARK["green"]
AMBER = brand.DARK["amber"]
RED = brand.DARK["coral"]
INPUT = brand.mix(TEXT, .05, OVERLAY)     # a field inside an overlay: the app's input wash
SELECTED = brand.mix(TEXT, .09, OVERLAY)  # the highlighted option: the app's selected wash
SCRIM = brand.RAW["dark"]["scrim"]        # behind a modal, see-through
TONES = {"success": GREEN, "review": ACCENT, "warning": AMBER, "error": RED, "neutral": MUTED}
APPROVALS = {"approved": ("✓", "You approved", GREEN), "denied": ("✗", "You declined", MUTED),
             "timeout": ("⏱", "Nobody answered", AMBER), "stopped": ("■", "Stopped while asking", MUTED)}


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
        if number is not None:
            meta.insert(0, f"step {number}")
        if isinstance(step.confidence, (int, float)) and step.operation not in {None, "DONE", "WAIT", "BLOCKED"}:
            meta.append(f"{step.confidence:.0%}")
        if step.duration_ms is not None:
            meta.append(seconds(step.duration_ms))
    rows = [_row(head, Text(", ".join(meta), style=FAINT))]
    if expanded:
        act_style = {"failed": RED, "unchanged": AMBER}.get(step.state, MUTED)
        for index, (phase, text) in enumerate(step.phases()):
            line = Text("  ⎿ " if index == 0 else "    ", style=FAINT)
            line.append(f"{phase:<8}", style=MUTED)
            line.append(text, style=act_style if phase == "act" else MUTED)
            rows.append(line)
    else:
        summary = _summary(step)
        if summary:
            line = Text("  ⎿ ", style=FAINT)
            line.append_text(summary)
            rows.append(line)
    return Group(*rows)


def _summary(step):
    """One line under the step: where it looked, what the phone did."""
    text = Text()
    looked = step.app or ""
    if step.elements is not None:
        looked = f"{looked}, {step.elements} elements" if looked else f"{step.elements} elements"
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
    text = Text("  ! " if item.tone != "neutral" else "  – ", style=color)
    text.append(item.text, style=color if item.tone != "neutral" else MUTED)
    return text


def approval_renderable(item):
    decision = item.decision
    if decision is None:
        text = Text("  ◆ ", style=ACCENT)
        text.append(f"Waiting for you: {item.title}", style=ACCENT)
        return text
    glyph, words, color = APPROVALS.get(decision, ("✓", f"You chose {decision.split(':', 1)[-1]}", GREEN))
    text = Text(f"  {glyph} ", style=color)
    text.append(f"{words}: {item.title}", style=MUTED)
    return text


def outcome_renderable(item, narrator=None):
    """The result: the head, the facts, the answer, then the proof or why it ended."""
    color = TONES.get(item.head_tone, MUTED)
    glyph = status_glyph(item.head_tone)
    head = Text()
    head.append(f"{glyph} ", style=f"bold {color}")
    head.append(item.head if item.status != "preview" else "Preview", style=f"bold {color}")
    summary = item.summary
    line = ""
    if narrator is not None:
        line = facts(narrator.actions, narrator.elapsed_ms(), narrator.cost_nanodollars,
                     bool(narrator.priced_calls or narrator.cost_nanodollars), plan=bool(narrator.plan_calls))
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
        if isinstance(data, str):
            # The answer is Markdown (frontier.SYSTEM's DONE line): Rich draws its lists, bold, tables and code, and
            # shows any HTML in it as text.
            rows.append(Padding(Markdown(data, hyperlinks=False), (0, 0, 0, 2), style=TEXT))
        else:
            rows.append(Text("  " + json.dumps(data, indent=2, ensure_ascii=False).replace("\n", "\n  "), style=TEXT))
    return Group(*rows)


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


ROW_ROLES = {"Cell", "Button", "Link", "Switch", "Toggle", "TextField", "SecureTextField", "SearchField",
             "TextView", "StaticText", "Slider", "Stepper", "Tab", "SegmentedControl", "Picker", "MenuItem",
             "Icon", "Image"}
FIELD_ROLES = {"TextField", "SecureTextField", "SearchField", "TextView"}
SWITCH_ROLES = {"Switch", "Toggle"}
CONTAINER_ROLES = {"Cell", "Button", "Link"}
HIGHLIGHT = f"on {brand.DARK['violet-wash']}"  # violet 16% on the canvas: the step's target


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
    """[(elements on one visual row)] from an observation, top to bottom, without the navigation bar."""
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
    """One row as it reads on the phone: labels on the left, a cell's value or a switch on the right."""
    left, right = _row_parts(row)
    marker = "❯ " if targeted else "  "
    right_room = len(right) + 2 if right else 0
    room = max(4, width - len(marker) - right_room)
    texts = [text for _, text in left]
    while sum(len(t) for t in texts) + 2 * (len(texts) - 1) > room:
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
    """The screen Mobster last read, as text: the app, the title bar, and each row, with the target marked."""
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


def doctor_report(checks):
    """The doctor screen's body: one line per check, a fix under each warning or failure, then the verdict."""
    colors = {doctor.OK: GREEN, doctor.WARN: AMBER, doctor.FAIL: RED, doctor.SKIP: FAINT}
    text = Text()
    for check in checks:
        text.append(f"{doctor.GLYPHS[check.state]} ", style=colors[check.state])
        text.append(f"{check.label:<18}", style=TEXT)
        text.append(f"{check.detail}\n", style=MUTED)
        if check.fix and check.state in {doctor.WARN, doctor.FAIL}:
            for route in check.fix.split("\n"):
                text.append(f"  {'':<18}→ {route}\n", style=FAINT)
    failed = doctor.problems(checks)
    text.append("\n")
    text.append("Ready for tasks." if not failed else
                f"{len(failed)} thing{'s' if len(failed) != 1 else ''} to fix before your first task.",
                style=RED if failed else GREEN)
    return text
