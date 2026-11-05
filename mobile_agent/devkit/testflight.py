"""TestFlight builds for a project: list them, choose one, install it on an iPhone, and record the install."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .. import env_file
from ..verify.runs import ensure_gitignore
from . import DevError, asc

RECORD_DIR = ("runs", "testflight")
READY = frozenset({"IN_BETA_TESTING", "READY_FOR_BETA_TESTING"})
WHY = {"PROCESSING": "Apple is still processing it", "PROCESSING_EXCEPTION": "Apple couldn't process it",
       "MISSING_EXPORT_COMPLIANCE": "it needs export compliance answered in App Store Connect",
       "IN_EXPORT_COMPLIANCE_REVIEW": "it is in export compliance review",
       "WAITING_FOR_BETA_REVIEW": "it is waiting for beta review", "IN_BETA_REVIEW": "it is in beta review",
       "BETA_REJECTED": "beta review rejected it", "EXPIRED": "it expired"}
BUNDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,200}")


def check_row(row):
    """``row`` (asc.build_row) with ``installable`` and, when it isn't, ``why`` in a sentence."""
    states = [state for state in (row.get("internal"), row.get("external")) if state]
    if row.get("expired") or "EXPIRED" in states:
        why = "it expired (TestFlight builds last 90 days)"
    elif row.get("processing") != "VALID":
        why = WHY.get(row.get("processing")) or "Apple hasn't finished processing it"
    elif not any(state in READY for state in states):
        why = next((WHY[state] for state in states if state in WHY), "no tester group has it")
    else:
        return {**row, "installable": True, "why": ""}
    return {**row, "installable": False, "why": why}


def builds(client, bundle, *, limit=20):
    """(app, rows): the app and its builds, newest first."""
    app = client.app(bundle)
    return app, [check_row(row) for row in client.builds(app["id"], limit=limit)]


def choose(rows, build=None, version=None):
    if build is None and version is None:
        for row in rows:
            if row["installable"]:
                return row
        raise DevError("No build of this app can be installed from TestFlight right now."
                       + (f" The newest, {_name(rows[0])}, {rows[0]['why']}." if rows else " It has no builds."),
                       "Upload a build, or add it to a tester group in App Store Connect." if not rows else
                       "Fix that in App Store Connect, or upload a new build.")
    found = [row for row in rows if (build is None or str(row["build"]) == str(build))
             and (version is None or row["version"] == version)]
    if not found:
        raise DevError(f"TestFlight has no build {version + ' ' if version else ''}{build or ''}".strip()
                       + f" in the newest {len(rows)} builds.", "Run `mobster testflight list` to see them.")
    row = found[0]
    if not row["installable"]:
        raise DevError(f"Build {_name(row)} can't be installed: {row['why']}.",
                       "Pick another build, or fix it in App Store Connect.")
    return row


def _name(row):
    return f"{row['version'] or '?'} ({row['build']})"


# -- the record ----------------------------------------------------------------------------------------------------

def _record_path(root, bundle):
    if not BUNDLE.fullmatch(bundle or ""):
        raise DevError(f"{bundle!r} isn't a bundle ID.", "Pass the app's bundle ID, such as com.example.habits.")
    return Path(root) / ".mobster" / Path(*RECORD_DIR) / f"{bundle}.json"


def read_record(root, bundle):
    """The record of the last install of ``bundle`` in this project, or None."""
    try:
        data = json.loads(_record_path(root, bundle).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise DevError("The previous TestFlight install record cannot be read.",
                       "Check the iPhone and reconcile the record before installing again.") from None
    if not isinstance(data, dict) or data.get("status") not in {"installing", "installed"}:
        raise DevError("The previous TestFlight install record is invalid.",
                       "Check the iPhone and reconcile the record before installing again.")
    return data


def _write_record(root, bundle, data):
    path = _record_path(root, bundle)
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_gitignore(path.parent.parent)
    temp = path.with_suffix(".tmp")
    with env_file.open_private(temp, "w") as handle:
        handle.write(json.dumps(data, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def recorded_build(root, bundle, *, devices=()):
    try:
        data = read_record(root, bundle)
    except DevError:
        return None  # metadata cannot prevent the suite from writing its actual verdict
    # an install whose outcome is unknown names no build: it may not be on the phone
    if (not data or data.get("status") != "installed"
            or (devices and not set(devices).intersection(data.get("devices") or ()))):
        return None
    return {key: data.get(key) for key in ("bundleId", "version", "build", "installedAt")}


# -- the install ---------------------------------------------------------------------------------------------------

def install(client, record, wda, bundle, *, root, build=None, version=None, progress=None, now=None, **options):
    with env_file.writer(_record_path(root, bundle)):
        app, rows = builds(client, bundle, limit=50)
        row = choose(rows, build, version)
        newest = next(item for item in rows if item["installable"])
        if row["id"] != newest["id"]:
            raise DevError(
                f"TestFlight installs the newest build, {_name(newest)}, from the app's page, and you asked for "
                f"{_name(row)}. Mobster won't pick from Previous Builds for you.",
                f"On the iPhone, open TestFlight, tap {app['name']}, tap Previous Builds, tap {_name(row)}, then tap "
                f"Install. Then run `mobster test --device NAME --testflight {bundle}`.")
        earlier = read_record(root, bundle)
        unknown = bool(earlier) and earlier.get("status") == "installing"
        if unknown and earlier.get("buildId") != row["id"]:
            raise DevError("An earlier TestFlight install has an unknown outcome.",
                           "Check that build on the iPhone before requesting a different build.")
        stamp = (now or (lambda: datetime.now(timezone.utc)))().strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = {"bundleId": bundle, "version": row["version"], "build": row["build"], "buildId": row["id"],
                 "startedAt": stamp, "devices": [value for value in (record.get("id"), record.get("name")) if value]}
        _write_record(root, bundle, {**entry, "status": "installing"})  # before the first tap, so a crash is "unknown"
        try:
            result = asc.testflight_install(record, wda, app["id"], progress=progress, tap=not unknown,
                                            expected=(row["version"], row["build"]), **options)
        except asc.UnknownOutcome:
            raise  # the record stays "installing"
        except DevError:  # no tap went out (terms, no button, a simulator, or watch-only): the old record stands
            if earlier:
                _write_record(root, bundle, earlier)
            else:
                _record_path(root, bundle).unlink(missing_ok=True)
            raise
        finished = {**entry, "status": "installed", "installedAt": stamp}
        _write_record(root, bundle, finished)
        return {"result": result, "row": row, "app": app, "record": finished}
