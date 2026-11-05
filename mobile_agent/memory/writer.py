"""How the rest of Mobster teaches memory: one place, safe to call from anywhere.

Each function turns something Mobster already knows (a recorded path, a suite's results, an X-ray map, a profile, a
finished run) into plain sentences and hands them to the store. Failures do not interrupt the caller: with no store,
with Suggest things to remember off, or on any error, it does nothing. Writers use these sources: ``run`` (a task),
``lesson`` (something the person taught), ``check`` (a suite), ``repo`` (the project's files) and ``profile``. What a
task learns mid-run is staged and saved when it finishes (runs.py); what is known at once (a profile saved, a suite
finished) is saved now.

Sentences, not screen text, make the fact: a label read from a screen is quoted as a name, never joined into an order.
"""

import logging
import time

from ..catalog import app_name
from . import project
from .model import FACT_CHARS
from .store import default_store

log = logging.getLogger("mobster.memory")
MAX_PATH_STEPS = 6


def _safe(function):
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:  # noqa: BLE001 -- memory never holds up the thing that taught it
            log.exception("memory: %s failed", function.__name__)
            return None
    wrapper.__name__ = function.__name__
    wrapper.__doc__ = function.__doc__
    return wrapper


def fit(text, limit=FACT_CHARS):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def quote(label, limit=40):
    return "“" + fit(label, limit).strip("“”\"") + "”"


def _name(bundle):
    return (app_name(bundle) if bundle else None) or bundle or "the app"


def learn(text, kind, scope, source, **kwargs):
    """Save a fact now. None when there is no store."""
    store = default_store()
    return store.learn(fit(text), kind, scope, source, **kwargs) if store is not None else None


def stage(run_id, items):
    """Hold facts for a run; saved when it finishes undisputed."""
    store = default_store()
    if store is None or not run_id:
        return 0
    return store.stage(run_id, [{**item, "text": fit(item["text"])} for item in items])


# -- a recorded path (learned/workflows.py) -----------------------------------------------------------------------

def describe_steps(steps):
    """"tap “General”, then tap “About”" from a recorded path's steps (labels only: typed text is never kept)."""
    parts = []
    for step in steps:
        label = ((step.get("target") or {}).get("label") or "").strip()
        op = str(step.get("op") or "").upper()
        if op == "LAUNCH_APP" or not label:
            continue
        verb = "type in" if op in ("TYPE", "TYPE_TEXT", "SET_TEXT") else "tap"
        parts.append(f"{verb} {quote(label, 30)}")
    return ", then ".join(parts[:MAX_PATH_STEPS]) + ("…" if len(parts) > MAX_PATH_STEPS else "")


@_safe
def path_learned(run_id, bundle, template, steps):
    """A run that ended done taught Mobster a path: how to do ``template`` in ``bundle``'s app."""
    route = describe_steps(steps)
    if not bundle or not route:
        return None
    return learn(f"In {_name(bundle)}, {quote(template, 70)} goes: {route}", "path", f"app:{bundle}", "run",
                 ref={"runId": run_id}, run_id=run_id,
                 key=f"path:{bundle}:{' '.join(str(template).lower().split())[:80]}",
                 confidence=0.8)


# -- a suite (testkit/api.py) ---------------------------------------------------------------------------------------

def _bundle(check):
    app = getattr(check, "app", None)
    app = app if isinstance(app, str) else None
    return app if app and "." in app and "/" not in app else None


def _project(store, root):
    scope = project.scope_of(store, root)
    project_id = scope.partition(":")[2]
    if store.project_path(project_id) is None:
        store.link_project(project_id, str(root.resolve()))
    project.import_file(store, project_id)
    return scope, project_id


@_safe
def from_suite(doc, planned):
    """The project a suite ran in, what its checks walk and the heals it recorded."""
    store = default_store()
    if store is None:
        return None
    scope, project_id = _project(store, planned.root)
    entries = {entry.file: entry for entry in planned.entries if entry.check is not None}
    names = [entry.name for entry in planned.entries]
    devices = sorted({target.label for target in planned.targets}) if planned.targets else []
    if names:
        learn(f"The project has {len(names)} check{'s' if len(names) != 1 else ''} in .mobster/checks: "
              f"{', '.join(quote(name, 30) for name in names[:8])}{'…' if len(names) > 8 else ''}"
              + (f". They run on {', '.join(devices[:4])}." if devices else "."),
              "repo", scope, "repo", key="repo:checks", confidence=0.9)
    bundles = sorted({b for b in (_bundle(entry.check) for entry in entries.values()) if b})
    if bundles:
        learn(f"The app under test is {', '.join(bundles[:3])}.", "repo", scope, "repo", key="repo:bundle",
              confidence=0.9)
    stamp = time.strftime("%-d %b %Y", time.localtime())
    for check in doc.get("checks") or ():
        entry = entries.get(check.get("file"))
        passed = [cell for cell in check.get("results") or () if cell.get("status") == "passed"
                  and not cell.get("quarantined")]
        if entry is not None and passed and entry.check.steps:
            learn(f"The check {quote(check['name'], 50)} passes on {passed[0]['device']}: "
                  + "; ".join(fit(step, 60) for step in entry.check.steps[:5]),
                  "structure", scope, "check", ref={"checkPath": check["file"]}, key=f"check:{check['file']}",
                  confidence=0.85)
        for cell in check.get("results") or ():
            for heal in cell.get("healed") or ():
                if isinstance(heal, dict):
                    from ..verify.report import heal_title
                    learn(f"In the check {quote(check['name'], 50)}, {heal_title(heal)} on {stamp}.", "outcome",
                          scope, "check", ref={"checkPath": check["file"]},
                          key=f"heal:{check['file']}:{heal_title(heal)}", confidence=0.8)
    project.export_file(store, project_id)
    return project_id


@_safe
def from_build(root, scheme, bundle):
    """`mobster test pr` built the project: its scheme and the app's bundle id."""
    store = default_store()
    if store is None or not scheme:
        return None
    scope, project_id = _project(store, root)
    result = learn(f"The scheme is {scheme}" + (f" and the bundle is {bundle}." if bundle else "."), "repo", scope,
                   "repo", key="repo:scheme", confidence=0.9)
    project.export_file(store, project_id)
    return result


# -- an X-ray map (xray/job.py) -------------------------------------------------------------------------------------

@_safe
def from_xray(run_id, bundle, result):
    """The screens App X-ray mapped: the app's structure, and where its deeper screens are."""
    screens = [s for s in result.get("screens") or () if s.get("name")]
    if not bundle or not screens:
        return 0
    name = _name(bundle)
    top = [s["name"] for s in screens if s.get("depth", 0) == 1][:6]
    items = [{"text": f"{name} has {len(screens)} mapped screens"
              + (f"; its tabs are {', '.join(quote(t, 24) for t in top)}" if top else "") + ".",
              "kind": "structure", "scope": f"app:{bundle}", "source": "run", "ref": {"runId": run_id},
              "key": f"xray:structure:{bundle}", "confidence": 0.85}]
    deep = sorted((s for s in screens if s.get("depth", 0) >= 2 and s.get("path")), key=lambda s: s["depth"])[:5]
    for screen in deep:
        crumbs = " › ".join(quote(step.get("label", ""), 24) for step in screen["path"] if step.get("label"))
        items.append({"text": f"In {name}, {quote(screen['name'], 40)} is under {crumbs}.", "kind": "path",
                      "scope": f"app:{bundle}", "source": "run", "ref": {"runId": run_id},
                      "key": f"xray:path:{bundle}:{screen['name'].lower()[:60]}", "confidence": 0.8})
    return stage(run_id, items)


# -- a profile (accounts/routes.py) ---------------------------------------------------------------------------------

@_safe
def from_profile(account):
    """A profile was saved: its voice and signature are facts the person edits where they edit the profile."""
    scope = f"profile:{account['id']}"
    ref = {"profileId": account["id"]}
    who = f"@{account['handle']}"
    store = default_store()
    if store is None:
        return None
    for field, kind, make in (
            ("notes", "preference", lambda text: f"{who} on {account['app']['name']}: {text}"),
            ("signature", "value", lambda text: f"{who}'s signature is: {text}")):
        text = (account.get(field) or "").strip()
        key = f"profile:{field}"
        if text:
            store.learn(fit(make(" ".join(text.split()))), kind, scope, "profile", ref=ref, key=key, confidence=1.0,
                        authoritative=True)
        else:
            store.drop(scope, key)
    return True


@_safe
def profile_removed(account_id):
    store = default_store()
    return store.drop_scope(f"profile:{account_id}") if store is not None else None


# -- a device (runs.py) ------------------------------------------------------------------------------------------------

@_safe
def from_device(device_id, name, info):
    """What a phone reports about itself: its model and iOS version."""
    version = (info or {}).get("iosVersion")
    model = (info or {}).get("modelName") or (info or {}).get("modelIdentifier")
    if not device_id or not version:
        return None
    return learn(f"{name or 'This phone'} is " + (f"an {model} " if model else "") + f"on iOS {version}.", "device",
                 f"phone:{device_id}", "run", key="device:ios", confidence=0.95)


# -- a lesson (server.py teach_stop / teach_save / teach_discard) ----------------------------------------------------

def _lesson_items(lesson, steps, request):
    bundle = lesson.start_app or next((step.get("app") for step in steps if step.get("app")), None)
    route = describe_steps(steps)
    if not bundle or not route:
        return []
    name, words = _name(bundle), " ".join(str(request or "").lower().split())[:80]
    ref = {"lessonId": lesson.id}
    items = [{"text": f"In {name}, {quote(request or 'the task you taught', 70)} goes: {route}", "kind": "path",
              "scope": f"app:{bundle}", "source": "lesson", "ref": ref, "key": f"path:{bundle}:{words}",
              "confidence": 0.95}]
    titles = list(dict.fromkeys(str(step.get("title") or "").strip() for step in lesson.steps if step.get("title")))
    if len(titles) > 1:
        items.append({"text": f"In {name}, {quote(request or 'the task you taught', 70)} passes through these screens: "
                              + ", ".join(quote(title, 30) for title in titles[:6]) + ".",
                      "kind": "structure", "scope": f"app:{bundle}", "source": "lesson", "ref": ref,
                      "key": f"lesson:screens:{bundle}:{words}", "confidence": 0.9})
    return items


@_safe
def lesson_staged(lesson, steps, request=None):
    """A lesson was stopped: what it taught is held (replacing what an earlier stop held) until it is saved."""
    store = default_store()
    if store is None:
        return 0
    store.commit_run(lesson.id, keep=False)
    return store.stage(lesson.id,
                       [{**item, "text": fit(item["text"])} for item in _lesson_items(lesson, steps, request)])


@_safe
def lesson_saved(lesson, steps, request):
    """The lesson was saved as a program: what it taught becomes facts, and the app is told (``learned``)."""
    store = default_store()
    if store is None:
        return []
    lesson_staged(lesson, steps, request)
    results = store.commit_run(lesson.id)
    facts = [r["fact"] for r in results if r["status"] in ("added", "updated") and r["fact"]]
    if facts:
        from .listener import publish
        publish({"event": "learned", "runId": lesson.id, "facts": facts})
    return facts


@_safe
def lesson_discarded(lesson_id):
    """The lesson was cancelled: what it held is dropped."""
    store = default_store()
    return store.commit_run(lesson_id, keep=False) if store is not None else None
