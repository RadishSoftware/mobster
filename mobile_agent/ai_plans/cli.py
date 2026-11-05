"""The person's own coding agent CLI as a Mobster task's agent: it signs its own requests;
Mobster never reads its credentials."""

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

SEARCH_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.npm-global/bin", "~/.bun/bin",
               "~/.volta/bin", "~/.yarn/bin", "~/.cargo/bin", "~/bin", "~/.claude/local", "~/.codex/bin",
               "/Applications/Codex.app/Contents/Resources", "~/Library/pnpm")
SHELL_PATH_SECONDS = 4.0
STATUS_SECONDS = 8.0
STDERR_KEEP = 8000
STOP_GRACE = 2.5
_shell_path = {"value": None, "at": 0.0}
_shell_lock = threading.Lock()


def login_shell_path(env=None, refresh=False):
    """The PATH a new Terminal window gets (the login shell's), cached for 10 minutes; "" when it can't be read."""
    env = os.environ if env is None else env
    with _shell_lock:
        if not refresh and _shell_path["value"] is not None and time.monotonic() - _shell_path["at"] < 600:
            return _shell_path["value"]
        shell = env.get("SHELL") or "/bin/zsh"
        value = ""
        try:
            done = subprocess.run([shell, "-l", "-c", 'printf "%s" "$PATH"'], capture_output=True, text=True,
                                  timeout=SHELL_PATH_SECONDS, stdin=subprocess.DEVNULL,
                                  env={k: v for k, v in env.items() if k in ("HOME", "USER", "LOGNAME", "SHELL",
                                                                              "TMPDIR", "LANG")})
            if done.returncode == 0:
                value = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
        except (OSError, subprocess.SubprocessError, IndexError):
            value = ""
        _shell_path.update(value=value, at=time.monotonic())
        return value


def _nvm_dirs():
    root = Path("~/.nvm/versions/node").expanduser()
    try:
        return [str(p / "bin") for p in sorted(root.iterdir(), reverse=True) if (p / "bin").is_dir()]
    except OSError:
        return []


def search_path(env=None):
    """Every directory Mobster looks in for a CLI, in order, without duplicates."""
    env = os.environ if env is None else env
    dirs = []
    for part in (env.get("PATH") or "").split(os.pathsep):
        dirs.append(part)
    dirs += [str(Path(d).expanduser()) for d in SEARCH_DIRS] + _nvm_dirs()
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def find_binary(name, env=None, override=None):
    """The executable ``name`` (an absolute path), or None."""
    env = os.environ if env is None else env
    if override:
        path = Path(override).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which(name, path=os.pathsep.join(search_path(env)))
    if found:
        return found
    shell = login_shell_path(env)
    return shutil.which(name, path=shell) if shell else None


# Mobster's secrets no agent CLI gets; each agent also drops what would move it off its plan.
MOBSTER_SECRETS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "TYPESAFE_API_KEY",
                   "TEXT_MODEL_API_KEY", "OPENROUTER_API_KEY", "MOBSTER_API_TOKEN", "MOBSTER_API_TOKEN_FILE",
                   "MOBSTER_APP_SESSION", "MOBSTER_MCP_TOKEN", "MOBSTER_LICENSE_KEY")


def child_env(binary, extra=None, env=None, drop=()):
    """The environment a CLI runs with."""
    env = dict(os.environ if env is None else env)
    for key in (*MOBSTER_SECRETS, *drop):
        env.pop(key, None)
    parts = [str(Path(binary).resolve().parent), str(Path(binary).parent)]
    shell = login_shell_path(env)
    parts += shell.split(os.pathsep) if shell else []
    parts += search_path(env)
    seen, path = set(), []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            path.append(part)
    env["PATH"] = os.pathsep.join(path)
    env.setdefault("TERM", "dumb")
    env["NO_COLOR"] = "1"
    env.update(extra or {})
    return env


def run_quietly(argv, env, timeout=STATUS_SECONDS):
    """(exit code, stdout, stderr) of a short command such as ``codex login status``; (None, "", why)
    when it couldn't run."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        return None, "", "timed out"
    except OSError as error:
        return None, "", str(error)


def count(value):
    """A token count as a non-negative int; 0 for anything else (a missing or odd figure is no tokens)."""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0


def usage_counts(input_tokens, output_tokens, cache_read=0, cache_write=0, *, input_includes_cache=True,
                 reasoning=None):
    """One call's (or one run's) tokens in Mobster's convention."""
    read, write = count(cache_read), count(cache_write)
    prompt = count(input_tokens) + (0 if input_includes_cache else read + write)
    out = {"input_tokens": prompt, "output_tokens": count(output_tokens), "cache_read_tokens": read,
           "cache_write_tokens": write}
    out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
    if reasoning is not None:
        out["reasoning_output_tokens"] = count(reasoning)
    return out


LIMIT_STATUSES = ("allowed", "allowed_warning", "rejected")


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def limit_signal(status, windows=None, *, kind=None, resets_at=None):
    """An agent's plan-limit reading in the shape the ledger keeps."""
    if status not in LIMIT_STATUSES:
        return None
    kept = {}
    for name, window in (windows.items() if isinstance(windows, dict) else ()):
        if isinstance(window, dict) and isinstance(name, str) and len(kept) < 8:
            kept[name[:40]] = {"utilization": _number(window.get("utilization")),
                               "resetsAt": _number(window.get("resetsAt"))}
    return {"status": status, "type": kind if isinstance(kind, str) else None, "resetsAt": _number(resets_at),
            "windows": kept}


@dataclass
class Command:
    """What an agent runs for one call: the argv, and the model Mobster asked for when the stream won't name it."""
    argv: list
    model: str | None = None


@dataclass
class AgentEvent:
    """One thing a CLI said, in Mobster's terms."""
    kind: str
    text: str = ""
    session_id: str | None = None
    tool: str | None = None
    server: str | None = None
    arguments: dict | None = None
    ok: bool = True
    usage: dict = field(default_factory=dict)
    code: str | None = None
    fatal: bool = False
    model: str | None = None
    resets_at: float | None = None
    call_id: str | None = None
    limits: dict | None = None
    cost_usd: float | None = None
    models: dict | None = None
    latency_ms: int | None = None


# Limit wording differs by CLI version, so it is matched loosely.
LIMIT_WORDS = re.compile(r"usage limit|hit your (?:\w+ )*limit|limit reached|rate limit|quota|too many requests|"
                         r"resource.?exhausted|you(?:'|’)ve reached|out of credits|spend cap|upgrade to plus|"
                         r"try again (?:in|at|after)", re.I)
SIGNED_OUT_WORDS = re.compile(r"not logged in|log ?in again|sign ?in again|please (?:log|sign) ?in|"
                              r"unauthori[sz]ed|401|token (?:has )?expired|invalid.*(?:token|credentials)|/login",
                              re.I)


def classify(text):
    """usage_limit, signed_out or other, for an error's text."""
    text = str(text or "")
    if LIMIT_WORDS.search(text):
        return "usage_limit"
    if SIGNED_OUT_WORDS.search(text):
        return "signed_out"
    return "other"


class AgentProcess:
    """One CLI run: started in its own process group (so Stop reaches whatever it starts)."""

    def __init__(self, argv, *, env, cwd, parse, stdin_text=None, log=None):
        self.argv, self.env, self.cwd, self.parse = list(argv), env, cwd, parse
        self.stdin_text = stdin_text
        self.log = log or (lambda message: None)
        self.process = None
        self.stderr = []
        self.stopped = threading.Event()
        self.started_at = None
        self._stderr_thread = None

    def start(self):
        self.started_at = time.monotonic()
        self.process = subprocess.Popen(
            self.argv, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE if self.stdin_text is not None
            else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            bufsize=1, text=True, encoding="utf-8", errors="replace")
        if self.stdin_text is not None:
            try:
                self.process.stdin.write(self.stdin_text)
                self.process.stdin.close()
            except OSError:
                pass
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True, name="mobster-agent-stderr")
        self._stderr_thread.start()
        return self

    def _drain_stderr(self):
        size = 0
        try:
            for line in self.process.stderr:
                self.stderr.append(line)
                size += len(line)
                while size > STDERR_KEEP and len(self.stderr) > 1:
                    size -= len(self.stderr.pop(0))
        except (OSError, ValueError):
            pass
        finally:
            self.process.stderr.close()

    def stderr_text(self):
        return "".join(self.stderr)[-STDERR_KEEP:]

    def events(self):
        """AgentEvents as the CLI prints them, until it exits or is stopped. A line that isn't JSON is skipped."""
        for line in self.process.stdout:
            line = line.strip()
            if not line or line[0] not in "{[":
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            try:
                parsed = self.parse(obj) or ()
            except Exception as error:  # noqa: BLE001 -- one odd event never ends the run
                self.log(f"an event couldn't be read ({type(error).__name__})")
                continue
            for event in parsed:
                yield event
        self.process.stdout.close()
        self.wait()

    def wait(self, timeout=None):
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    @property
    def returncode(self):
        return None if self.process is None else self.process.poll()

    def stop(self, grace=STOP_GRACE):
        """End the CLI and everything it started: SIGTERM to its process group, SIGKILL after ``grace``."""
        self.stopped.set()
        process = self.process
        if process is None or process.poll() is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                process.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue


class CliAgent:
    """What every agent CLI adapter provides."""

    id = ""                 # the run's engine: codex, claude_code, gemini
    name = ""               # "Codex"
    binary = ""             # "codex"
    plan = ""               # whose plan, in the person's words: "ChatGPT"
    vendor = ""             # "OpenAI"
    install_url = ""
    install_command = ""
    sign_in_command = ""
    path_env = ""           # MOBSTER_CODEX_PATH: a path the person chose
    model_env = ""          # MOBSTER_CODEX_MODEL
    drop_env = ()           # keys the CLI must not see (an API key would bill per token instead of the plan)

    def __init__(self, env=None):
        self.env = os.environ if env is None else env
        self._status = None
        self._status_at = 0.0
        self._lock = threading.Lock()

    def locate(self):
        return find_binary(self.binary, self.env, self.env.get(self.path_env) or None)

    def status(self, max_age=60.0):
        """{id, name, plan, installed, signedIn, path, version, account, billing, hint, ready}."""
        with self._lock:
            if self._status is not None and time.monotonic() - self._status_at < max_age:
                return dict(self._status)
        path = self.locate()
        state = {"id": self.id, "name": self.name, "plan": self.plan, "vendor": self.vendor, "installed": bool(path),
                 "signedIn": False, "path": path, "version": None, "account": None, "billing": None,
                 "hint": None, "ready": False, "installUrl": self.install_url,
                 "installCommand": self.install_command, "signInCommand": self.sign_in_command}
        if not path:
            state["hint"] = f"Install {self.name}"
        else:
            try:
                state.update(self.read_status(path))
            except Exception as error:  # noqa: BLE001
                state.update(signedIn=False, hint=f"Couldn't check {self.name} ({type(error).__name__})")
            self.version = state.get("version")
            if not state["signedIn"] and not state["hint"]:
                state["hint"] = f"Sign in to {self.name}"
            state["ready"] = bool(state["signedIn"])
        with self._lock:
            self._status, self._status_at = dict(state), time.monotonic()
        return state

    def forget_status(self):
        with self._lock:
            self._status = None

    def read_status(self, path):  # pragma: no cover - each agent's
        raise NotImplementedError

    def argv(self, path, prompt, *, mcp_url, token_env, instructions, workdir, resume=None, model=None):
        raise NotImplementedError

    def parse(self, obj):
        raise NotImplementedError

    version = None

    def extra_env(self, token_env, token):
        return {token_env: token}

    resumes = True             # a follow-up resumes the agent's own session
    signed_out_codes = ()      # exit codes that mean "not signed in"

    def stdin(self, prompt, instructions=None):
        """What to write to the CLI's stdin (the prompt, so it stays out of process lists), or None."""
        return None

    def parser(self):
        """The event parser for one run (an agent whose stream needs state across lines makes a fresh one)."""
        return self.parse

    def prepare(self, workdir, mcp_url, token):
        """Files a run needs beside its command line (Gemini's project settings); returns a path to remove after."""
        return None

    def model(self):
        chosen = (self.env.get(self.model_env) or "").strip()
        return chosen if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,99}", chosen) else None

    def limit_sentence(self, resets_at=None):
        """The usage-limit error in plain words, saying when it resets when the CLI said."""
        when = ""
        if resets_at:
            when = " It resets " + time.strftime("at %-I:%M %p", time.localtime(resets_at)) \
                if resets_at - time.time() < 20 * 3600 else " It resets " + time.strftime(
                    "on %A at %-I:%M %p", time.localtime(resets_at))
            when += "."
        return f"You've reached your {self.plan} plan's usage limit in {self.name}.{when}"

    def signed_out_sentence(self):
        return (f"{self.name} isn't signed in. Open Terminal, run {self.sign_in_command}, then try again."
                if self.sign_in_command else f"Sign in to {self.name}, then try again.")
