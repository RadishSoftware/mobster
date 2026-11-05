"""`mobster ci`: your checks on every pull request or merge request, on a Mac of yours or a hosted CI runner."""

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import __version__, plan_gate, premium_model
from . import DevError, self_argv
from .ci_hosts import COMMAND

CI_DIR = ".mobster/ci"
MARKER = "<!-- mobster:test-pr -->"
SCHEMA = "mobster.ci/1"
GITHUB_WORKFLOW = ".github/workflows/mobster.yml"
GITLAB_FILE = ".gitlab-ci.yml"
GITLAB_INCLUDE = ".gitlab/mobster.gitlab-ci.yml"
# Hosts whose runners Mobster doesn't see: every job on them is a hosted macOS runner, with no phones.
HOSTED_HOSTS = ("xcode-cloud", "codemagic", "bitrise", "circleci")
HOST_FILES = {"xcode-cloud": "ci_scripts/ci_post_xcodebuild.sh", "codemagic": "codemagic.yaml",
              "bitrise": "bitrise.yml", "circleci": ".circleci/config.yml"}
SNIPPETS = ".mobster/hosts"
PAGE = 100
MAX_PAGES = 30
MIN_FREE_GB, LOW_FREE_GB = 10, 30
COMMENT_LIMIT = 60_000
DOCS = "https://docs.mobster.dev"


def listed(text):
    """A comma- or newline-separated list, folded for comparison."""
    return {item.strip().casefold() for item in re.split(r"[,\n]", text or "") if item.strip()}


def host_of(environ=None):
    """The CI host of this job ("github", "gitlab", "xcode-cloud", "codemagic", "bitrise", "circleci"), else None (a
    terminal)."""
    environ = os.environ if environ is None else environ
    if environ.get("GITHUB_ACTIONS") == "true":
        return "github"
    if environ.get("GITLAB_CI") == "true":
        return "gitlab"
    if environ.get("CI_XCODE_CLOUD") == "TRUE":
        return "xcode-cloud"
    if environ.get("CM_BUILD_ID"):
        return "codemagic"
    if environ.get("BITRISE_IO") == "true":
        return "bitrise"
    if environ.get("CIRCLECI") == "true":
        return "circleci"
    return None


def is_hosted(environ=None):
    """Whether this job runs on a runner the CI host owns, not on a Mac of yours."""
    environ = os.environ if environ is None else environ
    return host_of(environ) in HOSTED_HOSTS or environ.get("RUNNER_ENVIRONMENT") == "github-hosted"


def remote_host(url):
    """"github" or "gitlab" from a remote URL, else None."""
    text = str(url or "").lower()
    if "github" in text:
        return "github"
    if "gitlab" in text:
        return "gitlab"
    return None


def _git(root, *args, runner=None):
    run = runner or (lambda argv, cwd: subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60))
    try:
        done = run(["git", *args], str(root))
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return done.returncode, (done.stdout or "").strip()


def repo_root(start, runner=None):
    code, out = _git(start, "rev-parse", "--show-toplevel", runner=runner)
    return Path(out) if code == 0 and out else Path(start)


# -- init --------------------------------------------------------------------------------------------------------

def _flags(devices, testflight):
    return "".join(f" --device {shlex.quote(device)}" for device in devices) + \
        "".join(f" --testflight {shlex.quote(bundle)}" for bundle in testflight)


def github_workflow(*, devices=(), testflight=()):
    flags = _flags(devices, testflight)
    return f"""# Mobster checks on every pull request, on a Mac of yours ({DOCS}/ci).
# Install uv on the runner (`brew install uv`). `mobster ci doctor` says what else is missing.
name: mobster
on: pull_request
permissions:
  contents: read
  pull-requests: write   # the one comment Mobster keeps up to date
concurrency:
  group: mobster-${{{{ github.event.pull_request.number }}}}
  cancel-in-progress: true
jobs:
  checks:
    runs-on: [self-hosted, macOS, mobster]
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # the diff needs the base branch
      - name: Test what this PR changed
        run: |
          {COMMAND} ci run{flags}
        env:
          ANTHROPIC_API_KEY: ${{{{ secrets.ANTHROPIC_API_KEY }}}}   # only checks with steps use it
      - name: Upload the report
        id: upload
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: mobster-report
          path: .mobster/ci/
          include-hidden-files: true
          retention-days: 14
          if-no-files-found: ignore
      - name: Comment on the PR
        if: always()
        run: |
          {COMMAND} ci comment
        env:
          GITHUB_TOKEN: ${{{{ github.token }}}}
          MOBSTER_ARTIFACT_URL: ${{{{ steps.upload.outputs.artifact-url }}}}
"""


def gitlab_job(*, devices=(), testflight=()):
    flags = _flags(devices, testflight)
    return f"""# Mobster checks on every merge request, on a Mac of yours ({DOCS}/ci/gitlab).
# Register the Mac as a runner with the shell executor and the tag "mobster". On it, `mobster ci doctor` says what's
# missing. Install uv with `brew install uv`. For the MR comment, add MOBSTER_GITLAB_TOKEN (a project access token
# with the api scope) as a masked CI/CD variable. For checks with steps, add ANTHROPIC_API_KEY the same way.
mobster:
  stage: test
  tags: [mobster]
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  variables:
    GIT_DEPTH: "0"
  timeout: 1h
  interruptible: true
  script:
    - |
      {COMMAND} ci run{flags}
  after_script:
    - |
      {COMMAND} ci comment
  artifacts:
    when: always
    expire_in: 14 days
    paths:
      - .mobster/ci/
    reports:
      junit: .mobster/ci/junit.xml
"""


def workflow(host, *, devices=(), testflight=(), hosted=False):
    """(the file's path in the repository, its text): what `ci init` writes and `ci init --print` prints. Raises
    DevError.

    ``hosted`` means GitHub's hosted macOS runner; the other hosted CI hosts are always hosted."""
    from . import ci_hosts
    if host in HOSTED_HOSTS or (hosted and host == "github"):
        if devices or testflight:
            raise DevError("A hosted runner has no iPhone to test on, so --device and --testflight don't go with it.",
                           "Drop them, or use a Mac of yours as the runner.")
        return Path(GITHUB_WORKFLOW if host == "github" else HOST_FILES[host]), ci_hosts.render(host)
    if hosted and host == "gitlab":
        raise DevError("Mobster has no hosted runner setup for GitLab CI yet.",
                       "Use a Mac of yours as the runner: https://docs.mobster.dev/ci/gitlab")
    if host == "github":
        return Path(GITHUB_WORKFLOW), github_workflow(devices=devices, testflight=testflight)
    if host == "gitlab":
        return Path(GITLAB_FILE), gitlab_job(devices=devices, testflight=testflight)
    raise DevError("Mobster can't tell where this repository lives.", "Pass --host github or --host gitlab.")


def init(root, *, host, devices=(), testflight=(), force=False, hosted=False):
    """Write the workflow. Returns (path written, extra lines to tell the person). Raises DevError."""
    root = Path(root)
    relative, text = workflow(host, devices=devices, testflight=testflight, hosted=hosted)
    target, notes = root / relative, []
    if host in HOSTED_HOSTS and host != "xcode-cloud" and target.exists() and not force:
        # these files hold a project's other jobs: leave them and put ours beside them to merge
        target = root / SNIPPETS / relative.name
        notes.append(f"{relative} already exists, so Mobster wrote the job to {target.relative_to(root)}. "
                     f"Merge it into {relative}.")
    elif host == "gitlab" and target.exists():
        target = root / GITLAB_INCLUDE
        notes.append(f"{GITLAB_FILE} already exists, so the job went to {GITLAB_INCLUDE}. Add it to "
                     f"{GITLAB_FILE}:\n\n  include:\n    - local: {GITLAB_INCLUDE}\n")
    if target.exists() and not force:
        raise DevError(f"{target.relative_to(root)} already exists, so Mobster left it as it is.",
                       "Pass --force to replace it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    if text.startswith("#!"):
        target.chmod(0o755)
    return target, notes


# -- doctor ------------------------------------------------------------------------------------------------------

class Row:
    def __init__(self, name, status, detail, fix=""):
        self.name, self.status, self.detail, self.fix = name, status, detail, fix

    def public(self):
        return {"name": self.name, "status": self.status, "detail": self.detail, "fix": self.fix or None}


def _run(argv, timeout=30):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def pmset_value(text, name):
    """The number pmset -g prints for ``name`` (" sleep 0"), or None."""
    match = re.search(rf"^\s*{re.escape(name)}\s+(\d+)", text or "", re.MULTILINE)
    return int(match.group(1)) if match else None


def runner_env(home, environ=None):
    environ = os.environ if environ is None else environ
    found = {key: environ[key] for key in ("MOBSTER_RUNNER_DEVICES", "MOBSTER_RUNNER_TESTFLIGHT") if environ.get(key)}
    if found:
        return found, "the job's environment"
    env_file = Path(home) / "actions-runner" / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() in ("MOBSTER_RUNNER_DEVICES", "MOBSTER_RUNNER_TESTFLIGHT"):
                found[key.strip()] = value.strip()
        if found:
            return found, str(env_file)
    except OSError:
        pass
    config = Path(home) / ".gitlab-runner" / "config.toml"
    try:
        text = config.read_text(encoding="utf-8")
        for key in ("MOBSTER_RUNNER_DEVICES", "MOBSTER_RUNNER_TESTFLIGHT"):
            match = re.search(rf'"{key}=([^"]*)"', text)
            if match:
                found[key] = match.group(1)
        if found:
            return found, str(config)
    except OSError:
        pass
    return {}, None


def doctor(*, run=_run, home=None, environ=None, machine=None, plan=None, agent=None, disk=None, hosted=None,
           ci_token=None):
    home = Path(home or Path.home())
    environ = os.environ if environ is None else environ
    hosted = is_hosted(environ) if hosted is None else hosted
    rows = []
    arch = machine if machine is not None else platform.machine()
    rows.append(Row("Apple silicon", "ok" if arch == "arm64" else "warn", f"this Mac is {arch or 'unknown'}",
                    "" if arch == "arm64" else "Simulators run fastest on Apple silicon; an Intel Mac works, slower."))
    code, out = run(["xcodebuild", "-version"])
    version = out.splitlines()[0].strip() if code == 0 and out.strip() else ""
    rows.append(Row("Xcode", "ok" if version else "fail", version or "xcodebuild didn't answer",
                    "" if version else "Install Xcode from the App Store, open it once, then run "
                                       "`sudo xcode-select -s /Applications/Xcode.app`."))
    code, out = run(["xcrun", "simctl", "list", "runtimes", "-j"])
    runtimes = []
    try:
        runtimes = [item.get("name") for item in json.loads(out).get("runtimes") or ()
                    if item.get("isAvailable") and str(item.get("name", "")).startswith("iOS")]
    except (ValueError, AttributeError):
        pass
    rows.append(Row("iOS Simulator runtime", "ok" if runtimes else "fail",
                    ", ".join(runtimes[-2:]) if runtimes else "none installed",
                    "" if runtimes else "Run `xcodebuild -downloadPlatform iOS`."))
    agent = self_argv()[0][0] if agent is None else agent
    installed = Path(agent).is_file()
    rows.append(Row("Mobster CLI", "ok" if installed else "fail",
                    f"version {__version__}" if installed else "executable not found",
                    "" if installed else f"Install uv, then run `{COMMAND} ci doctor`."))
    bundled = plan_gate.bundled()
    has_token = bool(environ.get("MOBSTER_CI_TOKEN", "").strip())
    if hosted or ci_token is not None or has_token:
        if ci_token is not None:
            token_row = _token_row(ci_token)
        elif bundled or has_token:
            token_row = hosted_token_row(environ)
        else:
            token_row = Row("CI token", "skip", "not required by the standalone CLI")
        rows.append(token_row)
    if not hosted:
        if bundled or plan is not None:
            problem = (plan or _plan_problem)()
            rows.append(Row("Mobster plan", "ok" if problem is None else "fail",
                            "active on this Mac" if problem is None else f"none active ({problem.replace('_', ' ')})",
                            "" if problem is None else "Open Mobster, sign in, and start your free trial or choose a "
                                                       "plan."))
        else:
            rows.append(Row("Mobster plan", "skip", "not required by the standalone CLI"))
    if hosted:
        # a hosted runner is a fresh machine for every job, so the rows about keeping one running don't apply
        for name in ("System sleep", "Restart after a power cut", "Automatic login", "Runner service"):
            rows.append(Row(name, "skip", "not needed on a hosted runner"))
        return rows + _hosted_tail(home, disk)
    code, out = run(["pmset", "-g"])
    sleep, restart = pmset_value(out, "sleep"), pmset_value(out, "autorestart")
    rows.append(Row("System sleep", "ok" if sleep == 0 else "fail" if sleep else "warn",
                    "never" if sleep == 0 else f"after {sleep} minutes" if sleep else "unknown",
                    "" if sleep == 0 else "Run `sudo pmset -a sleep 0 disksleep 0`, so the Mac never sleeps through "
                                          "a job."))
    rows.append(Row("Restart after a power cut", "ok" if restart == 1 else "warn",
                    "on" if restart == 1 else "off",
                    "" if restart == 1 else "Run `sudo pmset -a autorestart 1`."))
    code, out = run(["defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser"])
    user = out.strip() if code == 0 else ""
    rows.append(Row("Automatic login", "ok" if user else "warn", f"as {user}" if user else "off",
                    "" if user else "Turn it on in System Settings › Users & Groups, so the runner and the "
                                    "simulators start again after a restart."))
    code, out = run(["launchctl", "list"])
    services = [name for name, needle in (("GitHub Actions runner", "actions.runner"),
                                          ("GitLab Runner", "gitlab-runner")) if needle in out]
    rows.append(Row("Runner service", "ok" if services else "warn", ", ".join(services) or "none running",
                    "" if services else f"Install the runner as a service of this user: {DOCS}/ci/mac-runner."))
    free = (disk or (lambda path: shutil.disk_usage(path).free))(str(home))
    gigabytes = free / 1e9
    rows.append(Row("Free disk", "ok" if gigabytes >= LOW_FREE_GB else "warn" if gigabytes >= MIN_FREE_GB else
                    "fail", f"{gigabytes:.0f} GB",
                    "" if gigabytes >= LOW_FREE_GB else "Free some space: `xcrun simctl delete unavailable` and "
                                                        "Xcode's DerivedData are the usual places."))
    values, source = runner_env(home, environ)
    if source and source.startswith(str(home) + os.sep):
        source = "~" + source[len(str(home)):]
    phones = sorted(listed(values.get("MOBSTER_RUNNER_DEVICES", "")))
    rows.append(Row("Phones this runner allows", "ok",
                    f"{', '.join(phones)} (from {source})" if phones else "none: checks run on simulators only",
                    "" if phones else "To test on an iPhone, plug it in, set it up in Mobster, and list its name in "
                                      "MOBSTER_RUNNER_DEVICES in the runner's environment."))
    return rows


TOKEN_FIXES = {
    "off": "CI tokens are off in this Mobster. Update Mobster for Mac, or ask Mobster to turn them on for your team.",
    "no_token": "Add your CI token as the secret MOBSTER_CI_TOKEN and pass it to the step that runs `mobster ci run`.",
    "expired": "Create a new CI token and replace the secret.",
    "inactive": "This token's plan is no longer active. Check your plan, then create a new CI token.",
    "plan_required": "The plan behind this token isn't active. Check your plan, then create a new CI token.",
    "revoked": "This token was revoked. Create a new CI token and replace the secret.",
}


# The row's word for a verdict that refuses (plan_gate.ci_token_verdict), where its key doesn't read as one.
VERDICT_DETAILS = {"unchecked": "couldn't be checked", "unsendable": "can't be sent", "plan_required": "plan inactive"}


def hosted_token_row(environ=None):
    """A hosted runner's token row. With premiumModel on no run needs a token, but one that's there must check out."""
    environ = os.environ if environ is None else environ
    if not premium_model.enabled(environ):
        return _token_row(lambda: plan_gate.ci_token_problem(environ))
    verdict = plan_gate.ci_token_verdict(environ)
    if verdict is None:
        return Row("CI token", "skip", "not needed")
    if verdict == "ok":
        return Row("CI token", "ok", "checked with Mobster")
    return Row("CI token", "fail", VERDICT_DETAILS.get(verdict, verdict), plan_gate.ci_token_refusal(verdict))


def _token_row(problem):
    problem = problem()
    if problem is None:
        return Row("CI token", "ok", "valid for simulator runs")
    return Row("CI token", "fail", problem.replace("_", " "),
               TOKEN_FIXES.get(problem,
                               "This isn't a CI token Mobster accepts. Create a new one and replace the secret."))


def _hosted_tail(home, disk):
    free = (disk or (lambda path: shutil.disk_usage(path).free))(str(home)) / 1e9
    return [Row("Free disk", "ok" if free >= LOW_FREE_GB else "warn" if free >= MIN_FREE_GB else "fail",
                f"{free:.0f} GB", "" if free >= LOW_FREE_GB else "Pick a larger runner, or free space before the job."),
            Row("Phones this runner allows", "skip", "none: a hosted runner has no iPhone, checks run on simulators")]


def _plan_problem():
    if plan_gate.gate_turned_off():
        return None
    return plan_gate.PlanGate(plan_gate.app_account_file()).problem()


# -- run ---------------------------------------------------------------------------------------------------------

def guard(devices, testflight, environ=None):
    """In a CI job, refuse the whole run when one phone or TestFlight app isn't on the runner's own lists."""
    environ = os.environ if environ is None else environ
    # outside a CI job the person at the terminal is the runner's owner
    if host_of(environ) is None:
        return None
    allowed = listed(environ.get("MOBSTER_RUNNER_DEVICES", ""))
    refused = [device for device in devices if device.casefold() not in allowed]
    if refused:
        phones = ("the iPhone " if len(refused) == 1 else "the iPhones ") + ", ".join(f'"{name}"' for name in refused)
        raise DevError(f"This runner doesn't allow {phones}, so nothing ran.",
                       "Its owner lists the phones it may test in MOBSTER_RUNNER_DEVICES, in the runner's "
                       "environment, which a pull request can't change.")
    missing = sorted({bundle.casefold() for bundle in testflight} -
                     listed(environ.get("MOBSTER_RUNNER_TESTFLIGHT", "")))
    if missing:
        raise DevError(f"This runner doesn't allow the TestFlight build of {', '.join(missing)}, so nothing ran.",
                       "Its owner lists them in MOBSTER_RUNNER_TESTFLIGHT, in the runner's environment.")
    return None


BASE_VARIABLES = ("GITHUB_BASE_REF", "CI_MERGE_REQUEST_TARGET_BRANCH_NAME", "CI_PULL_REQUEST_TARGET_BRANCH",
                  "BITRISEIO_GIT_BRANCH_DEST", "MOBSTER_BASE_BRANCH")


def base_ref(environ=None):
    """(the branch the PR or MR merges into as origin/<branch>, its name), or (None, None)."""
    environ = os.environ if environ is None else environ
    branch = next((environ[name] for name in BASE_VARIABLES if environ.get(name, "").strip()), "")
    branch = branch.strip()
    return (f"origin/{branch}", branch) if branch else (None, None)


def fetch_base(root, branch, git=None):
    code, out = _git(root, "rev-parse", "--is-shallow-repository", runner=git)
    if code == 0 and out == "true":
        _git(root, "fetch", "--quiet", "--no-tags", "--unshallow", "origin", runner=git)
    if branch:
        _git(root, "fetch", "--quiet", "--no-tags", "origin",
             f"+refs/heads/{branch}:refs/remotes/origin/{branch}", runner=git)


def test_argv(*, base=None, devices=(), testflight=(), sims=(), video=True, share=False, extra=()):
    argv = ["test", "pr", "--json"]
    if base:
        argv += ["--base", base]
    for sim in sims:
        argv += ["--sim", sim]
    for device in devices:
        argv += ["--device", device]
    for bundle in testflight:
        argv += ["--testflight", bundle]
    if video:
        argv.append("--video")
    if share:
        argv.append("--share")
    return argv + list(extra)


def _last_json(text):
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _claim(target):
    """Clear the last job's copy in ``target``; refuse a folder holding files Mobster didn't write (no ci.json)."""
    if target.exists():
        if not (target / "ci.json").is_file() and any(target.iterdir()):
            raise DevError(f"{target} holds files Mobster didn't write, so it left them there.",
                           "Move them, then run it again.")
        shutil.rmtree(target)


def collect(root, suite, target):
    """Copy the suite folder to ``target`` (.mobster/ci), replacing the last job's copy."""
    source = Path(root) / ".mobster" / "test-results" / str(suite)
    target = Path(target)
    _claim(target)
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        target.mkdir(parents=True, exist_ok=True)


def set_output(name, value, environ=None):
    path = ((os.environ if environ is None else environ).get("GITHUB_OUTPUT") or "").strip()
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={str(value).splitlines()[0] if str(value) else ''}\n")


def run_checks(root, *, devices=(), testflight=(), sims=(), video=True, base=None, extra=(), environ=None,
               runner=None, git=None, progress=None):
    environ = os.environ if environ is None else environ
    progress = progress or (lambda line: None)
    root = Path(root)
    if any(flag.split("=", 1)[0] in ("--device", "--testflight") for flag in extra):
        raise DevError("This CI job can't pass --device or --testflight after --, so nothing ran.",
                       "Use mobster ci run --device and --testflight; the runner's allowlist checks those flags.")
    guard(devices, testflight, environ)
    host = host_of(environ)
    if base is None:
        base, branch = base_ref(environ)
        if branch:
            fetch_base(root, branch, git=git)
    argv, env_extra = self_argv()
    # With the team's upload key, the report goes to the team's hosted reports too, and the comment links it.
    share = bool(environ.get("MOBSTER_TEAM_KEY", "").strip())
    argv = argv + test_argv(base=base, devices=devices, testflight=testflight, sims=sims, video=video, share=share,
                            extra=extra)
    progress("$ mobster " + " ".join(argv[len(self_argv()[0]):]))
    if runner is None:
        def runner(argv, cwd, env):
            try:
                done = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, text=True, env=env)
            except FileNotFoundError:
                return 127, ""
            return done.returncode, done.stdout
    code, out = runner(argv, str(root), {**environ, **env_extra})
    pr = _last_json(out)
    target = root / CI_DIR
    doc = {"schema": SCHEMA, "host": host, "exitCode": code, "suite": None,
           "headline": "Mobster checks couldn't run"}
    if isinstance(pr, dict) and pr.get("suite"):
        doc.update(suite=pr["suite"], headline=pr.get("headline") or doc["headline"])
        collect(root, pr["suite"], target)
    else:
        _claim(target)
        target.mkdir(parents=True, exist_ok=True)
    (target / "ci.json").write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    set_output("exit-code", code, environ)
    set_output("headline", doc["headline"], environ)
    return code, doc


# -- comment -----------------------------------------------------------------------------------------------------

def links(host, environ=None, artifact_url=None):
    """(the report's link, a function from a file's path in the report to its link, or None)."""
    environ = os.environ if environ is None else environ
    if host == "gitlab":
        job, project, job_id = (environ.get(key, "").rstrip("/") for key in ("CI_JOB_URL", "CI_PROJECT_URL",
                                                                              "CI_JOB_ID"))
        if not job:
            return artifact_url, None
        browse = f"{job}/artifacts/browse/{CI_DIR}/"

        def file_url(path):
            quoted = urllib.parse.quote(f"{CI_DIR}/{path}")
            return f"{project}/-/jobs/{job_id}/artifacts/file/{quoted}" if project and job_id else browse
        return artifact_url or browse, file_url
    url = artifact_url or environ.get("MOBSTER_ARTIFACT_URL", "").strip()
    return url or _github_run_url(environ), None


def _github_run_url(environ):
    if not environ.get("GITHUB_RUN_ID"):
        return None
    server = environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    return f"{server}/{environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{environ['GITHUB_RUN_ID']}"


def failed_comment(doc, environ=None):
    environ = os.environ if environ is None else environ
    if host_of(environ) == "gitlab" and environ.get("CI_JOB_URL"):
        log = environ["CI_JOB_URL"]
    else:
        log = _github_run_url(environ)
    where = f"The [job's log]({log}) says why" if log else "The job's log says why"
    return f"{MARKER}\n### Mobster checks couldn't run\n\n{where} (exit code {doc.get('exitCode', '?')}).\n"


def comment_markdown(root, environ=None, artifact_url=None):
    """The comment for the report in .mobster/ci: pr_report's, with links into this job's artifacts."""
    from ..testkit import pr_report
    environ = os.environ if environ is None else environ
    folder = Path(root) / CI_DIR
    try:
        doc = json.loads((folder / "ci.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DevError(f"There's no report in {CI_DIR}, so there's nothing to comment.",
                       "Run `mobster ci run` first.") from None
    try:
        pr = json.loads((folder / "pr.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pr = None
    if not isinstance(pr, dict):
        return failed_comment(doc, environ)
    hosted = pr.get("hostedReport")
    if isinstance(hosted, str):
        try:
            return pr_report.comment(pr, artifact_url=hosted, file_url=pr_report.hosted_pages(hosted))
        except ValueError:
            pass  # not a report link: the job's artifacts, as without --share
    report, file_url = links(host_of(environ), environ, artifact_url)
    return pr_report.comment(pr, artifact_url=report, file_url=file_url)


def _open(request, opener=None, timeout=30):
    with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
        text = response.read().decode("utf-8")
    return json.loads(text) if text else None


class GitHub:
    """The one sticky PR comment, through the REST API with the job's token."""

    def __init__(self, token, repository, api="https://api.github.com", opener=None):
        self.token, self.repository, self.api, self.opener = token, repository, api.rstrip("/"), opener

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "mobster-ci"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        return _open(urllib.request.Request(f"{self.api}{path}", data=data, method=method, headers=headers),
                     self.opener)

    def sticky(self, number, body):
        base = f"/repos/{self.repository}/issues"
        for page in range(1, MAX_PAGES + 1):
            items = self.call("GET", f"{base}/{number}/comments?per_page={PAGE}&page={page}") or []
            for item in items:
                if MARKER in (item.get("body") or ""):
                    done = self.call("PATCH", f"{base}/comments/{item['id']}", {"body": body})
                    return "updated", (done or {}).get("html_url") or item.get("html_url")
            if len(items) < PAGE:
                break
        done = self.call("POST", f"{base}/{number}/comments", {"body": body})
        return "created", (done or {}).get("html_url")


class GitLab:
    """The one sticky MR note, through the REST API with a project access token (api scope)."""

    def __init__(self, token, project, api, opener=None):
        self.token, self.project, self.api, self.opener = token, str(project), api.rstrip("/"), opener

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"PRIVATE-TOKEN": self.token, "User-Agent": "mobster-ci"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        return _open(urllib.request.Request(f"{self.api}{path}", data=data, method=method, headers=headers),
                     self.opener)

    def sticky(self, iid, body):
        base = f"/projects/{urllib.parse.quote(self.project, safe='')}/merge_requests/{iid}/notes"
        for page in range(1, MAX_PAGES + 1):
            items = self.call("GET", f"{base}?per_page={PAGE}&page={page}&sort=asc") or []
            for item in items:
                if not item.get("system") and MARKER in (item.get("body") or ""):
                    self.call("PUT", f"{base}/{item['id']}", {"body": body})
                    return "updated", item["id"]
            if len(items) < PAGE:
                break
        done = self.call("POST", base, {"body": body})
        return "created", (done or {}).get("id")


def github_pr_number(environ):
    try:
        with open(environ.get("GITHUB_EVENT_PATH", ""), encoding="utf-8") as handle:
            event = json.load(handle)
    except (OSError, ValueError):
        return None
    number = (event.get("pull_request") or {}).get("number")
    if number is None and (event.get("issue") or {}).get("pull_request"):
        number = event["issue"].get("number")
    return number if isinstance(number, int) else None


def post(body, environ=None, opener=None):
    environ = os.environ if environ is None else environ
    host = host_of(environ)
    if host == "github":
        number = github_pr_number(environ)
        if number is None:
            return "Not a pull request, so there's no comment; the job's summary has the verdict."
        token = environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            return "No GITHUB_TOKEN, so Mobster didn't comment. Give the step `GITHUB_TOKEN: ${{ github.token }}`."
        client = GitHub(token, environ.get("GITHUB_REPOSITORY", ""),
                        environ.get("GITHUB_API_URL", "https://api.github.com"), opener)
        where = number
    elif host == "gitlab":
        iid = environ.get("CI_MERGE_REQUEST_IID", "").strip()
        if not iid:
            return "Not a merge request pipeline, so there's no comment."
        token = environ.get("MOBSTER_GITLAB_TOKEN", "").strip()
        if not token:
            return ("No MOBSTER_GITLAB_TOKEN, so Mobster didn't comment. Add a project access token with the api "
                    "scope as a masked CI/CD variable named MOBSTER_GITLAB_TOKEN.")
        client = GitLab(token, environ.get("CI_PROJECT_ID", ""), environ.get("CI_API_V4_URL", ""), opener)
        where = iid
    else:
        return "Not in a CI job, so Mobster posted nothing."
    # a comment never fails the job: every answer from the host becomes one line
    try:
        how, _ = client.sticky(where, body)
    except urllib.error.HTTPError as problem:
        if problem.code in (401, 403, 404):
            return (f"Mobster couldn't comment: {'GitLab' if host == 'gitlab' else 'GitHub'} answered "
                    f"{problem.code}. The token can't write to this {'merge request' if host == 'gitlab' else 'PR'}"
                    " (a PR from a fork gets a read-only token).")
        return f"Mobster couldn't comment: the host answered {problem.code}."
    except (urllib.error.URLError, OSError, ValueError) as problem:
        return f"Mobster couldn't reach the host to comment ({type(problem).__name__})."
    return f"{how.capitalize()} the {'MR' if host == 'gitlab' else 'PR'} comment."


def job_summary(body, environ=None):
    path = ((os.environ if environ is None else environ).get("GITHUB_STEP_SUMMARY") or "").strip()
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(body.replace(MARKER + "\n", "") + "\n")
