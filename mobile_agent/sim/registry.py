"""The registry of simulators Mobster created (`simulators.json`), and their port slots.

The registry is the list of UDIDs Mobster may ever change or delete: a simulator missing from it is never
touched, whatever its name. Every edit is a read, change and write under an exclusive fcntl lock on
`simulators.lock`, and the write is atomic (a temporary file in the same folder, then a rename), so two
processes never lose each other's edits and a crash never leaves half a file.
"""

import contextlib
import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import tempfile
import time

from .simctl import sim_error

REGISTRY = "simulators.json"
LOCK = "simulators.lock"
FIELDS = ("udid", "name", "device_type", "runtime", "wda_port", "mjpeg_port", "created_at", "last_used_at")
LOCK_TIMEOUT = 30.0
LOCK_POLL = .05

DEFAULT_PORT_BASE = 8310
MJPEG_OFFSET = 1000
SLOTS = 40
# Never these: the USB iPhone's relay (8100, 9100), the Mac app's API (8765), and the ranges the benchmark
# simulators use (8200–8299 and their MJPEG 9200–9299).
RESERVED = frozenset({8100, 8765, 9100, *range(8200, 8300), *range(9200, 9300)})


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Registry:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / REGISTRY
        self.lock_path = self.data_dir / LOCK

    @contextlib.contextmanager
    def locked(self, timeout=LOCK_TIMEOUT):
        """Hold the registry lock. Another process's edit takes well under a second; a lock held past
        ``timeout`` is a stuck process, reported as busy."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            end = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) or time.monotonic() >= end:
                        raise sim_error("busy", f"Another Mobster process has held {self.lock_path} for "
                                                f"{timeout:g} s.", "Wait for it to finish, then try again.") from None
                    time.sleep(LOCK_POLL)
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the lock

    def read(self):
        """The entries, oldest first. A missing file is an empty registry; a damaged one is an error, never
        an empty list: forgetting a UDID would leave its simulator behind for good."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return []
        try:
            data = json.loads(text)
            entries = data["simulators"] if isinstance(data, dict) else None
            if not isinstance(entries, list) or not all(isinstance(entry, dict) and entry.get("udid")
                                                        for entry in entries):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise sim_error("simulator", f"{self.path} is damaged, so Mobster can't tell which simulators "
                                         "are its own.",
                            "Delete Mobster's simulators in Xcode (their names start with “Mobster · ”), then "
                            f"remove {self.path}.") from None
        return [normalize(entry) for entry in entries]

    def write(self, entries):
        """Replace the file atomically. Call it under ``locked()``."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "simulators": [normalize(entry) for entry in entries]},
                             indent=2, ensure_ascii=False) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".simulators.", suffix=".json", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    @contextlib.contextmanager
    def edit(self):
        """Read, let the caller change the list in place, and write it back if it changed, all under the lock."""
        with self.locked():
            entries = self.read()
            before = json.dumps(entries, sort_keys=True)
            yield entries
            if json.dumps(entries, sort_keys=True) != before:
                self.write(entries)


def normalize(entry):
    """Exactly the registry's fields, in order."""
    return {field: entry.get(field) for field in FIELDS}


# -- ports -------------------------------------------------------------------------------------------------

def port_base(environ=None):
    """$MOBSTER_SIM_PORT_BASE, default 8310."""
    environ = os.environ if environ is None else environ
    text = str(environ.get("MOBSTER_SIM_PORT_BASE") or "").strip()
    if not text:
        return DEFAULT_PORT_BASE
    try:
        base = int(text)
    except ValueError:
        base = -1
    if not 1024 <= base <= 65535 - MJPEG_OFFSET - SLOTS:
        raise sim_error("environment", f"MOBSTER_SIM_PORT_BASE is {text!r}; it must be a port from 1024 to "
                                       f"{65535 - MJPEG_OFFSET - SLOTS}.", "Unset it to use 8310.")
    return base


def slot_ports(base, slot):
    """(WebDriverAgent port, MJPEG port) of a slot: base + slot, and that plus 1000."""
    wda = base + slot
    return wda, wda + MJPEG_OFFSET


def allowed(wda, mjpeg):
    return wda not in RESERVED and mjpeg not in RESERVED and 1024 <= wda and mjpeg <= 65535


def listening(port, host="127.0.0.1", timeout=.2):
    """Whether something accepts connections on this Mac's loopback at ``port``."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def free_slot(base, taken=(), busy=listening):
    """The first slot's (WDA, MJPEG) ports that no registry entry holds, no rule reserves, and nothing
    listens on. ``taken`` is the ports the registry already gives out."""
    taken = set(taken)
    for slot in range(SLOTS):
        wda, mjpeg = slot_ports(base, slot)
        if not allowed(wda, mjpeg) or wda in taken or mjpeg in taken:
            continue
        if busy(wda) or busy(mjpeg):
            continue
        return wda, mjpeg
    last = slot_ports(base, SLOTS - 1)[0]
    raise sim_error("simulator", f"Every port from {base} to {last} is in use or reserved, so there is no "
                                 "room for another simulator's WebDriverAgent.",
                    "Delete unused ones with `mobster sim prune`, or set MOBSTER_SIM_PORT_BASE to a free range.")


def taken_ports(entries, skip_udid=None):
    ports = set()
    for entry in entries:
        if entry.get("udid") == skip_udid:
            continue
        for key in ("wda_port", "mjpeg_port"):
            if entry.get(key):
                ports.add(int(entry[key]))
    return ports
