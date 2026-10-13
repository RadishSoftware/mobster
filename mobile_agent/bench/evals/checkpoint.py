"""L2 prototype: checkpointed starts, by snapshotting the iOSWorld apps' data on a simulator.

    python -m mobile_agent.bench.evals.checkpoint save    --udid UDID --repo IOSWORLD --name NAME --root DIR
    python -m mobile_agent.bench.evals.checkpoint restore --udid UDID --repo IOSWORLD --name NAME --root DIR
    python -m mobile_agent.bench.evals.checkpoint digest  --udid UDID --repo IOSWORLD

A checkpoint is what iOSWorld's own reset wipes. For each app, that's the data folders of its data container:
UserDefaults, Application Support, caches, cookies, saved state, WebKit, Documents. It also includes the shared
app-group containers the suite apps use. Saving and restoring copy with APFS clones (``cp -c``), so a checkpoint
takes about a second and almost no disk until an app writes.

Two uses:
- A reset: restoring the freshly seeded checkpoint replaces iOSWorld's wipe-and-relaunch-every-app reset.
- A crux start: a checkpoint taken just before a task's hard part lets a run begin there.

``digest`` hashes the persistent folders (Preferences, Application Support, Documents), so a restore can be
checked against its save. It refuses a simulator whose name doesn't start with one of ``--allow`` (default "Eval
Lab"), so the tuning loop's simulators are never touched.
"""

import argparse
import hashlib
import json
import os
import pathlib
import plistlib
import shutil
import subprocess
import time

DATA_DIRS = ("Library/Preferences", "Library/Application Support", "Library/Caches", "Library/Cookies",
             "Library/SplashBoard", "Library/Saved Application State", "Library/WebKit", "Documents")
PERSISTENT = ("Library/Preferences", "Library/Application Support", "Documents")
SHARED_GROUPS = ("group.com.iosworld.benchmark.workspacesuite",)
DEVICES = pathlib.Path("~/Library/Developer/CoreSimulator/Devices").expanduser()


def device_name(udid):
    out = subprocess.run(["xcrun", "simctl", "list", "devices", "-j"], capture_output=True, text=True, check=True).stdout
    for devices in json.loads(out)["devices"].values():
        for d in devices:
            if d["udid"] == udid:
                return d["name"]
    return None


def guard(udid, allow=("Eval Lab",)):
    name = device_name(udid)
    if not name or not name.startswith(tuple(allow)):
        raise SystemExit(f"refusing simulator {udid} ({name!r}): only simulators named {allow} are touched")
    return name


def manifest(repo):
    """{app: bundle id} from iOSWorld's bootstrap manifest."""
    path = next(iter(sorted(pathlib.Path(repo).expanduser().rglob(".app_manifest.json"))), None)
    if path is None:
        raise FileNotFoundError("no .app_manifest.json: run iOSWorld's bootstrap first")
    return {app: entry["bundle_id"] for app, entry in json.loads(path.read_text()).items() if entry.get("bundle_id")}


def app_container(udid, bundle):
    out = subprocess.run(["xcrun", "simctl", "get_app_container", udid, bundle, "data"], capture_output=True, text=True)
    path = out.stdout.strip()
    return pathlib.Path(path) if out.returncode == 0 and path else None


def group_containers(udid, groups=SHARED_GROUPS):
    root = DEVICES / udid / "data" / "Containers" / "Shared" / "AppGroup"
    out = {}
    for folder in root.iterdir() if root.is_dir() else ():
        meta = folder / ".com.apple.mobile_container_manager.metadata.plist"
        try:
            group = plistlib.loads(meta.read_bytes()).get("MCMMetadataIdentifier")
        except (OSError, plistlib.InvalidFileException):
            continue
        if group in groups:
            out[group] = folder
    return out


def terminate(udid, bundles):
    for bundle in bundles:
        subprocess.run(["xcrun", "simctl", "terminate", udid, bundle], capture_output=True)


def _clear(folder):
    for entry in folder.iterdir() if folder.is_dir() else ():
        if entry.name.startswith(".com.apple.mobile_container_manager"):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _clone(src, dst):
    """Copy ``src``'s contents into ``dst`` with APFS clones (``cp -c``; a plain copy elsewhere)."""
    dst.mkdir(parents=True, exist_ok=True)
    entries = [e for e in src.iterdir() if not e.name.startswith(".com.apple.mobile_container_manager")]
    if entries:
        subprocess.run(["cp", "-cRp", *map(str, entries), str(dst)], check=True)


def save(udid, bundles, dest):
    """Snapshot every app's data folders and the shared groups into ``dest`` (which must not exist)."""
    dest = pathlib.Path(dest)
    if dest.exists():
        raise FileExistsError(f"{dest} exists: a checkpoint is never overwritten")
    started = time.monotonic()
    terminate(udid, bundles.values())
    for app, bundle in bundles.items():
        container = app_container(udid, bundle)
        if container is None:
            continue
        for rel in DATA_DIRS:
            if (container / rel).is_dir():
                _clone(container / rel, dest / "apps" / bundle / rel)
    for group, folder in group_containers(udid).items():
        _clone(folder, dest / "groups" / group)
    meta = {"udid": udid, "saved": time.strftime("%Y-%m-%dT%H:%M:%S"), "apps": bundles,
            "digest": digest(udid, bundles), "seconds": round(time.monotonic() - started, 2)}
    (dest / "checkpoint.json").write_text(json.dumps(meta, indent=1))
    return meta


def restore(udid, bundles, src):
    """Put a checkpoint back: every app's data folders and the shared groups, as saved. Returns (seconds, ok)."""
    src = pathlib.Path(src)
    meta = json.loads((src / "checkpoint.json").read_text())
    started = time.monotonic()
    terminate(udid, bundles.values())
    for app, bundle in bundles.items():
        container = app_container(udid, bundle)
        if container is None:
            continue
        for rel in DATA_DIRS:
            target = container / rel
            if target.is_dir():
                _clear(target)
            saved = src / "apps" / bundle / rel
            if saved.is_dir():
                _clone(saved, target)
    for group, folder in group_containers(udid).items():
        _clear(folder)
        if (src / "groups" / group).is_dir():
            _clone(src / "groups" / group, folder)
    seconds = time.monotonic() - started
    return round(seconds, 2), digest(udid, bundles) == meta["digest"]


def digest(udid, bundles):
    """sha256 over the persistent folders' files (paths and bytes), apps in name order, then the shared groups."""
    h = hashlib.sha256()
    roots = []
    for app in sorted(bundles):
        container = app_container(udid, bundles[app])
        if container is not None:
            roots += [(f"{bundles[app]}/{rel}", container / rel) for rel in PERSISTENT]
    roots += [(f"group/{g}", f) for g, f in sorted(group_containers(udid).items())]
    for label, root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                if name.startswith(".com.apple.mobile_container_manager"):
                    continue
                path = pathlib.Path(dirpath) / name
                h.update(f"{label}/{path.relative_to(root)}".encode())
                try:
                    h.update(path.read_bytes())
                except OSError:
                    pass
    return h.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.evals.checkpoint", description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("save", "restore", "digest"))
    parser.add_argument("--udid", required=True)
    parser.add_argument("--repo", required=True, help="the iOSWorld checkout (or a folder holding its bootstrap manifest)")
    parser.add_argument("--name")
    parser.add_argument("--root", default="~/mobster-work/evals/checkpoints")
    parser.add_argument("--allow", nargs="*", default=["Eval Lab"])
    args = parser.parse_args(argv)
    guard(args.udid, tuple(args.allow))
    bundles = manifest(args.repo)
    folder = pathlib.Path(args.root).expanduser() / (args.name or "")
    if args.action == "save":
        print(json.dumps({k: v for k, v in save(args.udid, bundles, folder).items() if k != "apps"}))
    elif args.action == "restore":
        seconds, ok = restore(args.udid, bundles, folder)
        print(json.dumps({"restored": str(folder), "seconds": seconds, "digest_matches": ok}))
        return 0 if ok else 1
    else:
        print(digest(args.udid, bundles))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
