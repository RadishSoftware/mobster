"""A stand-in for the codex and claude binaries that drives the phone over the run's MCP server."""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

PROMPT = ""


def out(obj):
    print(json.dumps(obj), flush=True)


class MCP:
    def __init__(self, url, token):
        self.url, self.token, self.n, self.session = url, token, 0, None
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "fake-agent", "version": "1"},
                                "capabilities": {}})
        self.notify("notifications/initialized")
        self.tools = [t["name"] for t in self.rpc("tools/list", {})["tools"]]

    def _post(self, body):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self.token}"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=900) as response:
            self.session = response.headers.get("Mcp-Session-Id") or self.session
            raw = response.read()
            return json.loads(raw) if raw else None

    def rpc(self, method, params):
        self.n += 1
        answer = self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        if "error" in answer:
            raise RuntimeError(answer["error"]["message"])
        return answer["result"]

    def notify(self, method):
        self._post({"jsonrpc": "2.0", "method": method})

    def call(self, name, arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(c.get("text", "") for c in result["content"] if c.get("type") == "text")
        return text, bool(result.get("isError"))


def element_id(screen, label):
    """The id the screen gives ``label`` ("e3 Cell \"Alex Rivera\" ...")."""
    found = re.search(r"^(e\d+) \w+ " + re.escape(json.dumps(label)), screen, re.M)
    return found.group(1) if found else None


class CodexStream:
    def __init__(self):
        self.items = 0

    def session(self, session_id):
        out({"type": "thread.started", "thread_id": session_id})
        out({"type": "turn.started"})

    def tool(self, mcp, name, arguments):
        self.items += 1
        item = {"id": f"item_{self.items}", "type": "mcp_tool_call", "server": "mobster", "tool": name,
                "arguments": arguments, "result": None, "error": None, "status": "in_progress"}
        out({"type": "item.started", "item": item})
        text, failed = mcp.call(name, arguments)
        done = {**item, "status": "failed" if failed else "completed"}
        if failed:
            done["error"] = {"message": text}
        else:
            done["result"] = {"content": [{"type": "text", "text": text}], "structured_content": None}
        out({"type": "item.completed", "item": done})
        return text, failed

    def message(self, text):
        self.items += 1
        out({"type": "item.completed", "item": {"id": f"item_{self.items}", "type": "agent_message", "text": text}})

    def reasoning(self, text):
        self.items += 1
        out({"type": "item.completed", "item": {"id": f"item_{self.items}", "type": "reasoning", "text": text}})

    def end(self, final, session_id):
        if final:
            self.message(final)
        # Some Codex versions print a token_count with the call's usage and the plan's windows.
        out({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 12000, "cached_input_tokens": 9000,
                                                                  "output_tokens": 300}},
             "rate_limits": {"primary": {"used_percent": 37.0, "window_minutes": 300, "resets_at": 1792030800},
                             "secondary": {"used_percent": 10.0, "window_minutes": 10080,
                                           "resets_at": 1792447200}}})
        out({"type": "turn.completed", "usage": {"input_tokens": 12000, "cached_input_tokens": 9000,
                                                  "cache_write_input_tokens": 0, "output_tokens": 300,
                                                  "reasoning_output_tokens": 120}})

    def limit(self, session_id):
        out({"type": "error", "message": "You've hit your usage limit. Upgrade to Pro "
                                         "(https://chatgpt.com/explore/pro), or try again in 2 hours 5 minutes."})
        out({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Upgrade to Pro "
                                                         "(https://chatgpt.com/explore/pro), or try again in 2 hours "
                                                         "5 minutes."}})
        sys.exit(1)

    def signed_out(self, session_id):
        out({"type": "error", "message": "unexpected status 401 Unauthorized: Your access token could not be "
                                         "refreshed. Please log out and sign in again."})
        out({"type": "turn.failed", "error": {"message": "401 Unauthorized"}})
        sys.exit(1)


class ClaudeStream:
    def __init__(self):
        self.n = 0
        self.m = 0
        self.sid = None
        self.shared = None  # the id of a response whose thinking block was printed; its tool call shares it

    def session(self, session_id):
        self.sid = session_id
        out({"type": "system", "subtype": "init", "session_id": session_id, "model": "claude-sonnet-5-5",
             "tools": ["mcp__mobster__screen", "mcp__mobster__tap"], "apiKeySource": "none",
             "mcp_servers": [{"name": "mobster", "status": "connected"}]})
        out({"type": "rate_limit_event", "session_id": session_id, "rate_limit_info": {
            "status": "allowed", "resetsAt": 1792030800, "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": 0.37, "resetsAt": 1792030800},
                               "seven_day": {"utilization": 0.1, "resetsAt": 1792447200}}}})

    def response(self):
        """The id and usage of the next API response (a thinking block and its tool call share one)."""
        if self.shared:
            found, self.shared = self.shared, None
            return found
        self.m += 1
        return f"msg_{self.m:03d}"

    def usage(self):
        return {"input_tokens": 3, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 6000,
                "output_tokens": 40}

    def tool(self, mcp, name, arguments):
        self.n += 1
        tid = f"toolu_{self.n:03d}"
        out({"type": "assistant", "session_id": self.sid, "message": {
            "id": self.response(), "model": "claude-sonnet-5-5", "role": "assistant", "usage": self.usage(),
            "content": [{"type": "tool_use", "id": tid, "name": f"mcp__mobster__{name}", "input": arguments}]}})
        text, failed = mcp.call(name, arguments)
        out({"type": "user", "session_id": self.sid, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": text}],
             "is_error": failed}]}})
        return text, failed

    def message(self, text):
        out({"type": "assistant", "session_id": self.sid, "message": {
            "id": self.response(), "model": "claude-sonnet-5-5", "role": "assistant", "usage": self.usage(),
            "content": [{"type": "text", "text": text}]}})

    def reasoning(self, text):
        self.shared = self.response()
        out({"type": "assistant", "session_id": self.sid, "message": {
            "id": self.shared, "model": "claude-sonnet-5-5", "role": "assistant", "usage": self.usage(),
            "content": [{"type": "thinking", "thinking": text}]}})

    def end(self, final, session_id):
        if final:
            self.message(final)
        out({"type": "result", "subtype": "success", "is_error": False, "result": final or "", "session_id": session_id,
             "num_turns": 6, "duration_ms": 4200, "duration_api_ms": 3100, "total_cost_usd": 0.0412,
             "usage": {"input_tokens": 40, "cache_read_input_tokens": 18000, "cache_creation_input_tokens": 2400,
                       "output_tokens": 350},
             "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 40, "outputTokens": 350,
                                                  "cacheReadInputTokens": 18000, "cacheCreationInputTokens": 2400,
                                                  "webSearchRequests": 0, "costUSD": 0.0412,
                                                  "contextWindow": 1000000, "costBasis": "list"}}})

    def limit(self, session_id):
        out({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1999999999,
                                                              "rateLimitType": "five_hour"}})
        out({"type": "result", "subtype": "success", "is_error": True, "session_id": session_id,
             "result": "Claude AI usage limit reached|1999999999"})
        sys.exit(1)

    def signed_out(self, session_id):
        out({"type": "result", "subtype": "success", "is_error": True, "session_id": session_id,
             "result": "Invalid API key · Please run /login"})
        sys.exit(1)


class GeminiStream:
    def __init__(self):
        self.n = 0

    def session(self, session_id):
        out({"type": "init", "timestamp": "t", "session_id": session_id, "model": "gemini-3.5-pro"})

    def tool(self, mcp, name, arguments):
        self.n += 1
        tid = f"t{self.n}"
        out({"type": "tool_use", "timestamp": "t", "tool_name": f"mcp_mobster_{name}", "tool_id": tid,
             "parameters": arguments})
        text, failed = mcp.call(name, arguments)
        event = {"type": "tool_result", "timestamp": "t", "tool_id": tid, "status": "error" if failed else "success"}
        if failed:
            event["error"] = {"type": "tool_error", "message": text}
        else:
            event["output"] = text
        out(event)
        return text, failed

    def message(self, text):
        half = len(text) // 2
        out({"type": "message", "timestamp": "t", "role": "assistant", "content": text[:half], "delta": True})
        out({"type": "message", "timestamp": "t", "role": "assistant", "content": text[half:], "delta": True})

    def reasoning(self, text):
        pass

    def end(self, final, session_id):
        if final:
            self.message(final)
        out({"type": "result", "timestamp": "t", "status": "success",
             "stats": {"total_tokens": 9000, "input_tokens": 8700, "output_tokens": 300, "cached": 6000,
                       "duration_ms": 5000, "tool_calls": 3}})

    def limit(self, session_id):
        out({"type": "error", "timestamp": "t", "severity": "error",
             "message": "Quota exceeded for quota metric 'Gemini requests per day'. RESOURCE_EXHAUSTED"})
        out({"type": "result", "timestamp": "t", "status": "error",
             "error": {"type": "quota", "message": "RESOURCE_EXHAUSTED"}})
        sys.exit(1)

    def signed_out(self, session_id):
        print("Please set an Auth method in your settings.json", file=sys.stderr)
        sys.exit(41)


def text_alex(stream, mcp):
    screen, _ = stream.tool(mcp, "screen", {"image": False})
    stream.reasoning("Open Messages and find Alex.")
    screen, _ = stream.tool(mcp, "open_app", {"app": "Messages"})
    screen, _ = stream.tool(mcp, "tap", {"element": element_id(screen, "Alex Rivera")})
    screen, _ = stream.tool(mcp, "type_text", {"element": element_id(screen, "iMessage"), "text": "Running late"})
    stream.message("I've written the message; sending it now.")
    screen, failed = stream.tool(mcp, "tap", {"element": element_id(screen, "Send"), "irreversible": True})
    if failed:
        return f"I didn't send it: {screen.splitlines()[0]}"
    return "Sent Alex “Running late” in Messages."


def return_then_send(stream, mcp):
    """What Claude Code did on the real plan: type with Return marked irreversible (the scripted Messages adds a line
    instead of sending), then tap Send."""
    screen, _ = stream.tool(mcp, "open_app", {"app": "Messages"})
    screen, _ = stream.tool(mcp, "tap", {"element": element_id(screen, "Alex Rivera")})
    screen, _ = stream.tool(mcp, "type_text", {"element": element_id(screen, "iMessage"), "text": "Running late",
                                                "submit": True, "irreversible": True})
    stream.message("Return didn't send it; tapping Send.")
    screen, failed = stream.tool(mcp, "tap", {"element": element_id(screen, "Send"), "irreversible": True})
    return "Sent." if not failed else screen.splitlines()[0]


def read_about(stream, mcp):
    screen, _ = stream.tool(mcp, "open_app", {"app": "Settings"})
    screen, _ = stream.tool(mcp, "tap", {"label": "General"})
    screen, _ = stream.tool(mcp, "tap", {"element": element_id(screen, "About")})
    return "This iPhone runs iOS 26.4 (iPhone 17 Pro)."


def open_things(stream, mcp):
    """Two links into an app Mobster's map doesn't know: the first asks, the second follows the answer."""
    first, failed = stream.tool(mcp, "open_url", {"url": "things:///add?title=Milk"})
    if failed:
        return f"I didn't open it: {first.splitlines()[0]}"
    second, failed = stream.tool(mcp, "open_url", {"url": "things:///show?id=today"})
    return "Added Milk to Things." if not failed else second.splitlines()[0]


def main(argv):
    which, args = argv[0], argv[1:]
    login = os.environ.get("FAKE_LOGIN", "plan")
    if args[:1] == ["--version"]:
        print({"codex": "codex-cli 0.162.0", "claude": "2.1.294 (Claude Code)", "gemini": "0.63.0"}[which])
        return 0
    if which == "codex" and args[:2] == ["login", "status"]:
        said = {"plan": "Logged in using ChatGPT", "api_key": "Logged in using an API key - sk-proj-***ABCD",
                "out": "Not logged in"}[login]
        print(said, file=sys.stderr)
        return 0 if login != "out" else 1
    if which == "claude" and args[:2] == ["auth", "status"]:
        if login == "out":
            print(json.dumps({"loggedIn": False}))
            return 1
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai" if login == "plan" else "api_key",
                          "subscriptionType": "max" if login == "plan" else None}))
        return 0
    if os.environ.get("FAKE_AGENT_LOG"):
        with open(os.environ["FAKE_AGENT_LOG"], "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"which": which, "argv": args, "cwd": os.getcwd(),
                                     "env": {k: True for k in os.environ if k.endswith(("API_KEY", "AUTH_TOKEN"))},
                                     "token": bool(os.environ.get("MOBSTER_MCP_TOKEN"))}) + "\n")
    global PROMPT
    PROMPT = sys.stdin.read() if not sys.stdin.isatty() else ""
    scenario = os.environ.get("FAKE_AGENT_SCENARIO", "text_alex")
    if scenario == "replay":
        with open(os.environ["FAKE_AGENT_REPLAY"], encoding="utf-8") as handle:
            for line in handle:
                print(line.rstrip("\n"), flush=True)
        return 0
    stream = {"codex": CodexStream, "claude": ClaudeStream, "gemini": GeminiStream}[which]()
    resumed = None
    if which == "codex":
        joined = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a and a.startswith("mcp_servers")}
        url = json.loads(joined["mcp_servers.mobster.url"])
        token = os.environ[json.loads(joined["mcp_servers.mobster.bearer_token_env_var"])]
        if args[:2] == ["exec", "resume"]:
            resumed = args[-2]
    elif which == "gemini":
        config = json.load(open(os.path.join(os.getcwd(), ".gemini", "settings.json"), encoding="utf-8"))
        server = config["mcpServers"]["mobster"]
        url, token = server["httpUrl"], server["headers"]["Authorization"].split(" ", 1)[1]
        assert server.get("trust") is True and "--approval-mode" not in args and "--yolo" not in args
    else:
        config = json.load(open(args[args.index("--mcp-config") + 1], encoding="utf-8"))["mcpServers"]["mobster"]
        url, token = config["url"], config["headers"]["Authorization"].split(" ", 1)[1]
        if "--resume" in args:
            resumed = args[args.index("--resume") + 1]
    session = resumed or {"codex": "0199a213-81c0-7800-8aa1-bbab2a035a53",
                          "claude": "6f9a2b1c-3d4e-4f50-8a6b-7c8d9e0f1a2b",
                          "gemini": "gem-session-1"}[which]
    stream.session(session)
    if scenario == "limit":
        stream.limit(session)
    if scenario == "signed_out":
        stream.signed_out(session)
    mcp = MCP(url, token)
    if scenario == "hang":
        stream.message("Looking at the screen.")  # a response of its own: its usage is final once the next one starts
        stream.tool(mcp, "screen", {"image": False})
        time.sleep(120)
        return 0
    if resumed:
        prompt = PROMPT
        stream.tool(mcp, "screen", {"image": False})
        stream.end(f"Resumed {resumed[:8]}: {prompt[:40]}", session)
        return 0
    final = {"text_alex": text_alex, "read_about": read_about, "return_then_send": return_then_send,
             "open_things": open_things}[scenario](stream, mcp)
    stream.end(final, session)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
