"""Static audits of a built app's bundle: they read files only, never launch anything and never change the bundle."""

import os
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Finding", "AUDITS", "run_audit", "bundle_symbols", "USAGE_KEYS", "REQUIRED_REASON"]


@dataclass(frozen=True)
class Finding:
    id: str            # stable, such as "usage-description-missing"
    guideline: str     # "5.1.1(i)"
    level: str         # "fail" | "review"
    message: str       # what is wrong, then what to do
    evidence: str      # a path inside the bundle, with the key

    def to_dict(self):
        return {"id": self.id, "guideline": self.guideline, "level": self.level, "message": self.message,
                "evidence": self.evidence}


MACHO_MAGICS = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")
READ_LIMIT = 200 * 1024 * 1024


def _machos(app):
    """Every Mach-O file in the bundle: the executable, frameworks and plug-ins, as (relative path, path)."""
    found = []
    for folder, _, names in os.walk(app):
        for name in names:
            path = Path(folder) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                with open(path, "rb") as handle:
                    head = handle.read(4)
            except OSError:
                continue
            if head in MACHO_MAGICS:
                found.append((path.relative_to(app).as_posix(), path))
    return sorted(found)


def _present(name, data):
    # Swift mangles class and constant names inside longer ones; a C function is a whole NUL-delimited string.
    word = name.encode()
    if name[:1].isupper():
        return word in data
    return b"\0_" + word + b"\0" in data or b"\0" + word + b"\0" in data


def bundle_symbols(app, names):
    """{relative path of a Mach-O file: the set of ``names`` it holds} for the whole bundle."""
    out = {}
    for relative, path in _machos(Path(app)):
        try:
            if path.stat().st_size > READ_LIMIT:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        found = {name for name in names if _present(name, data)}
        if found:
            out[relative] = found
    return out


def uses(symbols, names):
    """{Mach-O file: the first of ``names`` it holds} over the scan ``symbols`` (bundle_symbols)."""
    hits = {}
    for relative, found in sorted(symbols.items()):
        for name in names:
            if name in found:
                hits[relative] = name
                break
    return hits


# The Info.plist key each API needs, and the names whose presence in a binary means the app can reach it.
USAGE_KEYS = (
    ("NSCameraUsageDescription", ("AVCaptureDeviceInput", "AVCaptureSession", "VNDocumentCameraViewController")),
    ("NSMicrophoneUsageDescription", ("AVAudioRecorder", "AVAudioApplication")),
    ("NSPhotoLibraryUsageDescription", ("PHPhotoLibrary",)),
    ("NSPhotoLibraryAddUsageDescription", ("UIImageWriteToSavedPhotosAlbum",)),
    ("NSLocationWhenInUseUsageDescription", ("CLLocationManager",)),
    ("NSContactsUsageDescription", ("CNContactStore",)),
    ("NSCalendarsUsageDescription", ("EKEventStore",)),
    ("NSFaceIDUsageDescription", ("LAContext",)),
    ("NSBluetoothAlwaysUsageDescription", ("CBCentralManager", "CBPeripheralManager")),
    ("NSMotionUsageDescription", ("CMMotionActivityManager", "CMPedometer")),
    ("NSSpeechRecognitionUsageDescription", ("SFSpeechRecognizer",)),
    ("NSUserTrackingUsageDescription", ("ATTrackingManager",)),
    ("NSHealthShareUsageDescription", ("HKHealthStore",)),
)
ALSO_ACCEPTED = {
    "NSLocationWhenInUseUsageDescription": ("NSLocationAlwaysAndWhenInUseUsageDescription",
                                            "NSLocationAlwaysUsageDescription"),
    "NSCalendarsUsageDescription": ("NSCalendarsFullAccessUsageDescription",
                                    "NSCalendarsWriteOnlyAccessUsageDescription",
                                    "NSRemindersUsageDescription", "NSRemindersFullAccessUsageDescription"),
    "NSPhotoLibraryAddUsageDescription": ("NSPhotoLibraryUsageDescription",),
    "NSHealthShareUsageDescription": ("NSHealthUpdateUsageDescription",),
}
PLACEHOLDER = re.compile(
    r"lorem ipsum|\btodo\b|\btbd\b|\bfixme\b|placeholder|\bxxx+\b|usage description|your (?:text|description) here|"
    r"enter (?:a |your )?(?:description|text|reason) here|^(?:test|testing|foo|bar|asdf|description|text|none|n/a|"
    r"camera|microphone|location|photos|contacts|calendar)\.?$", re.I)
MIN_WORDS = 3


def read_plist(path):
    """A property list as a dict (None when the file is missing, unreadable or not a dict)."""
    try:
        value = plistlib.loads(Path(path).read_bytes())
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def placeholder_reason(text):
    """Why a usage string reads as filler (None when it reads as a real reason)."""
    value = (text or "").strip()
    if not value:
        return "it is empty"
    if PLACEHOLDER.search(value):
        return "it reads as placeholder text"
    if len(value.split()) < MIN_WORDS:
        return f"it is {len(value.split())} word{'' if len(value.split()) == 1 else 's'}, too short to say why"
    return None


def audit_usage_descriptions(app, ctx):
    info = read_plist(Path(app) / "Info.plist")
    if info is None:
        return [Finding("info-plist-unreadable", "2.1", "review",
                        "The app's Info.plist can't be read. Build the app again and point the check at it.",
                        f"{Path(app).name}/Info.plist")]
    symbols = ctx.symbols()
    findings, plist = [], f"{Path(app).name}/Info.plist"
    for key, names in USAGE_KEYS:
        hits = uses(symbols, names)
        if not hits:
            continue
        accepted = (key, *ALSO_ACCEPTED.get(key, ()))
        present = [name for name in accepted if name in info]
        where = next(iter(hits.items()))
        if not present:
            findings.append(Finding(
                "usage-description-missing", "5.1.1", "fail",
                f"{where[0]} uses {where[1]} and Info.plist has no {key}. Add {key} with the reason you ask.",
                f"{plist} has no {key}; {where[1]} is in {where[0]}"))
            continue
        for name in present:
            why = placeholder_reason(info.get(name) if isinstance(info.get(name), str) else "")
            if why:
                findings.append(Finding(
                    "usage-description-placeholder", "5.1.1", "fail",
                    f"{name} is {info.get(name)!r}: {why}. Write the reason a person would accept.",
                    f"{plist} {name}"))
    # A key with filler even when no symbol showed the API: a framework may load it at run time.
    for name, value in sorted(info.items()):
        if name.endswith("UsageDescription") and isinstance(value, str):
            why = placeholder_reason(value)
            if why and not any(f.evidence == f"{plist} {name}" for f in findings):
                findings.append(Finding(
                    "usage-description-placeholder", "5.1.1", "fail",
                    f"{name} is {value!r}: {why}. Write the reason a person would accept.", f"{plist} {name}"))
    return findings


# "weak" names are libc calls a binary can carry for other reasons: an undeclared one needs review, not a failure.
REQUIRED_REASON = {
    "NSPrivacyAccessedAPICategoryFileTimestamp": {
        "sure": ("NSFileCreationDate", "NSFileModificationDate", "NSURLContentModificationDateKey",
                 "NSURLCreationDateKey", "NSURLContentAccessDateKey", "getattrlist", "getattrlistbulk",
                 "fgetattrlist"),
        "weak": ("stat", "fstat", "lstat", "fstatat")},
    "NSPrivacyAccessedAPICategorySystemBootTime": {
        "sure": ("systemUptime", "mach_absolute_time"), "weak": ()},
    "NSPrivacyAccessedAPICategoryDiskSpace": {
        "sure": ("NSURLVolumeAvailableCapacityKey", "NSURLVolumeAvailableCapacityForImportantUsageKey",
                 "NSURLVolumeTotalCapacityKey", "NSFileSystemFreeSize", "NSFileSystemSize", "statfs", "statvfs",
                 "fstatfs", "fstatvfs"), "weak": ()},
    "NSPrivacyAccessedAPICategoryActiveKeyboards": {"sure": ("activeInputModes",), "weak": ()},
    "NSPrivacyAccessedAPICategoryUserDefaults": {"sure": ("NSUserDefaults",), "weak": ()},
}
MANIFEST = "PrivacyInfo.xcprivacy"


def declared_categories(manifest):
    """({category: [reasons]} the manifest declares, a problem sentence or None)."""
    types = manifest.get("NSPrivacyAccessedAPITypes")
    if types is None:
        return {}, None
    if not isinstance(types, list):
        return {}, "NSPrivacyAccessedAPITypes is not a list"
    out = {}
    for item in types:
        if isinstance(item, dict) and isinstance(item.get("NSPrivacyAccessedAPIType"), str):
            reasons = item.get("NSPrivacyAccessedAPITypeReasons")
            out[item["NSPrivacyAccessedAPIType"]] = [r for r in reasons if isinstance(r, str)] \
                if isinstance(reasons, list) else []
    return out, None


def audit_privacy_manifest(app, ctx):
    app = Path(app)
    symbols = ctx.symbols()
    needed = {}
    for category, groups in REQUIRED_REASON.items():
        for strength in ("sure", "weak"):
            hits = uses(symbols, groups[strength])
            if hits:
                needed.setdefault(category, {})[strength] = hits
    top = app / MANIFEST
    findings = []
    if not top.is_file():
        if needed:
            first = ", ".join(f"{category} ({next(iter((hits.get('sure') or hits['weak']).items()))[1]} in "
                              f"{next(iter(sorted((hits.get('sure') or hits['weak']))))})"
                              for category, hits in sorted(needed.items()))
            findings.append(Finding(
                "privacy-manifest-missing", "5.1.1", "fail",
                f"{app.name} has no {MANIFEST} and its binaries use {len(needed)} required-reason API "
                f"categor{'y' if len(needed) == 1 else 'ies'}. Add the file to the app target and declare each "
                "with a reason.", f"{app.name}/{MANIFEST} is missing; uses {first}"))
        else:
            findings.append(Finding(
                "privacy-manifest-missing", "5.1.1", "review",
                f"{app.name} has no {MANIFEST}. No required-reason API shows in the binaries, so App Review "
                "may accept it; add an empty manifest to be sure.", f"{app.name}/{MANIFEST} is missing"))
        return findings
    manifest = read_plist(top)
    if manifest is None:
        return [Finding("privacy-manifest-invalid", "5.1.1", "fail",
                        f"{MANIFEST} is not a valid property list. Open it in Xcode and save it again.",
                        f"{app.name}/{MANIFEST}")]
    declared, problem = declared_categories(manifest)
    if problem:
        findings.append(Finding("privacy-manifest-invalid", "5.1.1", "fail",
                                f"{problem}. Open {MANIFEST} in Xcode and fix the key.", f"{app.name}/{MANIFEST}"))
    for category, reasons in sorted(declared.items()):
        if not reasons:
            findings.append(Finding(
                "privacy-reason-missing", "5.1.1", "fail",
                f"{category} is declared with no reason code. Add NSPrivacyAccessedAPITypeReasons.",
                f"{app.name}/{MANIFEST} {category}"))
    for category, strengths in sorted(needed.items()):
        if category in declared:
            continue
        for strength, level in (("sure", "fail"), ("weak", "review")):
            hits = strengths.get(strength) or {}
            own = {f: n for f, n in hits.items() if not f.startswith(("Frameworks/", "PlugIns/"))}
            if own:
                file, name = next(iter(sorted(own.items())))
                findings.append(Finding(
                    "required-reason-undeclared", "5.1.1", level,
                    f"{file} uses {name} and {MANIFEST} does not declare {category}. Add it with the reason "
                    "that fits how the app uses it.", f"{app.name}/{MANIFEST} lacks {category}; {name} is in {file}"))
                break
            embedded = {f: n for f, n in hits.items() if f not in own}
            if embedded:
                file, name = next(iter(sorted(embedded.items())))
                folder = "/".join(file.split("/")[:2])
                own_manifest = read_plist(app / folder / MANIFEST) or {}
                if category in declared_categories(own_manifest)[0]:
                    continue
                state = f"lacks {category}" if own_manifest else "is missing"
                findings.append(Finding(
                    "required-reason-sdk", "5.1.1", "review",
                    f"{file} uses {name} and neither it nor {MANIFEST} declares {category}. Update the SDK "
                    "to a version with its own privacy manifest, or declare the category yourself.",
                    f"{folder}/{MANIFEST} {state}; {name} is in {file}"))
                break
    return findings


class Context:
    """What the audits of one run share: the bundle's symbols, read once."""

    def __init__(self, app):
        self.app = Path(app)
        self._symbols = None

    def symbols(self):
        if self._symbols is None:
            names = {name for _, group in USAGE_KEYS for name in group}
            for groups in REQUIRED_REASON.values():
                names.update(groups["sure"])
                names.update(groups["weak"])
            self._symbols = bundle_symbols(self.app, sorted(names))
        return self._symbols


AUDITS = {
    "usage-descriptions": audit_usage_descriptions,
    "privacy-manifest": audit_privacy_manifest,
}


def run_audit(name, app, *, context=None):
    """The findings of audit ``name`` on the built app ``app``. A bundle that isn't there is one review finding,
    never a crash."""
    app = Path(app)
    if not app.is_dir():
        return [Finding("app-not-found", "2.1", "review", f"{app} isn't a built app. Build it, then run again.",
                        str(app))]
    ctx = context or Context(app)
    return AUDITS[name](app, ctx)
