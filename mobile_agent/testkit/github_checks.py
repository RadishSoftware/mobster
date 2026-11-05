"""Mobster Checks: `mobster test pr`'s verdict as a GitHub check run. A post never changes the verdict or exit code."""

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import pr_report
from .pr import run_git

MODES = ("check", "comment", "off")
GITHUB_MODES_ERROR = "--github is check, comment or off"
NAME = "Mobster"
LABEL = "Mobster Checks"
API = "https://api.github.com"
TOKEN_ROUTE = "/api/connectors/github-checks/token"
REPO = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
SHA = re.compile(r"[0-9a-f]{40}")
REMOTE = re.compile(r"(?:git@github\.com:|ssh://git@github\.com/|https://(?:[^@/\s]+@)?github\.com/)"
                    r"([A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?")
EARLY_S = 60          # a token this close to its expiry is asked for again
TIMEOUT_S = 20
SUMMARY_LIMIT = 65_000  # GitHub refuses a check run's summary over 65,535 characters


class Refused(Exception):
    """Mobster couldn't get a token for the repository: the sentence says why and what to do."""


def ask_mobster(repo):
    """{token, expiresAt, repo} from the running Mobster (connectors/github_app.py). Refused when it gives none."""
    from ..threads.client import Client, NoService, ServiceError
    try:
        return Client.connect(origin="cli").call("POST", TOKEN_ROUTE, {"repo": repo}, timeout=30)
    except NoService:
        raise Refused("Open Mobster, so it can post the verdict on GitHub. Then run the checks again.") from None
    except ServiceError as error:
        install = error.data.get("installUrl")
        raise Refused(f"{error} {install}" if isinstance(install, str) else str(error)) from None


def repository(root, environ, git=run_git):
    """owner/name on github.com: GITHUB_REPOSITORY in a GitHub Actions job, else the ``origin`` remote's."""
    if environ.get("GITHUB_ACTIONS") == "true":
        repo = environ.get("GITHUB_REPOSITORY", "")
        return repo if REPO.fullmatch(repo) else None
    code, url = git(["remote", "get-url", "origin"], root)
    match = REMOTE.fullmatch(url.strip()) if code == 0 else None
    return match.group(1) if match else None


def pull_request(environ):
    """(head commit, number) of the pull request a GitHub Actions job runs for, else (None, None)."""
    if environ.get("GITHUB_ACTIONS") != "true":
        return None, None
    try:
        with open(environ.get("GITHUB_EVENT_PATH", ""), encoding="utf-8") as handle:
            event = json.load(handle)
    except (OSError, ValueError):
        return None, None
    pull = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(pull, dict):
        return None, None
    sha = (pull.get("head") or {}).get("sha") if isinstance(pull.get("head"), dict) else None
    number = pull.get("number")
    return (sha if isinstance(sha, str) and SHA.fullmatch(sha) else None,
            number if isinstance(number, int) else None)


def conclusion(result):
    """success, failure, or neutral when no check covered the PR: mobster test's exit code decides."""
    if result.exit_code:
        return "failure"
    counts = result.pr.get("counts") or {}
    ran = any(counts.get(key) for key in ("passed", "failed", "needs_review", "couldnt_run"))
    return "success" if ran else "neutral"


def output(pr, artifact_url=None):
    title = str(pr.get("headline") or "Mobster checks").removeprefix("Mobster checks: ")
    summary = pr_report.comment(pr, artifact_url=artifact_url).replace(pr_report.MARKER + "\n", "", 1)
    return {"title": title[:200], "summary": summary[:SUMMARY_LIMIT]}


def _seconds(text):
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


class Poster:
    def __init__(self, root, mode="check", progress=None, *, environ=None, git=run_git, mint=ask_mobster,
                 opener=None, clock=time.time):
        self.root, self.mode = root, mode if mode in MODES else "check"
        self.progress = progress or (lambda line: None)
        self.environ = os.environ if environ is None else environ
        self.git, self.mint, self.opener, self.clock = git, mint, opener or urllib.request.urlopen, clock
        self.state = "off" if self.mode == "off" else "ready"   # ready, started, done or off
        self.repo = self.sha = self.number = self.check_id = None
        self._token, self._expires = None, 0.0

    @property
    def actions(self):
        return self.environ.get("GITHUB_ACTIONS") == "true"

    def say(self, text):
        self.progress(f"{LABEL}: {text}")


    # The installation token stays in memory, asked for again before it expires; nothing here logs or prints it.
    def _bearer(self):
        """The token, asked for again when there's none or it's within EARLY_S of expiring. Refused."""
        if self._token is not None and self.clock() < self._expires - EARLY_S:
            return self._token
        self._token = None
        answer = self.mint(self.repo)
        answer = answer if isinstance(answer, dict) else {}
        token, expires, repo = answer.get("token"), _seconds(answer.get("expiresAt")), answer.get("repo")
        if not isinstance(token, str) or not token or expires is None or self.clock() >= expires - EARLY_S:
            raise Refused("Mobster's server gave no usable token. Run the checks again.")
        if isinstance(repo, str) and REPO.fullmatch(repo):
            self.repo = repo   # GitHub's own spelling, after a rename
        self._token, self._expires = token, expires
        return token

    def _github(self, method, path, body=None):
        token = self._bearer()
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mobster-checks"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=headers)
        try:
            with self.opener(request, timeout=TIMEOUT_S) as response:
                text = response.read().decode("utf-8")
        finally:
            token = headers = request = None  # noqa: F841 -- the token's other references go with this frame
        return json.loads(text) if text else None

    def _now(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _failed(self, error, doing):
        """One line for a request that failed: what GitHub or Mobster said, and what to do."""
        if isinstance(error, Refused):
            return self.say(f"nothing posted. {error}")
        if isinstance(error, urllib.error.HTTPError):
            if error.code == 422 and doing == "start":
                return self.say(f"nothing posted: GitHub doesn't have {self.sha[:7]} yet. Push it, then run the "
                                "checks again.")
            return self.say(f"GitHub refused ({error.code}) when Mobster {doing_words(doing)}. Run the checks again.")
        return self.say(f"GitHub didn't answer when Mobster {doing_words(doing)}, so Mobster didn't send it again. "
                        "Run the checks again.")

    def start(self, changes):
        """The check run "Mobster", in progress on the commit the checks test."""
        if self.state != "ready":
            return
        self.state = "done"
        repo = repository(self.root, self.environ, self.git)
        if repo is None:
            return
        head, self.number = pull_request(self.environ)
        sha = head or getattr(changes, "head", None)
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            return
        self.repo, self.sha = repo, sha
        if getattr(changes, "uncommitted", False) and head is None:
            return self.say(f"nothing posted: these checks run on uncommitted changes, so their verdict isn't "
                            f"{sha[:7]}'s. Commit and push, then run them again.")
        try:
            self._bearer()
            run = self._github("POST", f"/repos/{self.repo}/check-runs", {
                "name": NAME, "head_sha": sha, "status": "in_progress", "started_at": self._now(),
                "output": {"title": "Testing the screens this PR changed",
                           "summary": "Mobster is building the app and running the checks that cover them."}})
        except (Refused, urllib.error.URLError, OSError, ValueError) as error:
            return self._failed(error, "start")
        if not isinstance(run, dict) or not isinstance(run.get("id"), int):
            return self.say("nothing posted: GitHub answered without a check run.")
        self.check_id, self.state = run["id"], "started"
        self.say(f"in progress on {self.repo} at {sha[:7]}")

    def finish(self, result):
        """The check run completed with the verdict; with ``comment`` (outside GitHub Actions), the one comment."""
        if self.state != "started":
            return
        self.state = "done"
        artifact_url = details = None
        if self.actions:
            from ..devkit.ci import links
            artifact_url, _ = links("github", self.environ)
            details = artifact_url
        verdict = output(result.pr, artifact_url)
        body = {"status": "completed", "conclusion": conclusion(result), "completed_at": self._now(),
                "output": verdict, **({"details_url": details} if details else {})}
        try:
            self._github("PATCH", f"/repos/{self.repo}/check-runs/{self.check_id}", body)
        except (Refused, urllib.error.URLError, OSError, ValueError) as error:
            return self._failed(error, "finish")
        line = f"posted “{verdict['title']}” on {self.repo} at {self.sha[:7]}"
        if self.mode == "comment" and not self.actions:
            line += self._comment(result.pr)
        self.say(line)

    def abandon(self):
        """The run stopped before a verdict (an error, ctrl+c): the check run says so instead of staying in progress."""
        if self.state != "started":
            return
        self.state = "done"
        try:
            self._github("PATCH", f"/repos/{self.repo}/check-runs/{self.check_id}", {
                "status": "completed", "conclusion": "cancelled", "completed_at": self._now(),
                "output": {"title": "Stopped", "summary": "The checks stopped before a verdict. Run them again."}})
        except (Refused, urllib.error.URLError, OSError, ValueError) as error:
            self._failed(error, "abandon")

    def _comment(self, pr):
        try:
            number = self.number or self._open_pull()
            if number is None:
                return f"; no open pull request has {self.sha[:7]} as its head, so there's no comment"
            from ..devkit.ci import GitHub
            how, _ = GitHub(self._bearer(), self.repo, API, self.opener).sticky(number, pr_report.comment(pr))
        except Refused as error:
            return f"; no comment: {error}"
        except urllib.error.HTTPError as error:
            return f"; GitHub answered {error.code} to the comment"
        except (urllib.error.URLError, OSError, ValueError):
            return "; GitHub didn't answer about the comment, so Mobster didn't send it again"
        return f", and {how} the comment on #{number}"

    def _open_pull(self):
        pulls = self._github("GET", f"/repos/{self.repo}/commits/{self.sha}/pulls") or []
        for pull in pulls if isinstance(pulls, list) else ():
            if pull.get("state") == "open" and (pull.get("head") or {}).get("sha") == self.sha:
                return pull.get("number") if isinstance(pull.get("number"), int) else None
        return None


def doing_words(doing):
    return {"start": "started the check run", "finish": "posted the verdict",
            "abandon": "marked the check run stopped"}.get(doing, "posted")
