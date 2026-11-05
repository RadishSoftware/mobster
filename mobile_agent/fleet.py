"""The server's devices (SPEC §3): one ``Phone`` per device, and the ``Fleet`` that lists and resolves them."""

import contextlib
import json
import shutil
import threading
import time
from pathlib import Path

from . import devices as registry
from . import ownership
from .devices import REASONS
from .fleet_phone import Phone, PrimaryPhone

# The thread class as imported: tests that park the runtime's workers (patching threading.Thread) don't count the
# background refresh of the simulator list as one.
_Thread = threading.Thread


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
            from .paths import user_data_dir
            self.data_dir = Path(getattr(self.config, "data_dir", None) or user_data_dir())
        self.store = registry.DeviceStore(self.data_dir) if self.data_dir is not None else None
        from .fleet_health import FleetHealth
        self.health = FleetHealth(self)
        # A phone's own settings (a nickname, the default, Disconnect): devices.json in the data folder, else beside the
        # journal (a server without USB phones), else in memory only.
        state_db = getattr(self.config, "state_db", None)
        self.prefs_store = self.store if self.store is not None else registry.DeviceStore(
            Path(state_db).parent if isinstance(state_db, str) and state_db else None)
        self.prefs_cache = None
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
        from .device_info import DeviceInfo
        from .device_manager import DeviceManager
        from .installed_apps import InstalledApps, InventoryCache
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
                                   busy=lambda: self.busy(phone) is not None,
                                   stop=lambda message: self.runtime.stop_phone_run(phone, message),
                                   idle=lambda: self.runtime.release_idle(phone))
        phone.device_info = DeviceInfo(manager=manager)
        if not getattr(self.config, "socket", None):
            phone.installed_apps = InstalledApps(manager.device, cache=InventoryCache(self.data_dir / "installed-apps"),
                                                 over_wifi=manager.wifi_udid)
        self.phones[udid] = phone
        threading.Thread(target=manager.watch, args=(manager.watching,), name=f"mobster-device-watch-{udid[-6:]}",
                         daemon=True).start()
        return phone

    def adopt(self, udid):
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
        if phone.kind != "usb" or phone.manager is None:
            if phone.kind == "simulator":
                raise LookupError("Mobster's simulators are managed with `mobster sim`. Remove it there.")
            if phone.kind == "wda":
                raise LookupError("This device comes from MOBSTER_WDA_DEVICES. Remove it there.")
            raise LookupError("This iPhone isn't set up in Mobster, so there's nothing to forget.")
        # Checked and dropped under the runtime's lock, which admits tasks: none can start on it in between.
        with self.runtime.lock:
            if self.busy(phone) is not None:
                raise LookupError("A task is running on it. Stop it first.")
            if not phone.primary:
                with self.lock:
                    self.phones.pop(phone.id, None)
        udid, forgotten = phone.udid, phone.id
        if phone.primary:
            self._forget_primary(phone)
        else:
            phone.close()
            self.store.remove(udid)
            with contextlib.suppress(OSError):
                shutil.rmtree(registry.phone_folder(self.data_dir, udid))
        self._drop_prefs(forgotten, udid)
        self.release_profiles(forgotten)
        with contextlib.suppress(OSError):
            (self.data_dir / "installed-apps" / f"{udid}.json").unlink()
        self.cache.pop("usb", None)  # the USB listing only: every phone keeps its lastUsedAt
        return forgotten

    def _forget_primary(self, phone):
        manager = phone.manager
        manager.stop_runner(remember=True)
        settings = {key: value for key, value in manager.settings().items()
                    if key not in ("udid", "device_name", "built_for", "built_patches", "autostart", "expires_at")}
        with contextlib.suppress(OSError):
            registry_dir = manager.settings_path.parent
            registry_dir.mkdir(parents=True, exist_ok=True)
            tmp = manager.settings_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(settings, indent=1))
            tmp.chmod(0o600)
            tmp.replace(manager.settings_path)
        with contextlib.suppress(OSError):
            shutil.rmtree(manager.derived)
        phone.id = phone.udid = None
        phone.name = None

    def release_profiles(self, device_id):
        # to the default phone, so a task as one of them starts instead of being refused for a phone that is gone
        from .accounts.store import store_for
        with contextlib.suppress(OSError):
            store_for(self.runtime).forget_device(device_id)

    def _drop_prefs(self, *ids):
        for device_id in {item for item in ids if item}:
            with contextlib.suppress(OSError, ValueError):
                self.prefs_store.remove(device_id)
            if self.last_used == device_id:
                self.last_used = None
        self.prefs_cache = None

    # -- a phone's own settings (Settings › Phones) ------------------------------------------------------------

    def stored(self):
        """{prefs, defaultDevice, disconnected, chosen}: read from devices.json at most once per change of the file."""
        store = self.prefs_store
        stamp = None
        if store.path is not None:  # in memory there is no file to stamp, and reading is free
            try:
                stamp = store.path.stat().st_mtime_ns
            except OSError:
                pass
            cached = self.prefs_cache
            if cached is not None and cached[0] == stamp:
                return cached[1]
        data = store.read()
        value = {"prefs": data["prefs"], "defaultDevice": data["defaultDevice"], "disconnected": data["disconnected"],
                 "chosen": data["chosen"]}
        if store.path is not None:
            self.prefs_cache = (stamp, value)
        return value

    def prefs(self, phone):
        return dict(self.stored()["prefs"].get(phone.id) or {}) if phone is not None and phone.id else {}

    def is_disconnected(self, phone):
        return bool(phone is not None and phone.id and phone.id in self.stored()["disconnected"])

    def set_prefs(self, phone, **changes):
        if not phone.id:
            raise LookupError("This phone has no identity yet. Plug it in and unlock it.")
        value = self.prefs_store.set_prefs(phone.id, **changes)
        self.prefs_cache = None
        if "nickname" in changes:
            phone.name = value.get("nickname") or phone.name
        return value

    def set_default(self, phone):
        """Make ``phone`` the one a new task runs on unless another is picked (None: no default of your own)."""
        device_id = phone.id if phone is not None else None
        if phone is not None and not device_id:
            raise LookupError("This phone has no identity yet. Plug it in and unlock it.")
        self.prefs_store.set_default(device_id)
        self.prefs_cache = None
        return device_id

    # -- the composer's phone (the Mac app) -----------------------------------------------------------------------

    def choose(self, device_ids):
        clean = registry.clean_chosen(device_ids) if isinstance(device_ids, (list, tuple)) else None
        if clean is None or len(clean) != len(device_ids):
            raise ValueError(f"devices is a list of at most {registry.MAX_CHOSEN} different device ids")
        self.prefs_store.set_chosen(clean)
        self.prefs_cache = None
        return clean

    def chosen(self, records=None):
        """Keep a connected choice, except one needing setup while another is set up; never save the fallback."""
        records = self.public() if records is None else records
        listed = {item["id"]: item for item in records if item.get("id")}
        kept = [device_id for device_id in self.stored()["chosen"]
                if device_id in listed and listed[device_id]["state"] != "disconnected"]
        if kept and not (all(listed[device_id]["state"] == "needs_setup" for device_id in kept)
                         and any(item["state"] not in ("needs_setup", "disconnected") for item in records)):
            return kept
        if not records:  # the fallback below is never saved, so the chosen phone comes back when plugged in
            return []
        rank = {"ready": 0, "busy": 1, "connected": 2, "needs_setup": 3, "disconnected": 4}
        best = min(enumerate(records), key=lambda pair: (rank.get(pair[1]["state"], 4), not pair[1].get("default"),
                                                         not pair[1].get("primary"), pair[0]))[1]
        return [best["id"]] if best.get("id") else []

    def disconnect(self, phone):
        if not phone.id:
            raise LookupError("This phone has no identity yet.")
        if phone.kind == "usb" and phone.manager is None:
            raise LookupError("This iPhone isn't set up yet, so it isn't in use.")
        with self.runtime.lock:
            if self.busy(phone) is not None:
                raise LookupError("A task is running on it. Stop it first.")
            self._set_disconnected(phone.id, True)
        if phone.manager is not None:
            with contextlib.suppress(Exception):
                phone.manager.stop_runner(remember=True)
        if not phone.primary:
            for name in ("_video", "_control"):
                item = getattr(phone, name, None)
                if item is not None:
                    with contextlib.suppress(Exception):
                        item.close()
                    setattr(phone, name, None)
        phone.health_at = 0.0
        return phone.id

    def connect(self, phone):
        if not phone.id:
            raise LookupError("This phone has no identity yet.")
        self._set_disconnected(phone.id, False)
        note = None
        manager = phone.manager
        if manager is not None:
            with contextlib.suppress(OSError):
                manager.save_settings(autostart=True)  # the watcher starts it when the phone is back
            try:
                manager.start_runner()
            except LookupError as error:
                note = str(error)
            except Exception:  # noqa: BLE001 -- the watcher tries again
                note = "Mobster will start its helper on it in a moment."
        phone.health_at = 0.0
        return note

    def _set_disconnected(self, device_id, value):
        self.prefs_store.set_disconnected(device_id, value)
        self.prefs_cache = None

    # -- discovery --------------------------------------------------------------------------------------

    def _usb_listing(self):
        manager = getattr(self.runtime, "manager", None)
        if manager is None:
            return []
        cached = self.cache.get("usb")
        if cached and time.monotonic() - cached[0] < self.USB_TTL:
            return cached[1]
        try:
            listing = [item for item in manager.devices() if isinstance(item, dict)
                       and isinstance(item.get("udid"), str)]
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
        # simctl takes up to a second: a stale list is served while a fresh one is read in the background
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
        run_on = getattr(self.runtime, "run_on", None)
        if callable(run_on):
            return run_on(phone)
        return next((run_id for run_id, key in list(self.runtime.active_runs.items()) if key == phone.key), None)

    def in_use(self, phone):
        if self.busy(phone) is not None:
            return True
        url = phone.wda_url
        return bool(url) and not self._lease_free(url, phone.udid)

    def _lease_free(self, url, udid=None):
        # Under the runtime's lock: Runtime.create takes the same lease under it, and two flocks in one process
        # on one file would make a task refused while this probe holds it for a moment. Never called with that
        # lock held (create resolves its device first).
        with self.runtime.lock:
            return registry.lease_free(url, udid)

    def inventory(self):
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
        # A nickname given in Settings › Phones is the phone's name everywhere (the device list, runs, conversations).
        nicknames = self.stored()["prefs"]
        for phone, _ in found:
            nickname = (nicknames.get(phone.id) or {}).get("nickname") if phone.id else None
            if nickname:
                phone.name = nickname
        # Devices that went away (a simulator deleted, a phone unplugged before it was set up) are dropped.
        live = {id(phone) for phone, _ in found}
        with self.lock:
            for key in [key for key, phone in self.phones.items() if id(phone) not in live and phone.manager is None
                        and phone.kind != "wda" and self.busy(phone) is None]:
                self.phones.pop(key, None)
        return found

    def usb_managers(self):
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
        return registry.wifi_phones(self.data_dir) if self.data_dir is not None else None

    def entries(self, found=None):
        from .device_info import MODEL_NAMES
        # one phone's probe (``entry``) isn't every phone: the alerts' baseline needs every one
        complete = found is None
        found = self.inventory() if found is None else found
        default = self.default(found)
        chosen_default = self.stored()["defaultDevice"]  # Settings › Phones › Set as default (not the phone used last)
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
            prefs = self.prefs(phone)
            disconnected = self.is_disconnected(phone)
            if disconnected and run_id is None and item["state"] != "needs_setup":
                item.update(state="disconnected", reason=REASONS["paused"])
            item.update(default=phone is default, activeRunId=run_id, approval=approval,
                        lastUsedAt=self.last_used_at(phone), nickname=prefs.get("nickname"),
                        disconnected=disconnected, askBeforeActing=prefs.get("askBeforeActing"),
                        apps=prefs.get("apps"), defaultSet=bool(phone.id) and phone.id == chosen_default)
            if wifi is not None and item["kind"] == "usb":
                item.update(self._transport_fields(phone, context, wifi))
            # How the screen and the taps reach it: "wda" (Pro), "mirroring" or "cable_bt" (Cable + Bluetooth). A
            # connection that sets its own on the phone wins.
            item["connection"] = getattr(phone, "connection", None) or "wda"
            item.update(self._ownership_fields(phone, item))
            item.update(self._reliability_fields(phone))
            rows.append((phone, item))
        # Last, so the fields other work adds to a record (the owner, the holder) are there to read.
        try:
            self.health.apply(rows, complete=complete)
        except Exception:  # noqa: BLE001 -- the device list never fails for health
            pass
        return rows

    def _ownership_fields(self, phone, item):
        read = getattr(self.runtime, "phone_ownership", None)
        if not callable(read) or item.get("state") == "needs_setup":
            return {}
        try:
            fields = read(phone)
        except Exception:  # noqa: BLE001 -- a device list is still a device list without ownership
            return {}
        if fields["holder"] is None and item.get("state") == "busy" and item.get("reason") == REASONS["busy_elsewhere"]:
            fields = {**fields, "holder": ownership.elsewhere_holder()}
        return fields

    @staticmethod
    def _reliability_fields(phone):
        manager = getattr(phone, "manager", None)
        fields = {}
        if getattr(manager, "presence", None) is not None:
            fields["owner"] = manager.presence.snapshot()
        if getattr(manager, "watchdog", None) is not None:
            fields["watchdog"] = manager.watchdog.snapshot()
        renewal = getattr(manager, "renewal", None)
        if renewal is not None:
            try:
                fields["renewal"] = renewal.public()
            except Exception:  # noqa: BLE001 -- a record never fails for a field that is only advice
                pass
        return fields

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
                                               lambda: self._lease_free(phone.wda_url, phone.udid),
                                               locked=bool(target.get("health")))
            if state == "connected" and target.get("health"):
                # The locked sentence is REASONS["locked"], which fleet_health keys its alert on.
                reason = REASONS["locked"] if phone.manager.locked() else target["health"]
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
        found = self.inventory() if found is None else found
        rows = [(phone, self._name_row(phone)) for phone, _ in found]
        item = registry.resolve([row for _, row in rows], query)
        return next(phone for phone, row in rows if row is item)

    def lookup(self, query):
        known = self._known(query)
        return known if known is not None else self.find(query)

    def _known(self, query):
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
            if phone.kind == "usb" and phone.manager is None:  # not set up: Setup may adopt it any moment
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
        """Resolve explicit preferences before a usable last-used phone; unattended tasks keep the primary fallback."""
        found = self.inventory() if found is None else found
        phones = [phone for phone, _ in found]
        if self.preferred:
            try:
                return self.find(self.preferred, found)
            except LookupError:
                pass
        chosen = self.stored()["defaultDevice"]  # Settings › Phones › Set as default
        if chosen:
            match = next((phone for phone in phones if phone.id == chosen), None)
            if match is not None:
                return match
        if len(phones) == 1:
            return phones[0]
        if not sticky:
            return self.primary
        unready = {id(phone) for phone, context in found if self._needs_setup(phone, context)}
        usable = [phone for phone in phones if id(phone) not in unready]
        if self.last_used:
            match = next((phone for phone in phones if phone.id == self.last_used), None)
            if match is not None and (id(match) not in unready or not usable):
                return match
        if not usable or any(phone is self.primary for phone in usable):
            return self.primary
        return max(usable, key=lambda phone: self.last_used_at(phone) or 0)

    @staticmethod
    def _needs_setup(phone, context):
        """Whether identity or build state requires setup, without probing the phone."""
        if context.get("loose"):
            return True
        if phone.kind != "usb":
            return False
        device = context.get("device")
        if device is not None and not device.get("trusted"):
            return True
        manager = phone.manager
        try:
            return manager is None or not manager.built()
        except Exception:  # noqa: BLE001 -- a helper folder it can't read is a phone it can't use yet
            return True

    @staticmethod
    def _name_row(phone):
        return {"id": phone.id or "", "udid": phone.udid, "name": phone.name}

    def for_run(self, query):
        if query is None:
            return self.default()
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 128:
            raise ValueError("device must be a device's id, UDID or name")
        return self.find(query)  # a phone plugged in but not set up too: admission refuses it as not ready

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
        if phone.kind == "usb":
            if action == "build" and set(body) <= {"team"}:
                target = phone if phone.primary else self.adopt(phone.udid)
                target.manager.start_build(body.get("team"))
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
            elif action == "photos" and not body:
                (phone.setup or self.runtime.setup).allow_photos(phone.id)
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
