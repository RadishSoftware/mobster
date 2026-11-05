"""Composition root: the driver for a run target, and best-effort shutdown."""

from . import harness_api
from .drivers import build_driver


def build_target_driver(*, wda_url=None, session=None, expected_bundle=None, connection=None, app_names=None,
                        labeller=None, icon_memory=None, **options):
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
    target_driver = harness_api.hooks().target_driver
    if options and target_driver is not None:
        return target_driver(expected_bundle=expected_bundle, **options)
    raise ValueError("A run needs --wda-url (and optionally --session)")


def _closer(client, name):
    try:
        return getattr(client, name, None)
    except Exception:
        return None


def close_all(*clients):
    for client in clients:
        if client is None:
            continue
        for closer in (_closer(client, "close"), _closer(_closer(client, "http"), "close")):
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
