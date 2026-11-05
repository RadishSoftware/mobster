import contextlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent import permissions
from mobile_agent.ai_plans import agents, cli, mcp_http
from mobile_agent.ai_plans.claude_code import ClaudeCode
from mobile_agent.ai_plans.codex import Codex
from mobile_agent.ai_plans.gemini import Gemini
from mobile_agent.tests import sota_world as world
from mobile_agent.tests.test_sota_e2e import Service

FAKE = Path(__file__).with_name("fake_agent_cli.py")
FIXTURES = Path(__file__).with_name("fixtures") / "ai_plans"


def fake_bin(folder, which):
    path = Path(folder) / which
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" {which} "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def lines(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mobster-ai-plans-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shell = patch.object(cli, "login_shell_path", lambda env=None, refresh=False: "")
        shell.start()
        self.addCleanup(shell.stop)
        agents.reset_for_tests()
        self.addCleanup(agents.reset_for_tests)


class CodexStreamTests(unittest.TestCase):
    def test_a_recorded_stream_reads_as_the_session_tools_message_and_usage(self):
        events = [e for obj in lines("codex-exec.jsonl") for e in Codex({}).parse(obj)]
        kinds = [e.kind for e in events]
        self.assertEqual(events[0].kind, "session")
        self.assertTrue(events[0].session_id)
        self.assertIn("tool_start", kinds)
        self.assertIn("tool_end", kinds)
        tools = [e.tool for e in events if e.kind == "tool_start"]
        self.assertTrue(set(tools) <= {"screen", "act", "tap", "type_text", "swipe", "open_app", "read_list",
                                       "scroll_to",
                                       "home", "long_press", "dismiss", "open_url", "ask_user", "wait"}, tools)
        self.assertTrue(all(e.server == "mobster" for e in events if e.kind in ("tool_start", "tool_end")))
        message = [e.text for e in events if e.kind == "message"]
        self.assertTrue(message and message[-1].strip())
        usage = next(e.usage for e in events if e.kind == "usage")
        self.assertGreater(usage["input_tokens"], 0)
        self.assertEqual(kinds[-1], "usage")

    def test_a_usage_limit_is_a_fatal_plan_limit_with_its_reset(self):
        now = time.time()
        event = Codex({}).parse({"type": "turn.failed", "error": {
            "message": "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), or try "
                       "again in 2 hours 5 minutes."}})[0]
        self.assertEqual((event.kind, event.code, event.fatal), ("error", "usage_limit", True))
        self.assertAlmostEqual(event.resets_at - now, 2 * 3600 + 5 * 60, delta=5)

    def test_an_api_error_passed_on_as_json_reads_as_its_message(self):
        event = Codex({}).parse({"type": "turn.failed", "error": {"message": json.dumps({
            "type": "error", "status": 400, "error": {"type": "invalid_request_error", "message":
                "The 'gpt-nope' model is not supported when using Codex with a ChatGPT account."}})}})[0]
        self.assertEqual(event.code, "model")
        self.assertTrue(event.text.startswith("The 'gpt-nope' model is not supported"))

    def test_a_failed_tool_call_says_why(self):
        event = Codex({}).parse({"type": "item.completed", "item": {
            "id": "item_3", "type": "mcp_tool_call", "server": "mobster", "tool": "tap", "arguments": {},
            "status": "failed", "error": {"message": "e9 isn't on the latest screen."}}})[0]
        self.assertEqual((event.kind, event.ok, event.text), ("tool_end", False, "e9 isn't on the latest screen."))

    def test_turn_usage_keeps_codexs_input_as_it_is_and_names_the_cache(self):
        events = [e for obj in lines("codex-token-count.jsonl") for e in Codex({}).parse(obj)]
        usage = next(e.usage for e in events if e.kind == "usage")
        self.assertEqual(usage, {"input_tokens": 17272, "output_tokens": 205, "total_tokens": 17477,
                                 "cache_read_tokens": 9000, "cache_write_tokens": 0, "reasoning_output_tokens": 40})

    def test_token_count_gives_a_step_and_the_plans_windows_in_either_shape(self):
        events = [e for obj in lines("codex-token-count.jsonl") for e in Codex({}).parse(obj)]
        steps = [e for e in events if e.kind == "step_usage"]
        limits = [e.limits for e in events if e.kind == "limits"]
        self.assertEqual(len(steps), 2)  # the plain shape and the {"msg": ...} one
        self.assertEqual((steps[0].usage["input_tokens"], steps[0].usage["cache_read_tokens"]), (17272, 9000))
        self.assertEqual(len(limits), 2)
        self.assertEqual(limits[0], {"status": "allowed", "type": "five_hour", "resetsAt": 1792030800.0, "windows": {
            "five_hour": {"utilization": 0.37, "resetsAt": 1792030800.0},
            "seven_day": {"utilization": 0.1, "resetsAt": 1792447200.0}}})

class ClaudeStreamTests(unittest.TestCase):
    def test_a_recorded_stream_reads_as_the_session_tools_message_and_result(self):
        events = [e for obj in lines("claude-stream.jsonl") for e in ClaudeCode({}).parse(obj)]
        kinds = [e.kind for e in events]
        self.assertEqual(kinds[0], "session")
        self.assertIn(("mcp", True), [(e.kind, e.ok) for e in events if e.kind == "mcp"])
        starts = [e for e in events if e.kind == "tool_start"]
        self.assertTrue(starts and all(e.server == "mobster" for e in starts))
        done = events[-1]
        self.assertEqual((done.kind, done.ok), ("done", True))
        self.assertTrue(done.text)

    def test_the_usage_limit_with_its_epoch_is_a_plan_limit(self):
        parsed = ClaudeCode({}).parse({"type": "result", "subtype": "success", "is_error": True,
                                       "result": "Claude AI usage limit reached|1999999999", "session_id": "s"})
        error = next(e for e in parsed if e.kind == "error")
        self.assertEqual((error.code, error.resets_at, error.fatal), ("usage_limit", 1999999999.0, True))
        self.assertFalse(parsed[-1].ok)

    def test_a_rejected_rate_limit_event_is_a_plan_limit(self):
        parsed = ClaudeCode({}).parse({"type": "rate_limit_event",
                                       "rate_limit_info": {"status": "rejected", "resetsAt": 1999999999}})
        self.assertEqual([e.kind for e in parsed], ["limits", "error"])
        error = parsed[1]
        self.assertEqual((error.code, error.resets_at), ("usage_limit", 1999999999.0))
        self.assertEqual(parsed[0].limits["status"], "rejected")

    def test_claudes_input_counts_the_cache_and_a_message_id_shared_by_two_events_is_one_call(self):
        events = [e for obj in lines("claude-stream.jsonl") for e in ClaudeCode({}).parse(obj)]
        steps = [e for e in events if e.kind == "step_usage"]
        self.assertEqual([e.call_id for e in steps], ["msg_01", "msg_01", "msg_02", "msg_03", "msg_04"])
        self.assertEqual(steps[0].model, "claude-sonnet-5-5")
        # input_tokens 3 + cache read 11500 (+ no cache write)
        self.assertEqual((steps[0].usage["input_tokens"], steps[0].usage["cache_read_tokens"],
                          steps[0].usage["total_tokens"]), (11503, 11500, 11503 + 12))
        usage = next(e for e in events if e.kind == "usage")
        self.assertEqual(usage.usage, {"input_tokens": 12 + 47650 + 2400, "output_tokens": 156, "total_tokens": 50218,
                                       "cache_read_tokens": 47650, "cache_write_tokens": 2400})
        self.assertEqual((usage.cost_usd, usage.latency_ms), (0.0219, 7410))
        self.assertEqual(usage.models["claude-sonnet-5-5"]["usage"]["input_tokens"], 50062)
        self.assertEqual(usage.models["claude-sonnet-5-5"]["cost_usd"], 0.0219)

class StatusTests(Isolated):
    def test_codex_signed_in_with_chatgpt_is_ready_on_the_plan(self):
        path = fake_bin(self.tmp, "codex")
        with patch.dict(os.environ, {"FAKE_LOGIN": "plan"}):
            state = Codex({**os.environ, "MOBSTER_CODEX_PATH": str(path)}).status(max_age=0)
        self.assertEqual((state["installed"], state["signedIn"], state["ready"], state["billing"]),
                         (True, True, True, "plan"))
        self.assertEqual(state["version"], "0.162.0")

    def test_signed_out_and_missing(self):
        path = fake_bin(self.tmp, "claude")
        with patch.dict(os.environ, {"FAKE_LOGIN": "out"}):
            state = ClaudeCode({**os.environ, "MOBSTER_CLAUDE_CODE_PATH": str(path)}).status(max_age=0)
        self.assertEqual((state["installed"], state["ready"], state["hint"]), (True, False, "Sign in to Claude Code"))
        missing = ClaudeCode({"PATH": str(self.tmp / "nowhere"), "HOME": str(self.tmp)})
        with patch.object(cli, "SEARCH_DIRS", ()), patch.object(cli, "_nvm_dirs", lambda: []):
            state = missing.status(max_age=0)
        self.assertEqual((state["installed"], state["ready"], state["hint"]), (False, False, "Install Claude Code"))

    def test_claude_on_its_plan_names_the_tier(self):
        path = fake_bin(self.tmp, "claude")
        with patch.dict(os.environ, {"FAKE_LOGIN": "plan"}):
            state = ClaudeCode({**os.environ, "MOBSTER_CLAUDE_CODE_PATH": str(path)}).status(max_age=0)
        self.assertEqual((state["ready"], state["billing"], state["account"]), (True, "plan", "Claude Max"))

    def test_the_child_never_sees_an_api_key_that_would_bill_instead_of_the_plan(self):
        path = fake_bin(self.tmp, "codex")
        env = cli.child_env(str(path), extra={"MOBSTER_MCP_TOKEN": "t"}, drop=Codex.drop_env,
                            env={"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-x", "HOME": str(self.tmp)})
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["MOBSTER_MCP_TOKEN"], "t")
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(path.resolve().parent))
        env = cli.child_env(str(path), drop=ClaudeCode.drop_env,
                            env={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant", "CLAUDECODE": "1"})
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDECODE", env)


class GeminiStreamTests(unittest.TestCase):
    def test_pieces_of_text_are_said_whole_and_the_result_is_the_answer(self):
        parse = Gemini({}).parser()
        events = []
        for obj in ({"type": "init", "session_id": "s1", "model": "gemini-3.5-pro"},
                    {"type": "message", "role": "assistant", "content": "Opening ", "delta": True},
                    {"type": "message", "role": "assistant", "content": "Settings.", "delta": True},
                    {"type": "tool_use", "tool_name": "mcp_mobster_open_app", "tool_id": "t1",
                     "parameters": {"app": "Settings"}},
                    {"type": "tool_result", "tool_id": "t1", "status": "success", "output": "ok"},
                    {"type": "message", "role": "assistant", "content": "It runs iOS 26.4.", "delta": True},
                    {"type": "result", "status": "success", "stats": {"input_tokens": 10, "output_tokens": 2}}):
            events += parse(obj)
        self.assertEqual([(e.kind, e.text) for e in events if e.kind == "message"],
                         [("message", "Opening Settings."), ("message", "It runs iOS 26.4.")])
        start = next(e for e in events if e.kind == "tool_start")
        self.assertEqual((start.server, start.tool), ("mobster", "open_app"))
        self.assertEqual((events[-1].kind, events[-1].text), ("done", "It runs iOS 26.4."))

    def test_the_run_settings_trust_only_mobster_and_drop_the_built_ins(self):
        settings = json.loads(Gemini({}).workspace_settings("http://127.0.0.1:9/mcp/runs/a", "tok"))
        self.assertEqual(list(settings["mcpServers"]), ["mobster"])
        self.assertIn("run_shell_command", settings["tools"]["exclude"])
        argv = Gemini({}).argv("/x/gemini", "t", mcp_url="u", token_env="T", instructions="i", workdir="/w").argv
        self.assertFalse({"--yolo", "--approval-mode"} & set(argv))

class CommandLineTests(unittest.TestCase):
    def test_codex_runs_headless_on_mobsters_mcp_server_only(self):
        argv = Codex({}).argv("/x/codex", "Task: hi", mcp_url="http://127.0.0.1:9/mcp/runs/abc", token_env="TOK",
                              instructions='Say "hi"\nthen stop', workdir="/w").argv
        joined = " ".join(argv)
        self.assertEqual(argv[:2], ["/x/codex", "exec"])
        for flag in ("--json", "--skip-git-repo-check", "--ignore-user-config"):
            self.assertIn(flag, argv)
        self.assertIn('mcp_servers.mobster.url="http://127.0.0.1:9/mcp/runs/abc"', argv)
        self.assertIn('mcp_servers.mobster.bearer_token_env_var="TOK"', argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertIn("features.shell_tool=false", argv)
        self.assertIn('developer_instructions="Say \\"hi\\"\\nthen stop"', argv)
        self.assertIn('mcp_servers.mobster.default_tools_approval_mode="approve"', argv)
        self.assertIn("mcp_servers.mobster.required=true", argv)
        self.assertIn('mcp_servers.mobster.omit_tools_from=["deferred","code_mode"]', argv)
        # The task goes on stdin, never into the process list.
        self.assertEqual(argv[-1], "-")
        self.assertNotIn("Task: hi", joined)
        self.assertEqual(Codex({}).stdin("Task: hi"), "Task: hi")
        self.assertNotIn("resume", argv)
        self.assertIn("--cd /w", joined)
        resumed = Codex({}).argv("/x/codex", "and then?", mcp_url="u", token_env="T", instructions="i", workdir="/w",
                                 resume="0199-abc").argv
        self.assertEqual(resumed[:3], ["/x/codex", "exec", "resume"])
        self.assertEqual(resumed[-2:], ["0199-abc", "-"])

    def test_claude_code_gets_no_built_in_tools_and_only_the_phone(self):
        agent = ClaudeCode({})
        argv = agent.argv("/x/claude", "Task: hi", mcp_url="u", token_env="T", instructions="I", workdir="/w",
                          config_path="/p/mcp.json", resume="sess-1").argv
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--mcp-config") + 1], "/p/mcp.json")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__mobster__*")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-1")
        self.assertNotIn("Task: hi", argv)
        self.assertEqual(agent.stdin("Task: hi"), "Task: hi")
        config = json.loads(agent.mcp_config("http://127.0.0.1:9/mcp/runs/abc", "tok"))
        self.assertEqual(config["mcpServers"]["mobster"], {"type": "http", "url": "http://127.0.0.1:9/mcp/runs/abc",
                                                           "headers": {"Authorization": "Bearer tok"},
                                                           "alwaysLoad": True, "timeout": 900000})

class FakeTools:
    def __init__(self):
        self.calls = []

    @staticmethod
    def definitions():
        return [{"name": "screen", "description": "d", "inputSchema": {"type": "object", "properties": {
            "image": {"type": "boolean"}}, "additionalProperties": False}}]

    def call(self, name, arguments):
        from mobile_agent.mcp_server.protocol import ToolResult
        self.calls.append((name, arguments))
        return ToolResult("Screen: Messages", {"app": "Messages"})


class McpHttpTests(unittest.TestCase):
    def setUp(self):
        self.tools = FakeTools()
        self.endpoint = mcp_http.register("run1", self.tools, "be careful", version="9")
        self.addCleanup(mcp_http.unregister, "run1")

    def post(self, body, token=None, method="POST", path="/mcp/runs/run1"):
        headers = {"Authorization": f"Bearer {token or self.endpoint.token}"}
        raw = json.dumps(body).encode()
        return mcp_http.respond(method, path, headers, lambda limit: raw)

    def test_initialize_list_and_call(self):
        status, headers, body = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                           "params": {"protocolVersion": "2025-06-18",
                                                      "clientInfo": {"name": "codex"}}})
        answer = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("Mcp-Session-Id", headers)
        self.assertEqual(answer["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(answer["result"]["instructions"], "be careful")
        self.assertEqual(self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        listed = json.loads(self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[2])
        self.assertEqual([t["name"] for t in listed["result"]["tools"]], ["screen"])
        called = json.loads(self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                       "params": {"name": "screen", "arguments": {"image": False}}})[2])
        self.assertEqual(called["result"]["content"][0]["text"], "Screen: Messages")
        self.assertNotIn("structuredContent", called["result"])
        self.assertEqual(self.tools.calls, [("screen", {"image": False})])

    def test_arguments_that_break_the_schema_are_a_tool_error_the_agent_can_read(self):
        called = json.loads(self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                       "params": {"name": "screen", "arguments": {"bogus": 1}}})[2])
        self.assertTrue(called["result"]["isError"])
        self.assertEqual(self.tools.calls, [])

    def test_another_runs_token_or_none_is_refused(self):
        self.assertEqual(self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="wrong")[0], 401)
        other = mcp_http.register("run2", FakeTools())
        self.addCleanup(mcp_http.unregister, "run2")
        self.assertEqual(self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=other.token)[0], 401)
        self.assertEqual(self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, path="/mcp/runs/gone")[0], 404)

    def test_get_is_not_offered_and_a_finished_run_is_gone(self):
        self.assertEqual(self.post(None, method="GET")[0], 405)
        mcp_http.unregister("run1")
        self.assertEqual(self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})[0], 404)

@contextlib.contextmanager
def commit_test(**behaviour):
    """The loop's commit test (frontier.safety.consequential) under every name a caller reads it by."""
    with patch("mobile_agent.frontier.safety.consequential", **behaviour), \
            patch("mobile_agent.frontier.stages.after.consequential", **behaviour):
        yield


class PhoneToolsTests(unittest.TestCase):

    def setUp(self):
        from mobile_agent import engines
        from mobile_agent.ai_plans.phone_tools import PhoneTools
        kwargs = engines.frontier_kwargs
        patcher = patch.object(engines, "frontier_kwargs", lambda *a, **kw: {**kwargs(*a, **kw), "settle_seconds": 0})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = world.new_phone_state()
        self.events, self.asked = [], []
        self.answer = "approved"

        def approve(request):
            self.asked.append(request)
            return self.answer
        phone = world.ScriptedPhone(self.state)
        phone.long_press = lambda element, snapshot, timeout=10: phone.execute("LONG_PRESS", element, snapshot)
        self.tools = PhoneTools(phone, apps=dict(world.APP_NAMES),
                                goal="Text Alex that I'm running late", emit=self.events.append, approve=approve,
                                ask=lambda request: (self.asked.append(request), "answer:the blue one")[1],
                                permissions=permissions.PermissionGate(permissions.ASK_IMPORTANT,
                                                                       goal="Text Alex that I'm running late"))

    def call(self, name, **arguments):
        result = self.tools.call(name, arguments)
        return result.text, result.is_error

    def test_the_screen_lists_elements_by_id(self):
        text, failed = self.call("screen", image=False)
        self.assertFalse(failed)
        self.assertIn("Screen: Home Screen", text)
        self.assertRegex(text, r'e1 Icon "Messages"')

    def test_a_send_waits_for_the_persons_ok_and_each_action_is_a_step(self):
        text, _ = self.call("open_app", app="Messages")
        self.assertIn("Opened Messages", text)
        text, _ = self.call("tap", label="Alex Rivera")
        text, _ = self.call("type_text", label="iMessage", text="Running late")
        self.assertEqual(self.asked, [])
        text, failed = self.call("tap", label="Send")
        self.assertFalse(failed, text)
        self.assertEqual([r["title"] for r in self.asked], ["Send this message to Alex Rivera?"])
        self.assertEqual(self.asked[0]["text"], "Running late")
        self.assertEqual(self.state["sent"], {"Alex Rivera": ["Running late"]})
        steps = [(e["text"], e.get("commit"), e.get("changed")) for e in self.events if e["event"] == "step"]
        self.assertEqual([step[:2] for step in steps], [("Opened Messages", None), ("Tapped Alex Rivera", False),
                                                        ("Wrote the message", None), ("Tapped Send", True)])
        self.assertTrue(all(isinstance(step[2], bool) for step in steps), steps)

    def test_a_commit_test_that_fails_after_a_tap_that_worked_lets_the_run_go_on(self):
        self.call("open_app", app="Messages")
        with commit_test(side_effect=AttributeError("rect")):
            text, failed = self.call("tap", label="Alex Rivera")
        self.assertFalse(failed, text)
        self.assertIsNone(self.tools.stopped)
        text, failed = self.call("type_text", label="iMessage", text="Running late")
        self.assertFalse(failed, text)

    def test_a_tap_counts_as_a_commit_when_the_commit_test_says_so_or_fails(self):
        self.call("open_app", app="Messages")
        with commit_test(return_value=True):
            self.call("tap", label="Alex Rivera")
        self.call("open_app", app="Messages")
        with commit_test(side_effect=AttributeError("rect")):
            self.call("tap", label="Alex Rivera")
        taps = [e.get("commit") for e in self.events if e["event"] == "step" and e["text"] == "Tapped Alex Rivera"]
        self.assertEqual(taps, [True, True])

    def test_a_declined_send_isnt_sent_and_the_agent_is_told_not_to_try_again(self):
        self.answer = "denied"
        self.call("open_app", app="Messages")
        self.call("tap", label="Alex Rivera")
        self.call("type_text", label="iMessage", text="Running late")
        text, failed = self.call("tap", label="Send")
        self.assertTrue(failed)
        self.assertIn("declined", text)
        self.assertIn("Don't try another way", text)
        self.assertEqual(self.state["sent"], {})

    def test_act_chains_a_send_in_one_call_and_still_asks(self):
        text, failed = self.call("act", steps=[{"tool": "open_app", "app": "Messages"},
                                               {"tool": "tap", "label": "Alex Rivera"},
                                               {"tool": "type_text", "label": "iMessage", "text": "Running late"},
                                               {"tool": "tap", "label": "Send", "irreversible": True}])
        self.assertFalse(failed, text)
        self.assertTrue(text.startswith("Done: Opened Messages. Tapped Alex Rivera. Wrote the message. Tapped Send."),
                        text)
        self.assertEqual([r["title"] for r in self.asked], ["Send this message to Alex Rivera?"])
        self.assertEqual(self.state["sent"], {"Alex Rivera": ["Running late"]})

    def test_act_stops_at_the_first_step_that_fails_and_says_what_was_done(self):
        text, failed = self.call("act", steps=[{"tool": "open_app", "app": "Messages"},
                                               {"tool": "tap", "label": "Nobody"},
                                               {"tool": "tap", "label": "Send"}])
        self.assertTrue(failed)
        self.assertTrue(text.startswith("Done first: Opened Messages."), text)
        self.assertIn("Step 2 didn't work", text)
        self.assertEqual(self.asked, [])

    def test_act_stops_after_a_step_that_shows_no_effect_and_says_what_didnt_run(self):
        self.call("open_app", app="Messages")
        with patch.object(self.tools.agent, "_settle", lambda before, **kw: before):
            text, failed = self.call("act", steps=[{"tool": "tap", "label": "Alex Rivera"},
                                                   {"tool": "tap", "label": "Sam Okafor"},
                                                   {"tool": "home"}])
        self.assertFalse(failed, text)
        self.assertIn("The screen didn't change.", text)
        self.assertIn("Step 1 showed no effect (the screen didn't change), so these didn't run: "
                      'tap "Sam Okafor", home.', text)
        self.assertEqual([x for x in self.state["log"] if x[0] == "TAP"][-1][1], "Alex Rivera")
        self.assertNotIn(("HOME", "", None), self.state["log"][-2:])

    def test_act_still_advances_when_each_step_moved_the_screen(self):
        text, failed = self.call("act", steps=[{"tool": "open_app", "app": "Messages"},
                                               {"tool": "tap", "label": "Alex Rivera"},
                                               {"tool": "home"}])
        self.assertFalse(failed, text)
        self.assertNotIn("didn't run", text)
        self.assertTrue(text.startswith("Done: Opened Messages. Tapped Alex Rivera."), text)
        self.assertEqual(self.state["app"], world.HOME)

    def test_act_advances_past_an_app_already_in_front_and_a_wait(self):
        """Neither moves the screen, and neither failed: the app is where it was asked to be."""
        self.call("open_app", app="Messages")
        text, failed = self.call("act", steps=[{"tool": "open_app", "app": "Messages"}, {"tool": "wait", "seconds": .5},
                                               {"tool": "tap", "label": "Alex Rivera"}])
        self.assertFalse(failed, text)
        self.assertNotIn("didn't run", text)
        self.assertEqual([x for x in self.state["log"] if x[0] == "TAP"][-1][1], "Alex Rivera")

    def test_act_stops_when_the_app_didnt_come_to_the_front(self):
        self.call("home")
        agent = self.tools.agent
        with patch.object(agent, "_check_app", lambda after: None), \
                patch.object(agent, "_execute", lambda *a, **k: self.tools.snapshot):
            text, failed = self.call("act", steps=[{"tool": "open_app", "app": "Messages"}, {"tool": "home"}])
        self.assertFalse(failed, text)
        self.assertIn('(the app isn\'t in front), so these didn\'t run: home.', text)

    def test_a_write_whose_field_reads_back_is_an_effect_even_on_a_screen_that_looks_the_same(self):
        self.call("open_app", app="Messages")
        self.call("tap", label="Alex Rivera")
        tools, agent = self.tools, self.tools.agent

        def execute(*args, **kwargs):
            agent.run_state.last_receipt = {"value": "x", "matches": True}
            return tools.snapshot
        with patch.object(agent, "_settle", lambda before, **kw: before), patch.object(agent, "_execute", execute):
            text, failed = self.call("act", steps=[{"tool": "type_text", "label": "iMessage", "text": "x"},
                                                   {"tool": "wait", "seconds": .5}])
        self.assertFalse(failed, text)
        self.assertNotIn("didn't run", text)

    def test_a_write_that_reads_back_wrong_stops_the_chain_even_on_a_moved_screen(self):
        self.call("open_app", app="Messages")
        self.call("tap", label="Alex Rivera")
        agent, real = self.tools.agent, self.tools.agent._execute

        def execute(*args, **kwargs):
            before = real(*args, **kwargs)
            agent.run_state.last_receipt = {"value": "not it", "matches": False}
            return before
        with patch.object(agent, "_execute", execute):
            text, failed = self.call("act", steps=[{"tool": "type_text", "label": "iMessage", "text": "x"},
                                                   {"tool": "wait", "seconds": .5}])
        self.assertFalse(failed, text)
        self.assertIn('(the field doesn\'t hold what was written), so these didn\'t run: wait.', text)

    def test_the_last_step_showing_no_effect_is_only_said_once(self):
        self.call("open_app", app="Messages")
        with patch.object(self.tools.agent, "_settle", lambda before, **kw: before):
            text, failed = self.call("act", steps=[{"tool": "tap", "label": "Alex Rivera"}])
        self.assertFalse(failed, text)
        self.assertIn("The screen didn't change.", text)
        self.assertNotIn("didn't run", text)

    def test_a_tap_after_a_write_is_judged_by_itself_and_not_by_the_writes_receipt(self):
        """The write before it read back, so its receipt matches. The tap that follows changes nothing, and the chain
        stops there: each action starts with no receipt."""
        self.call("open_app", app="Messages")
        self.call("tap", label="Alex Rivera")
        agent, real = self.tools.agent, self.tools.agent._execute

        def execute(op, *args, **kwargs):
            before = real(op, *args, **kwargs)
            if op in ("TYPE", "SET_TEXT"):
                agent.run_state.last_receipt = {"value": "x", "matches": True}
            return before
        settle = agent._settle
        with patch.object(agent, "_execute", execute), \
                patch.object(agent, "_settle", lambda before, **kw: before if self.state["log"][-1][0] == "TAP"
                             else settle(before, **kw)):
            text, failed = self.call("act", steps=[{"tool": "type_text", "label": "iMessage", "text": "x"},
                                                   {"tool": "tap", "label": "Alex Rivera"},
                                                   {"tool": "home"}])
        self.assertFalse(failed, text)
        self.assertIn('Step 2 showed no effect (the screen didn\'t change), so these didn\'t run: home.', text)
        self.assertNotEqual(self.state["log"][-1][0], "HOME")

    def test_home_is_an_effect_when_the_home_screen_shows_and_none_when_it_doesnt(self):
        text, failed = self.call("act", steps=[{"tool": "open_app", "app": "Messages"}, {"tool": "home"},
                                               {"tool": "open_app", "app": "Notes"}])
        self.assertFalse(failed, text)
        self.assertNotIn("didn't run", text)
        self.assertEqual(self.state["app"], world.NOTES)
        self.call("open_app", app="Messages")
        with patch.object(self.tools.agent, "_execute", lambda *a, **k: self.tools.snapshot), \
                patch.object(self.tools.agent, "_check_app", lambda after: None):
            text, failed = self.call("act", steps=[{"tool": "home"}, {"tool": "open_app", "app": "Notes"}])
        self.assertIn('(the Home Screen isn\'t showing), so these didn\'t run: open_app "Notes".', text)

    def test_a_scroll_counts_only_when_its_item_is_on_screen(self):
        tools = self.tools
        shown = types.SimpleNamespace(text="Groceries\nTrip ideas", bundle_id=world.NOTES)
        self.assertIsNone(tools._no_effect("SCROLL_TO", None, "trip ideas", shown, False))
        self.assertEqual(tools._no_effect("SCROLL_TO", None, "Packing list", shown, True), "the item isn't on screen")

    def test_a_step_is_named_by_its_tool_and_target_and_never_by_what_it_types(self):
        from mobile_agent.ai_plans.phone_tools import step_name
        self.assertEqual(step_name({"tool": "tap", "label": "Send"}), 'tap "Send"')
        self.assertEqual(step_name({"tool": "type_text", "label": "iMessage", "text": "my secret"}),
                         'type_text "iMessage"')
        self.assertEqual(step_name({"tool": "scroll_to", "text": "Trip ideas"}), 'scroll_to "Trip ideas"')
        self.assertEqual(step_name({"tool": "home"}), "home")
        self.assertEqual(step_name({"tool": "swipe", "direction": "up"}), 'swipe "up"')
        for malformed in ("tap Send", 3, ["tap"], None, {"label": "Send"}, {"tool": 7}):
            self.assertEqual(step_name(malformed), "a step with no tool")

    def test_a_malformed_step_after_one_that_shows_no_effect_is_named_not_raised(self):
        """Steps after the stop are never run or checked, so they can be anything; naming them can't fail after the
        first step was sent."""
        self.call("open_app", app="Messages")
        with patch.object(self.tools.agent, "_execute", lambda *a, **k: self.tools.snapshot), \
                patch.object(self.tools.agent, "_check_app", lambda after: None):
            text, failed = self.call("act", steps=[{"tool": "home"}, "tap Send", {"label": "Send"}])
        self.assertFalse(failed, text)
        self.assertIn("so these didn't run: a step with no tool, a step with no tool.", text)

    def test_no_effect_never_reaches_a_client(self):
        from mobile_agent.mcp_server.protocol import ToolResult
        self.assertNotIn("screen didn", json.dumps(ToolResult("x", {"a": 1}, no_effect="the screen didn't change")
                                                   .payload(True)))

    def test_irreversible_asks_even_where_mobster_sees_no_commit(self):
        self.call("open_app", app="Settings")
        self.call("tap", label="General", irreversible=True)
        self.assertEqual(len(self.asked), 1)

    def test_ask_user_returns_the_answer(self):
        text, failed = self.call("ask_user", question="Which one?", choices=["the red one", "the blue one"])
        self.assertEqual((text, failed), ("They answered: the blue one", False))
        self.assertEqual(self.asked[-1]["kind"], "clarify")

    def read_about(self):
        self.call("open_app", app="Settings")
        self.call("tap", label="General")
        text, failed = self.call("tap", label="About")
        self.assertFalse(failed, text)

    def test_a_read_only_answer_is_quoted_from_the_screen_that_shows_it(self):
        self.read_about()
        self.assertEqual(self.tools.proof("This iPhone is on iOS 26.4."),
                         [{"quote": "26.4", "app": "Settings", "screen": "About", "step": 3}])
        self.assertEqual(self.tools.proof('It\'s an "iPhone 17 Pro".'),
                         [{"quote": "iPhone 17 Pro", "app": "Settings", "screen": "About", "step": 3}])

    def test_an_answer_no_screen_showed_has_no_proof(self):
        self.read_about()
        self.assertEqual(self.tools.proof("This iPhone is on iOS 18.3.1."), [])
        self.assertEqual(self.tools.proof("It's on the latest version."), [])  # no fact a screen could show
        self.assertEqual(self.tools.proof("iOS 26"), [])  # 26 alone is not 26.4

    def test_a_run_that_typed_or_committed_has_no_proof_however_its_answer_reads(self):
        self.read_about()
        self.call("open_app", app="Messages")
        self.call("tap", label="Alex Rivera")
        self.call("type_text", label="iMessage", text="On 26.4")
        self.assertEqual(self.tools.proof("This iPhone is on iOS 26.4."), [])

    def test_stop_ends_every_tool(self):
        self.tools.cancelled = lambda: True
        text, failed = self.call("screen")
        self.assertTrue(failed)
        self.assertIn("stopped", text)

    def in_a_thread_with_a_draft(self):
        self.call("open_app", app="Messages")
        self.call("tap", label="Alex Rivera")
        self.call("type_text", label="iMessage", text="Running late")

    def test_a_long_press_that_commits_asks_first(self):
        self.in_a_thread_with_a_draft()
        self.answer = "denied"
        text, failed = self.call("long_press", label="Send")
        self.assertTrue(failed)
        self.assertIn("declined", text)
        self.assertEqual([(r["operation"], r["title"]) for r in self.asked],
                         [("LONG_PRESS", "Send this message to Alex Rivera?")])
        self.assertNotIn(("LONG_PRESS", "Send", None), self.state["log"])

    def test_a_long_press_on_a_row_still_acts_without_asking(self):
        self.call("open_app", app="Messages")
        text, failed = self.call("long_press", label="Alex Rivera")
        self.assertFalse(failed, text)
        self.assertEqual(self.asked, [])
        self.assertIn(("LONG_PRESS", "Alex Rivera", None), self.state["log"])

    def test_a_long_press_on_the_passcode_keypad_is_refused(self):
        from mobile_agent.state import Snapshot
        from mobile_agent.task_policy import SPRINGBOARD_BUNDLE
        keys = [world.el(i, str(i), "Button", (.02 + .09 * i, .5, .08, .06)) for i in range(10)]
        keys.append(world.el(10, "Enter Passcode", "StaticText", (.3, .3, .4, .04)))
        keypad = Snapshot(keys, "Enter Passcode", 390, 844, world.SOURCE, bundle_id=SPRINGBOARD_BUNDLE, revision="lock")
        with patch.object(self.tools.driver, "observe", lambda timeout=10: keypad):
            text, failed = self.call("long_press", label="5")
        self.assertTrue(failed)
        self.assertIn("Mobster refused this", text)
        self.assertEqual([entry for entry in self.state["log"] if entry[0] == "LONG_PRESS"], [])

    def test_a_link_that_acts_waits_for_the_card(self):
        opened = []
        self.tools.driver.open_url = opened.append
        self.answer = "denied"
        text, failed = self.call("open_url", url="tel:+15555550123")
        self.assertTrue(failed)
        self.assertEqual([(r["operation"], r["title"], r["text"]) for r in self.asked],
                         [("OPEN_URL", "Call +15555550123?", "tel:+15555550123")])
        self.assertEqual(opened, [])
        self.answer = "approved"
        text, failed = self.call("open_url", url="shortcuts://run-shortcut?name=Morning")
        self.assertFalse(failed, text)
        self.assertEqual(self.asked[-1]["title"], "Run the shortcut in shortcuts://run-shortcut?name=Morning?")
        self.assertEqual(opened, ["shortcuts://run-shortcut?name=Morning"])

    def test_a_link_asks_even_with_ask_before_acting_off_and_with_nobody_to_ask_isnt_opened(self):
        opened = []
        self.tools.driver.open_url = opened.append
        self.tools.approve = None  # Ask before acting is off: questions still reach the person
        self.call("open_url", url="sms:+15555550123&body=hi")
        self.assertEqual([r["operation"] for r in self.asked], ["OPEN_URL"])
        self.tools.ask = None
        text, failed = self.call("open_url", url="mailto:sam@example.com")
        self.assertTrue(failed)
        self.assertIn("didn't open it", text)
        self.assertEqual(opened, [])

    def test_a_web_page_still_opens_without_asking(self):
        opened = []
        self.tools.driver.open_url = opened.append
        text, failed = self.call("open_url", url="https://www.example.com/hours?token=abc")
        self.assertFalse(failed, text)
        self.assertEqual((opened, self.asked), (["https://www.example.com/hours?token=abc"], []))
        self.assertEqual([e["text"] for e in self.events if e["event"] == "step"], ["Opened example.com"])
        # A sign-in inside the address isn't a plain web page: it goes to the card.
        self.answer = "denied"
        self.call("open_url", url="https://user:secret@example.com/")
        self.assertEqual(opened, ["https://www.example.com/hours?token=abc"])
        self.assertEqual(len(self.asked), 1)

    # -- the run's permission gate decides, as it does for Mobster's agent: each "now acts" beside its "still asks" --

    def gated(self, gate, setting_on):
        self.tools.permissions = gate
        if not setting_on:
            self.tools.approve = None
        self.tools.ask = lambda request: (self.asked.append(request), self.answer)[1]

    def test_with_the_gate_full_access_lets_a_send_go(self):
        self.gated(permissions.PermissionGate(permissions.DONT_ASK), setting_on=False)
        self.in_a_thread_with_a_draft()
        text, failed = self.call("tap", label="Send")
        self.assertFalse(failed, text)
        self.assertEqual((self.asked, self.state["sent"]), ([], {"Alex Rivera": ["Running late"]}))

    def test_with_the_gate_a_send_still_asks_in_ask_before_acting(self):
        self.gated(permissions.PermissionGate(permissions.ASK_IMPORTANT), setting_on=True)
        self.in_a_thread_with_a_draft()
        text, failed = self.call("tap", label="Send")
        self.assertFalse(failed, text)
        self.assertEqual([r["title"] for r in self.asked], ["Send this message to Alex Rivera?"])
        # "Don't ask again" is an allow rule for sending messages in this app.
        self.assertEqual((self.asked[0]["remember"]["label"], self.asked[0]["remember"]["op"]), ("send", "ACT"))
        self.assertEqual(self.state["sent"], {"Alex Rivera": ["Running late"]})

    def test_with_the_gate_a_code_asks_with_the_setting_off_and_a_refusal_is_never_typed(self):

        class Gate(permissions.PermissionGate):
            def decide(self, action, existing_act=None):
                if action.op == "TYPE" and action.text == "482913":
                    return permissions.Decision("ask", "floor", "code", "Types a one-time code", 0.1,
                                                title="Enter this code?")
                return permissions.Decision("allow", "navigate", None, "", 0.1)
        self.gated(Gate(permissions.DONT_ASK), setting_on=False)
        self.call("open_app", app="Messages")
        self.call("tap", label="Alex Rivera")
        self.answer = "denied"
        text, failed = self.call("type_text", label="iMessage", text="482913")
        self.assertTrue(failed)
        self.assertEqual([(r["title"], r["reason"]) for r in self.asked],
                         [("Enter this code?", "Types a one-time code")])
        self.assertNotIn("remember", self.asked[0])  # never "Don't ask again" for a code
        self.assertNotIn("482913", json.dumps(self.state))

    def test_a_phone_guard_stop_is_a_tool_error_and_blocks_later_actions(self):
        from mobile_agent.agent_hooks import GuardVerdict
        from mobile_agent.frontier import GuardStop
        verdict = GuardVerdict("stop", "phone_locked", "Unlock your iPhone before trying again.")
        with patch.object(self.tools.agent, "_guard_check", side_effect=GuardStop(verdict)):
            text, failed = self.call("screen")
        self.assertTrue(failed)
        self.assertEqual(text, verdict.message)
        self.assertIs(self.tools.stopped, verdict)
        with patch.object(self.tools.driver, "execute") as execute:
            self.assertTrue(self.call("tap", label="Messages")[1])
        execute.assert_not_called()

    def test_a_tap_with_unknown_outcome_cannot_be_sent_again(self):
        from mobile_agent.transport import TransportError
        for error in (TransportError("outcome unknown"), TypeError("bad reply"), RuntimeError("bad receipt")):
            with self.subTest(error=type(error).__name__):
                self.tools.stopped = None
                with patch.object(self.tools.driver, "execute", side_effect=error) as execute:
                    for _ in range(2):
                        text, failed = self.call("tap", label="Messages")
                        self.assertTrue(failed)
                        self.assertIn("outcome is unknown", text)
                execute.assert_called_once()
                self.assertEqual(self.tools.stopped.code, "action_unknown")

    def test_a_failed_read_after_a_tap_cannot_invite_another_one(self):
        from mobile_agent.transport import TransportError
        with patch.object(self.tools.driver, "execute") as execute, \
                patch.object(self.tools.agent, "_settle", side_effect=TransportError("read failed")):
            self.assertTrue(self.call("tap", label="Messages")[1])
            self.assertTrue(self.call("tap", label="Messages")[1])
        execute.assert_called_once()
        self.assertEqual(self.tools.actions, 1)
        self.assertEqual(self.tools.stopped.code, "action_unknown")

    def test_a_failed_step_record_after_a_tap_stops_the_run(self):
        def emit(event):
            if event.get("event") == "step":
                raise OSError("result journal unavailable")
        self.tools.out = emit
        with patch.object(self.tools.driver, "execute") as execute:
            for _ in range(2):
                self.assertTrue(self.call("tap", label="Messages")[1])
        execute.assert_called_once()
        self.assertEqual(self.tools.stopped.code, "action_unknown")

    def test_moving_around_is_not_held_after_a_failure(self):
        from mobile_agent.transport import TransportError
        with patch.object(self.tools.driver, "execute", side_effect=TransportError("outcome unknown")):
            self.assertTrue(self.call("home")[1])
        self.assertIsNone(self.tools.stopped)
        self.assertFalse(self.call("home")[1])

    def test_a_known_pre_dispatch_refusal_still_allows_a_fresh_tap(self):
        from mobile_agent.wda.guards import DriverRejection
        with patch.object(self.tools.driver, "execute", side_effect=DriverRejection("stale_revision")):
            self.assertTrue(self.call("tap", label="Messages")[1])
        self.assertIsNone(self.tools.stopped)
        self.assertEqual(self.tools.actions, 0)
        self.assertFalse(self.call("tap", label="Messages")[1])
        self.assertEqual(self.tools.actions, 1)

    def test_invalid_text_is_correctable_before_any_send(self):
        self.call("open_app", app="Messages")
        self.call("tap", label="Alex Rivera")
        with patch.object(self.tools.driver, "execute") as execute:
            for text in ("a\x00b", "x" * 4001):
                self.assertTrue(self.call("type_text", label="iMessage", text=text)[1])
        execute.assert_not_called()
        self.assertIsNone(self.tools.stopped)
        self.assertFalse(self.call("type_text", label="iMessage", text="Running late")[1])


class RunnerApprovalTests(unittest.TestCase):

    def approver(self, origin, setting, gate=None):
        gate = gate or object()
        from contextlib import nullcontext
        from types import SimpleNamespace

        from mobile_agent.ai_plans import runner
        from mobile_agent.server import Runtime
        seen = {}

        class Built(Exception):
            pass

        def tools(driver, **kwargs):
            seen.update(kwargs)
            raise Built()
        runtime = SimpleNamespace(open_driver=lambda *a: object(), smart_apps=lambda run: {},
                                  ask_before_acting=lambda: setting)
        runtime.asks_before_acting = lambda run: Runtime.asks_before_acting(runtime, run)
        runtime.permission_gate = lambda run: gate
        run_ = SimpleNamespace(id="r1", engine="codex", origin=origin, goal="Text Sam", emit=lambda e: None,
                               request_approval=lambda request: "approved", stop=threading.Event(), guard=None,
                               device_name="iPhone", app={})
        agent = SimpleNamespace(status=lambda max_age=0: {"ready": True})
        trace = SimpleNamespace(span=lambda name: nullcontext())
        with patch.object(runner, "PhoneTools", tools), self.assertRaises(Built):
            runner.run(runtime, run_, trace, None, agent=agent)
        self.seen = seen
        return seen["approve"]

    def test_a_remote_run_asks_with_the_setting_off(self):
        self.assertIsNotNone(self.approver("remote", False))

    def test_the_runs_permission_gate_goes_to_the_tools(self):
        gate = object()
        self.approver("app", False, gate=gate)
        self.assertIs(self.seen["permissions"], gate)

    def test_other_runs_still_follow_the_setting(self):
        self.assertIsNone(self.approver("app", False))
        self.assertIsNone(self.approver("cli", False))
        self.assertIsNotNone(self.approver("app", True))

class AgentRuns(Service):

    def setUp(self):
        super().setUp()
        self.bin = self.root / "agent-bin"
        self.bin.mkdir()
        self.log = self.root / "agent.log"
        gemini_settings = self.root / ".gemini" / "settings.json"
        gemini_settings.parent.mkdir(parents=True, exist_ok=True)
        gemini_settings.write_text(json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}))
        os.environ.update({"MOBSTER_CODEX_PATH": str(fake_bin(self.bin, "codex")),
                           "MOBSTER_CLAUDE_CODE_PATH": str(fake_bin(self.bin, "claude")),
                           "MOBSTER_GEMINI_PATH": str(fake_bin(self.bin, "gemini")),
                           "FAKE_AGENT_LOG": str(self.log), "FAKE_LOGIN": "plan",
                           "OPENAI_API_KEY": "sk-mobsters-own", "ANTHROPIC_API_KEY": "sk-ant-mobsters-own"})
        shell = patch.object(cli, "login_shell_path", lambda env=None, refresh=False: "")
        shell.start()
        self.addCleanup(shell.stop)
        agents.reset_for_tests()
        self.addCleanup(agents.reset_for_tests)
        self.runtime.config.port = int(self.url.rsplit(":", 1)[1])
        world.PACE["value"] = 0

    def agent_log(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def approve_when_asked(self, run_id, approve=True):
        approval = self.asked(run_id, "commit")
        status, _ = self.call("POST", f"/api/runs/{run_id}/approval", {"id": approval["id"], "approve": approve},
                              origin=None)
        self.assertEqual(status, 200)
        return approval

    def test_codex_texts_alex_on_the_plan_with_the_approval_card(self):
        thread, run_id = self.start("Text Alex that I'm running late", engine="codex")
        approval = self.approve_when_asked(run_id)
        self.assertEqual((approval["title"], approval["text"]), ("Send this message to Alex Rivera?", "Running late"))
        run = self.finished(run_id)
        summary = run["summary"]
        self.assertEqual((summary["status"], summary["outcome"], summary["engine"]), ("completed", "done", "codex"))
        self.assertEqual((summary["billing"], summary["plan"], summary["costUsd"]), ("plan", "ChatGPT", None))
        self.assertEqual(summary["answer"], "Sent Alex “Running late” in Messages.")
        # A send: the plan path doesn't check that the message shows sent, so the result asks you to look.
        self.assertEqual((summary["proof"], summary["independently_verified"]), ([], False))
        self.assertEqual(summary["agent"]["session"], "0199a213-81c0-7800-8aa1-bbab2a035a53")
        self.assertEqual(self.phone["sent"], {"Alex Rivera": ["Running late"]})
        events = [e["event"] for e in run["events"]]
        self.assertIn("agent_started", events)
        self.assertIn("agent_message", events)
        steps = [e["text"] for e in run["events"] if e["event"] == "step"]
        self.assertEqual(steps[-1], "Tapped Send")
        self.assertEqual(self.result(thread, run_id)["outcome"], "done")
        # The plan, never Mobster's own keys: the CLI saw no API key, and got the run's MCP token.
        seen = self.agent_log()[0]
        self.assertEqual((seen["which"], seen["env"], seen["token"]), ("codex", {}, True))

    def test_one_message_one_approval_when_return_didnt_send(self):
        os.environ["FAKE_AGENT_SCENARIO"] = "return_then_send"
        _, run_id = self.start("Text Alex that I'm running late", engine="claude_code")
        approval = self.approve_when_asked(run_id)
        self.assertEqual((approval["title"], approval["text"]), ("Send this message to Alex Rivera?", "Running late"))
        run = self.finished(run_id)
        self.assertEqual(run["summary"]["status"], "completed", run["summary"])
        self.assertEqual(sum(1 for e in run["events"] if e["event"] == "approval_requested"), 1)
        self.assertEqual(self.phone["sent"], {"Alex Rivera": ["Running late"]})

    def test_a_link_into_a_new_app_asks_once_and_dont_ask_again_opens_the_next(self):
        os.environ["FAKE_AGENT_SCENARIO"] = "open_things"
        _, run_id = self.start("Add milk to Things", engine="codex")
        approval = self.asked(run_id, "commit")
        self.assertEqual((approval["title"], approval["rememberable"], approval["rememberLabel"]),
                         ("Open this link in Things?", True, "Don’t ask again for Things"))
        status, _ = self.call("POST", f"/api/runs/{run_id}/approval",
                              {"id": approval["id"], "approve": True, "remember": True}, origin=None)
        self.assertEqual(status, 200)
        run = self.finished(run_id)
        self.assertEqual(run["summary"]["status"], "completed", run["summary"])
        self.assertEqual(sum(1 for e in run["events"] if e["event"] == "approval_requested"), 1)
        self.assertEqual(self.phone["links"], ["things:///add?title=Milk", "things:///show?id=today"])
        remembered = self.runtime.permission_exceptions().public()
        self.assertEqual([(r["app"], r["appName"], r["op"]) for r in remembered],
                         [("url:things", "Things", "OPEN_URL")])

    def test_a_follow_up_resumes_the_agents_own_session(self):
        os.environ["FAKE_AGENT_SCENARIO"] = "read_about"
        thread, first = self.start("What iOS version is this phone on?", engine="codex")
        self.finished(first)
        _, second = self.start("And which model is it?", thread, engine="codex")
        run = self.finished(second)
        self.assertTrue(run["summary"]["answer"].startswith("Resumed 0199a213"), run["summary"])
        argv = self.agent_log()[-1]["argv"]
        self.assertEqual(argv[:2], ["exec", "resume"])

    def test_claude_code_runs_on_the_claude_plan(self):
        os.environ["FAKE_AGENT_SCENARIO"] = "read_about"
        _, run_id = self.start("What iOS version is this phone on?", engine="claude_code")
        summary = self.finished(run_id)["summary"]
        self.assertEqual((summary["status"], summary["plan"], summary["billing"]), ("completed", "Claude", "plan"))
        self.assertIn("iOS 26.4", summary["answer"])
        # Read off Settings › General › About, and quoted from it: Done, not Take a look (DR-13).
        self.assertEqual([(p["quote"], p["app"], p["screen"]) for p in summary["proof"]],
                         [("26.4", "Settings", "About"), ("iPhone 17 Pro", "Settings", "About")])
        self.assertTrue(summary["independently_verified"])
        self.assertEqual(self.agent_log()[0]["env"], {})

    def test_gemini_cli_runs_on_the_google_account_with_only_the_phone(self):
        os.environ["FAKE_AGENT_SCENARIO"] = "read_about"
        _, run_id = self.start("What iOS version is this phone on?", engine="gemini")
        run = self.finished(run_id)
        summary = run["summary"]
        self.assertEqual((summary["status"], summary["plan"], summary["billing"]), ("completed", "Google", "plan"),
                         summary)
        self.assertIn("iOS 26.4", summary["answer"])
        seen = self.agent_log()[0]
        self.assertEqual((seen["which"], seen["env"]), ("gemini", {}))
        # The run's settings (with its token) are gone once it ends.
        self.assertFalse(any(self.root.glob("**/agents/gemini/.gemini/settings.json")))

    def test_a_gemini_cli_that_isnt_signed_in_says_so(self):
        os.environ["FAKE_AGENT_SCENARIO"] = "signed_out"
        _, run_id = self.start("What iOS version is this phone on?", engine="gemini")
        summary = self.finished(run_id)["summary"]
        self.assertEqual((summary["status"], summary["model_error"]), ("error", "signed_out"))
        self.assertIn("Gemini CLI isn't signed in", summary["reason"])

    def usage_rows(self, provider):
        status, data = self.call("GET", "/api/usage")
        self.assertEqual(status, 200, data)
        return data, [p for p in data["providers"] if p["provider"] == provider]

    def test_a_claude_code_run_on_the_plan_shows_tokens_and_api_equivalent_never_spend(self):
        os.environ["FAKE_AGENT_SCENARIO"] = "read_about"
        _, run_id = self.start("What iOS version is this phone on?", engine="claude_code")
        run = self.finished(run_id)
        summary = run["summary"]
        self.assertIsNone(summary["costUsd"])
        # Cost-0 plan rows never make the run's own cost read 0: History and Home say the plan instead.
        self.assertIsNone(run["costUsd"])
        self.assertEqual(summary["agent"]["usage"], {"input_tokens": 20440, "output_tokens": 350, "total_tokens": 20790,
                                                     "cache_read_tokens": 18000, "cache_write_tokens": 2400})
        self.assertEqual(summary["agent"]["apiEquivalentUsd"], 0.0412)
        self.assertEqual(summary["agent"]["limits"]["status"], "allowed")
        data, rows = self.usage_rows("claude_code")
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual((row["billing"], row["model"], row["estimated_usd"], row["cache_read_tokens"],
                          row["cache_write_tokens"], row["total_tokens"]),
                         ("plan", "claude-sonnet-5-5", 0.0, 18000, 2400, 20790))
        self.assertAlmostEqual(row["api_equivalent_usd"], 0.0412)
        self.assertEqual(data["totals"]["estimated_usd"], 0.0)
        self.assertEqual(data["totals"]["plan_tokens"], 20790)
        self.assertEqual(data["limits"]["claude_code"]["windows"]["five_hour"]["utilization"], 0.37)
        events = run["events"]
        steps = [e for e in events if e["event"] == "agent_usage"]
        # three tool calls and the answer: four responses, each once
        self.assertEqual([e["call_id"] for e in steps], ["msg_001", "msg_002", "msg_003", "msg_004"])
        self.assertEqual(steps[0]["usage"], {"input_tokens": 6103, "output_tokens": 40, "cache_read_tokens": 6000,
                                             "cache_write_tokens": 100})
        self.assertEqual(steps[0]["model"], "claude-sonnet-5-5")
        self.assertEqual([e["event"] for e in events if e["event"].startswith("inference_")],
                         ["inference_started", "inference_finished"])
        self.assertEqual(sum(1 for e in events if e["event"] == "agent_limits"), 1)
        status, home = self.call("GET", "/api/home")
        self.assertEqual(status, 200)
        self.assertEqual((home["week"]["planTokens"], home["week"]["spentUsd"]), (20790, 0.0))
        self.assertAlmostEqual(home["week"]["apiEquivalentUsd"], 0.0412)

    def test_a_claude_code_run_on_an_api_key_records_the_cost_as_spend(self):
        os.environ.update({"FAKE_AGENT_SCENARIO": "read_about", "FAKE_LOGIN": "api_key"})
        _, run_id = self.start("What iOS version is this phone on?", engine="claude_code")
        run = self.finished(run_id)
        summary = run["summary"]
        self.assertEqual((summary["billing"], summary["costUsd"]), ("api_key", 0.0412))
        self.assertEqual(run["costUsd"], 0.0412)  # real spend: the run's cost, spending limits and Home count it
        data, rows = self.usage_rows("claude_code")
        self.assertEqual((rows[0]["billing"], rows[0]["estimated_usd"]), ("api_key", 0.0412))
        self.assertEqual(data["totals"]["estimated_usd"], 0.0412)
        self.assertIsNone(data["totals"]["plan_tokens"])

    def test_a_codex_run_on_the_plan_has_no_api_equivalent_for_an_unpriced_model(self):
        os.environ["FAKE_AGENT_SCENARIO"] = "read_about"
        _, run_id = self.start("What iOS version is this phone on?", engine="codex")
        run = self.finished(run_id)
        self.assertEqual(run["summary"]["agent"]["usage"], {
            "input_tokens": 12000, "output_tokens": 300, "total_tokens": 12300, "cache_read_tokens": 9000,
            "cache_write_tokens": 0, "reasoning_output_tokens": 120})
        self.assertIsNone(run["summary"]["agent"]["apiEquivalentUsd"])
        data, rows = self.usage_rows("codex")
        self.assertEqual((rows[0]["billing"], rows[0]["model"], rows[0]["estimated_usd"], rows[0]["cache_read_tokens"],
                          rows[0]["api_equivalent_usd"]), ("plan", "default", 0.0, 9000, None))
        self.assertEqual(data["limits"]["codex"]["windows"]["seven_day"]["utilization"], 0.1)
        self.assertEqual([e["call_id"] for e in run["events"] if e["event"] == "agent_usage"], [None])

    def test_stop_ends_the_cli_and_its_endpoint_and_still_records_the_calls_it_made(self):
        os.environ["FAKE_AGENT_SCENARIO"] = "hang"
        _, run_id = self.start("Do something long", engine="claude_code")
        # The fake prints a message and a tool call (two responses), then waits to be stopped; the first response's
        # usage is final once the second starts.
        self.until(lambda: any(e["event"] == "agent_usage" for e in self.run_of(run_id)["events"]), "a response")
        status, _ = self.call("POST", f"/api/runs/{run_id}/stop", {}, origin="app")
        self.assertIn(status, (200, 202))
        run = self.finished(run_id)
        self.assertEqual(run["summary"]["status"], "stopped")
        _, rows = self.usage_rows("claude_code")
        self.assertEqual((rows[0]["billing"], rows[0]["total_tokens"], rows[0]["cache_read_tokens"]),
                         ("plan", 12286, 12000))
        self.assertEqual(run["summary"]["agent"]["usage"]["total_tokens"], 12286)
        self.assertIsNone(mcp_http.endpoint_for(run_id))

    def test_a_ledger_row_the_journal_refuses_never_fails_the_run(self):
        from mobile_agent.ai_plans import runner
        from mobile_agent.journal import JournalError
        real = runner.record_usage

        def refuse(agent, run_, figures, billing, latency):
            def emit(event):
                if event["event"].startswith("inference_"):
                    raise JournalError("full")
            return real(agent, type("R", (), {"emit": staticmethod(emit)})(), figures, billing, latency)
        os.environ["FAKE_AGENT_SCENARIO"] = "read_about"
        with patch.object(runner, "record_usage", refuse):
            _, run_id = self.start("What iOS version is this phone on?", engine="claude_code")
            summary = self.finished(run_id)["summary"]
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(self.usage_rows("claude_code")[1], [])

    def test_the_plans_usage_limit_is_said_plainly(self):
        os.environ["FAKE_AGENT_SCENARIO"] = "limit"
        _, run_id = self.start("Text Alex", engine="codex")
        summary = self.finished(run_id)["summary"]
        self.assertEqual((summary["status"], summary["model_error"]), ("error", "plan_limit"))
        self.assertTrue(summary["reason"].startswith("You've reached your ChatGPT plan's usage limit in Codex."),
                        summary["reason"])

    def test_a_signed_out_cli_is_refused_before_the_phone_is_touched(self):
        os.environ["FAKE_LOGIN"] = "out"
        status, data = self.call("POST", "/api/threads", {"message": {"text": "Text Alex", "engine": "codex"}})
        self.assertEqual((status, data.get("code")), (409, "engine_unavailable"), data)
        self.assertIn("codex login", data["error"])

    def test_messages_while_it_works_are_refused_by_name(self):
        os.environ["FAKE_AGENT_SCENARIO"] = "hang"
        thread, run_id = self.start("Do something long", engine="codex")
        self.until(lambda: any(e["event"] == "agent_started" for e in self.run_of(run_id)["events"]),
                   "the agent to start")
        status, data = self.call("POST", f"/api/threads/{thread}/messages", {"text": "faster please"})
        self.assertEqual((status, data.get("code")), (409, "steer_unsupported"))
        self.assertTrue(data["error"].startswith("Codex can't take messages"))
        self.call("POST", f"/api/runs/{run_id}/stop", {}, origin="app")
        self.finished(run_id)


if __name__ == "__main__":
    unittest.main()
