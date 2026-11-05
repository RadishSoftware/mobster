#!/usr/bin/env python3
"""The Mobster test action's steps (action.yml), in the standard library only, so it runs on the runner's own
python3 (macOS ships 3.9).

    python3 action.py guard     refuse a device or TestFlight app the runner's owner didn't allow
    python3 action.py run       run `mobster test pr`, and set the step's outputs
    python3 action.py comment   create or update the one sticky PR comment, and the job summary
    python3 action.py finish    exit with mobster's code when fail-on-failure is true

Inputs arrive as INPUT_* environment variables (set in action.yml); GitHub's own variables (GITHUB_REPOSITORY,
GITHUB_EVENT_PATH, GITHUB_OUTPUT, ...) are read as GitHub sets them.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request

MARKER = "<!-- mobster:test-pr -->"
API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
PAGE = 100
MAX_PAGES = 30


def env(name, default=""):
    return os.environ.get(name, default).strip()


def truthy(text):
    return str(text).strip().lower() in ("1", "true", "yes", "on")


def items(text):
    """A comma- or newline-separated list, in order."""
    return [item.strip() for item in re.split(r"[,\n]", text or "") if item.strip()]


def listed(text):
    """items(text), folded for comparison."""
    return {item.casefold() for item in items(text)}


def extra_args(environ):
    args = shlex.split(environ.get("INPUT_ARGS", ""))
    if any(arg.split("=", 1)[0] in ("--device", "--testflight") for arg in args):
        raise ValueError("Use the device and testflight inputs instead of putting --device or --testflight in args.")
    return args


def set_output(name, value, environ=None):
    path = (os.environ if environ is None else environ).get("GITHUB_OUTPUT", "").strip()
    if not path:
        return
    value = str(value)
    with open(path, "a", encoding="utf-8") as handle:
        if "\n" in value:
            handle.write(f"{name}<<MOBSTER_EOF\n{value}\nMOBSTER_EOF\n")
        else:
            handle.write(f"{name}={value}\n")


def error(message):
    print(f"::error title=Mobster::{message}", flush=True)


def warning(message):
    print(f"::warning title=Mobster::{message}", flush=True)


def guard(environ=None):
    """0 when the inputs only reach what the runner's owner allowed, else 1 with the reason.

    A pull request can change the workflow file, so a phone or a TestFlight app named in it is not enough: the
    runner's own environment (its .env file, which a PR can't touch) has to list it too."""
    environ = os.environ if environ is None else environ
    try:
        extra_args(environ)
    except ValueError as problem:
        error(str(problem))
        return 1
    devices = items(environ.get("INPUT_DEVICE", ""))
    testflight = listed(environ.get("INPUT_TESTFLIGHT", ""))
    if testflight and not devices:
        error("testflight goes with device: a TestFlight build is on an iPhone, not a simulator.")
        return 1
    allowed = listed(environ.get("MOBSTER_RUNNER_DEVICES", ""))
    refused = [device for device in devices if device.casefold() not in allowed]
    if refused:
        phones = ("the iPhone " if len(refused) == 1 else "the iPhones ") + ", ".join(f'"{name}"' for name in refused)
        error(f"This runner doesn't allow {phones}, so nothing ran. Its owner lists the phones it may test in "
              "MOBSTER_RUNNER_DEVICES, in the runner's .env file.")
        return 1
    if testflight:
        allowed = listed(environ.get("MOBSTER_RUNNER_TESTFLIGHT", ""))
        missing = sorted(testflight - allowed)
        if missing:
            error(f"This runner doesn't allow the TestFlight build of {', '.join(missing)}. Its owner lists them "
                  "in MOBSTER_RUNNER_TESTFLIGHT, in the runner's .env file.")
            return 1
    return 0


def argv_for(environ=None):
    environ = os.environ if environ is None else environ
    argv = [environ.get("MOBSTER_BIN", "mobster") or "mobster", "test", "pr", "--json"]
    base = environ.get("INPUT_BASE", "").strip()
    if base:
        argv += ["--base", base]
    for sim in [item.strip() for item in environ.get("INPUT_SIM", "").splitlines() if item.strip()]:
        argv += ["--sim", sim]
    for device in items(environ.get("INPUT_DEVICE", "")):
        argv += ["--device", device]
    for bundle in items(environ.get("INPUT_TESTFLIGHT", "")):
        argv += ["--testflight", bundle]
    for name, flag in (("INPUT_SCHEME", "--scheme"), ("INPUT_PROJECT", "--project"), ("INPUT_DRAFTS", "--drafts")):
        if environ.get(name, "").strip():
            argv += [flag, environ[name].strip()]
    if not truthy(environ.get("INPUT_BUILD", "true")):
        argv.append("--no-build")
    if truthy(environ.get("INPUT_TESTED-WITH", environ.get("INPUT_TESTED_WITH", "false"))):
        argv.append("--tested-with")
    if environ.get("MOBSTER_TEAM_KEY", "").strip():
        argv.append("--share")  # the team's hosted report, linked from the comment (Team plan)
    argv += extra_args(environ)
    return argv


def run(environ=None, runner=None):
    """Run mobster; set exit-code, headline and report-dir. Always 0: the job's result is `finish`'s."""
    environ = os.environ if environ is None else environ
    if guard(environ):
        set_output("exit-code", 3, environ)
        set_output("headline", "Mobster checks couldn't run", environ)
        set_output("report-dir", "", environ)
        return 0
    argv = argv_for(environ)
    cwd = environ.get("INPUT_WORKING-DIRECTORY", environ.get("INPUT_WORKING_DIRECTORY", "")).strip() or "."
    print("$ " + " ".join(shlex.quote(item) for item in argv), flush=True)
    if runner is None:
        def runner(argv, cwd):
            try:
                done = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, text=True)
            except FileNotFoundError:
                return 127, ""
            return done.returncode, done.stdout
    code, out = runner(argv, cwd)
    set_output("exit-code", code, environ)
    try:
        pr = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    except ValueError:
        pr = None
    if code == 127:
        error("mobster isn't installed on this runner. Install Mobster for Mac on it (https://mobster.dev/download), "
              "or set MOBSTER_BIN to the mobster command.")
    if not isinstance(pr, dict) or not pr.get("suite"):
        set_output("headline", "Mobster checks couldn't run", environ)
        set_output("report-dir", "", environ)
        if code not in (0, 127):
            error(f"mobster test pr exited {code} before it wrote a report. The log above says why.")
        return 0
    folder = os.path.join(cwd, ".mobster", "test-results", pr["suite"])
    root = _project_root(cwd)
    if root:
        folder = os.path.join(root, ".mobster", "test-results", pr["suite"])
    set_output("headline", pr.get("headline") or "", environ)
    set_output("report-dir", folder, environ)
    return 0


def _project_root(start):
    folder = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(folder, ".mobster")):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


FILE_REF = re.compile(r"`([^`\n]+\.(?:jpg|gif|mp4))`")


HOSTED = "https://console.mobster.dev/r/"


def link_artifacts(markdown, url):
    """The local comment with links to the uploaded report: each frame and GIF name, and a report line. A comment that
    already links the team's hosted report (``--share``) is left as it is."""
    if not url or HOSTED in markdown:
        return markdown
    text = FILE_REF.sub(lambda match: f"[{match.group(1)}]({url})", markdown)
    text = text.replace(", in the suite folder.", f", [in the report]({url}).")
    line = f"[Report, frames and GIFs]({url})"
    if "<sub>Tested with" in text:
        head, _, tail = text.partition("<sub>Tested with")
        return head.rstrip("\n") + f"\n\n{line}\n\n<sub>Tested with" + tail
    return text.rstrip("\n") + f"\n\n{line}\n"


def failed_run_comment(environ):
    """The comment when mobster wrote no report: say so, and point at the job's log."""
    server = environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = f"{server}/{environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{environ.get('GITHUB_RUN_ID', '')}"
    return (f"{MARKER}\n### Mobster checks couldn't run\n\nThe [job's log]({run_url}) says why "
            f"(exit code {environ.get('MOBSTER_EXIT_CODE', '?')}).\n")


class GitHub:
    def __init__(self, token, repository, opener=None):
        self.token, self.repository = token, repository
        self.opener = opener or urllib.request.urlopen

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{API}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mobster-test-action",
            **({"Content-Type": "application/json"} if data is not None else {})})
        with self.opener(request, timeout=30) as response:
            text = response.read().decode("utf-8")
        return json.loads(text) if text else None

    def sticky(self, number, body):
        """Update the comment that carries MARKER, else create it. Returns ("updated" | "created", url)."""
        for page in range(1, MAX_PAGES + 1):
            items = self.call("GET", f"/repos/{self.repository}/issues/{number}/comments?per_page={PAGE}"
                                     f"&page={page}") or []
            for item in items:
                if MARKER in (item.get("body") or ""):
                    done = self.call("PATCH", f"/repos/{self.repository}/issues/comments/{item['id']}",
                                     {"body": body})
                    return "updated", (done or {}).get("html_url") or item.get("html_url")
            if len(items) < PAGE:
                break
        done = self.call("POST", f"/repos/{self.repository}/issues/{number}/comments", {"body": body})
        return "created", (done or {}).get("html_url")


def pr_number(environ):
    path = environ.get("GITHUB_EVENT_PATH", "")
    try:
        with open(path, encoding="utf-8") as handle:
            event = json.load(handle)
    except (OSError, ValueError):
        return None
    number = (event.get("pull_request") or {}).get("number")
    if number is None and (event.get("issue") or {}).get("pull_request"):
        number = event["issue"].get("number")
    return number if isinstance(number, int) else None


def comment(environ=None, opener=None):
    environ = os.environ if environ is None else environ
    folder = environ.get("MOBSTER_REPORT_DIR", "").strip()
    url = environ.get("MOBSTER_ARTIFACT_URL", "").strip()
    path = os.path.join(folder, "pr-comment.md") if folder else ""
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            body = link_artifacts(handle.read(), url)
    else:
        body = failed_run_comment(environ)
    summary = environ.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(body.replace(MARKER + "\n", "") + "\n")
    if not truthy(environ.get("INPUT_COMMENT", "true")):
        return 0
    number = pr_number(environ)
    if number is None:
        print("Not a pull request: the summary is in the job's summary, with no PR comment.", flush=True)
        return 0
    token = environ.get("INPUT_GITHUB-TOKEN", environ.get("INPUT_GITHUB_TOKEN", "")).strip()
    if not token:
        warning("No github-token, so Mobster didn't comment on the PR. The summary is in the job's summary.")
        return 0
    try:
        how, link = GitHub(token, environ.get("GITHUB_REPOSITORY", ""), opener).sticky(number, body)
    except urllib.error.HTTPError as problem:
        if problem.code in (403, 404):
            warning("Mobster couldn't comment on the PR: the token can't write to it (a PR from a fork gets a "
                    "read-only token). Give the job `permissions: pull-requests: write`. The summary is in the "
                    "job's summary.")
        else:
            warning(f"Mobster couldn't comment on the PR: GitHub answered {problem.code}.")
        return 0
    except (urllib.error.URLError, OSError, ValueError) as problem:
        warning(f"Mobster couldn't reach GitHub to comment on the PR ({type(problem).__name__}).")
        return 0
    print(f"PR comment {how}: {link}", flush=True)
    return 0


def finish(environ=None):
    environ = os.environ if environ is None else environ
    try:
        code = int(environ.get("MOBSTER_EXIT_CODE", "3") or "3")
    except ValueError:
        code = 3
    if not truthy(environ.get("INPUT_FAIL-ON-FAILURE", environ.get("INPUT_FAIL_ON_FAILURE", "true"))):
        return 0
    if code:
        print(f"{environ.get('MOBSTER_HEADLINE') or 'Mobster checks'} (exit code {code}).", flush=True)
    return code


def main(argv):
    steps = {"guard": guard, "run": run, "comment": comment, "finish": finish}
    if len(argv) != 2 or argv[1] not in steps:
        print(f"usage: action.py {{{','.join(steps)}}}", file=sys.stderr)
        return 2
    return steps[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
