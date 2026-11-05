"""Android emulators Mobster boots: one lease per run, a cap like the iOS simulators', and only its own are stopped."""

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..sim.models import SimError, SimLease
from . import adb as adb_module
from .adb import Adb, AdbError

DEFAULT_MAX = 2
BOOT_TIMEOUT = 240
FIRST_PORT, LAST_PORT = 5554, 5584   # emulator console ports are even; adb sees emulator-<port>
EMULATOR_ARGS = ("-no-window", "-no-audio", "-no-snapshot-save", "-no-boot-anim", "-gpu", "swiftshader_indirect")
AVD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,63}")


@dataclass(frozen=True)
class AndroidTarget:
    serial: str
    name: str        # "Pixel_8" or the phone's model
    kind: str        # "emulator" or "usb"
    avd: str = ""


def _lease(identity):
    from ..journal import Lease
    return Lease.device(identity)


def _spawn(argv):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


class EmulatorManager:
    """``adb`` lists devices, ``spawn`` starts the emulator process, ``lease`` takes the per-device lock."""

    def __init__(self, data_dir, adb=None, run=adb_module.run_process, spawn=_spawn, lease=_lease, root=None,
                 sleep=time.sleep, clock=time.monotonic):
        self.data_dir = Path(data_dir).expanduser() / "android"
        self.root = root if root is not None else adb_module.sdk_root()
        self.adb = adb or Adb(run=run, binary=adb_module.tool("adb", self.root) or "adb")
        self._run, self._spawn, self._lease, self.sleep, self.clock = run, spawn, lease, sleep, clock

    @property
    def max_emulators(self):
        text = str(os.environ.get("MOBSTER_MAX_SIMS") or "").strip()
        if not text:
            return DEFAULT_MAX
        if not text.isdigit() or int(text) < 1:
            raise SimError("environment", f"MOBSTER_MAX_SIMS is {text!r}; it must be a whole number of 1 or more.",
                           "Unset it to use 2.")
        return int(text)

    # -- what exists

    def avds(self):
        emulator = adb_module.tool("emulator", self.root)
        if not emulator:
            return []
        result = self._run([emulator, "-list-avds"], 20, None)
        return [line.strip() for line in result.text.splitlines() if AVD_NAME.fullmatch(line.strip())]

    def avd_of(self, serial):
        result = Adb(serial, self._run, self.adb.binary).adb("emu", "avd", "name", timeout=10, check=False)
        lines = [line.strip() for line in result.text.splitlines() if line.strip() and line.strip() != "OK"]
        return lines[0] if lines else ""

    def devices(self):
        """Android devices as rows: serial, name, kind, state, avd, owned (a Mobster-booted emulator), in_use."""
        rows = []
        for device in self.adb.devices():
            avd = self.avd_of(device.serial) if device.kind == "emulator" and device.ready else ""
            identity = self._identity(device.serial, avd)
            rows.append({"platform": "android", "serial": device.serial, "name": avd or device.model or device.serial,
                         "kind": device.kind, "state": device.state, "avd": avd,
                         "owned": self._pid_file(avd).exists() if avd else False,
                         "in_use": self._in_use(identity)})
        return rows

    @staticmethod
    def _identity(serial, avd):
        return f"android:avd:{avd}" if avd else f"android:{serial}"

    def _in_use(self, identity):
        try:
            held = self._lease(identity)
        except Exception:
            return True
        held.close()
        return False

    def _pid_file(self, avd):
        return self.data_dir / f"{avd}.json"

    # -- acquire and release

    def acquire(self, avd=None, serial=None, timeout=BOOT_TIMEOUT):
        """A lease on an idle ready device: ``serial`` (a USB phone or running emulator), else a running emulator of
        ``avd``, else a booted one. SimError("busy") when everything is in use and the cap is reached."""
        rows = self.devices()
        if serial:
            rows = [row for row in rows if row["serial"] == serial]
            if not rows:
                raise SimError("environment", f"No Android device {serial} is connected.", "Run `adb devices -l`.")
        elif avd:
            rows = [row for row in rows if row["avd"] == avd]
        else:
            rows = [row for row in rows if row["kind"] == "emulator"]  # a USB phone is used only when named
        for row in rows:
            if row["state"] != "device":
                continue
            lease = self._try_lease(self._identity(row["serial"], row["avd"]))
            if lease is not None:
                target = AndroidTarget(row["serial"], row["name"], row["kind"], row["avd"])
                return SimLease(target, [lease])
        if serial:
            raise SimError("busy", f"{serial} is running a check.", "Wait for it to finish.")
        return self._boot_and_lease(avd, timeout)

    def _try_lease(self, identity):
        try:
            return self._lease(identity)
        except Exception:
            return None

    def _boot_and_lease(self, avd, timeout):
        running = [row for row in self.devices() if row["owned"]]
        if len(running) >= self.max_emulators:
            raise SimError("busy", f"All {self.max_emulators} of Mobster's Android emulators are running checks.",
                           "Wait for one to finish, or set MOBSTER_MAX_SIMS higher.")
        names = self.avds()
        if not names:
            raise SimError("environment", "There is no Android virtual device (AVD).",
                           "Create one in Android Studio's Device Manager, or with avdmanager.")
        if avd and avd not in names:
            raise SimError("environment", f"There is no AVD named {avd}; there are {', '.join(names)}.",
                           "Pick one of those, or create it in Android Studio's Device Manager.")
        avd = avd or names[0]
        lease = self._try_lease(self._identity("", avd))
        if lease is None:
            raise SimError("busy", f"{avd} is running a check.", "Wait for it to finish.")
        try:
            serial = self._boot(avd, timeout)
        except BaseException:
            lease.close()
            raise
        return SimLease(AndroidTarget(serial, avd, "emulator", avd), [lease])

    def _free_port(self):
        taken = {int(row["serial"].split("-")[1]) for row in self.adb.devices() if row.kind == "emulator"}
        for port in range(FIRST_PORT, LAST_PORT + 1, 2):
            if port not in taken:
                return port
        raise SimError("simulator", "Every emulator port from 5554 to 5584 is in use.",
                       "Stop an emulator you started by hand.")

    def _boot(self, avd, timeout):
        emulator = adb_module.tool("emulator", self.root)
        if not emulator:
            raise SimError("environment", "The Android emulator is not installed.",
                           "Install it from Android Studio's SDK Manager.")
        port = self._free_port()
        serial = f"emulator-{port}"
        process = self._spawn([emulator, "-avd", avd, "-port", str(port), *EMULATOR_ARGS])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._pid_file(avd).write_text(json.dumps({"pid": process.pid, "serial": serial}))
        guest = Adb(serial, self._run, self.adb.binary)
        end = self.clock() + timeout
        while self.clock() < end:
            if process.poll() is not None:
                self._pid_file(avd).unlink(missing_ok=True)
                raise SimError("simulator", f"The emulator for {avd} exited while booting.",
                               "Start it once from Android Studio to see why.")
            try:
                if serial in {d.serial for d in self.adb.devices() if d.ready} and guest.booted():
                    return serial
            except AdbError:
                pass
            self.sleep(2)
        self._stop(avd, serial, process.pid)
        raise SimError("simulator", f"{avd} did not finish booting in {timeout} s.",
                       "Close other heavy jobs and try again.")

    def _stop(self, avd, serial, pid):
        Adb(serial, self._run, self.adb.binary).adb("emu", "kill", timeout=10, check=False)
        end = self.clock() + 15
        while self.clock() < end:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            self.sleep(.5)
        else:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        self._pid_file(avd).unlink(missing_ok=True)

    def shutdown(self, avd=None):
        """Stop the emulators Mobster booted (all, or ``avd``) that are not running a check. Others are never
        touched."""
        stopped = []
        for row in self.devices():
            if not row["owned"] or row["in_use"] or (avd and row["avd"] != avd):
                continue
            info = json.loads(self._pid_file(row["avd"]).read_text())
            self._stop(row["avd"], row["serial"], int(info["pid"]))
            stopped.append(row["avd"])
        return stopped
