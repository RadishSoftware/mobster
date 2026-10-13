"""Composition root: the driver for a run target, and best-effort shutdown."""

from .drivers import build_driver
from .extensions import load as load_extensions


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


def _closer(client, name):
    try:
        return getattr(client, name, None)
    except Exception:
        return None


def close_all(*clients):
    """Best-effort shutdown for drivers and model clients. Never raises."""
    for client in clients:
        if client is None:
            continue
        for closer in (_closer(client, "close"), _closer(_closer(client, "http"), "close")):
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
