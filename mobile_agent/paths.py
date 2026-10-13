"""Where Mobster writes. A source checkout keeps its files beside the package; an installed or frozen copy
uses the user's data folder, never site-packages or the signed app bundle."""

import os
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parent


def user_data_dir():
    """Shared with the desktop app, so one iPhone setup serves both."""
    return Path.home() / "Library" / "Application Support" / "app.mobster.desktop"


def dev_data_dir():
    """The developer tools' folder (`mobster verify`, `mobster sim`, `mobster mcp`): simulators, the
    WebDriverAgent build and logs. $MOBSTER_DATA_DIR when set, else the `dev` folder of the data folder."""
    value = os.environ.get("MOBSTER_DATA_DIR", "").strip()
    return Path(value).expanduser() if value else user_data_dir() / "dev"


def source_checkout():
    return not getattr(sys, "frozen", False) and (PACKAGE.parent / "pyproject.toml").is_file()


def build_dir():
    """Compiled helpers (`mobster build-ocr`, `vision_judge build-t0`)."""
    return PACKAGE / ".build" if source_checkout() else user_data_dir() / "build"
