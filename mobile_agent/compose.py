"""Composition root: one place that wires drivers, models, helpers, and readers.

The serve worker and the run command used to duplicate target selection, client
construction, and shutdown. They now share these builders. Phased flows (launch
before model construction) still sequence the pieces themselves.
"""

from .drivers import build_driver
from .extensions import load as load_extensions
from .models import Helper, Jev


def build_target_driver(*, wda_url=None, session=None, expected_bundle=None, **options):
    """Select and construct the driver for one run target. No probing, no fallback.

    WDA sessions get one configuration call (measured snapshot settings)
    before the driver is returned. Other targets (``options``) exist only when
    an extension provides them (``extensions.py``).
    """
    if wda_url:
        driver = build_driver("wda", url=wda_url, session=session)
        driver.configure()
        return driver
    options = {key: value for key, value in options.items() if value is not None}
    hooks = load_extensions()
    if options and hooks.target_driver is not None:
        return hooks.target_driver(expected_bundle=expected_bundle, **options)
    raise ValueError("A run needs --wda-url (and optionally --session)")


def build_models(*, helper=False, helper_model=None, on_inference=None):
    """Jev plus the optional text helper. Unconfigured clients raise their own errors."""
    return Jev(on_inference=on_inference), (
        Helper(on_inference=on_inference, model=helper_model) if helper else None)


def build_visual(*, enabled=False, driver=None, cancelled=None, **options):
    """Opt-in screen reader, or None. Never inferred from an empty AX tree.

    The core has none: WDA runs read the accessibility tree only. An extension's
    target may provide one (``extensions.Hooks.screen_reader``).
    """
    if not enabled:
        return None
    hooks = load_extensions()
    if hooks.screen_reader is None:
        return None
    return hooks.screen_reader(driver, {k: v for k, v in options.items() if v is not None}, cancelled)


def build_vision_judge(*, enabled=False, on_inference=None):
    """The image judge for compiled loops, or None. Needs the helper model's keys."""
    if not enabled:
        return None
    try:
        from .vision_judge import VisionJudge
        return VisionJudge(on_inference=on_inference)
    except Exception:
        return None


def warm_clients(model=None, helper=None, judge=None, *, jev_connections=3):
    """Pre-open model connections (and the gcloud token) off the caller's thread. Never raises.

    Jev gets one connection for decisions plus the speculation, prediction and
    answer-prefetch side channels; the helper's first call of a run was +455 ms
    cold, mostly the token (latency breakdown of 23 Sep 2026, item 3).
    """
    import threading

    def run():
        for client, args in ((model, (jev_connections,)), (helper, ()), (judge, ())):
            warm = getattr(client, "warm", None) if client is not None else None
            if not callable(warm):
                continue
            try:
                try:
                    warm(*args)
                except TypeError:
                    warm()
            except Exception:
                pass  # Warming is an optimization; the first real call connects itself.
    thread = threading.Thread(target=run, name="mobster-warm", daemon=True)
    thread.start()
    return thread


def _closer(client, name):
    try:
        return getattr(client, name, None)
    except Exception:
        return None


def close_all(*clients):
    """Best-effort shutdown for drivers, models, and helpers. Never raises."""
    for client in clients:
        if client is None:
            continue
        for closer in (_closer(client, "close"), _closer(_closer(client, "http"), "close")):
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
