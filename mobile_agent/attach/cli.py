"""`mobster doctor --apps`: score the apps on the phone for automation. Hidden; needs MOBSTER_EXPERIMENTAL_ATTACH=1."""

import json
import sys
import time
import urllib.request

from . import ENV_FLAG, enabled, score

SETTLE_SECONDS = 3.0
ENV_HINT = f"mobster doctor --apps is experimental: set {ENV_FLAG}=1 to use it"


def installed_bundles(udid):
    """User-app bundle IDs on the phone, or () when the listing is unavailable."""
    from ..installed_apps import InstalledApps
    inventory = InstalledApps(lambda: {"udid": udid, "trusted": True}).refresh() if udid else None
    return tuple(app["bundleId"] for app in inventory.user) if inventory else ()


def run(args, wda_url):
    """Launch each installed app, read its first screen, store and print the scores. Returns an exit code."""
    from ..compose import build_target_driver
    from ..verify.tree import read_tree
    if not enabled():
        print(ENV_HINT, file=sys.stderr)
        return 2
    with urllib.request.urlopen(wda_url + "/status", timeout=10) as response:
        session = json.load(response)["sessionId"]
    driver = build_target_driver(wda_url=wda_url, session=session)
    bundles = installed_bundles(getattr(args, "device_udid", None))
    if not bundles:
        print("mobster doctor --apps: no installed apps could be listed. Plug the iPhone in with a cable, or "
              "name a USB device with --device.", file=sys.stderr)
        return 1
    scores = score.score_apps(
        lambda: read_tree(driver), lambda b: driver.call("POST", "/wda/apps/activate", {"bundleId": b}, timeout=20),
        bundles, settle=lambda: time.sleep(SETTLE_SECONDS))
    driver.call("POST", "/wda/pressButton", {"name": "home"}, timeout=10)
    path = score.save(scores)
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "apps": [s.public() for s in scores], "stored": str(path)}))
    else:
        print(score.report(scores))
        print(f"Stored in {path}")
    return 0
