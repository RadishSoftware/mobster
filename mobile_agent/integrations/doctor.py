"""`mobster mcp doctor`: start the server the way a client would, talk MCP to it, and time each step.

It runs the command `mobster mcp install` writes, with the environment an app opened from the Dock gets (launchd's
PATH, no shell variables), in the home folder: the three things that differ between a terminal and Claude
Desktop or Cursor. Then it sends ``initialize``, lists the tools and calls ``status``, the one tool that reads
only. It never calls a tool that acts on a device.
"""

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

PROTOCOL_VERSION = "2025-11-25"
GUI_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
STEP_TIMEOUT_S = 30.0
KEPT_ENV = ("HOME", "USER", "LOGNAME", "TMPDIR", "SHELL", "LANG", "MOBSTER_DATA_DIR", "MOBSTER_RUNS_DIR")


class DoctorError(Exception):
    """A step failed: the message says which and what the server said."""


def gui_env(environ, extra=None):
    """The environment a GUI app's server gets: launchd's PATH and the basics, plus ``extra``."""
    env = {key: environ[key] for key in KEPT_ENV if environ.get(key)}
    env["PATH"] = GUI_PATH
    env.update(extra or {})
    return env


class Session:
    """One server process spoken to over newline-delimited JSON-RPC."""

    def __init__(self, argv, env, cwd):
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env, cwd=cwd)
        self.lines = queue.Queue()
        self.stderr = []
        self.next_id = 0
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read_out(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _read_err(self):
        for line in self.process.stderr:
            self.stderr.append(line.decode("utf-8", "replace").rstrip())
            del self.stderr[:-40]

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params=None, timeout=STEP_TIMEOUT_S):
        self.next_id += 1
        ident = self.next_id
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise DoctorError(f"{method} got no answer in {timeout:.0f} s")
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                code = self.process.wait(timeout=5)
                raise DoctorError(f"the server exited ({code}) before answering {method}")
            try:
                message = json.loads(line)
            except ValueError:
                raise DoctorError(f"the server wrote something that isn't JSON on stdout: {line[:120]!r}") from None
            if message.get("id") != ident:
                continue  # a notification, such as progress
            if "error" in message:
                raise DoctorError(f"{method} failed: {message['error'].get('message')}")
            return message.get("result") or {}

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            code = self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            code = self.process.wait(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        return code


def probe(server, environ=None, cwd=None):
    """Start ``server`` (a clients.Server), initialize, list tools, call status. Returns a report dict:
    ok, the steps with their milliseconds, the tool names, status's first lines, and the server's stderr tail
    when something failed."""
    environ = os.environ if environ is None else environ
    env = gui_env(environ, server.env)
    cwd = cwd or environ.get("HOME") or str(Path.home())
    report = {"ok": False, "command": server.argv(), "steps": [], "tools": [], "status": None}
    command = Path(server.command)
    if not command.is_file() or not os.access(command, os.X_OK):
        report["error"] = f"{server.command} doesn't exist or can't be run"
        return report
    started = time.monotonic()
    try:
        session = Session(server.argv(), env, cwd)
    except OSError as error:
        report["error"] = f"{server.command} didn't start ({error.strerror})"
        return report
    step = started
    try:
        result = session.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "mobster-mcp-doctor", "version": "1"}})
        report["server"] = result.get("serverInfo") or {}
        report["protocol"] = result.get("protocolVersion")
        session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        step = _mark(report, "initialize", step)
        tools = session.request("tools/list").get("tools") or []
        report["tools"] = [tool.get("name") for tool in tools]
        step = _mark(report, "tools/list", step)
        if "status" not in report["tools"]:
            raise DoctorError("the server offers no status tool")
        status = session.request("tools/call", {"name": "status", "arguments": {}})
        texts = [block.get("text", "") for block in status.get("content") or [] if block.get("type") == "text"]
        report["status"] = "\n".join(texts).strip()
        if status.get("isError"):
            raise DoctorError("status answered with an error: " + report["status"][:200])
        _mark(report, "tools/call status", step)
        report["ok"] = True
    except DoctorError as error:
        report["error"] = str(error)
    except (OSError, ValueError) as error:
        report["error"] = f"talking to the server failed ({error})"
    finally:
        code = session.close()
        report["exit_code"] = code
        report["total_ms"] = round((time.monotonic() - started) * 1000)
        if not report["ok"]:
            report["stderr"] = session.stderr[-12:]
    return report


def _mark(report, name, since):
    now = time.monotonic()
    report["steps"].append({"step": name, "ms": round((now - since) * 1000)})
    return now
