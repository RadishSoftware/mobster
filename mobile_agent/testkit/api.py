import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from ..devkit import links
from ..verify.checks import project_root
from ..verify.fixtures import FixtureError, with_fixtures
from . import a11y_baseline
from .discover import (
    DiscoveryError,
    apply_quarantine,
    discover,
    in_locales,
    in_matrix,
    load_quarantine,
    parse_shard,
    with_names,
    with_tags,
)
from .discover import shard as take_shard
from .targets import TargetError, resolve

RESULTS_DIR = "test-results"


class UsageError(Exception):
    """A problem with how `mobster test` was asked: one plain sentence (exit 3)."""


@dataclass
class Options:
    paths: tuple = ()
    sims: tuple = ()
    devices: tuple = ()
    # --android [AVD or serial]: "" is any idle emulator (MOBSTER_EXPERIMENTAL_ANDROID)
    android: Optional[str] = None
    matrix: Optional[str] = None
    parallel: int = 1
    retries: Optional[int] = None     # None: 1, or 0 with --repeat (a flake run measures, it doesn't rescue)
    repeat: int = 1
    quarantine: Optional[str] = None
    tags: tuple = ()
    shard: Optional[str] = None
    junit: Optional[str] = None
    html: Optional[str] = None
    strict: bool = False
    keyless: bool = False
    video: bool = False
    assert_timeout: float = 5
    command: Optional[str] = None
    testflight: tuple = ()            # bundle IDs of the person's TestFlight builds (--testflight, --device only)
    fresh: bool = False               # --fresh: Smart runs every step and records the path again
    no_heal: bool = False             # --no-heal: replay, and a changed step fails the check (strict CI)
    audit: tuple = ()                 # --audit a11y: the accessibility audit, added to every check
    a11y: Optional[str] = None        # --a11y: "warn" or "strict" for every check (a check's own a11y: strict stays)
    # --update-a11y-baseline: write this run's findings as .mobster/a11y-baseline.json
    update_a11y_baseline: bool = False
    spread: bool = False              # --spread: each check runs once, on whichever device is free
    locales: tuple = ()               # --locale de,ja,ar: every check runs once in each language (conditions.locale)
    update_snapshots: bool = False    # --update-snapshots: write each check's snapshot baseline from this run
    names: tuple = ()                 # --check NAME: only checks with this name or file name
    auto_quarantine: bool = False     # --auto-quarantine: checks the history calls flaky are quarantined for this run
    xctest: bool = False              # --xctest: also run the project's XCTest and Swift Testing targets (xctest.py)
    xctest_scheme: Optional[str] = None   # --scheme
    xctest_project: Optional[str] = None  # --project
    xctest_plan: Optional[str] = None     # --test-plan
    xctest_only: tuple = ()               # --only-testing, repeatable

    @property
    def replay_mode(self):
        """The replay core's mode (replay_core/checks.py): off with --keyless (no Smart at all), fresh with --fresh,
        strict with --no-heal, else auto."""
        if self.keyless:
            return "off"
        if self.fresh:
            return "fresh"
        return "strict" if self.no_heal else "auto"

    @property
    def effective_retries(self):
        if self.retries is not None:
            return self.retries
        return 0 if self.repeat > 1 else 1

    def public(self):
        out = self._public()
        if self.xctest:
            out["xctest"] = True
        return out

    def _public(self):
        return {"retries": self.effective_retries, "repeat": self.repeat, "parallel": self.parallel,
                "strict": self.strict, "keyless": self.keyless, "video": self.video, "tags": list(self.tags),
                "shard": self.shard, "replay": self.replay_mode, "audit": list(self.audit), "a11y": self.a11y,
                "locales": list(self.locales), "spread": self.spread, "updateSnapshots": self.update_snapshots,
                "autoQuarantine": self.auto_quarantine}


@dataclass
class Planned:
    root: Path
    entries: list
    targets: list
    options: Options
    notes: list = field(default_factory=list)
    xctest_build: Optional[dict] = None   # xctest.build's result when `test pr` already built for testing
    changed: Optional[tuple] = None       # the files a PR changed: they pick the XCTest targets to run
    a11y_snapshot: Optional[str] = None


MAX_LOCALES = 12


def parse_locales(values):
    """``--locale de,ja_JP --locale ar`` as ("de", "ja_JP", "ar"), each checked as conditions.locale is. UsageError."""
    from ..verify.assertions import CheckError
    from ..verify.conditions import parse
    out = []
    for text in values or ():
        for item in str(text).split(","):
            if not item.strip():
                continue
            try:
                locale = parse({"locale": item.strip()})["locale"]
            except CheckError as error:
                raise UsageError(f"--locale {item.strip()!r}: write a language, and a region if you like, such as de, "
                                 "ja or pt_BR.") from error
            if locale not in out:
                out.append(locale)
    if len(out) > MAX_LOCALES:
        raise UsageError(f"--locale names {len(out)} languages; the most is {MAX_LOCALES}. "
                         "Run the rest in another run.")
    return tuple(out)


def check_options(options):
    """Refuse, before anything is read or built, the flags no run of `mobster test` or `test pr` can honour together."""
    if options.repeat > 1 and options.fresh:
        raise UsageError("--repeat needs one approved recording; run --fresh separately before measuring it.")
    if options.repeat > 1 and options.spread:
        raise UsageError("--repeat needs one target per check; leave --spread out when measuring repetitions.")


def resolve_targets(options, *, root, named_device=None, matrix=None):
    """The devices and simulators a run uses, from --sim, --device, --android and a matrix. Raises UsageError."""
    try:
        targets = resolve(sims=options.sims, devices=options.devices, matrix=matrix, root=root,
                          named_device=named_device, android=options.android)
    except TargetError as error:
        raise UsageError(str(error)) from None
    if options.spread and len(targets) < 2:
        raise UsageError("--spread shares the checks between two or more devices, and this run has "
                         f"{len(targets)}. Name more with --device or --sim, or leave --spread out.")
    return targets


def plan(options, *, cwd=None, named_device=None):
    """The checks and targets of a run. Raises UsageError."""
    check_options(options)
    cwd = Path(cwd or Path.cwd())
    first = options.paths[0] if options.paths else None
    start = (cwd / first) if first and not Path(first).is_absolute() else (Path(first) if first else cwd)
    root = project_root(start if start.exists() else cwd)
    notes = []
    try:
        try:
            entries = discover(options.paths, root, cwd=cwd)
        except DiscoveryError:
            if not options.xctest or options.paths:
                raise
            entries = []   # --xctest alone runs the project's tests when it has no saved checks
        if options.quarantine:
            listed = load_quarantine(_path(options.quarantine, cwd), required=True)
        else:
            listed = load_quarantine(root / ".mobster" / "quarantine.yaml")
        for key in apply_quarantine(entries, listed):
            notes.append(f"The quarantine file names {key!r}, which no check matches.")
        if options.auto_quarantine:
            from .history import flaky_reasons
            reasons = flaky_reasons(root)
            for entry in entries:
                if entry.quarantined is None and entry.file in reasons:
                    entry.quarantined = reasons[entry.file]
                    notes.append(f"{entry.name} is quarantined for this run: {entry.quarantined}.")
        baseline_snapshot = a11y_baseline.snapshot(root) if options.update_a11y_baseline else None
        baseline = frozenset() if options.update_a11y_baseline else a11y_baseline.load(root)
        prepare_entries(entries, root, baseline=baseline, a11y=options.a11y, audit=options.audit,
                        refresh=options.update_a11y_baseline)
        if options.tags:
            entries = with_tags(entries, options.tags)
            if not entries:
                raise UsageError(f"No check has the tag {', '.join(options.tags)}.")
        if options.names:
            entries = with_names(entries, options.names)
            if not entries:
                raise UsageError(f"No check is called {', '.join(options.names)}.")
        if options.update_snapshots:
            for entry in entries:
                if entry.check is not None:
                    entry.check = replace(entry.check, snapshot_update=True)
            if not any(entry.check is not None and entry.check.snapshot for entry in entries):
                notes.append("--update-snapshots found no check with a snapshot: in its expect.")
        if options.shard:
            index, count = parse_shard(options.shard)
            entries = take_shard(entries, index, count)
            if not entries:
                notes.append(f"Shard {index} of {count} has no checks.")
        if options.locales:
            entries = in_locales(entries, options.locales)
        entries = in_matrix(entries)
    except (DiscoveryError, FixtureError, a11y_baseline.BaselineError, links.LinksError) as error:
        raise UsageError(str(error)) from None
    targets = resolve_targets(options, root=root, named_device=named_device,
                              matrix=_path(options.matrix, cwd) if options.matrix else None)
    return Planned(root=root, entries=entries, targets=targets, options=options, notes=notes,
                   a11y_snapshot=baseline_snapshot)


def prepare_entries(entries, root, *, baseline, a11y=None, audit=(), refresh=False):
    """Apply the same saved-check link and accessibility context to suites and local scenarios."""
    _prepare_fixtures(entries, root)
    _expand_link_steps(entries, root)
    for entry in entries:
        if entry.check is None:
            continue
        level = "strict" if "strict" in (a11y, entry.check.a11y) else a11y or entry.check.a11y
        entry.check = replace(entry.check, a11y=level, a11y_scope=entry.file, a11y_baseline=baseline,
                              a11y_refresh=refresh)
        if audit:
            entry.check = replace(entry.check, audit=tuple(dict.fromkeys(entry.check.audit + audit)))


def _prepare_fixtures(entries, root):
    for entry in entries:
        if entry.check is not None:
            try:
                entry.check = with_fixtures(entry.check, root)
            except FixtureError as error:
                entry.check, entry.error = None, str(error)


def _expand_link_steps(entries, root):
    """"open cart" or "open link cart" in a step becomes "Open the link myapp://cart" when the project has a link called
    cart (devkit/links.py)."""
    wanted = [entry for entry in entries if entry.check is not None
              and any(links.OPEN_STEP.match(step) for step in entry.check.steps)]
    if not wanted:
        return
    registry = links.discover(root, deep=False)
    for entry in wanted:
        entry.check = replace(entry.check, steps=links.expand_steps(entry.check.steps, registry))


def _heal_diffs_from(folder, results):
    """Each heal's ``diff`` as a path relative to the suite folder (results.json is read from there)."""
    import os
    for check in results.get("checks") or ():
        for cell in check.get("results") or ():
            for heal in cell.get("healed") or ():
                if isinstance(heal, dict) and heal.get("diff"):
                    try:
                        heal["diff"] = os.path.relpath(heal["diff"], folder)
                    except ValueError:
                        pass


def _path(text, cwd):
    path = Path(text).expanduser()
    return path if path.is_absolute() else Path(cwd) / path


def _testflight_builds(root, bundles, devices=()):
    """The build `mobster testflight install` recorded for each --testflight app: what the run's report names."""
    from ..devkit.testflight import recorded_build
    found = (recorded_build(root, bundle, devices=devices) for bundle in bundles or ())
    return [build for build in found if build]


def execute(planned, *, progress=None, stop=None, key=None, attempts=None, gate=None, named_device=None,
            suite_id=None, cwd=None, on_suite=None, before_attempt=None):
    """Run the plan and write its reports."""
    from .. import __version__
    from ..verify.runs import ensure_gitignore
    from .provenance import FIELDS, case_provenance, require_case_provenance, unpinned
    from .reports import document, make_suite_dir, write_all
    from .suite import Suite
    from .targets import DeviceGate
    # Direct Planned callers (including PR builds) keep their app override while gaining the same effective steps.
    try:
        _prepare_fixtures(planned.entries, planned.root)
        _expand_link_steps(planned.entries, planned.root)
    except (FixtureError, links.LinksError) as error:
        raise UsageError(str(error)) from None
    options = planned.options
    progress = progress or (lambda line: None)
    stop = stop or threading.Event()
    mobster = planned.root / ".mobster"
    runs_dir = mobster / "runs"
    ensure_gitignore(runs_dir)
    suite_id, folder = make_suite_dir(mobster / RESULTS_DIR, suite_id)
    if attempts is None:
        from .execute import Attempts
        attempts = Attempts(runs_dir=runs_dir, keyless=options.keyless, key=key, assert_timeout=options.assert_timeout,
                            parallel=options.parallel, progress=progress, stop=stop,
                            video_dir=folder / "videos" if options.video else None, replay=options.replay_mode)
    gate = gate or DeviceGate(progress=progress, testflight=options.testflight)
    initial_cache = {}
    provenance = {id(entry): case_provenance(entry, planned.root, asset_cache=initial_cache)
                  for entry in planned.entries}
    if options.repeat > 1:
        for entry in planned.entries:
            # A repeat's pin holds the app's version, not its bytes, so its runs could span two builds of one version.
            if entry.check is not None and entry.check.app_path and provenance[id(entry)]["appDigest"] is None:
                entry.check, entry.error = None, (
                    f"--repeat needs one pinned build, and Mobster could not pin {Path(entry.check.app_path).name}: "
                    "it is missing, holds a symlink, or is over 2 GB or 50,000 files. Run it without --repeat.")
                provenance[id(entry)] = case_provenance(entry, planned.root)

    def admitted_attempt(entry, check, record):
        frozen = provenance[id(entry)]
        require_case_provenance(entry, planned.root, frozen)
        if before_attempt is not None:
            before_attempt(entry, check, record)
            require_case_provenance(entry, planned.root, frozen)

    from .gitref import checkout
    source = checkout(planned.root)  # the branch and commit as the suite starts: what it tests
    suite = Suite(planned.entries, planned.targets, attempt=attempts, retries=options.effective_retries,
                  repeat=options.repeat, parallel=options.parallel, strict=options.strict, gate=gate,
                  named_device=named_device, progress=progress, stop=stop, spread=options.spread,
                  on_cancel=getattr(attempts, "abort_all", None), before_attempt=admitted_attempt)
    if on_suite is not None:
        on_suite(suite)
    results = suite.run()
    if suite.interrupted is None:
        from ..verify.evidence import late_reports
        late_reports(results, progress=progress)  # the stacks macOS hadn't written when their runs ended
    final_cache = {}
    for entry, item in zip(planned.entries, results["checks"]):
        frozen = provenance[id(entry)]
        drifted = case_provenance(entry, planned.root, asset_cache=final_cache) != frozen
        item.update({field: None if drifted else frozen[field] for field in FIELDS if field != "appDigest"})
        if drifted:
            item["provenanceDrift"] = True
        for cell in item["results"]:
            cell["appDigest"] = None if drifted else frozen["appDigest"]
    suite.seal()
    terminal = suite.results()
    results["stopReason"], results["exitCode"] = terminal["stopReason"], terminal["exitCode"]
    _heal_diffs_from(folder, results)
    doc = document(results, suite_id=suite_id, options=options.public(), version=__version__,
                   command=options.command,
                   testflight=_testflight_builds(planned.root, options.testflight, options.devices), git=source)
    doc["executionPolicy"] = {"retries": options.effective_retries, "repeat": options.repeat,
                              "replay": options.replay_mode, "keyless": options.keyless,
                              "assertTimeout": options.assert_timeout, "mobster": __version__}
    notes = list(planned.notes)
    for entry in planned.entries:
        names = unpinned(provenance[id(entry)], entry.check) if entry.check is not None else []
        if names and not provenance[id(entry)]["inputChanged"]:
            note = (f"Mobster could not pin the {' and '.join(names)} of {entry.file}: a file is missing, is a symlink "
                    "or is over its size limit. `mobster test compare` can't report a verified fix for it.")
            notes.append(note)
            progress(note)
    if notes:
        doc["notes"] = notes
    if options.xctest and suite.interrupted is None and doc.get("stopReason") is None:
        from . import xctest
        try:
            doc["xctest"] = xctest.run_suite(planned, attempts, folder, progress)
            xctest.merge_summary(doc)
        except KeyboardInterrupt as stop:   # the checks' results are kept, as when ctrl+c stops them
            signum = getattr(stop, "signum", None)
            doc["exitCode"] = 130 if signum is None else 128 + int(signum)
            doc["stopped"], doc["stopReason"] = True, "stopped"
    if options.update_a11y_baseline:
        error = a11y_baseline.refresh_error(doc, planned.entries)
        if error is None:
            try:
                if planned.a11y_snapshot is None:
                    error = "The accessibility baseline was not pinned at planning. Start a new run."
                else:
                    doc["a11yBaseline"] = a11y_baseline.write(planned.root, doc,
                                                                expected_snapshot=planned.a11y_snapshot)
            except a11y_baseline.BaselineError as issue:
                error = str(issue)
        if error is not None:
            if doc.get("stopReason") != "stopped":
                doc["exitCode"] = 3
            doc.setdefault("notes", []).append(error)
            progress(error)
    from .history import record
    record(planned.root, doc)  # each result's history, and the checks to suggest for quarantine
    paths = write_all(doc, folder, junit_path=_path(options.junit, cwd or Path.cwd()) if options.junit else None,
                      html_dir=_path(options.html, cwd or Path.cwd()) if options.html else None,
                      strict=options.strict, cwd=cwd)
    paths["folder"] = folder
    from .recent import remember
    remember(doc, folder, planned.root)  # for the Mac app's Test results page
    from ..memory import writer
    writer.from_suite(doc, planned)  # the project, the checks' paths and the heals become memory
    return doc, paths
