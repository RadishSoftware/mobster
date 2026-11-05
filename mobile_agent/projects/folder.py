"""Choosing a project's folder, and reading what is in one.

``choose(text)`` turns what the person picked or typed into the folder a project is, or refuses with a sentence.
``summary(path)`` is what the Projects list shows: the folder's state, its apps, how many checks and workflow files
its `.mobster/` holds, its simulator matrix and its branch. ``detail(path)`` adds what costs more to read: the
checks by name and whether the branch has changes. Both read the folder each time and never write to it.

Every git call runs with no terminal prompt, no optional locks (so Mobster never takes the index lock from under
your own git) and no fsmonitor hook (so a folder's git config can't make Mobster run a program), and gives up after
3 seconds.
"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..api_errors import APIError

MAX_PATH = 1024
MAX_APPS = 8
MAX_LISTED_CHECKS = 200
GIT_TIMEOUT = 3
PBXPROJ_LIMIT = 8_000_000
# Folders an app's sources don't live in: never searched for an Xcode project.
NOT_SOURCES = frozenset({"node_modules", "Pods", "Carthage", "DerivedData", "build", "dist", "vendor"})
# Folders that hold everything rather than one app: refused as a project.
BROAD = ("Desktop", "Documents", "Downloads", "Library", "Movies", "Music", "Pictures", "Public", "Applications")
SYSTEM = ("/", "/Users", "/System", "/Library", "/Applications", "/Volumes", "/private", "/usr", "/opt", "/tmp",
          "/var", "/bin", "/sbin", "/etc", "/cores")
WORKFLOW_SUFFIXES = (".yaml", ".yml")
JSON_LIMIT = 1_000_000
ICON_DEPTH = 4
ICON_FOLDERS = 2000
ICON_LIMIT = 4_000_000
NOT_IPHONE = ("mac", "watch", "watch-marketing", "car", "tv", "vision", "ipad")
BUNDLE = re.compile(r'PRODUCT_BUNDLE_IDENTIFIER = "?([^";\s]+)"?;')
GITHUB = re.compile(r"(?:https://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
                    r"([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")


def shown(path, home=None):
    """``path`` as a sentence shows it: ~/… inside the home folder."""
    home = Path(home or Path.home())
    path = Path(path)
    try:
        return "~/" + path.relative_to(home).as_posix() if path != home else "~"
    except ValueError:
        return str(path)


def choose(text, *, home=None, data_dir=None):
    """The folder ``text`` names, resolved: an absolute path (~ allowed) to a folder Mobster can read that holds one
    project. A `.mobster` folder means the folder holding it. APIError with what to do otherwise."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_PATH or "\0" in text:
        raise APIError("Give the folder as a full path, such as ~/Developer/YourApp.", 400, "invalid_path")
    home = Path(home or Path.home()).resolve()
    given = Path(text.strip()).expanduser()
    if not given.is_absolute():
        raise APIError("Give the folder as a full path, such as ~/Developer/YourApp.", 400, "invalid_path")
    try:
        path = given.resolve(strict=True)
    except FileNotFoundError:
        raise APIError(f"There's no folder at {shown(given, home)}. Check the path, or choose the folder again.",
                       404, "missing") from None
    except (OSError, RuntimeError):
        raise APIError(f"Mobster can't read {shown(given, home)}. Check the path, or choose the folder again.",
                       403, "unreadable") from None
    if path.name == ".mobster":
        path = path.parent
    if not path.is_dir():
        raise APIError(f"{shown(path, home)} is a file. Choose the folder that holds your app.", 400, "not_folder")
    system = {Path(p).resolve() for p in SYSTEM}
    if path in system or path == home or path.parent == home and path.name in BROAD:
        raise APIError(f"{shown(path, home)} holds more than one project. Choose your app's own folder inside it.",
                       400, "too_broad")
    if data_dir is not None:
        mine = Path(data_dir).resolve()
        if path == mine or mine in path.parents:
            raise APIError("That's Mobster's own data folder. Choose your app's folder.", 400, "mobster_data")
    unreadable = (f"Mobster can't read {shown(path, home)}. Allow Mobster in System Settings › Privacy & "
                  "Security › Files and Folders, then add it again.")
    if not os.access(path, os.R_OK | os.X_OK):
        raise APIError(unreadable, 403, "unreadable")
    try:
        next(path.iterdir(), None)
    except OSError:
        raise APIError(unreadable, 403, "unreadable") from None
    repos = _repositories_in(path)
    if repos >= 2:
        raise APIError(f"{shown(path, home)} holds {repos} repositories. Choose the one with your app.", 400,
                       "too_broad")
    return path


def _repositories_in(path, limit=200):
    """How many folders directly in ``path`` are repositories or projects, when ``path`` is neither itself and has
    no app at its root; else 0."""
    try:
        if any((path / name).exists() for name in (".git", ".mobster", "package.json")) \
                or any(path.glob("*.xcodeproj")) or any(path.glob("*.xcworkspace")):
            return 0
        count = 0
        for index, child in enumerate(path.iterdir()):
            if index >= limit:
                break
            if child.is_dir() and ((child / ".git").exists() or (child / ".mobster").is_dir()):
                count += 1
        return count
    except OSError:
        return 0


# -- reading a folder -------------------------------------------------------------------------------------------

def state(path, home=None):
    """("ready" | "missing" | "unreadable", a sentence or None)."""
    path = Path(path)
    try:
        if not path.is_dir():
            return "missing", (f"The folder {shown(path, home)} isn't there any more. Move it back, or remove the "
                               "project and add the folder where it is now.")
        next(path.iterdir(), None)
    except PermissionError:
        return "unreadable", (f"Mobster can't read {shown(path, home)}. Allow Mobster in System Settings › Privacy & "
                              "Security › Files and Folders.")
    except OSError:
        return "unreadable", f"Mobster can't read {shown(path, home)}. Check the folder is still there."
    return "ready", None


def _bundle(xcodeproj):
    """The app's bundle id from the project file: the shortest PRODUCT_BUNDLE_IDENTIFIER that names no build
    setting and no test target. None when there's none."""
    try:
        with open(Path(xcodeproj) / "project.pbxproj", encoding="utf-8", errors="replace") as handle:
            text = handle.read(PBXPROJ_LIMIT)
    except OSError:
        return None
    found = {value for value in BUNDLE.findall(text) if "$" not in value and "test" not in value.lower()}
    return min(found, key=lambda value: (len(value), value)) if found else None


def _no_node(argv, cwd, *args, **kwargs):
    return 1, ""   # reading a folder never starts Node: devkit/expo.py falls back to app.json


def apps(root):
    """The apps ``root`` builds, at most MAX_APPS: an Expo or React Native app at its root, then each Xcode workspace
    (else project) at the root or one folder down. Each is {kind, name, path (relative), bundle}."""
    from ..devkit import expo
    root = Path(root)
    out = []
    native = None
    try:
        found = expo.detect(root, runner=_no_node)
    except (OSError, ValueError):
        found = None
    if found is not None:
        out.append({"kind": "expo" if found.expo else "react-native", "name": found.name, "path": ".",
                    "bundle": found.bundle})
        native = root / "ios"
    try:
        children = sorted(child for child in root.iterdir() if child.is_dir() and not child.name.startswith(".")
                          and child.name not in NOT_SOURCES and child.suffix not in (".xcodeproj", ".xcworkspace"))
    except OSError:
        children = []
    for folder in (root, *children):
        if len(out) >= MAX_APPS:
            break
        if folder == native:
            continue
        try:
            workspaces = sorted(p for p in folder.glob("*.xcworkspace") if p.is_dir())
            projects = sorted(p for p in folder.glob("*.xcodeproj") if p.is_dir())
        except OSError:
            continue
        for path in (workspaces or projects)[:MAX_APPS - len(out)]:
            twin = path.with_suffix(".xcodeproj")
            source = path if path.suffix == ".xcodeproj" else twin if twin.is_dir() else \
                (projects[0] if len(projects) == 1 else None)
            out.append({"kind": "xcode", "name": path.stem, "path": path.relative_to(root).as_posix(),
                        "bundle": _bundle(source) if source else None})
    return out


def _inside(root, path):
    """``path`` resolved, when it is a file inside ``root`` (a link can't lead out of the project), else None."""
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_file() and Path(root).resolve() in resolved.parents else None


def _json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.loads(handle.read(JSON_LIMIT))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _icon_sets(root):
    """The Xcode asset catalogs' app icon sets under ``root`` (AppIcon first), searched at most ICON_DEPTH folders down
    and through at most ICON_FOLDERS folders, never in hidden folders or NOT_SOURCES."""
    found, seen, queue = [], 0, [(Path(root), 0)]
    while queue and seen < ICON_FOLDERS:
        folder, depth = queue.pop(0)
        seen += 1
        try:
            children = sorted(child for child in folder.iterdir() if child.is_dir() and not child.is_symlink()
                              and not child.name.startswith(".") and child.name not in NOT_SOURCES)
        except OSError:
            continue
        for child in children:
            if child.suffix == ".appiconset" and folder.suffix == ".xcassets":
                found.append(child)
            elif depth < ICON_DEPTH and child.suffix not in (".appiconset", ".xcodeproj", ".xcworkspace"):
                queue.append((child, depth + 1))
    return sorted(found, key=lambda path: (path.stem != "AppIcon", "test" in str(path).lower(), str(path)))


_icons = {}          # project folder -> (when it was looked for, the icon found or None)


def icon(root, clock=time.monotonic):
    """The app's own icon in the project, a PNG file inside it: Expo's (app.json's ios.icon, else icon), else the
    largest iPhone image of an Xcode asset catalog's app icon set, AppIcon first. None when there is none. What was
    found is kept for a minute, so a list of projects doesn't search every folder each time it is read."""
    now = clock()
    kept = _icons.get(str(root))
    if kept is not None and now - kept[0] < 60 and (kept[1] is None or _inside(root, kept[1]) is not None):
        return kept[1]
    found = _find_icon(Path(root))
    if len(_icons) > 200:
        _icons.clear()
    _icons[str(root)] = (now, found)
    return found


def _find_icon(root):
    config = _json(root / "app.json")
    expo = config.get("expo") if isinstance(config, dict) and isinstance(config.get("expo"), dict) else config
    if isinstance(expo, dict):
        ios = expo.get("ios") if isinstance(expo.get("ios"), dict) else {}
        for value in (ios.get("icon"), expo.get("icon")):
            path = _inside(root, root / value) if isinstance(value, str) and value.lower().endswith(".png") else None
            if path is not None:
                return path
    for folder in _icon_sets(root):
        contents = _json(folder / "Contents.json") or {}
        best = None
        for image in contents.get("images") or ():
            if not isinstance(image, dict) or image.get("appearances") or image.get("idiom") in NOT_IPHONE:
                continue
            name, size, scale = image.get("filename"), str(image.get("size") or ""), str(image.get("scale") or "1x")
            match = re.fullmatch(r"(\d+(?:\.\d+)?)x\1", size)
            path = _inside(root, folder / name) if isinstance(name, str) and name.lower().endswith(".png") else None
            if match and path is not None:
                pixels = float(match.group(1)) * float(scale.rstrip("x") or 1)
                if best is None or pixels > best[0]:
                    best = (pixels, path)
        if best is not None:
            return best[1]
    return None


def icon_version(path):
    """A word that changes when the icon file does (its size and change time), for a cache-busting address."""
    try:
        status = Path(path).stat()
    except OSError:
        return None
    return f"{status.st_mtime_ns:x}{status.st_size:x}"


def _count(folder, suffixes):
    try:
        return sum(1 for path in Path(folder).iterdir() if path.is_file() and path.suffix.lower() in suffixes
                   and not path.name.startswith("."))
    except OSError:
        return 0


def checks(root, limit=None):
    """The check files in ``root``/.mobster/checks, relative to the project, sorted (testkit/discover's walk)."""
    from ..testkit.discover import check_files
    folder = Path(root) / ".mobster" / "checks"
    if not folder.is_dir():
        return []
    try:
        files = check_files(folder)
    except OSError:
        return []
    return [path.relative_to(root).as_posix() for path in files[:limit]]


def check_tags(root):
    """The tags the project's checks carry, sorted; [] when they can't be read."""
    from ..testkit.discover import DiscoveryError, discover
    try:
        entries = discover([], root)
    except (DiscoveryError, OSError):
        return []
    return sorted({tag for entry in entries for tag in entry.tags})


def simulators(root):
    """(the simulators .mobster/matrix.yaml lists, as "TYPE@RUNTIME", or [], and the file's problem or None)."""
    from ..testkit.targets import TargetError, load_matrix
    path = Path(root) / ".mobster" / "matrix.yaml"
    if not path.is_file():
        return [], None
    try:
        return [f"{t.device_type}@{t.runtime}" if t.runtime else t.device_type for t in load_matrix(path)], None
    except TargetError as error:
        return [], str(error).replace(str(path), ".mobster/matrix.yaml")


_ready = {"at": None, "value": False}


def git_ready(clock=time.monotonic):
    """Whether git can run without macOS offering to install the developer tools (the /usr/bin/git stub opens that
    window when they're missing): a git outside /usr/bin, or a developer folder xcode-select names. Asked again after
    a minute, so installing the tools needs no restart."""
    now = clock()
    if _ready["at"] is not None and now - _ready["at"] < 60:
        return _ready["value"]
    found = shutil.which("git")
    value = bool(found) and Path(found).resolve().parent != Path("/usr/bin")
    if found and not value:
        try:
            result = subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True, timeout=GIT_TIMEOUT,
                                    stdin=subprocess.DEVNULL)
            folder = result.stdout.decode("utf-8", errors="replace").strip() if result.returncode == 0 else ""
            value = bool(folder) and Path(folder, "usr", "bin", "git").is_file()
        except (OSError, subprocess.TimeoutExpired):
            value = False
    _ready.update(at=now, value=value)
    return value


def _git(argv, cwd):
    """(exit code, stdout) of a read-only git call in ``cwd``; never raises, never prompts, never locks."""
    if not git_ready():
        return 1, ""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(["git", "-c", "core.fsmonitor=false", *argv], cwd=str(cwd), capture_output=True,
                                timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


def repo_of(url):
    """"owner/name" for a GitHub remote URL, else None."""
    match = GITHUB.match(url or "")
    return f"{match.group(1)}/{match.group(2)}" if match else None


def clean_url(url):
    """A remote URL with any user name and password taken out (https://token@github.com/... is never shown)."""
    return re.sub(r"^(https?://)[^@/]+@", r"\1", url or "") or None


def git(root):
    """{branch, remote, repo} for a git working tree at ``root``, else None. ``branch`` is None when detached."""
    if not (Path(root) / ".git").exists():
        return None
    code, top = _git(["rev-parse", "--show-toplevel"], root)
    if code != 0:
        return None
    code, branch = _git(["symbolic-ref", "--short", "-q", "HEAD"], root)
    _, remote = _git(["config", "--get", "remote.origin.url"], root)
    remote = clean_url(remote.strip())
    return {"branch": (branch.strip() or None) if code == 0 else None, "remote": remote, "repo": repo_of(remote)}


def git_status(root):
    """{head, upstream, ahead, behind, changed} from `git status --porcelain=v2 --branch` (tracked files only), or
    {} when it can't be read in time."""
    code, out = _git(["status", "--porcelain=v2", "--branch", "--untracked-files=no"], root)
    if code != 0:
        return {}
    status = {"head": None, "upstream": None, "ahead": 0, "behind": 0, "changed": 0}
    for line in out.splitlines():
        if line.startswith("# branch.oid "):
            oid = line[13:].strip()
            status["head"] = oid[:12] if re.fullmatch(r"[0-9a-f]{40,64}", oid) else None
        elif line.startswith("# branch.upstream "):
            status["upstream"] = line[18:].strip() or None
        elif line.startswith("# branch.ab "):
            match = re.fullmatch(r"\+(\d+) -(\d+)", line[12:].strip())
            if match:
                status["ahead"], status["behind"] = int(match.group(1)), int(match.group(2))
        elif line and not line.startswith("#"):
            status["changed"] += 1
    return status


def summary(path, home=None):
    """What the Projects list shows for the folder at ``path``."""
    path = Path(path)
    current, problem = state(path, home)
    out = {"state": current, "problem": problem, "mobster": False, "apps": [], "icon": None, "checks": 0,
           "workflows": 0, "simulators": [], "matrixProblem": None, "git": None}
    if current != "ready":
        return out
    out["mobster"] = (path / ".mobster").is_dir()
    out["apps"] = apps(path)
    found = icon(path)
    out["icon"] = icon_version(found) if found else None
    if out["mobster"]:
        out["checks"] = len(checks(path))
        out["workflows"] = _count(path / ".mobster" / "workflows", WORKFLOW_SUFFIXES)
        out["simulators"], out["matrixProblem"] = simulators(path)
    out["git"] = git(path)
    return out


def detail(path, home=None):
    """``summary`` with the checks by file and their tags and, in a git working tree, the commit, upstream and changed
    files."""
    out = summary(path, home)
    out["checkFiles"] = checks(path, MAX_LISTED_CHECKS) if out["mobster"] else []
    out["checkTags"] = check_tags(path) if out["checkFiles"] else []
    if out["git"] is not None:
        out["git"].update(git_status(path))
    return out
