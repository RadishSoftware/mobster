"""Track `accounts`'s registrations (tracks.load calls ``register(api)`` once per process, staged)."""

from .. import alerts
from . import context, routing, screen

TOOL_ORDER = 5


def tool(ctx):
    check = screen.check_for(ctx)
    return screen.CheckAccount(check) if check is not None else None


class Listener:
    """Forgets a finished run's profile check; counts each commit a run as a profile carried out (pacing's ledger);
    and pauses a profile whose run ended on an app's challenge (a captcha, "we suspect automated behavior", an ID
    check), with an alert, so nothing else runs as it until the person resumes it."""

    def on_step(self, run, record):
        account = (getattr(run, "extras", None) or {}).get("account")
        if not isinstance(account, dict) or not getattr(record, "commit", None):
            return
        pacing = _pacing(run)
        if pacing is not None:
            pacing.record(account["id"], record.commit, getattr(run, "id", None))

    def on_finished(self, run, summary, private):
        screen.forget(run.id)
        account = (getattr(run, "extras", None) or {}).get("account")
        if not isinstance(account, dict) or not isinstance(summary, dict):
            return
        from .pacing import looks_challenged
        data = summary.get("data") if isinstance(summary.get("data"), dict) else {}
        if (data.get("challenge") is True or summary.get("code") == "challenge" or (
                summary.get("status") != "completed"
                and looks_challenged(summary.get("reason"), summary.get("answer")))):
            reason = (f"{account.get('app', {}).get('name', 'The app')} showed @{account.get('handle')} a check "
                      "(a captcha, a verification or a warning about automated behavior).")
            pacing = _pacing(run)
            if pacing is not None:
                pacing.pause(account["id"], reason)
            alerts.Alerts().challenge(f"@{account.get('handle')} paused", reason, getattr(run, "id", None))


def _pacing(run):
    from .pacing import pacing_for
    runtime = _RUNTIME[0]
    if runtime is None:
        return None
    try:
        return pacing_for(runtime)
    except Exception:  # noqa: BLE001
        return None


_RUNTIME = [None]


def started(runtime):
    _RUNTIME[0] = runtime


def stopped(runtime):
    if _RUNTIME[0] is runtime:
        _RUNTIME[0] = None


def register(api):
    """``api`` is mobile_agent.harness_api."""
    from ..mcp_server import registry as mcp_registry
    from . import mcp, routes
    api.register_run_field("account", routing.validate, public=True)
    api.register_context_provider(context.provider, order=context.ORDER)
    api.register_tool(tool, order=TOOL_ORDER)
    api.register_run_listener(Listener())
    api.register_service(started, stopped)
    routes.register_routes()
    mcp_registry.register_provider(mcp.AccountsProvider())
