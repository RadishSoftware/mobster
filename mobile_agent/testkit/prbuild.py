import json
import plistlib
import subprocess
from pathlib import Path

BUILD_DIR = "build"            # .mobster/build: the derived data folder
LOG_NAME = "pr-build.log"
BUILD_TIMEOUT = 45 * 60
LIST_TIMEOUT = 120
SDKS = {"sim": "iphonesimulator", "device": "iphoneos"}
DESTINATIONS = {"sim": "generic/platform=iOS Simulator", "device": "generic/platform=iOS"}


class BuildError(Exception):
    """The build couldn't be made: one plain sentence, and the fix."""

    def __init__(self, message, fix=""):
        super().__init__(message)
        self.message, self.fix = message, fix


def find_project(root):
    """The workspace or project to build: one at the project root, else one a folder below it."""
    root = Path(root)
    folders = [root]
    if root.is_dir():
        folders += sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
                          and p.suffix not in (".xcodeproj", ".xcworkspace"))
    for folder in folders:
        workspaces = [p for p in folder.glob("*.xcworkspace") if p.is_dir()]
        projects = [p for p in folder.glob("*.xcodeproj") if p.is_dir()]
        if len(workspaces) > 1 or (not workspaces and len(projects) > 1):
            names = ", ".join(p.name for p in (workspaces or projects))
            raise BuildError(f"{folder} has more than one Xcode project ({names}).",
                             "Pass the one to build with --project.")
        if workspaces:
            return "workspace", workspaces[0]
        if projects:
            return "project", projects[0]
    return None


def project_arg(path):
    """("workspace" | "project", path) for a path given with --project."""
    path = Path(path)
    if path.suffix == ".xcworkspace":
        return "workspace", path
    if path.suffix == ".xcodeproj":
        return "project", path
    raise BuildError(f"{path} isn't an .xcodeproj or an .xcworkspace.", "Pass --project YourApp.xcodeproj.")


def run_command(argv, cwd, log=None, timeout=BUILD_TIMEOUT):
    """(exit code, output); output goes to ``log`` when given."""
    try:
        if log is not None:
            Path(log).parent.mkdir(parents=True, exist_ok=True)
            with open(log, "wb") as handle:
                result = subprocess.run(argv, cwd=str(cwd), stdout=handle, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, timeout=timeout)
            return result.returncode, Path(log).read_text(encoding="utf-8", errors="replace")[-200_000:]
        result = subprocess.run(argv, cwd=str(cwd), capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout)
        return result.returncode, (result.stdout + result.stderr).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return 127, "xcodebuild isn't installed."
    except subprocess.TimeoutExpired:
        return 124, f"xcodebuild didn't finish in {timeout // 60} minutes."


def schemes(found, *, runner=None, cwd=None):
    kind, path = found
    code, output = (runner or run_command)(
        ["xcodebuild", "-list", "-json", f"-{kind}", str(path)], cwd or path.parent)
    # stderr follows the JSON (xcodebuild's warnings, CocoaPods' notes), so read one object.
    start = output.find("{")
    try:
        data = json.JSONDecoder().raw_decode(output[start:])[0] if start >= 0 else {}
    except ValueError:
        data = {}
    block = data.get(kind) or data.get("project") or data.get("workspace") or {}
    return [name for name in block.get("schemes") or () if isinstance(name, str)]


def pick_scheme(found, *, runner=None, cwd=None):
    """The only scheme, or the one named like the project; else BuildError (pass --scheme)."""
    names = schemes(found, runner=runner, cwd=cwd)
    stem = Path(found[1]).stem
    if len(names) == 1:
        return names[0]
    if stem in names:
        return stem
    listed = ", ".join(names[:8]) or "none"
    raise BuildError(f"Mobster can't tell which scheme to build (schemes: {listed}).", "Pass --scheme NAME.")


def build(root, *, scheme, project, platform="sim", configuration="Debug", runner=None, progress=None,
          bundle=None, for_testing=False, test_plan=None):
    """Build ``scheme`` for ``platform`` ("sim" or "device") into .mobster/build. With ``for_testing`` the same build
    also makes the test bundles and the .xctestrun file that ``xctest.run`` runs (build-for-testing)."""
    root = Path(root)
    kind, path = project
    derived = root / ".mobster" / BUILD_DIR
    log = derived / LOG_NAME
    argv = ["xcodebuild", f"-{kind}", str(path), "-scheme", scheme, "-configuration", configuration,
            "-destination", DESTINATIONS[platform], "-derivedDataPath", str(derived)]
    if platform == "sim":
        argv.append("CODE_SIGNING_ALLOWED=NO")
    else:
        argv.append("-allowProvisioningUpdates")
    if for_testing and test_plan:
        argv += ["-testPlan", test_plan]
    argv.append("build-for-testing" if for_testing else "build")
    (progress or (lambda line: None))(f"Building {scheme} for the {'simulator' if platform == 'sim' else 'iPhone'}")
    code, output = (runner or run_command)(argv, root, log)
    if code != 0:
        errors = [line.strip() for line in output.splitlines() if "error:" in line][:3]
        detail = ("; ".join(errors))[:600] if errors else f"xcodebuild exited {code}"
        fix = "Fix the build, or pass --no-build to test the builds the checks already name."
        if platform == "device" and "signing" in output.lower():
            fix = "Open the project in Xcode and pick a team under Signing & Capabilities, then run it again."
        raise BuildError(f"The build failed: {detail}.", fix)
    built = {**product(derived, configuration, platform, scheme=scheme, bundle=bundle), "log": str(log)}
    from ..memory import writer
    writer.from_build(root, scheme, built.get("bundle"))  # the project's scheme and bundle become memory
    return built


def product(derived, configuration, platform, *, scheme=None, bundle=None):
    """The .app the build made: the one with the checks' bundle ID, else the scheme's name, else the only one."""
    folder = Path(derived) / "Build" / "Products" / f"{configuration}-{SDKS[platform]}"
    apps = sorted(p for p in folder.glob("*.app") if p.is_dir())
    found = [(app, _bundle(app)) for app in apps]
    choice = next((app for app, bid in found if bundle and bid == bundle), None) or \
        next((app for app, _ in found if scheme and app.stem == scheme), None) or \
        (apps[0] if len(apps) == 1 else None)
    if choice is None:
        raise BuildError(f"The build finished, but Mobster couldn't tell which app in {folder} to test.",
                         "Pass --scheme with the app's scheme.")
    return {"bundle": _bundle(choice), "path": str(choice), "platform": platform}


def _bundle(app):
    try:
        with open(Path(app) / "Info.plist", "rb") as handle:
            value = plistlib.load(handle).get("CFBundleIdentifier")
        return value if isinstance(value, str) else None
    except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
        return None
