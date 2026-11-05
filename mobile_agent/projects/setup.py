"""Set up checks in a project: `.mobster/` with its `.gitignore` and a starter check for the project's app.

The starter check opens the app and expects its first screen to show something with a label. It passes when the app
launches, stays in front and doesn't crash, and it needs no model and no key. It is a normal check file
(docs/checks.mdx), checked as `mobster test` checks one before it is written, and Mobster never writes over a file:
a project with checks already is refused.

    created = create(root, apps)       # [".mobster/.gitignore", ".mobster/checks/app-opens.yaml"]
"""

from pathlib import Path

from ..api_errors import APIError
from . import folder as folders

STARTER = "app-opens.yaml"
HEADER = ("# A Mobster check: it opens {name} and passes when its first screen shows. No model, no key.\n"
          "# Run checks in Mobster for Mac builds the app, then runs it; `mobster test` runs it on the app the "
          "simulator has.\n")


def starter(app):
    """The starter check for ``app`` (folder.apps' {kind, name, path, bundle}) as file data."""
    return {"version": 1, "name": f"{app['name']} opens to its first screen"[:120], "app": {"bundle": app["bundle"]},
            "tags": ["smoke"], "expect": [{"visible": {"label": "/.+/"}, "name": "The first screen shows something"}]}


def pick(apps, wanted=None):
    """The app the starter check is for: the one at ``wanted`` (its path in folder.apps), else the first with a bundle
    id. APIError with what to do when there's none."""
    if wanted is not None:
        app = next((app for app in apps if app["path"] == wanted), None)
        if app is None:
            raise APIError("This project has no app at that path. Choose one of the apps on its page.", 400, "no_app")
    else:
        app = next((app for app in apps if app.get("bundle")), apps[0] if apps else None)
    if app is None:
        raise APIError("Mobster found no Xcode project or Expo app in this folder, so it can't write a check for one. "
                       "Add one with: mobster test add smoke --bundle YOUR.BUNDLE.ID", 409, "no_app")
    if not app.get("bundle"):
        raise APIError(f"Mobster couldn't read {app['name']}'s bundle id from its project. Add a check with: "
                       "mobster test add smoke --bundle YOUR.BUNDLE.ID", 409, "no_bundle")
    return app


def create(root, apps, *, wanted=None, home=None):
    """Write `.mobster/.gitignore` (unless there is one) and `.mobster/checks/app-opens.yaml`. Returns the paths it
    wrote, relative to the project. APIError when the project has checks already or the folder can't be written."""
    import yaml

    from ..verify.assertions import CheckError
    from ..verify.checks import check_from_dict
    from ..verify.runs import ensure_gitignore
    root = Path(root)
    if folders.checks(root):
        raise APIError("This project has checks already, in .mobster/checks. Run them, or add one with mobster test "
                       "add.", 409, "has_checks")
    app = pick(apps, wanted)
    data = starter(app)
    try:
        check_from_dict(data, base_dir=root)
    except CheckError as error:   # the bundle id the project file holds isn't one a check takes
        raise APIError(f"Mobster couldn't write a check for {app['name']}: {error}.", 409, "no_bundle") from None
    text = HEADER.format(name=app["name"]) + yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                                                            default_flow_style=False, width=100)
    mobster = root / ".mobster"
    target = mobster / "checks" / STARTER
    created = []
    try:
        had_ignore = (mobster / ".gitignore").exists()
        ensure_gitignore(mobster / "runs")
        if not had_ignore and (mobster / ".gitignore").exists():
            created.append(".mobster/.gitignore")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        raise APIError(f".mobster/checks/{STARTER} is there already, and Mobster never writes over a file. Run it, or "
                       "move it.", 409, "has_checks") from None
    except OSError as error:
        raise APIError(f"Mobster can't write in {folders.shown(mobster, home)} ({error.strerror or 'refused'}). Check "
                       "the folder's permissions, then try again.", 403, "unwritable") from None
    created.append(f".mobster/checks/{STARTER}")
    return created
