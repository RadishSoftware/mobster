"""Argument definitions and usage errors for ``mobster test``."""

import sys

USAGE_EXIT = 3


def add_arguments(parser, helpers):
    """`mobster test`'s flags. ``helpers``: env_option, path_type and bounded from __main__."""
    from . import repro, snapshots_cli
    bounded = helpers["bounded"]
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="check files or folders (default: .mobster/checks); `record` saves a task; "
                             "`capsule` inspects evidence; `compare` reads suites; `scenario` runs ordered checks; "
                             "`minimize` tries a paired local reproducer")
    where = parser.add_argument_group("where checks run")
    where.add_argument("--sim", action="append", metavar="TYPE[@RUNTIME]",
                       help="a simulator device type, with an optional runtime, such as \"iPhone 17 Pro@iOS 26.4\"; "
                            "repeatable (default: each check's own, or .mobster/matrix.yaml)")
    where.add_argument("--device", action="append", metavar="NAME",
                       help="a device from `mobster devices`, or all: every iPhone ready on this Mac, side by side "
                            "in one report; repeatable. On a real iPhone, checks drive only builds you installed, "
                            "never App Store apps")
    from .. import android
    android.add_test_flag(where)
    where.add_argument("--testflight", action="append", metavar="BUNDLE",
                       help="with --device: your app's bundle ID, installed from TestFlight, which checks may "
                            "drive as your own build; repeatable")
    where.add_argument("--matrix", metavar="FILE",
                       help="a YAML file of simulators to run every check on (simulators only)")
    where.add_argument("--locale", action="append", metavar="LANG[,LANG]",
                       help="run every check once in each language, such as de,ja,ar: the app launches in that "
                            "language and region, and the screens are read for truncated text, untranslated "
                            "strings and, in a right-to-left language, a layout that didn't mirror")
    where.add_argument("--spread", action="store_true",
                       help="with two or more devices: run each check once, on whichever device is free, instead of "
                            "on every one (the report groups checks by device)")
    where.add_argument("--parallel", type=bounded(int, 1, 8), default=1, metavar="N",
                       help="simulators running checks at once (default: 1); each iPhone runs one check at a time")
    how = parser.add_argument_group("retries and flakes")
    how.add_argument("--retries", type=bounded(int, 0, 5), metavar="N",
                     help="attempts after a failure, from a fresh app (default: 1, or 0 with --repeat)")
    how.add_argument("--repeat", type=bounded(int, 1, 20), default=1, metavar="K",
                     help="run each check K times and report its pass rate (flake hunting)")
    how.add_argument("--strict", action="store_true", help="a flaky check fails the run")
    how.add_argument("--quarantine", metavar="FILE",
                     help="checks that run and are reported but never fail the run (default: "
                          ".mobster/quarantine.yaml)")
    how.add_argument("--auto-quarantine", action="store_true",
                     help="quarantine for this run the checks whose last 20 results say flaky; without it Mobster "
                          "only suggests them")
    which = parser.add_argument_group("which checks")
    which.add_argument("--tag", action="append", metavar="TAG", help="only checks with this tag; repeatable")
    which.add_argument("--audit", action="append", choices=("a11y",), metavar="NAME",
                       help="also audit what each check reads: a11y finds controls with no label, tap targets "
                            "under 44 pt, images with no label and text clipped at a large text size; no model "
                            "is called")
    which.add_argument("--a11y", nargs="?", const="warn", choices=("warn", "strict"), metavar="LEVEL",
                       help="audit the screens each check reads for accessibility: warn (the default) reports "
                            "findings and passes, strict fails on a new one; findings in "
                            ".mobster/a11y-baseline.json never fail")
    which.add_argument("--update-a11y-baseline", action="store_true",
                       help="write this run's accessibility findings to .mobster/a11y-baseline.json, so later runs "
                            "fail only on new ones")
    snapshots_cli.add_arguments(which)
    which.add_argument("--shard", metavar="I/N", help="run the I-th of N even shards, such as 2/4")
    which.add_argument("--keyless", action="store_true",
                       help="never call a model: checks with steps couldn't run")
    replay = parser.add_argument_group("replay")
    replay.add_argument("--replay", action="store_true",
                        help="replay each check's recorded path with no model, and heal a step that changed (the "
                             "default)")
    replay.add_argument("--fresh", action="store_true",
                        help="run every step with Smart and record the path again")
    replay.add_argument("--no-heal", action="store_true",
                        help="replay, and fail a check whose screen changed instead of healing it (strict CI)")
    unit = parser.add_argument_group("your XCTest targets")
    unit.add_argument("--xctest", action="store_true",
                      help="also build your Xcode project once and run its XCTest and Swift Testing targets on a "
                           "simulator beside the checks; failures join the report, the JUnit file, the history and "
                           "the PR comment. Pass --scheme and --project to name them")
    unit.add_argument("--test-plan", metavar="NAME", help="with --xctest: the test plan to run (default: the "
                                                           "scheme's)")
    unit.add_argument("--only-testing", action="append", metavar="ID",
                      help="with --xctest: run only this Target, Target/Class or Target/Class/test; repeatable")
    out = parser.add_argument_group("reports")
    out.add_argument("--junit", metavar="FILE", help="also write the JUnit XML here")
    out.add_argument("--html", metavar="DIR", help="also write the HTML report (with each run's report) here")
    out.add_argument("--video", action="store_true",
                     help="record each run's screen, on a simulator or an iPhone, into the report")
    out.add_argument("--history", action="store_true",
                     help="print each check's last 20 results (pass rate, duration trend, flake score) and run "
                          "nothing")
    out.add_argument("--json", action="store_true", help="print JSON on stdout instead of the summary")
    out.add_argument("--include", action="append", dest="capsule_include", choices=("junit", "report", "visual"),
                     metavar="KIND", help="with capsule export: copy selected raw junit, report or visual evidence "
                                          "locally; repeatable")
    out.add_argument("--share", action="store_true",
                     help="also upload the report, sealed, to your team's hosted reports and print its link (Team "
                          "plan: one of the team's Macs, or MOBSTER_TEAM_KEY in CI)")
    out.add_argument("--share-days", type=int, choices=(7, 30, 90), default=30, metavar="DAYS",
                     help="how long a shared report is kept: 7, 30 or 90 (default: 30)")
    out.add_argument("--assert-timeout", type=bounded(float, 0, 60), default=5, metavar="S",
                     help="how long expectations may wait for the screen to settle (default: 5)")
    record = parser.add_argument_group("mobster test record")
    record.add_argument("--run", dest="record_run", metavar="RUN_ID", help="the finished task to make a check from")
    record.add_argument("--name", dest="record_name", metavar="TEXT", help="the check's name (default: the task)")
    record.add_argument("-o", "--output", dest="record_output", metavar="PATH",
                        help="with record: write the check here; with import: checks folder; with capsule export: "
                             "new output directory")
    imp = parser.add_argument_group("mobster test import")
    imp.add_argument("--bundle", metavar="ID",
                     help="the app's bundle ID for flows with no appId, and over the appId of the others")
    imp.add_argument("--app", metavar="PATH", help="write this .app (built for the iOS Simulator) into each check")
    imp.add_argument("--skip-unsupported", action="store_true",
                     help="drop commands Mobster can't run instead of turning each into a step marked TODO, and list "
                          "each one; an assertion is never dropped")
    imp.add_argument("--dry-run", action="store_true", help="say what would be written, and write nothing")
    imp.add_argument("--force", action="store_true", help="replace checks that already exist")
    repro.add_arguments(parser)
    add = parser.add_argument_group("mobster test add")
    add.add_argument("--list", dest="add_list", action="store_true", help="list the recipes, one line each")
    add.add_argument("--set", dest="add_set", action="append", metavar="KEY=VALUE",
                     help="a recipe param; repeatable (`mobster test add --list` shows each recipe's)")
    pr = parser.add_argument_group("mobster test pr")
    pr.add_argument("--base", metavar="REF",
                    help="the branch the PR merges into (default: $GITHUB_BASE_REF, else origin/main)")
    pr.add_argument("--no-build", action="store_true",
                    help="don't build the app: test the builds the checks name")
    pr.add_argument("--scheme", metavar="NAME", help="the Xcode scheme to build (default: the only one); also the "
                                                      "scheme --xctest builds")
    pr.add_argument("--project", metavar="PATH",
                    help="the .xcodeproj or .xcworkspace to build (default: the one in the project); also the one "
                         "--xctest builds")
    pr.add_argument("--drafts", choices=("run", "write", "off"), default="run",
                    help="checks for changed screens no check covers: write and run them (default), only write "
                         "them, or neither. Drafts never fail the PR")
    pr.add_argument("--no-gifs", action="store_true", help="don't make a GIF of each failed check")
    pr.add_argument("--tested-with", action="store_true",
                    help="end the PR comment with a \"Tested with Mobster\" line that links to mobster.dev")
    pr.add_argument("--comment", metavar="FILE", help="also write the PR comment's Markdown here")
    pr.add_argument("--github", choices=("check", "comment", "off"), default="check",
                    help="put the verdict on the pull request with Mobster Checks, the GitHub App: a check run "
                         "(check, the default), the check run and one comment kept up to date (comment), or nothing "
                         "(off)")
    nightly = parser.add_argument_group("mobster test nightly")
    nightly.add_argument("--at", metavar="HH:MM",
                         help="with nightly on: the time to run every night, on a 24-hour clock, such as 02:00")
    heal = parser.add_argument_group("mobster test heal")
    heal.add_argument("--accept", action="store_true", help="keep the healed steps of these checks (default: all)")
    heal.add_argument("--reject", action="store_true",
                      help="keep failing: put the recorded steps back and fail when they change again")
    reduce = parser.add_argument_group("mobster test minimize")
    reduce.add_argument("--bad-app", dest="minimize_bad_app", metavar="PATH",
                        help="with minimize: the exact saved bad simulator .app build")
    reduce.add_argument("--control-app", dest="minimize_control_app", metavar="PATH",
                        help="with minimize: the exact saved clean-control simulator .app build")
    reduce.add_argument("--isolated", dest="minimize_isolated", action="store_true",
                        help="with minimize: confirm the external fixture is isolated for every trial")
    reduce.add_argument("--max-attempts", dest="minimize_max_attempts", type=bounded(int, 5, 8), metavar="N",
                        help="with minimize: total bad and control suite dispatches, 5–8 (default: 8)")
    helpers["env_option"](parser)
    parser._option_string_actions["--env-file"].help = ("load KEY=VALUE lines, such as OPENAI_API_KEY or "
                                                        "ANTHROPIC_API_KEY for Smart steps; variables already set "
                                                        "win; default: $MOBSTER_ENV_FILE")
    _usage_exits_3(parser)


def _usage_exits_3(parser):
    """argparse's usage errors exit 2, which `test` uses for needs review: they exit 3 here."""
    parse_known = parser.parse_known_args

    def parse_known_args(args=None, namespace=None):
        namespace, extras = parse_known(args, namespace)
        stray = [item for item in extras if item.startswith("-")]
        if stray:
            parser.error(f"unrecognized arguments: {' '.join(stray)}")
        if extras:
            namespace.paths = list(getattr(namespace, "paths", None) or []) + extras
        return namespace, []

    def error(message):
        parser.print_usage(sys.stderr)
        hint = ""
        try:
            from ..devtools import option_hint
            hint = option_hint(parser, message)
        except ImportError:
            pass
        print(f"{parser.prog}: error: {message}{hint}", file=sys.stderr, flush=True)
        raise SystemExit(USAGE_EXIT)
    parser.parse_known_args = parse_known_args
    parser.error = error
