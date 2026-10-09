#!/usr/bin/env python3
"""A real key-less (or Smart) session through `mobster mcp`, driven by a minimal stdio MCP client.

Opt-in: it needs Xcode and boots a Mobster simulator. By default it checks Settings the way a coding agent
would: initialize, tools/list, verify_start on com.apple.Preferences expecting `visible: {label: About}`,
screen, tap "General", wait_for "About", verify_finish, and asserts the verdict is passed with a
verdict-ax proof frame. Every call is timed; a call over 45 s (wait over 50 s) fails the smoke.

    python scripts/mcp_smoke.py
    python scripts/mcp_smoke.py --repeat 3 --json            # cold, then warm sessions in one server
    python scripts/mcp_smoke.py --smart "Open General, then About" --expect '{"text": "iOS Version"}' \\
        --server-arg=--env-file --server-arg=/path/to/.env      # one Smart run (costs model spend)

Other apps (the ship workstream's Daybreak legs) pass their own launch fields, actions and verdict:

    python scripts/mcp_smoke.py --app /abs/Daybreak.app --launch-arg=-DaybreakBug --launch-arg=missing-plan \\
        --action '{"tool": "tap", "target": {"id": "onboarding_continue"}}' \\
        --action '{"tool": "alert", "action": "dismiss", "button": "Don’t Allow"}' \\
        --expect '{"count": {"id": "/^plan_/"}, "equals": 3}' --expect-verdict failed

An --action is a tools/call: {"tool": NAME, ...arguments}; run_id is filled in. iOS 26.4 names the
notification prompt's button "Don’t Allow", with a curly apostrophe (U+2019). The alert tool compares names
after normalization, so "Don't Allow" presses it too, and it sends WebDriverAgent the real name.
Exit codes: 0 every session ended with the expected verdict inside the time limits, 1 otherwise, 2 usage.
"""

import argparse
import json
import os
from pathlib import Path
import queue
import re
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
CALL_LIMIT, WAIT_LIMIT = 45.0, 50.0
READY_LIMIT = 600.0  # a cold first run builds WebDriverAgent (about a minute) and boots a simulator
DEFAULT_ACTIONS = [{"tool": "screen", "image": False}, {"tool": "tap", "target": {"label": "General"}},
                   {"tool": "wait_for", "expect": [{"text": "About"}], "timeout_s": 10}]
ACTIONS = {"tap", "type_text", "swipe", "alert", "open_url", "relaunch"}


def log(message):
    print(f"mcp_smoke {time.strftime('%H:%M:%S')} {message}", file=sys.stderr, flush=True)


class Client:
    """Newline-delimited JSON-RPC over the server's stdin and stdout."""

    def __init__(self, command, env, server_log):
        self.log_handle = open(server_log, "ab") if server_log else None
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.log_handle or None, env=env, cwd=str(ROOT))
        self.messages = queue.Queue()
        self.next_id = 0
        self.calls = []  # (tool, seconds, is_error)
        self.progress = 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                self.messages.put({"_bad_line": line.decode(errors="replace")[:200]})
        self.messages.put(None)

    def request(self, method, params=None, timeout=120.0):
        self.next_id += 1
        request_id = self.next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()
        end = time.monotonic() + timeout
        while True:
            try:
                reply = self.messages.get(timeout=max(0.01, end - time.monotonic()))
            except queue.Empty:
                raise SystemExit(f"no answer to {method} in {timeout:.0f} s") from None
            if reply is None:
                raise SystemExit(f"the server exited during {method}")
            if "_bad_line" in reply:
                raise SystemExit(f"the server wrote a line that is not JSON: {reply['_bad_line']!r}")
            if reply.get("method") == "notifications/progress":
                self.progress += 1
                continue
            if reply.get("id") == request_id:
                if "error" in reply:
                    raise SystemExit(f"{method} failed: {reply['error']}")
                return reply["result"]

    def call(self, tool, arguments):
        started = time.monotonic()
        result = self.request("tools/call", {"name": tool, "arguments": arguments, "_meta": {"progressToken": tool}})
        seconds = time.monotonic() - started
        self.calls.append((tool, seconds, bool(result.get("isError"))))
        text = result["content"][0]["text"]
        first = text.splitlines()[0] if text else ""
        log(f"{tool} {seconds:.2f} s{' ERROR' if result.get('isError') else ''}: {first[:150]}")
        return result, seconds

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            code = self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            code = "killed"
        if self.log_handle:
            self.log_handle.close()
        return code


def structured(result):
    if "structuredContent" in result:
        return result["structuredContent"]
    raise SystemExit("the result has no structuredContent (the client asked for 2025-11-25)")


def session(client, args, launch, index):
    """One key-less session (or one Smart run); returns its facts."""
    facts = {"index": index}
    if args.smart:
        result, seconds = client.call("verify", {**launch, "steps": args.smart, "expect": args.expect,
                                                 "max_usd": args.max_usd, "image": False})
        facts["start_seconds"] = seconds
    else:
        result, seconds = client.call("verify_start", {**launch, "steps": args.step or ["Open General"],
                                                       "expect": args.expect, "image": False})
        facts["start_seconds"] = seconds
    data = structured(result)
    if result.get("isError"):
        raise SystemExit(result["content"][0]["text"])
    run_id = data.get("run_id")
    facts["run_id"] = run_id
    waited, deadline = 0.0, time.monotonic() + READY_LIMIT
    while data.get("status") in ("preparing", "running", "finishing"):
        if time.monotonic() > deadline:
            raise SystemExit(f"run {run_id} was still {data['status']} after {READY_LIMIT:.0f} s")
        result, seconds = client.call("wait", {"run_id": run_id, "timeout_s": 40})
        waited += seconds
        data = structured(result)
    facts["ready_seconds"] = round(facts["start_seconds"] + waited, 2)
    if not args.smart and data.get("status") == "ready":
        for action in args.action or DEFAULT_ACTIONS:
            action = dict(action)
            tool = action.pop("tool")
            result, seconds = client.call(tool, {"run_id": run_id, **action})
            if result.get("isError"):
                facts.setdefault("action_errors", []).append(f"{tool}: {result['content'][0]['text'][:200]}")
            elif tool in ACTIONS:
                facts.setdefault("action_seconds", []).append(round(seconds, 3))
                facts.setdefault("action_server_seconds", []).append(structured(result).get("seconds"))
        result, seconds = client.call("verify_finish", {"run_id": run_id, "image": True})
        facts["finish_seconds"] = round(seconds, 2)
        data = structured(result)
        facts["images"] = sum(1 for block in result["content"] if block["type"] == "image")
    facts["verdict"] = data.get("verdict")
    facts["summary"] = data.get("summary")
    facts["reason"] = data.get("reason")
    facts["cost_usd"] = data.get("cost_usd")
    facts["proof"] = data.get("proof") or []
    facts["verdict_ax"] = next((p for p in facts["proof"] if "verdict-ax" in p and os.path.isfile(p)), None)
    facts["report"] = data.get("report")
    return facts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default=None,
                        help="the server command (default: this checkout's python -m mobile_agent.mcp_server)")
    parser.add_argument("--server-arg", action="append", default=[], help="an extra server argument; repeatable")
    parser.add_argument("--server-log", help="append the server's stderr here (default: this stderr)")
    parser.add_argument("--bundle", default=None, help="bundle ID (default com.apple.Preferences without --app)")
    parser.add_argument("--app", help="absolute path to a .app built for the iOS Simulator")
    parser.add_argument("--device", help="simulator device type")
    parser.add_argument("--runtime", help="iOS runtime")
    parser.add_argument("--reset", choices=["none", "data", "reinstall"], help="reset level")
    parser.add_argument("--launch-arg", action="append", default=[], help="a launch argument; repeatable")
    parser.add_argument("--launch-env", action="append", default=[], help="KEY=VALUE; repeatable")
    parser.add_argument("--open-url", help="a deep link opened after launch")
    parser.add_argument("--step", action="append", help="a step, in plain English; repeatable")
    parser.add_argument("--expect", action="append", type=json.loads,
                        help='an assertion as JSON; repeatable (default {"visible": {"label": "About"}})')
    parser.add_argument("--action", action="append", type=json.loads,
                        help='a tools/call as JSON, {"tool": NAME, ...}; repeatable (default: screen, tap '
                             'General, wait_for About)')
    parser.add_argument("--expect-verdict", default="passed",
                        choices=["passed", "failed", "needs_review", "couldnt_run"], help="default passed")
    parser.add_argument("--smart", action="append", metavar="STEP",
                        help="run Smart's verify with these steps instead of a key-less session (model spend)")
    parser.add_argument("--max-usd", type=float, default=0.15, help="Smart's spend cap (default 0.15)")
    parser.add_argument("--repeat", type=int, default=1, help="sessions in one server (default 1)")
    parser.add_argument("--protocol", default="2025-11-25", help="the protocolVersion to ask for")
    parser.add_argument("--json", action="store_true", help="print the facts as JSON on stdout")
    args = parser.parse_args(argv)
    args.expect = args.expect or [{"visible": {"label": "About"}}]
    if args.app and not os.path.isabs(args.app):
        parser.error("--app must be an absolute path")
    launch = {"bundle_id": args.bundle or (None if args.app else "com.apple.Preferences"), "app_path": args.app,
              "device": args.device, "runtime": args.runtime, "reset": args.reset,
              "launch_args": args.launch_arg or None, "open_url": args.open_url,
              "launch_env": dict(item.split("=", 1) for item in args.launch_env) or None}
    launch = {key: value for key, value in launch.items() if value}

    command = args.server.split() if args.server else [sys.executable, "-m", "mobile_agent.mcp_server"]
    command += args.server_arg
    if not args.smart and "--keyless" not in command:
        command.append("--keyless")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    client = Client(command, env, args.server_log)
    report = {"date": time.strftime("%Y-%m-%d"), "command": " ".join(command[1:] if not args.server else command)}
    failures = []
    try:
        started = time.monotonic()
        init = client.request("initialize", {"protocolVersion": args.protocol, "capabilities": {},
                                             "clientInfo": {"name": "mcp_smoke", "version": "1"}})
        report["initialize_seconds"] = round(time.monotonic() - started, 3)
        report["protocol"] = init["protocolVersion"]
        client.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        client.process.stdin.flush()
        tools = [tool["name"] for tool in client.request("tools/list")["tools"]]
        report["tools"] = tools
        log(f"{len(tools)} tools: {', '.join(tools)}")
        if args.smart and "verify" not in tools:
            raise SystemExit("verify isn't offered: the server found no OpenAI or Anthropic key for Smart, and its log "
                             "says why (pass --server-arg=--env-file ...)")
        smart = re.search(r"verify runs the steps itself with (\S+), on the user's (\w+) key",
                          init.get("instructions", ""))
        if smart:
            report["smart_model"], report["smart_provider"] = smart.groups()
            log(f"Smart: {smart.group(1)}, on the {smart.group(2)} key")
        sessions = []
        for index in range(args.repeat):
            facts = session(client, args, launch, index)
            sessions.append(facts)
            log(f"session {index + 1}: {facts['verdict']} ({facts.get('summary')})")
            if facts["verdict"] != args.expect_verdict:
                failures.append(f"session {index + 1} ended {facts['verdict']}, expected {args.expect_verdict}: "
                                f"{facts.get('reason')}")
            if facts["verdict"] in ("passed", "failed") and not facts["verdict_ax"]:
                failures.append(f"session {index + 1} has no verdict-ax proof frame")
            if facts.get("action_errors"):
                failures.append(f"session {index + 1} action errors: {facts['action_errors']}")
        report["sessions"] = sessions
    finally:
        report["exit_code"] = client.close()
    actions = [s for facts in report.get("sessions", []) for s in facts.get("action_seconds", [])]
    report["calls"] = len(client.calls)
    report["progress_notifications"] = client.progress
    if actions:
        report["action_p50_seconds"] = round(statistics.median(actions), 3)
        report["action_n"] = len(actions)
    longest = max(client.calls, key=lambda call: call[1], default=None)
    if longest:
        report["longest_call"] = {"tool": longest[0], "seconds": round(longest[1], 2)}
    for tool, seconds, _error in client.calls:
        if seconds > (WAIT_LIMIT if tool == "wait" else CALL_LIMIT):
            failures.append(f"{tool} took {seconds:.1f} s")
    if report.get("exit_code") != 0:
        failures.append(f"the server exited with {report.get('exit_code')}")
    report["ok"] = not failures
    report["failures"] = failures
    if args.json:
        print(json.dumps(report, indent=2))
    for failure in failures:
        log("FAIL " + failure)
    log("PASS" if not failures else "FAILED")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
