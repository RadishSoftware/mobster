"""The local API for in-app feedback, under /api/feedback (api_routes, owner "feedback")."""

import base64
import json
import re

from ..api_errors import APIError
from ..api_routes import Response, Route, register
from ..service_support import needs_app
from . import bundle, outbox

MAX_BODY = 6_000_000
KINDS = ("bug", "feature", "support")
FIELDS = {"draftId", "kind", "title", "description", "email", "runId", "app", "screenshot", "diagnostics",
          "accessToken"}
APP_FIELDS = ("version", "macos", "connection", "phone", "ios")
APP_ONLY = "Sending feedback is in Mobster for Mac."
MASKS = re.compile(r"\[masked\]|••••••")


def _json(request):
    body = request.body
    if isinstance(body, (bytes, bytearray)):
        try:
            body = json.loads(body)
        except ValueError:
            raise ValueError("The body must be a JSON object") from None
    if not isinstance(body, dict):
        raise ValueError("The body must be a JSON object")
    return body


def _token(body):
    token = body.pop("accessToken", None)
    if not isinstance(token, str) or not 16 <= len(token) <= 8192:
        raise APIError("Sign in to Mobster to send feedback.", 401, "session")
    return token


def _masked(text, emails=True):
    """(masked text, how many values it masked)."""
    if not isinstance(text, str):
        return text, 0
    out = bundle.scrub(text, emails=emails)
    return out, max(0, len(MASKS.findall(out)) - len(MASKS.findall(text)))


def report_of(body):
    """The report as it goes to the account service: checked, masked."""
    unknown = set(body) - FIELDS
    if unknown:
        raise ValueError(f"{sorted(unknown)[0][:40]} isn't a feedback field")
    draft = body.get("draftId")
    if not isinstance(draft, str) or not outbox.DRAFT_ID.fullmatch(draft):
        raise ValueError("draftId is the draft's id, a lowercase UUID")
    if body.get("kind") not in KINDS:
        raise ValueError("kind is bug, feature or support")
    title = body.get("title")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
        raise ValueError("Add a title of 1 to 120 characters")
    description = body.get("description") or ""
    if not isinstance(description, str) or len(description) > 5000:
        raise ValueError("Keep the description under 5,000 characters")
    masked = 0
    title, count = _masked(" ".join(title.split()), emails=False)
    masked += count
    description, count = _masked(description.strip(), emails=False)
    masked += count
    report = {"clientId": draft, "kind": body["kind"], "title": title[:120], "description": description}
    email = body.get("email")
    if email not in (None, ""):
        if (not isinstance(email, str) or len(email) > 254
                or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}", email.strip())):
            raise ValueError("That reply-to email doesn't look right")
        report["email"] = email.strip()
    run_id = body.get("runId")
    if run_id is not None:
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{12}", run_id):
            raise ValueError("runId is a task's id")
        report["runId"] = run_id
    app = body.get("app")
    if app is not None:
        if not isinstance(app, dict) or set(app) - set(APP_FIELDS):
            raise ValueError("app is {version, macos, connection, phone, ios}")
        facts = {}
        for key in APP_FIELDS:
            value = app.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"app.{key} is text")
            text, count = _masked(" ".join(value.split())[:80])
            masked += count
            facts[key] = text
        report["app"] = facts
    shot = body.get("screenshot")
    if shot is not None:
        if (not isinstance(shot, dict) or set(shot) != {"type", "data"}
                or shot["type"] not in ("image/png", "image/jpeg")
                or not isinstance(shot["data"], str)):
            raise ValueError("screenshot is {type: image/png or image/jpeg, data: base64}")
        try:
            raw = base64.b64decode(shot["data"], validate=True)
        except ValueError:
            raise ValueError("The screenshot isn't base64") from None
        if len(raw) > 2_500_000:
            raise APIError("That screenshot is over 2.5 MB. Crop it, or send the report without one.", 413, "too_large")
        png, jpeg = raw[:8] == b"\x89PNG\r\n\x1a\n", raw[:3] == b"\xff\xd8\xff"
        if not (png if shot["type"] == "image/png" else jpeg):
            raise ValueError("The screenshot isn't the image it says it is")
        report["screenshot"] = {"type": shot["type"], "data": shot["data"]}
    diagnostics = body.get("diagnostics")
    if diagnostics is not None:
        before = len(MASKS.findall(diagnostics)) if isinstance(diagnostics, str) else 0
        report["diagnostics"] = bundle.check(diagnostics)
        masked += max(0, len(MASKS.findall(report["diagnostics"])) - before)
    return report, masked


def diagnostics(request):
    body = request.body or {}
    if set(body) - {"runId", "appVersion"}:
        raise ValueError("The diagnostics take runId and appVersion")
    run_id = body.get("runId")
    if run_id is not None and (not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{12}", run_id)):
        raise ValueError("runId is a task's id")
    version = body.get("appVersion")
    if version is not None and not isinstance(version, str):
        raise ValueError("appVersion is text")
    return Response.json(bundle.build(request.runtime, run_id=run_id, app_version=(version or "")[:80] or None))


def send(request):
    needs_app(request, APP_ONLY)
    body = _json(request)
    token = _token(body)
    try:
        report, masked = report_of(body)
        answer = outbox.send(request.runtime, report, token)
    finally:
        token = None
    return Response.json({**answer, "masked": masked}, 202 if answer["queued"] else 201)


def waiting(request):
    return Response.json({"items": outbox.outbox_for(request.runtime).public()})


def flush(request):
    needs_app(request, APP_ONLY)
    body = request.body or {}
    token = _token(body)
    if body:
        raise ValueError("Sending waiting reports takes only the account's accessToken")
    try:
        return Response.json(outbox.flush(request.runtime, token))
    finally:
        token = None


def delete(request):
    needs_app(request, APP_ONLY)
    draft = request.params["draft"]
    if not outbox.outbox_for(request.runtime).remove(draft):
        raise APIError("That report isn't waiting to send.", 404, "not_found")
    return Response.json({"deleted": draft})


def register_routes():
    for route in (Route("feedback", "POST", "/api/feedback/diagnostics", diagnostics),
                  Route("feedback", "POST", "/api/feedback", send, body="raw", max_bytes=MAX_BODY),
                  Route("feedback", "GET", "/api/feedback/outbox", waiting, body="none"),
                  Route("feedback", "POST", "/api/feedback/outbox/flush", flush),
                  Route("feedback", "DELETE",
                        "/api/feedback/outbox/(?P<draft>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                        delete, body="none")):
        register(route)
