"""Drive Mobster from Python: the engine the terminal UI uses, without the UI.

Runs offline against the scripted demo phone by default, so you can try it with no
iPhone and no keys:

    python examples/embed_runtime.py
    python examples/embed_runtime.py --wda-url http://127.0.0.1:8100 "Turn on Dark Mode"   # a real phone

Approvals are answered by a rule in this file (approve everything except Delete),
to show where your own policy would go.
"""

import argparse
import sys
import threading

from mobile_agent.narrate import Narrator, Outcome, Step, seconds
from mobile_agent.tui.session import Session


def policy(request):
    """Your approval rule: True to let the action go ahead."""
    return "delete" not in (request.get("label") or "").lower()


def main():
    parser = argparse.ArgumentParser(description="Run one task through Mobster's runtime and print its steps.")
    parser.add_argument("goal", nargs="?", default="Text Alex 'running 10 minutes late'")
    parser.add_argument("--app", default=None, help="app id (default: messages for the demo, else settings)")
    parser.add_argument("--wda-url", help="drive a real phone at this WebDriverAgent address instead of the demo")
    parser.add_argument("--env-file", help="KEY=VALUE file with TYPESAFE_API_KEY for a real phone")
    args = parser.parse_args()

    demo = args.wda_url is None
    session = Session(demo=demo, wda_url=args.wda_url, env_file=args.env_file, pace=0.2)
    try:
        run = session.start(args.app or ("messages" if demo else "settings"), args.goal)
        narrator, printed = Narrator(), set()
        print(f"❯ {args.goal}  ({run.app['name']}{', scripted demo' if demo else ''})")

        def on_event(event):
            for item in narrator.feed(event):
                if isinstance(item, Step) and item.act and item.ended_at is not None and item.key not in printed:
                    printed.add(item.key)
                    print(f"  {item.title}  ({seconds(item.duration_ms)})")
                elif isinstance(item, Outcome):
                    print(f"{item.label}: {item.message}")
            if event["event"] == "approval_requested":
                request = session.pending_approval(run)
                allow = policy(request)
                print(f"  approval: {request['operation'].lower()} “{request['label']}” → {'yes' if allow else 'no'}")
                # Answer from another thread: this callback runs on the thread following the run.
                threading.Thread(target=session.answer, args=(request["id"], allow), kwargs={"run": run}).start()

        session.follow(run, on_event)
        return 0 if run.status in {"completed_unverified", "expected_text_visible"} else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
