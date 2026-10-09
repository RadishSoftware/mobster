"""`mobster run` on Mobster's agent (Smart), or on the scripted demo phone (`run --demo`).

Smart runs through the same runtime the terminal UI and Mobster for Mac use (``tui.session.Session``, started
without a UI), so nothing about a run is reimplemented: its events, approvals, stop and result are the runtime's.
On a terminal each step prints as a line (``console.ConsoleRenderer``); piped, or with --json, every event prints as
one JSON object, and the last one is the ``result``, as `run` on Fast always did.

Approvals: Smart asks before it sends, buys, posts or deletes. On a terminal you answer with y or n. Without one
(--json, a pipe, a script) nobody can answer, so the answer is no: the run ends as declined (exit 1) and says where
to run it instead. Nothing typed into a task, and no flag, approves an action.
"""

import os
import sys
import threading

DONE = {"completed", "completed_unverified", "expected_text_visible", "user_condition_met"}
NOBODY_TO_ASK = ("Mobster asks before it sends, buys, posts or deletes, and nobody can answer here (--json or a "
                 "pipe), so it didn't. Run it on a terminal without --json, in `mobster`, or in Mobster for Mac.")
REASSURANCE = {"send": "sent", "post": "posted", "pay": "paid", "buy": "bought", "book": "booked",
               "delete": "deleted"}


def run_smart(args, demo=False):
    """Run ``args.goal`` on Smart (or the demo). Returns the exit code."""
    from .__main__ import EXIT_COULDNT, EXIT_FAILED, EXIT_OK, EXIT_USAGE, MissingKey, NO_KEY, NoDevice, \
        error_result, event_printer, output, stop_at_safe_point, wda_url_for
    as_json = bool(getattr(args, "json", False)) or not sys.stdout.isatty()
    remember = remember_only(args.goal)
    if remember:
        # "Remember that my gym is …" is a note for Mobster, not a task for the phone: nothing runs.
        quoted = remember[0].text.replace('"', "'")
        message = ("That's something for Mobster to remember, not a task for your iPhone, so nothing ran. Save it "
                   f'with: mobster memory add "{quoted}"')
        if as_json:
            output({"event": "result", "status": "error", "ok": False, "error": message, "reason": message,
                    "routed": "remember", "exit_code": EXIT_USAGE})
        else:
            print(f"mobster run: {message}", file=sys.stderr)
        return EXIT_USAGE
    if not demo:
        from .engines import smart_key
        if not smart_key():
            raise MissingKey(NO_KEY)
        if not args.execute:
            message = ("Mobster's agent acts from its first step, so there is nothing to preview. Add --execute to "
                       "run it: it asks before it sends, buys, posts or deletes.")
            if as_json:
                output({"event": "result", "status": "error", "ok": False, "error": message, "reason": message,
                        "exit_code": EXIT_USAGE})
            else:
                print(f"mobster run: {message}", file=sys.stderr)
            return EXIT_USAGE
    if getattr(args, "allow_app", None) or getattr(args, "expected_text", None) or getattr(args, "helper", False):
        flags = [flag for flag, on in (("--allow-app", args.allow_app), ("--expected-text", args.expected_text),
                                       ("--helper", args.helper)) if on]
        message = f"{' and '.join(flags)} {'is' if len(flags) == 1 else 'are'} for Quick mode: add --engine fast."
        print(f"mobster run: {message}", file=sys.stderr)
        return EXIT_USAGE
    from .tui.session import Session, error_text
    emit = event_printer(args, preview=False)
    try:
        session = Session(wda_url=None if demo else wda_url_for(args), demo=demo, journal=False, origin="cli",
                          spend_cap_usd=args.spend_cap_usd, pace=float(os.environ.get("MOBSTER_DEMO_PACE", "1")))
    except Exception as error:
        raise RuntimeError(error_text(error)) from None
    stop = threading.Event()
    try:
        app_id = _app_for(args.goal, session) if demo else None
        try:
            run = session.start(app_id, args.goal)
        except Exception as error:
            text = error_text(error)
            code = getattr(error, "code", None)
            exc = NoDevice(text) if code in ("device_unavailable", "device_busy") else RuntimeError(text)
            if code == "engine_unavailable" and "key" in text.lower():
                exc = MissingKey(text)
            raise exc from None
        if emit is not output:
            from .engines import smart_model
            where = "scripted demo · no iPhone, no model calls" if demo else f"Smart · {smart_model()}"
            emit.start(args.goal, where)
        prompt = None if as_json else Approver(session, run, emit)
        with stop_at_safe_point(stop, on_stop=lambda: session.stop(run)) as caught:
            result = {}

            def on_event(event):
                kind = event.get("event")
                if kind == "run_finished":
                    return
                if kind == "result":
                    # Printed last, after the run's other closing events, so a script's last line is the result.
                    result.update(event)
                    return
                if emit is output:
                    output(event)
                else:
                    emit(event)
                if kind == "approval_requested":
                    if prompt is not None:
                        prompt.ask(event)
                    else:
                        session.answer(event.get("approval_id"), False, run=run)
                        output({"event": "note", "text": NOBODY_TO_ASK})
            try:
                session.follow(run, on_event)
            except KeyboardInterrupt:
                session.stop(run)
                if emit is output:
                    output({"event": "result", "status": "stopped", "reason": "Stopped with a second signal before "
                            "the next safe point; an action may have been sent", "exit_code": 130})
                raise
        if not result:
            summary = run.summary if isinstance(getattr(run, "summary", None), dict) else {}
            result = {**summary, "event": "result", "status": run.status}
        if emit is output:
            output(result)
        else:
            emit(result)
        if caught:
            return 128 + caught[0]
        status = result.get("status") or run.status
        if status == "error" and _no_phone(result):
            return EXIT_COULDNT
        return EXIT_OK if status in DONE else EXIT_FAILED
    finally:
        if emit is not output:
            emit.close()
        session.close()


def remember_only(goal):
    """The things to remember when ``goal`` only asks Mobster to remember something, else None."""
    try:
        from .memory.extract import remember_only as only
    except ImportError:  # a build without memory runs it as a task, as before
        return None
    return only(goal)


def _no_phone(result):
    text = f"{result.get('reason') or ''} {result.get('error') or ''}".lower()
    return any(words in text for words in ("not reachable", "not answering", "unplugged", "no phone"))


def _app_for(goal, session):
    """The demo's app: the one the task names, else Settings (app_choice.infer_app, as the terminal UI does)."""
    from .app_choice import infer_app
    apps = session.apps()
    app = infer_app(goal, apps)
    return (app or next(a for a in apps if a["id"] == "settings"))["id"]


def verbs(request):
    """(yes, no) as Mobster for Mac's buttons say them: Send / Don't send …, else Approve / Decline."""
    from .threads.cli import verbs as chat_verbs
    return chat_verbs(request or {})


def reassurance(request):
    """MESSAGING §11: "Nothing is sent until you choose. If you're away, Mobster waits 10 minutes, then stops
    without sending it." (Nothing happens … for Approve.)"""
    yes, _ = verbs(request)
    done = REASSURANCE.get(yes.lower())
    if done is None:
        return ("Nothing happens until you choose. If you're away, Mobster waits 10 minutes, then stops without "
                "doing it.")
    verb = {"sent": "sending", "posted": "posting", "paid": "paying", "bought": "buying", "booked": "booking",
            "deleted": "deleting"}[done]
    return f"Nothing is {done} until you choose. If you're away, Mobster waits 10 minutes, then stops without " \
           f"{verb} it."


class Approver:
    """Asks on the terminal: the exact action and text, then y or n. Only a line that is exactly y or n answers;
    ctrl+c answers no and stops the task."""

    def __init__(self, session, run, renderer):
        self.session, self.run, self.renderer = session, run, renderer

    def ask(self, event):
        from .style import palette
        from .threads.cli import approval_title
        request = self.session.pending_approval(self.run) or {}
        if not request:
            return
        paint = palette(sys.stdout)
        yes, no = verbs(request)
        pause = getattr(self.renderer, "pause", None)
        if pause is not None:
            pause()
        lines = ["", "  " + paint("?", "accent", "bold") + " " + paint(approval_title(request), "bold")]
        if request.get("text"):
            lines.append("    " + f"“{request['text']}”")
        lines.append("    " + paint(reassurance(request), "tertiary"))
        lines.append("    " + paint("[y]", "accent", "bold") + f" {yes}   " + paint("[n]", "bold") + f" {no}")
        print("\n".join(lines), flush=True)
        answer = None
        while answer is None:
            try:
                line = input("    Type y or n, then Return: ")
            except (EOFError, KeyboardInterrupt):
                print()
                answer = False
                self.session.answer(request["id"], False, run=self.run)
                self.session.stop(self.run)
                break
            word = line.strip().lower()
            if word in ("y", "n"):
                answer = word == "y"
        if answer:
            self.session.answer(request["id"], True, run=self.run)
        elif answer is False and self.session.pending_approval(self.run):
            self.session.answer(request["id"], False, run=self.run)
        resume = getattr(self.renderer, "resume", None)
        if resume is not None:
            resume()
