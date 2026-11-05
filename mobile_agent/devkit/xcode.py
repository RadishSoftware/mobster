"""Xcode: your checks run by themselves after each simulator build, from a post-action in the scheme."""

import json
import os
import shlex
import signal
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from . import DevError, agent_argv

TITLE = "Mobster checks"
ACTION_TYPE = "Xcode.IDEStandardExecutionActionsCore.ExecutionActionType.ShellScriptAction"
STATE_DIR = ".mobster/xcode"


def scheme_file(project, scheme):
    """The shared scheme's file in an .xcodeproj or .xcworkspace, else DevError."""
    project = Path(project)
    for candidate in (project / "xcshareddata" / "xcschemes" / f"{scheme}.xcscheme",):
        if candidate.is_file():
            return candidate
    if project.suffix == ".xcworkspace":
        for sibling in project.parent.glob("*.xcodeproj"):
            candidate = sibling / "xcshareddata" / "xcschemes" / f"{scheme}.xcscheme"
            if candidate.is_file():
                return candidate
    raise DevError(f"{project.name} has no shared scheme named {scheme}.",
                   "In Xcode, open Product › Scheme › Manage Schemes and tick Shared for it, then try again.")


def command_line(*, sims=(), tags=(), agent=None):
    """The post-action's script: Mobster for Mac's agent when it's installed, else this Mobster."""
    if agent is None:
        argv = agent_argv()[0]
    else:
        argv = [agent]
    argv = list(argv) + ["xcode", "after-build", "--background"]
    for sim in sims:
        argv += ["--sim", sim]
    for tag in tags:
        argv += ["--tag", tag]
    return " ".join(shlex.quote(str(item)) for item in argv) + " > /dev/null 2>&1"


def _buildable(root):
    """The scheme's app target, for EnvironmentBuildable (so the script sees BUILT_PRODUCTS_DIR and the rest)."""
    for entry in root.iter("BuildActionEntry"):
        reference = entry.find("BuildableReference")
        if reference is not None and str(reference.get("BuildableName", "")).endswith(".app"):
            return reference
    reference = root.find(".//BuildableReference")
    if reference is None:
        raise DevError("The scheme builds nothing Mobster can find.", "Pick the scheme that builds your app.")
    return reference


def _ours(action):
    content = action.find("ActionContent")
    return content is not None and content.get("title") == TITLE


def hook(project, scheme, *, sims=(), tags=(), agent=None):
    """Add (or replace) the post-action. Returns the scheme file."""
    path = scheme_file(project, scheme)
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        raise DevError(f"{path.name} isn't a scheme Mobster can read.") from None
    root = tree.getroot()
    build = root.find("BuildAction")
    if build is None:
        raise DevError(f"{path.name} has no build action.")
    reference = _buildable(root)
    post = build.find("PostActions")
    if post is None:
        post = ET.Element("PostActions")
        children = list(build)
        at = next((i for i, child in enumerate(children) if child.tag == "BuildActionEntries"), len(children))
        build.insert(at, post)
    for action in [a for a in post.findall("ExecutionAction") if _ours(a)]:
        post.remove(action)
    action = ET.SubElement(post, "ExecutionAction", {"ActionType": ACTION_TYPE})
    content = ET.SubElement(action, "ActionContent", {"title": TITLE, "scriptText": command_line(
        sims=sims, tags=tags, agent=agent), "shellToInvoke": "/bin/sh"})
    environment = ET.SubElement(content, "EnvironmentBuildable")
    environment.append(ET.fromstring(ET.tostring(reference)))
    _write(tree, path)
    return path


def unhook(project, scheme):
    """Take Mobster's post-action out. Returns True when there was one."""
    path = scheme_file(project, scheme)
    tree = ET.parse(path)
    build = tree.getroot().find("BuildAction")
    post = build.find("PostActions") if build is not None else None
    if post is None:
        return False
    ours = [a for a in post.findall("ExecutionAction") if _ours(a)]
    for action in ours:
        post.remove(action)
    if not list(post):
        build.remove(post)
    if ours:
        _write(tree, path)
    return bool(ours)


def _attribute(value):
    return (str(value).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\n", "&#10;"))


# Written the way Xcode writes a scheme, so a scheme Mobster rewrites diffs only where its post-action is.
def xcode_xml(element, depth=0):
    pad = "   " * depth
    lines = [f"{pad}<{element.tag}"]
    items = list(element.attrib.items())
    if items:
        lines += [f"{pad}   {name} = \"{_attribute(value)}\"" for name, value in items]
        lines[-1] += ">"
    else:
        lines[-1] += ">"
    for child in element:
        lines.append(xcode_xml(child, depth + 1))
    lines.append(f"{pad}</{element.tag}>")
    return "\n".join(lines)


def _write(tree, path):
    text = '<?xml version="1.0" encoding="UTF-8"?>\n' + xcode_xml(tree.getroot()) + "\n"
    temporary = Path(path).with_suffix(".xcscheme.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


# -- after a build ------------------------------------------------------------------------------------------------

def built_app(environ):
    """(the .app Xcode built, its platform) from the post-action's environment, or DevError."""
    folder, name = environ.get("BUILT_PRODUCTS_DIR"), environ.get("FULL_PRODUCT_NAME") or environ.get("WRAPPER_NAME")
    if not folder or not name:
        raise DevError("This runs from Xcode's post-action, which sets BUILT_PRODUCTS_DIR and FULL_PRODUCT_NAME.",
                       "Add it with `mobster xcode hook --scheme NAME`.")
    return Path(folder) / name, environ.get("PLATFORM_NAME") or ""


KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "BUILT_PRODUCTS_DIR", "FULL_PRODUCT_NAME",
        "WRAPPER_NAME", "PLATFORM_NAME", "SRCROOT", "PYTHONPATH")


# Xcode's build settings (SDKROOT, ARCHS and hundreds more) would leak into the builds the checks start.
def clean_env(environ):
    return {key: value for key, value in environ.items()
            if key in KEEP or key.startswith(("LC_", "MOBSTER_", "ANTHROPIC_", "OPENAI_"))}


def spawn_background(root, argv, env, *, popen=None, kill=os.kill):
    folder = Path(root) / STATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    pidfile = folder / "run.pid"
    try:
        kill(-int(pidfile.read_text().strip()), signal.SIGTERM)
    except (OSError, ValueError):
        pass
    with open(folder / "last.log", "wb") as log:
        process = (popen or subprocess.Popen)(argv, cwd=str(root), stdin=subprocess.DEVNULL, stdout=log,
                                              stderr=subprocess.STDOUT, start_new_session=True,
                                              env=clean_env(env))
    pidfile.write_text(f"{process.pid}\n")
    return process.pid


def checks_for(entries, app_path, bundle):
    """The checks of the app Xcode built, pointed at that build."""
    from ..testkit.prrun import with_build
    mine = [entry for entry in entries if entry.check is not None and (
        (entry.check.bundle_id and entry.check.bundle_id == bundle) or
        (not entry.check.bundle_id and entry.check.app_path and Path(entry.check.app_path).name == app_path.name))]
    return with_build(mine, {"path": str(app_path), "bundle": bundle}, ())


def bundle_of(app_path):
    import plistlib
    try:
        with open(Path(app_path) / "Info.plist", "rb") as handle:
            return plistlib.load(handle).get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def notify(title, text, *, run=None):
    """A macOS notification (no window). Never raises."""
    script = f"display notification {json.dumps(text)} with title {json.dumps(title)}"
    try:
        (run or (lambda argv: subprocess.run(argv, capture_output=True, timeout=10)))(["osascript", "-e", script])
    except Exception:
        pass


def run_checks(root, app_path, *, sims=(), tags=(), plan=None, execute=None, progress=None, notifier=None):
    """Run the checks of ``app_path`` and say the verdict. Returns (exit code, headline)."""
    from dataclasses import replace

    from ..testkit.api import Options, UsageError
    from ..testkit.reports import counts_text
    if plan is None:
        from ..testkit.api import plan
    if execute is None:
        from ..testkit.api import execute
    progress = progress or (lambda line: None)
    bundle = bundle_of(app_path)
    options = Options(sims=tuple(sims), tags=tuple(tags), command="mobster xcode after-build")
    try:
        planned = plan(options, cwd=root)
    except UsageError as error:
        raise DevError(str(error)) from None
    entries = checks_for(planned.entries, app_path, bundle)
    folder = Path(root) / STATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(app_path).stem
    if not entries:
        headline = f"No check covers {bundle or name}"
        (folder / "last.json").write_text(json.dumps({"app": str(app_path), "headline": headline, "exitCode": 0}))
        return 0, headline
    progress(f"{len(entries)} check{'' if len(entries) == 1 else 's'} of {name}, on the build Xcode just made")
    doc, paths = execute(replace(planned, entries=entries), progress=progress)
    headline = counts_text(doc.get("summary") or {})
    code = int(doc.get("exitCode", 3))
    (folder / "last.json").write_text(json.dumps({"app": str(app_path), "headline": headline, "exitCode": code,
                                                  "report": str(paths.get("html") or "")}, indent=1))
    (notifier or notify)(f"Mobster checks: {name}", headline + ("" if code == 0 else
                                                                ". The report is in .mobster/test-results."))
    return code, headline
