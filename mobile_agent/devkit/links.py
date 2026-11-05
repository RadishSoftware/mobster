"""A project's deep links: where they come from, what `mobster links check` does with them, and what a step can say.

Four sources, merged by address (a link in several keeps the first source's name, and takes an expected title from
whichever has one):

1. ``.mobster/links.yaml``: the links you name, with the title each screen should show.
2. The Expo config: ``scheme`` and ``ios.associatedDomains`` (app.json, or `npx expo config` for an app.config.*).
3. The Xcode project: ``CFBundleURLTypes`` in an Info.plist (each URL scheme) and ``com.apple.developer.associated-
   domains`` in an .entitlements file (each ``applinks:`` domain).
4. Links learned: a link a check opened successfully (``learn``, called by the runner) is kept in
   ``.mobster/links-learned.json``. The memory store takes over this file when it lands (CLAIMS.md, deep-link
   registry row).

    links:
      - name: cart
        url: myapp://cart
        title: Your cart            # optional: the text the screen shows when the link works
    # or:  cart: myapp://cart       # name: address

A scheme or a domain alone (``myapp://``, ``https://example.com/``) opens the app and no screen in it; it is listed,
and ``check`` reports that it opened without judging a title.

``check_for(link, ...)`` is the check a link becomes: launch the app with the link, expect the title. Run through
`mobster test`'s engine, so the verdict, the screenshot of a failure and the reports are the ones checks have.
``expand_steps`` turns a step "open cart" or "open link cart" into "Open the link myapp://cart" for a name in the
registry, so a step never needs the address.

Reading dynamic Expo configuration invokes its CLI; read-only API discovery skips that step. Only a custom scheme
is learned; web pages and links Mobster never opens (deeplinks.BLOCKED_SCHEMES) are not.
"""

import json
import os
import plistlib
import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from ..deeplinks import BLOCKED_SCHEMES, LinkError, risk_of
from ..env_file import writer
from ..paths import write_text_atomic

FILE = "links.yaml"
LEARNED = "links-learned.json"
LEARNED_SCHEMA = "mobster.links-learned/1"
SOURCES = ("links.yaml", "expo", "xcode", "learned")      # in the order a duplicate keeps
MAX_LINKS = 200
MAX_LEARNED = 50
SKIP_DIRS = frozenset({"Pods", "build", "DerivedData", "node_modules", ".git", "Carthage", ".mobster", "vendor"})
SCAN_DEPTH = 5
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}")
OPEN_STEP = re.compile(r"^\s*open\s+(?:the\s+)?(?:(?:deep\s*)?link\s+)?[\"']?(.+?)[\"']?\s*\.?\s*$", re.I)


class LinksError(Exception):
    """A links file or an argument can't be used: one sentence that says what to do."""


@dataclass(frozen=True)
class ProjectLink:
    name: str
    url: str
    source: str
    title: str = ""           # the text the landing screen shows; "" when nobody said

    @property
    def screen(self):
        """Whether the address names a screen. A scheme or a domain alone opens the app and nothing in it."""
        parts = urlsplit(self.url)
        return bool(parts.path.strip("/") or parts.query or (parts.netloc and parts.scheme not in ("http", "https")))

    def public(self):
        return {"name": self.name, "url": self.url, "source": self.source, "title": self.title or None,
                "screen": self.screen}


# -- the sources ------------------------------------------------------------------------------------------------

def _file_links(root):
    path = Path(root) / ".mobster" / FILE
    if not path.is_file():
        return []
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise LinksError(f".mobster/{FILE} can't be read. Check that it is a text file.") from None
    except yaml.YAMLError:
        raise LinksError(f".mobster/{FILE} isn't valid YAML. Write `links:` as a "
                         "list of name, url and title.") from None
    items = data.get("links") if isinstance(data, dict) and set(data) == {"links"} else data
    if isinstance(items, dict):
        items = [{"name": key, **(value if isinstance(value, dict) else {"url": value})} for key,
                 value in items.items()]
    if not isinstance(items, list) or len(items) > MAX_LINKS:
        raise LinksError(f".mobster/{FILE} holds `links:` as a list of at most {MAX_LINKS}.")
    out = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict) or set(item) - {"name", "url", "title"}:
            raise LinksError(f".mobster/{FILE}, link {index}: use name, url and title.")
        name, url, title = str(item.get("name") or "").strip(), item.get("url"), item.get("title") or ""
        if not NAME.fullmatch(name):
            raise LinksError(f".mobster/{FILE}, link {index}: `name` is 1 to 60 letters, digits, spaces, dots, "
                             "dashes or underscores.")
        if not isinstance(url, str) or not isinstance(title, str) or len(title) > 200:
            raise LinksError(f".mobster/{FILE}, link {name}: `url` and `title` are text; a title is at most 200.")
        try:
            risk_of(url)
        except LinkError as error:
            raise LinksError(f".mobster/{FILE}, link {name}: {error}") from None
        out.append(ProjectLink(name, url.strip(), "links.yaml", title.strip()))
    return out


def _expo_links(root, runner):
    from . import expo
    root = Path(root)
    if not (root / "package.json").is_file():
        return []
    config = expo.read_config(root, runner=runner)
    schemes = config.get("scheme")
    out = [ProjectLink(scheme, f"{scheme}://", "expo")
           for scheme in ([schemes] if isinstance(schemes, str) else schemes or ())
           if isinstance(scheme, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]{0,40}", scheme)]
    ios = config.get("ios") if isinstance(config.get("ios"), dict) else {}
    for domain in ios.get("associatedDomains") or ():
        out += _applink(domain, "expo")
    return out


def _applink(entry, source):
    kind, _, host = str(entry).partition(":")
    host = host.split("?")[0].strip()
    if kind != "applinks" or not re.fullmatch(r"(\*\.)?[A-Za-z0-9.-]{1,253}", host) or host.startswith("*"):
        return []
    return [ProjectLink(host, f"https://{host}/", source)]


def _walk(root):
    root = Path(root)
    for folder, dirs, files in os.walk(root):
        depth = len(Path(folder).relative_to(root).parts)
        dirs[:] = (
            [d for d in sorted(dirs) if d not in SKIP_DIRS and not d.endswith(".xcarchive")]
            if depth < SCAN_DEPTH else [])
        for name in sorted(files):
            if name.endswith("Info.plist") or name.endswith(".entitlements"):
                yield Path(folder) / name


def _xcode_links(root):
    out = []
    for path in _walk(root):
        try:
            data = plistlib.loads(path.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        if not isinstance(data, dict):
            continue
        for kind in data.get("CFBundleURLTypes") or ():
            for scheme in (kind.get("CFBundleURLSchemes") if isinstance(kind, dict) else None) or ():
                if isinstance(scheme, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]{0,40}", scheme) \
                        and scheme.casefold() not in BLOCKED_SCHEMES:
                    out.append(ProjectLink(scheme, f"{scheme}://", "xcode"))
        for domain in data.get("com.apple.developer.associated-domains") or ():
            out += _applink(domain, "xcode")
    return out


def _learned(root):
    path = Path(root) / ".mobster" / LEARNED
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("links") if isinstance(data, dict) and data.get("schema") == LEARNED_SCHEMA else None
    if not isinstance(items, list):
        return []
    return [ProjectLink(str(item["name"])[:60], item["url"], "learned") for item in items[:MAX_LEARNED]
            if isinstance(item, dict) and isinstance(item.get("url"), str) and isinstance(item.get("name"), str)]


def discover(root, *, runner=None, deep=True):
    """The project's links, deduplicated by address. ``deep`` False skips `npx expo config` (an app.config.* needs Node,
    which takes seconds): app.json is still read. LinksError when .mobster/links.yaml is wrong."""
    root = Path(root)
    found = _file_links(root)
    if deep or not any((root / name).is_file() for name in ("app.config.js", "app.config.ts")):
        found += _expo_links(root, runner)
    found += _xcode_links(root) + _learned(root)
    merged = {}
    for link in found:
        key = link.url
        if key not in merged:
            merged[key] = link
        elif link.title and not merged[key].title:
            merged[key] = replace(merged[key], title=link.title)
    return list(merged.values())[:MAX_LINKS]


def find(links, wanted):
    """The link a name (case ignored) or an exact address names. LinksError when none or two match."""
    address = str(wanted or "").strip()
    text = " ".join(address.split()).casefold()
    hits = [link for link in links if link.name.casefold() == text] or [link for link in links if link.url == address]
    if not hits:
        names = ", ".join(link.name for link in links[:8])
        raise LinksError(f"No link is called {str(wanted)[:60]}." + (f" You have: {names}." if names else
                                                                    " `mobster links list` shows what Mobster found."))
    if len(hits) > 1:
        raise LinksError(f"Several links are called {str(wanted)[:60]}. Use the address.")
    return hits[0]


# -- saving -----------------------------------------------------------------------------------------------------

def _write(root, items):
    import yaml
    folder = Path(root) / ".mobster"
    folder.mkdir(exist_ok=True)
    rows = [{key: value for key, value in (("name", link.name), ("url", link.url), ("title", link.title)) if value}
            for link in items]
    write_text_atomic(folder / FILE, yaml.safe_dump({"links": rows}, sort_keys=False, allow_unicode=True))


def save(root, name, url, title=""):
    """Add or replace the link called ``name`` in .mobster/links.yaml.
    LinksError for a name or address that can't be kept."""
    name = " ".join(str(name or "").split())
    if not NAME.fullmatch(name):
        raise LinksError("A link's name is 1 to 60 letters, digits, spaces, dots, dashes or underscores.")
    if not isinstance(url, str):
        raise LinksError("A link needs an address, such as myapp://cart.")
    try:
        risk_of(url)
    except LinkError as error:
        raise LinksError(str(error)) from None
    if not isinstance(title, str) or len(title) > 200:
        raise LinksError("A link's title is text of at most 200 characters.")
    link = ProjectLink(name, url.strip(), "links.yaml", title.strip())
    with writer(Path(root) / ".mobster" / ".links"):
        kept = [item for item in _file_links(root) if item.name.casefold() != name.casefold()]
        if len(kept) >= MAX_LINKS:
            raise LinksError(f"A project keeps at most {MAX_LINKS} links in .mobster/{FILE}.")
        _write(root, kept + [link])
        return link


def remove(root, name):
    """Take the link called ``name`` out of .mobster/links.yaml. Returns whether it was there."""
    text = " ".join(str(name or "").split()).casefold()
    with writer(Path(root) / ".mobster" / ".links"):
        items = _file_links(root)
        kept = [item for item in items if item.name.casefold() != text]
        if len(kept) == len(items):
            return False
        _write(root, kept)
        return True


# -- learning ---------------------------------------------------------------------------------------------------

def learn(root, url, name=None):
    """Keep ``url`` as learned after a run opened it. Never raises; does nothing without a .mobster folder or for a
    web page or a link Mobster never opens. True when the link is new."""
    try:
        scheme = urlsplit(url).scheme.casefold()
        if not scheme or scheme in BLOCKED_SCHEMES or scheme in ("http", "https") or risk_of(url) != "opens":
            return False
        folder = Path(root) / ".mobster"
        if not folder.is_dir():
            return False
        with writer(Path(root) / ".mobster" / ".links"):
            known = [{"name": link.name, "url": link.url} for link in _learned(root)]
            if any(item["url"] == url for item in known):
                return False
            parts = urlsplit(url)
            label = name or (parts.netloc + parts.path).strip("/").replace("/", " ") or scheme
            known = ([{"name": re.sub(r"[^A-Za-z0-9 ._-]", "", label)[:60].strip() or scheme, "url": url}]
                     + known)[:MAX_LEARNED]
            write_text_atomic(folder / LEARNED, json.dumps({"schema": LEARNED_SCHEMA, "links": known}, indent=1) + "\n")
            return True
    except Exception:  # noqa: BLE001 -- what a run learned is a convenience, never a reason to fail it
        return False


# -- steps and checks -------------------------------------------------------------------------------------------

def expand_steps(steps, links):
    """``steps`` with each "open <link name>" or "open link <name>" naming a project link turned into "Open the link
    <address>". Other steps, and "open Settings", are left as written."""
    if not links:
        return tuple(steps)
    out = []
    for step in steps:
        match = OPEN_STEP.match(step)
        hit = None
        if match:
            text = " ".join(match.group(1).split()).casefold()
            if any(link.name.casefold() == text for link in links):
                hit = find(links, match.group(1))
        out.append(f"Open the link {hit.url}" if hit else step)
    return tuple(out)


def checkable(found):
    """(the links ``check`` opens, the ones it skips): a link that acts as it opens (a call, a shortcut) is skipped."""
    def opens(link):
        try:
            return risk_of(link.url) == "opens"
        except LinkError:
            return False
    chosen = [link for link in found if opens(link)]
    return chosen, [link for link in found if link not in chosen]


def check_data(link, *, bundle=None, app_path=None):
    """The check file's content for ``link``: launch with the address, expect the title."""
    app = {"path": app_path} if app_path else {"bundle": bundle}
    data = {"name": f"Link: {link.name}", "app": app, "tags": ["links"], "launch": {"url": link.url}}
    if link.title:
        data["expect"] = [{"text": link.title}]
    return data


def checks_for(links, *, bundle=None, app_path=None):
    """[(link, Check)] for the links. A link that names no screen is skipped by the caller, not here."""
    from ..verify.checks import check_from_dict
    return [(link, check_from_dict(check_data(link, bundle=bundle, app_path=app_path))) for link in links]


def bundle_id(root, *, runner=None):
    """The app's bundle ID: the Expo config's ``ios.bundleIdentifier``, else the first literal PRODUCT_BUNDLE_IDENTIFIER
    of an Xcode project (a test target's is skipped). None when neither names one."""
    from . import expo
    root = Path(root)
    if (root / "package.json").is_file():
        ios = expo.read_config(root, runner=runner).get("ios")
        if isinstance(ios, dict) and isinstance(ios.get("bundleIdentifier"), str):
            return ios["bundleIdentifier"]
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in sorted(dirs) if d not in SKIP_DIRS
                   and len(Path(folder).relative_to(root).parts) < SCAN_DEPTH]
        if folder.endswith(".xcodeproj") and "project.pbxproj" in files:
            try:
                text = (Path(folder) / "project.pbxproj").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for found in re.findall(r"PRODUCT_BUNDLE_IDENTIFIER = ([A-Za-z0-9.-]+);", text):
                if not re.search(r"tests?$", found, re.I):
                    return found
    return None


def plan_check(root, selected, options, *, bundle=None, app_path=None, named_device=None):
    """A testkit Planned for ``selected`` links: one check each, run by `mobster test`'s engine. LinksError when the
    app isn't known."""
    from ..testkit.api import Planned
    from ..testkit.discover import Entry
    from ..testkit.targets import resolve
    root = Path(root)
    bundle = bundle or (None if app_path else bundle_id(root))
    if not bundle and not app_path:
        raise LinksError("Mobster can't tell which app the links open. Pass --bundle com.example.app, or --app PATH "
                         "to a built .app.")
    entries = []
    for link, check in checks_for(selected, bundle=bundle, app_path=app_path):
        file = f".mobster/{FILE}#{link.name}"
        entry = Entry(path=root / ".mobster" / FILE, file=file, check=check,
                      classname="links." + re.sub(r"[^A-Za-z0-9_]+", "_", link.name),
                      keys=(link.name.casefold(), link.url.casefold()))
        entries.append(entry)
    targets = resolve(sims=options.sims, devices=options.devices, root=root, named_device=named_device)
    return Planned(root=root, entries=entries, targets=targets, options=options)
