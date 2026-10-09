"""A check: a plain-English flow plus the assertions that judge it (docs/checks.md).

One in-memory form, ``Check``, and three ways in: a YAML (or JSON) file, the CLI's flags and the MCP tools'
arguments. ``check_from_dict`` reads the file shape and the tool shape; every unknown key is an error, so a typo
never silently weakens a check.

The file shape::

    version: 1
    name: A new user sees three plans on the paywall
    app: {bundle: dev.mobster.daybreak, path: .mobster/build/.../Daybreak.app}
    device: iPhone 17 Pro
    runtime: iOS 26.4
    reset: reinstall                 # none | data | reinstall
    tags: [smoke, paywall]           # `mobster test --tag smoke` picks checks by tag
    launch: {args: [...], env: {KEY: value}, url: daybreak://paywall}
    steps: [...]                     # plain English; none = launch-only
    expect: [...]                    # assertions (assertions.py)
    budget: {max_seconds: 180, max_usd: 0.25}

The tool shape is flat: app_path, bundle_id, device, runtime, reset, launch_args, launch_env, open_url, name,
steps, expect, max_usd, max_seconds.
"""

from dataclasses import dataclass, field
import difflib
import json
import math
import os
from pathlib import Path
import re
from typing import Optional

from ..state import validate_bundle_id
from .assertions import CheckError, expand_assertion

__all__ = ["CheckError", "Check", "check_from_dict", "load_check", "dump_check", "project_root",
           "RESETS", "LIMITS"]

RESETS = ("none", "data", "reinstall")
LIMITS = {"name": 120, "steps": 20, "step": 500, "expect": 50, "args": 20, "env": 20, "url": 2000,
          "max_usd": 1.00, "max_seconds": 600, "tags": 10}
DEFAULT_MAX_SECONDS = 180
DEFAULT_MAX_USD = 0.25
FILE_LIMIT = 1_000_000
ENV_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
TAG = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
LAUNCH_ONLY_NAME = "Launch check"

FILE_KEYS = ("version", "name", "app", "device", "runtime", "reset", "tags", "launch", "steps", "expect", "budget")
TOOL_KEYS = ("name", "app_path", "bundle_id", "device", "runtime", "reset", "launch_args", "launch_env", "open_url",
             "steps", "expect", "max_usd", "max_seconds")
# Keys an MCP tool call carries that are not part of the check.
TOOL_ONLY = ("image",)


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

    def reset_level(self):
        """The reset this run uses: the check's own, else reinstall with a .app and data without. A system
        app (com.apple.*) is never reset."""
        if (self.bundle_id or "").startswith("com.apple."):
            return "none"
        if self.reset != "auto":
            return self.reset
        return "reinstall" if self.app_path else "data"


# -- reading -----------------------------------------------------------------------------------------------------

def _unknown(obj, allowed, where):
    for key in obj:
        if key not in allowed:
            close = difflib.get_close_matches(str(key), allowed, n=1)
            hint = f"; did you mean {close[0]}?" if close else f"; use {', '.join(allowed)}"
            raise CheckError(f"{where}has an unknown key {key!r}{hint}")


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


def _expect(value):
    if value is None:
        return ()
    if not isinstance(value, list):
        raise CheckError("expect must be a list of assertions, such as [{text: Continue}]")
    out = []
    for index, item in enumerate(value):
        out.extend(expand_assertion(item, f"expect[{index}]"))
    if len(out) > LIMITS["expect"]:
        raise CheckError(f"expect has more than {LIMITS['expect']} assertions")
    return tuple(out)


def _url(value, where):
    url = _text(value, where, LIMITS["url"])
    if url is not None and not URL_SCHEME.match(url):
        raise CheckError(f"{where} must be a link with a scheme, such as daybreak://paywall")
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


def check_from_dict(data, *, base_dir=None):
    """A Check from the file shape (§2.1) or the MCP tool arguments. A relative ``app.path`` resolves against
    ``base_dir`` (the project root for a file), else the working directory. Raises CheckError."""
    if not isinstance(data, dict):
        raise CheckError("a check must be an object (a YAML mapping)")
    data = {key: value for key, value in data.items() if key not in TOOL_ONLY}
    file_only = [key for key in ("version", "app", "launch", "budget") if key in data]
    tool_only = [key for key in ("app_path", "bundle_id", "launch_args", "launch_env", "open_url", "max_usd",
                                 "max_seconds") if key in data]
    if file_only and tool_only:
        raise CheckError(f"the check mixes the file format ({', '.join(file_only)}) and tool arguments "
                         f"({', '.join(tool_only)})")
    if "tags" in data and tool_only:
        raise CheckError("tags goes in a check file, not in tool arguments")
    if not file_only and not tool_only:
        _unknown(data, tuple(dict.fromkeys(FILE_KEYS + TOOL_KEYS)), "the check ")
        raise CheckError("the check needs an app: app.bundle or app.path in a check file, bundle_id or app_path "
                         "in tool arguments")
    if tool_only:
        _unknown(data, TOOL_KEYS, "the check ")
        fields = {"app_path": data.get("app_path"), "bundle": data.get("bundle_id"),
                  "args": data.get("launch_args"), "env": data.get("launch_env"), "url": data.get("open_url"),
                  "max_usd": data.get("max_usd"), "max_seconds": data.get("max_seconds"),
                  "where": {"path": "app_path", "bundle": "bundle_id", "args": "launch_args",
                            "env": "launch_env", "url": "open_url", "max_usd": "max_usd",
                            "max_seconds": "max_seconds"}}
    else:
        _unknown(data, FILE_KEYS, "the check ")
        version = data.get("version", 1)
        if version != 1 or isinstance(version, bool):
            raise CheckError(f"version {version!r} is not supported; 1 is the only version")
        app = data.get("app") or {}
        if not isinstance(app, dict):
            raise CheckError("app must be an object with bundle and/or path")
        _unknown(app, ("bundle", "path"), "app ")
        launch = data.get("launch") or {}
        if not isinstance(launch, dict):
            raise CheckError("launch must be an object with args, env and/or url")
        _unknown(launch, ("args", "env", "url"), "launch ")
        budget = data.get("budget") or {}
        if not isinstance(budget, dict):
            raise CheckError("budget must be an object with max_seconds and/or max_usd")
        _unknown(budget, ("max_seconds", "max_usd"), "budget ")
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
    return Check(
        name=name, steps=steps, expect=_expect(data.get("expect")), app_path=app_path, bundle_id=bundle,
        device=_text(data.get("device"), "device", 100), runtime=_text(data.get("runtime"), "runtime", 100),
        reset=reset or "auto", launch_args=_strings(fields["args"], where["args"], LIMITS["args"]),
        launch_env=_env(fields["env"], where["env"]), open_url=_url(fields["url"], where["url"]),
        max_seconds=_number(fields["max_seconds"], where["max_seconds"], 0, LIMITS["max_seconds"],
                            DEFAULT_MAX_SECONDS),
        max_usd=_number(fields["max_usd"], where["max_usd"], 0, LIMITS["max_usd"], DEFAULT_MAX_USD),
        tags=_tags(data.get("tags")))


def project_root(path):
    """The directory holding ``.mobster/``, walking up from ``path`` (a file or folder); else ``path``'s own
    folder."""
    path = Path(path).resolve()
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


def _with_source(check, source):
    from dataclasses import replace
    return replace(check, source=source)


# -- writing -----------------------------------------------------------------------------------------------------

def check_to_dict(check, *, project_root=None):
    """The file shape of ``check``: ``app.path`` relative to ``project_root`` when it sits inside it."""
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
    out["expect"] = [assertion.to_dict() for assertion in check.expect]
    budget = {}
    if check.max_seconds != DEFAULT_MAX_SECONDS:
        budget["max_seconds"] = _plain_number(check.max_seconds)
    if check.max_usd != DEFAULT_MAX_USD:
        budget["max_usd"] = _plain_number(check.max_usd)
    if budget:
        out["budget"] = budget
    return out


def _plain_number(value):
    return int(value) if float(value).is_integer() else value


def dump_check(check, *, project_root=None):
    """``check`` as a YAML check file that ``load_check`` reads back to the same check."""
    import yaml
    header = "# A Mobster check. Run it with: mobster verify --check <this file>\n"
    return header + yaml.safe_dump(check_to_dict(check, project_root=project_root), sort_keys=False,
                                   allow_unicode=True, default_flow_style=False, width=100)
