"""Run a task through `mobster serve`, print its steps as they stream, and answer approvals here.

Start the server first (it prints the path of its token file on its first line):

    mobster serve --wda-url http://127.0.0.1:8100 --enable-live --env-file "$MOBSTER_ENV_FILE" > /tmp/mobster-serve.log &

Then:

    python examples/api_client.py --token-file "$(head -1 /tmp/mobster-serve.log | jq -r .tokenFile)" \
        settings "Turn on Dark Mode"

Standard library only. The API is described in docs/scripting.md.
"""

import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request


class Mobster:
    def __init__(self, token, base="http://127.0.0.1:8765"):
        self.base, self.token = base.rstrip("/"), token

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = json.load(error).get("error", error.reason)
            raise SystemExit(f"{method} {path}: {error.code} {detail}") from None

    def events(self, run_id):
        """The run's events, from the first, until it finishes (server-sent events)."""
        request = urllib.request.Request(f"{self.base}/api/runs/{run_id}/events",
                                         headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(request, timeout=None) as stream:
            for raw in stream:
                line = raw.decode().rstrip("\n")
                if line.startswith("data: "):
                    yield json.loads(line[len("data: "):])


def describe(event, labels):
    """One line for the events a person wants to see, else None."""
    kind = event["event"]
    if kind == "observation":
        labels.clear()
        labels.update({e["id"]: e.get("label") for e in event.get("elements", [])})
    elif kind == "decision":
        target = event.get("target_label") or labels.get(event.get("target")) or ""
        return f"  {event['operation'].lower():<12} {target}  ({event.get('confidence', 0):.0%})"
    elif kind == "observation_after_action":
        return "               → screen changed" if event.get("changed") else "               → no visible change"
    elif kind == "run_finished":
        summary = event.get("summary") or {}
        return f"{event['status']}: {summary.get('reason', '')}"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("app", help="app id from GET /api/apps, e.g. settings, messages, safari")
    parser.add_argument("goal", help="the task, in plain words")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    args = parser.parse_args()

    mobster = Mobster(args.token_file.read_text().strip(), args.base)
    status = mobster.request("GET", "/api/status")
    if not status.get("live_enabled"):
        raise SystemExit("Live tasks are off: " + "; ".join(status.get("limitations") or ["see /api/status"]))

    run = mobster.request("POST", "/api/runs", {"appId": args.app, "goal": args.goal})["run"]
    print(f"❯ {args.goal}  ({run['appName']}, task {run['id']})")
    labels = {}
    for event in mobster.events(run["id"]):
        line = describe(event, labels)
        if line:
            print(line, flush=True)
        if event["event"] == "approval_requested":
            # The pending request (with any text to be typed) is on the run itself, never in the event stream.
            approval = mobster.request("GET", f"/api/runs/{run['id']}")["run"]["approval"]
            if approval:
                text = f' with "{approval["text"]}"' if approval.get("text") else ""
                answer = input(f"  Mobster wants to {approval['operation'].lower()} "
                               f"\"{approval['label']}\" in {approval['app']}{text}. Allow? [y/N] ")
                mobster.request("POST", f"/api/runs/{run['id']}/approval",
                                {"id": approval["id"], "approve": answer.strip().lower() == "y"})
    final = mobster.request("GET", f"/api/runs/{run['id']}")["run"]
    return 0 if final["status"] in {"completed_unverified", "expected_text_visible"} else 1


if __name__ == "__main__":
    sys.exit(main())
