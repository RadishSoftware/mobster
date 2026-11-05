"""The link atlas: the schemes and universal-link paths the apps on one phone declare.

Sources, none of which needs the network here:
- an app's Info.plist ``CFBundleURLTypes`` (schemes) and, when the bundle carries it, its entitlements'
  ``com.apple.developer.associated-domains`` (``applinks:`` domains; a simulator build keeps
  ``archived-expanded-entitlements.xcent``);
- an apple-app-site-association document (the caller fetched it, when the network is allowed): the paths each
  domain sends to the app.
A simulator is read through ``xcrun simctl listapps``. Every entry is "discovered" until a run opens it.
"""

import fnmatch
import json
import plistlib
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ..deeplinks import BLOCKED_SCHEMES, SCHEME

SCHEME_NAME = re.compile(r"^[a-z][a-z0-9+.-]{0,40}$")
DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
ENTITLEMENTS = "archived-expanded-entitlements.xcent"
STATE = "discovered"


@dataclass
class Entry:
    bundle: str
    name: str
    schemes: list = field(default_factory=list)
    domains: list = field(default_factory=list)
    paths: dict = field(default_factory=dict)    # domain -> [{"pattern": "/invite/*", "exclude": False}]

    def public(self):
        return {"bundleId": self.bundle, "app": self.name, "schemes": self.schemes, "domains": self.domains,
                "state": STATE}


def _schemes(info):
    found = []
    for kind in info.get("CFBundleURLTypes") or ():
        for scheme in (kind.get("CFBundleURLSchemes") or ()) if isinstance(kind, dict) else ():
            scheme = str(scheme).strip().casefold()
            if SCHEME_NAME.match(scheme) and scheme not in BLOCKED_SCHEMES and scheme not in found:
                found.append(scheme)
    return found


def _domains(entitlements):
    found = []
    for item in (entitlements or {}).get("com.apple.developer.associated-domains") or ():
        kind, _, host = str(item).partition(":")
        host = host.split("?", 1)[0].strip().casefold()
        if kind == "applinks" and DOMAIN.match(host) and not host.startswith("*") and host not in found:
            found.append(host)
    return found


def entry_from_bundle(bundle, name, info, entitlements=None):
    """An ``Entry`` from an app's Info.plist (a dict) and its entitlements; None when it declares no link."""
    entry = Entry(bundle, str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or name or bundle),
                  _schemes(info), _domains(entitlements))
    return entry if entry.schemes or entry.domains else None


def read_app_folder(folder):
    """An ``Entry`` from an unpacked ``.app`` folder; None when it is unreadable or declares nothing."""
    folder = Path(folder)
    try:
        info = plistlib.loads((folder / "Info.plist").read_bytes())
        bundle = str(info["CFBundleIdentifier"])
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException):
        return None
    try:
        entitlements = plistlib.loads((folder / ENTITLEMENTS).read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        entitlements = None
    return entry_from_bundle(bundle, folder.stem, info, entitlements)


def parse_aasa(document, bundle):
    """The path rules an apple-app-site-association document (text or dict) gives ``bundle``: a list of
    {"pattern", "exclude"}. Old ``paths`` ("NOT /x" excludes) and ``components`` ("/" patterns) are both read."""
    try:
        data = json.loads(document) if isinstance(document, (str, bytes)) else document
        details = data["applinks"]["details"]
    except (ValueError, KeyError, TypeError):
        return []
    rules = []
    for detail in details if isinstance(details, list) else ():
        ids = list(detail.get("appIDs") or ()) + ([detail["appID"]] if detail.get("appID") else [])
        if not any(str(i).partition(".")[2] == bundle for i in ids):
            continue
        for path in detail.get("paths") or ():
            text = str(path)
            rules.append({"pattern": text.removeprefix("NOT ").strip(), "exclude": text.startswith("NOT ")})
        for part in detail.get("components") or ():
            if isinstance(part, dict) and isinstance(part.get("/"), str):
                rules.append({"pattern": part["/"], "exclude": bool(part.get("exclude"))})
    return [r for r in rules if r["pattern"].startswith(("/", "*"))]


def path_allowed(rules, path):
    """Whether the first rule that matches ``path`` includes it (a rule list that never matches refuses)."""
    for rule in rules:
        if fnmatch.fnmatchcase(path, rule["pattern"]):
            return not rule["exclude"]
    return False


class Atlas:
    def __init__(self, entries=()):
        self.entries = {entry.bundle: entry for entry in entries}

    def add_aasa(self, bundle, domain, document):
        entry = self.entries.get(bundle)
        if entry is not None and domain in entry.domains:
            entry.paths[domain] = parse_aasa(document, bundle)

    def find(self, url, installed=None):
        """The ``Entry`` that declares ``url``'s scheme, or whose associated domain and paths send it to the app;
        None otherwise. ``installed``: bundle ids to limit to (an app that left the phone claims nothing)."""
        text = str(url or "").strip()
        found = SCHEME.match(text)
        if not found:
            return None
        scheme = found.group(1).casefold()
        for entry in self.entries.values():
            if installed is not None and entry.bundle not in installed:
                continue
            if scheme in ("http", "https"):
                parts = urlsplit(text)
                host = (parts.hostname or "").casefold()
                if host in entry.domains and path_allowed(entry.paths.get(host, ()), parts.path or "/"):
                    return entry
            elif scheme in entry.schemes:
                return entry
        return None

    def by_app(self, words):
        """Entries whose app name or bundle id holds every word of ``words``."""
        wants = [w for w in re.split(r"\W+", str(words or "").casefold()) if w]
        return [e for e in self.entries.values()
                if wants and all(w in f"{e.name} {e.bundle}".casefold() for w in wants)]

    def named_in(self, goal):
        """Entries whose app name is a whole word run in ``goal`` ("open Things and add milk" finds Things)."""
        said = " " + " ".join(re.split(r"\W+", str(goal or "").casefold())) + " "
        return [e for e in self.entries.values() if f" {' '.join(re.split(r'\W+', e.name.casefold()))} " in said]

    def to_json(self):
        return json.dumps({"entries": [vars(e) for e in self.entries.values()]}, indent=1)

    @classmethod
    def from_json(cls, text):
        try:
            rows = json.loads(text)["entries"]
            return cls(Entry(**row) for row in rows)
        except (ValueError, KeyError, TypeError):
            return cls()


def path_for(key):
    from ..paths import dev_data_dir
    return dev_data_dir() / "link-atlas" / (re.sub(r"[^A-Za-z0-9._-]", "_", key) + ".json")


def load(key):
    try:
        return Atlas.from_json(path_for(key).read_text())
    except OSError:
        return Atlas()


def save(key, atlas):
    from ..paths import write_text_atomic
    write_text_atomic(path_for(key), atlas.to_json())


def harvest_simulator(udid, run):
    """The atlas of a simulator's apps. ``run(argv, timeout)`` is ``sim.simctl.run``'s shape (a ``Result``).
    Reads Info.plist and entitlements of each app ``simctl listapps`` names; an unreadable app is skipped."""
    listed = run(["xcrun", "simctl", "listapps", udid], timeout=60)
    if not listed.ok:
        return Atlas()
    with tempfile.TemporaryDirectory(prefix="mobster-atlas-") as folder:
        source = Path(folder) / "apps.plist"
        source.write_text(listed.out)
        converted = run(["plutil", "-convert", "json", "-o", "-", str(source)], timeout=30)
    try:
        apps = json.loads(converted.out) if converted.ok else {}
    except ValueError:
        apps = {}
    entries = []
    for bundle, app in apps.items() if isinstance(apps, dict) else ():
        where = str(app.get("Path") or "") if isinstance(app, dict) else ""
        entry = read_app_folder(where) if where else None
        if entry is None and isinstance(app, dict):
            entry = entry_from_bundle(bundle, app.get("CFBundleDisplayName") or "", app, None)
        if entry is not None and entry.bundle == bundle:
            entries.append(entry)
    return Atlas(entries)


def discovered(args):
    """The atlas entries (as dicts) for an ``open_url`` listing's ``app`` and ``query`` on its ``device``."""
    words = f"{args.get('app') or ''} {args.get('query') or ''}"
    return [e.public() for e in load(args.get("device") or "default").by_app(words)]


def describe(rows):
    lines = [f"{r['app']}: {', '.join(s + '://' for s in r['schemes']) or 'no scheme'}" for r in rows]
    return "Discovered in the apps' own files, not verified. open_url still asks as usual:\n" + "\n".join(lines)
