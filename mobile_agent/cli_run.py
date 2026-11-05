"""What a command's run needs besides parsing; run-time modules import it, never `__main__`,
which `python -m` loads twice."""

import os
import sys

from .cli_constants import DEFAULT_WDA_URL

NO_KEY = "no model key yet. Run `mobster login`, then run this again."


def output(value):
    import json
    try:
        print(json.dumps(value, allow_nan=False), flush=True)
    except BrokenPipeError:
        # The reader went away (`demo | head -1`): stop quietly, not with a traceback on exit.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)


class stop_at_safe_point:
    """While a task runs: the first ctrl+c, SIGTERM or SIGHUP sets ``stop``; a second raises KeyboardInterrupt."""

    def __init__(self, stop, on_stop=None):
        self.stop, self.on_stop, self.caught, self.previous = stop, on_stop, [], {}

    def handler(self, signum, frame):
        self.caught.append(signum)
        if self.stop.is_set():
            raise KeyboardInterrupt
        self.stop.set()
        if self.on_stop is not None:
            self.on_stop()
        try:
            print("mobster: stopping at the next safe point (ctrl+c again to force)", file=sys.stderr, flush=True)
        except (OSError, ValueError, RuntimeError):
            # A signal can interrupt stderr's buffered write; the diagnostic must not turn Stop into an error.
            pass

    def __enter__(self):
        import signal
        import threading
        if threading.current_thread() is threading.main_thread():
            for name in ("SIGINT", "SIGTERM", "SIGHUP"):
                number = getattr(signal, name, None)
                if number is not None:
                    self.previous[number] = signal.signal(number, self.handler)
        return self.caught

    def __exit__(self, *exc):
        import signal
        for number, old in self.previous.items():
            signal.signal(number, old)
        return False


def event_printer(args, preview=False):
    """``output`` (JSON lines) for scripts and --json; the step renderer on a terminal."""
    if getattr(args, "json", False) or not sys.stdout.isatty():
        return output
    from .console import ConsoleRenderer
    return ConsoleRenderer(preview=preview)


def wda_url_for(args):
    return (getattr(args, "wda_url", None) or os.environ.get("MOBSTER_WDA_URL") or DEFAULT_WDA_URL).rstrip("/")


def device_target(name):
    """The device `--device NAME` names (devices.discover), with a WDA address to drive."""
    from . import devices
    try:
        item = devices.resolve(devices.discover(probe=False), name)
    except LookupError as error:
        raise NoDevice(str(error)) from None
    if not item.get("wdaUrl"):
        raise NoDevice(f"“{item['name']}” isn't set up yet. {item.get('reason') or ''} Set it up with "
                       "`mobster setup`, or in Mobster for Mac.".replace("  ", " "))
    return item


class NoDevice(ConnectionError):
    """Nothing answered at the WDA address (exit code 3)."""


class DeviceBusy(RuntimeError):
    """Another Mobster process holds the phone's device lease."""


class MissingKey(ValueError):
    """The model key a command needs is not set (exit code 3)."""


def no_device_reason(exc):
    """A few words on how reaching WDA failed, from the transport's message (which never holds a body)."""
    text = f"{type(exc).__name__} {exc}".lower()
    for needle, words in (("refused", "connection refused"), ("reset", "connection reset"),
                          ("disconnected", "connection closed"), ("gaierror", "unknown host"),
                          ("timeout", "timed out"), ("deadline", "timed out"), ("http 4", "not WebDriverAgent"),
                          ("json", "not WebDriverAgent")):
        if needle in text:
            return words
    return "no usable answer"


def error_result(exc, exit_code):
    """`run`'s (and `demo`'s) JSON line for an error before or around the run."""
    return {"event": "result", "status": "error", "ok": False, "error_type": type(exc).__name__,
            "error": str(exc), "reason": str(exc), "exit_code": exit_code}


def error_line(exc):
    """A human line for a terminal: what happened, then what to do."""
    return f"mobster: {exc}"
