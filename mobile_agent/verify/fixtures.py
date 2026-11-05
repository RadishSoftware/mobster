"""Fixtures (.mobster/fixtures/<name>.yaml): what changes a device's state is for simulators, refused on a phone."""

import os
import re
import shutil
import subprocess
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import dialogs as dialog_policy
from .assertions import CheckError

FOLDER = "fixtures"
SUFFIXES = (".yaml", ".yml")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")
KEYS = ("extends", "reset", "launch", "files", "keychain", "pasteboard", "steps", "dialogs")
DEPTH = 6
FILES_LIMIT = 50
BYTES_LIMIT = 20 * 1024 * 1024
PASTEBOARD_LIMIT = 10_000
PHONE_REFUSAL = "{what} changes the state of a phone, and fixtures do that on simulators only (from {name})."
PHONE_FIX = "Run the check on a simulator, or use a fixture with only launch args, env and steps on the phone."


class FixtureError(Exception):
    """A fixture can't be read, found or applied: one sentence that says what to do."""


@dataclass(frozen=True)
class Effective:
    """A fixture and what it extends, flattened."""
    names: tuple = ()
    reset: str = "none"
    args: tuple = ()
    env: dict = field(default_factory=dict)
    steps: tuple = ()
    files: tuple = ()            # of (absolute source, destination relative to the container)
    keychain: bool = False
    pasteboard: str = ""
    dialogs: dict = field(default_factory=dict)   # how a run answers iOS's own dialogs (dialogs.py)

    @property
    def changes_state(self):
        """What a real iPhone must not get: a reset, files, a keychain reset or a clipboard."""
        return [word for word, held in (("a reset", self.reset != "none"), ("seed files", bool(self.files)),
                                        ("a keychain reset", self.keychain), ("a pasteboard", bool(self.pasteboard)))
                if held]

    def public(self):
        return {"names": list(self.names), "reset": self.reset, "args": list(self.args), "env": dict(self.env),
                "steps": list(self.steps), "files": [{"from": str(src), "to": dest} for src, dest in self.files],
                "keychain": self.keychain, "pasteboard": bool(self.pasteboard), "dialogs": dict(self.dialogs),
                "destructive": bool(self.changes_state)}


def folder(root):
    return Path(root) / ".mobster" / FOLDER


def path_of(root, name):
    for suffix in SUFFIXES:
        path = folder(root) / f"{name}{suffix}"
        if path.is_file():
            return path
    return None


def names(root):
    """Every fixture's name in the project, sorted."""
    base = folder(root)
    if not base.is_dir():
        return []
    return sorted({path.stem for path in base.iterdir() if path.suffix in SUFFIXES and NAME.fullmatch(path.stem)})


def _load(root, name):
    """One fixture file as a dict of validated parts (not yet flattened)."""
    from . import checks
    path = path_of(root, name)
    if path is None:
        have = ", ".join(names(root)[:8])
        raise FixtureError(f"There's no fixture called {name}." + (f" You have: {have}." if have else
                           " Add .mobster/fixtures/NAME.yaml."))
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise FixtureError(f"{path.name} can't be read. Check that it is a text file.") from None
    except yaml.YAMLError:
        raise FixtureError(f"{path.name} isn't valid YAML.") from None
    if data is None:
        data = {}
    if not isinstance(data, dict) or set(data) - set(KEYS):
        bad = sorted(set(data) - set(KEYS))[0] if isinstance(data, dict) else None
        raise FixtureError(f"{path.name}: " + (f"`{str(bad)[:40]}` isn't a fixture key. Use {', '.join(KEYS)}."
                                              if bad is not None else "a fixture is a mapping of keys."))
    where = path.name
    try:
        extends = data.get("extends") or []
        extends = [extends] if isinstance(extends, str) else extends
        if not isinstance(extends, list) or not all(isinstance(item, str) and NAME.fullmatch(item) for item in extends):
            raise CheckError("extends is a fixture name or a list of them")
        reset = data.get("reset")
        if reset is not None and reset not in checks.RESETS:
            raise CheckError(f"reset is one of {', '.join(checks.RESETS)}, not {reset!r}")
        launch = data.get("launch") or {}
        if not isinstance(launch, dict) or set(launch) - {"args", "env"}:
            raise CheckError("launch holds args and env")
        args = checks._strings(launch.get("args"), "launch.args", checks.LIMITS["args"])
        env = checks._env(launch.get("env"), "launch.env")
        steps = checks._steps(data.get("steps"))
        keychain = data.get("keychain")
        if keychain not in (None, "reset", False):
            raise CheckError("keychain is reset")
        pasteboard = data.get("pasteboard") or ""
        if not isinstance(pasteboard, str) or len(pasteboard) > PASTEBOARD_LIMIT:
            raise CheckError(f"pasteboard is text of at most {PASTEBOARD_LIMIT:,} characters")
        files = _files(data.get("files"), folder(root))
        answers = dialog_policy.parse(data.get("dialogs"))
    except CheckError as error:
        raise FixtureError(f"{where}: {error}") from None
    return {"extends": extends, "reset": reset, "args": args, "env": env, "steps": steps, "files": files,
            "keychain": keychain == "reset", "pasteboard": pasteboard, "dialogs": answers}


def _files(value, base):
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > FILES_LIMIT:
        raise CheckError(f"files is a list of at most {FILES_LIMIT} entries with from and to")
    out = []
    for index, item in enumerate(value, 1):
        if (not isinstance(item, dict) or set(item) != {"from", "to"}
                or not all(isinstance(v, str) for v in item.values())):
            raise CheckError(f"files[{index}] needs from and to, both text")
        source = (Path(base) / item["from"])
        real = Path(os.path.realpath(source))
        if Path(item["from"]).is_absolute() or ".." in Path(item["from"]).parts or not real.exists() or \
                not (real == Path(os.path.realpath(base)) or Path(os.path.realpath(base)) in real.parents):
            raise CheckError(f"files[{index}].from {item['from']!r} must be a file or folder inside .mobster/{FOLDER}/")
        dest = Path(item["to"])
        if not item["to"].strip() or dest.is_absolute() or ".." in dest.parts or str(dest) in (".", ""):
            raise CheckError(f"files[{index}].to {item['to']!r} must be a path inside the app's container, such as "
                             "Documents/cart.json")
        out.append((real, dest.as_posix()))
    return tuple(out)


def resolve(root, wanted):
    """The flattened Effective of the fixture names in ``wanted``, in order."""
    state = {"names": [], "reset": "none", "args": [], "env": {}, "steps": [], "files": {}, "keychain": False,
             "pasteboard": "", "dialogs": {}}
    done = []

    def add(name, chain):
        if name in chain:
            raise FixtureError(f"Fixtures extend each other in a circle: {' → '.join([*chain, name])}.")
        if len(chain) >= DEPTH:
            raise FixtureError(f"Fixtures extend more than {DEPTH} deep, ending at {name}.")
        if name in done:
            return
        part = _load(root, name)
        for parent in part["extends"]:
            add(parent, (*chain, name))
        done.append(name)
        state["names"].append(name)
        if part["reset"] is not None:
            state["reset"] = part["reset"]
        state["args"] += part["args"]
        state["env"].update(part["env"])
        state["steps"] += part["steps"]
        for source, dest in part["files"]:
            state["files"][dest] = source
        state["keychain"] = state["keychain"] or part["keychain"]
        state["pasteboard"] = part["pasteboard"] or state["pasteboard"]
        state["dialogs"] = dialog_policy.merge(state["dialogs"], part["dialogs"])

    for name in wanted:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise FixtureError(
                "A fixture name is 1 to 60 lowercase letters, digits or hyphens, starting with a letter or digit.")
        add(name, ())
    return Effective(names=tuple(state["names"]), reset=state["reset"], args=tuple(state["args"]),
                     env=dict(state["env"]), steps=tuple(state["steps"]),
                     files=tuple((source, dest) for dest, source in state["files"].items()),
                     keychain=state["keychain"], pasteboard=state["pasteboard"], dialogs=dict(state["dialogs"]))


def with_fixtures(check, root):
    if not check.fixture or check.fixture_origin is not None:
        return check
    effective = resolve(root, check.fixture)
    return replace(check, fixture_origin=check,
                   reset=effective.reset if check.reset == "auto" and effective.reset != "none" else check.reset,
                   launch_args=effective.args + check.launch_args,
                   launch_env={**effective.env, **check.launch_env}, steps=effective.steps + check.steps,
                   dialogs=dialog_policy.merge(effective.dialogs, check.dialogs),
                   seed=Seed(effective.files, effective.keychain, effective.pasteboard, tuple(effective.changes_state)))


def for_run(check, runs_dir):
    """The check with its fixtures flattened in. The project is the folder that holds ``.mobster/`` beside the runs,
    else the folder the check was read from; a check that names a fixture with no project to take it from is refused
    before any simulator is touched."""
    if not check.fixture or check.fixture_origin is not None:
        return check
    from .checks import project_root
    runs_dir = Path(runs_dir)
    root = runs_dir.parent.parent if runs_dir.parent.name == ".mobster" else None
    if root is None and check.source:
        root = project_root(check.source)
    if root is None:
        raise FixtureError(f"This check uses the fixture {', '.join(check.fixture)}, which comes from a project's "
                           ".mobster/fixtures/. Run it from a project folder that has .mobster/, with --out left at "
                           "its default.")
    return with_fixtures(check, root)


@dataclass(frozen=True)
class Seed:
    files: tuple = ()
    keychain: bool = False
    pasteboard: str = ""
    risky: tuple = ()            # what the fixtures change that a real iPhone must not get (Effective.changes_state)

    def __bool__(self):
        return bool(self.files or self.keychain or self.pasteboard)


def refuse_on_phone(check):
    """FixtureError when the check's fixtures would change a real iPhone's state."""
    if check.seed.risky:
        raise FixtureError(PHONE_REFUSAL.format(what=check.seed.risky[0], name=", ".join(check.fixture))
                           + " " + PHONE_FIX)


def seed_simulator(seed, *, udid, bundle, run, pbcopy=None, actions=None):
    """Put the seed on simulator ``udid``: the keychain emptied, the files copied into ``bundle``'s data container and
    the pasteboard set."""
    did = []
    if seed.keychain:
        with actions.action("FIXTURE_KEYCHAIN") if actions is not None else nullcontext():
            code, out = run(["keychain", udid, "reset"])
            if code != 0:
                raise FixtureError(f"The simulator's keychain wouldn't reset ({out[:120] or code}).")
        did.append("keychain reset")
    if seed.files:
        code, container = run(["get_app_container", udid, bundle, "data"])
        if code != 0 or not container.strip() or container.strip() == "(null)":
            raise FixtureError(f"{bundle} isn't installed on the simulator, so there is no container to put files in.")
        count = _copy(seed.files, container.strip(), udid, actions=actions)
        did.append(f"{count} file{'' if count == 1 else 's'} copied")
    if seed.pasteboard:
        with actions.action("FIXTURE_PASTEBOARD") if actions is not None else nullcontext():
            (pbcopy or _pbcopy)(udid, seed.pasteboard)
        did.append("pasteboard set")
    return did


def _pbcopy(udid, text):
    try:
        done = subprocess.run(["xcrun", "simctl", "pbcopy", udid], input=text, text=True, capture_output=True,
                              timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise FixtureError("The simulator's pasteboard wouldn't set.") from None
    if done.returncode != 0:
        raise FixtureError("The simulator's pasteboard wouldn't set.")


def _copy(files, container, udid, *, actions=None):
    from ..sim import apps
    from ..sim.api import SimError
    try:
        root = apps.guard_container(container, udid)
    except SimError as error:
        raise FixtureError(str(error)) from None
    total = count = 0
    plan = []
    for source, dest in files:
        target = (root / dest)
        for path in ([source] if source.is_file() else sorted(p for p in source.rglob("*"))):
            if path.is_symlink():
                raise FixtureError(f"{path.name} is a symbolic link, so Mobster didn't copy it.")
            if path.is_file():
                total += path.stat().st_size
                count += 1
                plan.append((path, target if source.is_file() else target / path.relative_to(source)))
    if count > FILES_LIMIT * 4 or total > BYTES_LIMIT:
        raise FixtureError(f"The seed files are {count} files and {total // 1024} KB; the limit is "
                           f"{FILES_LIMIT * 4} files and {BYTES_LIMIT // 1024 // 1024} MB.")
    for _, target in plan:
        for parent in [target, *target.parents]:
            if parent == root:
                break
            if parent.is_symlink():
                raise FixtureError(f"{target.relative_to(root)} goes through a symbolic link in the container, so "
                                   "Mobster didn't copy it.")
        if root not in Path(os.path.realpath(target)).parents:
            raise FixtureError(f"{target.relative_to(root)} leaves the app's container, so Mobster didn't copy it.")
    for path, target in plan:
        with actions.action("FIXTURE_FILE") if actions is not None else nullcontext():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    return count
