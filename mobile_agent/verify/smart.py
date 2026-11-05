import os
import re

NO_KEY_FIX = ("Set OPENAI_API_KEY or ANTHROPIC_API_KEY for Smart, or let your coding agent drive through "
              "`mobster mcp`.")


def no_smart_key(env=None):
    """(why, fix) when Smart has no key for its model."""
    from .. import engines
    env = os.environ if env is None else env
    model = engines.smart_model(env)
    from ..ai_plans import providers
    routed = providers.get(engines.model_provider(model))
    if routed is not None:
        return (f"{routed.name} has no key or sign-in here", f"Run `mobster login {routed.id}`, or `mobster login` "
                "for an API key.")
    if (env.get("MOBSTER_SMART_MODEL") or "").strip() != model:
        return "no OpenAI or Anthropic key is set", NO_KEY_FIX
    variable = "ANTHROPIC_API_KEY" if engines.model_provider(model) == "anthropic" else "OPENAI_API_KEY"
    return (f"MOBSTER_SMART_MODEL is {model}, which needs {variable}, and it isn't set",
            f"Set {variable} for Smart on {model}, or let your coding agent drive through `mobster mcp`.")


# Acts that name no commit: none at all, the replay's "act" for a step it couldn't read, and contract_screen's "none".
UNCLASSIFIED_ACTS = frozenset({"", "act", "none"})


def approve_on_simulator(request):
    """Smart's approver on a simulator: the app under test may buy, send, post and delete, because money and people
    don't exist there. A commit the frontier could not classify is refused. The simulator's dialogs, including the
    purchase sheet, answer to the check's ``dialogs``."""
    act = request.get("act") if isinstance(request, dict) else None
    if not isinstance(act, str) or act.strip().casefold() in UNCLASSIFIED_ACTS:
        return "denied"
    return "approved"


def deny_commits(request):
    """Smart's approver in a check on a real device (a USB iPhone, a WDA address): every declared commit is refused."""
    # The phone is someone's own: a send, delete, post or purchase there is real, and a check never asks first.
    return "denied"


def is_real_device(manager):
    """Whether ``manager`` drives someone's phone. Fails closed: only ``real_device = False`` is a simulator."""
    return getattr(manager, "real_device", None) is not False


def check_approver(manager):
    return deny_commits if is_real_device(manager) else approve_on_simulator


# System view services an app presents over itself (a share sheet, Sign in with Apple, a purchase sheet): part of
# the app under test, not another app. SpringBoard (alerts, the Home Screen) the frontier never stops for.
VIEW_SERVICE = re.compile(r"com\.apple\.[A-Za-z0-9.]*(?:ViewService|UIService)")
LEFT_APP = ("On your iPhone a check stays in {app}, and Smart opened {other}, so the check stopped there. Nothing "
            "was read from {other} for the report.")
LEFT_APP_FIX = "Keep the check's steps inside the app, or run it on a simulator with --sim."


class StayInApp:
    """The phone guard for a Smart check on a real device: only the app under test, so no step reads their data."""

    def __init__(self, bundle, inner=None):
        self.bundle, self.inner, self.left = bundle, inner, None

    def check(self, driver, *, cause):
        return self.inner.check(driver, cause=cause) if self.inner is not None else None

    def attached(self):
        return self.inner.attached() if self.inner is not None else None

    def allows_app(self, bundle_id):
        if not bundle_id or VIEW_SERVICE.fullmatch(bundle_id):
            return True
        if bundle_id == self.bundle:  # the user's own list of apps Mobster never opens still applies
            allows = getattr(self.inner, "allows_app", None)
            return bool(allows(bundle_id)) if callable(allows) else True
        self.left = bundle_id
        return False

    def finish(self, driver):
        if self.inner is not None:
            self.inner.finish(driver)


def check_guard(manager, bundle):
    if not is_real_device(manager):
        return None
    record = getattr(manager, "record", None) or {}
    inner = None
    try:
        from ..lockscreen import guard_for
        inner = guard_for(device_id=record.get("udid") or record.get("id"), wda_url=record.get("wdaUrl"),
                          mode="scripts", usb=record.get("kind") == "usb")
    except Exception:
        inner = None  # no lock detection: the app restriction still holds
    return StayInApp(bundle, inner)


def smart_request(app_name, steps):
    """What Smart is asked: "In <App>: <the step>", or the steps numbered."""
    if len(steps) == 1:
        return f"In {app_name}: {steps[0]}"
    return f"In {app_name}: " + " ".join(f"{index}. {step}" for index, step in enumerate(steps, 1))
