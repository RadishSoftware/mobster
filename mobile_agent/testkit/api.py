"""`mobster test` as two calls, for the CLI and the MCP tools: ``plan`` (which checks on which targets) and
``execute`` (run them, write the reports).

    planned = plan(Options(paths=["checks/"], sims=["iPhone 17 Pro"]))
    doc, paths = execute(planned, progress=print)      # doc: results.json; paths: where the reports went
"""

from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import Optional

from .discover import DiscoveryError, apply_quarantine, discover, load_quarantine, parse_shard, project_root, \
    shard as take_shard, with_tags
from .targets import TargetError, resolve

RESULTS_DIR = "test-results"


class UsageError(Exception):
    """A problem with how `mobster test` was asked: one plain sentence (exit 3)."""


@dataclass
class Options:
    paths: tuple = ()
    sims: tuple = ()
    devices: tuple = ()
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

    @property
    def effective_retries(self):
        if self.retries is not None:
            return self.retries
        return 0 if self.repeat > 1 else 1

    def public(self):
        return {"retries": self.effective_retries, "repeat": self.repeat, "parallel": self.parallel,
                "strict": self.strict, "keyless": self.keyless, "video": self.video, "tags": list(self.tags),
                "shard": self.shard}


@dataclass
class Planned:
    root: Path
    entries: list
    targets: list
    options: Options
    notes: list = field(default_factory=list)


def plan(options, *, cwd=None, named_device=None):
    """The checks and targets of a run. Raises UsageError."""
    cwd = Path(cwd or Path.cwd())
    first = options.paths[0] if options.paths else None
    start = (cwd / first) if first and not Path(first).is_absolute() else (Path(first) if first else cwd)
    root = project_root(start if start.exists() else cwd)
    notes = []
    try:
        entries = discover(options.paths, root, cwd=cwd)
        if options.quarantine:
            listed = load_quarantine(_path(options.quarantine, cwd), required=True)
        else:
            listed = load_quarantine(root / ".mobster" / "quarantine.yaml")
        for key in apply_quarantine(entries, listed):
            notes.append(f"The quarantine file names {key!r}, which no check matches.")
        if options.tags:
            entries = with_tags(entries, options.tags)
            if not entries:
                raise UsageError(f"No check has the tag {', '.join(options.tags)}.")
        if options.shard:
            index, count = parse_shard(options.shard)
            entries = take_shard(entries, index, count)
            if not entries:
                notes.append(f"Shard {index} of {count} has no checks.")
        targets = resolve(sims=options.sims, devices=options.devices,
                          matrix=_path(options.matrix, cwd) if options.matrix else None, root=root,
                          named_device=named_device)
    except (DiscoveryError, TargetError) as error:
        raise UsageError(str(error)) from None
    return Planned(root=root, entries=entries, targets=targets, options=options, notes=notes)


def _path(text, cwd):
    path = Path(text).expanduser()
    return path if path.is_absolute() else Path(cwd) / path


def execute(planned, *, progress=None, stop=None, key=None, attempts=None, gate=None, named_device=None,
            suite_id=None, cwd=None, on_suite=None):
    """Run the plan and write its reports. Returns (results.json's document, {report: path}).

    ``attempts`` (execute.Attempts or a fake with ``__call__`` and ``abort_all``) runs each attempt; ``on_suite``
    is called with the Suite before it starts (the MCP tools read its progress)."""
    from .. import __version__
    from ..verify.runner import ensure_gitignore
    from .reports import document, make_suite_dir, write_all
    from .suite import Suite
    from .targets import DeviceGate
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
                            video_dir=folder / "videos" if options.video else None)
    gate = gate or DeviceGate(progress=progress)
    suite = Suite(planned.entries, planned.targets, attempt=attempts, retries=options.effective_retries,
                  repeat=options.repeat, parallel=options.parallel, strict=options.strict, gate=gate,
                  named_device=named_device, progress=progress, stop=stop,
                  on_interrupt=getattr(attempts, "abort_all", None))
    if on_suite is not None:
        on_suite(suite)
    results = suite.run()
    doc = document(results, suite_id=suite_id, options=options.public(), version=__version__,
                   command=options.command)
    if suite.interrupted is not None:
        signum = getattr(suite.interrupted, "signum", None)
        doc["exitCode"] = 130 if signum is None else 128 + int(signum)
        doc["stopped"] = True
    if planned.notes:
        doc["notes"] = list(planned.notes)
    paths = write_all(doc, folder, junit_path=_path(options.junit, cwd or Path.cwd()) if options.junit else None,
                      html_dir=_path(options.html, cwd or Path.cwd()) if options.html else None,
                      strict=options.strict, cwd=cwd)
    paths["folder"] = folder
    return doc, paths
