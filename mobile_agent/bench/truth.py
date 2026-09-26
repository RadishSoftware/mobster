"""Per-device ground truth, recorded by the harness itself before any agent runs.

``truth/<device>.json`` holds values such as the iOS version or the Auto-Lock
setting. Each entry says where it came from: ``seed`` values were read on the
phone earlier (``evals/tasks.DEVICES``) or are public facts about the model;
``assumed`` values are iOS 26 screen titles written from documentation and
MUST be confirmed with ``python -m mobile_agent.bench capture-truth`` (the
dry plan lists every unconfirmed key). ``capture`` values were read by the
navigator below.

The navigator is the harness's own read-only hands: it relaunches an app,
taps rows by their exact accessibility label, and reads. It never taps a
switch, slider or anything the safety monitor would refuse, and it is only
used by ``capture-truth``, ``--dry-plan --probe`` and fixture verification,
never while an agent is being measured.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from ..evals.oracles import ProbeUnavailable, navigation_title, screen_text
from ..models import _SYMBOL_NAME as SF_SYMBOL  # one definition of an SF Symbol name
from .checks import norm_text
from .safety import STATEFUL_ROLES
from .suite import BUNDLES

TRUTH_DIR = Path(__file__).resolve().parent / "truth"

S = BUNDLES["settings"]


@dataclass(frozen=True)
class TruthSpec:
    key: str
    bundle: str
    path: tuple
    kind: str          # "title" | "row_value" | "switch"
    label: str = ""


# What capture-truth reads. Titles confirm the navigation tasks' target screens.
SPECS = (
    TruthSpec("title.accessibility", S, ("Accessibility",), "title"),
    TruthSpec("title.keyboards", S, ("General", "Keyboard"), "title"),
    TruthSpec("title.location_services", S, ("Privacy & Security", "Location Services"), "title"),
    TruthSpec("title.display", S, ("Display & Brightness",), "title"),
    TruthSpec("title.notifications", S, ("Notifications",), "title"),
    TruthSpec("title.date_time", S, ("General", "Date & Time"), "title"),
    TruthSpec("title.about", S, ("General", "About"), "title"),
    TruthSpec("title.formats", S, ("Camera", "Formats"), "title"),
    TruthSpec("title.privacy", S, ("Privacy & Security",), "title"),
    TruthSpec("title.apps", S, ("Apps",), "title"),
    TruthSpec("title.legal", S, ("General", "Legal & Regulatory"), "title"),
    TruthSpec("title.per_app", S, ("Accessibility", "Per-App Settings"), "title"),
    TruthSpec("title.auto_lock", S, ("Display & Brightness", "Auto-Lock"), "title"),
    TruthSpec("title.files_on_my_iphone", BUNDLES["files"], ("Browse", "On My iPhone"), "title"),
    TruthSpec("device.ios_version", S, ("General", "About"), "row_value", "iOS Version"),
    TruthSpec("device.model_number", S, ("General", "About"), "row_value", "Model Number"),
    TruthSpec("device.model_name", S, ("General", "About"), "row_value", "Model Name"),
    TruthSpec("device.capacity", S, ("General", "About"), "row_value", "Capacity"),
    TruthSpec("device.modem_firmware", S, ("General", "About"), "row_value", "Modem Firmware"),
    TruthSpec("settings.auto_lock", S, ("Display & Brightness",), "row_value", "Auto-Lock"),
    # iOS 26 moved Low Power Mode to Battery > Power Mode.
    TruthSpec("settings.low_power", S, ("Battery", "Power Mode"), "switch", "Low Power Mode"),
    TruthSpec("settings.time_auto", S, ("General", "Date & Time"), "switch", "Set Automatically"),
    TruthSpec("settings.auto_correction", S, ("General", "Keyboard"), "switch", "Auto-Correction"),
    TruthSpec("settings.region", S, ("General", "Language & Region"), "row_value", "Region"),
    TruthSpec("settings.bold_text", S, ("Accessibility", "Display & Text Size"), "switch", "Bold Text"),
)

# Read from the Settings root screen at every attempt's reset (state that may change day to day).
CAPTURE_AT_RESET = {
    "settings.bluetooth": ("row_value", "Bluetooth"),
    "settings.airplane": ("switch", "Airplane Mode"),
    "settings.wifi": ("wifi", "Wi-Fi"),
}

# Written before any run. "assumed" must be confirmed by capture-truth.
SEEDS = {
    "iphone15pro": {
        # Read on the phone 2026-09-22 through WDAProbe (evals/tasks.py DEVICES).
        "device.ios_version": ("26.0.1", "seed: evals/tasks.DEVICES, read on device 2026-09-22"),
        "device.model_number": ("MTQM3LL/A", "seed: evals/tasks.DEVICES, read on device 2026-09-22"),
        "device.model_name": ("iPhone 15 Pro", "seed: evals/tasks.DEVICES, read on device 2026-09-22"),
        "device.capacity": ("128 GB", "seed: evals/tasks.DEVICES, read on device 2026-09-22"),
        # Public facts about the model / OS (Apple press releases).
        "device.model_release_year": ("2023", "seed: public fact (iPhone 15 Pro released September 2023)"),
        "device.ios_major_release_date": (r"(september 15,? 2025|15 september 2025|2025-09-15)",
                                          "seed: public fact (iOS 26 released 15 September 2025); regex"),
        **{key: (value, "assumed: iOS 26 title, confirm with capture-truth") for key, value in {
            "title.accessibility": "Accessibility", "title.keyboards": "Keyboards",
            "title.location_services": "Location Services", "title.display": "Display & Brightness",
            "title.notifications": "Notifications", "title.date_time": "Date & Time", "title.about": "About",
            "title.formats": "Formats", "title.privacy": "Privacy & Security", "title.apps": "Apps",
            "title.legal": "Legal & Regulatory", "title.per_app": "Per-App Settings",
            "title.auto_lock": "Auto-Lock", "title.files_on_my_iphone": "On My iPhone"}.items()},
    },
}


class TruthStore:
    def __init__(self, device, path=None):
        self.device = device
        self.path = Path(path) if path else TRUTH_DIR / f"{device}.json"
        self.values = {}
        if self.path.exists():
            self.values = json.loads(self.path.read_text()).get("values", {})
        else:
            for key, (value, source) in SEEDS.get(device, {}).items():
                self.values[key] = {"value": value, "source": source}

    def get(self, key):
        entry = self.values.get(key)
        return None if entry is None else entry.get("value")

    def source(self, key):
        entry = self.values.get(key)
        return None if entry is None else entry.get("source")

    def set(self, key, value, source):
        self.values[key] = {"value": value, "source": source,
                            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def unconfirmed(self):
        return sorted(key for key, entry in self.values.items() if str(entry.get("source", "")).startswith("assumed"))

    def missing(self, keys):
        return sorted(key for key in keys if self.get(key) is None)

    def digest(self):
        blob = json.dumps({k: v.get("value") for k, v in sorted(self.values.items())}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"device": self.device, "values": self.values}, indent=1,
                                        sort_keys=True) + "\n")


# ---------------------------------------------------------------- reading screens

def _row_elements(snapshot, label):
    wanted = norm_text(label)
    return [e for e in snapshot.elements if norm_text(e.label) == wanted]


def row_value(snapshot, label):
    """A Settings row's value: the cell's own AX value, else the text to its right on the same row."""
    for element in _row_elements(snapshot, label):
        if element.role in STATEFUL_ROLES:
            continue
        # iOS 26 repeats the label as some rows' own value ("iOS Version" / "iOS Version");
        # that is not the value, so read the text on the right of the row instead.
        if element.value.strip() and norm_text(element.value) != norm_text(label):
            return element.value.strip()
        # iOS 26 rows carry their value in the label: "Bluetooth, On".
        head, _, tail = element.label.partition(",")
        if tail.strip() and norm_text(head) == norm_text(label):
            return tail.strip()
        x, y, w, h = element.rect
        centre = y + h / 2
        # The row's disclosure arrow ("chevron.forward") is an SF Symbol name, never a value
        # (pass 3 graded state.bluetooth against 'chevron.forward').
        right = [other for other in snapshot.elements
                 if other is not element and other.label.strip() and norm_text(other.label) != norm_text(label)
                 and other.role != "Image" and not SF_SYMBOL.match(other.label.strip())
                 and other.rect[1] <= centre <= other.rect[1] + other.rect[3] and other.rect[0] > x]
        if right:
            return max(right, key=lambda other: other.rect[0]).label.strip()
    return None


def switch_value(snapshot, label):
    for element in _row_elements(snapshot, label):
        if element.role == "Switch":
            return {"1": "on", "0": "off", "true": "on", "false": "off"}.get(element.value.strip().casefold())
    return None


def wifi_state(snapshot, label="Wi-Fi"):
    value = row_value(snapshot, label)
    if value is None:
        return None
    return "off" if norm_text(value) == "off" else "on"


def read(kind, snapshot, label=""):
    if kind == "title":
        return navigation_title(snapshot) or None
    if kind == "row_value":
        return row_value(snapshot, label)
    if kind == "switch":
        return switch_value(snapshot, label)
    if kind == "wifi":
        return wifi_state(snapshot, label)
    raise ValueError(kind)


def capture_from_reset(snapshot, bundle):
    """Values read from an attempt's freshly reset start screen (Settings root only)."""
    if bundle != S or snapshot is None:
        return {}
    captured = {}
    for key, (kind, label) in CAPTURE_AT_RESET.items():
        value = read(kind, snapshot, label)
        if value is not None:
            captured[key] = value
    return captured


# ---------------------------------------------------------------- navigator

class Navigator:
    """Read-only navigation by exact labels, for truth capture and fixture checks."""

    def __init__(self, probe, max_swipes=8, sleep=time.sleep):
        self.probe = probe
        self.max_swipes = max_swipes
        self.sleep = sleep

    def _find(self, snapshot, label):
        wanted = norm_text(label)
        # Files and Settings rows append a summary ("dogs12, 12 items"): match the row's name.
        hits = [e for e in snapshot.elements if wanted in (norm_text(e.label), norm_text(e.label.split(",")[0]))
                and e.role not in STATEFUL_ROLES and "TAP" in e.actions]
        # Prefer the row/cell over a nested label; skip the nav-bar title of the current screen.
        hits.sort(key=lambda e: (e.role not in ("Cell", "Button"), "NavigationBar" in e.locator))
        return hits[0] if hits else None

    def walk(self, bundle, path, url=None):
        start = self.probe.reset(bundle, url)
        if not path:
            return start
        driver = self.probe.driver()
        try:
            snapshot = driver.observe_ready(timeout=10)
            for label in path:
                target, swipes = self._find(snapshot, label), 0
                while target is None and swipes < self.max_swipes:
                    driver.execute("SWIPE_UP", None, snapshot, timeout=10)
                    snapshot = driver.wait_for_change(snapshot, timeout=3)
                    target, swipes = self._find(snapshot, label), swipes + 1
                if target is None:
                    raise ProbeUnavailable(f"navigator could not find {label!r}")
                driver.execute("TAP", target, snapshot, timeout=10)
                driver.wait_for_change(snapshot, timeout=3)  # settle the push; the ready read below is what is used
                snapshot = driver.observe_ready(timeout=10)
            snapshot.bundle_id = snapshot.bundle_id or bundle
            return snapshot
        finally:
            driver.close()


def capture_truth(probe, store, specs=SPECS, log=print):
    """Walk every spec (grouped by path) and record the values read. Returns {key: value|None}."""
    navigator = Navigator(probe)
    results = {}
    by_path = {}
    for spec in specs:
        by_path.setdefault((spec.bundle, spec.path), []).append(spec)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for (bundle, path), group in by_path.items():
        try:
            snapshot = navigator.walk(bundle, path)
        except ProbeUnavailable as error:
            for spec in group:
                results[spec.key] = None
            log(f"  {' > '.join(path)}: unavailable ({error})")
            continue
        for spec in group:
            value = read(spec.kind, snapshot, spec.label)
            if value is None and spec.kind in ("row_value", "switch"):
                # Rows below the fold (Modem Firmware): scroll within the same screen, read-only.
                value = _scroll_read(probe, snapshot, spec)
            results[spec.key] = value
            if value is not None:
                store.set(spec.key, value, f"capture: read on device {stamp}")
            log(f"  {spec.key}: {value!r}")
    return results


def _scroll_read(probe, snapshot, spec, max_swipes=4):
    driver = probe.driver()
    try:
        for _ in range(max_swipes):
            driver.execute("SWIPE_UP", None, snapshot, timeout=10)
            snapshot = driver.wait_for_change(snapshot, timeout=3)
            value = read(spec.kind, snapshot, spec.label)
            if value is not None:
                return value
    except Exception:
        return None
    finally:
        driver.close()
    return None


VERIFY_SCREENS = {"notes": ("Notes",), "photos": ("Collections",)}


def fixture_verifier(probe, fixtures, navigator=None):
    """Callable(fixture_key) -> (ok|None, detail): the fixture is on the phone as specified."""
    navigator = navigator or Navigator(probe)

    def verify(key):
        try:
            app, path, want = fixtures.verify_path(key)
        except KeyError:
            return None, f"unknown fixture {key}"
        if app == "calendar":
            return None, "calendar fixture needs manual verification"
        if app == "contacts":
            # Sorted by last name: "Bench Tester" is under T, past hundreds of rows.
            return None, "contacts fixture needs manual verification"
        # Where each app keeps the record on iOS 26 (verification only; not part of the
        # frozen suite): Notes opens on its folder list, Photos on Collections.
        path = VERIFY_SCREENS.get(app, ()) + tuple(path)
        bundle = BUNDLES[app]
        try:
            snapshot = navigator.walk(bundle, path)
        except ProbeUnavailable as error:
            return False, f"could not reach fixture: {error}"
        seen = norm_text(screen_text(snapshot))
        missing = [item for item in want if norm_text(item) not in seen]
        if missing:
            # Long lists (12 files) may need a scroll; read-only.
            driver = probe.driver()
            try:
                for _ in range(3):
                    driver.execute("SWIPE_UP", None, snapshot, timeout=10)
                    snapshot = driver.wait_for_change(snapshot, timeout=3)
                    seen += "\n" + norm_text(screen_text(snapshot))
                    missing = [item for item in want if norm_text(item) not in seen]
                    if not missing:
                        break
            finally:
                driver.close()
        return (not missing), ("present" if not missing else f"missing {missing[:3]}")

    return verify
