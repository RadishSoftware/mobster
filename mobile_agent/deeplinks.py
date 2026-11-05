"""Deep links: open a screen in one call instead of tapping through menus, with a small map of links that work.

``open_link(driver, url, bundle=None)`` opens a URL the way WebDriverAgent's POST /url does (XCTest's
openDefaultApplicationForURL, iOS 16.4+; with ``bundleId``, that app's openURL:usingApplication:, which keeps a
universal link in its app instead of Safari) and answers iOS's "Open in “App”?" when it shows (system_dialogs:
Mobster opened the link itself). ``xcrun devicectl device process launch --payload-url`` is the other way in (iOS 17+)
and isn't needed while WebDriverAgent runs.

``LINKS`` is the map the agent and MCP's ``open_url`` (with no url) read: each link's app, what it opens, its URL
template and how safe it is. Rules:
- ``opens``: shows a screen (search results, a profile, a Settings pane). Nothing is sent, bought or changed.
- ``prefills``: opens a composer with text in it (Mail, Messages, WhatsApp). Sending is still a tap the user approves.
- ``acts``: does something as soon as it opens or after one system confirmation (a call, a FaceTime call, running a
  shortcut).
- Whether a link asks first is ``decide``'s one rule (below): every path that opens a link uses it.
- Never opened: ``javascript:``, ``data:``, ``file:``, ``itms-services:`` (installs an app) and anything without a
  scheme.

Settings links (``App-prefs:``) are undocumented: Apple's DTS calls them unsupported (developer.apple.com/forums/
thread/691712) and their sub-pages broke in iOS 18 (thread/759900). They are kept as ``best_effort`` and checked on the
phone (``verified``); when one doesn't land, the agent navigates the usual way.

Every link with ``verified`` set was opened on a real iPhone (the date, the model and the iOS version).

A web address can open an app on the wrong screen. ``APP_FORMS`` lists the ones recorded doing that and the app link
that opens the screen they name: ``open_link`` opens the app link instead, and ``app_for`` names the app it opens.
"""

import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

BLOCKED_SCHEMES = frozenset({"javascript", "data", "file", "itms-services", "about", "blob", "vbscript"})
# Links that act as they open, or after one system confirmation.
ACTING_SCHEMES = frozenset({"tel", "telprompt", "facetime", "facetime-audio", "facetime-group"})
ACTING_PATHS = (re.compile(r"^shortcuts://(?:x-callback-url/)?run-shortcut", re.I),
                re.compile(r"^workflow://(?:x-callback-url/)?run-workflow", re.I),
                re.compile(r"^shortcuts://(?:x-callback-url/)?import-shortcut", re.I))
SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]{0,40}):")
MAX_URL = 2000


@dataclass(frozen=True)
class Link:
    id: str                  # "maps.search"
    app: str                 # the app's name as people say it
    bundle: str              # the app that opens it ("" when iOS picks: the default browser)
    does: str                # what it opens, as a short phrase ("search Maps")
    template: str            # the URL with {placeholders}
    risk: str = "opens"      # opens | prefills | acts
    best_effort: bool = False
    verified: str = ""       # "2026-10-09 iPhone 15 Pro, iOS 26.x" when opened on a real phone
    source: str = ""

    @property
    def params(self):
        return tuple(dict.fromkeys(re.findall(r"{(\w+)}", self.template)))

    def fill(self, **values):
        """The URL with each placeholder filled, percent-encoded. KeyError when one is missing."""
        missing = [name for name in self.params if not str(values.get(name) or "").strip()]
        if missing:
            raise KeyError(", ".join(missing))
        return self.template.format(**{name: quote(str(values[name]).strip(), safe="@+") for name in self.params})

    def public(self):
        return {"id": self.id, "app": self.app, "bundleId": self.bundle or None, "opens": self.does,
                "url": self.template, "risk": self.risk, "bestEffort": self.best_effort or None,
                "verified": self.verified or None}


APPLE_SCHEMES = "https://developer.apple.com/library/archive/featuredarticles/iPhoneURLScheme_Reference/"
INSTAGRAM_CHECKED = "2026-10-14 iPhone 15 Pro, iOS 26.0.1"
LINKS = (
    # Settings (undocumented, best effort; see the module's docstring)
    Link("settings.app", "Settings", "com.apple.Preferences", "an app's own page in Settings",
         "App-prefs:{bundle_id}", best_effort=True, source="https://developer.apple.com/forums/thread/759900"),
    Link("settings.root", "Settings", "com.apple.Preferences", "Settings", "App-prefs:", best_effort=True,
         source="https://developer.apple.com/forums/thread/691712"),
    Link("settings.wifi", "Settings", "com.apple.Preferences", "Settings › Wi-Fi", "App-prefs:WIFI",
         best_effort=True),
    Link("settings.bluetooth", "Settings", "com.apple.Preferences", "Settings › Bluetooth", "App-prefs:Bluetooth",
         best_effort=True),
    Link("settings.general", "Settings", "com.apple.Preferences", "Settings › General", "App-prefs:General",
         best_effort=True),
    Link("settings.notifications", "Settings", "com.apple.Preferences", "Settings › Notifications",
         "App-prefs:NOTIFICATIONS_ID", best_effort=True),
    Link("settings.battery", "Settings", "com.apple.Preferences", "Settings › Battery",
         "App-prefs:BATTERY_USAGE", best_effort=True),
    Link("settings.privacy", "Settings", "com.apple.Preferences", "Settings › Privacy & Security",
         "App-prefs:Privacy", best_effort=True),
    # Maps and the web
    Link("maps.search", "Maps", "com.apple.Maps", "search Maps", "maps://?q={query}", source=APPLE_SCHEMES),
    Link("maps.directions", "Maps", "com.apple.Maps", "directions to a place (not started)",
         "maps://?daddr={destination}", source=APPLE_SCHEMES),
    Link("google_maps.search", "Google Maps", "com.google.Maps", "search Google Maps", "comgooglemaps://?q={query}",
         source="https://developers.google.com/maps/documentation/urls/ios-urlscheme"),
    Link("web.search", "Safari", "", "a web search", "https://www.google.com/search?q={query}"),
    # Stores and media
    Link("app_store.search", "App Store", "com.apple.AppStore", "search the App Store",
         "itms-apps://search.itunes.apple.com/WebObjects/MZSearch.woa/wa/search?media=software&term={query}",
         best_effort=True),
    Link("app_store.app", "App Store", "com.apple.AppStore", "an app's App Store page",
         "itms-apps://apps.apple.com/app/id{app_id}"),
    Link("youtube.search", "YouTube", "com.google.ios.youtube", "search YouTube",
         "youtube://results?search_query={query}", best_effort=True),
    Link("spotify.search", "Spotify", "com.spotify.client", "search Spotify", "spotify:search:{query}",
         best_effort=True),
    # Profiles
    Link("instagram.profile", "Instagram", "com.burbn.instagram", "an Instagram profile",
         "instagram://user?username={username}", verified=INSTAGRAM_CHECKED,
         source="https://help.short.io/en/articles/9114578-how-to-create-instagram-deep-link"),
    Link("instagram.inbox", "Instagram", "com.burbn.instagram", "the Instagram DM inbox", "instagram://direct-inbox",
         verified=INSTAGRAM_CHECKED),
    Link("instagram.reels", "Instagram", "com.burbn.instagram", "the Instagram Reels viewer",
         "instagram://reels_home", verified=INSTAGRAM_CHECKED),
    Link("x.profile", "X", "com.atebits.Tweetie2", "an X profile", "twitter://user?screen_name={username}",
         best_effort=True),
    Link("tiktok.profile", "TikTok", "com.zhiliaoapp.musically", "a TikTok profile",
         "https://www.tiktok.com/@{username}", best_effort=True),
    Link("telegram.profile", "Telegram", "ph.telegra.Telegraph", "a Telegram chat or channel",
         "tg://resolve?domain={username}", source="https://core.telegram.org/api/links"),
    Link("reddit.subreddit", "Reddit", "com.reddit.Reddit", "a subreddit", "https://www.reddit.com/r/{name}",
         best_effort=True),
    # Composers (nothing is sent until a Send the user approves)
    Link("mail.compose", "Mail", "com.apple.mobilemail", "a new email, filled in",
         "mailto:{to}?subject={subject}&body={body}", "prefills", source=APPLE_SCHEMES),
    Link("messages.compose", "Messages", "com.apple.MobileSMS", "a new message, filled in", "sms:{to}&body={body}",
         "prefills", source=APPLE_SCHEMES),
    Link("whatsapp.compose", "WhatsApp", "net.whatsapp.WhatsApp", "a WhatsApp message to pick a chat for",
         "whatsapp://send?text={text}", "prefills", best_effort=True),
    # Apps' home screens
    Link("photos.open", "Photos", "com.apple.mobileslideshow", "Photos", "photos-redirect://", best_effort=True),
    Link("calendar.open", "Calendar", "com.apple.mobilecal", "Calendar", "calshow:", best_effort=True),
    Link("notes.open", "Notes", "com.apple.mobilenotes", "Notes", "mobilenotes://", best_effort=True),
    Link("shortcuts.open", "Shortcuts", "com.apple.shortcuts", "a shortcut, open to edit (not run)",
         "shortcuts://open-shortcut?name={name}", source="https://support.apple.com/guide/shortcuts/apd624386f42/ios"),
    # Acting links: asked first
    Link("phone.call", "Phone", "com.apple.mobilephone", "a call (iOS confirms first)", "tel:{number}", "acts",
         source="https://developer.apple.com/forums/thread/73406"),
    Link("facetime.call", "FaceTime", "com.apple.facetime", "a FaceTime call", "facetime:{handle}", "acts",
         source=APPLE_SCHEMES),
    Link("shortcuts.run", "Shortcuts", "com.apple.shortcuts", "runs a shortcut at once",
         "shortcuts://run-shortcut?name={name}", "acts",
         source="https://support.apple.com/guide/shortcuts/apd624386f42/ios"),
)
BY_ID = {link.id: link for link in LINKS}


class LinkError(ValueError):
    """Why a URL won't be opened, in one plain sentence."""


def risk_of(url):
    """opens | prefills | acts for ``url``; LinkError for one Mobster never opens."""
    text = str(url or "").strip()
    if not text or len(text) > MAX_URL or any(ord(c) < 32 for c in text):
        raise LinkError("That isn't a link Mobster can open.")
    found = SCHEME.match(text)
    if not found:
        raise LinkError("A link needs a scheme, such as https://example.com or maps://?q=coffee.")
    scheme = found.group(1).casefold()
    if scheme in BLOCKED_SCHEMES:
        raise LinkError(f"Mobster never opens {scheme}: links.")
    if scheme in ACTING_SCHEMES or any(pattern.match(text) for pattern in ACTING_PATHS):
        return "acts"
    if scheme in ("mailto", "sms", "whatsapp") or text.casefold().startswith("tg://msg"):
        return "prefills"
    return "opens"


# Web addresses that open Instagram's home feed, not the screen they name (opened on an iPhone 15 Pro, iOS 26.0.1,
# 2026-10-14), and the app link that opens that screen. A post's address (instagram.com/p/<code>/) is not here: it
# opens the post.
_INSTAGRAM = r"^https?://(?:www\.)?instagram\.com"
APP_FORMS = (
    (re.compile(_INSTAGRAM + r"/direct/inbox/?(?:[?#].*)?$", re.I), "instagram://direct-inbox"),
    (re.compile(_INSTAGRAM + r"/reels/?(?:[?#].*)?$", re.I), "instagram://reels_home"),
)


def app_form(url):
    text = str(url or "").strip()
    for pattern, app_url in APP_FORMS:
        if pattern.match(text):
            return app_url
    return url


def app_for(url):
    """The map's bundle id for ``url`` when it matches a link's scheme and host ("" when unknown)."""
    url = app_form(url)
    text = str(url or "")
    scheme = (SCHEME.match(text).group(1).casefold() if SCHEME.match(text) else "")
    if scheme in ("http", "https"):
        host = (urlsplit(text).hostname or "").casefold()
        for link in LINKS:
            if link.bundle and link.template.startswith("https://") and host and host == (
                    urlsplit(link.template.split("{")[0] + "x").hostname or "").casefold():
                return link.bundle
        return ""
    for link in LINKS:
        if link.bundle and link.template.casefold().startswith(scheme + ":"):
            return link.bundle
    return ""


# Whether a link may open without asking (``decide``), one rule for every path that opens one: the agent's OPEN_URL
# (skills/open_url.py), a coding agent's open_url (ai_plans/phone_tools.py), MCP's open_url and `mobster phone open`.
# - Opens: a web page (http or https with a host and no sign-in in the address), Maps, the App Store, and the apps
#   this map knows (Settings, Photos, Instagram...).
# - Asks every time: a link that acts or writes to someone (tel:, FaceTime, sms:, mailto:, running a shortcut). Never
#   remembered, whatever was remembered for its app.
# - Asks once per app: any other app's link (things://, venmo://). "Don't ask again for Things" covers its later links.
# With nobody to answer, a link that asks isn't opened.
ALWAYS_ASK_SCHEMES = ACTING_SCHEMES | {"sms", "mailto", "facetime-audio-prompt"}
OPEN_SCHEMES = frozenset({"maps", "itms-apps", "itms-appss"})
LINK_LABEL, LINK_OP = "Links", "OPEN_URL"
APP_NAMES = {"maps": "Maps", "itms-apps": "App Store", "itms-appss": "App Store"}


@dataclass(frozen=True)
class LinkDecision:
    url: str
    action: str              # "open" | "ask"
    always: bool = False     # asks every time: "Don't ask again" isn't offered
    # the app the link goes to: its bundle id, or "url:<scheme>" for an app the map doesn't know
    app: str = ""
    app_name: str = ""
    title: str | None = None  # the approval card's question

    def remember(self):
        """The approval's "Don't ask again" (run.Run.request_approval's ``remember``), or None."""
        if self.action != "ask" or self.always or not self.app:
            return None
        return {"app": self.app, "label": LINK_LABEL, "op": LINK_OP, "app_name": self.app_name,
                "words": f"Don’t ask again for {self.app_name}"}


def _shown(url, limit=80):
    return url if len(url) <= limit else url[:limit - 1] + "…"


def link_title(url):
    """The card's question for a link that asks every time ("Call +1 555 0100?")."""
    scheme, _, rest = url.partition(":")
    scheme = scheme.casefold()
    who = rest.lstrip("/").split("?", 1)[0].split("&", 1)[0][:40]
    if scheme in ("tel", "telprompt"):
        return f"Call {who}?" if who else "Place this call?"
    if scheme.startswith("facetime"):
        return f"Start a FaceTime call to {who}?" if who else "Start this FaceTime call?"
    if scheme == "sms":
        return f"Start a message to {who}?" if who else "Start a message?"
    if scheme == "mailto":
        return f"Start an email to {who}?" if who else "Start an email?"
    if any(pattern.match(url) for pattern in ACTING_PATHS):
        return f"Run the shortcut in {_shown(url)}?"
    return f"Open {_shown(url)}?"


def decide(url, remembered=None):
    """``LinkDecision`` for ``url``; LinkError for one Mobster never opens. ``remembered(app, label, op)``: whether the
    person said "Don't ask again" for that app's links (permissions.exceptions.Exceptions.contains)."""
    risk_of(url)
    text = str(url).strip()
    scheme = SCHEME.match(text).group(1).casefold()
    if scheme in ("http", "https"):
        parts = urlsplit(text)
        if parts.hostname and "@" not in parts.netloc:
            return LinkDecision(text, "open", app=app_for(text))
        return LinkDecision(text, "ask", always=True, title=f"Open {_shown(text)}?")
    if scheme in ALWAYS_ASK_SCHEMES or any(pattern.match(text) for pattern in ACTING_PATHS):
        return LinkDecision(text, "ask", always=True, app=app_for(text), title=link_title(text))
    if scheme in OPEN_SCHEMES:
        return LinkDecision(text, "open", app=app_for(text), app_name=APP_NAMES[scheme])
    known = app_for(text)
    if known:
        return LinkDecision(text, "open", app=known,
                            app_name=next((link.app for link in LINKS if link.bundle == known), ""))
    app = f"url:{scheme}"
    name = scheme.replace("-", " ").replace("_", " ").strip().title() or scheme
    if callable(remembered):
        try:
            if remembered(app, LINK_LABEL, LINK_OP):
                return LinkDecision(text, "open", app=app, app_name=name)
        except Exception:
            pass  # an unreadable list remembers nothing: the link asks
    return LinkDecision(text, "ask", app=app, app_name=name, title=f"Open this link in {name}?")


def describe(link_ids=None, apps=None, limit=None):
    """Compact lines for a prompt: "maps.search: maps://?q={query} (search Maps)"."""
    lines = []
    for link in LINKS:
        if link_ids is not None and link.id not in link_ids:
            continue
        if apps is not None and link.bundle and link.bundle not in apps:
            continue
        mark = "" if link.risk == "opens" else f", {link.risk}"
        lines.append(f"{link.template} ({link.does}{mark})")
        if limit and len(lines) >= limit:
            break
    return lines


def search(app=None, query=None):
    """Links for an app (its name or bundle id) and/or matching words."""
    out = []
    want_app = str(app or "").strip().casefold()
    words = [w for w in re.split(r"\W+", str(query or "").casefold()) if w]
    for link in LINKS:
        if want_app and want_app not in (link.app.casefold(), link.bundle.casefold()):
            continue
        hay = f"{link.id} {link.app} {link.does}".casefold()
        if words and not all(word in hay for word in words):
            continue
        out.append(link)
    return out


def open_link(driver, url, *, bundle=None, timeout=20, settle=0.6, clock=time.monotonic, sleep=time.sleep,
              answer_prompt=True):
    """Open ``url`` on the device. Returns {"url", "bundleId", "prompt"}: ``prompt`` is the "Open in" answer
    Mobster gave ("Open"), or None. A web address in ``APP_FORMS`` opens as its app link, and ``requested`` is the
    address that was asked for. LinkError for a link Mobster never opens; the runner's refusal propagates."""
    from .wda import system_dialogs
    risk_of(url)
    requested = url
    url = app_form(url)
    if url != requested:
        bundle = app_for(url) or bundle   # the app link opens in its own app, whichever app the caller named
    body = {"url": url}
    if bundle:
        body["bundleId"] = bundle
    system_dialogs.note_open(driver, url, clock=clock)
    driver.call("POST", "/url", body, timeout=timeout)
    pressed = None
    if answer_prompt:
        sleep(settle)
        dialog = system_dialogs.read(driver, timeout=2)
        if dialog is not None and dialog.kind == "open_in_app":
            decision = system_dialogs.decide(dialog, system_dialogs.ASK_IMPORTANT, can_ask=False,
                                             recent_=system_dialogs.recent(driver), clock=clock)
            if decision is not None and decision.action == "press":
                system_dialogs.press(driver, decision)
                pressed = decision.button
    opened = {"url": url, "bundleId": bundle or None, "prompt": pressed}
    if url != requested:
        opened["requested"] = requested
    return opened
