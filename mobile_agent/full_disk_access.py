"""Whether Mobster has Full Disk Access to the folders a feature needs, checked without reading content."""

import errno
import os
from pathlib import Path

from .api_errors import APIError
from .api_routes import Response, Route, register

HOME = Path(os.path.expanduser("~"))
USERNOTED_DB = HOME / "Library/Group Containers/group.com.apple.usernoted/db2/db"
IPHONE_LIBRARY = HOME / ("Library/Group Containers/group.com.apple.UserNotifications/Library/UserNotifications/"
                         "Remote/default/Library.plist")
MESSAGES_DB = HOME / "Library/Messages/chat.db"

# The paths each feature needs, in the order the answer's ``path`` prefers them.
FEATURES = {
    "triggers": (IPHONE_LIBRARY, USERNOTED_DB),
    "waiting": (MESSAGES_DB,),
}
MESSAGES = {
    "granted": "Mobster can read this.",
    "needed": "macOS is keeping this from Mobster. Turn on Mobster in System Settings › Privacy & Security › Full "
              "Disk Access.",
    "missing": "This isn't on this Mac yet.",
}


def check(path) -> str:
    """One path: "granted", "needed" or "missing". Reads no content."""
    target = Path(path)
    try:
        if target.is_dir():
            os.listdir(target)
        else:
            with open(target, "rb"):
                pass
    except FileNotFoundError:
        return "missing"
    except NotADirectoryError:
        return "missing"
    except PermissionError:
        return "needed"
    except OSError as error:
        # fails closed: an error it doesn't know is "needed"
        return "missing" if error.errno in (errno.ENOENT, errno.ENOTDIR) else "needed"
    return "granted"


def status(paths) -> dict:
    """{"state": "granted"|"needed"|"missing", "path": str|None, "paths": [{"path", "state"}]}."""
    results = [{"path": str(path), "state": check(path)} for path in paths]
    for state in ("needed", "granted"):
        for result in results:
            if result["state"] == state:
                return {"state": state, "path": result["path"], "paths": results}
    return {"state": "missing", "path": results[0]["path"] if results else None, "paths": results}


def feature_status(feature: str, paths=None) -> dict:
    """``status`` for a feature's own paths, with the feature and a sentence. ValueError for an unknown feature."""
    if paths is None:
        if feature not in FEATURES:
            raise ValueError("feature is one of: " + ", ".join(sorted(FEATURES)))
        paths = FEATURES[feature]
    answer = status(paths)
    return {"feature": feature, **answer, "message": MESSAGES[answer["state"]]}


def route(request):
    feature = (request.query.get("feature") or [""])[0]
    try:
        return Response.json(feature_status(feature))
    except ValueError as error:
        raise APIError(str(error), 400, "bad_feature") from None


def register_route() -> None:
    """Register GET /api/access/full-disk once per process; a second caller (another feature's track) is a no-op."""
    from . import api_routes
    if any(r.method == "GET" and r.pattern == "/api/access/full-disk" for r in api_routes._routes):
        return
    register(Route("access", "GET", "/api/access/full-disk", route, body="none"))
