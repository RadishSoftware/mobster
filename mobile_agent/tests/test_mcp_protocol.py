import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import tracks
from mobile_agent.mcp_server import cli, registry
from mobile_agent.mcp_server.__main__ import build_parser
from mobile_agent.mcp_server.protocol import (
    DEFAULT_PROTOCOL_VERSION,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSIONS,
    ProtocolError,
    Server,
    ToolResult,
)
from mobile_agent.mcp_server.tools import (
    ACTS,
    CORE_TOOL_NAMES,
    INSTRUCTIONS,
    PROVIDER_SENTENCE_MAX,
    READ_ONLY,
    ToolError,
    ToolSet,
)
from mobile_agent.testkit.mcp import TestTools
from mobile_agent.tests.seam_support import isolate
from mobile_agent.tests.test_mcp_tools import FakeAPI, FakeManager, fake_image
from mobile_agent.tests.timing import bound

TESTING = TestTools().instructions()

ROOT = Path(__file__).resolve().parents[2]


class Pipe:
    def __init__(self, testcase, key=None, progress_seconds=5.0, **options):
        self.folder = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.folder.cleanup)
        run_options = {k: options.pop(k) for k in ("gate", "tree", "on_action", "prepare_error", "smart_gate")
                       if k in options}
        self.api = FakeAPI(**run_options)
        self.tools = ToolSet(runs_dir=Path(self.folder.name) / "runs", key=key, api=self.api, manager=FakeManager(),
                             imager=fake_image, version="0.0.test", **options)
        self.server = Server(self.tools, version="0.0.test", progress_seconds=progress_seconds)
        in_read, self._in_write = os.pipe()
        out_read, out_write = os.pipe()
        self.reader = os.fdopen(in_read, "rb")
        self.writer = os.fdopen(out_write, "wb", buffering=0)
        self._out = os.fdopen(out_read, "rb")
        self.messages = queue.Queue()
        self.exit_code = None
        threading.Thread(target=self._pump, daemon=True).start()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        testcase.addCleanup(self.close)

    def _serve(self):
        try:
            self.exit_code = self.server.serve(self.reader, self.writer)
        finally:
            self.reader.close()
            self.writer.close()

    def _pump(self):
        with self._out:
            for line in self._out:
                self.messages.put(json.loads(line))

    def send(self, message):
        data = message if isinstance(message, bytes) else json.dumps(message).encode()
        os.write(self._in_write, data + b"\n")

    def request(self, request_id, method, params=None):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def call(self, request_id, name, arguments=None, meta=None):
        params = {"name": name, "arguments": arguments or {}}
        if meta:
            params["_meta"] = meta
        self.request(request_id, "tools/call", params)

    def receive(self, timeout=5.0):
        return self.messages.get(timeout=bound(timeout))

    def response(self, request_id, timeout=5.0):
        end = time.monotonic() + bound(timeout)
        while True:
            message = self.messages.get(timeout=max(0.01, end - time.monotonic()))
            if message.get("id") == request_id and "method" not in message:
                return message

    def initialize(self, version="2025-11-25"):
        self.request(0, "initialize", {"protocolVersion": version, "capabilities": {},
                                       "clientInfo": {"name": "test-client", "version": "1.0"}})
        result = self.response(0)["result"]
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def close(self):
        try:
            os.close(self._in_write)
        except OSError:
            pass
        self.thread.join(bound(15))


class NegotiationTests(unittest.TestCase):
    def test_initialize_echoes_every_supported_version(self):
        for version in PROTOCOL_VERSIONS:
            with self.subTest(version=version):
                pipe = Pipe(self)
                result = pipe.initialize(version)
                self.assertEqual(result["protocolVersion"], version)
                self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
                self.assertEqual(result["serverInfo"], {"name": "mobster", "version": "0.0.test"})
                self.assertEqual(result["instructions"], INSTRUCTIONS)
                self.assertEqual(pipe.server.client["name"], "test-client")

    def test_an_unknown_version_gets_the_default(self):
        for version in ("2099-01-01", None, 7):
            with self.subTest(version=version):
                self.assertEqual(Pipe(self).initialize(version)["protocolVersion"], DEFAULT_PROTOCOL_VERSION)
        self.assertEqual(DEFAULT_PROTOCOL_VERSION, "2025-11-25")

    def test_structured_content_only_from_2025_06_18(self):
        for version, structured in (("2024-11-05", False), ("2025-03-26", False), ("2025-06-18", True),
                                    ("2025-11-25", True), ("2026-07-28", True)):
            with self.subTest(version=version):
                pipe = Pipe(self)
                pipe.initialize(version)
                pipe.call(1, "status")
                result = pipe.response(1)["result"]
                self.assertEqual("structuredContent" in result, structured)
                self.assertEqual(result["content"][0]["type"], "text")
                self.assertFalse(result["isError"])

    def test_result_type_complete_only_from_2026_07_28(self):
        for version, typed in (("2025-11-25", False), ("2026-07-28", True)):
            with self.subTest(version=version):
                pipe = Pipe(self)
                pipe.initialize(version)
                pipe.call(1, "status")
                self.assertEqual(pipe.response(1)["result"].get("resultType"), "complete" if typed else None)
                pipe.request(2, "tools/list")
                self.assertEqual(pipe.response(2)["result"].get("resultType"), "complete" if typed else None)


class ErrorTests(unittest.TestCase):
    def test_tool_errors_are_results_and_protocol_errors_are_json_rpc_errors(self):
        pipe = Pipe(self)
        pipe.initialize()
        pipe.call(1, "tap", {"run_id": "20260928-120000-abcd", "ref": "e1"})
        tool_error = pipe.response(1)
        self.assertTrue(tool_error["result"]["isError"])
        self.assertEqual(tool_error["result"]["content"][0]["text"],
                         "This server has no run 20260928-120000-abcd. Start one with verify_start.")
        pipe.call(2, "no_such_tool")
        self.assertEqual(pipe.response(2)["error"]["code"], INVALID_PARAMS)
        pipe.call(3, "tap", {"run_id": "x", "ref": "e1", "force": True})
        self.assertEqual(pipe.response(3)["error"], {"code": INVALID_PARAMS,
                                                     "message": "arguments.force isn't a field this tool takes."})
        pipe.call(4, "verify_start", {"bundle_id": "com.apple.Preferences", "steps": ["Open General"], "smart": True})
        self.assertEqual(pipe.response(4)["error"]["code"], INVALID_PARAMS)
        pipe.request(5, "resources/list")
        self.assertEqual(pipe.response(5)["error"]["code"], METHOD_NOT_FOUND)
        pipe.request(6, "tools/call", {"name": "status", "arguments": []})
        self.assertEqual(pipe.response(6)["error"]["code"], INVALID_PARAMS)
        pipe.send(b"{not json")
        self.assertEqual(pipe.receive(), {"jsonrpc": "2.0", "id": None,
                                          "error": {"code": PARSE_ERROR, "message": "The message is not valid JSON."}})
        pipe.send({"id": 7, "method": "ping"})
        self.assertEqual(pipe.response(7)["error"]["code"], INVALID_REQUEST)

    def test_batches_are_answered_element_by_element(self):
        pipe = Pipe(self)
        pipe.initialize()
        pipe.send([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                   {"jsonrpc": "2.0", "method": "notifications/initialized"},
                   {"jsonrpc": "2.0", "id": 3, "method": "nope"}])
        answers = {}
        for _ in range(3):
            message = pipe.receive()
            answers[message["id"]] = message
        self.assertEqual(answers[1]["result"], {})
        self.assertIn("verify_start", [tool["name"] for tool in answers[2]["result"]["tools"]])
        self.assertEqual(answers[3]["error"]["code"], METHOD_NOT_FOUND)
        pipe.send([])
        self.assertEqual(pipe.receive()["error"]["code"], INVALID_REQUEST)
        with self.assertRaises(queue.Empty):
            pipe.receive(timeout=.2)

    def test_a_message_that_raises_is_logged_and_the_session_goes_on(self):
        logged = []
        pipe = Pipe(self)
        pipe.server.log = logged.append
        pipe.initialize()
        real = pipe.server._dispatch

        def dispatch(message):
            if isinstance(message, dict) and message.get("method") == "boom":
                raise TypeError("unhashable type: 'list'")
            return real(message)
        pipe.server._dispatch = dispatch
        pipe.send({"jsonrpc": "2.0", "method": "boom"})
        pipe.request(2, "ping")
        self.assertEqual(pipe.response(2)["result"], {})
        self.assertTrue(any("TypeError: unhashable type" in line for line in logged), logged)
        pipe.close()
        self.assertEqual(pipe.exit_code, 0)

    def test_a_result_json_cant_encode_becomes_an_internal_error(self):
        pipe = Pipe(self)
        pipe.initialize()
        pipe.server._handle = lambda method, params, call, version: {"value": float("nan"), "object": object()}
        pipe.request(7, "ping")
        reply = pipe.response(7)
        self.assertEqual(reply["error"]["code"], -32603)
        self.assertIn("couldn't encode", reply["error"]["message"])

class ConcurrencyTests(unittest.TestCase):
    def test_transport_cancellation_before_admission_prevents_dispatch(self):
        from unittest.mock import Mock

        from mobile_agent.mcp_server.protocol import ToolResult
        for when in ("never", "before", "during"):
            with self.subTest(cancelled=when):
                cancelled, answers = threading.Event(), []
                tools = Mock()

                def work(*args):
                    if when == "during":
                        cancelled.set()
                    return ToolResult("finished")

                tools.call.side_effect = work
                if when == "before":
                    cancelled.set()
                server = Server(tools, version="test")
                result = server.handle_message(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "tap", "arguments": {}}},
                    answers.append, protocol_version=DEFAULT_PROTOCOL_VERSION, cancelled=cancelled)
                self.assertEqual(tools.call.call_count, 0 if when == "before" else 1)
                self.assertEqual(len(answers), 1 if when == "never" else 0)
                self.assertEqual(result is None, when != "never")

    def test_duplicate_in_flight_ids_never_dispatch_a_second_tool(self):
        from unittest.mock import Mock
        for remote in (False, True):
            with self.subTest(remote=remote):
                entered, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                tools = Mock()

                def work(*args):
                    entered.set()
                    release.wait(bound(3))
                    return ToolResult("finished")

                tools.call.side_effect = work
                server = Server(tools, version="test")
                message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "tap", "arguments": {}}}
                answers = []
                server.send = answers.append

                def receive():
                    if remote:
                        server.handle_message(message, answers.append, protocol_version=DEFAULT_PROTOCOL_VERSION)
                    else:
                        server._dispatch(message)

                first = threading.Thread(target=receive, daemon=True)
                first.start()
                self.assertTrue(entered.wait(bound(1)))
                original = server._calls[1]
                receive()
                self.assertEqual(tools.call.call_count, 1)
                self.assertIs(server._calls[1], original)
                self.assertEqual(answers[-1]["error"]["code"], INVALID_REQUEST)
                initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                if remote:
                    server.handle_message(initialize, answers.append, protocol_version=DEFAULT_PROTOCOL_VERSION)
                else:
                    server._dispatch(initialize)
                self.assertEqual(server.client, {})
                self.assertEqual(answers[-1]["error"]["code"], INVALID_REQUEST)
                server._notification("notifications/cancelled", {"requestId": 1})
                self.assertTrue(original.cancelled.is_set())
                release.set()
                self.assertTrue(original.done.wait(bound(2)))
                first.join(bound(1))

    def start_preparing(self, pipe):
        pipe.call(1, "verify_start", {"bundle_id": "com.apple.Preferences", "steps": [], "expect": []})
        started = pipe.response(1)["result"]["structuredContent"]
        self.assertEqual(started["status"], "preparing")
        return started["run_id"]

    def test_ping_is_answered_while_wait_blocks(self):
        gate = threading.Event()
        pipe = Pipe(self, gate=gate, prepare_wait=.2)
        pipe.initialize()
        run_id = self.start_preparing(pipe)
        pipe.call(2, "wait", {"run_id": run_id, "timeout_s": 20})
        time.sleep(.2)
        sent = time.monotonic()
        pipe.request(3, "ping")
        first = pipe.receive()
        self.assertEqual(first, {"jsonrpc": "2.0", "id": 3, "result": {}})
        self.assertLess(time.monotonic() - sent, bound(1))
        gate.set()
        waited = pipe.response(2, timeout=10)["result"]["structuredContent"]
        self.assertEqual(waited["status"], "ready")

    def test_a_cancelled_request_stops_waiting_and_gets_no_response(self):
        gate = threading.Event()
        pipe = Pipe(self, gate=gate, prepare_wait=.2)
        pipe.initialize()
        run_id = self.start_preparing(pipe)
        pipe.call(2, "wait", {"run_id": run_id, "timeout_s": 30})
        time.sleep(.2)
        pipe.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2, "reason": "x"}})
        deadline = time.monotonic() + bound(3)
        while pipe.server._calls and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(pipe.server._calls, {})
        pipe.request(3, "ping")
        self.assertEqual(pipe.receive()["id"], 3)
        with self.assertRaises(queue.Empty):
            pipe.receive(timeout=.3)
        self.assertTrue(pipe.tools.sessions[run_id].open)
        gate.set()


class ShutdownTests(unittest.TestCase):
    def test_eof_stops_every_run_releases_its_lease_and_returns_0(self):
        pipe = Pipe(self)
        pipe.initialize()
        pipe.call(1, "verify_start", {"bundle_id": "com.apple.Preferences", "steps": [], "expect": []})
        self.assertEqual(pipe.response(1)["result"]["structuredContent"]["status"], "ready")
        run = pipe.api.runs[-1]
        pipe.close()
        self.assertFalse(pipe.thread.is_alive())
        self.assertEqual(pipe.exit_code, 0)
        self.assertEqual(len(run.aborted), 1)
        self.assertEqual(run.aborted[0][0], "stopped")
        self.assertIn("the MCP client disconnected", run.aborted[0][1])
        self.assertEqual(run.lease.released, 1)

    def test_calls_in_flight_at_eof_still_answer(self):
        pipe = Pipe(self)
        pipe.initialize()
        pipe.call(1, "status")
        pipe.close()
        self.assertEqual(pipe.response(1)["id"], 1)


class StdoutTests(unittest.TestCase):
    def run_python(self, source, lines, extra_env=None):
        env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), "PYTHONPATH": str(ROOT),
               **(extra_env or {})}
        return subprocess.run([sys.executable, "-c", textwrap.dedent(source)], cwd=str(ROOT), env=env,
                              input="".join(json.dumps(line) + "\n" for line in lines).encode(),
                              capture_output=True, timeout=bound(60))

    def test_a_stray_print_never_reaches_the_protocol_stream(self):
        source = """
            import os, sys
            from mobile_agent.mcp_server.protocol import Server, ToolResult, claim_stdout

            class Tools:
                def instructions(self):
                    return "x"
                def list_tools(self):
                    return [{"name": "noisy", "description": "d",
                             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]
                def call(self, name, arguments, call):
                    print("STRAY PRINT")
                    sys.stdout.flush()
                    os.system("echo CHILD OUTPUT")
                    return ToolResult("done")
                def shutdown(self, reason):
                    print("SHUTDOWN", reason)

            proto = claim_stdout()
            print("AFTER CLAIM")
            sys.exit(Server(Tools(), version="t").serve(sys.stdin.buffer, proto))
        """
        done = self.run_python(source, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "noisy", "arguments": {}}}])
        self.assertEqual(done.returncode, 0, done.stderr)
        replies = [json.loads(line) for line in done.stdout.decode().splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertEqual(replies[1]["result"]["content"], [{"type": "text", "text": "done"}])
        for noise in (b"STRAY PRINT", b"CHILD OUTPUT", b"AFTER CLAIM", b"SHUTDOWN"):
            self.assertIn(noise, done.stderr)
            self.assertNotIn(noise, done.stdout)

    def serve(self, env, lines):
        with tempfile.TemporaryDirectory() as folder:
            done = subprocess.run(
                [sys.executable, "-m", "mobile_agent.mcp_server", "--out", str(Path(folder) / "runs")],
                cwd=str(ROOT), env={"PATH": "/usr/bin:/bin", "HOME": folder, "PYTHONPATH": str(ROOT), **env},
                input=b"".join(json.dumps(line).encode() + b"\n" for line in [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "t", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}] + lines),
                capture_output=True, timeout=bound(60))
        self.assertEqual(done.returncode, 0, done.stderr)
        for name, value in env.items():
            if name.endswith("_KEY"):
                self.assertNotIn(value.encode(), done.stderr)
        return {reply["id"]: reply for reply in map(json.loads, done.stdout.decode().splitlines())}, done.stderr

    def test_an_anthropic_key_alone_runs_smart_on_claude_and_says_so(self):
        replies, stderr = self.serve({"ANTHROPIC_API_KEY": "sk-ant-test-not-real"},
                                     [{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        instructions = replies[1]["result"]["instructions"]
        self.assertIn(" verify_start with smart runs the steps itself with claude-sonnet-5-5, "
                      "on the user's Anthropic key.",
                      instructions)
        self.assertIn(" " + TESTING, instructions)
        tools = {tool["name"]: tool for tool in replies[2]["result"]["tools"]}
        self.assertEqual(len(set(tools) & CORE_TOOL_NAMES), 31)
        # Every testkit tool the provider defines is one the real command serves (gap 12, 24 Oct 2026).
        self.assertTrue({"run_tests", "test_history", "import_maestro", "test_schedule", "review_heals"} <= set(tools))
        flag = tools["verify_start"]["inputSchema"]["properties"]["smart"]["description"]
        self.assertIn("with claude-sonnet-5-5, on the user's Anthropic key", flag)
        for text in (instructions, flag, stderr.decode()):
            self.assertNotIn("OpenAI", text)
        self.assertIn(b"Smart on (claude-sonnet-5-5, on the user's Anthropic key)", stderr)

    def test_the_real_command_answers_every_request_and_exits_0(self):
        lines = [b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",'
                 b'"capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}',
                 b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
                 b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"no_such_\\ud800","arguments":{}}}',
                 b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"no_such_tool","arguments":{}}}',
                 b'{"jsonrpc":"2.0","id":4,"method":"bogus/\\udfff"}',
                 b'{"jsonrpc":"2.0","id":5,"method":"ping"}']
        for attempt in range(3):
            with self.subTest(attempt=attempt), tempfile.TemporaryDirectory() as folder:
                done = subprocess.run(
                    [sys.executable, "-m", "mobile_agent.mcp_server", "--keyless", "--out", str(Path(folder) / "r")],
                    cwd=str(ROOT), env={"PATH": "/usr/bin:/bin", "HOME": folder, "PYTHONPATH": str(ROOT)},
                    input=b"\n".join(lines) + b"\n", capture_output=True, timeout=bound(60))
                self.assertEqual(done.returncode, 0, done.stderr[-2000:])
                replies = [json.loads(line) for line in done.stdout.decode().splitlines()]
                self.assertEqual(sorted(reply["id"] for reply in replies), [1, 2, 3, 4, 5])
                self.assertNotIn(b"Traceback", done.stderr)


ROOT = Path(__file__).resolve().parents[2]
FLAGS = ["--env-file", "--keyless", "--device", "--allow-device", "--allow-testflight", "--allow-files",
         "--allow-photos", "--runtime", "--out", "--log"]


class FlagTests(unittest.TestCase):
    def test_every_flag_has_help(self):
        parser = build_parser()
        options = [action for action in parser._actions if action.option_strings
                   and "--help" not in action.option_strings]
        self.assertEqual(sorted(o for action in options for o in action.option_strings), sorted(FLAGS))
        for action in options:
            with self.subTest(option=action.option_strings[0]):
                self.assertTrue(action.help and action.help[0].islower() and not action.help.endswith("."))

    def test_run_never_raises(self):
        import contextlib
        import io
        args = argparse.Namespace(log=None)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with mock.patch.object(cli, "_serve", side_effect=RuntimeError("boom")):
                self.assertEqual(cli.run(args), 1)
            with mock.patch.object(cli, "_serve", side_effect=KeyboardInterrupt):
                self.assertEqual(cli.run(args), 130)
        self.assertIn("The server stopped on an error (RuntimeError: boom).", stderr.getvalue())


class SmartSetupTests(unittest.TestCase):
    def test_the_key_and_model_follow_engines(self):
        from mobile_agent.mcp_server.tools import NO_SMART_KEY
        openai, anthropic = {"OPENAI_API_KEY": "sk-o"}, {"ANTHROPIC_API_KEY": "sk-a"}
        cases = [
            ({}, (None, None, NO_SMART_KEY)),
            (openai, ("sk-o", "gpt-5.6-sol", None)),
            (anthropic, ("sk-a", "claude-sonnet-5-5", None)),
            ({**openai, **anthropic}, ("sk-a", "claude-sonnet-5-5", None)),
            ({**openai, **anthropic, "MOBSTER_SMART_PROVIDER": "openai"}, ("sk-o", "gpt-5.6-sol", None)),
            ({**openai, **anthropic, "MOBSTER_SMART_MODEL": "claude-opus-5-5"}, ("sk-a", "claude-opus-5-5", None)),
            ({**openai, "MOBSTER_SMART_MODEL": "claude-opus-5-5"},
             (None, None, "MOBSTER_SMART_MODEL is claude-opus-5-5, which needs an Anthropic key; set "
                          "ANTHROPIC_API_KEY, or unset MOBSTER_SMART_MODEL to use your OpenAI key")),
            ({**anthropic, "MOBSTER_SMART_MODEL": "gpt-5.6-sol"},
             (None, None, "MOBSTER_SMART_MODEL is gpt-5.6-sol, which needs an OpenAI key; set OPENAI_API_KEY, or "
                          "unset MOBSTER_SMART_MODEL to use your Anthropic key")),
            ({"MOBSTER_SMART_MODEL": "claude-opus-5-5"},
             (None, None, "MOBSTER_SMART_MODEL is claude-opus-5-5, which needs an Anthropic key; set "
                          "ANTHROPIC_API_KEY")),
        ]
        for env, expected in cases:
            with self.subTest(env=sorted(env)):
                self.assertEqual(cli.smart_setup(env=env), expected)
        self.assertEqual(cli.smart_setup(keyless=True, env={**openai, **anthropic}), (None, None, "--keyless"))

    def test_a_missing_env_file_leads_the_reason_smart_is_off(self):
        from mobile_agent.mcp_server.tools import NO_SMART_KEY
        self.assertEqual(cli.smart_setup(env={}, env_file="/nonexistent/agent.env"),
                         (None, None, "/nonexistent/agent.env does not exist; no keys or settings were loaded from "
                                      "it; " + NO_SMART_KEY))
        self.assertEqual(cli.smart_setup(env={"OPENAI_API_KEY": "sk-o"}, env_file="/nonexistent/agent.env")[2], None)
        self.assertEqual(cli.smart_setup(keyless=True, env={}, env_file="/nonexistent/agent.env")[2], "--keyless")

    def test_the_client_a_verify_run_builds_matches_what_the_server_says(self):
        from mobile_agent import engines
        for env, model, client in (({"OPENAI_API_KEY": "sk-o"}, "gpt-5.6-sol", "OpenAIChat"),
                                   ({"ANTHROPIC_API_KEY": "sk-a"}, "claude-sonnet-5-5", "AnthropicChat")):
            with self.subTest(model=model), mock.patch.dict(os.environ, env, clear=True):
                key, said, _off = cli.smart_setup()
                built = engines.build_client(key=key)
                self.assertEqual((said, built.model, type(built).__name__), (model, model, client))


class RunsFolderTests(unittest.TestCase):
    def test_runs_folder_order(self):
        with tempfile.TemporaryDirectory() as folder:
            home, project = Path(folder) / "home", Path(folder) / "project"
            home.mkdir()
            project.mkdir()
            data = {"MOBSTER_DATA_DIR": str(Path(folder) / "data")}
            self.assertEqual(cli.runs_dir("/abs/out", env={}), Path("/abs/out"))
            self.assertEqual(cli.runs_dir(None, env={"MOBSTER_RUNS_DIR": "/env/runs"}), Path("/env/runs"))
            self.assertEqual(cli.runs_dir(None, env={}, cwd=project, home=home), project / ".mobster" / "runs")
            for cwd in (home, Path("/")):
                with self.subTest(cwd=str(cwd)), mock.patch.dict(os.environ, data):
                    self.assertEqual(cli.runs_dir(None, env=data, cwd=cwd, home=home),
                                     Path(data["MOBSTER_DATA_DIR"]) / "runs")


def hints(tool):
    return {key: value for key, value in tool["annotations"].items() if key != "title"}


class LogTests(unittest.TestCase):
    def test_log_lines_go_to_stderr_and_the_log_file(self):
        import io
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            log = cli.Log(Path(folder) / "mcp.log", stream=stream)
            log("Run 20260928-120000-abcd is ready.")
            written = (Path(folder) / "mcp.log").read_text()
        self.assertRegex(stream.getvalue(), r"^mobster mcp \d\d:\d\d:\d\d Run 20260928-120000-abcd is ready\.\n$")
        self.assertEqual(written, stream.getvalue())


class Threads:
    name = "threads"

    def __init__(self):
        self.calls = []

    def definitions(self, toolset):
        return [{"name": "phone_task", "description": "Run a task on the phone in a conversation.",
                 "inputSchema": {"type": "object", "properties": {"goal": {"type": "string", "minLength": 1}},
                                 "required": ["goal"], "additionalProperties": False}},
                {"name": "list_files", "description": "List files.",
                 "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]

    def annotations(self):
        return {"phone_task": ACTS, "list_files": READ_ONLY}

    def instructions(self):
        return "phone_task runs a task on the user's iPhone and keeps the conversation."

    def call(self, name, arguments, call, toolset):
        self.calls.append((name, arguments, toolset))
        if arguments.get("goal") == "fail":
            raise ToolError("That didn't work.")
        return ToolResult(f"Done: {arguments.get('goal')}")


class Sentence(Threads):
    """A provider with no tools and only its sentence for the server's instructions."""

    def __init__(self, name, text):
        super().__init__()
        self.name, self.text = name, text

    def definitions(self, toolset):
        return []

    def annotations(self):
        return {}

    def instructions(self):
        return self.text


class McpRegistryTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def toolset(self, **kwargs):
        tools = ToolSet(runs_dir=tempfile.mkdtemp(prefix="mobster-seam-mcp-"), keyless=True, **kwargs)
        self.addCleanup(tools.shutdown)
        return tools

    def test_a_providers_tool_is_listed_and_called(self):
        provider = Threads()
        registry.register_provider(provider)
        tools = self.toolset()
        listed = {tool["name"]: tool for tool in tools.list_tools()}
        self.assertIn("phone_task", listed)
        self.assertEqual(hints(listed["phone_task"]), ACTS)
        self.assertNotIn("list_files", listed)
        self.assertTrue(tools.instructions().endswith("phone_task runs a task on the user's iPhone and keeps the "
                                                      "conversation."))
        result = tools.call("phone_task", {"goal": "Text Sam"}, None)
        self.assertEqual(result.text, "Done: Text Sam")
        self.assertIs(provider.calls[0][2], tools)
        self.assertTrue(tools.call("phone_task", {"goal": "fail"}, None).is_error)
        with self.assertRaises(ProtocolError):
            tools.call("phone_task", {"goal": ""}, None)
        with self.assertRaises(ProtocolError):
            tools.call("list_files", {}, None)

    def test_file_tools_need_allow_files(self):
        registry.register_provider(Threads())
        tools = self.toolset(allow_files=True)
        self.assertIn("list_files", [tool["name"] for tool in tools.list_tools()])

    def test_a_clash_with_a_core_tool_raises(self):
        class Clash(Threads):
            name = "clash"

            def annotations(self):
                return {"screen": READ_ONLY}
        with self.assertRaisesRegex(ValueError, "core Mobster tool"):
            registry.register_provider(Clash())
        registry.register_provider(Threads())
        with self.assertRaises(ValueError):
            registry.register_provider(Threads())
        with self.assertRaises(ValueError):
            registry.register_provider(object())
        self.assertEqual([p.name for p in registry.providers()], ["threads"])

    def test_the_instructions_carry_every_tracks_sentence_whole_with_its_period(self):
        tracks.load()
        built = self.toolset().instructions()
        sentences = [" ".join(text.split()) for text in (p.instructions() for p in registry.providers()) if text]
        self.assertGreater(len(sentences), 5)
        for sentence in sentences:
            with self.subTest(sentence=sentence[:40]):
                self.assertLessEqual(len(sentence), PROVIDER_SENTENCE_MAX)
                self.assertTrue(sentence.endswith("."))
                self.assertIn(" " + sentence, built)
        self.assertTrue(built.endswith("."))

    def test_a_sentence_over_the_cap_is_left_out_whole_and_one_without_a_period_gets_one(self):
        registry.register_provider(Sentence("long", "long_tool " + "reads rows " * 30 + "by code."))
        registry.register_provider(Sentence("bare", "bare_tool reads rows"))
        logged = []
        built = self.toolset(log=logged.append).instructions()
        self.assertNotIn("long_tool", built)
        self.assertTrue(built.endswith(" bare_tool reads rows."))
        self.assertEqual(logged, [f"The long tools' instructions are longer than {PROVIDER_SENTENCE_MAX} characters, "
                                  "so Mobster left them out."])

if __name__ == "__main__":
    unittest.main()
