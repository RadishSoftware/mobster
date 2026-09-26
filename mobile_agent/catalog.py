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


# Names for apps outside the catalog (a benchmark's own apps), registered by whoever installs them.
_EXTRA_NAMES = {}


def register_app_names(names):
    """{bundle_id: display name} for apps the catalog does not list."""
    _EXTRA_NAMES.update({str(bundle): str(name)[:60] for bundle, name in names.items() if bundle and name})


def app_label(bundle):
    """The app's display name with its bundle ("CalTrack (com.x.caltrack)"), else the bundle."""
    name = _EXTRA_NAMES.get(bundle) or next((a["name"] for a in APPS if a["bundleId"] == bundle), None)
    return f"{name} ({bundle})" if name else bundle
