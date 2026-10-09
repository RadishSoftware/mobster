"""Every phone Mobster can drive, their ports, and naming one (SPEC §3, docs/devices.md).

A device is a USB iPhone (``usb``), one of Mobster's simulators (``simulator``) or a WebDriverAgent given by
address (``wda``). Each has its own WebDriverAgent and its own device lease (``journal.Lease.device``, keyed by
the WDA address), so tasks on two devices never meet.

USB ports. The primary phone, the one chosen in Setup (``device.json``), keeps slot 0: 8100 and 9100, where it
always was. Every other phone gets the lowest free slot N in 1..99, WDA on 8100+N and its MJPEG stream on
9100+N, kept in ``devices.json`` in the data folder so the phone keeps its ports across restarts and the CLI
finds them without the server. A slot is free when no phone holds it and nothing listens on either port.
Simulators keep the ports the simulator registry gave them (8310+N).

This module only reads the machine (USB listing, the registries, a WDA /status, lease probes); the server's
side, which starts runners and streams, is ``fleet.py``.
"""

import contextlib
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import time
from urllib.parse import urlsplit

KINDS = ("usb", "simulator", "wda")
STATES = ("ready", "busy", "connected", "needs_setup", "disconnected")
STORE = "devices.json"
STORE_LOCK = "devices.lock"
LOCK_TIMEOUT = 10.0
WDA_BASE, MJPEG_BASE = 8100, 9100
FIRST_SLOT, LAST_SLOT = 1, 99
LOOPBACK = "127.0.0.1"
ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
UDID_PATTERN = re.compile(r"[0-9A-Fa-f-]{24,40}")

# The one sentence each state says, when nothing more precise is known.
REASONS = {
    # Plain words: the app shows these under a phone's name (dashboard/src/lib/devices.ts), and the CLI's
    # `mobster devices` prints them. "Mobster's helper" is the on-phone runner (WebDriverAgent).
    "busy": "A task is running on it.",
    "busy_elsewhere": "Another Mobster app or command is using it.",
    "untrusted": "Unlock it and tap Trust on it.",
    "not_built": "Set it up once. Mobster walks you through it.",
    "built_for_other": "Set it up again: Mobster's helper on it was made for another iPhone.",
    "expired": "It needs a quick refresh. Free Apple accounts ask for this every 7 days.",
    "starting": "Mobster's helper on it isn't answering yet. Keep it unlocked and connected.",
    "stopped": "Mobster's helper isn't running on it. Start it again to see the screen.",
    "unplugged": "It's unplugged. Plug it into this Mac with a cable.",
    "sim_off": "The simulator is shut down. Start it to use it.",
    "sim_no_wda": "The simulator is on, but Mobster's helper isn't running on it. Start it to use it.",
    "wda_down": "Nothing answers at its address.",
    "locked": "Unlock it to run tasks and see its screen.",
    # Only with Wi-Fi for iPhones on (MOBSTER_WIFI_TRANSPORT) and Wi-Fi turned on for the phone (wireless/).
    "wifi_unreachable": "It isn't reachable over Wi-Fi. Keep it unlocked and on the same Wi-Fi as this Mac, or plug "
                        "it in.",
}


class DeviceNotFound(LookupError):
    """No device has that id, UDID or name."""


class AmbiguousDevice(LookupError):
    """More than one device has that name."""


def wda_url(port):
    return f"http://{LOOPBACK}:{int(port)}"


def port_of(url):
    try:
        return urlsplit(url or "").port
    except ValueError:
        return None


def slot_ports(slot):
    """(WDA port, MJPEG port) of a USB slot: 8100+N and 9100+N."""
    return WDA_BASE + slot, MJPEG_BASE + slot


def listening(port, host=LOOPBACK, timeout=.2):
    """Whether something accepts connections on this Mac's loopback at ``port``."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def allocate_slot(entries, busy=listening):
    """The lowest slot in 1..99 no entry holds and whose two ports nothing listens on. LookupError when none is."""
    taken = {int(entry["slot"]) for entry in entries if isinstance(entry.get("slot"), int)}
    for slot in range(FIRST_SLOT, LAST_SLOT + 1):
        if slot in taken:
            continue
        wda, mjpeg = slot_ports(slot)
        if busy(wda) or busy(mjpeg):
            continue  # another program's port: skipped, never taken over
        return slot
    raise LookupError(f"Every port from {WDA_BASE + FIRST_SLOT} to {WDA_BASE + LAST_SLOT} is taken, so Mobster "
                      "has no room for another iPhone. Forget a phone you no longer use.")


def device_id_for_url(url):
    """The id of a device known only by its WDA address: wda-<port>."""
    port = port_of(url)
    return f"wda-{port}" if port else "wda"


# -- devices.json --------------------------------------------------------------------------------------------

class DeviceStore:
    """devices.json in the data folder: the USB phones set up besides the primary one, with their slots, and the
    device the last task ran on. Every edit is a read, change and write under an fcntl lock with an atomic
    rename, so the server and the CLI never lose each other's edits; a damaged file reads as empty and is
    rewritten on the next edit (it holds no secret and nothing that can't be set up again)."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / STORE
        self.lock_path = self.data_dir / STORE_LOCK

    def read(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"version": 1, "phones": [], "lastUsed": None}
        phones = [entry for entry in data.get("phones") or [] if isinstance(entry, dict)
                  and isinstance(entry.get("udid"), str) and UDID_PATTERN.fullmatch(entry["udid"])
                  and isinstance(entry.get("slot"), int) and FIRST_SLOT <= entry["slot"] <= LAST_SLOT] \
            if isinstance(data, dict) else []
        last = data.get("lastUsed") if isinstance(data, dict) else None
        return {"version": 1, "phones": phones,
                "lastUsed": last if isinstance(last, str) and ID_PATTERN.fullmatch(last) else None}

    def phones(self):
        return self.read()["phones"]

    @contextlib.contextmanager
    def edit(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            end = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) or time.monotonic() >= end:
                        raise OSError("Another Mobster process is holding the device list") from None
                    time.sleep(.05)
            data = self.read()
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                self._write(data)
        finally:
            os.close(fd)

    def _write(self, data):
        fd, temporary = tempfile.mkstemp(prefix=".devices.", suffix=".json", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps(data, indent=1) + "\n")
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def add(self, udid, name=None, busy=listening):
        """The phone's entry, allocating its slot the first time."""
        with self.edit() as data:
            entry = next((item for item in data["phones"] if item["udid"] == udid), None)
            if entry is None:
                slot = allocate_slot(data["phones"], busy)
                wda, mjpeg = slot_ports(slot)
                entry = {"udid": udid, "name": name, "slot": slot, "wdaPort": wda, "mjpegPort": mjpeg,
                         "addedAt": round(time.time() * 1000)}
                data["phones"].append(entry)
            elif name and entry.get("name") != name:
                entry["name"] = name
            return dict(entry)

    def remove(self, udid):
        with self.edit() as data:
            data["phones"] = [item for item in data["phones"] if item["udid"] != udid]
            if data.get("lastUsed") == udid:
                data["lastUsed"] = None

    def set_last_used(self, device_id):
        with self.edit() as data:
            data["lastUsed"] = device_id

    def set_wifi(self, udid, value):
        """Set (a dict) or clear (None) the phone's ``wifi`` (wireless/store.py). LookupError when it has no entry."""
        with self.edit() as data:
            entry = next((item for item in data["phones"] if item["udid"].upper() == str(udid).upper()), None)
            if entry is None:
                raise LookupError("not_set_up")
            if value is None:
                entry.pop("wifi", None)
            else:
                entry["wifi"] = dict(value)


def phone_folder(data_dir, udid):
    """Where a phone other than the primary keeps its runner build, settings and logs."""
    return Path(data_dir) / "devices" / udid


# -- reading the machine ---------------------------------------------------------------------------------------

def record(*, id, kind, name, wda_url_value, udid=None, model=None, model_name=None, ios=None, state="disconnected",
           reason=None, mjpeg_url=None, primary=False, set_up=True):
    """One device as every surface shows it (the server adds activeRunId, approval and default)."""
    if state not in STATES:
        raise ValueError(f"unknown device state {state}")
    return {"id": id, "kind": kind, "name": name or ("iPhone" if kind == "usb" else id), "udid": udid,
            "model": model, "modelName": model_name, "ios": ios, "state": state,
            "reason": None if state == "ready" else (reason or None), "primary": bool(primary),
            "setUp": bool(set_up), "wdaUrl": wda_url_value, "mjpegUrl": mjpeg_url}


def usb_state(device, manager, wda_ready, lease_free, locked=None):
    """(state, reason) of a USB phone. ``device`` is its USB listing entry (None when unplugged); ``manager`` its
    DeviceManager; ``wda_ready()`` whether its WDA answers; ``lease_free()`` whether no Mobster process holds it;
    ``locked`` True when WDA answers but can't reach the UI."""
    if device is None:
        return "disconnected", REASONS["unplugged"]
    if not device.get("trusted"):
        return "needs_setup", REASONS["untrusted"]
    if manager is None:
        return "needs_setup", REASONS["not_built"]
    settings = manager.settings()
    built_for = settings.get("built_for", settings.get("udid"))
    if manager.expired():
        return "needs_setup", REASONS["expired"]
    if not manager.built():
        if built_for and built_for != device.get("udid"):
            return "needs_setup", REASONS["built_for_other"]
        return "needs_setup", REASONS["not_built"]
    if not wda_ready():
        running = manager.runner is not None and manager.runner.running
        return "connected", REASONS["starting"] if running else REASONS["stopped"]
    if not lease_free():
        return "busy", REASONS["busy_elsewhere"]
    if locked:
        return "connected", REASONS["locked"]
    return "ready", None


def wda_answers(url, timeout=1.0):
    """WDA's /status says ready."""
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/status", timeout=timeout) as response:
            return json.load(response).get("value", {}).get("ready") is True
    except Exception:
        return False


def lease_free(url):
    """Whether no Mobster process holds this device's lease (taken and released at once, never waited for)."""
    from .journal import JournalError, Lease, LeaseHeld
    try:
        lease = Lease.device(url, wait=0)
    except LeaseHeld:
        return False
    except JournalError:
        return True  # an unusable lease folder guards nothing; the run says so when it starts
    lease.close()
    return True


def simulator_folder():
    from .paths import dev_data_dir
    return dev_data_dir()


def simulator_rows(data_dir=None, manager=None):
    """Mobster's simulators from the simulator registry with simctl's state for each: [] when there are none
    (no simctl call then) or this build has no simulator manager."""
    try:
        from .sim.registry import Registry
    except ImportError:
        return []
    folder = Path(data_dir) if data_dir else simulator_folder()
    if not (folder / "simulators.json").exists():
        return []  # never runs simctl on a Mac with no Mobster simulator
    try:
        entries = Registry(folder).read()
    except Exception:
        return []
    states = {}
    try:
        if manager is None:
            from .sim import SimulatorManager
            manager = SimulatorManager(data_dir=folder)
        states = {item.udid: item.state for item in manager._devices()}
    except Exception:
        pass
    return [{"udid": entry["udid"], "name": entry.get("name"), "device_type": entry.get("device_type"),
             "runtime": entry.get("runtime"), "state": states.get(entry["udid"], "Missing" if states else "Unknown"),
             "wda_url": wda_url(entry["wda_port"]), "mjpeg_url": wda_url(entry["mjpeg_port"])}
            for entry in entries if entry.get("wda_port") and entry.get("mjpeg_port")]


def simulator_record(row, *, ready=None, free=None, busy_here=False):
    """One simulator row (simulator_rows) as a device. ``ready``/``free`` default to probing its WDA and lease."""
    booted = row.get("state") == "Booted"
    if busy_here:
        state, reason = "busy", REASONS["busy"]
    elif not booted:
        state, reason = "disconnected", REASONS["sim_off"]
    elif not (wda_answers(row["wda_url"]) if ready is None else ready):
        state, reason = "connected", REASONS["sim_no_wda"]
    elif not (lease_free(row["wda_url"]) if free is None else free):
        state, reason = "busy", REASONS["busy_elsewhere"]
    else:
        state, reason = "ready", None
    runtime = str(row.get("runtime") or "")
    return record(id=row["udid"], kind="simulator", name=row.get("name"), udid=row["udid"],
                  model=row.get("device_type"), model_name=row.get("device_type"),
                  ios=runtime.removeprefix("iOS ").strip() or None, state=state, reason=reason,
                  wda_url_value=row["wda_url"], mjpeg_url=row.get("mjpeg_url"))


def simulator_records(data_dir=None, manager=None, probe=True):
    return [simulator_record(row, **({} if probe else {"ready": True, "free": True}))
            for row in simulator_rows(data_dir, manager)]


def static_specs(config=None):
    """WDA devices given by address: ``MOBSTER_WDA_DEVICES`` (pool.wda_specs_from_env) and ``config.devices``."""
    from .pool import wda_specs_from_config
    try:
        return wda_specs_from_config(config if config is not None else type("Config", (), {"devices": None})())
    except ValueError:
        return []


def static_record(url, *, id=None, name=None, mjpeg_url=None, ready=None, free=None):
    ready = wda_answers(url) if ready is None else ready
    if not ready:
        state, reason = "disconnected", REASONS["wda_down"]
    elif not (lease_free(url) if free is None else free):
        state, reason = "busy", REASONS["busy_elsewhere"]
    else:
        state, reason = "ready", None
    return record(id=id or device_id_for_url(url), kind="wda", name=name or f"WebDriverAgent on port {port_of(url)}",
                  state=state, reason=reason, wda_url_value=url.rstrip("/"), mjpeg_url=mjpeg_url)


def discover(data_dir=None, *, sims=True, extra_urls=(), probe=True):
    """Every device, read without the server (`mobster devices`, `--device`, MCP's list_devices): the primary USB
    phone, the other phones set up, phones plugged in but not set up, Mobster's simulators, and WDA addresses
    from MOBSTER_WDA_DEVICES and ``extra_urls``. Busy means another process holds the device's lease.
    ``probe=False`` asks no WebDriverAgent and takes no lease: enough to name a device (its state then assumes
    that WDA answers and the device is free)."""
    from .device_manager import DeviceManager, WDA_PORT
    from .device_info import MODEL_NAMES
    from .paths import user_data_dir
    data_dir = Path(data_dir) if data_dir else user_data_dir()
    records, seen_urls = [], set()

    def add(item):
        identity = (item.get("wdaUrl") or "").rstrip("/")
        if identity and identity in seen_urls:
            return
        if identity:
            seen_urls.add(identity)
        records.append(item)

    primary = DeviceManager(data_dir, wda_url(WDA_PORT))
    store = DeviceStore(data_dir)
    extras = store.phones()
    claimed = {entry["udid"] for entry in extras}
    primary.claimed = lambda: claimed
    try:
        connected = primary.devices()
    except Exception:
        connected = []
    by_udid = {item["udid"]: item for item in connected}
    wifi = wifi_phones(data_dir)

    def usb(udid, manager, url, mjpeg, is_primary, saved_name):
        device = by_udid.get(udid)
        over_wifi = False
        if device is None and wifi is not None and udid.upper() in wifi and probe and wda_answers(url):
            # Unplugged, but the Mac app's Wi-Fi relay answers on its port: the phone is there over Wi-Fi.
            device, over_wifi = {"udid": udid, "trusted": True}, True
        state, reason = usb_state(device, manager, (lambda: wda_answers(url)) if probe else (lambda: True),
                                  (lambda: lease_free(url)) if probe else (lambda: True))
        if wifi is not None and udid.upper() in wifi and state == "disconnected":
            reason = REASONS["wifi_unreachable"]
        model = (device or {}).get("model")
        item = record(id=udid, kind="usb", name=(device or {}).get("name") or saved_name, udid=udid, model=model,
                      model_name=MODEL_NAMES.get(model), ios=(device or {}).get("ios"), state=state, reason=reason,
                      wda_url_value=url, mjpeg_url=mjpeg, primary=is_primary,
                      set_up=manager is not None and manager.built())
        if wifi is not None:
            item["transport"] = "wifi" if over_wifi else "usb" if udid in by_udid else None
        return item

    chosen = primary.device(connected)
    primary_udid = primary.settings().get("udid") or (chosen or {}).get("udid")
    if primary_udid:
        add(usb(primary_udid, primary, wda_url(WDA_PORT), wda_url(MJPEG_BASE), True,
                primary.settings().get("device_name")))
    for entry in extras:
        if entry["udid"] == primary_udid:
            continue
        add(usb(entry["udid"], manager_for(entry["udid"], data_dir), wda_url(entry["wdaPort"]),
                wda_url(entry["mjpegPort"]), False, entry.get("name")))
    for device in connected:
        if device["udid"] in claimed or device["udid"] == primary_udid:
            continue
        add(record(id=device["udid"], kind="usb", name=device.get("name"), udid=device["udid"],
                   model=device.get("model"), model_name=MODEL_NAMES.get(device.get("model")), ios=device.get("ios"),
                   state="needs_setup", reason=REASONS["untrusted"] if not device.get("trusted") else REASONS["not_built"],
                   wda_url_value=None, set_up=False))
    if sims:
        for item in simulator_records(probe=probe):
            add(item)
    for spec in static_specs():
        add(static_record(spec.wda_url, id=spec.id, name=spec.label or None, mjpeg_url=spec.mjpeg_url or None,
                          **({} if probe else {"ready": True, "free": True})))
    for url in extra_urls:
        if url and url.rstrip("/") not in seen_urls:
            add(static_record(url, **({} if probe else {"ready": True, "free": True})))
    return records


def wifi_phones(data_dir):
    """The UDIDs (upper case) of the phones with Wi-Fi on, when Wi-Fi for iPhones is on in this process
    (MOBSTER_WIFI_TRANSPORT); None when it is off, so the cable-only listing stays exactly as it was."""
    from .wireless import store, transport_on
    if not transport_on():
        return None
    try:
        return store.enabled_udids(data_dir)
    except Exception:
        return set()


def manager_for(udid, data_dir=None):
    """The DeviceManager of a USB phone set up besides the primary one (devices.json), or None."""
    from .device_manager import DeviceManager
    from .paths import user_data_dir
    data_dir = Path(data_dir) if data_dir else user_data_dir()
    entry = next((item for item in DeviceStore(data_dir).phones() if item["udid"] == udid), None)
    if entry is None:
        return None
    return DeviceManager(data_dir, wda_url(entry["wdaPort"]), udid=udid, root=phone_folder(data_dir, udid),
                         video_port=entry["mjpegPort"])


def resolve(records, query):
    """The device ``query`` names: its id or UDID (any case), else its name (any case, exactly). Raises
    DeviceNotFound or AmbiguousDevice with a sentence that lists what is there."""
    text = str(query or "").strip()
    if not text:
        raise DeviceNotFound("Name a device: its id, UDID or name.")
    folded = text.casefold()
    for item in records:
        if folded in {str(item.get("id") or "").casefold(), str(item.get("udid") or "").casefold()}:
            return item
    named = [item for item in records if str(item.get("name") or "").casefold() == folded]
    if len(named) == 1:
        return named[0]
    if named:
        raise AmbiguousDevice(f"{len(named)} devices are named “{text}”. Name one by its id: "
                              + ", ".join(item["id"] for item in named) + ".")
    known = ", ".join(f"{item['name']} ({item['id']})" for item in records[:8])
    raise DeviceNotFound(f"No device is named or has the id “{text}”." +
                         (f" Devices: {known}." if known else " Mobster sees no device: plug in an iPhone or "
                                                              "prepare a simulator (`mobster sim prepare`)."))


def describe(item):
    """One line for a terminal: name, kind, state and reason."""
    kind = {"usb": "iPhone (USB)", "simulator": "Simulator", "wda": "WebDriverAgent"}.get(item["kind"], item["kind"])
    if item["kind"] == "usb" and item.get("transport") == "wifi":
        kind = "iPhone (Wi-Fi)"
    details = ", ".join(part for part in (item.get("modelName") or item.get("model"),
                                          f"iOS {item['ios']}" if item.get("ios") else None) if part)
    state = item["state"].replace("_", " ")
    line = f"{item['name']}  [{kind}{', ' + details if details else ''}]  {state}"
    if item.get("reason"):
        line += f": {item['reason']}"
    return line
