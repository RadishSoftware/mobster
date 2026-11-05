"""App Store screenshots from your real flows, as full-resolution PNGs in the folders `fastlane deliver` reads."""

import json
import plistlib
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from ..sim import simctl

# App Store Connect's screenshot sizes (Screenshot specifications, developer.apple.com/help/app-store-connect,
# read 14 Oct 2026): the display class, the simulators that have it (newest first), and the portrait pixel sizes
# Apple accepts for it. 6.9" is required for an iPhone app; 13" for an app that runs on iPad.
SIZES = {
    "6.9": ("iPhone 6.9-inch", ("iPhone 17 Pro Max", "iPhone 16 Pro Max", "iPhone 16 Plus"),
            ((1320, 2868), (1290, 2796), (1260, 2736))),
    "6.5": ("iPhone 6.5-inch", ("iPhone 14 Plus", "iPhone 11 Pro Max"), ((1284, 2778), (1242, 2688))),
    "6.3": ("iPhone 6.3-inch", ("iPhone 17 Pro", "iPhone 17", "iPhone 16 Pro", "iPhone 16"),
            ((1206, 2622), (1179, 2556))),
    "13": ("iPad 13-inch", ("iPad Pro 13-inch (M5)", "iPad Pro 13-inch (M4)", "iPad Air 13-inch (M3)"),
           ((2064, 2752), (2048, 2732))),
}
# The App Store's locale codes for an .lproj with a language only.
LOCALES = {"en": "en-US", "de": "de-DE", "es": "es-ES", "fr": "fr-FR", "nl": "nl-NL", "pt": "pt-PT", "ar": "ar-SA",
           "nb": "no", "zh-Hans": "zh-Hans", "zh-Hant": "zh-Hant", "zh_CN": "zh-Hans", "zh_TW": "zh-Hant"}
STATUS_BAR = ("--time", "9:41", "--dataNetwork", "wifi", "--wifiMode", "active", "--wifiBars", "3",
              "--cellularMode", "active", "--cellularBars", "4", "--batteryState", "charged", "--batteryLevel", "100")


@dataclass(frozen=True)
class Shot:
    index: int
    check: object        # verify.checks.Check
    size: str
    device: str
    locale: str

    @property
    def name(self):
        slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", self.check.name.lower())).strip("-")[:40] or "shot"
        return f"{self.index:02d}_{self.size.replace('.', '')}_{slug}.png"


def app_locales(app_path):
    """The App Store locales a built .app ships: its .lproj folders (Base left out), else CFBundleLocalizations."""
    found = []
    folder = Path(app_path or "")
    if folder.is_dir():
        found = sorted(p.stem for p in folder.glob("*.lproj") if p.stem not in ("Base",))
        if not found:
            try:
                with open(folder / "Info.plist", "rb") as handle:
                    found = list(plistlib.load(handle).get("CFBundleLocalizations") or [])
            except (OSError, ValueError, plistlib.InvalidFileException):
                found = []
    return [asc_locale(item) for item in found] or ["en-US"]


def asc_locale(code):
    code = str(code).replace("_", "-")
    if code in LOCALES:
        return LOCALES[code]
    return code


def launch_args(locale):
    """``-AppleLanguages (de-DE) -AppleLocale de_DE``: the app runs in that language, the rest of iOS unchanged."""
    language = locale if locale != "no" else "nb"
    region = locale.replace("-", "_") if "-" in locale and not locale.startswith("zh-") else language
    return ("-AppleLanguages", f"({language})", "-AppleLocale", region)


def is_ipad_app(app_path):
    try:
        with open(Path(app_path) / "Info.plist", "rb") as handle:
            family = plistlib.load(handle).get("UIDeviceFamily") or []
    except (OSError, ValueError, plistlib.InvalidFileException, TypeError):
        return False
    return 2 in family


def available_device_types(run=None):
    """Names of the simulator device types this Mac's Xcode has."""
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout)
    try:
        data = json.loads(run(["xcrun", "simctl", "list", "devicetypes", "-j"]))
    except (ValueError, OSError, subprocess.SubprocessError):
        return []
    return [item.get("name") for item in data.get("devicetypes") or () if item.get("name")]


def device_for(size, available):
    """The first simulator of ``size`` this Mac has, or None."""
    for name in SIZES[size][1]:
        if name in available:
            return name
    return None


def plan_shots(checks, *, sizes, locales, available):
    """Every (check × size × locale) as Shots, and the notes on sizes this Mac can't make."""
    shots, notes = [], []
    for size in sizes:
        if size not in SIZES:
            raise ValueError(f"{size} isn't a screenshot size Mobster knows: {', '.join(SIZES)}")
        device = device_for(size, available)
        if device is None:
            notes.append(f"No {SIZES[size][0]} simulator on this Mac ({', '.join(SIZES[size][1][:2])}), so no "
                         f"{size}\" shots. Add one in Xcode › Settings › Components.")
            continue
        for locale in locales:
            for index, check in enumerate(checks, 1):
                shots.append(Shot(index, check, size, device, locale))
    return shots, notes


class StatusBarAndShot:
    """A simulator manager that sets the status bar to 9:41 on each lease and shoots the screen as it goes back."""

    def __init__(self, manager, path, *, run=simctl.run):
        self._manager, self.path = manager, Path(path)
        self._run = run
        self.taken = False

    def __getattr__(self, name):
        return getattr(self._manager, name)

    def acquire(self, *args, **kwargs):
        lease = self._manager.acquire(*args, **kwargs)
        self._run(["xcrun", "simctl", "status_bar", lease.target.udid, "override", *STATUS_BAR])
        return _ShotLease(self, lease)


class _ShotLease:
    def __init__(self, owner, lease):
        self._owner, self._lease, self.target = owner, lease, lease.target

    def __getattr__(self, name):
        return getattr(self._lease, name)

    def __enter__(self):
        return self.target

    def __exit__(self, *exc):
        self.release()

    def release(self):
        owner, udid = self._owner, self.target.udid
        # the shot is taken as the lease goes back: the run's verdict is decided by then
        if not owner.taken:
            owner.path.parent.mkdir(parents=True, exist_ok=True)
            owner.taken = simctl.screenshot(udid, owner.path, timeout=60, runner=owner._run).ok
            owner._run(["xcrun", "simctl", "status_bar", udid, "clear"])
        self._lease.release()


def png_size(path):
    data = Path(path).read_bytes()[:24]
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def take(shots, *, out, runs_dir, manager=None, verify=None, progress=None):
    """Run every shot. Returns {"shots": [...], "ok": bool} and writes screenshots.json in ``out``."""
    from ..verify.runner import verify as verify_run
    verify = verify or verify_run
    progress = progress or (lambda line: None)
    if manager is None:
        from ..sim import SimulatorManager
        manager = SimulatorManager(progress=progress)
    out = Path(out)
    rows = []
    for number, shot in enumerate(shots, 1):
        target = out / shot.locale / shot.name
        target.unlink(missing_ok=True)
        check = replace(shot.check, device=shot.device, runtime=None,
                        launch_args=tuple(shot.check.launch_args) + launch_args(shot.locale))
        progress(f"[{number}/{len(shots)}] {shot.check.name}: {shot.device}, {shot.locale}")
        wrapped = StatusBarAndShot(manager, target)
        try:
            result = verify(check, runs_dir=runs_dir, manager=wrapped)
        except KeyboardInterrupt:
            raise
        except Exception as error:  # one shot's failure never stops the rest
            result = {"verdict": "couldnt_run", "summary": f"{type(error).__name__}: {error}"}
        verdict = result.get("verdict") or "couldnt_run"
        if verdict == "couldnt_run" and target.is_file():
            target.unlink()
        size = png_size(target) if target.is_file() else None
        accepted = SIZES[shot.size][2]
        row = {"check": shot.check.name, "size": shot.size, "device": shot.device, "locale": shot.locale,
               "verdict": verdict, "file": str(target.relative_to(out)) if target.is_file() else None,
               "pixels": list(size) if size else None,
               "fits": bool(size and (tuple(size) in accepted or (size[1], size[0]) in accepted)),
               "summary": result.get("summary"), "run": result.get("run_id")}
        rows.append(row)
    doc = {"schema": "mobster.screenshots/1", "shots": rows,
           "ok": bool(rows) and all(row["file"] and row["fits"] for row in rows)}
    out.mkdir(parents=True, exist_ok=True)
    (out / "screenshots.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return doc
