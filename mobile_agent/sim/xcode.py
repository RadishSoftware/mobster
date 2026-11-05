"""The Xcode Mobster runs simulators with: its version, its build and the environment to run its tools in."""

import plistlib
import re
from pathlib import Path
from typing import NamedTuple

from .. import mac_tools
from .models import SimError


class Tools(NamedTuple):
    """Xcode's tools for one run: its version and build, and the environment to run them in."""
    xcodebuild: str
    git: str
    version: str      # "26.4"
    build: str        # "17E192"
    env: dict


def find_tools(run):
    xcode, selected = mac_tools.developer_dir()
    if not xcode:
        raise SimError("environment", "Xcode isn't installed. Mobster runs simulators with it.",
                       "Install Xcode from the App Store, open it once, then run `mobster sim doctor`.")
    # The Command Line Tools are selected: use the Xcode found anyway. Never the shell's settings for building for
    # this Mac (mac_tools.HOST_BUILD_ENV): they break the simulator's helper build.
    env = mac_tools.xcode_env(xcode if selected != xcode else None)
    version, build = app_version(xcode)
    xcodebuild = str(Path(xcode) / "usr" / "bin" / "xcodebuild")
    if not build:
        result = run([xcodebuild, "-version"], timeout=60, env=env)
        version, build = parse_version(result.out)
        if not result.ok or not build:
            text = result.out + result.err
            if "license" in text.lower():
                raise SimError("environment", "Xcode's license isn't accepted yet.",
                               "Run `sudo xcodebuild -license accept` in Terminal.")
            raise SimError("environment", f"xcodebuild didn't report its version: {result.last_line()}",
                           "Open Xcode once to finish its setup, then run `mobster sim doctor`.")
    git = mac_tools.xcode_tool("git", developer=xcode) or ""
    return Tools(xcodebuild=xcodebuild, git=git, version=version, build=build, env=env)


def app_version(developer_dir):
    """("26.4", "17E192") from the Xcode app's version.plist, else ("", "")."""
    try:
        with open(Path(developer_dir).parent / "version.plist", "rb") as stream:
            info = plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return "", ""
    return str(info.get("CFBundleShortVersionString") or ""), str(info.get("ProductBuildVersion") or "")


def parse_version(text):
    """("26.4", "17E192") from `xcodebuild -version`."""
    version = re.search(r"Xcode\s+(\S+)", text or "")
    build = re.search(r"Build version\s+(\S+)", text or "")
    return (version.group(1) if version else ""), (build.group(1) if build else "")


def installed_builds():
    """The build versions of the Xcode apps in /Applications and ~/Applications."""
    builds = set()
    for root in mac_tools.XCODE_APP_DIRS:
        try:
            candidates = list(Path(root).glob("Xcode*.app"))
        except OSError:
            continue
        for app in candidates:
            build = app_version(app / "Contents" / "Developer")[1]
            if build:
                builds.add(build)
    return builds
