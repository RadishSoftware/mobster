"""Whether this copy of Mobster is the one we signed, as the frozen agent checks it itself (the shell's own check is
desktop/src-tauri/src/genuine.rs). With ``premiumModel`` on no plan holds a changed copy back, and MCP clients and the
terminal start this agent as processes of their own, which the shell's check never reaches.

It runs only in the frozen agent with the flag on, once per process: one ``codesign`` of the whole app, about 0.2 to
0.3 s on the installed app (the shell's check of the same bundle took 0.20 to 0.28 s, 3 Nov 2026, warm cache). Only an
ad hoc build, whose team file is empty and whose agent carries no team, is never refused. A copy fails as CHANGED when
codesign ran and rejected the app, and as UNCHECKED when anything else stands in the way: no team file, a team that
isn't one, an empty team file beside an agent a team signed, no app around the agent, and a check that errors or runs
out of time."""

import re
import subprocess
import sys
import threading
from pathlib import Path

from . import build_identity, premium_model

# build-sidecar.sh writes the team that signed the build here and mobster-agent.spec bundles it (Contents/Resources,
# linked from sys._MEIPASS). It's empty for an ad hoc build.
TEAM_FILE = "signing-team"
CODESIGN = "/usr/bin/codesign"
# The page offers a fresh copy after the same 20 s (account-gate.tsx VERIFY_STUCK_MS); codesign is killed then.
CHECK_SECONDS = 20.0
CHANGED = "changed"  # codesign ran and rejected the app
UNCHECKED = "unchecked"  # anything else that keeps the copy from passing
_lock = threading.Lock()
_answer = []  # this process's answer once asked: (CHANGED or UNCHECKED, what failed), or None


def team_of(identity):
    """The team in a Developer ID signing identity, by team.rs's rule ("Developer ID Application: Radish Retail, LLC
    (ABCDE12345)" gives ABCDE12345), else "". build-sidecar.sh writes it into the bundle."""
    rest = identity.strip()
    prefix = "Developer ID Application:"
    if not rest.startswith(prefix):
        return ""
    rest = rest[len(prefix):].rstrip()
    if not rest.endswith(")") or "(" not in rest:
        return ""
    team = rest[:-1].rsplit("(", 1)[1]
    return team if re.fullmatch(r"[A-Z0-9]{10}", team) else ""


def requirement(team):
    """The code-signing requirement a genuine copy meets: Apple's chain, our team (genuine.rs › requirement)."""
    return f'anchor apple generic and certificate leaf[subject.OU] = "{team}"'


def failure():
    """None when this copy passed its check, and in a source checkout or with ``premiumModel`` off, where none runs;
    else CHANGED or UNCHECKED (problem)."""
    if not build_identity.app_bundle() or not premium_model.enabled():
        return None
    with _lock:
        if not _answer:
            _answer.append(problem())
            if _answer[0] is not None:
                print(f"mobster: this copy fails its signature check ({_answer[0][1]})", file=sys.stderr, flush=True)
        return None if _answer[0] is None else _answer[0][0]


def problem(team_file=None, executable=None, run=None):
    """None when the app around ``executable`` passes, or this is an ad hoc build; else (CHANGED or UNCHECKED, what
    failed)."""
    if team_file is None:
        folder = getattr(sys, "_MEIPASS", None)
        if not folder:
            return UNCHECKED, "no bundle folder"
        team_file = Path(folder) / TEAM_FILE
    try:
        team = Path(team_file).read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return UNCHECKED, "this build's signing team can't be read"
    path = Path(executable or sys.executable).resolve()
    if not team:
        return own_team_problem(path, run)
    if not re.fullmatch(r"[A-Z0-9]{10}", team):
        return UNCHECKED, "the build's signing team isn't one"
    app = next((parent for parent in path.parents if parent.suffix == ".app" and (parent / "Contents").is_dir()), None)
    if app is None:
        return UNCHECKED, "no app around the agent"
    # Strict and deep, as genuine.rs's STRICT_VALIDATE and CHECK_NESTED_CODE. codesign checks no revocation unless
    # asked (--check-revocation), so it stays offline, as NO_NETWORK_ACCESS does.
    code, said = codesign(["--verify", "--strict", "--deep", f"-R={requirement(team)}", str(app)], run)
    if code is None:
        return UNCHECKED, said
    if code != 0:
        lines = said.strip().splitlines()
        return CHANGED, f"codesign: {lines[0][:200] if lines else f'exit {code}'}"
    return None


def own_team_problem(path, run=None):
    """With an empty team file: None when the agent at ``path`` carries no team, as an ad hoc build's doesn't (the shell
    skips its check then too); else why it refuses. A Developer ID build always writes its team (finish-release.sh
    checks), so an empty file beside a team's signature was emptied, and a signature that can't be read refuses."""
    code, said = codesign(["-d", "--verbose=2", str(path)], run)
    if code is None:
        return UNCHECKED, said
    found = re.findall(r"^TeamIdentifier=(.*)$", said, re.M)
    if code != 0 or len(found) != 1:
        return UNCHECKED, "the agent's own signature can't be read"
    if found[0].strip() == "not set":
        return None
    return UNCHECKED, "the build's signing team is empty, but a team signed the agent"


def codesign(arguments, run=None):
    """(codesign's exit code, what it printed), or (None, why it couldn't run): it's killed after CHECK_SECONDS and
    never waits on stdin."""
    try:
        done = (run or subprocess.run)([CODESIGN, *arguments], capture_output=True, text=True, errors="replace",
                                       timeout=CHECK_SECONDS, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, f"codesign took over {CHECK_SECONDS:.0f} s"
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"codesign couldn't run ({type(error).__name__})"
    return done.returncode, f"{done.stderr or ''}{done.stdout or ''}"
