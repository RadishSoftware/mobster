"""`mobster mcp`'s protocol: version negotiation, errors, batches, concurrency, cancellation, progress,
stdout discipline and shutdown on EOF. A real Server runs over OS pipes against the tools on fakes."""

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

from mobile_agent.mcp_server.protocol import (DEFAULT_PROTOCOL_VERSION, INVALID_PARAMS, INVALID_REQUEST,
                                              METHOD_NOT_FOUND, PARSE_ERROR, PROTOCOL_VERSIONS, Server)
from mobile_agent.mcp_server.tools import CORE_TOOL_NAMES, INSTRUCTIONS, ToolSet, smart_sentence
from mobile_agent.tests.test_mcp_tools import FakeAPI, FakeManager, fake_image
from mobile_agent.tests.timing import bound
from mobile_agent.testkit.mcp import TestTools

# The sentence the testing track's tools (run_tests, record_check) add to the instructions of the real server.
TESTING = TestTools().instructions()

ROOT = Path(__file__).resolve().parents[2]


class Pipe:
    """A Server on a thread, fed and read through OS pipes like a client's stdio."""

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

    def test_smart_adds_its_sentence_to_the_instructions(self):
        result = Pipe(self, key="sk-test", smart_model="gpt-5.6-sol").initialize()
        self.assertEqual(result["instructions"], INSTRUCTIONS + " " + smart_sentence("gpt-5.6-sol"))

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
        pipe.call(4, "verify", {"bundle_id": "com.apple.Preferences", "steps": ["Open General"]})
        self.assertEqual(pipe.response(4)["error"]["code"], INVALID_PARAMS)  # no key: not offered
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


class BadMessageTests(unittest.TestCase):
    """Review 3: a message that raised on the reader thread ended the server (exit 1, calls unanswered, the open
    run aborted). A bad line now gets its error, and the session goes on."""

    def test_a_cancel_naming_a_list_or_object_is_ignored(self):
        gate = threading.Event()
        pipe = Pipe(self, gate=gate, prepare_wait=.2)
        pipe.initialize()
        pipe.call(1, "verify_start", {"bundle_id": "com.apple.Preferences", "steps": [], "expect": []})
        run_id = pipe.response(1)["result"]["structuredContent"]["run_id"]
        for request_id in ([1], {"id": 1}, None, 1.5, True):
            pipe.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}})
        pipe.request(2, "ping")
        self.assertEqual(pipe.response(2), {"jsonrpc": "2.0", "id": 2, "result": {}})
        self.assertTrue(pipe.thread.is_alive())
        self.assertTrue(pipe.tools.sessions[run_id].open)
        gate.set()

    def test_a_batch_nested_10000_deep_is_a_parse_error(self):
        pipe = Pipe(self)
        pipe.initialize()
        pipe.send(b"[" * 10000 + b"]" * 10000)
        self.assertEqual(pipe.receive()["error"]["code"], PARSE_ERROR)
        pipe.request(2, "ping")
        self.assertEqual(pipe.response(2)["result"], {})
        self.assertTrue(pipe.thread.is_alive())

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


class EncodingTests(unittest.TestCase):
    """Every request gets an answer, whatever the client sent or a tool returned (review correctness-7)."""

    def test_a_lone_surrogate_in_a_name_is_answered(self):
        pipe = Pipe(self)
        pipe.initialize()
        pipe.send(b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"no_such_\\ud800","arguments":{}}}')
        pipe.send(b'{"jsonrpc":"2.0","id":4,"method":"bogus/\\udfff"}')
        pipe.request(5, "ping")
        replies = {}
        while set(replies) != {2, 4, 5}:  # each request runs on its own thread: any order
            reply = pipe.receive()
            replies[reply.get("id")] = reply
        self.assertEqual(replies[2]["error"]["code"], INVALID_PARAMS)
        self.assertIn("no_such_?", replies[2]["error"]["message"])
        self.assertEqual(replies[4]["error"]["code"], METHOD_NOT_FOUND)
        self.assertIn("bogus/?", replies[4]["error"]["message"])
        self.assertEqual(replies[5]["result"], {})

    def test_a_result_json_cant_encode_becomes_an_internal_error(self):
        pipe = Pipe(self)
        pipe.initialize()
        pipe.server._handle = lambda method, params, call: {"value": float("nan"), "object": object()}
        pipe.request(7, "ping")
        reply = pipe.response(7)
        self.assertEqual(reply["error"]["code"], -32603)
        self.assertIn("couldn't encode", reply["error"]["message"])

    def test_the_real_command_answers_every_request_and_exits_0(self):
        """The same input aborted the process with SIGABRT (exit 134) in 4 of 5 runs."""
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


class ConcurrencyTests(unittest.TestCase):
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
        self.assertEqual(pipe.server._calls, {})  # the wait ended long before its 30 s
        pipe.request(3, "ping")
        self.assertEqual(pipe.receive()["id"], 3)
        with self.assertRaises(queue.Empty):
            pipe.receive(timeout=.3)
        self.assertTrue(pipe.tools.sessions[run_id].open)  # cancelling a wait doesn't stop the run
        gate.set()

    def test_progress_notifications_while_a_call_waits(self):
        gate = threading.Event()
        pipe = Pipe(self, gate=gate, prepare_wait=.2, progress_seconds=.1)
        pipe.initialize()
        run_id = self.start_preparing(pipe)
        pipe.call(2, "wait", {"run_id": run_id, "timeout_s": 20}, meta={"progressToken": "tok-1"})
        notes = []
        while len(notes) < 8:
            message = pipe.receive()
            self.assertEqual(message["method"], "notifications/progress")
            notes.append(message["params"])
        gate.set()
        self.assertEqual(pipe.response(2, timeout=10)["result"]["structuredContent"]["status"], "ready")
        self.assertEqual({note["progressToken"] for note in notes}, {"tok-1"})
        self.assertEqual([note["progress"] for note in notes], list(range(1, 9)))
        self.assertIn("Booting the simulator.", [note["message"] for note in notes])
        pipe.request(3, "ping")
        self.assertEqual(pipe.receive(), {"jsonrpc": "2.0", "id": 3, "result": {}})  # no more progress

    def test_no_progress_without_a_token(self):
        gate = threading.Event()
        pipe = Pipe(self, gate=gate, prepare_wait=.2, progress_seconds=.05)
        pipe.initialize()
        run_id = self.start_preparing(pipe)
        pipe.call(2, "wait", {"run_id": run_id, "timeout_s": 1})
        self.assertEqual(pipe.receive(timeout=5)["id"], 2)
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

    def test_the_real_command_serves_and_exits_0_at_eof(self):
        with tempfile.TemporaryDirectory() as folder:
            done = subprocess.run(
                [sys.executable, "-m", "mobile_agent.mcp_server", "--keyless", "--out", str(Path(folder) / "runs")],
                cwd=str(ROOT), env={"PATH": "/usr/bin:/bin", "HOME": folder, "PYTHONPATH": str(ROOT),
                                    "OPENAI_API_KEY": "sk-test-not-real"},
                input=b"".join(json.dumps(line).encode() + b"\n" for line in [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "t", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]),
                capture_output=True, timeout=bound(60))
        self.assertEqual(done.returncode, 0, done.stderr)
        replies = [json.loads(line) for line in done.stdout.decode().splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        names = [tool["name"] for tool in replies[1]["result"]["tools"]]
        self.assertNotIn("verify", names)  # --keyless, even with a key
        # 17 for checks and devices, and 6 for phone I/O, codes and unlock; never a clipboard read. (The SOTA tracks'
        # providers add their own tools after these: mcp_server/registry.py, among them the testing track's run_tests
        # and record_check, testkit/mcp.py.)
        self.assertEqual(len(set(names) & CORE_TOOL_NAMES), 23)
        self.assertTrue({"run_tests", "record_check"} <= set(names))
        self.assertTrue({"use_code", "read_notifications", "set_clipboard", "install_app", "unlock_status",
                         "unlock"} <= set(names))
        self.assertFalse([name for name in names if "clipboard" in name and name != "set_clipboard"])
        self.assertNotIn(b"sk-test-not-real", done.stderr)
        self.assertIn(b"Smart off (--keyless)", done.stderr)

    def serve(self, env, lines):
        """`python -m mobile_agent.mcp_server` with only ``env`` (and a PATH and HOME of its own)."""
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
                self.assertNotIn(value.encode(), done.stderr)  # no key in the log
        return {reply["id"]: reply for reply in map(json.loads, done.stdout.decode().splitlines())}, done.stderr

    def test_an_anthropic_key_alone_runs_smart_on_claude_and_says_so(self):
        """Review 3: with only ANTHROPIC_API_KEY (which a stdio server inherits from Claude Code), the server
        offered verify on the user's "OpenAI key" while Smart would bill the Anthropic key on claude-sonnet-5-5.
        What it says now names that model and that key."""
        replies, stderr = self.serve({"ANTHROPIC_API_KEY": "sk-ant-test-not-real"},
                                     [{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        instructions = replies[1]["result"]["instructions"]
        # The core's instructions end with this; the tracks' providers may add a sentence each after it.
        self.assertIn(" verify runs the steps itself with claude-sonnet-5-5, on the user's Anthropic key.",
                      instructions)
        self.assertIn(" " + TESTING, instructions)  # the tracks' tools come after the core's
        tools = {tool["name"]: tool for tool in replies[2]["result"]["tools"]}
        self.assertEqual(len(set(tools) & CORE_TOOL_NAMES), 24)
        self.assertTrue({"run_tests", "record_check"} <= set(tools))
        self.assertIn("with claude-sonnet-5-5, on the user's Anthropic key", tools["verify"]["description"])
        for text in (instructions, tools["verify"]["description"], stderr.decode()):
            self.assertNotIn("OpenAI", text)
        self.assertIn(b"Smart on (claude-sonnet-5-5, on the user's Anthropic key)", stderr)

    def test_a_smart_model_without_its_providers_key_turns_smart_off_and_says_why(self):
        replies, stderr = self.serve(
            {"OPENAI_API_KEY": "sk-test-not-real", "MOBSTER_SMART_MODEL": "claude-opus-5-5"},
            [{"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "verify", "arguments": {"bundle_id": "com.example.app", "steps": ["Open it"]}}}])
        self.assertTrue(replies[1]["result"]["instructions"].startswith(INSTRUCTIONS + " "))
        self.assertIn(" " + TESTING, replies[1]["result"]["instructions"])
        self.assertNotIn("verify runs the steps", replies[1]["result"]["instructions"])
        self.assertEqual(len({tool["name"] for tool in replies[2]["result"]["tools"]} & CORE_TOOL_NAMES), 23)
        why = ("MOBSTER_SMART_MODEL is claude-opus-5-5, which needs an Anthropic key; set ANTHROPIC_API_KEY, or unset "
               "MOBSTER_SMART_MODEL to use your OpenAI key")
        self.assertEqual(replies[3]["error"], {"code": INVALID_PARAMS, "message": f"Smart is off ({why}), so this "
                                               "server has no verify tool. Drive the app with verify_start instead."})
        self.assertIn(f"Smart off ({why})".encode(), stderr)


if __name__ == "__main__":
    unittest.main()
