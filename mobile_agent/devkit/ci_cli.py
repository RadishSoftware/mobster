"""`mobster ci`'s parser and output (ci.py does the work)."""

import json
import os
import sys
import unicodedata
from pathlib import Path

from . import DevError, main, say, subcommands
from .ci import HOSTED_HOSTS

MARKS = {"ok": "✓", "warn": "!", "fail": "✗", "skip": "-"}


def add_arguments(parser, helpers):
    add = subcommands(parser, "ci_command")
    init = add("init", help="write the CI workflow for this repository (GitHub Actions, GitLab CI or a hosted Mac)",
                           description="Write the workflow that runs `mobster ci run` on every pull request or "
                                       "merge request: on a Mac of yours as a self-hosted runner, or with --hosted "
                                       "on a GitHub-hosted macOS runner. --host xcode-cloud, codemagic, bitrise or "
                                       "circleci writes that service's file, which runs on its hosted Macs.")
    init.add_argument("--host", choices=("github", "gitlab", *HOSTED_HOSTS),
                      help="where the repository lives, or the CI service to write for (default: from the origin "
                           "remote)")
    init.add_argument("--hosted", action="store_true",
                      help="run on a GitHub-hosted macOS runner and sign in with a CI token, with no Mac of yours "
                           "(no iPhone either)")
    init.add_argument("--device", action="append", metavar="NAME",
                      help="also test on this iPhone, plugged into the runner; repeatable (the runner must list each "
                           "in MOBSTER_RUNNER_DEVICES)")
    init.add_argument("--testflight", action="append", metavar="BUNDLE",
                      help="with --device: your app's TestFlight build on the phone; repeatable")
    init.add_argument("--force", action="store_true", help="replace the workflow file if it exists")
    init.add_argument("--print", dest="print_only", action="store_true",
                      help="print the workflow instead of writing it")

    doctor = add("doctor", help="check that this Mac is ready to be a CI runner",
                             description="Check this Mac as a CI runner: Xcode, a simulator runtime, Mobster for "
                                         "Mac and its plan, system sleep, automatic login, the runner service, "
                                         "free disk and the phones the runner allows. Reads only.")
    doctor.add_argument("--json", action="store_true", help="print the rows as JSON")
    doctor.add_argument("--hosted", action="store_true",
                        help="check for a hosted runner: skip the rows about keeping a Mac running and check the CI "
                             "token (automatic inside a hosted job)")

    run = add("run", help="test the PR or MR this job builds; the report goes to .mobster/ci",
                          description="Run `mobster test pr` for the pull request or merge request this CI job "
                                      "builds, with a screen recording of each simulator run. The suite's report, "
                                      "JUnit file, frames, GIFs and videos go to .mobster/ci for the job's "
                                      "artifacts. Works the same in a terminal.",
                          epilog="exit codes: 0 passed, 1 failed, 2 needs review, 3 couldn't run\n\n"
                                 "examples:\n  mobster ci run\n  mobster ci run --device \"Test iPhone\"\n"
                                 "  mobster ci run --device \"Test iPhone\" --device \"Lab iPhone\" -- --spread\n"
                                 "  mobster ci run -- --retries 2 --parallel 2")
    run.add_argument("--base", metavar="REF",
                     help="the branch the PR merges into (default: the job's target branch, else origin/main)")
    run.add_argument("--sim", action="append", metavar="TYPE[@RUNTIME]",
                     help="a simulator device type to run on; repeatable (default: each check's own)")
    run.add_argument("--device", action="append", metavar="NAME",
                     help="an iPhone plugged into this Mac; repeatable. In a CI job the runner must list each in "
                          "MOBSTER_RUNNER_DEVICES, or nothing runs")
    run.add_argument("--testflight", action="append", metavar="BUNDLE",
                     help="with --device: your app's TestFlight build on the phone; repeatable")
    run.add_argument("--no-video", action="store_true", help="don't record the simulator's screen")
    run.add_argument("extra", nargs="*", metavar="TEST_FLAG",
                     help="more `mobster test pr` flags, after a --: mobster ci run -- --retries 2")

    comment = add("comment", help="keep one comment on the PR or MR with the verdict and repro steps",
                              description="Post the verdict in .mobster/ci as one comment on the pull request "
                                          "(GITHUB_TOKEN) or merge request (MOBSTER_GITLAB_TOKEN), updated in place "
                                          "on every push: each failure's repro steps, frame, GIF, video and the "
                                          "command that runs it again. Never fails the job.")
    comment.add_argument("--artifact-url", metavar="URL",
                         help="the uploaded report's link (default: $MOBSTER_ARTIFACT_URL, else the job's page)")
    comment.add_argument("--dry-run", action="store_true", help="print the comment and post nothing")


def run(args, *, cwd=None, environ=None):
    environ = os.environ if environ is None else environ
    handlers = {"init": _init, "doctor": _doctor, "run": _run, "comment": _comment}
    return main("ci", handlers, args, cwd=Path(cwd or Path.cwd()), environ=environ)


def _phones(args):
    """(--device, --testflight) as tuples; --testflight goes with --device (a TestFlight build is on an iPhone)."""
    devices, testflight = tuple(args.device or ()), tuple(args.testflight or ())
    for flag, value in [*(("--device", name) for name in devices), *(("--testflight", app) for app in testflight)]:
        # a line break would end the run line of the workflow ci init writes
        if any(unicodedata.category(char) in ("Cc", "Zl", "Zp") for char in value):
            raise DevError(f"{flag} {value!r} holds a line break, a tab or another control character, so Mobster did "
                           "nothing.", "Remove it: no iPhone's name or app's bundle ID has one.")
    if testflight and not devices:
        raise DevError("--testflight goes with --device: a TestFlight build is on an iPhone, not a simulator.")
    return devices, testflight


def _init(args, cwd, environ):
    from . import ci
    root = ci.repo_root(cwd)
    host = args.host
    if host is None:
        code, url = ci._git(root, "remote", "get-url", "origin")
        host = ci.remote_host(url) if code == 0 else None
    devices, testflight = _phones(args)
    if args.print_only:
        say(ci.workflow(host, devices=devices, testflight=testflight, hosted=args.hosted)[1], sys.stdout)
        return 0
    hosted = args.hosted or host in HOSTED_HOSTS
    target, notes = ci.init(root, host=host, devices=devices, testflight=testflight, force=args.force,
                            hosted=args.hosted)
    if hosted:
        from . import ci_hosts
        lines = [f"Wrote {target.relative_to(root)}.", *notes, "", "Next:",
                 *(f"  {number}. {step}" for number, step in enumerate(ci_hosts.NEXT[host], 1)),
                 "", "On the runner, `mobster ci doctor` checks this runner before the run."]
        say("\n".join(lines))
        return 0
    github = host == "github"
    steps = ["On the Mac that runs the checks: mobster ci doctor",
             ("Register it as a self-hosted runner with the label mobster" if github else
              "Register it as a GitLab runner (shell executor) with the tag mobster") + f": {ci.DOCS}/ci/mac-runner",
             f"Add ANTHROPIC_API_KEY as a {'repository secret' if github else 'masked CI/CD variable'}, for checks "
             "with steps."]
    if not github:
        steps.append("Add MOBSTER_GITLAB_TOKEN, a project access token with the api scope, for the MR comment.")
    if devices:
        steps.append(f"On the runner, allow the phone{'' if len(devices) == 1 else 's'}: "
                     f"MOBSTER_RUNNER_DEVICES={','.join(devices)}")
    steps.append(f"Commit the file and open a {'pull' if github else 'merge'} request.")
    lines = [f"Wrote {target.relative_to(root)}.", *notes, "", "Next:",
             *(f"  {number}. {step}" for number, step in enumerate(steps, 1))]
    if not (root / ".mobster" / "checks").is_dir():
        lines += ["", "This project has no checks yet. `mobster test pr` drafts one for each screen a PR changes, "
                      "and `mobster test record` turns a finished task into one."]
    say("\n".join(lines))
    return 0


def _doctor(args, cwd, environ):
    from . import ci
    rows = ci.doctor(environ=environ, hosted=True if args.hosted else None)
    failed = any(row.status == "fail" for row in rows)
    if args.json:
        say(json.dumps({"ok": not failed, "rows": [row.public() for row in rows]}, ensure_ascii=False))
        return 1 if failed else 0
    width = max(len(row.name) for row in rows)
    lines = []
    for row in rows:
        lines.append(f"{MARKS[row.status]} {row.name.ljust(width)}  {row.detail}")
        if row.fix and row.status != "ok":
            lines.append(f"  {' ' * width}  {row.fix}")
    if failed:
        lines += ["", "Fix the rows marked ✗, then run `mobster ci doctor` again."]
    else:
        lines += ["", "This Mac is ready to run Mobster's checks. Next, `mobster sim doctor --fix` prepares a "
                      "simulator, so the first job doesn't wait for one."]
        if args.hosted or ci.is_hosted(environ):
            lines[-1:] = ["This runner is ready to run Mobster's checks."]
    say("\n".join(lines))
    return 1 if failed else 0


def _run(args, cwd, environ):
    from ..devtools import ProgressLines
    from . import ci
    extra = list(args.extra or ())
    devices, testflight = _phones(args)
    root = ci.repo_root(cwd)
    progress = ProgressLines(sys.stderr)
    code, doc = ci.run_checks(root, devices=devices, testflight=testflight, sims=tuple(args.sim or ()),
                              video=not args.no_video, base=args.base, extra=extra, environ=environ,
                              progress=progress)
    progress.end()
    say(f"{doc['headline']}. Report: {ci.CI_DIR}/")
    return code


def _comment(args, cwd, environ):
    from . import ci
    root = ci.repo_root(cwd)
    body = ci.comment_markdown(root, environ, args.artifact_url)
    target = root / ci.CI_DIR / "comment.md"
    try:
        target.write_text(body, encoding="utf-8")
    except OSError:
        pass
    if args.dry_run:
        say(body)
        return 0
    ci.job_summary(body, environ)
    if ci.host_of(environ) is None:
        say(body)
        say(f"Not in a CI job, so Mobster posted nothing. The comment is in {ci.CI_DIR}/comment.md.", sys.stderr)
        return 0
    say(ci.post(body, environ), sys.stderr)
    return 0
