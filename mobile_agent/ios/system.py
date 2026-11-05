"""iOS's own moves as code, shared by every connection: the WebDriverAgent tree, iPhone Mirroring, and Cable +
Bluetooth.

Each primitive takes a driver with the device contract (``drivers.Driver``: ``observe``, ``execute``; optionally
``wait_for_change``, ``active_app``, ``system_shortcut``, ``type_focused``) and returns an
``Outcome``. It reads the screen after every move and stops as soon as it isn't sure where it is: it never guesses
and never retries a move it sent. Nothing here taps a control that commits anything, and none of them needs a model.

- ``open_app(driver, bundle, name)``: by bundle id where the connection can (WebDriverAgent's activate); otherwise
  Spotlight, then the App Library's search, then the Home Screen's pages. Spotlight counts only when its Top Hit is the
  app: with "Show App in Search" off for an app (Settings › Search), Spotlight lists only web and dictionary results,
  and Return would open those (a real iPhone on iOS 26.0.1, 14 Oct 2026). The App Library lists every installed app
  either way. A ``RouteMemory`` remembers which route worked on this phone, so the next open goes straight there.
- ``go_home``: the Home Screen's first page.
- iOS's alerts are ``system_dialogs``: it reads them from WebDriverAgent, or from the screen on a connection that sees
  only pixels, and answers them under the user's permission mode.

Labels checked on an iOS 26.4 simulator's tree (14 Oct 2026): the Home Screen's "Page control" ("Page 1 of 3") and
"Search" button above the Dock; the App Library's search field (value "App Library"), its results as rows named by
the app; Spotlight's "SpotlightSearchField", its "Top Hit" with the app's cell, and the field's inline "Settings —
Open" completion.
"""

import re
import time
import weakref
from dataclasses import dataclass, field

from . import kb

SPRINGBOARD = "com.apple.springboard"
SPOTLIGHT = "com.apple.Spotlight"
ROUTES = ("bundle", "spotlight", "app_library", "home_pages")
# The most Home Screen pages swiped past before the App Library (iOS allows 15 pages).
MAX_PAGES = 15
# The budget for a move's screen to settle and be read (a WebDriverAgent read under load takes 1-3 s).
SETTLE = 8.0
RESULTS_WAIT = 1.2
# Where Spotlight's Top Hit and results sit, and the App Library's results (screen fractions, top to bottom).
RESULTS_BAND = (.06, .62)
# Refusals a connection makes before it sends anything (wda.guards.DriverRejection.PRE_DISPATCH's first cause).
PRE_DISPATCH = frozenset({"stale_revision", "unsupported_or_stale_target"})


@dataclass
class Outcome:
    ok: bool
    route: str = ""
    detail: str = ""
    steps: list = field(default_factory=list)
    snapshot: object = None


class Unsure(Exception):
    """The screen isn't what the primitive expects: it stops there."""


def fold(text):
    return " ".join(str(text or "").replace(" ", " ").replace("…", "").split()).casefold()


MAGNIFIER = re.compile(r"^q\s+(?=\S)")


def field_name(element):
    """What a search field says, without the magnifier OCR reads as "Q " ("Q App Library" -> "app library")."""
    return MAGNIFIER.sub("", fold(element.label)) if element.role == "StaticText" else fold(element.label)


def labels(snapshot):
    return [e.label for e in getattr(snapshot, "elements", ()) or ()]


def _wait(driver, snapshot, seconds=.6):
    """The screen after a move: the driver's own wait when it has one, else a fresh read."""
    wait = getattr(driver, "wait_for_change", None)
    if callable(wait) and snapshot is not None:
        try:
            return wait(snapshot, timeout=SETTLE, wait_seconds=seconds)
        except TimeoutError:
            pass  # a slow read: settle by a plain read below
    time.sleep(seconds)
    return driver.observe()


def _tap(driver, element, snapshot, steps, why):
    """Tap ``element``. When the connection refused before sending anything because the screen moved since it was
    read (WebDriverAgent's stale_revision), the same control is found on a fresh read and tapped once; nothing was
    sent, so this is no retry of an action whose outcome is unknown."""
    try:
        driver.execute("TAP", element, snapshot)
    except Exception as error:  # noqa: BLE001 -- only a refusal before dispatch is handled here
        if getattr(error, "code", None) not in PRE_DISPATCH:
            raise
        snapshot = driver.observe()
        element = _same(snapshot, element)
        driver.execute("TAP", element, snapshot)
    steps.append(f"tap {element.label[:40]!r} ({why})")
    return _wait(driver, snapshot)


def _same(snapshot, element):
    """``element`` on a fresher read: the same role and label, nearest to where it was; Unsure when it's gone."""
    matches = [e for e in snapshot.elements if e.role == element.role and e.label == element.label]
    if not matches:
        raise Unsure(f"{element.label[:40]!r} is no longer on the screen")
    cx, cy = element.center
    return min(matches, key=lambda e: abs(e.center[0] - cx) + abs(e.center[1] - cy))


def _type(driver, snapshot, text, steps):
    """Type into the focused field (Spotlight's, a search field's), on a fresh read of it."""
    snapshot = driver.observe()
    focused = getattr(driver, "type_focused", None)
    field_ = next((e for e in snapshot.elements if e.editable and e.role in ("SearchField", "TextField")), None)
    # A search field keeps its last query (Spotlight does): clear it first, or the name is typed after it.
    clear = next((e for e in snapshot.elements if e.role == "Button" and fold(e.label) == "clear text"), None)
    if clear is not None:
        snapshot = _tap(driver, clear, snapshot, steps, "clear the old query")
        field_ = next((e for e in snapshot.elements if e.editable and e.role in ("SearchField", "TextField")),
                      field_)
    elif callable(getattr(driver, "clear_focused", None)) and getattr(snapshot, "keyboard", "") == "visible":
        driver.clear_focused()  # pixels can't tell an old query from the placeholder: empty the field either way
        steps.append("clear the field")
    if callable(focused) and getattr(snapshot, "keyboard", "") == "visible":
        focused(text)
    elif field_ is not None:
        driver.execute("TYPE", field_, snapshot, text)
    else:
        raise Unsure("no field to type into")
    steps.append(f"type {text[:40]!r}")
    time.sleep(RESULTS_WAIT)
    return driver.observe()


def screen(snapshot):
    """kb.screen_of for a snapshot."""
    return kb.screen_of(getattr(snapshot, "bundle_id", ""), labels(snapshot), getattr(snapshot, "keyboard", ""))


def _named(snapshot, name, band=None, roles=None):
    want = fold(name)
    out = [e for e in snapshot.elements if fold(e.label) == want and (roles is None or e.role in roles)
           and (band is None or band[0] <= e.center[1] <= band[1])]
    return sorted(out, key=lambda e: e.center[1])


def go_home(driver, snapshot=None):
    """The Home Screen's pages: Home returns to the page an app was opened from, so it is pressed again there."""
    snapshot = snapshot if snapshot is not None else driver.observe()
    steps = []
    after = snapshot
    for _ in range(2):
        if not _shortcut(driver, "home"):
            driver.execute("HOME", None, after)
        steps.append("home")
        after = _wait(driver, after, .8)
        if screen(after) not in ("app_library", "spotlight", "control_center"):
            break
    return Outcome(screen(after) in ("home", "") or after.bundle_id == SPRINGBOARD, "home", "", steps, after)


def _shortcut(driver, name):
    """The connection's own key for ``name`` (iPhone Mirroring's cmd+1 Home, cmd+3 Spotlight), when it has one."""
    send = getattr(driver, "system_shortcut", None)
    if not callable(send):
        return False
    try:
        return bool(send(name))
    except ValueError:
        return False


def open_app(driver, bundle, name=None, routes=None, phone=""):
    """Open ``bundle`` (an app named ``name`` on its icon)."""
    can_bundle = getattr(driver, "opens_by_bundle", True)
    order = []
    remembered = routes.get(phone, bundle) if routes is not None else None
    for route in (remembered, *ROUTES):
        if route in ROUTES and route not in order and (route != "bundle" or can_bundle):
            order.append(route)
    if not name and order != ["bundle"]:
        order = [route for route in order if route == "bundle"]
    tried = []
    for route in order:
        try:
            outcome = ROUTE_FUNCS[route](driver, bundle, name)
        except Unsure as reason:
            outcome = Outcome(False, route, str(reason))
        tried.append(f"{route}: {'ok' if outcome.ok else outcome.detail or 'no'}")
        if outcome.ok:
            if routes is not None:
                routes.put(phone, bundle, route)
            outcome.steps = [*tried[:-1], *outcome.steps]
            return outcome
        if routes is not None and route == remembered:
            routes.miss(phone, bundle, route)
        if route == "bundle":
            break  # an app iOS can't activate by id isn't on the phone: no route opens it
    if tried and order != ["bundle"]:
        try:
            go_home(driver)  # leave the phone on the Home Screen, not in a half-typed search
        except Exception:  # noqa: BLE001 -- the failure below is what the caller needs to hear
            pass
    return Outcome(False, "", "couldn't open " + (name or bundle) + " (" + "; ".join(tried) + ")", tried)


def _opened(driver, bundle, before, after):
    """Whether the app is in front now."""
    reader = getattr(driver, "active_app", None)
    if callable(reader) and getattr(driver, "reads_front_app", True):
        try:
            return reader() == bundle
        except Exception:  # noqa: BLE001 -- can't tell: not proven
            return False
    return screen(after) not in ("spotlight", "app_library", "home") \
        and getattr(after, "image_hash", "x") != getattr(before, "image_hash", "y")


def _by_bundle(driver, bundle, name):
    snapshot = driver.observe()
    driver.execute("LAUNCH_APP", bundle, snapshot)
    after = _wait(driver, snapshot, .8)
    return Outcome(_opened(driver, bundle, snapshot, after), "bundle", "", [f"launch {bundle}"], after)


def _spotlight(driver, bundle, name):
    steps = []
    home = go_home(driver)
    snapshot = home.snapshot
    if not _shortcut(driver, "spotlight"):
        search = _named(snapshot, "Search", band=(.7, .9))
        if search:
            snapshot = _tap(driver, search[0], snapshot, steps, "Spotlight")
        else:
            driver.execute("SWIPE_DOWN", None, snapshot)
            steps.append("pull down for Spotlight")
            snapshot = _wait(driver, snapshot)
    else:
        steps.append("Spotlight key")
        time.sleep(.6)
        snapshot = driver.observe()
    results = _type(driver, snapshot, name, steps)
    hit = top_hit(results, name)
    if hit is None:
        _leave(driver, results, steps)
        return Outcome(False, "spotlight", f"Spotlight's top hit isn't {name}", steps)
    after = _tap(driver, hit, results, steps, "Spotlight's top hit")
    ok = _opened(driver, bundle, results, after)
    return Outcome(ok, "spotlight", "" if ok else f"{name} didn't open from Spotlight", steps, after)


def top_hit(snapshot, name):
    """The element to tap for app ``name`` in Spotlight's results: its cell or icon under "Top Hit" (or under
    "Apps"), never a web, dictionary or Siri Knowledge result. None when the app isn't a hit."""
    rows = sorted(snapshot.elements, key=lambda e: e.center[1])
    # Section headings ("Settings" is one too, over settings results: never the app's own name).
    heads = [e for e in rows if e.role != "Cell" and fold(e.label) != fold(name) and fold(e.label) in (
        "top hit", "apps", "suggestions", "siri suggestions", "siri knowledge", "search in apps", "websites",
        "search the web", "related searches", "dictionary", "maps", "contacts", "messages", "settings", "music",
        "photos", "files", "mail", "notes", "shortcuts") and e.role == "StaticText"]
    for head in heads:
        if fold(head.label) not in ("top hit", "apps"):
            continue
        below = [h.center[1] for h in heads if h.center[1] > head.center[1] + .01]
        end = min(below) if below else RESULTS_BAND[1]
        for element in rows:
            if head.center[1] < element.center[1] < end and fold(element.label) == fold(name) \
                    and element.role != "TextField" and not element.editable:
                return element
    return None


def _leave(driver, snapshot, steps):
    """Back out of Spotlight or a search to the Home Screen, sending nothing."""
    go_home(driver, snapshot)
    steps.append("leave search")


def _app_library(driver, bundle, name):
    steps = []
    snapshot = _to_app_library(driver, steps)
    field_ = next((e for e in snapshot.elements if e.role == "SearchField" or field_name(e) == "app library"), None)
    if field_ is None:
        raise Unsure("no App Library search field")
    if getattr(snapshot, "keyboard", "") != "visible":
        snapshot = _tap(driver, field_, snapshot, steps, "App Library search")
    results = _type(driver, snapshot, name, steps)
    found = _named(results, name, band=(.1, .7), roles=("Cell", "StaticText", "Icon", "Button"))
    cells = [e for e in found if e.role == "Cell"] or found
    if not cells:
        _leave(driver, results, steps)
        return Outcome(False, "app_library", f"the App Library has no {name}", steps)
    after = _tap(driver, cells[0], results, steps, "App Library result")
    ok = _opened(driver, bundle, results, after)
    return Outcome(ok, "app_library", "" if ok else f"{name} didn't open from the App Library", steps, after)


def _to_app_library(driver, steps):
    """Home, then swipes left past the last page until the App Library shows."""
    snapshot = go_home(driver).snapshot
    steps.append("home")
    for _ in range(MAX_PAGES + 1):
        if screen(snapshot) == "app_library":
            return snapshot
        driver.execute("SWIPE_LEFT", None, snapshot)
        steps.append("next page")
        after = _wait(driver, snapshot)
        if fold(" ".join(labels(after))) == fold(" ".join(labels(snapshot))) and screen(after) != "app_library":
            raise Unsure("the Home Screen stopped turning before the App Library")
        snapshot = after
    raise Unsure("no App Library after the last Home Screen page")


def _home_pages(driver, bundle, name):
    steps = []
    snapshot = go_home(driver).snapshot
    if not _first_page(snapshot):
        snapshot = go_home(driver, snapshot).snapshot  # Home on a Home Screen page shows the first page
    for _ in range(MAX_PAGES):
        icons = _named(snapshot, name, band=(.05, .97), roles=("Icon", "StaticText", "Button"))
        if icons:
            after = _tap(driver, icons[0], snapshot, steps, "Home Screen icon")
            ok = _opened(driver, bundle, snapshot, after)
            return Outcome(ok, "home_pages", "" if ok else f"{name} didn't open from its icon", steps, after)
        if screen(snapshot) == "app_library":
            break
        driver.execute("SWIPE_LEFT", None, snapshot)
        steps.append("next page")
        after = _wait(driver, snapshot)
        if fold(" ".join(labels(after))) == fold(" ".join(labels(snapshot))):
            break
        snapshot = after
    return Outcome(False, "home_pages", f"no {name} icon on the Home Screen", steps)


def _first_page(snapshot):
    """True when the Home Screen's page dots say page 1 (a tree reads them; on pixels it is unknown: False)."""
    dots = next((e for e in snapshot.elements if e.role == "PageIndicator"), None)
    return dots is not None and bool(re.match(r"page 1 of", fold(dots.value)))


ROUTE_FUNCS = {"bundle": _by_bundle, "spotlight": _spotlight, "app_library": _app_library,
               "home_pages": _home_pages}


_versions = weakref.WeakKeyDictionary()


def ios_version(driver):
    try:
        if driver in _versions:
            return _versions[driver]
    except TypeError:
        pass
    version = getattr(driver, "ios_version", None)
    http = getattr(driver, "http", None)
    if not isinstance(version, str) and http is not None:
        try:
            status = http.request("GET", "/status", None, 3)
            version = ((status or {}).get("value") or {}).get("os", {}).get("version")
        except Exception:  # noqa: BLE001 -- unknown is a fine answer
            version = None
    version = version if isinstance(version, str) else None
    try:
        _versions[driver] = version
    except TypeError:
        pass
    return version
