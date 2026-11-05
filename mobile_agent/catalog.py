"""User-facing applications, not internal iOS view services or VM controls."""

APPS = [
    {"id": slug, "name": name, "bundleId": bundle, "available": True,
     "installed": None, "availability": "demo", "automationVerified": False}
    for slug, name, bundle in [
        ("tiktok", "TikTok", "com.zhiliaoapp.musically"),
        ("safari", "Safari", "com.apple.mobilesafari"),
        ("settings", "Settings", "com.apple.Preferences"),
        ("notes", "Notes", "com.apple.mobilenotes"),
        ("reminders", "Reminders", "com.apple.reminders"),
        ("calendar", "Calendar", "com.apple.mobilecal"),
        ("contacts", "Contacts", "com.apple.MobileAddressBook"),
        ("files", "Files", "com.apple.DocumentsApp"),
        ("photos", "Photos", "com.apple.mobileslideshow"),
        ("maps", "Maps", "com.apple.Maps"),
        ("clock", "Clock", "com.apple.mobiletimer"),
        ("calculator", "Calculator", "com.apple.calculator"),
        ("shortcuts", "Shortcuts", "com.apple.shortcuts"),
        ("mail", "Mail", "com.apple.mobilemail"),
        ("messages", "Messages", "com.apple.MobileSMS"),
        ("music", "Music", "com.apple.Music"),
        ("podcasts", "Podcasts", "com.apple.podcasts"),
        ("weather", "Weather", "com.apple.weather"),
        ("books", "Books", "com.apple.iBooks"),
        ("voice-memos", "Voice Memos", "com.apple.VoiceMemos"),
        ("camera", "Camera", "com.apple.camera"),
        ("app-store", "App Store", "com.apple.AppStore"),
        ("health", "Health", "com.apple.Health"),
        ("fitness", "Fitness", "com.apple.Fitness"),
        ("home", "Home", "com.apple.Home"),
        ("find-my", "Find My", "com.apple.findmy"),
        ("wallet", "Wallet", "com.apple.Passbook"),
        ("translate", "Translate", "com.apple.Translate"),
        ("phone", "Phone", "com.apple.mobilephone"),
        ("face-time", "FaceTime", "com.apple.facetime"),
    ]
]


_EXTRA_NAMES = {"com.google.chrome.ios": "Chrome", "org.mozilla.ios.Firefox": "Firefox", "com.microsoft.msedge": "Edge",
                "com.brave.ios.browser": "Brave", "com.duckduckgo.mobile.ios": "DuckDuckGo",
                "com.opera.OperaTouch": "Opera"}


def register_app_names(names):
    """{bundle_id: display name} for apps the catalog does not list."""
    _EXTRA_NAMES.update({str(bundle): str(name)[:60] for bundle, name in names.items() if bundle and name})


def app_name(bundle):
    """The app's display name ("Safari"), or None when the catalog and the registered names don't have it."""
    return _EXTRA_NAMES.get(bundle) or next((a["name"] for a in APPS if a["bundleId"] == bundle), None)


def pack_apps():
    """(key, bundle id, name) of each app Mobster has an app pack for (Instagram, X, YouTube...), or () without packs.
    A saved task may name these before the phone lists them; the run says so if the phone doesn't have one."""
    try:
        from .packs.register import all_packs
        return tuple((pack.key, pack.bundle, pack.name) for pack in all_packs())
    except Exception:  # noqa: BLE001 -- no packs track: the catalog's apps only
        return ()


def app_label(bundle):
    """The app's display name with its bundle ("CalTrack (com.x.caltrack)"), else the bundle."""
    name = app_name(bundle)
    return f"{name} ({bundle})" if name else bundle
