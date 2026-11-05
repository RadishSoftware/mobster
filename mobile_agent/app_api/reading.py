"""One App API read, shared by REST (service.py) and MCP (mcp.py): the phone it reads, and the read itself."""

import threading
import time
from datetime import datetime, timezone

from ..packs import core
from ..packs import tools as pack_tools
from ..transport import TransportError

READ_SECONDS = min(40.0, pack_tools.READ_SECONDS)   # what an MCP pack read gets (packs/mcp.py)
NO_SERVICE = "Open Mobster or run `mobster serve` so Mobster can find your profiles."
SCALARS = (str, int, float, bool, type(None))
_gates = {}
_gates_lock = threading.Lock()


class Failure(Exception):
    """A read that didn't happen or stopped: ``status`` (HTTP), ``code`` and one plain message, what happened then what
    to do."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


class _IO(core.PackIO):
    """PackIO that notes each control it refused to tap (navigation reports a refusal as a plain Lost)."""

    def __init__(self, driver, pack, refusals, **options):
        super().__init__(driver, pack, **options)
        self.refusals = refusals

    def may_tap(self, label, opens=False, pattern=None):
        allowed = super().may_tap(label, opens, pattern)
        if not allowed:
            self.refusals.append(core.plain(label)[:60])
        return allowed


def service_profiles(origin):
    """The profiles the running Mobster has (Settings › Profiles), as GET /api/accounts lists them."""
    def load():
        from ..threads.client import Client, NoService, ServiceError
        try:
            return (Client.connect(origin=origin).call("GET", "/api/accounts") or {}).get("accounts") or []
        except (NoService, ServiceError):
            raise Failure(503, "no_service", NO_SERVICE) from None
    return load


def phone_for(endpoint, inputs, profiles):
    """The phone ``inputs`` names: ``phone``, or the phone ``profile`` is signed in on, or None (the default)."""
    phone, profile = inputs.get("phone"), inputs.get("profile")
    if phone and profile:
        raise Failure(400, "bad_input", "Pass phone or profile, not both.")
    if not profile:
        return phone
    from ..accounts.store import AccountNotFound, AmbiguousAccount, resolve
    accounts = [a for a in (profiles() if profiles else []) if isinstance(a, dict) and isinstance(a.get("app"), dict)
                and isinstance(a.get("handle"), str) and isinstance(a.get("id"), str)]
    pack = endpoint.pack
    try:
        account = resolve(accounts, profile, pack.bundle)
    except (AccountNotFound, AmbiguousAccount, ValueError) as error:
        raise Failure(404, "unknown_profile", str(error)) from None
    if account["app"].get("bundleId") != pack.bundle:
        raise Failure(400, "wrong_app", f"@{account['handle']} is a {account['app'].get('name')} profile, not "
                                        f"{pack.name}. Pass a {pack.name} profile.")
    return account.get("device") or account.get("deviceName") or None


def _gate(key):
    with _gates_lock:
        return _gates.setdefault(key, threading.Lock())


def claim(toolset, phone, guard=None):
    """(session, run, gate) for the phone ``phone`` names (None: the default), with the phone's read gate held: the
    caller releases it. One read at a time per phone: a second one is refused at once (409), never queued."""
    from ..mcp_server.tools import ToolError
    try:
        session = toolset._target_session({"device": phone} if phone else {})
    except ToolError as error:
        raise Failure(503, "no_phone", str(error)) from None
    run = session.run
    if guard is not None:
        guard(run)
    record = getattr(run, "record", None) or {}
    key = getattr(run, "device_id", None) or record.get("id") or str(getattr(run, "run_dir", "")) or id(run)
    gate = _gate(key)
    if not gate.acquire(blocking=False):
        raise Failure(409, "busy", "Another read is going on this phone. Try again when it returns.")
    return session, run, gate


def answer(endpoint, inputs, toolset, *, call, profiles=None, client_factory=None, guard=None, **options):
    """Read ``endpoint`` (a pack's, or the general screen endpoint) with ``inputs``: the output object, or Failure."""
    if endpoint.pack is None:
        from . import screen
        return screen.read(endpoint, inputs, toolset, call=call, guard=guard, **options,
                           **({"client_factory": client_factory} if client_factory else {}))
    return read(endpoint, inputs, toolset, call=call, profiles=profiles, guard=guard, **options)


def read(endpoint, inputs, toolset, *, call, profiles=None, guard=None, clock=time.monotonic, sleep=time.sleep,
         deadline=READ_SECONDS):
    """Read ``endpoint`` with ``inputs`` (already checked against its input schema): the output object, or Failure."""
    from ..mcp_server.action_journal import record_action
    from ..mcp_server.tools import Busy, ToolError
    pack, what = endpoint.pack, endpoint.thing
    n = inputs.get("n", endpoint.default_n)
    arg = core.plain(inputs.get("arg") or "") or None
    started = clock()
    session, run, gate = claim(toolset, phone_for(endpoint, inputs, profiles), guard)
    record = getattr(run, "record", None) or {}
    refusals = []    # navigation turns a refusal into a plain Lost: these say a tap was refused

    def permit(op, element, snapshot, text):
        if op in ("LAUNCH_APP", "OPEN_URL"):
            return  # the app was permitted below; a pack's links open only its own app
        try:
            if toolset._permit(session, op, None, element, snapshot, call, text=text):
                toolset._rebind(session, None, snapshot, call)
        except ToolError as refused:
            refusals.append(str(refused))
            raise core.Refused(str(refused)) from None

    title = endpoint.title
    try:
        with toolset._locked(session), record_action(run.run_dir, endpoint.name.upper()):
            if toolset._real_phone(session) is not None:
                try:
                    toolset._permit_app(session, pack.bundle)
                except ToolError as refused:
                    raise Failure(403, "app_not_allowed", str(refused)) from None
            io = _IO(run.driver, pack, refusals, permit=permit,
                     cancelled=lambda: toolset._action_cancelled(session, call),
                     sleep=sleep, clock=clock, deadline=deadline)
            try:
                result = pack.read(io, what, n, arg)
            finally:
                session.outline, session.anchor = None, None   # the screen moved: refs come from a new `screen`
    except Busy as busy:
        raise Failure(409, "busy", str(busy)) from None
    except core.Lost as lost:
        said = str(lost).rstrip(".")
        if isinstance(lost, core.Refused) or refusals:
            raise Failure(403, "refused", f"Mobster stopped reading {title} in {pack.name}: {said}. Nothing else "
                                          "was tapped.") from None
        if said == "it ran out of time":
            raise Failure(504, "timeout", f"Reading {title} in {pack.name} took longer than {int(deadline)} "
                                          "seconds. Try again with a smaller n.") from None
        raise Failure(422, "couldnt_read", f"Mobster couldn't read {title} in {pack.name}: {said}. Check the phone "
                                           f"shows {pack.name}, then try again.") from None
    except ToolError as error:
        raise Failure(503, "no_phone", str(error)) from None
    except (TransportError, TimeoutError) as error:
        # The phone stopped answering mid-read (a tree too slow to read, then no picture either): an answer, never a
        # dropped connection. Nothing is read again or retried.
        raise Failure(502, "phone_error", f"Mobster couldn't read {title} in {pack.name}: the phone didn't answer "
                                          f"({str(error).rstrip('.')}). Nothing was retried. Check the phone, then "
                                          "try again.") from None
    finally:
        gate.release()
    return {"app": pack.key, "reader": what, "title": result.title, "n": n,
            "columns": [{"key": k, "label": label, "type": kind} for k, label, kind in result.columns],
            "rows": [{k: v for k, v in row.items() if isinstance(v, SCALARS)} for row in result.rows],
            "stopped": result.stopped or None, "screen": result.screen,
            "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "phone": getattr(run, "device_name", None) or record.get("name"),
            "took_ms": round((clock() - started) * 1000)}
