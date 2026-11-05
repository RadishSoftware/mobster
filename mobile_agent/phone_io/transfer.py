"""Transfers: photos, videos and files between this Mac and an iPhone, both ways, over every connection Mobster has,
checked by count and checksum, with progress. `mobster phone photos`/`file`, MCP's send_media and pull_media, the Mac
app (POST /api/transfers) and the content queue all use it.

Routes, in the order ``auto`` tries them:

| Direction | What | Route (``via``) | How | Checked by |
|---|---|---|---|---|
| to the iPhone | photos, videos | ``cable`` / ``wifi`` | Mobster's runner (photos.send) on a USB or Wi-Fi phone | SHA-256 on the phone, the library's count |
| | | ``airdrop`` | AirDrop to the phone by its name (handoff.airdrop) | the count, and SHA-256 on the phone where it can read it |
| | | ``mirroring`` | a drop on the iPhone Mirroring window, Photos in front | the same |
| | | ``icloud`` | this Mac's Photos library, then iCloud Photos (icloud.py); only when chosen | the same, once it arrives |
| | files | ``cable`` | an app's Documents folder, or a debug build's own folder (files.py) | size, then SHA-256 read back |
| to this Mac | photos, videos | ``cable`` | the camera roll's files (photos.CameraRoll, AFC) | size against the phone's listing |
| | | ``wifi`` | the runner reads the originals (photos.export) | SHA-256 on the phone against this Mac's |
| | | ``tunnel`` | Apple's own tunnel, no cable (tunnel.py, pymobiledevice3 if installed) | nothing is compared; the SHA-256 of what arrived is kept |
| | | ``icloud`` | this Mac's Photos library (icloud.py); only when chosen | not checked |
| | files | ``cable`` | files.py get | size against the phone's listing |

Rules every route follows:

- **One route per transfer.** ``auto`` moves to the next route only when the one before refused before anything was
  sent (not set up, no cable, a helper missing). Once a route sent anything, it is never tried again on another route:
  an outcome that is unknown stays unknown ("Look in Photos before you send it again"), so nothing arrives twice.
- **Checked.** Each item ends ``verified`` (SHA-256 matches on both sides), ``counted`` (the library grew by the
  number sent, or a pulled file's size matched the phone's listing, but no checksum was compared), ``sent`` (the route
  said it went; nothing on the phone could be read or compared) or ``failed``. The transfer keeps the library's count
  before and after.
- **Each transfer names one phone.** Several phones run their transfers at the same time; one phone takes one at a
  time (``Transfers``' per-phone lock).
"""  # noqa: E501

import contextlib
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import PhoneIOError, checked_request

DIRECTIONS = ("to_phone", "to_mac")
WHAT = ("photos", "files")
VIA = ("auto", "cable", "wifi", "airdrop", "mirroring", "icloud", "tunnel")
ROUTES = {
    ("to_phone", "photos"): ("cable", "wifi", "airdrop", "mirroring", "icloud"),
    ("to_phone", "files"): ("cable",),
    ("to_mac", "photos"): ("cable", "wifi", "tunnel", "icloud"),
    ("to_mac", "files"): ("cable",),
}
# Routes ``auto`` never picks: they are slow or reach outside the two devices, so the person chooses them.
CHOSEN_ONLY = frozenset({"icloud"})
# Codes that mean a route refused before sending anything: auto may try the next one.
NOT_SENT = frozenset({"needs_cable", "no_device", "tools", "needs_rebuild", "no_mirroring", "permission",
                      "airdrop_not_found", "airdrop_ambiguous", "not_ready", "usage_route"})
ARRIVAL_WAIT_S = {"airdrop": 60, "mirroring": 60, "icloud": 600}
POLL_S = 2.0
MAX_ITEMS = 50
KEEP = 50
ALBUM = "Mobster"


@dataclass
class Phone:
    """One iPhone as a transfer reaches it."""
    record: dict
    runner: Optional[Callable[[], Any]] = None
    files: Optional[Callable[..., Any]] = None
    roll: Optional[Callable[..., Any]] = None
    tunnel: Optional[Callable[..., Any]] = None
    handoff: Any = None
    icloud: Any = None
    mirroring: Optional[Callable[..., Any]] = None

    @property
    def id(self):
        return self.record.get("id") or self.record.get("udid") or self.name

    @property
    def name(self):
        return self.record.get("name") or self.record.get("id") or "the iPhone"

    @property
    def cabled(self):
        return self.record.get("kind") == "usb" and self.record.get("transport") != "wifi"

    def phone_files(self, container=False):
        if self.files is not None:
            return self.files(self.record, container=container)
        from .files import PhoneFiles
        return PhoneFiles(self.record, container=container)

    def camera_roll(self):
        if self.roll is not None:
            return self.roll(self.record)
        from .photos import CameraRoll
        return CameraRoll(self.phone_files())

    def tunnel_roll(self):
        if self.tunnel is not None:
            return self.tunnel(self.record)
        from .tunnel import TunnelRoll
        return TunnelRoll(self.record)

    def handoffs(self):
        if self.handoff is not None:
            return self.handoff
        from . import handoff
        return handoff

    def cloud(self):
        if self.icloud is not None:
            return self.icloud
        from . import icloud
        return icloud


def phone_from_record(record):
    """A Phone for a devices.py record outside the Mac app; its runner holds the device's lease while open."""
    from . import wda

    @contextlib.contextmanager
    def runner():
        with wda(record) as request:
            yield request
    return Phone(record, runner=runner if record.get("wdaUrl") else None)


def phone_from_runtime(runtime, device=None):
    """A Phone for a device of the running Mobster; its runner is refused while a task runs on that phone."""
    import json
    import urllib.request

    from ..journal import JournalError, Lease, LeaseHeld

    fleet_phone = runtime.resolve_device(device)
    record = {"id": fleet_phone.id, "kind": fleet_phone.kind, "udid": fleet_phone.udid,
              "name": fleet_phone.name or fleet_phone.id, "transport": getattr(fleet_phone, "transport", None),
              "wdaUrl": fleet_phone.wda_url}

    @contextlib.contextmanager
    def runner():
        def check_busy():
            if getattr(runtime, "closing", False):
                raise PhoneIOError("Mobster is closing; the transfer did not start.", "device_busy")
            if runtime.device_busy(fleet_phone.id):
                raise PhoneIOError(f"{record['name']} is busy with a task.", "device_busy", "Try again when it ends.")
            if fleet_phone.wda_url != record["wdaUrl"]:
                raise PhoneIOError(f"{record['name']}'s connection changed before the transfer started.",
                                   "device_busy", "Try the transfer again on its current connection.")

        check_busy()
        url = (record["wdaUrl"] or "").rstrip("/")
        if not url:
            raise PhoneIOError(f"{record['name']} isn't set up yet.", "no_device", "Set it up in Mobster.")
        try:
            held = Lease.device(url, wait=0, udid=record["udid"])
        except (LeaseHeld, JournalError) as error:
            raise PhoneIOError(f"{record['name']} is busy or its device lock is unavailable: {error}",
                               "device_busy", "Try again when its task ends.") from None
        try:
            check_busy()  # Admission may have changed while the cross-process lease was being acquired.

            def send(method, path, body, timeout):
                data = json.dumps(body).encode() if body is not None else None
                req = urllib.request.Request(f"{url}/session/{fleet_phone.wda_session()}{path}", data=data,
                                             method=method, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    return json.load(response)

            yield checked_request(send)
        finally:
            held.close()
    return Phone(record, runner=runner if fleet_phone.wda_url else None)


@dataclass
class Item:
    name: str
    bytes: int = 0
    sent: int = 0
    state: str = "queued"          # queued | sending | checking | verified | counted | sent | failed | unknown
    kind: Optional[str] = None
    sha256: Optional[str] = None
    path: Optional[str] = None     # the Mac file (to the iPhone: the source; to this Mac: where it landed)
    remote: Optional[str] = None   # where it is on the phone: a Photos id or a path, when known
    error: Optional[str] = None

    def public(self):
        return {"name": self.name, "bytes": self.bytes, "sent": self.sent, "state": self.state, "kind": self.kind,
                "sha256": self.sha256, "path": self.path, "remote": self.remote, "error": self.error}


@dataclass
class Transfer:
    """One transfer: what moves, which way, to or from which phone, the route it took and how each item ended."""
    direction: str
    what: str
    phone: Phone
    via: str = "auto"
    paths: list = field(default_factory=list)        # to the iPhone: the Mac files
    folder: Optional[str] = None                     # to this Mac: where files land
    count: int = 1                                   # to this Mac, photos: how many of the newest
    since: Optional[float] = None
    names: list = field(default_factory=list)        # to this Mac, files: the names in the app's folder
    app: Optional[str] = None                        # files: the app's bundle ID
    container: bool = False
    album: Optional[str] = ALBUM
    convert_heic: bool = False
    date_now: bool = False                           # to the iPhone over the runner: first in Recents (the queue)
    queue_item: Optional[str] = None                 # the content queue's post this transfer is for
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = "queued"                            # queued | running | done | failed | unknown
    route: Optional[str] = None
    items: list = field(default_factory=list)
    before: Optional[int] = None                     # the phone's photos and videos, before and after
    after: Optional[int] = None
    error: Optional[dict] = None
    tried: list = field(default_factory=list)        # routes that refused before sending: [{"via", "why"}]
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    listeners: list = field(default_factory=list)

    def public(self):
        done = sum(i.sent for i in self.items)
        total = sum(i.bytes for i in self.items)
        return {"id": self.id, "direction": self.direction, "what": self.what, "device": self.phone.id,
                "deviceName": self.phone.name, "via": self.via, "route": self.route, "state": self.state,
                "items": [i.public() for i in self.items], "bytes": total, "sent": done,
                "before": self.before, "after": self.after, "error": self.error, "tried": list(self.tried),
                "album": self.album if self.direction == "to_phone" and self.what == "photos" else None,
                "app": self.app, "folder": self.folder, "queueItem": self.queue_item,
                "createdAt": round(self.created_at * 1000),
                "finishedAt": round(self.finished_at * 1000) if self.finished_at else None,
                "verified": bool(self.items) and all(i.state == "verified" for i in self.items)}

    def changed(self):
        for listener in list(self.listeners):
            try:
                listener(self)
            except Exception:  # noqa: BLE001 - a listener never stops a transfer
                pass


def validate(transfer):
    if transfer.direction not in DIRECTIONS or transfer.what not in WHAT:
        raise PhoneIOError("A transfer goes to_phone or to_mac, with photos or files.", "usage")
    if transfer.via not in VIA or (transfer.via != "auto" and transfer.via not in
                                   ROUTES[(transfer.direction, transfer.what)]):
        allowed = ", ".join(ROUTES[(transfer.direction, transfer.what)])
        raise PhoneIOError(f"That route doesn't move {transfer.what} that way. Choose auto or one of: {allowed}.",
                           "usage")
    if transfer.direction == "to_phone":
        if not transfer.paths or len(transfer.paths) > MAX_ITEMS:
            raise PhoneIOError(f"Name 1 to {MAX_ITEMS} files to send.", "usage")
        for path in transfer.paths:
            if not Path(path).expanduser().is_file():
                raise PhoneIOError(f"There is no file at {path}.", "usage")
    if transfer.what == "files" and not transfer.app:
        raise PhoneIOError("Name the app whose folder the files go to or come from (its bundle ID).", "usage")
    if transfer.direction == "to_mac" and not transfer.folder:
        raise PhoneIOError("Name the folder on this Mac the files go to.", "usage")
    if transfer.album is not None and (not isinstance(transfer.album, str) or len(transfer.album) > 100):
        raise PhoneIOError("An album's name is at most 100 characters.", "usage")


def _candidates(transfer):
    order = ROUTES[(transfer.direction, transfer.what)]
    if transfer.via != "auto":
        return [transfer.via]
    return [route for route in order if route not in CHOSEN_ONLY]


def _ready(transfer, route):
    """None when ``route`` can run now, else why not (a sentence)."""
    phone = transfer.phone
    if phone.record.get("kind") == "simulator" and route != "cable":
        return "a simulator takes files only from this Mac directly"
    if route == "cable":
        if transfer.direction == "to_phone" and transfer.what == "photos":
            if phone.record.get("kind") == "simulator":
                return None
            return None if phone.cabled and phone.runner is not None else (
                "it isn't on a cable" if not phone.cabled else "Mobster's runner isn't set up on it")
        return None if phone.cabled else "it isn't on a cable"
    if route == "wifi":
        if phone.runner is None:
            return "Mobster's runner isn't set up on it"
        return None if not phone.cabled or transfer.via == "wifi" else "it's on the cable (the cable route goes first)"
    if route == "tunnel":
        from .tunnel import tool
        return None if phone.tunnel is not None or tool() else "pymobiledevice3 isn't installed"
    if route == "mirroring":
        if phone.mirroring is not None:
            return None
        if not _mirrors(phone):
            return "this Mac isn't set to mirror it (MOBSTER_MIRRORS names the iPhone it mirrors)"
        return None
    return None


def mirrored_phone():
    """The iPhone this Mac mirrors, as named in MOBSTER_MIRRORS: a drop on the wrong phone can't be undone."""
    value = os.environ.get("MOBSTER_MIRRORS", "").strip()
    return value or None


def _mirrors(phone):
    named = mirrored_phone()
    return named is not None and named in (phone.name, phone.record.get("udid"), phone.record.get("id"))


def run(transfer, *, clock=time.monotonic, sleep=time.sleep):
    """Run ``transfer`` to its end, updating it as it goes (``transfer.changed()`` after each step). Never raises for
    a phone's refusal: the transfer ends ``failed`` with ``error``. Returns the transfer."""
    validate(transfer)
    transfer.state = "running"
    transfer.changed()
    last = None
    for route in _candidates(transfer):
        why = _ready(transfer, route)
        if why is not None:
            transfer.tried.append({"via": route, "why": why})
            last = PhoneIOError(f"{transfer.phone.name}: {why}.", "not_ready")
            continue
        transfer.route = route
        transfer.items = []
        try:
            _ROUTERS[(transfer.direction, transfer.what)](transfer, route, clock, sleep)
        except PhoneIOError as error:
            if error.code == "device_busy":
                _end(transfer, error)
                return transfer
            # A NOT_SENT code is raised only before anything went, and a copy to this Mac changes nothing on the
            # phone: unless an item already arrived, try the next route.
            if (error.code in NOT_SENT or transfer.direction == "to_mac") and not any(
                    i.sent or i.state in ("verified", "counted", "sent", "unknown") for i in transfer.items):
                transfer.tried.append({"via": route, "why": str(error)})
                last = error
                if transfer.via == "auto":
                    continue
            _end(transfer, error)
            return transfer
        transfer.state = "done" if all(i.state in ("verified", "counted", "sent") for i in transfer.items) else "failed"
        if transfer.state == "failed" and any(i.state == "unknown" for i in transfer.items):
            transfer.state = "unknown"
        transfer.finished_at = time.time()
        transfer.changed()
        return transfer
    _end(transfer, last or PhoneIOError("No route reaches that iPhone right now.", "not_ready"))
    return transfer


def _end(transfer, error):
    transfer.state = "unknown" if error.code == "unknown" else "failed"
    transfer.error = error.public()
    for item in transfer.items:
        if item.state in ("queued", "sending", "checking"):
            item.state = "unknown" if error.code == "unknown" and item.state != "queued" else "failed"
            item.error = item.error or str(error)
    transfer.finished_at = time.time()
    transfer.changed()


def _items_from_paths(transfer):
    from .photos import digest, sniff
    items = []
    for path in transfer.paths:
        file = Path(path).expanduser()
        kind, _ = sniff(file) if transfer.what == "photos" else (None, None)
        items.append(Item(name=file.name, bytes=file.stat().st_size, kind=kind, sha256=digest(file),
                          path=str(file.resolve())))
    transfer.items = items
    transfer.changed()
    return items


def _library_count(request):
    from .photos import count
    try:
        value = count(request)
    except PhoneIOError:
        return None
    if not value or "photos" not in value:
        return None
    return value["photos"] + value["videos"]


def _photos_to_phone(transfer, route, clock, sleep):
    items = _items_from_paths(transfer)
    phone = transfer.phone
    if route in ("cable", "wifi"):
        if phone.record.get("kind") == "simulator":
            return _simulator_add(transfer)
        from .photos import send
        with phone.runner() as request:
            transfer.before = _library_count(request)
            transfer.changed()
            for item in items:
                item.state = "sending"
                transfer.changed()

                def progress(sent, total, item=item):
                    item.sent = sent
                    transfer.changed()
                try:
                    result = send(request, item.path, name=phone.name, album=transfer.album, progress=progress,
                                  sha256=item.sha256, date_now=transfer.date_now,
                                  activate=lambda bundle: request("POST", "/wda/apps/activate", {"bundleId": bundle},
                                                                  15))
                except PhoneIOError as error:
                    item.state = "unknown" if error.code == "unknown" else "failed"
                    item.error = str(error)
                    if item is items[0] and error.code in NOT_SENT:
                        raise
                    if error.code in ("unknown",):
                        raise
                    continue
                item.sent, item.remote = item.bytes, result.get("localIdentifier")
                item.state = "verified" if result.get("verified") else "sent"
                if result.get("albumError"):
                    item.error = result["albumError"]
                transfer.changed()
            transfer.after = _library_count(request)
            _settle_by_count(transfer)
        return
    # AirDrop, Mirroring and iCloud hand the files over and can't say whether the phone kept them: count before and
    # after (the runner, else the cable's camera roll), then read each new item's checksum where it can.
    watch = _Watch(transfer)
    transfer.before = watch.count()
    transfer.changed()
    for item in items:
        item.state = "sending"
    transfer.changed()
    paths = [item.path for item in items]
    if route == "airdrop":
        accepting = threading.Event()
        watcher = threading.Thread(target=_accept_airdrop, args=(phone, accepting, sleep), daemon=True)
        watcher.start()
        try:
            phone.handoffs().airdrop(paths, phone.name)
        finally:
            accepting.set()
            watcher.join(timeout=5)
    elif route == "mirroring":
        _mirroring_photos(transfer, paths)
    else:
        phone.cloud().import_media(paths, album=transfer.album or ALBUM)
    for item in items:
        item.sent, item.state = item.bytes, "checking"
    transfer.changed()
    watch.wait_for(len(items), ARRIVAL_WAIT_S[route], clock, sleep)
    watch.check(items)
    transfer.changed()


def _accept_airdrop(phone, done, sleep=time.sleep, poll=1.0):
    """While Mobster's own AirDrop is on its way, tap Accept on an alert whose text says AirDrop, and nothing else.
    Not journaled: a transfer has no run, so the tap is bounded here (the alert's text, its Accept, the send)."""
    if phone.runner is None:
        return
    try:
        with phone.runner() as request:
            while not done.is_set():
                try:
                    text = request("GET", "/alert/text", None, 5)
                    if isinstance(text, str) and "airdrop" in text.casefold():
                        buttons = request("GET", "/wda/alert/buttons", None, 5)
                        if isinstance(buttons, list) and "Accept" in buttons:
                            request("POST", "/alert/accept", {"name": "Accept"}, 10)
                            return
                except PhoneIOError:
                    pass
                sleep(poll)
    except PhoneIOError:
        return


def _mirroring_photos(transfer, paths):
    """Photos in front on the phone (Spotlight, through Mirroring), then the drop in the middle of its window."""
    phone = transfer.phone
    if phone.mirroring is not None:
        phone.mirroring(paths)
        return
    open_on_mirroring("Photos")
    phone.handoffs().mirroring_drop(paths, 0.5, 0.55)


def open_on_mirroring(app_name, helper=None, sleep=time.sleep):
    """Open ``app_name`` on the mirrored iPhone with Spotlight, as Mirroring's driver opens apps."""
    from ..mirroring import Helper, MirroringError
    own = helper is None
    try:
        helper = helper or Helper()
    except Exception as error:  # noqa: BLE001 - no helper, no Xcode tools
        raise PhoneIOError(f"Mobster's Mirroring helper couldn't start ({type(error).__name__}).",
                           "no_mirroring") from None
    try:
        state = helper.request("state")
        if state.get("state") != "ready":
            raise PhoneIOError("iPhone Mirroring isn't showing the iPhone.", "no_mirroring",
                               "Open iPhone Mirroring, lock the iPhone and keep it near this Mac, then try again.")
        helper.request("key", combo="cmd+3")
        sleep(0.8)
        helper.request("type", text=app_name, mode="keys")
        sleep(1.0)
        helper.request("key", combo="return")
        sleep(1.5)
    except MirroringError as error:
        raise PhoneIOError(f"iPhone Mirroring refused: {error}.", "no_mirroring") from None
    finally:
        if own:
            try:
                helper.request("restore", timeout=3)
            except Exception:  # noqa: BLE001
                pass
            helper.close()


def _settle_by_count(transfer):
    """Items sent but not checked by checksum become ``counted`` when the library grew by exactly as many."""
    if transfer.before is None or transfer.after is None:
        return
    sent = [i for i in transfer.items if i.state in ("verified", "sent")]
    grew = transfer.after - transfer.before
    for item in sent:
        if item.state == "sent" and grew == len(sent):
            item.state = "counted"
    if grew != len(sent):
        for item in sent:
            item.error = item.error or (f"The library grew by {grew}, not {len(sent)}: look in Photos on "
                                        f"{transfer.phone.name}.")


def _simulator_add(transfer):
    import subprocess

    from ..journal import JournalError, Lease, LeaseHeld
    from ..native_helpers import require_devices
    record = transfer.phone.record
    url = record.get("wdaUrl")
    if not url:
        raise PhoneIOError(f"{transfer.phone.name} has no device lock identity.", "no_device")
    udid = record.get("udid") or record.get("id")
    if not isinstance(udid, str) or not udid:
        raise PhoneIOError(f"{transfer.phone.name} has no simulator identity.", "no_device")
    try:
        held = Lease.device(url, wait=0, udid=udid)
    except (LeaseHeld, JournalError) as error:
        raise PhoneIOError(f"{transfer.phone.name} is busy or its device lock is unavailable: {error}",
                           "device_busy", "Try again when its task ends.") from None
    try:
        try:
            require_devices()
        except RuntimeError as error:
            raise PhoneIOError(str(error), "tools") from None
        for item in transfer.items:
            item.state = "sending"
            transfer.changed()
            try:
                done = subprocess.run(["xcrun", "simctl", "addmedia", udid, item.path], capture_output=True,
                                      text=True, timeout=120)
            except (OSError, subprocess.SubprocessError) as error:
                raise PhoneIOError(f"The simulator import may have happened ({type(error).__name__}).",
                                   "unknown", "Check Photos before sending it again.") from None
            if done.returncode != 0:
                raise PhoneIOError(f"The simulator import may have happened: "
                                   f"{(done.stderr or done.stdout).strip()[:160]}", "unknown",
                                   "Check Photos before sending it again.")
            item.state, item.sent = "sent", item.bytes
            transfer.changed()
    finally:
        held.close()


class _Watch:
    """How many photos and videos the phone has, and which arrived."""

    def __init__(self, transfer):
        self.transfer = transfer
        self.names_before = None

    def _runner_count(self):
        phone = self.transfer.phone
        if phone.runner is None:
            return None
        try:
            with phone.runner() as request:
                return _library_count(request)
        except PhoneIOError:
            return None

    def count(self):
        found = self._runner_count()
        if found is not None:
            self.how = "runner"
            return found
        if self.transfer.phone.cabled:
            try:
                media = self.transfer.phone.camera_roll().media()
            except PhoneIOError:
                media = None
            if media is not None:
                self.how, self.names_before = "cable", dict(media)
                return len(media)
        self.how = None
        return None

    def wait_for(self, grew, limit, clock, sleep):
        transfer = self.transfer
        if transfer.before is None:
            return
        deadline = clock() + limit
        while True:
            now = self._again()
            transfer.after = now
            transfer.changed()
            if now is not None and now - transfer.before >= grew or clock() >= deadline:
                return
            sleep(POLL_S)

    def _again(self):
        if self.how == "runner":
            return self._runner_count()
        if self.how == "cable":
            try:
                self.names_after = self.transfer.phone.camera_roll().media()
            except PhoneIOError:
                return None
            return len(self.names_after)
        return None

    def check(self, items):
        """Each item ``verified`` when a new file on the phone has its checksum, else ``counted`` or ``sent``."""
        transfer = self.transfer
        grew = None if transfer.before is None or transfer.after is None else transfer.after - transfer.before
        matched = set()
        if self.how == "cable" and getattr(self, "names_after", None) is not None:
            new = {path: size for path, size in self.names_after.items() if path not in (self.names_before or {})}
            roll = transfer.phone.camera_roll()
            import tempfile

            from .photos import digest
            with tempfile.TemporaryDirectory(prefix="mobster-check-") as scratch:
                for item in items:
                    for path, size in new.items():
                        if path in matched or size != item.bytes:
                            continue
                        try:
                            local = roll.fetch(path, scratch, size)
                        except PhoneIOError:
                            continue
                        if digest(local) == item.sha256:
                            matched.add(path)
                            item.state, item.remote = "verified", path
                            break
        elif self.how == "runner":
            matched = self._runner_checks(items, grew)
        for item in items:
            if item.state == "verified":
                continue
            if grew is not None and grew >= len(items):
                item.state = "counted"
            elif grew is None:
                item.state = "sent"
                item.error = item.error or f"Mobster couldn't read {transfer.phone.name}'s library to check it."
            else:
                item.state = "unknown"
                item.error = (f"{transfer.phone.name}'s library grew by {grew} of {len(items)}. Look in Photos "
                              "before you send it again.")

    def _runner_checks(self, items, grew):
        from .photos import checksum, recent
        phone = self.transfer.phone
        found = set()
        try:
            with phone.runner() as request:
                newest = recent(request, max(len(items), grew or 0) + 2, name=phone.name)
                for item in items:
                    for candidate in newest:
                        if candidate["id"] in found:
                            continue
                        answer = checksum(request, candidate["id"], phone.name)
                        if answer and answer.get("sha256") == item.sha256:
                            found.add(candidate["id"])
                            item.state, item.remote = "verified", candidate["id"]
                            break
        except PhoneIOError:
            pass
        return found


def _files_to_phone(transfer, route, clock, sleep):
    import tempfile

    from .photos import digest
    items = _items_from_paths(transfer)
    phone_files = transfer.phone.phone_files(container=transfer.container)
    for item in items:
        item.state = "sending"
        transfer.changed()
        try:
            put = phone_files.put(transfer.app, item.path)
        except PhoneIOError as error:
            item.state, item.error = "failed", str(error)
            if item is items[0] and error.code in NOT_SENT:
                raise
            continue
        item.sent, item.remote, item.state = item.bytes, put["path"], "checking"
        transfer.changed()
        with tempfile.TemporaryDirectory(prefix="mobster-check-") as scratch:
            try:
                back = phone_files.get(transfer.app, put["name"], Path(scratch))
                item.state = "verified" if digest(back) == item.sha256 else "failed"
                if item.state == "failed":
                    item.error = f"{put['name']} on {transfer.phone.name} isn't the same as this Mac's copy."
            except PhoneIOError:
                item.state = "counted"  # its size matched (files.put checks it); the read-back didn't run
        transfer.changed()


def _photos_to_mac(transfer, route, clock, sleep):
    folder = Path(transfer.folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    before = len([p for p in folder.iterdir() if p.is_file()])
    phone = transfer.phone
    if route == "wifi" or (route == "cable" and phone.record.get("kind") == "simulator"):
        from .photos import export, recent
        with phone.runner() as request:
            chosen = recent(request, transfer.count, transfer.since, name=phone.name)
            transfer.items = [Item(name=c["filename"] or c["id"], kind=c["kind"], remote=c["id"]) for c in chosen]
            transfer.changed()
            for item, picked in zip(transfer.items, chosen):
                item.state = "sending"

                def progress(sent, total, item=item):
                    item.sent, item.bytes = sent, total
                    transfer.changed()
                try:
                    got = export(request, picked, folder, progress=progress, name=phone.name)
                except PhoneIOError as error:
                    item.state, item.error = "failed", str(error)
                    if item is transfer.items[0] and error.code in NOT_SENT:
                        raise
                    continue
                item.path, item.sha256, item.bytes, item.sent = got["path"], got["sha256"], got["bytes"], got["bytes"]
                item.state = "verified"
                transfer.changed()
    else:
        if route == "cable":
            got = phone.camera_roll().pull(folder, count=transfer.count, since=transfer.since,
                                           convert_heic=transfer.convert_heic)
        elif route == "tunnel":
            got = phone.tunnel_roll().pull(folder, count=transfer.count)
        else:
            exported = phone.cloud().export_recent(folder, count=transfer.count)
            from .photos import digest
            got = [{"path": p, "name": Path(p).name, "bytes": Path(p).stat().st_size, "sha256": digest(p),
                    "kind": None} for p in exported["paths"]]
        transfer.items = [Item(name=g["name"], bytes=g["bytes"], sent=g["bytes"], kind=g.get("kind"),
                               sha256=g.get("sha256"), path=g["path"], remote=g.get("remote"),
                               state=_PULL_STATE[route]) for g in got]
    after = len([p for p in folder.iterdir() if p.is_file()])
    transfer.before, transfer.after = before, after
    if after - before != sum(1 for i in transfer.items if i.path):
        for item in transfer.items:
            item.error = item.error or "The folder's count didn't grow by as many as arrived."
    transfer.changed()


def _files_to_mac(transfer, route, clock, sleep):
    from .photos import digest
    folder = Path(transfer.folder).expanduser()
    phone_files = transfer.phone.phone_files(container=transfer.container)
    listing = {e["name"]: e for e in phone_files.ls(transfer.app)}
    names = transfer.names or []
    if not names:
        raise PhoneIOError("Name the files to copy.", "usage")
    transfer.items = [Item(name=n, bytes=(listing.get(n) or {}).get("bytes") or 0) for n in names]
    transfer.changed()
    for item in transfer.items:
        item.state = "sending"
        transfer.changed()
        try:
            saved = phone_files.get(transfer.app, item.name, folder)
        except PhoneIOError as error:
            item.state, item.error = "failed", str(error)
            continue
        item.path, item.sha256, item.sent = str(saved), digest(saved), saved.stat().st_size
        item.state = "counted" if item.sent == item.bytes else "failed"
        transfer.changed()


# What a pull to this Mac proves: the cable compared the file's size with the phone's listing; the tunnel and iCloud
# compared nothing, so only the wifi route (photos.export) ever reaches ``verified``.
_PULL_STATE = {"cable": "counted", "tunnel": "sent", "icloud": "sent"}

_ROUTERS = {("to_phone", "photos"): _photos_to_phone, ("to_phone", "files"): _files_to_phone,
            ("to_mac", "photos"): _photos_to_mac, ("to_mac", "files"): _files_to_mac}


class Transfers:
    """The running Mobster's transfers: one at a time per phone, phones in parallel; the newest ``KEEP`` are listed."""

    def __init__(self, runner=run):
        self._run = runner
        self._lock = threading.Lock()
        self._phone_locks = {}
        self._all = {}

    def start(self, transfer, wait=False):
        validate(transfer)
        with self._lock:
            self._all[transfer.id] = transfer
            while len(self._all) > KEEP:
                oldest = min(self._all.values(), key=lambda t: t.created_at)
                if oldest.state in ("queued", "running"):
                    break
                self._all.pop(oldest.id)
            lock = self._phone_locks.setdefault(transfer.phone.id, threading.Lock())

        def work():
            with lock:
                self._run(transfer)
        if wait:
            work()
            return transfer
        threading.Thread(target=work, name=f"mobster-transfer-{transfer.id}", daemon=True).start()
        return transfer

    def get(self, transfer_id):
        with self._lock:
            return self._all.get(transfer_id)

    def all(self):
        with self._lock:
            return sorted(self._all.values(), key=lambda t: t.created_at, reverse=True)


def transfers_for(runtime):
    """The runtime's Transfers, made on first use."""
    if getattr(runtime, "transfers", None) is None:
        runtime.transfers = Transfers()
    return runtime.transfers


def target(runtime=None, *, device=None, profile=None, records=None):
    """The phone a transfer goes to."""
    if profile is not None:
        from ..accounts.store import AccountNotFound, AmbiguousAccount, resolve, store_for
        try:
            account = resolve(store_for(runtime).all(), profile)
        except (AccountNotFound, AmbiguousAccount) as error:
            raise PhoneIOError(str(error), "usage") from None
        wanted = account.get("device")
        if device is not None and wanted and device != wanted:
            raise PhoneIOError(f"@{account['handle']} is on {account.get('deviceName') or 'another phone'}. Leave "
                               "the phone out.", "usage")
        device = wanted or device
    if runtime is not None:
        return phone_from_runtime(runtime, device)
    from . import choose_device
    return phone_from_record(choose_device(device, records))


def summary(transfer):
    """One sentence for the CLI and MCP: what moved, where, by which route, and how it was checked."""
    public = transfer.public()
    items = transfer.items
    verified = sum(1 for i in items if i.state == "verified")
    counted = sum(1 for i in items if i.state == "counted")
    sent = sum(1 for i in items if i.state == "sent")
    words = {"cable": "over the cable", "wifi": "over Wi-Fi", "airdrop": "by AirDrop", "mirroring":
             "through iPhone Mirroring", "icloud": "through iCloud Photos", "tunnel": "over Apple's tunnel"}
    where = (f"to {transfer.phone.name}" if transfer.direction == "to_phone" else f"from {transfer.phone.name}")
    if transfer.state == "failed" and not items:
        return str((transfer.error or {}).get("message") or "Nothing moved.")
    parts = [f"Moved {verified + counted + sent} of {len(items)} {where} {words.get(transfer.route or '', '')}".strip()
             + "."]
    if verified:
        parts.append(f"{verified} checked by SHA-256.")
    if counted:
        parts.append(f"{counted} matched by size." if transfer.direction == "to_mac"
                     else f"{counted} counted in the library.")
    if sent:
        parts.append(f"{sent} not checked." if transfer.direction == "to_mac"
                     else f"{sent} not checked: Mobster couldn't read the phone.")
    if public["before"] is not None and public["after"] is not None and transfer.direction == "to_phone":
        parts.append(f"Photos and videos on the phone: {public['before']:,} before, {public['after']:,} after.")
    failed = [i for i in items if i.state in ("failed", "unknown")]
    if failed:
        parts.append(f"{len(failed)} didn't: {failed[0].error or failed[0].state}")
    return " ".join(parts)
