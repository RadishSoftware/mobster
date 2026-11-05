"""The terminal UI's look: the Textual theme, the app's stylesheet and the welcome screen's mark."""

from rich.text import Text
from textual.theme import Theme

from .. import brand
from .render import (
    ACCENT,
    AMBER,
    CANVAS,
    CHROME,
    FAINT,
    GREEN,
    INPUT,
    LINE,
    LINE_STRONG,
    MUTED,
    OVERLAY,
    QUATERNARY,
    RAISED,
    RED,
    SCRIM,
    SELECTED,
    TEXT,
)

THEME = Theme(
    name="mobster", primary=ACCENT, secondary=MUTED, accent=ACCENT, warning=AMBER, error=RED, success=GREEN,
    foreground=TEXT, background=CANVAS, surface=RAISED, panel=OVERLAY, boost=OVERLAY, dark=True,
    variables={"footer-key-foreground": ACCENT, "input-selection-background": f"{ACCENT}40",
               "block-cursor-background": SELECTED, "block-cursor-foreground": TEXT, "input-cursor-background": ACCENT,
               "scrollbar": LINE, "scrollbar-hover": LINE_STRONG, "scrollbar-active": QUATERNARY,
               "scrollbar-background": CANVAS, "scrollbar-corner-color": CANVAS},
)


def mark_text():
    """The welcome mark: brand.PIXEL_MARK cut to its pixels, two pixel rows to a line of half blocks."""
    grid = {}
    for x, y, width, height, colour in brand.PIXEL_MARK["dark"]:
        for row in range(y, y + height):
            for column in range(x, x + width):
                grid[column, row] = colour
    columns, rows = [column for column, _ in grid], [row for _, row in grid]
    text = Text()
    for row in range(min(rows), max(rows) + 1, 2):
        for column in range(min(columns), max(columns) + 1):
            top, bottom = grid.get((column, row)), grid.get((column, row + 1))
            if top and bottom:
                text.append("▀", style=f"{top} on {bottom}")
            elif top or bottom:
                text.append("▀" if top else "▄", style=top or bottom)
            else:
                text.append(" ")
        text.append("\n")
    text.rstrip()
    return text


APP_CSS = f"""
    Screen {{ background: {CANVAS}; color: {TEXT}; layers: base overlay; }}
    #header {{ height: 1; padding: 0 1; background: {CHROME}; }}
    #body {{ height: 1fr; }}
    #transcript {{ height: 1fr; padding: 0 1 0 2; scrollbar-size-vertical: 1; align-vertical: bottom; }}
    #transcript.idle {{ align-vertical: top; }}
    .tall #header {{ margin: 1 0 0 0; }}
    #welcome {{ margin: 1 0 1 0; }}
    .run {{ height: auto; margin: 0 0 1 0; }}
    .task {{ margin: 0 0 1 0; }}
    .outcome {{ margin: 1 0 0 0; }}
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
    #suggest > .option-list--option-highlighted {{ background: {SELECTED}; color: {TEXT}; text-style: none; }}
    #suggest-hint {{ height: 1; margin: 0 2; padding: 0 1; background: {OVERLAY}; display: none; }}
    #suggest-hint.open {{ display: block; }}
    #approval {{ border: round {LINE_STRONG}; padding: 0 1; margin: 0 1; height: auto; max-height: 60vh; overflow-y: auto; display: none;
                background: {OVERLAY}; }}
    #approval.open {{ display: block; }}
    #approval:focus {{ border: round {LINE_STRONG}; }}
    #notice {{ height: auto; margin: 0 2; display: none; }}
    #notice.open {{ display: block; }}
    Composer {{ height: 3; background: {RAISED}; border: tall {RAISED}; margin: 0 1; padding: 0 0; }}
    Composer:focus-within {{ border-left: tall {ACCENT}; }}
    Composer.busy {{ border-left: tall {LINE}; }}
    #chip {{ width: auto; height: 1; padding: 0 1; color: {ACCENT}; text-style: bold; background: {RAISED}; }}
    #prompt {{ border: none; height: 1; padding: 0 1 0 0; background: {RAISED}; width: 1fr; }}
    #prompt:focus {{ border: none; background-tint: {RAISED} 0%; }}
    #prompt > .input--placeholder {{ color: {FAINT}; }}
    #status {{ height: 1; padding: 0 2; color: {MUTED}; }}
    ModalScreen {{ background: {SCRIM}; align: center middle; }}
    #picker {{ width: 76; max-width: 90%; height: auto; max-height: 80%; border: round {LINE}; background: {OVERLAY};
              padding: 0 1; }}
    #picker-title {{ margin: 0 0 1 0; }}
    #picker-filter {{ border: none; background: {INPUT}; height: 1; margin: 0 0 1 0; padding: 0 1; }}
    #picker-list {{ height: auto; max-height: 24; border: none; background: {OVERLAY}; }}
    #picker-list > .option-list--option-highlighted {{ background: {SELECTED}; text-style: none; }}
    #picker-hint {{ margin: 1 0 0 0; }}
    #info {{ width: 84; max-width: 94%; height: auto; max-height: 90%; border: round {LINE}; background: {OVERLAY};
            padding: 0 2; }}
    #info-title {{ margin: 0 0 1 0; }}
    #info-hint {{ margin: 1 0 0 0; }}
    #ask, #login {{ width: 72; max-width: 94%; height: auto; border: round {LINE_STRONG}; background: {OVERLAY};
                   padding: 0 2; }}
    #ask-title, #login-title {{ margin: 0 0 1 0; }}
    #login-key {{ border: none; background: {INPUT}; height: 1; margin: 1 0; padding: 0 1; }}
    #login-hint {{ margin: 1 0 0 0; }}
    """  # noqa: E501
