"""Two workflow steps that act on the phone by code, with no model: "Open a link" (``phone.open_url``) and "Wait for a
notification" (``phone.notification``). Each rides a run field (``openUrl``, ``waitNotification``) that the step's task
carries, and a pre-run hook ends the run with the step's result, as the app pack read does (packs/register.py).

Open a link goes through the same rules as every other link (deeplinks.decide): a web address or an app Mobster knows
opens; a link that acts or writes to someone (tel:, sms:, a shortcut) asks, and with nobody to answer is not opened;
a scheme Mobster never opens is refused when the workflow is saved. The app it opens must be one the person allows.

Wait for a notification reads Notification Center every POLL_S seconds until a notification from the app (when named)
holds the words asked for, or the time is up. A match opens it when ``tap`` is on; no match in time is not a failure:
the step's ``timeout`` port runs. Notification text is data: codes are masked, and nothing in it instructs anything.
"""

import time

from .. import deeplinks, harness_api

ORDER = 30                      # after the front door and packs (2, 3); before the learned paths (20+)
POLL_S = 15.0
MAX_MINUTES = 240
URL_CHARS = 2000
MATCH_CHARS = 200


def validate_open_url(value, runtime):
    """``openUrl``: {"url"}, a link Mobster may open (a link that asks is allowed here: the hook asks)."""
    if not isinstance(value, dict) or set(value) != {"url"} or not isinstance(value["url"], str):
        raise ValueError("openUrl is {url}")
    url = value["url"].strip()
    if not url or len(url) > URL_CHARS:
        raise ValueError(f"openUrl's url is 1 to {URL_CHARS} characters")
    deeplinks.risk_of(url)       # LinkError (a ValueError): a scheme Mobster never opens
    return {"url": url}


def validate_wait_notification(value, runtime):
    """``waitNotification``: {"app": bundle id | None, "match": words | None, "timeoutMinutes", "tap"}."""
    allowed = {"app", "match", "timeoutMinutes", "tap"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("waitNotification is {app, match, timeoutMinutes, tap}")
    app, match = value.get("app"), value.get("match")
    minutes, tap = value.get("timeoutMinutes", 10), value.get("tap", True)
    if app is not None and (not isinstance(app, str) or not 1 <= len(app) <= 255):
        raise ValueError("waitNotification's app is an app's bundle id")
    if match is not None and (not isinstance(match, str) or len(match) > MATCH_CHARS):
        raise ValueError(f"waitNotification's match is at most {MATCH_CHARS} characters")
    if type(minutes) is not int or not 1 <= minutes <= MAX_MINUTES:
        raise ValueError(f"waitNotification waits 1 to {MAX_MINUTES} minutes")
    if type(tap) is not bool:
        raise ValueError("waitNotification's tap is true or false")
    return {"app": app, "match": " ".join(match.split()) if match and match.strip() else None,
            "timeoutMinutes": minutes, "tap": tap}


def register():
    """Idempotent."""
    fields = harness_api.run_fields()
    if "openUrl" not in fields:
        harness_api.register_run_field("openUrl", validate_open_url, model=False)
        harness_api.register_pre_run(open_url_hook, order=ORDER)
    if "waitNotification" not in fields:
        harness_api.register_run_field("waitNotification", validate_wait_notification, model=False)
        harness_api.register_pre_run(notification_hook, order=ORDER + 1)


def _result(status, outcome, answer, reason=None, data=None, started=None, actions=1):
    return harness_api.PreRun(result={
        "event": "result", "status": status, "engine": "smart", "outcome": outcome, "answer": answer, "reason": reason,
        "data": data, "actions": actions, "decisions": 0, "costUsd": 0.0, "model": None,
        "elapsed_ms": round((time.monotonic() - started) * 1000) if started else 0,
        "independently_verified": False, "proof": []})


def _refused(reason, started):
    return _result("completion_not_confirmed", "check", None, reason, started=started, actions=0)


def _app_allowed(ctx, bundle):
    allows = getattr(ctx.runtime, "allows_app", None)
    return True if not bundle or ctx.runtime is None or not callable(allows) else bool(allows(ctx.run_id, bundle))


def open_url_hook(ctx, driver, approve, clock=time.monotonic):
    spec = (ctx.extras or {}).get("openUrl")
    if not spec or driver is None:
        return None
    started = clock()
    url = spec["url"]
    try:
        decision = deeplinks.decide(url)
    except deeplinks.LinkError as error:
        return _refused(f"{error} Mobster didn't open it.", started)
    # A web address with no app of its own opens in Safari; that is the app that must be allowed.
    bundle = deeplinks.app_for(url) or ("com.apple.mobilesafari" if url.casefold().startswith(("http://", "https://"))
                                        else None)
    if not _app_allowed(ctx, bundle):
        return _refused("That link opens an app you haven't allowed, so Mobster didn't open it. Allow the app, then "
                        "run the step again.", started)
    if decision.action == "ask":
        request = {"kind": "commit", "step": 1, "operation": "OPEN_URL", "label": f"Open {url[:180]}", "role": "Link",
                   "text": url[:500], "title": decision.title or "Open this link?", "act": "open link",
                   "app_name": decision.app_name or None}
        answer = "denied"
        if approve is not None:
            try:
                answer = str(approve(request) or "denied")
            except Exception:  # noqa: BLE001 -- no answer is no
                answer = "denied"
        if answer != "approved":
            return _refused("That link acts or writes to someone, and nobody approved it, so Mobster didn't open it.",
                            started)
    ctx.emit({"event": "action_started", "operation": "OPEN_URL", "target": url[:80]})
    try:
        opened = deeplinks.open_link(driver, url, bundle=bundle)
        snapshot = driver.observe(timeout=10)
    except Exception as error:  # noqa: BLE001 -- the link may have opened: never opened again by this step
        return _result("completion_not_confirmed", "check", None,
                       f"Mobster sent the link, but couldn't confirm it opened ({type(error).__name__}). It won't "
                       "send it again; check the phone.", started=started)
    from ..contract_screen import screen_title
    landed = getattr(snapshot, "bundle_id", "") or opened.get("bundleId") or ""
    data = {"url": url, "landedApp": landed, "screen": (screen_title(snapshot) or "")[:120]}
    return _result("completed", "done", f"Opened {url[:120]}.", data=data, started=started)


def _matches(row, app_bundle, words):
    from ..catalog import app_name
    if app_bundle:
        name = (app_name(app_bundle) or "").casefold()
        if not name or name not in str(row.app or "").casefold():
            return False
    return not words or words.casefold() in " ".join(str(row.text or "").split()).casefold()


def notification_hook(ctx, driver, approve, clock=time.monotonic, sleep=time.sleep, center=None):
    spec = (ctx.extras or {}).get("waitNotification")
    if not spec or driver is None:
        return None
    from ..skills.notifications import NotificationCenter
    started = clock()
    if not _app_allowed(ctx, spec.get("app")):
        return _refused("That notification is from an app you haven't allowed, so Mobster didn't wait for it.", started)
    center = center or NotificationCenter(driver)
    deadline = started + spec["timeoutMinutes"] * 60
    ctx.emit({"event": "action_started", "operation": "WAIT_NOTIFICATION", "target": (spec.get("app") or "")[:80]})
    while True:
        if ctx.cancelled():
            return _refused("Stopped while waiting for a notification.", started)
        try:
            snapshot = driver.observe(timeout=10)
            rows = center.read(origin=getattr(snapshot, "bundle_id", "") or "", size=snapshot, keep_partial=True)
        except Exception as error:  # noqa: BLE001 -- a read that fails is tried again until the time is up
            rows = []
            ctx.emit({"event": "notification_read_failed", "error": type(error).__name__})
        found = next((row for row in rows if _matches(row, spec.get("app"), spec.get("match"))), None)
        if found is not None:
            public = found.public(now=time.time())
            if spec["tap"]:
                try:
                    tapped = center.open_notification(found.text, origin=getattr(snapshot, "bundle_id", "") or "",
                                                      size=snapshot)
                except Exception as error:  # noqa: BLE001 -- it may have opened: never tapped again by this step
                    return _result("completion_not_confirmed", "check", None,
                                   f"Mobster found the notification but couldn't confirm it opened it "
                                   f"({type(error).__name__}). It won't tap it again; check the phone.",
                                   started=started)
                if not tapped:
                    return _result("completion_not_confirmed", "check", None,
                                   "Mobster found the notification, but it was gone before it could open it.",
                                   started=started)
            data = {"title": public["title"], "body": public["body"], "app": public["app"], "at": public["time"],
                    "tapped": bool(spec["tap"])}
            return _result("completed", "done", f"Got a notification: {public['title'][:80]}.", data=data,
                           started=started)
        if clock() + POLL_S >= deadline:
            return _result("completed", "done", "No matching notification arrived in time.",
                           data={"timeout": True}, started=started, actions=0)
        sleep(POLL_S)
