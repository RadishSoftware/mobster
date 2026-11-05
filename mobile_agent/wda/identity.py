"""Which phone answers on a WebDriverAgent port: checked before any session or action (DR-68).

Every Mobster puts its first phone on 8100, so a second Mobster on this Mac (a dev window, a QA app, the installed
app) can find another Mobster's phone answering where it expects its own. ``verify`` compares what the WebDriverAgent
reports (``/wda/device/info``: the phone's name and model, and its UDID when the helper reports one) with the phone this
Mobster means. A mismatch, or an answer that can't be read, refuses: nothing is sent to a phone that isn't confirmed.
A phone whose name Mobster doesn't know yet can't be compared, and is let through.
"""

from ..errors import MobsterError


class WrongDevice(MobsterError):
    """The helper answering on this port is another phone's."""


def _same(a, b):
    return " ".join(str(a or "").split()).casefold() == " ".join(str(b or "").split()).casefold()


def reported(url, timeout=3.0):
    """WebDriverAgent's own device info at ``url`` ({name, model, udid?}), or None when it can't be read."""
    from ..transport import HTTP
    client = None
    try:
        client = HTTP(url)
        value = client.request("GET", "/wda/device/info", timeout=timeout).get("value")
    except Exception:  # noqa: BLE001 -- unreadable is a refusal in verify
        return None
    finally:
        if client is not None:
            client.close()
    return value if isinstance(value, dict) else None


def verify(url, *, name=None, model=None, udid=None, read=None):
    """Raise WrongDevice unless the helper at ``url`` is the phone named by ``udid`` (when it reports one), else
    ``name`` (and ``model`` when both sides have one). Nothing known to compare: returns without reading."""
    if not (udid or name):
        return
    info = (read or reported)(url)
    if info is None:
        raise WrongDevice(f"Mobster couldn't confirm which iPhone answers at {url}, so it did nothing. Check that "
                          "the iPhone is unlocked and its helper is running, then try again.")
    seen_udid = info.get("udid") or info.get("UDID")
    if udid and seen_udid:
        ok = _same(seen_udid, udid)
    elif name:
        ok = _same(info.get("name"), name) and (not model or not info.get("model") or _same(info.get("model"), model))
    else:
        return
    if not ok:
        raise WrongDevice(f"The iPhone answering at {url} is “{str(info.get('name') or 'another iPhone')[:60]}”, not "
                          f"“{str(name or udid)[:60]}”. Another Mobster may be using that port. Mobster did nothing; "
                          "quit the other Mobster or restart this phone's helper, then try again.")
