"""The runner's test plan for a launch over Wi-Fi (SPEC §3.7 item 3).

WebDriverAgent listens on the address in its ``USE_IP`` environment variable. On the cable that is 127.0.0.1 (the
phone's loopback, which usbmux reaches; device_manager.pin_loopback keeps the built plan so). Over Wi-Fi it must be
the phone's end of the encrypted CoreDevice tunnel, an fd00::/8 address, so WDA answers only through the tunnel and
never on the phone's Wi-Fi address.

``write`` makes a per-launch copy of the built ``.xctestrun`` in its own folder (never beside the original, which
xcodebuild's scheme-based launch on the cable finds by itself), with ``__TESTROOT__`` spelled out as the products
folder and ``USE_IP`` set on every test target. Any other address is refused: never 0.0.0.0, ::, empty or a LAN
address, and never an fd00::/8 address this Mac reaches through Wi-Fi or Ethernet (relay.require_tunnel).
"""

import os
from pathlib import Path
import plistlib
import tempfile

from .relay import check_target, require_tunnel

FOLDER = "mobster-wifi"
TESTROOT = "__TESTROOT__"


def source_plan(products):
    """The built ``.xctestrun`` in ``products`` (the newest, when a rebuild left more than one), or LookupError."""
    plans = sorted(Path(products).glob("*.xctestrun"), key=lambda path: (path.stat().st_mtime, path.name))
    if not plans:
        raise LookupError("Build Mobster's helper for this iPhone first.")
    return plans[-1]


def targets(plan):
    """The test targets of a parsed ``.xctestrun``: format 2 keeps them in TestConfigurations[].TestTargets[] (beside
    other top-level dicts, such as TestPlan, that aren't targets); format 1 at the top level."""
    configurations = plan.get("TestConfigurations")
    if isinstance(configurations, list):
        return [target for configuration in configurations if isinstance(configuration, dict)
                for target in configuration.get("TestTargets") or () if isinstance(target, dict)]
    return [value for key, value in plan.items() if not key.startswith("__") and isinstance(value, dict)]


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
    """A copy of the parsed ``plan`` for a launch over Wi-Fi: ``__TESTROOT__`` spelled out and USE_IP the tunnel
    address on every target. ValueError for an address that isn't a tunnel address; LookupError for a plan with no
    test target."""
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


def write(products, folder, address, *, through=None):
    """Write the Wi-Fi copy of the built plan into ``folder`` (0700, the file 0600); returns its path. ValueError
    for an address that isn't a tunnel address, or that this Mac doesn't reach through the tunnel (``through``
    replaces that route lookup in tests)."""
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
