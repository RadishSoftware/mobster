"""A check (docs/checks.md). Every unknown key is an error, so a typo never silently weakens a check."""

import difflib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import links
from ..state import validate_bundle_id
from . import conditions, dialogs, netstubs, perf, wire
from .assertions import CheckError, expand_assertion, reject_unknown_keys
from .snapshots import Snapshot, parse_snapshot
from .system_surfaces import spec as surface_spec

__all__ = ["CheckError", "Check", "check_from_dict", "load_check", "dump_check", "project_root",
           "RESETS", "LIMITS"]

RESETS = ("none", "data", "reinstall")
LIMITS = {"name": 120, "steps": 20, "step": 500, "expect": 50, "args": 20, "env": 20, "url": 2000,
          "max_usd": 1.00, "max_seconds": 600, "tags": 10}
DEFAULT_MAX_SECONDS = 180
DEFAULT_MAX_USD = 0.25
FILE_LIMIT = 1_000_000
MAX_NESTING = 100   # lists and objects in a check file; one nests a few deep
ENV_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
TAG = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
LAUNCH_ONLY_NAME = "Launch check"

FILE_KEYS = ("version", "name", "app", "device", "runtime", "reset", "tags", "launch", "steps", "expect",
             "throughout", "somewhere", "audit", "a11y", "fixture", "business", "conditions", "dialogs", "stubs",
             "budget", "schedule")
TOOL_KEYS = ("name", "app_path", "bundle_id", "device", "runtime", "reset", "launch_args", "launch_env", "open_url",
             "steps", "expect", "max_usd", "max_seconds")
TOOL_ONLY = ("image",)
FILE_SHAPE_KEYS = ("version", "app", "launch", "budget")
TOOL_SHAPE_KEYS = ("app_path", "bundle_id", "launch_args", "launch_env", "open_url", "max_usd", "max_seconds")


@dataclass(frozen=True)
class Check:
    name: str
    steps: tuple                 # of str
    expect: tuple                # of assertions.Assertion
    app_path: Optional[str]      # absolute
    bundle_id: Optional[str]
    device: Optional[str]
    runtime: Optional[str]
    reset: str                   # "auto" | "none" | "data" | "reinstall"
    launch_args: tuple
    launch_env: dict = field(default_factory=dict)
    open_url: Optional[str] = None
    max_seconds: float = DEFAULT_MAX_SECONDS
    max_usd: float = DEFAULT_MAX_USD
    source: Optional[str] = None  # the file it came from
    tags: tuple = ()              # of str: `mobster test --tag` picks checks by them
    # "every 1h" or a five-field cron: Mobster runs this check by itself (testkit/health.py)
    schedule: Optional[str] = None
    throughout: tuple = ()        # of assertions.Assertion (no_text, absent): must hold on every screen the run reads
    somewhere: tuple = ()         # of assertions.Assertion: each must hold on at least one screen the run reads
    audit: tuple = ()             # of str: static audits of the built app (audit.AUDITS)
    fixture: tuple = ()           # of str: names in .mobster/fixtures/ the check starts from (fixtures.py)
    business: object = None       # business.BusinessSpec: locally registered app-state fixture and comparisons
    seed: object = field(default_factory=lambda: _no_seed())   # fixtures.Seed, once the fixtures are flattened in
    fixture_origin: object = None  # the check as written, once the fixtures are flattened into it
    sound: tuple = ()             # of str: sounds (.mobster/sounds/) the iPhone must play during the run (sound.py)
    a11y: Optional[str] = None    # "warn" | "strict": the accessibility audit (a11y.py); `audit: a11y` is strict
    a11y_scope: str = ""          # the check's file as results.json names it: a baseline entry's first part
    a11y_baseline: frozenset = frozenset()   # baseline keys (testkit/a11y_baseline.py) whose findings never fail
    a11y_refresh: bool = False    # internal --update-a11y-baseline run; never part of a saved check
    draft_trial: bool = False     # internal `mobster test minimize` trial; never part of a saved check
    conditions: dict = field(default_factory=dict)   # conditions.parse's block: the device's state for the run
    dialogs: dict = field(default_factory=dict)      # dialogs.parse's block: how the run answers iOS's own dialogs
    perf_budget: dict = field(default_factory=dict)   # budget.launch_ms / memory_mb / hangs (perf.judge)
    surfaces: tuple = ()          # of system_surfaces.Surface: widgets, Live Activities and the like (experimental)
    stubs: tuple = ()             # of dict: netstubs.parse's entries, answered by the run's proxy (simulator only)
    wire: tuple = ()              # of wire.WireAssertion: expect request, no_request and storage (dark, simulator only)
    l10n: bool = False            # `mobster test --locale`: audit the screens for truncation, untranslated text, RTL
    # the `snapshot:` in expect: the final screen against its baseline (snapshots.py)
    snapshot: Optional[Snapshot] = None
    snapshot_update: bool = False  # `mobster test --update-snapshots`: write the baseline from this run
    snapshot_record: bool = True   # False in an approved CI suite: a missing baseline fails, and none is recorded

    def reset_level(self):
        """The reset this run uses: the check's own, else reinstall with a .app and data without. A system app
        (com.apple.*) is never reset."""
        if (self.bundle_id or "").startswith("com.apple."):
            return "none"
        if self.reset != "auto":
            return self.reset
        return "reinstall" if self.app_path else "data"


def _text(value, where, limit, *, required=False):
    if value is None:
        if required:
            raise CheckError(f"{where} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise CheckError(f"{where} must be text; quote it if it looks like a number or yes/no")
    value = value.strip()
    if not value:
        if required:
            raise CheckError(f"{where} is empty")
        return None
    if len(value) > limit:
        raise CheckError(f"{where} is longer than {limit:,} characters")
    return value


def _number(value, where, low, high, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CheckError(f"{where} must be a number")
    if not low < value <= high:
        raise CheckError(f"{where} must be more than {low:g} and at most {high:g}")
    return float(value)


def _strings(value, where, limit, item_limit=1000):
    if value is None:
        return ()
    if not isinstance(value, list):
        raise CheckError(f"{where} must be a list")
    if len(value) > limit:
        raise CheckError(f"{where} has more than {limit} items")
    out = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, str):
            raise CheckError(f"{where}[{index}] must be text; quote it, as in \"YES\" or \"3\"")
        if len(item) > item_limit:
            raise CheckError(f"{where}[{index}] is longer than {item_limit:,} characters")
        out.append(item)
    return tuple(out)


def _stubs(data, base_dir):
    stubs = netstubs.parse(data.get("stubs"), base_dir)
    if stubs and (data.get("conditions") or {}).get("network") == "offline":
        raise CheckError("stubs answer requests, and conditions.network offline refuses them all. Use slow-3g, lte "
                         "or flaky with stubs, or remove the stubs.")
    return stubs


def _env(value, where):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CheckError(f"{where} must be an object of KEY: value")
    if len(value) > LIMITS["env"]:
        raise CheckError(f"{where} has more than {LIMITS['env']} entries")
    out = {}
    for key, item in value.items():
        if not isinstance(key, str) or not ENV_KEY.fullmatch(key):
            raise CheckError(f"{where}: {key!r} is not a variable name (A-Z, 0-9 and _, not starting with a digit)")
        if isinstance(item, bool) or not isinstance(item, str):
            raise CheckError(f"{where}.{key} must be text; quote it, as in \"3\" or \"YES\"")
        if len(item) > 1000:
            raise CheckError(f"{where}.{key} is longer than 1,000 characters")
        out[key] = item
    return out


def _perf_budget(budget):
    """``budget.launch_ms``, ``memory_mb`` and ``hangs``: the limits a run's measurements must stay inside."""
    out = {}
    for key, high in (("launch_ms", 600_000), ("memory_mb", 65_536)):
        if budget.get(key) is not None:
            out[key] = _plain_number(_number(budget[key], f"budget.{key}", 0, high, None))
    hangs = budget.get("hangs")
    if hangs is not None:
        if isinstance(hangs, bool) or not isinstance(hangs, int) or not 0 <= hangs <= 1000:
            raise CheckError("budget.hangs must be a whole number from 0 to 1000: the hangs of 250 ms or longer "
                             "a run may have")
        out["hangs"] = hangs
    return out


def _steps(value):
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise CheckError("steps must be a list of plain-English steps")
    if len(value) > LIMITS["steps"]:
        raise CheckError(f"steps has more than {LIMITS['steps']} steps")
    return tuple(_text(step, f"steps[{index}]", LIMITS["step"], required=True) for index, step in enumerate(value))


def _expect_list(value, key, snapshots=None):
    """The assertions of a list; a ``snapshot`` entry goes to ``snapshots`` (only ``expect`` takes one)."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise CheckError(f"{key} must be a list of assertions, such as [{{text: Continue}}]")
    out = []
    for index, item in enumerate(value):
        if isinstance(item, dict) and "snapshot" in item:
            if snapshots is None:
                raise CheckError(f"{key}[{index}] is a snapshot, which goes in expect")
            if snapshots:
                raise CheckError(f"{key}[{index}] is a second snapshot. A check ends on one screen and has one "
                                 "snapshot; write another check for another screen.")
            snapshots.append(parse_snapshot(item, f"{key}[{index}]"))
            continue
        if key == "expect" and wire.is_wire(item):
            continue    # wire.parse reads these
        out.extend(expand_assertion(item, f"{key}[{index}]"))
    if len(out) > LIMITS["expect"]:
        raise CheckError(f"{key} has more than {LIMITS['expect']} assertions")
    return tuple(out)


def _expect(value):
    """(the assertions, the snapshot or None) of a check's ``expect``."""
    snapshots = []
    return _expect_list(value, "expect", snapshots), (snapshots[0] if snapshots else None)


def _split_expect(value):
    """(the tree assertions, the sound names) of ``expect``: ``{sound: sent}`` is heard (sound.py), not read."""
    from .sound import NAME
    if not isinstance(value, list):
        return value, ()
    tree, sounds = [], []
    for index, item in enumerate(value):
        if not (isinstance(item, dict) and "sound" in item):
            tree.append(item)
        elif set(item) != {"sound"} or not isinstance(item["sound"], str) or not NAME.fullmatch(item["sound"]):
            raise CheckError(f"expect[{index}] sound is a name such as {{sound: sent}}: lowercase letters, digits, "
                             "- and _")
        else:
            sounds.append(item["sound"])
    return tree, tuple(dict.fromkeys(sounds))


THROUGHOUT_KINDS = ("no_text", "absent")


def _throughout(value):
    """``throughout``: assertions that must hold on every screen the run reads, so only the ones about what must never
    show (no_text, absent)."""
    out = _expect_list(value, "throughout")
    for index, item in enumerate(out):
        if item.kind not in THROUGHOUT_KINDS:
            raise CheckError(f"throughout[{index}] is a {item.kind}; use no_text or absent, which can hold on "
                             "every screen")
    return out


def _audit(value):
    from .a11y import NAME as A11Y
    from .audit import AUDITS
    from .device_audit import NAME as AX_DEVICE
    names = (*AUDITS, A11Y, AX_DEVICE)
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise CheckError("audit must be a list of names, such as [usage-descriptions]")
    for index, name in enumerate(value):
        if name not in names:
            close = difflib.get_close_matches(str(name), list(names), n=1)
            raise CheckError(f"audit[{index}] {name!r} is not an audit; "
                             + (f"did you mean {close[0]}?" if close else f"use {', '.join(names)}"))
    return tuple(dict.fromkeys(value))


def _no_seed():
    from .fixtures import Seed
    return Seed()


def _fixture(value, base_dir):
    """A check file's ``fixture``: a name or a list of names. With a project folder, each must exist."""
    from . import fixtures
    if value is None:
        return ()
    value = [value] if isinstance(value, str) else value
    if not isinstance(value, list) or not value or len(value) > 10 or not all(
            isinstance(item, str) and fixtures.NAME.fullmatch(item) for item in value):
        raise CheckError("fixture is the name of a fixture in .mobster/fixtures/, or a list of names")
    if base_dir is not None and (Path(base_dir) / ".mobster").is_dir():
        for name in value:
            if fixtures.path_of(base_dir, name) is None:
                have = ", ".join(fixtures.names(base_dir)[:8])
                raise CheckError(f"fixture {name!r} isn't in .mobster/fixtures/"
                                 + (f"; you have {have}" if have else ""))
    return tuple(dict.fromkeys(value))


def _a11y(value):
    if value is None or value is False:
        return None
    if value is True:
        return "warn"
    if value not in ("warn", "strict"):
        raise CheckError("a11y is warn, strict or false: warn reports findings and passes, strict fails the check")
    return value


def _url(value, where):
    url = _text(value, where, LIMITS["url"])
    if url is not None and not URL_SCHEME.match(url):
        raise CheckError(f"{where} must be a link with a scheme, such as yourapp://lesson")
    return url


def _tags(value):
    """A check file's ``tags``: a list of short lowercase words (a-z, 0-9, - and _), or one such word."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise CheckError("tags must be a list of words, such as [smoke, paywall]")
    if len(value) > LIMITS["tags"]:
        raise CheckError(f"tags has more than {LIMITS['tags']} tags")
    out = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, str) or not TAG.fullmatch(item.strip()):
            raise CheckError(f"tags[{index}] must be a word of 1 to 40 of a-z, 0-9, - and _, such as smoke")
        if item.strip() not in out:
            out.append(item.strip())
    return tuple(out)


def _resolve(path, base_dir):
    path = Path(os.path.expanduser(path))
    if not path.is_absolute():
        path = Path(base_dir or os.getcwd()) / path
    return str(path.resolve())


def _business(value):
    from .business import parse_business
    return parse_business(value)


def check_from_dict(data, *, base_dir=None):
    """A Check from the file shape (§2.1) or the MCP tool arguments."""
    if not isinstance(data, dict):
        raise CheckError("a check must be an object (a YAML mapping)")
    surfaces = data.get("surfaces")
    data = {key: value for key, value in data.items() if key not in (*TOOL_ONLY, "surfaces")}
    file_only = [key for key in FILE_SHAPE_KEYS if key in data]
    tool_only = [key for key in TOOL_SHAPE_KEYS if key in data]
    if file_only and tool_only:
        raise CheckError(f"the check mixes the file format ({', '.join(file_only)}) and tool arguments "
                         f"({', '.join(tool_only)})")
    if "tags" in data and tool_only:
        raise CheckError("tags goes in a check file, not in tool arguments")
    if "schedule" in data and tool_only:
        raise CheckError("schedule goes in a check file, not in tool arguments")
    if surfaces is not None and tool_only:
        raise CheckError("surfaces goes in a check file, not in tool arguments")
    if not file_only and not tool_only:
        reject_unknown_keys(data, tuple(dict.fromkeys(FILE_KEYS + TOOL_KEYS)), "the check")
        raise CheckError("the check needs an app: app.bundle or app.path in a check file, bundle_id or app_path "
                         "in tool arguments")
    perf_limits = {}
    if tool_only:
        reject_unknown_keys(data, TOOL_KEYS, "the check")
        fields = {"app_path": data.get("app_path"), "bundle": data.get("bundle_id"),
                  "args": data.get("launch_args"), "env": data.get("launch_env"), "url": data.get("open_url"),
                  "max_usd": data.get("max_usd"), "max_seconds": data.get("max_seconds"),
                  "where": {"path": "app_path", "bundle": "bundle_id", "args": "launch_args",
                            "env": "launch_env", "url": "open_url", "max_usd": "max_usd",
                            "max_seconds": "max_seconds"}}
    else:
        reject_unknown_keys(data, FILE_KEYS, "the check")
        version = data.get("version", 1)
        if version != 1 or isinstance(version, bool):
            raise CheckError(f"version {version!r} is not supported; 1 is the only version")
        app = {} if data.get("app") is None else data["app"]
        if not isinstance(app, dict):
            raise CheckError("app must be an object with bundle and/or path")
        reject_unknown_keys(app, ("bundle", "path"), "app")
        launch = {} if data.get("launch") is None else data["launch"]
        if not isinstance(launch, dict):
            raise CheckError("launch must be an object with args, env and/or url")
        reject_unknown_keys(launch, ("args", "env", "url"), "launch")
        budget = {} if data.get("budget") is None else data["budget"]
        perf_limits = budget
        if not isinstance(budget, dict):
            raise CheckError("budget must be an object with max_seconds, max_usd, launch_ms, memory_mb and/or hangs")
        reject_unknown_keys(budget, ("max_seconds", "max_usd", *perf.BUDGET_KEYS), "budget")
        fields = {"app_path": app.get("path"), "bundle": app.get("bundle"), "args": launch.get("args"),
                  "env": launch.get("env"), "url": launch.get("url"), "max_usd": budget.get("max_usd"),
                  "max_seconds": budget.get("max_seconds"),
                  "where": {"path": "app.path", "bundle": "app.bundle", "args": "launch.args", "env": "launch.env",
                            "url": "launch.url", "max_usd": "budget.max_usd", "max_seconds": "budget.max_seconds"}}
    where = fields["where"]
    app_path = _text(fields["app_path"], where["path"], 4096)
    if app_path is not None:
        app_path = _resolve(app_path, base_dir)
    bundle = _text(fields["bundle"], where["bundle"], 255)
    if bundle is not None:
        try:
            bundle = validate_bundle_id(bundle)
        except ValueError:
            raise CheckError(f"{where['bundle']} {bundle!r} is not a bundle ID such as com.example.app") from None
    if app_path is None and bundle is None:
        raise CheckError(f"the check needs {where['bundle']} or {where['path']}: the app to check")
    reset = data.get("reset")
    if reset is not None and reset not in RESETS:
        raise CheckError(f"reset must be one of {', '.join(RESETS)}, not {reset!r}")
    steps = _steps(data.get("steps"))
    name = _text(data.get("name"), "name", LIMITS["name"])
    if name is None:
        name = (steps[0][:LIMITS["name"] - 1] + "…" if len(steps[0]) > LIMITS["name"] else steps[0]) if steps \
            else LAUNCH_ONLY_NAME
    tree_expect, sounds = _split_expect(data.get("expect"))
    expect, snapshot = _expect(tree_expect)
    return Check(
        name=name, steps=steps, expect=expect, snapshot=snapshot, sound=sounds, app_path=app_path, bundle_id=bundle,
        device=_text(data.get("device"), "device", 100), runtime=_text(data.get("runtime"), "runtime", 100),
        reset=reset or "auto", launch_args=_strings(fields["args"], where["args"], LIMITS["args"]),
        launch_env=_env(fields["env"], where["env"]), open_url=_url(fields["url"], where["url"]),
        max_seconds=_number(fields["max_seconds"], where["max_seconds"], 0, LIMITS["max_seconds"],
                            DEFAULT_MAX_SECONDS),
        max_usd=_number(fields["max_usd"], where["max_usd"], 0, LIMITS["max_usd"], DEFAULT_MAX_USD),
        tags=_tags(data.get("tags")), schedule=_text(data.get("schedule"), "schedule", 128),
        throughout=_throughout(data.get("throughout")),
        somewhere=_expect_list(data.get("somewhere"), "somewhere"),
        audit=_audit(data.get("audit")), a11y=_a11y(data.get("a11y")), fixture=_fixture(data.get("fixture"), base_dir),
        business=_business(data.get("business")),
        conditions=conditions.parse(data.get("conditions"), base_dir), dialogs=dialogs.parse(data.get("dialogs")),
        stubs=_stubs(data, base_dir), wire=wire.parse(data.get("expect")),
        surfaces=surface_spec.parse(surfaces, base_dir), perf_budget=_perf_budget(perf_limits))


def project_root(path=None):
    """The directory holding ``.mobster/``, walking up from ``path`` (a file or folder; default the working directory);
    else ``path``'s own folder."""
    path = Path(path or Path.cwd()).resolve()
    start = path if path.is_dir() else path.parent
    for folder in (start, *start.parents):
        if (folder / ".mobster").is_dir():
            return folder
    return start


def load_check(path):
    """A check file (.yaml, .yml or .json). Raises CheckError with the file and line on a problem."""
    path = Path(path).expanduser()
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise CheckError(f"{path} does not exist") from None
    except OSError as error:
        raise CheckError(f"{path} can't be read: {error.strerror or error}") from None
    if not path.is_file():
        raise CheckError(f"{path} is not a file")
    if size > FILE_LIMIT:
        raise CheckError(f"{path} is larger than 1 MB")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CheckError(f"{path} can't be read as UTF-8 text ({type(error).__name__})") from None
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise CheckError(f"{path} line {error.lineno}: {error.msg}") from None
        except (RecursionError, ValueError):
            raise CheckError(f"{path}: nested too deeply") from None
        if _nests_too_deep(data):
            raise CheckError(f"{path}: nested too deeply")
    else:
        import yaml
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as error:
            mark = getattr(error, "problem_mark", None)
            line = f" line {mark.line + 1}" if mark is not None else ""
            problem = getattr(error, "problem", None) or "not valid YAML"
            raise CheckError(f"{path}{line}: {problem}") from None
        except (RecursionError, ValueError):
            raise CheckError(f"{path}: nested too deeply") from None
    if data is None:
        raise CheckError(f"{path} is empty")
    try:
        check = check_from_dict(data, base_dir=project_root(path))
    except CheckError as error:
        raise CheckError(f"{path}: {error}") from None
    return _with_source(check, str(path.resolve()))


def _nests_too_deep(value):
    """True when ``value``'s lists and objects nest more than MAX_NESTING deep. From Python 3.14 json.loads parses
    nesting that 3.13 and earlier refused with RecursionError; YAML's parser still refuses it that way."""
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if isinstance(node, (dict, list)):
            if depth == MAX_NESTING:
                return True
            pending.extend((child, depth + 1) for child in (node.values() if isinstance(node, dict) else node))
    return False


def _with_source(check, source):
    from dataclasses import replace
    return replace(check, source=source)


def check_to_dict(check, *, project_root=None):
    """The file shape of ``check``: ``app.path`` relative to ``project_root`` when it sits inside it."""
    check = check.fixture_origin or check     # a check with its fixtures flattened in is saved as it was written
    out = {"version": 1, "name": check.name}
    app = {}
    if check.bundle_id:
        app["bundle"] = check.bundle_id
    if check.app_path:
        path = Path(check.app_path)
        if project_root is not None:
            try:
                path = path.resolve().relative_to(Path(project_root).resolve())
            except ValueError:
                pass
        app["path"] = str(path)
    out["app"] = app
    if check.device:
        out["device"] = check.device
    if check.runtime:
        out["runtime"] = check.runtime
    if check.reset != "auto":
        out["reset"] = check.reset
    if check.tags:
        out["tags"] = list(check.tags)
    if check.schedule:
        out["schedule"] = check.schedule
    launch = {}
    if check.launch_args:
        launch["args"] = list(check.launch_args)
    if check.launch_env:
        launch["env"] = dict(check.launch_env)
    if check.open_url:
        launch["url"] = check.open_url
    if launch:
        out["launch"] = launch
    if check.steps:
        out["steps"] = list(check.steps)
    out["expect"] = [assertion.to_dict() for assertion in (*check.expect, *check.wire)] + [
        {"sound": name} for name in check.sound]
    if check.snapshot:
        out["expect"].append(check.snapshot.to_dict())
    if check.throughout:
        out["throughout"] = [assertion.to_dict() for assertion in check.throughout]
    if check.somewhere:
        out["somewhere"] = [assertion.to_dict() for assertion in check.somewhere]
    if check.audit:
        out["audit"] = list(check.audit)
    if check.a11y:
        out["a11y"] = check.a11y
    if check.fixture:
        out["fixture"] = list(check.fixture)
    if check.business is not None:
        out["business"] = check.business.to_dict()
    if check.conditions:
        out["conditions"] = conditions.to_dict(check.conditions, project_root)
    if check.dialogs:
        out["dialogs"] = dict(check.dialogs)
    if check.stubs:
        out["stubs"] = netstubs.to_dict(check.stubs, project_root)
    if check.surfaces:
        out["surfaces"] = surface_spec.to_dict(check.surfaces)
    budget = {}
    if check.max_seconds != DEFAULT_MAX_SECONDS:
        budget["max_seconds"] = _plain_number(check.max_seconds)
    if check.max_usd != DEFAULT_MAX_USD:
        budget["max_usd"] = _plain_number(check.max_usd)
    budget.update(check.perf_budget)
    if budget:
        out["budget"] = budget
    return out


def _plain_number(value):
    return int(value) if float(value).is_integer() else value


def dump_check(check, *, project_root=None):
    """``check`` as a YAML check file that ``load_check`` reads back to the same check."""
    import yaml
    header = (f"# yaml-language-server: $schema={links.CHECK_SCHEMA}\n"
              "# A Mobster check. Run it with: mobster verify --check <this file>\n")
    return header + yaml.safe_dump(check_to_dict(check, project_root=project_root), sort_keys=False,
                                   allow_unicode=True, default_flow_style=False, width=100)


SAVE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")


def save_check_file(check, name, checks_dir):
    """Write ``check`` as ``<checks_dir>/<name>.yaml``, with paths relative to that project. Returns the path."""
    if not isinstance(name, str) or not SAVE_NAME.fullmatch(name):
        raise CheckError("a check's name is 1 to 60 of a-z, 0-9 and -, starting with a letter or digit")
    checks_dir = Path(checks_dir)
    checks_dir.mkdir(parents=True, exist_ok=True)
    root = checks_dir.parent.parent if checks_dir.parent.name == ".mobster" else None
    path = checks_dir / f"{name}.yaml"
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(dump_check(check, project_root=root), encoding="utf-8")
    os.replace(temporary, path)
    return path
