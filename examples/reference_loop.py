"""A reference for reading the engine: one task through the observe, decide, verify, act loop, in the order a run
uses its API.

You bring a WebDriverAgent runner that already serves an iPhone at a URL, and a Claude or OpenAI key in the
environment. This repository doesn't build, sign or start WebDriverAgent. The task and the one app it may open are
fixed below. Every approval the loop asks for is answered no, so it stops before anything it would send, buy, post or
delete. The events stay in memory; mobile_agent/journal.py is how Mobster journals each one before the next action.

    make venv
    WDA_URL=http://127.0.0.1:8100 ANTHROPIC_API_KEY=... PYTHONPATH=. mobile_agent/.venv/bin/python examples/reference_loop.py

Mobster for Mac is the product built on this loop: https://mobster.dev
"""

import json
import os
import sys

from mobile_agent.drivers import build_driver
from mobile_agent.engines import build_client, build_frontier, smart_config, smart_key, summarize

GOAL = "Open Settings, then General, then About, and read the iOS version."
APPS = {"com.apple.Preferences": "Settings"}  # the only app the agent may open
MAX_STEPS = 12


def decline(request):
    """Every approval (a send, a buy, a post, a delete, an iOS prompt) is answered no."""
    return "denied"


def run(driver, client, goal=GOAL, apps=APPS):
    """One task: the agent observes the screen through ``driver``, decides with ``client``, verifies and acts.
    Returns the summary (status, answer, reason) and every event the loop emitted."""
    events = []
    agent = build_frontier(driver, client, apps=apps, emit=events.append, approve=decline, dialog_approve=decline,
                           config=smart_config(), max_steps=MAX_STEPS)
    outcome = agent.run(goal)
    return summarize(outcome, agent, None, request=goal), events


def main():
    url = os.environ.get("WDA_URL")
    if not url or not smart_key():
        print(__doc__, file=sys.stderr)
        return 2
    driver = build_driver("wda", url=url.rstrip("/"), session=None)  # observe and act: WebDriverAgent's HTTP API
    driver.configure()
    client = build_client(smart_config(), key=smart_key())          # decide: the model, on your key
    try:
        summary, events = run(driver, client)
    finally:
        driver.close()
    for event in events:
        if event.get("event") == "frontier_decision":
            print(event.get("step"), event.get("operation"), event.get("target_label") or "")
    print(json.dumps({key: summary.get(key) for key in ("status", "answer", "reason")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
