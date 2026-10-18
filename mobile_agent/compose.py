"""Composition root: the driver for a run target, and best-effort shutdown."""

from .drivers import build_driver
from .extensions import load as load_extensions


def build_target_driver(*, wda_url=None, session=None, expected_bundle=None, connection=None, app_names=None,
                        labeller=None, icon_memory=None, **options):
    """Select and construct the driver for one run target. No probing, no fallback.

    WDA sessions get one configuration call (measured snapshot settings)
    before the driver is returned. ``connection="mirroring"`` is the experimental
    iPhone Mirroring driver (mirroring.py) and ``connection="cable_bt"`` the experimental
    Cable + Bluetooth one (cable_bt.py, ``options``: its phone), each only with its setting on. Other
    targets (``options``) exist only when an extension provides them (``extensions.py``).
    """
    if connection == "mirroring":
        from . import mirroring
        if not mirroring.enabled():
            raise ValueError(f"The iPhone Mirroring connection is experimental: set {mirroring.ENV_FLAG}=1 to use it")
        return build_driver("mirroring", app_names=app_names, labeller=labeller, icon_memory=icon_memory)
    if connection == "cable_bt":
        from . import cable_bt
        if not cable_bt.enabled():
            raise ValueError(f"The Cable + Bluetooth connection is experimental: set {cable_bt.ENV_FLAG}=1 to use it")
        return build_driver("cable_bt", app_names=app_names, labeller=labeller, icon_memory=icon_memory, **options)
    if connection not in (None, "wda"):
        raise ValueError(f"Unknown connection {connection!r}")
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
