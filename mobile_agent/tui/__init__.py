"""The terminal UI (`mobster`, `mobster tui`). Textual loads only when it opens, so `serve` never imports it."""

import signal
import sys


def main(args):
    """Open the terminal UI for parsed command-line ``args``. Returns an exit code."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("mobster: the terminal UI needs an interactive terminal. In a script, run the task with "
              "`mobster run \"TASK\" --execute` (JSON lines when piped).", file=sys.stderr)
        return 2
    try:
        from .app import MobsterApp
    except ImportError as error:
        print(f"mobster: the terminal UI is not available in this build ({error.name} is missing). "
              "Give the task in Mobster for Mac (https://mobster.dev/download), or use `mobster run`.",
              file=sys.stderr)
        return 1
    from .images import image_widget
    image_class = image_widget()  # before the UI starts: it may ask the terminal what it can draw
    demo = bool(getattr(args, "demo", False))
    wda_url = getattr(args, "wda_url", None)
    env_file = getattr(args, "env_file", None)

    def open_session():
        # Runs on a worker thread after the first frame: the agent's modules load behind the UI.
        from .session import Session
        return Session(wda_url=wda_url, env_file=env_file, demo=demo)

    resume = getattr(args, "resume", None)
    app = MobsterApp(open_session, demo=demo, resume=resume,
                     app_id=getattr(args, "app", None),
                     bell=not getattr(args, "no_bell", False), image_class=image_class,
                     task=getattr(args, "task", None), continue_=bool(getattr(args, "continue_", False)),
                     thread_id=getattr(args, "thread_id", None))
    with _exit_on_signals(app) as caught:
        app.run()
    from ..style import color_enabled
    summary = app.transcript_summary(color=color_enabled(sys.stdout))
    session = app.session
    if session is not None:
        if session.run is not None and session.run.finished_at is None:
            _say("Stopping the running task at its next safe point…", sys.stderr)
        session.close()
    if summary:
        _say(summary, sys.stdout)
    return 128 + caught[0] if caught else 0


class _exit_on_signals:
    """SIGTERM and SIGHUP (a closed terminal window, `kill`) end the UI the way ctrl+d does,
    so Textual restores the terminal."""

    NAMES = ("SIGTERM", "SIGHUP")

    def __init__(self, app):
        self.app, self.caught, self.previous = app, [], {}

    def handler(self, signum, frame):
        self.caught.append(signum)
        loop = getattr(self.app, "_loop", None)
        if len(self.caught) == 1 and loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self.app.exit)  # safe from a signal handler: it wakes the loop
                return
            except RuntimeError:
                pass
        raise KeyboardInterrupt

    def __enter__(self):
        for name in self.NAMES:
            number = getattr(signal, name, None)
            if number is not None:
                self.previous[number] = signal.signal(number, self.handler)
        return self.caught

    def __exit__(self, *exc):
        for number, old in self.previous.items():
            signal.signal(number, old)
        return False


def _say(text, stream):
    """Print after the UI closed; the terminal may be gone (SIGHUP), and that is no error."""
    try:
        print(text, file=stream, flush=True)
    except (OSError, ValueError):
        pass
