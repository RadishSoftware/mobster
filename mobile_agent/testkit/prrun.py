from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from . import pr as prmod
from . import pr_report
from .prbuild import BuildError

SMOKE = "smoke"
DRAFT_MODES = ("run", "write", "off")


@dataclass
class PrOptions:
    base: Optional[str] = None       # default: the branch the PR merges into (pr.default_base)
    build: bool = True
    scheme: Optional[str] = None
    project: Optional[str] = None    # an .xcodeproj or .xcworkspace; default the one in the project
    configuration: str = "Debug"
    drafts: str = "run"              # run | write | off
    gifs: bool = True
    tested_with: bool = False        # end the PR comment with "Tested with Mobster" (opt-in)
    github: str = "check"            # check | comment | off: Mobster Checks on the PR (github_checks.py)


@dataclass
class PrResult:
    pr: dict
    doc: dict
    paths: dict
    exit_code: int
    notes: list = field(default_factory=list)


def with_build(entries, built, testflight):
    """Point each check of the built app at the new build; a TestFlight app's checks use the build on the phone."""
    out = []
    for entry in entries:
        check = entry.check
        if check is None:
            out.append(entry)
            continue
        if check.bundle_id and check.bundle_id in testflight:
            entry = replace(entry, check=replace(check, app_path=None))
        elif built and built.get("path") and (
                (check.bundle_id and check.bundle_id == built.get("bundle")) or
                (not check.bundle_id and check.app_path and Path(check.app_path).name == Path(built["path"]).name)):
            entry = replace(entry, check=replace(check, app_path=built["path"]))
        out.append(entry)
    return out


def _all_checks(root):
    from .discover import DiscoveryError, discover
    if not (Path(root) / ".mobster" / "checks").is_dir():
        return []
    try:
        return discover((), root)
    except DiscoveryError:
        return []


def plan_pr(root, pr_options, *, git=prmod.run_git, entries=None, env=None):
    """(PrPlan without drafts, every saved check). Raises pr.PrError."""
    changes = prmod.read_changes(root, pr_options.base, git=git, env=env)
    screens = prmod.screens_in(changes)
    entries = _all_checks(root) if entries is None else entries
    picked = prmod.pick(entries, screens, changes)
    fallback = None
    if not picked and not screens:
        smoke = [entry for entry in entries if SMOKE in {tag.casefold() for tag in entry.tags}]
        picked = [prmod.Pick(entry, ["tagged smoke"], []) for entry in smoke]
        fallback = SMOKE if smoke else None
    return prmod.PrPlan(changes=changes, screens=screens, picked=picked, fallback=fallback), entries


def build_app(root, pr_options, options, *, runner=None, progress=None, bundle=None):
    from . import prbuild
    if not pr_options.build:
        return None
    platform = "device" if any(True for _ in options.devices) else "sim"
    if not pr_options.project and not options.xctest:
        built = _build_react_native(root, pr_options, platform, runner=runner, progress=progress, bundle=bundle)
        if built is not None:
            return built
    found = prbuild.project_arg(Path(root) / pr_options.project if not Path(pr_options.project).is_absolute()
                                else pr_options.project) if pr_options.project else prbuild.find_project(root)
    if found is None:
        return None
    scheme = pr_options.scheme or prbuild.pick_scheme(found, runner=runner, cwd=root)
    if options.xctest:   # one build-for-testing makes the app the checks drive and the tests
        from . import xctest
        return xctest.build(root, scheme=scheme, project=found, test_plan=options.xctest_plan,
                            configuration=pr_options.configuration, runner=runner, progress=progress, bundle=bundle)
    return prbuild.build(root, scheme=scheme, project=found, platform=platform,
                         configuration=pr_options.configuration, runner=runner, progress=progress, bundle=bundle)


def _build_react_native(root, pr_options, platform, *, runner=None, progress=None, bundle=None):
    """An Expo or React Native app's build, or None for any other project."""
    # A Debug React Native build loads its JavaScript from Metro, which a test run doesn't start: Debug becomes Release.
    from ..devkit import DevError, expo
    app = expo.detect(root)
    if app is None or not (app.expo or app.native):
        return None
    configuration = "Release" if pr_options.configuration == "Debug" else pr_options.configuration
    (progress or (lambda line: None))(f"{app.name} is a{'n Expo' if app.expo else ' React Native'} app: building "
                                      f"{configuration}, with the JavaScript inside")
    try:
        return expo.build_sim(app, configuration=configuration, platform=platform, runner=runner, progress=progress,
                              bundle=bundle)
    except DevError as error:
        raise BuildError(error.message, error.fix) from None


def test_pr(pr_options, options, *, root, progress=None, git=prmod.run_git, env=None, github=None, **kwargs):
    """Run the PR's checks."""
    from .github_checks import GITHUB_MODES_ERROR, MODES, Poster
    if pr_options.github not in MODES:
        from .api import UsageError
        raise UsageError(GITHUB_MODES_ERROR)
    poster = github or Poster(Path(root), pr_options.github, progress, environ=env, git=git)
    try:
        result = _test_pr(pr_options, options, root=root, progress=progress, git=git, env=env, on_plan=poster.start,
                          **kwargs)
    except BaseException:
        poster.abandon()
        raise
    poster.finish(result)
    return result


def _test_pr(pr_options, options, *, root, progress=None, stop=None, key=None, suite_id=None, on_suite=None,
             git=prmod.run_git, build_runner=None, execute=None, gif=None, env=None, named_device=None,
             attempts=None, gate=None, on_plan=None):
    from .api import Planned, UsageError, check_options, resolve_targets
    from .discover import DiscoveryError, apply_quarantine, discover, load_quarantine
    from .reports import make_suite_dir
    progress = progress or (lambda line: None)
    if pr_options.drafts not in DRAFT_MODES:
        raise UsageError(f"--drafts is one of {', '.join(DRAFT_MODES)}")
    if options.testflight and not options.devices:
        raise UsageError("--testflight goes with --device: a TestFlight build is on an iPhone, not a simulator.")
    check_options(options)
    root = Path(root)
    # before on_plan starts the PR's check run, so a usage error leaves none behind marked cancelled
    targets = resolve_targets(options, root=root, named_device=named_device)
    plan, entries = plan_pr(root, pr_options, git=git, env=env)
    if on_plan is not None:
        on_plan(plan.changes)
    notes = []
    if plan.changes.truncated:
        notes.append(f"This PR changes more than {prmod.MAX_FILES} files; Mobster read the first {prmod.MAX_FILES}.")
    progress(_plan_line(plan))
    preferred = next((p.entry.check.bundle_id for p in plan.picked if p.entry.check is not None
                      and p.entry.check.bundle_id), None)
    tested_on_phone = set(options.testflight or ())
    every_testflight = bool(options.devices) and plan.picked and all(
        p.entry.check is not None and p.entry.check.bundle_id in tested_on_phone for p in plan.picked)
    built = None
    if not every_testflight and (plan.picked or plan.screens or options.xctest):
        try:
            built = build_app(root, pr_options, options, runner=build_runner, progress=progress, bundle=preferred)
        except BuildError as error:
            return _failed_build(plan, error, root, options, suite_id, pr_options, make_suite_dir)
    picked_entries = with_build([p.entry for p in plan.picked], built, tested_on_phone)
    listed = load_quarantine(root / ".mobster" / "quarantine.yaml")
    apply_quarantine(picked_entries, listed)
    if pr_options.drafts != "off":
        app = prmod.app_of(entries, built)
        plan.drafts = prmod.draft_checks(plan.screens, plan.picked, app=app, root=root)
    draft_entries = []
    if plan.drafts and pr_options.drafts == "run":
        try:
            draft_entries = discover([str(path) for _, path in plan.drafts], root)
        except DiscoveryError:
            draft_entries = []
        for entry in draft_entries:
            entry.quarantined = pr_report.DRAFT_REASON
        draft_entries = with_build(draft_entries, built, tested_on_phone)
    run_entries = picked_entries + draft_entries
    plan_public = plan.public(root)
    head = plan.changes.head
    if not run_entries and not options.xctest:
        suite_id, folder = make_suite_dir(root / ".mobster" / "test-results", suite_id)
        doc = {"suite": suite_id, "checks": [], "exitCode": 0, "summary": {}}
        result = pr_report.document(plan_public, doc, folder=folder, frames={}, gifs={}, built=_public_build(built),
                                    head=head, tested_with=pr_options.tested_with, notes=notes)
        paths = {"folder": folder, **pr_report.write(result, folder)}
        return PrResult(result, doc, paths, 0, notes)
    planned = Planned(root=root, entries=run_entries, targets=targets, options=options, notes=list(notes),
                      xctest_build=built if options.xctest else None,
                      changed=tuple(change.path for change in plan.changes.files) if options.xctest else None)
    if execute is None:
        from .api import execute
    kwargs = {"progress": progress, "stop": stop, "key": key, "suite_id": suite_id, "on_suite": on_suite}
    if named_device is not None:
        kwargs["named_device"] = named_device
    if attempts is not None:
        kwargs["attempts"] = attempts
    if gate is not None:
        kwargs["gate"] = gate
    doc, paths = execute(planned, **kwargs)
    folder = Path(paths["folder"])
    frames = pr_report.copy_frames(doc, folder)
    gifs = pr_report.make_gifs(doc, folder, **({"build": gif} if gif else {})) if pr_options.gifs else {}
    result = pr_report.document(plan_public, doc, folder=folder, frames=frames, gifs=gifs,
                                built=_public_build(built), head=head, tested_with=pr_options.tested_with,
                                notes=list(dict.fromkeys(notes + list(doc.get("notes") or ()))))
    paths.update(pr_report.write(result, folder))
    return PrResult(result, doc, paths, int(doc.get("exitCode", 3)), notes)


def _plan_line(plan):
    checks = len(plan.picked)
    noun = "check" if checks == 1 else "checks"
    if plan.screens:
        screens = len(plan.screens)
        return (f"mobster test pr: {screens} changed screen{'' if screens == 1 else 's'}, {checks} saved {noun} "
                "cover them")
    if plan.fallback == SMOKE:
        return f"mobster test pr: no screen changed, so the {checks} {noun} tagged smoke run"
    return "mobster test pr: no screen changed and no check is tagged smoke"


def _public_build(built):
    if not built:
        return None
    return {"bundle": built.get("bundle"), "app": Path(built["path"]).name if built.get("path") else None,
            "platform": built.get("platform")}


def _failed_build(plan, error, root, options, suite_id, pr_options, make_suite_dir):
    suite_id, folder = make_suite_dir(Path(root) / ".mobster" / "test-results", suite_id)
    doc = {"suite": suite_id, "checks": [], "exitCode": 3, "summary": {}}
    note = f"{error.message} {error.fix}".strip()
    result = pr_report.document(plan.public(root), doc, folder=folder, frames={}, gifs={}, built=None,
                                head=plan.changes.head, tested_with=pr_options.tested_with, notes=[note])
    result["headline"] = "Mobster checks: the build failed"
    paths = {"folder": folder, **pr_report.write(result, folder)}
    return PrResult(result, doc, paths, 3, [note])


test_pr.__test__ = False  # a function named test_* that unittest must not collect
