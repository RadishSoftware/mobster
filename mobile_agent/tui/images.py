"""Whether this terminal can show the phone's real screenshot inside the UI."""

import os


def likely(env):
    """True when the environment names a terminal that shows images."""
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
