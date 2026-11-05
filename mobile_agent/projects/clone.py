"""Cloning a repository by URL into a project folder.

``parse(text)`` reads what the person pasted: an https or SSH URL, `github.com/owner/name`, or `owner/name` (a
GitHub repository). ``Clone`` runs one `git clone` in the background and says how far it got.

The rules: git runs with no shell and with ``--`` before the URL, with no terminal prompt, no Git Credential Manager
window and, unless your git config names its own SSH command, SSH in batch mode, so a clone never waits on a
question nobody sees. Your Mac's git sign-in (the Keychain, an SSH agent) is used as it is; a password or token in the
URL is refused, so it is never kept or shown. A clone that fails is never retried: it says why and what to do, and
the folder it made is removed. A clone has 30 minutes.
"""

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from ..api_errors import APIError
from . import folder as folders

MAX_URL = 2048
CLONE_TIMEOUT = 30 * 60
HOST = r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
REPO_PATH = r"[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*/?"
HTTPS = re.compile(rf"https://(?P<host>{HOST})(?::\d{{1,5}})?/(?P<path>{REPO_PATH})")
SSH = re.compile(rf"ssh://[A-Za-z0-9._-]{{1,64}}@(?P<host>{HOST})(?::\d{{1,5}})?/(?P<path>{REPO_PATH})")
SCP = re.compile(rf"[A-Za-z0-9._-]{{1,64}}@(?P<host>{HOST}):(?P<path>{REPO_PATH})")
SHORT = re.compile(r"(?:github\.com/)?(?P<owner>[A-Za-z0-9-]{1,39})/(?P<name>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?")
CREDENTIALS = re.compile(r"^https?://[^/@]+@")
NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
PROGRESS = re.compile(r"(Counting objects|Compressing objects|Receiving objects|Resolving deltas|Updating files):\s+"
                      r"(\d{1,3})%")
# The share of the bar each phase takes (Receiving is most of a clone's time).
PHASES = {"Counting objects": (0, 5), "Compressing objects": (5, 10), "Receiving objects": (10, 85),
          "Resolving deltas": (85, 95), "Updating files": (95, 100)}
TAIL = 20


def parse(text):
    """(url, host, folder name) for what the person pasted. APIError with what to write otherwise."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_URL or any(c.isspace() for c in text.strip()):
        raise APIError("Paste the repository's URL, such as https://github.com/owner/name, or owner/name.", 400,
                       "invalid_url")
    text = text.strip()
    if CREDENTIALS.match(text):
        raise APIError("Leave the password or token out of the URL. Mobster clones with your Mac's git sign-in (the "
                       "Keychain or your SSH key).", 400, "credentials_in_url")
    short = SHORT.fullmatch(text)
    if short and not text.startswith(("http", "ssh", "git@")) and "@" not in text and ":" not in text:
        text = f"https://github.com/{short.group('owner')}/{short.group('name')}.git"
    for pattern in (HTTPS, SSH, SCP):
        match = pattern.fullmatch(text)
        if match:
            parts = [part for part in match.group("path").split("/") if part]
            if not parts or any(part in (".", "..") or part.startswith("-") for part in parts):
                break
            name = parts[-1][:-4] if parts[-1].endswith(".git") else parts[-1]
            if not NAME.fullmatch(name) or name in (".", ".."):
                break
            return text, match.group("host").lower(), name
    raise APIError("Mobster clones from https and SSH URLs, such as https://github.com/owner/name or "
                   "git@github.com:owner/name.git.", 400, "invalid_url")


def destination(parent, name, *, default_parent, home=None):
    """The folder a clone makes: ``name`` in ``parent`` (the default folder when None, made when missing). APIError
    when the name or the folder won't do, or something is already there."""
    if name is None or not isinstance(name, str) or not NAME.fullmatch(name) or name in (".", "..") \
            or name.startswith("-"):
        raise APIError("Name the folder with letters, numbers, dots, dashes and underscores.", 400, "invalid_name")
    if parent is None:
        folder = Path(default_parent)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise APIError(f"Mobster couldn't make {folders.shown(folder, home)}. Choose another folder to clone "
                           "into.", 403, "unwritable") from None
    else:
        if not isinstance(parent, str) or not Path(parent).expanduser().is_absolute() or "\0" in parent:
            raise APIError("Give the folder to clone into as a full path.", 400, "invalid_path")
        folder = Path(parent).expanduser()
        if not folder.is_dir():
            raise APIError(f"There's no folder at {folders.shown(folder, home)}. Choose the folder again.", 404,
                           "missing")
    folder = folder.resolve()
    if not os.access(folder, os.W_OK | os.X_OK):
        raise APIError(f"Mobster can't write in {folders.shown(folder, home)}. Choose another folder to clone into.",
                       403, "unwritable")
    target = folder / name
    if os.path.lexists(target):
        raise APIError(f"{folders.shown(target, home)} is already there. Add that folder, or clone under another "
                       "name.", 409, "exists", details={"path": str(target)})
    return target


def classify(lines, url, host):
    """(code, sentence) for a clone that failed, from git's last lines."""
    text = "\n".join(lines).lower()
    if "could not create work tree dir" in text or "could not create leading directories" in text:
        return "unwritable", "git couldn't make the folder to clone into. Choose another folder, then clone again."
    if "could not resolve host" in text or "couldn't connect" in text or "could not connect" in text \
            or "network is unreachable" in text or "operation timed out" in text or "connection timed out" in text:
        return "offline", f"Mobster couldn't reach {host}. Check your connection, then clone again."
    if "host key verification failed" in text:
        return "host_key", (f"SSH doesn't know {host} yet. Run ssh -T git@{host} in Terminal once, or clone with "
                            "the https URL.")
    if any(words in text for words in ("repository not found", "authentication failed", "could not read username",
                                       "could not read password", "terminal prompts disabled", "permission denied",
                                       "access denied", "error: 401", "error: 403", "error: 404", "not found",
                                       "could not read from remote repository")):
        return "no_access", (f"{host} didn't let Mobster clone this repository. Check the URL, and that git on this "
                             "Mac is signed in to an account that can read it.")
    if "no space left" in text:
        return "disk_full", "This Mac ran out of disk space during the clone. Free some space, then clone again."
    last = next((line for line in reversed(lines) if line.strip()), "")
    last = re.sub(r"https?://[^/@\s]+@", "https://", last.strip())[:200]
    return "failed", f"git couldn't clone {url}" + (f": {last}" if last else ".")


class Clone:
    """One `git clone` in the background. ``on_change(clone)`` is told as it goes (at most 4 times a second) and
    once when it ends; ``on_done(clone)`` runs on the clone's thread after git succeeded, before the end is told,
    and may set ``error``. ``popen`` is subprocess.Popen (tests pass a fake)."""

    def __init__(self, identifier, url, host, target, *, on_change=None, on_done=None, popen=subprocess.Popen,
                 clock=time.time, timeout=CLONE_TIMEOUT, home=None, env=None, signed_in=None):
        self.id, self.url, self.host, self.target = identifier, url, host, Path(target)
        self.state, self.phase, self.percent = "cloning", None, 0
        self.code = self.error = self.project_id = None
        self.started_at, self.ended_at = clock(), None
        self._on_change, self._on_done, self._popen, self._clock = on_change, on_done, popen, clock
        self._timeout, self._home = timeout, home
        # git's own environment for a sign-in (github.clone_env: never argv, never the URL), and whose sign-in it is.
        self._extra_env, self._signed_in = dict(env or {}), signed_in
        self._process = None
        self._made = None        # identity of the directory this job made; cleanup never owns a replacement
        self._canceled = False
        self._told = 0.0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name=f"mobster-clone-{identifier}", daemon=True)

    def public(self):
        return {"id": self.id, "url": self.url, "path": str(self.target),
                "shownPath": folders.shown(self.target, self._home),
                "name": self.target.name, "state": self.state, "phase": self.phase, "percent": self.percent,
                "code": self.code, "error": self.error, "projectId": self.project_id,
                "startedAt": round(self.started_at * 1000),
                "endedAt": round(self.ended_at * 1000) if self.ended_at else None}

    def start(self):
        self._thread.start()

    def join(self, timeout=None):
        self._thread.join(timeout)

    def cancel(self):
        """Stop the clone; the folder it made is removed. False when it had already ended."""
        with self._lock:
            if self.state != "cloning":
                return False
            self._canceled = True
            process = self._process
        if process is not None:
            _stop(process)
        return True

    def _env(self):
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_OPTIONAL_LOCKS": "0",
               **self._extra_env}
        if not _git_config("core.sshCommand") and "GIT_SSH_COMMAND" not in os.environ:
            env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=20"
        return env

    def _tell(self, force=False):
        now = self._clock()
        if self._on_change is not None and (force or now - self._told >= 0.25):
            self._told = now
            try:
                self._on_change(self)
            except Exception:  # noqa: BLE001 -- telling never stops a clone
                pass

    def _run(self):
        lines = []
        try:
            if not folders.git_ready():
                raise FileNotFoundError("git")
            env = self._env()
            with self._lock:
                if self._canceled:
                    raise _Canceled
                self.target.mkdir()
                made = self.target.stat()
                self._made = (made.st_dev, made.st_ino)
                self._process = self._popen(
                    ["git", "-c", "core.fsmonitor=false", "clone", "--progress", "--", self.url, str(self.target)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
                    start_new_session=True)
            deadline = threading.Timer(self._timeout, self._expire)
            deadline.daemon = True
            deadline.start()
            try:
                for line in _lines(self._process.stderr):
                    self._progress(line)
                    lines.append(line)
                    del lines[:-TAIL]
                code = self._process.wait()
            finally:
                deadline.cancel()
                self._process.stderr.close()
            if self._canceled:
                raise _Canceled
            if code != 0:
                if self._expired:
                    self._fail("timeout", "The clone took longer than 30 minutes, so Mobster stopped it. Clone it in "
                                          "Terminal, then add the folder.")
                else:
                    code, sentence = classify(lines, self.url, self.host)
                    if code == "no_access" and self._signed_in:
                        sentence = (f"{self.host} didn't let Mobster clone this repository with your {self._signed_in} "
                                    "sign-in. Check the repository, or sign in in Connectors with an account that can "
                                    "read it.")
                    self._fail(code, sentence)
                return
            self.phase, self.percent = None, 100
            if self._on_done is not None:
                try:
                    self._on_done(self)
                except Exception:  # noqa: BLE001 -- git succeeded: the folder stays whatever happens next
                    self.code = self.code or "refused"
                    where = folders.shown(self.target, self._home)
                    self.error = self.error or (f"The repository is cloned at {where}, but Mobster couldn't add it. "
                                                "Add the folder from Add project.")
            self._end("failed" if self.error else "done")
        except _Canceled:
            self._remove()
            self._end("canceled")
        except FileExistsError:
            self._fail("exists", f"{folders.shown(self.target, self._home)} is already there. Add that folder, or "
                                 "clone under another name.")
        except FileNotFoundError:
            self._fail("no_git", "Mobster needs git to clone. Install the Xcode command line tools "
                                 "(xcode-select --install), then clone again.")
        except Exception:  # noqa: BLE001 -- a clone that broke says so, never retries
            self._fail("failed", f"Mobster couldn't clone {self.url}. Try again, or clone it in Terminal and add "
                                 "the folder.")

    _expired = False

    def _expire(self):
        self._expired = True
        if self._process is not None:
            _stop(self._process)

    def _progress(self, line):
        match = PROGRESS.search(line)
        if not match:
            return
        phase, share = match.group(1), min(int(match.group(2)), 100)
        start, end = PHASES[phase]
        self.phase = phase
        self.percent = max(self.percent, start + (end - start) * share // 100)
        self._tell()

    def _remove(self):
        if not self._made:
            return
        try:
            current = self.target.lstat()
            if (current.st_dev, current.st_ino) == self._made and self.target.is_dir() and not self.target.is_symlink():
                shutil.rmtree(self.target)
        except OSError:
            pass

    def _fail(self, code, sentence):
        self._remove()
        self.code, self.error = code, sentence
        self._end("failed")

    def _end(self, state):
        with self._lock:
            self.state, self.ended_at = state, self._clock()
            if state != "cloning":
                self._process = None
        self._tell(force=True)


class _Canceled(Exception):
    pass


def _lines(stream):
    """git's progress lines: it ends them with \\r while a phase goes on and \\n when it is done."""
    buffer = b""
    while True:
        chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
        if not chunk:
            break
        buffer += chunk
        *done, buffer = re.split(rb"[\r\n]", buffer)
        for line in done:
            if line:
                yield line.decode("utf-8", errors="replace")
        buffer = buffer[-8192:]
    if buffer:
        yield buffer.decode("utf-8", errors="replace")


def _stop(process):
    """End git and what it started (git-remote-https, ssh): TERM to its process group, KILL after 2 seconds."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, AttributeError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    except Exception:  # noqa: BLE001 -- a fake process in tests
        pass


def _git_config(key):
    try:
        result = subprocess.run(["git", "config", "--global", "--get", key], capture_output=True, timeout=3,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.decode("utf-8", errors="replace").strip() or None
