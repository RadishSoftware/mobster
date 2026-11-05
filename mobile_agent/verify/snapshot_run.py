"""The snapshot step of one verify run: take the screen, compare it to its baseline, record or update the baseline."""

import shutil
from pathlib import Path

from . import snapshots
from .checks import project_root

LEVEL_MESSAGES = {
    snapshots.CHANGED: "{pct} of the screen changed; the limit is {limit}. Open the diff, and run "
                       "`mobster test --update-snapshots` if the new screen is right.",
    snapshots.SIZE_CHANGED: "the screen is {size} px and the baseline is {base} px. Run the check on the baseline's "
                            "device, or `mobster test --update-snapshots` to record this size.",
}


def _percent(value):
    return f"{value:.2f} %" if value >= .01 or value == 0 else "under 0.01 %"


def project_of(run):
    """The folder holding .mobster/ for this run, or None."""
    if run.runs_dir.parent.name == ".mobster":
        return run.runs_dir.parent.parent
    return project_root(run.check.source) if run.check.source else None


def _finding(name, level, message, frame, evidence):
    return {"id": f"snapshot {name}", "audit": "snapshot", "guideline": None, "level": level, "frame": frame,
            "message": f"snapshot {name}: {message}", "evidence": evidence}


def take(run, tree, frame):
    """Compare the screen to the check's baseline. Returns (the result dict or None, findings). Never raises: a
    snapshot that can't be taken is a finding for a person, not a crash."""
    from PIL import Image
    check = run.check
    spec = check.snapshot
    name = spec.name
    root = project_of(run)
    if root is None:
        return None, [_finding(name, "review", "Mobster has no project folder to keep the baseline in. Run the check "
                                               "from a folder with .mobster/.", frame, "no .mobster folder")]
    target = run.target
    device = getattr(target, "device_type", None) or "iphone"
    key = snapshots.check_key(check.source, root, check.name)
    baseline = snapshots.baseline_file(root, key, name, device, check.conditions)
    folder = run.run_dir / snapshots.FOLDER
    current_path = folder / f"{name}-current.png"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        run._call(run.manager.screenshot, target, current_path, quality=100)
        with Image.open(current_path) as opened:
            current = opened.convert("RGB")
    except Exception as error:
        run.log(f"snapshot: no screenshot ({type(error).__name__})")
        return None, [_finding(name, "review", "Mobster couldn't take the screenshot, so nothing was compared.",
                               frame, "no screenshot")]
    rects = snapshots.mask_rects(spec, tree, current.width, status_bar_set=run._status_bar_set)
    relative = baseline.relative_to(root).as_posix()
    result = {"name": name, "key": relative, "status": None, "thresholdPercent": spec.threshold,
              "size": [current.width, current.height], "masks": [[round(v) for v in rect] for rect in rects],
              "regions": [], "changedPercent": 0.0, "changedPixels": 0, "comparedPixels": 0, "diffMs": None,
              "baseline": None, "current": current_path.relative_to(run.run_dir).as_posix(), "diff": None,
              "device": device, "appearance": (check.conditions or {}).get("appearance"),
              "contentSize": (check.conditions or {}).get("content_size"),
              "locale": (check.conditions or {}).get("locale"), "statusBarSet": run._status_bar_set}
    existed = baseline.is_file()
    if not existed and not check.snapshot_record:
        return None, [_finding(name, "fail", f"there's no baseline at {relative}, and an approved CI suite never "
                                             "records one. Record it with `mobster test --update-snapshots`, then "
                                             "approve a new revision.", frame, relative)]
    if check.snapshot_update or not existed:
        snapshots.save_png(current, baseline)
        result["status"] = snapshots.UPDATED if existed else snapshots.RECORDED
        result["baseline"] = _copy_baseline(baseline, folder, name, run.run_dir)
        run.log(f"snapshot {name}: baseline {result['status']} at {relative}")
        if check.snapshot_update:
            return result, []
        return result, [_finding(name, "review", f"baseline recorded at {relative}. Review the screen, commit the "
                                                 "file, and the next run compares to it.", frame, relative)]
    try:
        with Image.open(baseline) as opened:
            old = opened.convert("RGB")
    except Exception as error:
        run.log(f"snapshot: unreadable baseline ({type(error).__name__})")
        return result, [_finding(name, "review", f"the baseline {relative} can't be read as an image. Run "
                                                 "`mobster test --update-snapshots` to write it again.", frame,
                                 relative)]
    result["baseline"] = _copy_baseline(baseline, folder, name, run.run_dir)
    found = snapshots.compare(old, current, rects, spec.threshold)
    result.update(status=found.status, changedPercent=round(found.changed_percent, 4),
                  changedPixels=found.changed_pixels, comparedPixels=found.compared_pixels, regions=found.regions,
                  diffMs=round(found.seconds * 1000))
    run.log(f"snapshot {name}: {found.status}, {_percent(found.changed_percent)} changed, {result['diffMs']} ms")
    if found.status == snapshots.MATCHED:
        return result, []
    evidence = relative
    if found.diff is not None:
        diff_path = folder / f"{name}-diff.png"
        snapshots.save_png(found.diff, diff_path)
        result["diff"] = evidence = diff_path.relative_to(run.run_dir).as_posix()
    message = LEVEL_MESSAGES[found.status].format(
        pct=_percent(found.changed_percent), limit=f"{spec.threshold:g} %", size="x".join(map(str, current.size)),
        base="x".join(map(str, old.size)))
    return result, [_finding(name, "fail", message, frame, evidence)]


def _copy_baseline(baseline, folder, name, run_dir):
    """A copy of the baseline in the run's folder, so a shared report carries all three images."""
    copy = Path(folder) / f"{name}-baseline.png"
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(baseline, copy)
    return copy.relative_to(run_dir).as_posix()
