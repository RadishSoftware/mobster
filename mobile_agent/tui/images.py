"""Whether this terminal can show the phone's real screenshot inside the UI.

The kitty graphics protocol (kitty, Ghostty) and sixel (iTerm2, WezTerm, foot, Konsole and
others) draw sharp images; textual-image renders both inside Textual's layout. Asking the
terminal means writing a query and reading its reply before the UI owns the keyboard, so it
is only asked where the environment suggests a graphics-capable terminal, and only when the
UI opens. Elsewhere the pane shows the screen as text (render.screen_outline).

MOBSTER_PHONE_VIEW=text never asks; MOBSTER_PHONE_VIEW=image asks in any terminal.
"""

import os


def likely(env=None):
    """True when the environment names a terminal that shows images."""
    env = os.environ if env is None else env
    from ..terminal_image import protocol
    term = env.get("TERM", "")
    return (protocol(env) != "blocks" or term.startswith("foot") or "KONSOLE_VERSION" in env
            or env.get("TERM_PROGRAM", "").lower() in {"rio", "contour"})


def image_widget(env=None):
    """textual-image's widget class for this terminal (kitty protocol or sixel), or None for the text view."""
    env = os.environ if env is None else env
    choice = env.get("MOBSTER_PHONE_VIEW", "auto").lower()
    if choice == "text" or (choice != "image" and not likely(env)):
        return None
    try:
        import textual_image.renderable as renderable  # asks the terminal once, on import
        from textual_image.widget import SixelImage, TGPImage
    except Exception:
        return None
    if renderable.Image is renderable.TGPImage:
        return TGPImage
    if renderable.Image is renderable.SixelImage:
        return SixelImage
    return None
