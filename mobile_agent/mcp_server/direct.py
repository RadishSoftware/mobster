"""A device the phone tools drive directly, outside a check (SPEC §3: MCP drives real USB iPhones too)."""

import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

FRAME_QUALITY = 75
EVALUATE_PAUSE = .5


def url_app(url):
    # An ordinary web link stays in Safari, so the person is asked about Safari before the page opens.
    from ..deeplinks import app_for
    return app_for(url) or ("com.apple.mobilesafari" if urlsplit(url).scheme.casefold() in ("http", "https") else "")


class PhoneStop(Exception):
    def __init__(self, verdict):
        super().__init__(getattr(verdict, "message", "") or "Your iPhone isn't ready. No action was taken.")
        self.code = getattr(verdict, "code", "") or "phone_locked"
        self.verdict = verdict


CHECK_FAILED = "Mobster couldn't check whether your iPhone is unlocked, so it stopped."
VERDICTS = ("ready", "unlocked", "stop")


def strict_check(guard, driver, cause):
    from ..agent_hooks import GuardVerdict
    try:
        verdict = guard.check(driver, cause=cause)
    except Exception:
        verdict = None
    if getattr(verdict, "state", None) not in VERDICTS:
        return GuardVerdict("stop", "phone_locked", CHECK_FAILED)
    return verdict


def _decline(*_args, **_kwargs):
    # MCP has nobody to ask: an approval the guard would want is declined.
    return "denied"


def default_guard(record, emit=None):
    if record.get("kind") == "simulator":
        return None
    from ..lockscreen import guard_for
    return guard_for(device_id=record["id"], wda_url=record.get("wdaUrl"), mode="scripts", approve=_decline,
                     emit=emit or (lambda event: None))


def lockscreen_available():
    try:
        from ..lockscreen import CAN_UNLOCK
    except ImportError:
        return False
    return CAN_UNLOCK is True


def unlock_settings(record):
    keys = {"enabled": False, "askBeforeUnlocking": True, "scripts": False, "relockAfter": True, "needsCheck": False}
    settings = None
    try:
        from .. import lockscreen
        reader = getattr(lockscreen, "unlock_settings", None)
        if callable(reader):
            settings = reader(record["id"])
    except ImportError:
        pass
    except Exception:
        settings = None
    if not isinstance(settings, dict):
        try:
            from ..devices import DeviceStore
            from ..paths import user_data_dir
            entry = next((item for item in DeviceStore(user_data_dir()).phones()
                          if item.get("udid") in (record.get("udid"), record.get("id"))), None)
            settings = (entry or {}).get("unlock")
        except Exception:
            settings = None
    settings = settings if isinstance(settings, dict) else {}
    return {key: settings.get(key) is True if not default else settings.get(key) is not False
            for key, default in keys.items()}


def passcode_saved(record):
    try:
        from .. import passcode_vault
    except ImportError:
        return False
    try:
        return bool(passcode_vault.exists(record.get("udid") or record["id"]))
    except Exception:
        return False


class DeviceRun:
    mode = "direct"

    def __init__(self, record, *, runs_dir, manager=None, driver_factory=None, guard_factory=None, emit=None,
                 acquire_timeout=None):
        from ..device_targets import WdaDevice
        from ..verify.runs import make_run_dir
        self.record = record
        self.device_id = record["id"]
        self.device_name = record.get("name") or record["id"]
        self.manager = manager or WdaDevice(record)
        self._driver_factory = driver_factory
        self._acquire_timeout = acquire_timeout  # None: the manager's own wait
        self.runs_dir = Path(runs_dir).expanduser().resolve()
        # Ignored in git and readable by this user only: the frames show the phone's screen.
        self.run_id, self.run_dir = make_run_dir(self.runs_dir)
        self.lock = threading.RLock()
        self.steps, self.frames = [], []
        self.state = "new"
        self.driver = self.target = self.lease = self.app = None
        self.result = None
        self._guard_factory = guard_factory or default_guard
        self._emit = emit or (lambda event: None)
        self.guard = self.preflight = None
        self._frame_n = 0
        self._frame_width = None
        self._t0 = time.monotonic()
        self._release_lock = threading.Lock()
        self.log(f"device {self.device_name} ({self.device_id}), driven directly")

    def log(self, line):
        try:
            with open(self.run_dir / "run.log", "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        except OSError:
            pass

    def prepare(self):
        self.lease = (self.manager.acquire() if self._acquire_timeout is None
                      else self.manager.acquire(timeout=self._acquire_timeout))
        self.target = self.lease.target
        try:
            if self._driver_factory is not None:
                self.driver = self._driver_factory(self.target)
            else:
                from ..verify.runner import build_driver
                self.driver = build_driver(self.target, self.manager, self.run_dir, "keyless")
            self._check_phone("preflight")
        except BaseException:
            self._release()
            raise
        self.state = "ready"

    def _check_phone(self, cause):
        if self.guard is None and self.preflight is None:
            self.guard = self._guard_factory(self.record, self._emit)
        if self.guard is None:
            return None
        verdict = strict_check(self.guard, self.driver, cause)
        if cause == "preflight":
            self.preflight = verdict
        self.log(f"phone check ({cause}): {getattr(verdict, 'state', '?')} {getattr(verdict, 'code', '')}".rstrip())
        if getattr(verdict, "state", "") == "stop":
            raise PhoneStop(verdict)
        return verdict

    def check_phone(self, cause="resume"):
        with self.lock:
            return self._check_phone(cause)

    def read_tree(self):
        from ..verify.tree import read_tree, tree_from_pixels
        pixels = getattr(self.driver, "pixel_snapshot", None)
        if not callable(pixels):
            return read_tree(self.driver, pause=time.sleep)
        snapshot = pixels()
        if snapshot is not None:
            return tree_from_pixels(snapshot)
        from ..wda.observe import WDA_TREE_BUDGET_SECONDS
        started = time.monotonic()
        try:
            return read_tree(self.driver, timeout=WDA_TREE_BUDGET_SECONDS, pause=time.sleep, retry_slow=False)
        except Exception:
            if time.monotonic() - started < WDA_TREE_BUDGET_SECONDS - .1:
                raise
            snapshot = pixels(mark=True)
            if snapshot is None:
                raise
            return tree_from_pixels(snapshot)

    def frame(self, label):
        self._frame_n += 1
        label = re.sub(r"[^a-z0-9-]+", "-", str(label).lower()).strip("-") or "frame"
        path = self.run_dir / "frames" / f"{self._frame_n:02d}-{label}.jpg"
        try:
            self.manager.screenshot(self.target, path, max_width=self._frame_width, quality=FRAME_QUALITY)
            if self._frame_width is None:
                from PIL import Image
                with Image.open(path) as image:
                    self._frame_width = max(1, image.width // 2)
        except Exception as error:
            self.log(f"no {label} frame: {type(error).__name__}")
            return None
        self.frames.append(path.relative_to(self.run_dir).as_posix())
        return path

    def record_step(self, *, op, text, target=None, typed=None, changed=None, frame=None):
        from ..journal import append_jsonl
        step = {"index": len(self.steps) + 1, "op": op, "text": text,
                "target": ({"id": target.get("id"), "label": target.get("label"), "role": target.get("role")}
                           if target else None),
                "typed": typed, "changed": changed,
                "frame": Path(frame).relative_to(self.run_dir).as_posix() if frame else None,
                "at_ms": round((time.monotonic() - self._t0) * 1000)}
        append_jsonl(self.run_dir / "events.jsonl", {"event": "step", **step})
        self.steps.append(step)
        return step

    def open_url(self, url):
        from ..deeplinks import open_link
        from ..verify.checks import URL_SCHEME, CheckError
        if not isinstance(url, str) or not URL_SCHEME.match(url) or len(url) > 2000:
            raise CheckError("the link needs a scheme, such as https://example.com or myapp://home")
        with self.lock:
            return open_link(self.driver, url, bundle=url_app(url) or None)

    def launch_app(self, bundle_id):
        with self.lock:
            self.driver.call("POST", "/wda/apps/activate", {"bundleId": bundle_id}, timeout=30)

    def home(self):
        with self.lock:
            self.driver.call("POST", "/wda/pressButton", {"name": "home"}, timeout=10)

    def evaluate(self, assertions, *, timeout=5):
        from ..verify.assertions import evaluate_on
        deadline = time.monotonic() + max(0.0, timeout)
        with self.lock:
            while True:
                results = evaluate_on(assertions, self.read_tree())
                if all(result.ok for result in results) or time.monotonic() >= deadline:
                    return results
                time.sleep(EVALUATE_PAUSE)

    def _release(self):
        with self._release_lock:
            driver, self.driver = self.driver, None
            lease, self.lease = self.lease, None
            guard, self.guard = self.guard, None
        if guard is not None and driver is not None:
            try:
                guard.finish(driver)  # locks the phone again if this guard unlocked it and relock is on
            except Exception as error:
                self.log(f"the phone guard's finish failed ({type(error).__name__})")
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass
        if lease is not None:
            try:
                lease.release()
            except Exception:
                pass

    def close(self, reason):
        if self.result is None:
            self._release()
            self.state = "finished"
            self.result = {"schema": "mobster.device/1", "run_id": self.run_id, "device": self.device_id,
                           "device_name": self.device_name, "status": "closed", "summary": reason,
                           "steps": list(self.steps), "frames": list(self.frames), "run_dir": str(self.run_dir)}
            self.log(f"closed: {reason}")
        return self.result
