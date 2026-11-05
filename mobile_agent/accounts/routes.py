"""The profiles API (Settings › Profiles), registered under /api/accounts (api_routes, seam S3)."""

import re

from .. import harness_api
from ..api_errors import APIError
from ..api_routes import Request, Response, Route, register
from .store import AccountNotFound, AmbiguousAccount, display, resolve, store_for

KEY = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
MAX_RUN_ACCOUNTS = 20
RUN_FIELDS = frozenset({"accounts", "goal", "appId", "engine", "outputFormat"})
NOT_HERE = "Change your profiles in Mobster on your Mac, in Settings › Profiles."


def _writable(request):
    if request.origin in ("remote", "mcp"):
        raise APIError(NOT_HERE, 403, "not_here")


def _device(runtime, value):
    """(id, name) of the phone ``value`` names, or None for null. ValueError when no phone has that name."""
    if value is None:
        return None
    fleet = getattr(runtime, "fleet", None)
    if fleet is None:
        raise ValueError("This Mobster runs every task on one phone. Leave the profile's phone out.")
    try:
        phone = fleet.find(value)
    except LookupError as error:
        raise ValueError(str(error)) from None
    return phone.id, phone.name


def public(account):
    return {**account, "display": display(account)}


def list_accounts(request: Request):
    return Response.json({"accounts": [public(a) for a in store_for(request.runtime).all()]})


def _remember(account):
    """A saved profile's voice and signature are memory facts, edited where the profile is (memory/writer.py)."""
    from ..memory import writer
    writer.from_profile(account)


def add_account(request: Request):
    _writable(request)
    body = request.body or {}
    device = _device(request.runtime, body.get("device")) if isinstance(body, dict) else None
    account = store_for(request.runtime).add(body, device)
    _remember(account)
    return Response.json({"account": public(account)}, 201)


def edit_account(request: Request):
    _writable(request)
    body = request.body or {}
    device = _device(request.runtime, body.get("device")) if isinstance(body, dict) and "device" in body else None
    try:
        account = store_for(request.runtime).update(request.params["id"], body, device)
    except AccountNotFound as error:
        raise APIError(str(error), 404, "account_not_found") from None
    _remember(account)
    return Response.json({"account": public(account)})


def delete_account(request: Request):
    _writable(request)
    try:
        store_for(request.runtime).remove(request.params["id"])
    except AccountNotFound as error:
        raise APIError(str(error), 404, "account_not_found") from None
    from ..memory import writer
    writer.profile_removed(request.params["id"])
    return Response.json({"deleted": True})


def _key(request):
    headers = request.headers
    values = headers.get_all("Idempotency-Key", []) if hasattr(headers, "get_all") else (
        [headers["Idempotency-Key"]] if headers.get("Idempotency-Key") else [])
    if not values:
        return None
    if len(values) > 1 or not KEY.fullmatch(values[0]):
        raise ValueError("Invalid Idempotency-Key")
    return values[0]


def run_accounts(request: Request):
    """One task per account."""
    runtime, body = request.runtime, request.body or {}
    if not isinstance(body, dict) or set(body) - RUN_FIELDS:
        raise ValueError("A task for several profiles has accounts, goal, and optionally appId, engine and "
                         "outputFormat")
    names = body.get("accounts")
    if not isinstance(names, list) or not 1 <= len(names) <= MAX_RUN_ACCOUNTS:
        raise ValueError(f"accounts is a list of 1 to {MAX_RUN_ACCOUNTS} profiles")
    stored = store_for(runtime).all()
    chosen = []
    for name in names:
        try:
            account = resolve(stored, name)
        except (AccountNotFound, AmbiguousAccount) as error:
            raise APIError(str(error), 404 if isinstance(error, AccountNotFound) else 400,
                           "account_not_found" if isinstance(error, AccountNotFound) else "account_ambiguous") from None
        if all(account["id"] != other["id"] for other in chosen):
            chosen.append(account)
    by_phone = {}
    for account in chosen:
        by_phone.setdefault(account.get("device") or "", []).append(account)
    for together in by_phone.values():
        if len(together) > 1:
            where = together[0].get("deviceName") or "your default phone"
            listed = " and ".join(display(a) for a in together)
            raise APIError(f"{listed} are both on {where}, and a phone runs one task at a time. Run them one after "
                           "the other, or move one profile to another phone.", 409, "same_phone")
    key = _key(request)
    origin = request.origin if request.origin in harness_api.ORIGINS else "api"
    rows = []
    for account in chosen:
        try:
            created = runtime.create(body.get("appId"), body.get("goal"), "live",
                                     f"{key}:{account['id']}" if key else None,
                                     output_format=body.get("outputFormat", "auto"), engine=body.get("engine"),
                                     extras={"account": account["id"]}, origin=origin)
        except APIError as error:
            rows.append({"account": account["id"], "display": display(account), "error": str(error),
                         "code": error.code})
            continue
        except ValueError as error:
            if len(chosen) == 1:
                raise
            rows.append({"account": account["id"], "display": display(account), "error": str(error),
                         "code": "invalid"})
            continue
        run = created[0] if isinstance(created, tuple) else created
        rows.append({"account": account["id"], "display": display(account), "run": run.public(include_events=False)})
    started = any("run" in row for row in rows)
    return Response.json({"runs": rows}, 201 if started else 409)


def _pacing_answer(request, profile):
    from .pacing import pacing_for
    pacing = pacing_for(request.runtime)
    return {**pacing.settings(profile), "today": pacing.today(profile)}


def _known(request):
    profile = request.params["id"]
    if store_for(request.runtime).get(profile) is None:
        raise APIError("No profile has that id. Add it in Settings › Profiles.", 404, "account_not_found")
    return profile


def get_pacing(request):
    return Response.json({"pacing": _pacing_answer(request, _known(request))})


def save_pacing(request):
    from .pacing import PacingError, pacing_for
    _writable(request)
    profile = _known(request)
    try:
        pacing_for(request.runtime).save(profile, request.body or {})
    except PacingError as error:
        raise APIError(str(error), 400, "invalid") from None
    return Response.json({"pacing": _pacing_answer(request, profile)})


def resume_profile(request):
    from .pacing import pacing_for
    _writable(request)
    profile = _known(request)
    pacing_for(request.runtime).resume(profile)
    return Response.json({"pacing": _pacing_answer(request, profile)})


def get_stats(request):
    from ..packs.register import all_packs
    from . import stats
    app = request.params["app"]
    if app not in {pack.key for pack in all_packs()}:
        raise APIError(f"There is no {app} pack. Use one of: " + ", ".join(p.key for p in all_packs()) + ".", 404,
                       "stats_unknown_app")
    try:
        days = int((request.query.get("days") or ["30"])[0])
    except ValueError:
        days = 0
    if not 1 <= days <= 365:
        raise APIError("days is a number from 1 to 365.", 400, "invalid")
    return Response.json(stats.series(store_for(request.runtime).data_dir, app, request.params["handle"], days))


ROUTES = (
    Route("accounts", "GET", "/api/accounts", list_accounts, body="none"),
    Route("accounts", "POST", "/api/accounts", add_account),
    Route("accounts", "POST", "/api/accounts/runs", run_accounts, idempotency=True),
    Route("accounts", "POST", "/api/accounts/{id}", edit_account),
    Route("accounts", "GET", "/api/accounts/{id}/pacing", get_pacing, body="none"),
    Route("accounts", "POST", "/api/accounts/{id}/pacing", save_pacing),
    Route("accounts", "POST", "/api/accounts/{id}/resume", resume_profile, body="none"),
    Route("accounts", "GET", "/api/accounts/{app}/{handle}/stats", get_stats, body="none"),
    Route("accounts", "DELETE", "/api/accounts/{id}", delete_account, body="none"),
)


def register_routes():
    for route in ROUTES:
        register(route)
