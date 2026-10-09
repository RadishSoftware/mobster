"""`mobster chat` when neither Mobster for Mac nor `mobster serve` is running: the conversation runs here instead.

- `mobster chat` alone, on a terminal: the terminal UI opens on your last conversation (`mobster -c`).
- `mobster chat "message"`: the message goes to a conversation in this terminal's own history, the one the terminal
  UI keeps, and its task runs in this process (the same runtime, ``tui.session.Session``), its steps printed as
  `mobster run` prints them. --json prints one object at the end, as `mobster chat --json` does with the app open.

When a Mobster does answer, this steps aside and `mobster chat` talks to it as before (threads/cli.py).
"""

import json
import os
import sys

LOCAL = "terminal"  # the key this terminal's own conversations are remembered under (threads/cli.remember_thread)
NOT_RUNNING_HERE = ("Mobster isn't running. Open Mobster for Mac, or run `mobster` for a conversation in this "
                    "terminal.")


def service_running(url=None):
    """Whether a Mobster (the Mac app or `mobster serve`) answers at ``url``."""
    from .threads.client import Client, NoService, ServiceError
    try:
        Client.connect(url, origin="cli")
    except NoService:
        return False
    except ServiceError:
        return True  # it answers, but refuses: threads/cli says why
    return True


def maybe_here(args):
    """The exit code when the conversation ran here, or None to let `mobster chat` use the running Mobster."""
    if getattr(args, "url", None) or service_running():
        return None
    text = " ".join(getattr(args, "message", None) or []).strip()
    as_json = bool(getattr(args, "json", False))
    if not text:
        if sys.stdin.isatty() and sys.stdout.isatty() and not as_json:
            from .tui import main as tui_main
            for name, value in (("continue_", not getattr(args, "new", False)), ("demo", False), ("resume", None),
                                ("app", None), ("no_bell", False), ("task", None), ("wda_url", None),
                                ("env_file", getattr(args, "env_file", None))):
                if not hasattr(args, name) or name == "continue_":
                    setattr(args, name, value)
            args.thread_id = getattr(args, "thread", None)
            return tui_main(args)
        if as_json:
            print(json.dumps({"status": "couldnt_run", "error": NOT_RUNNING_HERE, "code": "not_running"}), flush=True)
        else:
            print(f"mobster: {NOT_RUNNING_HERE}", file=sys.stderr)
        return 3
    return run_here(args, text, as_json)


def run_here(args, text, as_json):
    from .engines import smart_key
    from .smart_run import Approver, NOBODY_TO_ASK
    from .threads import cli as chat_cli
    from .tui.session import Session, error_text
    if not smart_key():
        message = "no model key yet. Run `mobster login`, then run this again."
        if as_json:
            print(json.dumps({"status": "couldnt_run", "error": message, "code": "no_key"}), flush=True)
        else:
            print(f"mobster: {message}", file=sys.stderr)
        return chat_cli.EXIT_COULDNT
    try:
        session = Session(wda_url=os.environ.get("MOBSTER_WDA_URL"), origin="cli")
    except Exception as error:  # noqa: BLE001 -- another terminal UI holds the history, or the runtime can't start
        message = f"{error_text(error)} {NOT_RUNNING_HERE}"
        if as_json:
            print(json.dumps({"status": "couldnt_run", "error": message, "code": "not_running"}), flush=True)
        else:
            print(f"mobster: {message}", file=sys.stderr)
        return chat_cli.EXIT_COULDNT
    try:
        if session.threads() is None:
            print(f"mobster: {NOT_RUNNING_HERE}", file=sys.stderr)
            return chat_cli.EXIT_COULDNT
        thread_id = _thread(session, args)
        try:
            routed, run, response = session.send(thread_id, text)
        except Exception as error:  # noqa: BLE001 -- the service's own sentence
            message = error_text(error)
            if as_json:
                print(json.dumps({"status": "couldnt_run", "error": message, "threadId": thread_id}), flush=True)
            else:
                print(f"mobster: {message}", file=sys.stderr)
            return chat_cli.EXIT_COULDNT
        chat_cli.remember_thread(LOCAL, thread_id)
        if routed == "remember":
            return _offer(response, thread_id, as_json)
        if run is None:
            print(json.dumps({"threadId": thread_id, "status": routed}) if as_json else "Mobster has your message.")
            return chat_cli.EXIT_DONE
        from .__main__ import event_printer, output
        emit = output if as_json else event_printer(args)
        if emit is not output:
            from .engines import smart_model
            emit.start(text, f"Smart · {smart_model()}")
        approver = None if as_json else Approver(session, run, emit)

        def on_event(event):
            kind = event.get("event")
            if emit is not output and kind != "run_finished":
                emit(event)
            if kind == "approval_requested":
                if approver is not None:
                    approver.ask(event)
                else:
                    session.answer(event.get("approval_id"), False, run=run)
        try:
            session.follow(run, on_event)
        except KeyboardInterrupt:
            session.stop(run)
            return 130
        finally:
            if emit is not output:
                emit.close()
        public = run.public(include_events=False)
        result = chat_cli.outcome(public, thread_id)
        if as_json:
            if public.get("status") == "approval_denied":
                result["note"] = NOBODY_TO_ASK
            print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        return chat_cli.EXIT_DONE if result["status"] in chat_cli.DONE else chat_cli.EXIT_NOT_DONE
    finally:
        session.close()


def _thread(session, args):
    """--thread ID, else this terminal's last conversation (unless --new), else a new one."""
    from .threads import cli as chat_cli
    live = session.threads()
    wanted = (getattr(args, "thread", None) or "").strip().lower()
    if wanted:
        live.thread(wanted)  # APIError 404 when it isn't here
        return wanted
    if not getattr(args, "new", False):
        last = chat_cli.last_thread(LOCAL)
        if last and live.store.get(last) and not live.store.get(last).get("archived"):
            return last
    return session.new_thread()["id"]


def _offer(response, thread_id, as_json):
    """A message that only asked Mobster to remember something: offer it, and on a terminal ask Remember or Not
    now. Nothing is saved without a yes."""
    proposals = response.get("proposals") or []
    if as_json:
        print(json.dumps({"threadId": thread_id, "status": "remember", "proposals": proposals}), flush=True)
        return 0
    from .memory.store import default_store
    from .style import palette
    paint = palette(sys.stdout)
    store = default_store()
    if not proposals:
        print("Mobster already has this.")
        return 0
    for proposal in proposals:
        print(paint("Remember this?", "bold") + f"  “{proposal['text']}”")
        print("  " + paint("Mobster will use it in tasks it fits. It stays on this Mac.", "tertiary"))
        if not sys.stdin.isatty():
            print("  " + paint("Answer it in `mobster` or Mobster for Mac.", "tertiary"))
            continue
        print("  " + paint("[y]", "accent", "bold") + " Remember   " + paint("[n]", "bold") + " Not now")
        while True:
            try:
                word = input("  Type y or n, then Return: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                word = "n"
            if word in ("y", "n"):
                break
        try:
            store.resolve_proposal(proposal["id"], word == "y")
        except Exception as error:  # noqa: BLE001 -- the store's own sentence
            print(f"mobster: {error}", file=sys.stderr)
            return 1
        print(paint("✓ ", "green") + "Mobster will remember this." if word == "y" else "Not remembered.")
    return 0
