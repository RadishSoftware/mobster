"""The branch and commit a suite tested: `git {branch, head}` in results.json, JUnit's properties and the Mac app's
runs by branch.

    checkout(root)        # {"branch": "onboarding-v2", "head": "217873d1…"}, or None outside a git checkout

A CI checkout is usually a detached HEAD; its branch then comes from the CI's own variable (ENV_BRANCHES), the pull
request's head branch first. Outside a git checkout git never runs, so a Mac without the developer tools never sees
macOS offer to install them. git runs with no prompt and no optional locks, for at most 3 seconds, and a value that
isn't a branch name or a commit is left out.
"""

import os
import re
import subprocess
from pathlib import Path

TIMEOUT = 3
HEAD = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# The CI variables that name the branch a detached checkout is on: GitHub (a pull request's head, then a push), GitLab,
# Bitrise, Buildkite, CircleCI, Xcode Cloud.
ENV_BRANCHES = ("GITHUB_HEAD_REF", "GITHUB_REF_NAME", "CI_COMMIT_REF_NAME", "BITRISE_GIT_BRANCH", "BUILDKITE_BRANCH",
                "CIRCLE_BRANCH", "CI_BRANCH")
MAX_BRANCH = 255


def _repository(root):
    """Whether ``root`` or a folder above it holds .git (a folder, or a worktree's file)."""
    try:
        start = Path(root).resolve()
    except OSError:
        return False
    return any((folder / ".git").exists() for folder in (start, *start.parents))


def _run(argv, cwd):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(["git", "-c", "core.fsmonitor=false", *argv], cwd=str(cwd), capture_output=True,
                                timeout=TIMEOUT, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


def _branch(text):
    text = (text or "").strip()
    if not text or text == "HEAD" or len(text) > MAX_BRANCH or any(ord(c) < 32 or c == "\x7f" for c in text):
        return None
    return text.removeprefix("refs/heads/")


def checkout(root, *, env=None, run=None):
    """{branch, head} of the git checkout ``root`` is in, or None outside one (or when git can't say). ``env`` is
    os.environ and ``run(argv, cwd)`` git's runner (tests pass fakes)."""
    if not _repository(root):
        return None
    env = os.environ if env is None else env
    code, out = (run or _run)(["rev-parse", "HEAD", "--abbrev-ref", "HEAD"], root)   # the commit, then the branch
    lines = out.splitlines() if code == 0 else []
    head = lines[0].strip() if lines and HEAD.fullmatch(lines[0].strip()) else None
    branch = _branch(lines[1]) if len(lines) > 1 else None
    if branch is None and not (env.get("GITHUB_REF_TYPE") and env.get("GITHUB_REF_TYPE") != "branch"):
        branch = next((value for value in (_branch(env.get(name)) for name in ENV_BRANCHES) if value), None)
    if branch is None and head is None:
        return None
    return {"branch": branch, "head": head}
