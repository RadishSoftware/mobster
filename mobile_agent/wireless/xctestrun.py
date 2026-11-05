"""The runner's test plan for a launch over Wi-Fi (SPEC §3.7 item 3)."""

import os
import plistlib
import tempfile
from pathlib import Path

from ..wda.patches import xctestrun_targets as targets
from .relay import check_target, require_tunnel

FOLDER = "mobster-wifi"
TESTROOT = "__TESTROOT__"


def source_plan(products):
    """The built ``.xctestrun`` in ``products`` (the newest, when a rebuild left more than one), or LookupError."""
    plans = sorted(Path(products).glob("*.xctestrun"), key=lambda path: (path.stat().st_mtime, path.name))
    if not plans:
        raise LookupError("Build Mobster's helper for this iPhone first.")
    return plans[-1]


def use_ips(plan):
    """Every USE_IP the plan sets, in order (EnvironmentVariables, then TestingEnvironmentVariables)."""
    values = []
    for target in targets(plan):
        for section in ("EnvironmentVariables", "TestingEnvironmentVariables"):
            env = target.get(section)
            if isinstance(env, dict) and "USE_IP" in env:
                values.append(env["USE_IP"])
    return values


def _spell_out(value, root):
    if isinstance(value, str):
        return value.replace(TESTROOT, root)
    if isinstance(value, list):
        return [_spell_out(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _spell_out(item, root) for key, item in value.items()}
    return value


def prepare(plan, products, address):
    address = check_target(address)
    copy = _spell_out(plan, str(Path(products).resolve()))
    found = targets(copy)
    if not found:
        raise LookupError("Mobster's helper build has no test in it. Build it again in Setup.")
    for target in found:
        env = target.get("EnvironmentVariables")
        if not isinstance(env, dict):
            env = target["EnvironmentVariables"] = {}
        env["USE_IP"] = address
        testing = target.get("TestingEnvironmentVariables")
        if isinstance(testing, dict) and "USE_IP" in testing:
            testing["USE_IP"] = address
    if not use_ips(copy) or any(value != address for value in use_ips(copy)):
        raise ValueError("The helper's address couldn't be set.")
    return copy


# Its own folder, never beside the built plan: the cable's scheme-based launch finds that one by itself.
def write(products, folder, address, *, through=None):
    address = require_tunnel(address, through)
    source = source_plan(products)
    with open(source, "rb") as stream:
        plan = plistlib.load(stream)
    copy = prepare(plan, products, address)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    target = folder / source.name
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".xctestrun-")
    try:
        with os.fdopen(handle, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            plistlib.dump(copy, stream)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target
