"""Mobster for developers: the commands that fit Mobster's checks into how an app is built and shipped."""

import os
import sys
from pathlib import Path

APP_AGENT = "/Applications/Mobster.app/Contents/MacOS/mobster-agent"


def self_argv():
    if getattr(sys, "frozen", False):
        return [sys.executable], {}
    root = str(Path(__file__).resolve().parents[2])
    path = os.environ.get("PYTHONPATH", "")
    return [sys.executable, "-m", "mobile_agent"], {"PYTHONPATH": root + (os.pathsep + path if path else "")}


def agent_argv():
    """(argv, extra environment) for Mobster for Mac's agent when it's installed, else this Mobster."""
    if Path(APP_AGENT).exists():
        return [APP_AGENT], {}
    return self_argv()


def say(text, stream=None):
    try:
        print(text, file=stream or sys.stdout, flush=True)
    except BrokenPipeError:
        pass


def subcommands(parser, dest):
    subs = parser.add_subparsers(dest=dest, metavar="COMMAND")
    subs.required = True

    def add(*args, **kwargs):
        kwargs.setdefault("formatter_class", parser.formatter_class)
        return subs.add_parser(*args, **kwargs)
    return add


def main(prefix, handlers, args, **context):
    from ..phone_io import PhoneIOError
    try:
        return handlers[getattr(args, f"{prefix}_command")](args, **context)
    except DevError as error:
        say(f"mobster {prefix}: {error.line()}", sys.stderr)
        return error.code
    except PhoneIOError as error:
        say(f"mobster {prefix}: {error} {error.fix or ''}".rstrip(), sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


class DevError(Exception):
    """A plain sentence saying what happened, and the fix. ``code`` is the exit code (3: couldn't run)."""

    def __init__(self, message, fix="", code=3):
        super().__init__(message)
        self.message, self.fix, self.code = message, fix, code

    def line(self):
        return f"{self.message} {self.fix}".strip()
