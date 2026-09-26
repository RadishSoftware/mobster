"""Where Mobster writes. A source checkout keeps its files beside the package; an installed or frozen copy
uses the user's data folder, never site-packages or the signed app bundle."""

from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parent


def user_data_dir():
    """Shared with the desktop app, so one iPhone setup serves both."""
    return Path.home() / "Library" / "Application Support" / "app.mobster.desktop"


def source_checkout():
    return not getattr(sys, "frozen", False) and (PACKAGE.parent / "pyproject.toml").is_file()


def build_dir():
    """Compiled helpers (`mobster build-ocr`, `vision_judge build-t0`)."""
    return PACKAGE / ".build" if source_checkout() else user_data_dir() / "build"
