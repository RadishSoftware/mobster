"""The server's devices (SPEC §3): one ``Phone`` per device, and the ``Fleet`` that lists and resolves them.

The primary phone is the runtime itself: its ``video``, ``control``, ``wda_session()``, ``target_status()``,
``manager``, ``setup``, ``apps()`` and ``device_info``, exactly as with one phone, so single-phone behaviour,
its tests and the runtimes that subclass ``server.Runtime`` don't change. Every other device has its own of
each: a USB phone set up here gets a pinned ``DeviceManager`` (its own runner, relay on 8100+N, build folder and
Setup), a simulator or a WDA address only its WDA objects. See devices.py for ports and states.
"""

import threading
import time

from . import devices as registry

# The thread class as imported: tests that park the runtime's workers (patching threading.Thread) don't count the
# background refresh of the simulator list as one.
_Thread = threading.Thread
from .devices import REASONS


class Phone:
    """One device as the server drives it."""

    primary = False

    def __init__(self, fleet, *, id, kind, wda_url=None, mjpeg_url=None, udid=None, name=None, model=None, ios=None,
                 manager=None, setup=None):
        self.fleet, self.runtime = fleet, fleet.runtime
        self.id, self.kind, self.udid = id, kind, udid
        self._wda_url, self.mjpeg_url = (wda_url or "").rstrip("/") or None, mjpeg_url
        self.name, self.model, self.ios = name, model, ios
        self.manager, self.setup = manager, setup
        self.session_id, self.session_lock = None, threading.Lock()
        self.health_at, self.health_ready = 0.0, False
        self.health_lock = threading.Lock()
        self._video = self._control = None
        self.installed_apps = self.device_info = None
        self.objects_lock = threading.Lock()
        # A simulator's start (boot and WebDriverAgent) runs in the background: {"state", "error"}.
        self.start_state = {"state": "idle", "error": None}

    @property
    def key(self):
        """What ``Runtime.active_runs`` records for a run on this device."""
        return self.id

    @property
    def wda_url(self):
        return self._wda_url

    # -- WebDriverAgent ---------------------------------------------------------------------------------

    def wda_session(self):
        from .drivers import resolve_wda_session
        with self.session_lock:
            self.session_id = resolve_wda_session(self.wda_url, preferred=self.session_id)
            return self.session_id

    def current_session(self):
        try:
            return self.wda_session()
        except Exception:
            return None

    def target_status(self):
        """The runtime's target_status for this device: whether WDA answers and reaches the phone's UI."""
        ready, health = False, None
        if self.wda_url:
            with self.health_lock:
                if not self.health_at or time.monotonic() - self.health_at > 5:
                    from .transport import HTTP
                    client = None
                    try:
                        client = HTTP(self.wda_url)
                        response = client.request("GET", "/status", timeout=2)
                        self.health_ready = response.get("value", {}).get("ready") is True
                        if isinstance(response.get("sessionId"), str) and response["sessionId"]:
                            self.session_id = response["sessionId"]
                    except Exception:
                        self.health_ready = False
                    finally:
                        if client:
                            client.close()
                        self.health_at = time.monotonic()
                ready = self.health_ready
            health = self.manager.health_error() if self.manager is not None else None
            if ready and health:
                ready = False
        return {"device": bool(self.wda_url), "ready": ready, "health": health, "can_act": bool(self.wda_url),
                "driver": "wda", "screen_reading": False, "notes": []}

    # -- per-device objects -----------------------------------------------------------------------------

    @property
    def video(self):
        """This device's live view (and the frame clock's stream), made on first use."""
        if self._video is None and self.wda_url:
            from .wda_video import WdaVideo
            with self.objects_lock:
                if self._video is None:
                    self._video = WdaVideo(self.wda_url, mjpeg_url=self.mjpeg_url, session=self.current_session,
                                           quality=self.runtime.video_quality())
        return self._video

    @property
    def control(self):
        if self._control is None and self.wda_url:
            from .manual_control import ManualControl
            with self.objects_lock:
                if self._control is None:
                    self._control = ManualControl(self.wda_url, self.wda_session)
        return self._control

    def apps(self):
        """The apps a task on this device may name: the catalog, then the apps found on the phone."""
        from .catalog import APPS
        from .installed_apps import merge as merge_installed
        inventory = self.installed_apps.snapshot() if self.installed_apps else None
        return merge_installed(APPS, inventory)

    def info(self):
        if self.device_info is not None:
            return self.device_info.snapshot()
        return {"modelIdentifier": self.model if self.kind == "usb" else None,
                "modelName": self.model if self.kind == "simulator" else None, "iosVersion": self.ios,
                "buildVersion": None, "virtual": self.kind != "usb", "source": self.kind,
                "verifiedAt": None, "stale": False}

    def close(self, keep_runner=False):
        for item in (self._video, self._control):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    pass
        if self.manager is not None:
            if keep_runner:
                self.manager.watching.set()
            else:
                self.manager.close()


class PrimaryPhone(Phone):
    """The runtime's own device: everything is the runtime's, looked up when used."""

    primary = True

    def __init__(self, fleet):
        super().__init__(fleet, id=None, kind="usb")

    @property
    def key(self):
        return "@primary"

    @property
    def wda_url(self):
        url = getattr(self.runtime.config, "wda_url", None)
        return url.rstrip("/") if isinstance(url, str) and url else None

    @property
    def manager(self):
        return getattr(self.runtime, "manager", None)

    @manager.setter
    def manager(self, value):
        pass

    @property
    def setup(self):
        return getattr(self.runtime, "setup", None)

    @setup.setter
    def setup(self, value):
        pass

    @property
    def video(self):
        return self.runtime.video

    @property
    def control(self):
        return getattr(self.runtime, "control", None)

    def wda_session(self):
        return self.runtime.wda_session()

    def current_session(self):
        return self.runtime.current_wda_session()

    def target_status(self):
        return self.runtime.target_status()

    def apps(self):
        return self.runtime.apps()

    def info(self):
        return self.runtime.device_info.snapshot()

    def close(self, keep_runner=False):
        pass  # Runtime.close closes the runtime's own objects


class Fleet:
    """Every device this server can run tasks on, and which one a request means."""

    USB_TTL = 2.0
    SIM_TTL = 5.0

    def __init__(self, runtime):
        self.runtime = runtime
        self.config = runtime.config
        self.lock = threading.RLock()
        self.primary = PrimaryPhone(self)
        self.phones = {}          # id -> Phone: USB phones set up here, simulators, WDA addresses
        self.cache = {}
        self.sims_lock = threading.Lock()
        self.closed = False
        manager = getattr(runtime, "manager", None)
        self.data_dir = None
        if manager is not None:
            from pathlib import Path
            from .paths import user_data_dir
            self.data_dir = Path(getattr(self.config, "data_dir", None) or user_data_dir())
        self.store = registry.DeviceStore(self.data_dir) if self.data_dir is not None else None
        stored = self.store.read() if self.store is not None else {"phones": [], "lastUsed": None}
        self.last_used = stored["lastUsed"]
        preferred = getattr(self.config, "device", None)  # serve --device
        self.preferred = preferred if isinstance(preferred, str) and preferred else None
        if manager is not None:
            manager.claimed = lambda: {phone.udid for phone in list(self.phones.values())
                                       if phone.kind == "usb" and phone.manager is not None}
            for entry in stored["phones"]:
                if entry["udid"] == self._chosen():
                    continue
                try:
                    self._usb_phone(entry)
                except Exception as error:  # one phone's folder never stops the others, or the server
                    import sys
                    print(f"mobster: an iPhone set up earlier couldn't be loaded ({type(error).__name__})",
                          file=sys.stderr, flush=True)
        if getattr(runtime, "pool", None) is None:
            for spec in registry.static_specs(self.config):
                if spec.wda_url.rstrip("/") != (self.primary.wda_url or ""):
                    self.phones.setdefault(spec.id, Phone(self, id=spec.id, kind="wda", wda_url=spec.wda_url,
                                                          mjpeg_url=spec.mjpeg_url or None,
                                                          name=spec.label or None))

    # -- USB phones set up here -------------------------------------------------------------------------

    def _usb_phone(self, entry):
        from .device_manager import DeviceManager
        from .device_info import DeviceInfo
        from .installed_apps import InstalledApps
        from .setup_service import SetupService
        udid = entry["udid"]
        url, mjpeg = registry.wda_url(entry["wdaPort"]), registry.wda_url(entry["mjpegPort"])
        folder = registry.phone_folder(self.data_dir, udid)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        manager = DeviceManager(self.data_dir, url, udid=udid, root=folder, video_port=entry["mjpegPort"])
        primary = self.runtime.manager
        manager.cache = primary.cache  # one Xcode, team and USB reading for every phone
        phone = Phone(self, id=udid, kind="usb", wda_url=url, mjpeg_url=mjpeg, udid=udid, name=entry.get("name"),
                      manager=manager)
        env_file = self.runtime.setup.env_file if self.runtime.setup is not None else None
        phone.setup = SetupService(self.runtime, manager, env_file, video=lambda: phone.video,
                                   busy=lambda: self.busy(phone) is not None)
        phone.device_info = DeviceInfo(manager=manager)
        if not getattr(self.config, "socket", None):
            phone.installed_apps = InstalledApps(manager.device)
        self.phones[udid] = phone
        threading.Thread(target=manager.watch, args=(manager.watching,), name=f"mobster-device-watch-{udid[-6:]}",
                         daemon=True).start()
        return phone

    def adopt(self, udid):
        """The phone to set up for ``udid``: the primary when no phone is chosen yet (one phone stays the
        single-phone path), else its own device with the next free slot."""
        manager = self.runtime.manager
        if manager is None:
            raise LookupError("This Mobster doesn't manage USB iPhones. Start it with --manage-device.")
        with self.lock:
            if udid in self.phones and self.phones[udid].manager is not None:
                return self.phones[udid]
            chosen = self._chosen()
            if not chosen or chosen == udid:
                manager.choose(udid)
                return self.primary
            listing = {item["udid"]: item for item in self._usb_listing()}
            if udid not in listing:
                raise LookupError("That iPhone is not connected")
            entry = self.store.add(udid, listing[udid].get("name"))
            return self._usb_phone(entry)

    def forget(self, query):
        phone = self.find(query)
        if phone.primary or phone.kind != "usb" or phone.manager is None:
            raise LookupError("Only an extra iPhone set up here can be forgotten. To change your main iPhone, "
                              "choose another one in Setup.")
        # Checked and dropped under the runtime's lock, which admits tasks: none can start on it in between.
        with self.runtime.lock:
            if self.busy(phone) is not None:
                raise LookupError("A task is running on it. Stop it first.")
            with self.lock:
                self.phones.pop(phone.id, None)
        phone.close()
        self.store.remove(phone.udid)
        self.cache.pop("usb", None)  # the USB listing only: every phone keeps its lastUsedAt
        return phone.id

    # -- discovery --------------------------------------------------------------------------------------

    def _usb_listing(self):
        manager = getattr(self.runtime, "manager", None)
        if manager is None:
            return []
        cached = self.cache.get("usb")
        if cached and time.monotonic() - cached[0] < self.USB_TTL:
            return cached[1]
        try:
            listing = [item for item in manager.devices() if isinstance(item, dict) and isinstance(item.get("udid"), str)]
        except Exception:
            listing = []
        self.cache["usb"] = (time.monotonic(), listing)
        return listing

    def _sims_wanted(self):
        url = self.primary.wda_url or ""
        # No WDA target (a scripted phone, the terminal UI's demo): no simulators beside it.
        return getattr(self.runtime, "manager", None) is not None or url.startswith(("http://", "https://"))

    def _read_sims(self):
        try:
            rows = registry.simulator_rows()
        except Exception:
            rows = []
        self.cache["sims"] = (time.monotonic(), rows)
        return rows

    def _sim_rows(self):
        """Mobster's simulators. simctl takes up to a second, so a stale list is served while a fresh one is read
        in the background; only the first read waits, and only on a Mac with Mobster simulators."""
        if not self._sims_wanted():
            return []
        cached = self.cache.get("sims")
        if cached is None:
            with self.sims_lock:
                cached = self.cache.get("sims")
                return cached[1] if cached is not None else self._read_sims()
        if time.monotonic() - cached[0] >= self.SIM_TTL and self.sims_lock.acquire(blocking=False):
            def refresh():
                try:
                    self._read_sims()
                finally:
                    self.sims_lock.release()
            _Thread(target=refresh, name="mobster-fleet-sims", daemon=True).start()
        return cached[1]

    def _sim_phone(self, row):
        with self.lock:
            phone = self.phones.get(row["udid"])
            if phone is None or phone.kind != "simulator" or phone.wda_url != row["wda_url"].rstrip("/"):
                phone = Phone(self, id=row["udid"], kind="simulator", wda_url=row["wda_url"],
                              mjpeg_url=row.get("mjpeg_url"), udid=row["udid"], name=row.get("name"),
                              model=row.get("device_type"),
                              ios=str(row.get("runtime") or "").removeprefix("iOS ").strip() or None)
                self.phones[row["udid"]] = phone
            return phone

    def _chosen(self):
        """The UDID chosen in Setup for the primary phone, or None."""
        manager = getattr(self.runtime, "manager", None)
        try:
            udid = manager.settings().get("udid") if manager is not None else None
        except Exception:
            return None
        return udid if isinstance(udid, str) and udid else None

    def _primary_identity(self):
        """(id, kind, record fields) for the primary device, or None when it has none yet (no phone chosen and
        not exactly one connected; or no WDA address at all)."""
        manager = getattr(self.runtime, "manager", None)
        url = self.primary.wda_url
        if manager is not None:
            listing = self._usb_listing()
            udid = self._chosen()
            if udid is None:
                try:
                    found = manager.device(listing)
                except Exception:
                    found = None
                udid = found.get("udid") if isinstance(found, dict) else None
            if not isinstance(udid, str) or not udid:
                return None
            device = next((item for item in listing if item["udid"] == udid), None)
            saved = manager.settings().get("device_name")
            return {"id": udid, "kind": "usb", "udid": udid, "device": device,
                    "name": (device or {}).get("name") or (saved if isinstance(saved, str) else None)}
        if not url:
            return None
        for row in self._sim_rows():
            if row["wda_url"].rstrip("/") == url:
                return {"id": row["udid"], "kind": "simulator", "udid": row["udid"], "row": row,
                        "name": row.get("name")}
        return {"id": registry.device_id_for_url(url), "kind": "wda", "udid": None, "name": None}

    def busy(self, phone):
        """The id of the run active on ``phone``, or None (Runtime.run_on). Read without the runtime's lock
        (create holds it)."""
        run_on = getattr(self.runtime, "run_on", None)
        if callable(run_on):
            return run_on(phone)
        return next((run_id for run_id, key in list(self.runtime.active_runs.items()) if key == phone.key), None)

    def in_use(self, phone):
        """Whether a task runs on ``phone`` now: here (``busy``), or in another Mobster process that holds its
        device lease (`mobster run`, an agent's MCP server). Wi-Fi changes a phone's transport only when it's False
        (wireless/monitor.py). Never called with the runtime's lock held."""
        if self.busy(phone) is not None:
            return True
        url = phone.wda_url
        return bool(url) and not self._lease_free(url)

    def _lease_free(self, url):
        # Under the runtime's lock: Runtime.create takes the same lease under it, and two flocks in one process
        # on one file would make a task refused while this probe holds it for a moment. Never called with that
        # lock held (create resolves its device first).
        with self.runtime.lock:
            return registry.lease_free(url)

    def inventory(self):
        """[(phone, context)] for every device, without probing them, primary first. ``context`` holds what the
        listing knew (the USB entry, the simulator row). The primary is left out while it has no identity."""
        found = []
        identity = self._primary_identity()
        primary_url = self.primary.wda_url
        if identity is not None:
            phone = self.primary
            phone.id, phone.kind, phone.udid = identity["id"], identity["kind"], identity["udid"]
            phone.name = identity.get("name") or phone.name
            found.append((phone, identity))
        with self.lock:
            usb_phones = [phone for phone in self.phones.values() if phone.kind == "usb" and phone.manager is not None]
        claimed = {phone.udid for phone in usb_phones}
        if identity is not None and identity["kind"] == "usb":
            claimed.add(identity["udid"])
        listing = {item["udid"]: item for item in self._usb_listing()}
        for phone in usb_phones:
            device = listing.get(phone.udid)
            if device is not None:
                phone.name = device.get("name") or phone.name
                phone.model, phone.ios = device.get("model"), device.get("ios")
            found.append((phone, {"device": device}))
        for udid, device in listing.items():
            if udid in claimed:
                continue
            with self.lock:
                loose = self.phones.get(udid)
                if loose is None or loose.manager is not None:
                    loose = self.phones[udid] = Phone(self, id=udid, kind="usb", udid=udid)
            loose.name, loose.model, loose.ios = device.get("name"), device.get("model"), device.get("ios")
            found.append((loose, {"device": device, "loose": True}))
        seen = {primary_url} if primary_url else set()
        seen |= {phone.wda_url for phone in usb_phones}
        for row in self._sim_rows():
            if row["wda_url"].rstrip("/") in seen:
                continue
            phone = self._sim_phone(row)
            found.append((phone, {"row": row}))
            seen.add(phone.wda_url)
        with self.lock:
            static = [phone for phone in self.phones.values() if phone.kind == "wda"]
        for phone in static:
            if phone.wda_url not in seen:
                found.append((phone, {}))
                seen.add(phone.wda_url)
        # Devices that went away (a simulator deleted, a phone unplugged before it was set up) are dropped.
        live = {id(phone) for phone, _ in found}
        with self.lock:
            for key in [key for key, phone in self.phones.items() if id(phone) not in live and phone.manager is None
                        and phone.kind != "wda" and self.busy(phone) is None]:
                self.phones.pop(key, None)
        return found

    def usb_managers(self):
        """[(phone, UDID, DeviceManager)] for every USB phone set up here: the primary (once one is chosen in Setup)
        and each phone added besides it. Wi-Fi's transport monitor (wireless/monitor.py) drives their runners."""
        out = []
        manager = getattr(self.runtime, "manager", None)
        udid = self._chosen() if manager is not None else None
        if udid:
            out.append((self.primary, udid, manager))
        with self.lock:
            others = [phone for phone in self.phones.values() if phone.kind == "usb" and phone.manager is not None]
        out += [(phone, phone.udid, phone.manager) for phone in others if phone.udid and phone.udid != udid]
        return out

    def _wifi(self):
        """The UDIDs with Wi-Fi on when Wi-Fi for iPhones is on (MOBSTER_WIFI_TRANSPORT), else None: then every
        record is exactly the cable-only one."""
        return registry.wifi_phones(self.data_dir) if self.data_dir is not None else None

    def entries(self, found=None):
        """[(phone, public record)]: devices.record plus the server's fields, primary first. With Wi-Fi for iPhones
        on, a USB phone's record also has ``transport`` ("usb" | "wifi" | None) and ``wifi`` {enabled, reachable}."""
        from .device_info import MODEL_NAMES
        found = self.inventory() if found is None else found
        default = self.default(found)
        wifi = self._wifi()
        rows = []
        for phone, context in found:
            if context.get("loose"):
                device = context["device"]
                item = registry.record(
                    id=phone.id, kind="usb", name=phone.name, udid=phone.udid, model=phone.model,
                    model_name=MODEL_NAMES.get(phone.model), ios=phone.ios, state="needs_setup",
                    reason=REASONS["not_built"] if device.get("trusted") else REASONS["untrusted"],
                    wda_url_value=None, set_up=False)
            else:
                item = self._record(phone, context, wifi)
            run_id = self.busy(phone)
            run = self.runtime.runs.get(run_id) if run_id else None
            approval = None
            if run is not None:
                with run.condition:
                    approval = dict(run.approval) if run.approval else None
            item = {key: value for key, value in item.items() if key not in ("wdaUrl", "mjpegUrl")}
            item.update(default=phone is default, activeRunId=run_id, approval=approval,
                        lastUsedAt=self.last_used_at(phone))
            if wifi is not None and item["kind"] == "usb":
                item.update(self._transport_fields(phone, context, wifi))
            rows.append((phone, item))
        return rows

    @staticmethod
    def _transport_fields(phone, context, wifi):
        from .wireless import transport
        link = transport.link(phone.udid) if phone.udid else None
        manager = phone.manager
        now = getattr(manager, "transport", None) if manager is not None and manager.runner is not None else None
        if now is None and context.get("device") is not None:
            now = "usb"
        return {"transport": now, "wifi": {"enabled": str(phone.udid or "").upper() in wifi,
                                           "reachable": bool(link is not None and link.reachable
                                                             and link.transport == "wifi")}}

    def _record(self, phone, context, wifi=None):
        """A USB phone with a manager here, a simulator or a WDA address, with its state probed now. ``wifi``: the
        UDIDs with Wi-Fi on (Fleet._wifi), or None while Wi-Fi for iPhones is off."""
        from .device_info import MODEL_NAMES
        if phone.kind == "simulator":
            return self._sim_record(phone, context["row"])
        if phone.kind == "wda":
            return self._static_record(phone)
        device = context.get("device")
        wifi_on = wifi is not None and str(phone.udid or "").upper() in wifi
        manager = phone.manager
        if device is None and wifi_on and manager is not None and getattr(manager, "transport", None) == "wifi":
            # Unplugged, and its runner runs over Wi-Fi: the phone is here, through the encrypted tunnel.
            device = {"udid": phone.udid, "trusted": True}
        if self.busy(phone) is not None:
            state, reason = "busy", REASONS["busy"]
        else:
            target = phone.target_status() if device is not None and device.get("trusted") else {}
            answered = bool(target.get("ready") or target.get("health"))
            state, reason = registry.usb_state(device, phone.manager, lambda: answered,
                                               lambda: self._lease_free(phone.wda_url), locked=bool(target.get("health")))
            if state == "connected" and target.get("health"):
                reason = target["health"]
            if state == "disconnected" and wifi_on:
                reason = REASONS["wifi_unreachable"]
        built = phone.manager is not None and phone.manager.built()
        return registry.record(id=phone.id, kind="usb", name=phone.name, udid=phone.udid, model=phone.model,
                               model_name=MODEL_NAMES.get(phone.model), ios=phone.ios, state=state, reason=reason,
                               wda_url_value=phone.wda_url, mjpeg_url=phone.mjpeg_url, primary=phone.primary,
                               set_up=built)

    def _sim_record(self, phone, row):
        busy_here = self.busy(phone) is not None
        ready = None
        if not busy_here and row.get("state") == "Booted":
            ready = phone.target_status()["ready"]
        item = registry.simulator_record(row, ready=ready, free=None if busy_here else self._lease_free(phone.wda_url)
                                         if ready else True, busy_here=busy_here)
        if phone.start_state["state"] == "starting" and item["state"] != "ready":
            item.update(state="connected", reason="Starting the simulator and Mobster's helper on it.")
        elif phone.start_state["state"] == "failed" and item["state"] != "ready":
            item["reason"] = phone.start_state["error"] or item["reason"]
        return {**item, "primary": phone.primary}

    def _static_record(self, phone):
        busy_here = self.busy(phone) is not None
        if busy_here:
            item = registry.record(id=phone.id, kind="wda", name=phone.name or f"WebDriverAgent on port "
                                   f"{registry.port_of(phone.wda_url)}", state="busy", reason=REASONS["busy"],
                                   wda_url_value=phone.wda_url)
        else:
            ready = phone.target_status()["ready"]
            item = registry.static_record(phone.wda_url, id=phone.id, name=phone.name, ready=ready,
                                          free=self._lease_free(phone.wda_url) if ready else True)
        return {**item, "primary": phone.primary}

    # -- what a request means -----------------------------------------------------------------------------

    def public(self):
        return [item for _, item in self.entries()]

    def find(self, query, found=None):
        """The phone ``query`` names (id, UDID or name), resolved without probing any device: DeviceNotFound or
        AmbiguousDevice otherwise."""
        found = self.inventory() if found is None else found
        rows = [(phone, self._name_row(phone)) for phone, _ in found]
        item = registry.resolve([row for _, row in rows], query)
        return next(phone for phone, row in rows if row is item)

    def lookup(self, query):
        """``find`` for a request about one device (its live view's stream and frames, gestures, info, Setup),
        which the app sends every few seconds: a device already known here by that id or UDID is found without
        listing the devices again. A task's device is resolved with ``find``, against the devices there now."""
        known = self._known(query)
        return known if known is not None else self.find(query)

    def _known(self, query):
        """The phone whose id or UDID is ``query`` (any case) among those already known, or None: the primary
        while its identity can't change under it (its UDID chosen in Setup, or no USB phones managed here), and
        the other devices listed before, except a phone plugged in but not set up (Setup may adopt it any moment).
        A name, or anything else, goes through the full listing."""
        folded = str(query or "").strip().casefold()
        if not folded:
            return None
        primary = self.primary
        if primary.id and folded in {primary.id.casefold(), str(primary.udid or "").casefold()}:
            manager = getattr(self.runtime, "manager", None)
            stable = manager is None or (primary.kind == "usb" and self._chosen() == primary.udid)
            return primary if stable else None
        with self.lock:
            phones = list(self.phones.values())
        for phone in phones:
            if phone.kind == "usb" and phone.manager is None:
                continue
            if phone.id and folded in {phone.id.casefold(), str(phone.udid or "").casefold()}:
                return phone
        return None

    def entry(self, query):
        """(phone, public record) for one device, probing only that one."""
        found = self.inventory()
        phone = self.find(query, found)
        return next(row for row in self.entries([(candidate, context) for candidate, context in found
                                                  if candidate is phone]))

    def default(self, found=None, sticky=True):
        """Where a task that names no device runs: `serve --device`, else the only device, else the one last
        used, else the primary. ``sticky=False`` (a saved task, which may run unattended) leaves out the one last
        used: it runs on the same phone whichever phone someone used last."""
        found = self.inventory() if found is None else found
        phones = [phone for phone, _ in found]
        if self.preferred:
            try:
                return self.find(self.preferred, found)
            except LookupError:
                pass
        if len(phones) == 1:
            return phones[0]
        if sticky and self.last_used:
            match = next((phone for phone in phones if phone.id == self.last_used), None)
            if match is not None:
                return match
        return self.primary

    @staticmethod
    def _name_row(phone):
        return {"id": phone.id or "", "udid": phone.udid, "name": phone.name}

    def for_run(self, query):
        """The phone a new task runs on: the one ``query`` names, or the default device for None. A device
        found plugged in but not set up is returned too; admission refuses it as not ready."""
        if query is None:
            return self.default()
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128:
            raise ValueError("device must be a device's id, UDID or name")
        return self.find(query)

    # -- Setup, per device ---------------------------------------------------------------------------------

    def setup_state(self, phone):
        """What Setup shows for this device. LookupError for a device Mobster doesn't set up (a WDA address)."""
        if phone.kind == "usb":
            if phone.primary or (phone.setup is None and not self._primary_chosen()):
                if self.runtime.setup is None:
                    raise LookupError("Setup is managed outside this app")
                return self.runtime.setup.state()
            service = phone.setup if phone.setup is not None else self._loose_setup(phone)
            return service.state()
        if phone.kind == "simulator":
            row = next((row for row in self._sim_rows() if row["udid"] == phone.udid), None)
            item = self._sim_record(phone, row) if row is not None else None
            runner = ("starting" if phone.start_state["state"] in ("starting", "stopping") else
                      "running" if item is not None and item["state"] in ("ready", "busy") else
                      "failed" if phone.start_state["state"] == "failed" else "stopped")
            return {"kind": "simulator", "complete": runner == "running",
                    "device": {key: value for key, value in (item or {}).items() if key not in ("wdaUrl", "mjpegUrl")},
                    "runner": {"state": runner, "error": phone.start_state.get("error")}}
        raise LookupError("Setup is managed outside this app")

    def _primary_chosen(self):
        return self._chosen() is not None

    def _loose_setup(self, phone):
        """Setup's state for a phone plugged in but not set up yet, read through a manager pinned to it that
        writes nothing until the phone is adopted."""
        from .device_manager import DeviceManager
        from .setup_service import SetupService
        if self.runtime.setup is None:
            raise LookupError("Setup is managed outside this app")
        manager = DeviceManager(self.data_dir, registry.wda_url(0), udid=phone.udid,
                                root=registry.phone_folder(self.data_dir, phone.udid))
        manager.cache = self.runtime.manager.cache
        return SetupService(self.runtime, manager, self.runtime.setup.env_file, video=lambda: None,
                            busy=lambda: False)

    def setup_action(self, phone, action, body):
        """One Setup action on this device; returns the device Setup now concerns. KeyError for an action this
        device has none of; LookupError when it can't be done now; ValueError for a bad body."""
        if phone.kind == "usb":
            if action == "build" and set(body) == {"team"}:
                target = phone if phone.primary else self.adopt(phone.udid)
                target.manager.start_build(body["team"])
                self.cache.pop("usb", None)
                return target
            manager = phone.manager
            if manager is None:
                if action == "refresh" and not body:
                    # "Check again" on a new phone's first steps (Trust, Developer Mode, the tools): read them anew.
                    self._loose_setup(phone).refresh()
                    self.cache.pop("usb", None)
                    return phone
                raise LookupError("Set this iPhone up first: install Mobster's helper on it.")
            if action == "start" and not body:
                manager.start_runner()
            elif action == "stop" and not body:
                manager.stop_runner(remember=True)
            elif action == "refresh" and not body:
                (phone.setup or self.runtime.setup).refresh()
            else:
                raise KeyError(action)
            phone.health_at = 0.0
            return phone
        if phone.kind == "simulator":
            if action == "start" and not body:
                self._simulator_job(phone, "starting")
            elif action == "stop" and not body:
                if self.busy(phone) is not None:
                    raise LookupError("A task is running on it. Stop it first.")
                self._simulator_job(phone, "stopping")
            else:
                raise KeyError(action)
            return phone
        raise LookupError("Setup is managed outside this app")

    def _simulator_job(self, phone, kind):
        """Boot a simulator and start its WebDriverAgent, or shut it down, in the background."""
        with self.lock:
            if phone.start_state["state"] in ("starting", "stopping"):
                return
            phone.start_state = {"state": kind, "error": None}

        def work():
            try:
                from .sim import SimulatorManager
                manager = SimulatorManager(data_dir=registry.simulator_folder())
                if kind == "starting":
                    manager.acquire_udid(phone.udid, timeout=30).release()
                else:
                    manager.shutdown(phone.udid)
                phone.start_state = {"state": "idle", "error": None}
            except Exception as error:
                phone.start_state = {"state": "failed", "error": " ".join(
                    part for part in (str(error), getattr(error, "fix", "")) if part)[:300]}
            finally:
                self._read_sims()
                phone.health_at = 0.0
        threading.Thread(target=work, name=f"mobster-sim-{kind}", daemon=True).start()

    def mark_used(self, phone):
        if phone is None or not phone.id:
            return
        self.last_used = phone.id
        self.cache.setdefault("used_at", {})[phone.id] = round(time.time() * 1000)
        if self.store is not None:
            try:
                self.store.set_last_used(phone.id)
            except OSError:
                pass

    def last_used_at(self, phone):
        return self.cache.get("used_at", {}).get(phone.id)

    def videos(self):
        return [phone._video for phone in list(self.phones.values()) if phone._video is not None]

    def close(self, keep_runner=False):
        self.closed = True
        with self.lock:
            phones = list(self.phones.values())
        for phone in phones:
            phone.close(keep_runner)
