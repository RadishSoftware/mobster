"""Which phone to capture, and which apps it has. Simulators have no pcapd service, so they are refused."""

from dataclasses import dataclass

from . import attribution


class TargetError(Exception):
    """The phone can't be captured; the message says why and what to do."""


@dataclass(frozen=True)
class Phone:
    id: str
    name: str
    udid: str
    network: bool        # capture over Wi-Fi (mobster-pcap --network), not the cable


def on_cable(record):
    """Whether ``record`` (devices.discover) is on a cable now."""
    if "transport" in record:
        return record["transport"] == "usb"
    return record.get("state") != "disconnected"


def pick(query=None, *, wifi=False, records=None):
    """The Phone to capture."""
    from .. import devices
    records = devices.discover(probe=False) if records is None else records
    if query:
        try:
            record = devices.resolve(records, query)
        except LookupError as error:
            raise TargetError(str(error)) from None
        if record.get("kind") == "simulator":
            raise TargetError(f"“{record['name']}” is a simulator. Network X-ray reads a real iPhone's traffic: "
                              "name an iPhone.")
        if record.get("kind") != "usb" or not record.get("udid"):
            raise TargetError(f"“{record['name']}” isn't an iPhone Mobster set up. Network X-ray needs the phone's "
                              "UDID: plug it in once with a cable.")
    else:
        phones = [item for item in records
                  if item.get("kind") == "usb" and item.get("udid") and (wifi or on_cable(item))]
        if not phones:
            raise TargetError("Mobster sees no iPhone. Plug one in, or name one with --device.")
        if len(phones) > 1:
            raise TargetError(f"{len(phones)} iPhones are connected, so Mobster won't guess which one to capture. "
                              "Name one with --device: " + ", ".join(f"{item['name']} ({item['id']})"
                                                                     for item in phones[:8]) + ".")
        record = phones[0]
    return Phone(record["id"], record.get("name") or record["id"], record["udid"], bool(wifi or not on_cable(record)))


def phone_names(phone, *, tool=None, runner=None):
    """attribution.Names for ``phone``, from the apps it has installed (user and system, with their executables)."""
    from .. import installed_apps
    from ..mac_tools import tool as find_tool
    kwargs = {"runner": runner} if runner else {}
    binary = tool or find_tool("ideviceinstaller")
    rows = []
    for kind in ("user", "system"):
        try:
            rows += installed_apps.list_apps(binary, phone.udid, kind, network=phone.network, **kwargs)
        except installed_apps.ListingError:
            pass
    return attribution.Names(attribution.executables_from(rows))
