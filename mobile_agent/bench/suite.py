"""The pre-registered task suite (MobsterBench-iOS v1).

Written and frozen before any benchmark run: ``python -m mobile_agent.bench
freeze`` stores the SHA-256 of the canonical suite (tasks, checks, budgets,
fixture ground truth and the verdict rule) in ``manifest.json``, and the runner
refuses to run a suite whose hash differs. Changing a task therefore shows up
as a manifest diff, never silently.

Every task has: a category, a start state the harness resets to, an
independent oracle (probe-read device state and/or ground truth fixed before
the run), a safety class, a step budget and a time budget. No task sends,
buys, posts, deletes, likes, calls, or changes account, security or
connectivity settings; ``safety.py`` blocks any action that would.

Ground truth kinds (see ``checks.py``): ``Literal`` (stable public facts,
written here), ``Truth`` (per-device values in ``truth/<device>.json``,
recorded by the harness with read-only navigation before a run), ``Captured``
(read by the probe from the start screen at each attempt's reset, for state
that can legitimately change between days) and ``Fixture`` (operator-installed
records and labelled image sets, ``fixtures.py``).
"""

from dataclasses import dataclass, fields, is_dataclass
import hashlib
import json

from .checks import (
    Abstains, ActedAtLeast, AnswerIs, Captured, Claimed, Fixture, FixtureIntact, InApp, ItemLabels,
    ItemSet, Literal, OnScreen, Shows, Truth,
)

SUITE_NAME = "MobsterBench-iOS"
SUITE_VERSION = "1.0"

CATEGORIES = ("navigation", "retrieval", "settings_state", "text_entry", "multi_app", "web",
              "scroll", "visual")

# Safety classes. None of them persists a change to user data or settings.
READ_ONLY = "read_only"                  # observe and navigate only
READ_ONLY_HISTORY = "read_only_history"  # also leaves local history (Safari/Maps recents, search)
REVERSIBLE_RESET = "reversible_reset"    # transient UI state (a search field, Calculator display)
SAFETY_CLASSES = (READ_ONLY, READ_ONLY_HISTORY, REVERSIBLE_RESET)

BUNDLES = {
    "settings": "com.apple.Preferences", "safari": "com.apple.mobilesafari",
    "notes": "com.apple.mobilenotes", "reminders": "com.apple.reminders",
    "calendar": "com.apple.mobilecal", "contacts": "com.apple.MobileAddressBook",
    "files": "com.apple.DocumentsApp", "photos": "com.apple.mobileslideshow",
    "maps": "com.apple.Maps", "clock": "com.apple.mobiletimer", "calculator": "com.apple.calculator",
}
SETTINGS, SAFARI = BUNDLES["settings"], BUNDLES["safari"]
WIKI = "https://en.m.wikipedia.org/wiki/"

# Well-known benchmark task types each category mirrors (closest analogue, not a port).
MIRRORS = {
    "navigation": ("AndroidLab operation tasks (navigate-to-screen)", "iOSWorld Settings navigation",
                   "MobileAgentBench open-screen tasks"),
    "retrieval": ("AndroidLab query tasks", "SPA-Bench single-app information retrieval"),
    "settings_state": ("AndroidWorld System* state tasks (read-only variant)", "AndroidLab Settings queries"),
    "text_entry": ("AndroidWorld search/typing sub-goals", "MobileAgentBench search tasks"),
    "multi_app": ("SPA-Bench cross-app tasks", "AndroidWorld multi-app information transfer (read-only)"),
    "web": ("WebArena/Mind2Web information seeking (mobile Safari)", "SPA-Bench web tasks"),
    "scroll": ("AndroidLens long-horizon scrolling", "iOSWorld scroll-to-target"),
    "visual": ("VisualWebArena visual predicates", "AndroidWorld batch tasks (dry-run visual variant)"),
    "abstention": ("AndroidLab infeasible tasks",),
}

BUDGETS = {  # (max steps, max seconds) — identical for every agent
    "navigation": (15, 90), "retrieval": (20, 120), "settings_state": (20, 120), "text_entry": (20, 150),
    "multi_app": (35, 300), "web": (25, 180), "scroll": (25, 180), "visual": (80, 480),
}


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    apps: tuple            # catalog ids; the first is the start app
    goal: str
    checks: tuple
    safety: str = READ_ONLY
    answer: tuple = ()     # ((field, "string" | "string_list" | "labels"), ...)
    start_url: str = ""
    fixtures: tuple = ()
    allow: tuple = ()      # extra permissions for the safety monitor: "text_entry", "calculator_keys"
    abstention: bool = False
    dry_run: bool = False
    tags: tuple = ()
    steps: int = 0
    seconds: float = 0

    @property
    def bundle(self):
        return BUNDLES[self.apps[0]]

    @property
    def bundles(self):
        return tuple(BUNDLES[app] for app in self.apps)

    @property
    def max_steps(self):
        return self.steps or BUDGETS[self.category][0]

    @property
    def max_seconds(self):
        return self.seconds or BUDGETS[self.category][1]

    @property
    def mirrors(self):
        return MIRRORS[self.category] + (MIRRORS["abstention"] if self.abstention else ())

    def answer_schema(self):
        """The same field names for every agent: Mobster gets it as output_schema, baselines in the prompt."""
        if not self.answer:
            return None
        types = {"string": {"type": "string"},
                 "string_list": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                 "labels": {"type": "array", "items": {
                     "type": "object", "properties": {"file": {"type": "string"}, "label": {"type": "string"}},
                     "required": ["file", "label"], "additionalProperties": False}, "maxItems": 100}}
        return {"type": "object", "properties": {name: types[kind] for name, kind in self.answer},
                "required": [name for name, _ in self.answer], "additionalProperties": False}


def _t(id, category, apps, goal, checks, **kw):
    return Task(id=id, category=category, apps=tuple(apps), goal=goal, checks=tuple(checks), **kw)


NO_CHANGE = " Do not change any setting."
LOOK_ONLY = " Only look; do not open, favorite, share, edit or delete anything."


def _nav(id, title_key, goal, *, category="navigation", min_actions=1, tags=()):
    return _t(id, category, ["settings"], goal + NO_CHANGE,
              [Claimed(), ActedAtLeast(min_actions), InApp(SETTINGS), OnScreen(SETTINGS, Truth(title_key))],
              tags=tags)


def _answer(id, category, apps, goal, field, expect, *, mode="text", alternatives=(), extra=(),
            start_url="", fixtures=(), safety=READ_ONLY, allow=(), min_actions=0, tags=()):
    checks = [Claimed(), AnswerIs(field, expect, mode, tuple(alternatives)), *extra]
    if min_actions:
        checks.insert(1, ActedAtLeast(min_actions))
    return _t(id, category, apps, goal, checks, answer=((field, "string"),), start_url=start_url,
              fixtures=tuple(fixtures), safety=safety, allow=tuple(allow), tags=tuple(tags))


def _web(id, goal, field, expect, start, *, category="web", mode="text", alternatives=(), min_actions=0,
         shows=None, tags=()):
    extra = [InApp(SAFARI)] + ([Shows(SAFARI, shows)] if shows else [])
    return _answer(id, category, ["safari"], goal, field, expect, mode=mode, alternatives=alternatives,
                   extra=extra, start_url=start, safety=READ_ONLY_HISTORY,
                   allow=("text_entry",) if min_actions else (), min_actions=min_actions, tags=tags)


def _abstain(id, category, apps, goal, field, *, start_url="", safety=READ_ONLY):
    return _t(id, category, apps, goal, [Abstains()], answer=((field, "string"),), start_url=start_url,
              safety=safety, abstention=True)


def build_suite():
    s = SETTINGS
    tasks = [
        # ------------------------------------------------------------ navigation (10)
        _nav("nav.accessibility", "title.accessibility", "In Settings, open the Accessibility screen."),
        _nav("nav.keyboards", "title.keyboards", "In Settings, open General, then Keyboard.", min_actions=2),
        _nav("nav.location_services", "title.location_services",
             "In Settings, open Privacy & Security, then Location Services.", min_actions=2),
        _nav("nav.display", "title.display", "In Settings, open Display & Brightness."),
        _nav("nav.notifications", "title.notifications", "In Settings, open Notifications."),
        _nav("nav.date_time", "title.date_time", "In Settings, open General, then Date & Time.", min_actions=2),
        _nav("nav.about", "title.about", "In Settings, open General, then About.", min_actions=2),
        _nav("nav.camera_formats", "title.formats", "In Settings, open Camera, then Formats.", min_actions=2),
        _t("nav.files_on_my_iphone", "navigation", ["files"],
           "In Files, show the contents of the On My iPhone location." + LOOK_ONLY,
           [Claimed(), InApp(BUNDLES["files"]), OnScreen(BUNDLES["files"], Truth("title.files_on_my_iphone"))]),
        _t("nav.clock_stopwatch", "navigation", ["clock"],
           "In Clock, switch to the Stopwatch tab. Do not start, stop or reset anything.",
           [Claimed(), ActedAtLeast(1), InApp(BUNDLES["clock"]), Shows(BUNDLES["clock"], "Lap")]),

        # ------------------------------------------------------------ retrieval (10)
        _answer("ret.ios_version", "retrieval", ["settings"],
                "In Settings, open General, then About, and report the iOS version shown there." + NO_CHANGE,
                "ios_version", Truth("device.ios_version"), min_actions=2, extra=[OnScreen(s, Truth("title.about"))]),
        _answer("ret.model_number", "retrieval", ["settings"],
                "In Settings, open General, then About, and report the Model Number exactly as shown." + NO_CHANGE,
                "model_number", Truth("device.model_number"), min_actions=2),
        _answer("ret.model_name", "retrieval", ["settings"],
                "In Settings > General > About, report the Model Name (not the model number)." + NO_CHANGE,
                "model_name", Truth("device.model_name"), min_actions=2),
        _answer("ret.capacity", "retrieval", ["settings"],
                "In Settings > General > About, report the total storage Capacity (not Available)." + NO_CHANGE,
                "capacity", Truth("device.capacity"), mode="number", min_actions=2),
        _answer("ret.note_code", "retrieval", ["notes"],
                "In Notes, open the note titled 'MobsterBench Note' and report the locker code written in it."
                + LOOK_ONLY, "locker_code", Fixture("notes.bench", "code"), min_actions=1,
                fixtures=("notes.bench",)),
        _answer("ret.contact_company", "retrieval", ["contacts"],
                "In Contacts, open the contact Bench Tester and report the company name. Do not call, message "
                "or edit the contact.", "company", Fixture("contacts.bench", "company"), min_actions=1,
                fixtures=("contacts.bench",)),
        _answer("ret.reminder_count", "retrieval", ["reminders"],
                "In Reminders, open the list named MobsterBench and report how many incomplete reminders it "
                "has. Do not complete, edit or add reminders.", "count", Fixture("reminders.bench", "count"),
                mode="number", min_actions=1, fixtures=("reminders.bench",),
                extra=[FixtureIntact("reminders.bench")]),
        _answer("ret.calendar_location", "retrieval", ["calendar"],
                "In Calendar, find the event 'MobsterBench Review' on 15 October 2026 and report its location. "
                "Do not edit, delete or respond to any event.", "location", Fixture("calendar.bench", "location"),
                min_actions=1, fixtures=("calendar.bench",), allow=("text_entry",), safety=REVERSIBLE_RESET),
        _abstain("ret.absent.home_address", "retrieval", ["settings"],
                 "Report the device owner's home address as shown in Settings. If it is not shown, say so."
                 + NO_CHANGE, "home_address"),
        _abstain("ret.absent.passport", "retrieval", ["settings"],
                 "Report the device owner's passport number as shown in Settings. If it is not shown, say so."
                 + NO_CHANGE, "passport_number"),

        # ------------------------------------------------------------ settings state (9)
        _answer("state.bluetooth", "settings_state", ["settings"],
                "In Settings, is Bluetooth on or off? Answer 'on' or 'off'." + NO_CHANGE,
                "bluetooth", Captured("settings.bluetooth"), mode="bool"),
        _answer("state.airplane", "settings_state", ["settings"],
                "In Settings, is Airplane Mode on or off? Answer 'on' or 'off'." + NO_CHANGE,
                "airplane_mode", Captured("settings.airplane"), mode="bool"),
        _answer("state.wifi", "settings_state", ["settings"],
                "In Settings, is Wi-Fi turned on or off? Answer 'on' or 'off'." + NO_CHANGE,
                "wifi", Captured("settings.wifi"), mode="bool"),
        _answer("state.auto_lock", "settings_state", ["settings"],
                "In Settings, what is the Auto-Lock time set to (under Display & Brightness)?" + NO_CHANGE,
                "auto_lock", Truth("settings.auto_lock"), min_actions=1),
        _answer("state.low_power", "settings_state", ["settings"],
                "In Settings > Battery, is Low Power Mode on or off? Answer 'on' or 'off'." + NO_CHANGE,
                "low_power_mode", Truth("settings.low_power"), mode="bool", min_actions=1),
        _answer("state.time_auto", "settings_state", ["settings"],
                "In Settings > General > Date & Time, is 'Set Automatically' on or off? Answer 'on' or 'off'."
                + NO_CHANGE, "set_automatically", Truth("settings.time_auto"), mode="bool", min_actions=2),
        _answer("state.auto_correction", "settings_state", ["settings"],
                "In Settings > General > Keyboard, is Auto-Correction on or off? Answer 'on' or 'off'."
                + NO_CHANGE, "auto_correction", Truth("settings.auto_correction"), mode="bool", min_actions=2),
        _answer("state.region", "settings_state", ["settings"],
                "In Settings > General > Language & Region, which Region is selected?" + NO_CHANGE,
                "region", Truth("settings.region"), min_actions=2),
        _answer("state.bold_text", "settings_state", ["settings"],
                "In Settings > Accessibility > Display & Text Size, is Bold Text on or off? Answer 'on' or 'off'."
                + NO_CHANGE, "bold_text", Truth("settings.bold_text"), mode="bool", min_actions=2),

        # ------------------------------------------------------------ text entry and search (8)
        _t("text.settings_search", "text_entry", ["settings"],
           "Use the search field in Settings to find Auto-Lock and open the Auto-Lock screen." + NO_CHANGE,
           [Claimed(), ActedAtLeast(2), InApp(s), OnScreen(s, Truth("title.auto_lock"))],
           safety=REVERSIBLE_RESET, allow=("text_entry",)),
        _web("text.safari_url", "In Safari, go to en.m.wikipedia.org/wiki/Grace_Hopper and report the year "
             "Grace Hopper was born.", "birth_year", Literal("1906"), "https://example.com",
             category="text_entry", min_actions=1, shows="Hopper"),
        _web("text.safari_search", "Search the web for Hedy Lamarr, open her Wikipedia article, and report the "
             "year she was born.", "birth_year", Literal("1914"), "https://example.com",
             category="text_entry", min_actions=2, shows="Lamarr"),
        _answer("text.maps_search", "text_entry", ["maps"],
                "In Maps, search for the Eiffel Tower and report the city it is in. Do not start directions, "
                "save, share or report anything.", "city", Literal("Paris"), min_actions=1,
                safety=READ_ONLY_HISTORY, allow=("text_entry",)),
        _answer("text.notes_search", "text_entry", ["notes"],
                "In Notes, use search to find the note containing 'Locker code' and report the code." + LOOK_ONLY,
                "locker_code", Fixture("notes.bench", "code"), min_actions=1, fixtures=("notes.bench",),
                safety=REVERSIBLE_RESET, allow=("text_entry",)),
        _answer("text.contacts_search", "text_entry", ["contacts"],
                "In Contacts, search for Bench and report Bench Tester's phone number. Do not call, message or "
                "edit.", "phone", Fixture("contacts.bench", "phone"), mode="digits", min_actions=1,
                fixtures=("contacts.bench",), safety=REVERSIBLE_RESET, allow=("text_entry",)),
        _answer("text.calc_multiply", "text_entry", ["calculator"],
                "In Calculator, compute 5 times 5 using the keypad and report the result shown on the display.",
                "result", Literal("25"), mode="number", min_actions=3, safety=REVERSIBLE_RESET,
                allow=("calculator_keys",), tags=("known_gap_mobster_ax_drawn_display",)),
        _answer("text.calc_add", "text_entry", ["calculator"],
                "In Calculator, compute 123 plus 456 using the keypad and report the result shown on the display.",
                "result", Literal("579"), mode="number", min_actions=7, safety=REVERSIBLE_RESET,
                allow=("calculator_keys",), tags=("known_gap_mobster_ax_drawn_display",)),

        # ------------------------------------------------------------ multi-step / multi-app (6)
        _answer("multi.model_release", "multi_app", ["settings", "safari"],
                "Find this iPhone's Model Name in Settings > General > About, then in Safari open the Wikipedia "
                "article about that model and report the year it was released." + NO_CHANGE,
                "release_year", Truth("device.model_release_year"), min_actions=3,
                safety=READ_ONLY_HISTORY, allow=("text_entry",)),
        _answer("multi.note_link", "multi_app", ["notes", "safari"],
                "Open the note 'MobsterBench Note' in Notes, open the web link written in it in Safari, and "
                "report the birth year of the person the page is about." + LOOK_ONLY,
                "birth_year", Literal("1912"), min_actions=2, fixtures=("notes.bench",),
                safety=READ_ONLY_HISTORY, extra=[InApp(SAFARI), Shows(SAFARI, "Turing")]),
        _answer("multi.ios_release", "multi_app", ["settings", "safari"],
                "In Settings > General > About, read the major iOS version number (for example 18 or 26). Then in "
                "Safari open en.m.wikipedia.org/wiki/IOS_<that number> and report that version's initial "
                "release date." + NO_CHANGE, "release_date", Truth("device.ios_major_release_date"),
                mode="regex", min_actions=3, safety=READ_ONLY_HISTORY, allow=("text_entry",)),
        _answer("multi.reminders_calc", "multi_app", ["reminders", "calculator"],
                "Count the incomplete reminders in the Reminders list MobsterBench, then use Calculator to "
                "multiply that count by 7, and report the product. Do not change any reminder.",
                "product", Fixture("reminders.bench", "count_x7"), mode="number", min_actions=4,
                fixtures=("reminders.bench",), safety=REVERSIBLE_RESET, allow=("calculator_keys",)),
        _answer("multi.eiffel_calc", "multi_app", ["safari", "calculator"],
                "In Safari, find the year the Eiffel Tower was completed on its Wikipedia article, then use "
                "Calculator to subtract 1800 from that year, and report the result.",
                "result", Literal("89"), mode="number", min_actions=3, start_url=WIKI + "Eiffel_Tower",
                safety=REVERSIBLE_RESET, allow=("calculator_keys",)),
        _answer("multi.contact_state", "multi_app", ["contacts", "maps"],
                "In Contacts, find the city in Bench Tester's address, then search Maps for that city and report "
                "which US state it is in. Do not call, message, edit, share or start directions.",
                "state", Literal("California"), alternatives=("CA",), min_actions=2,
                fixtures=("contacts.bench",), safety=READ_ONLY_HISTORY, allow=("text_entry",)),

        # ------------------------------------------------------------ web retrieval (10)
        _web("web.heading", "Report the main heading of this web page.", "heading",
             Literal("Example Domain"), "https://example.com"),
        _web("web.eiffel_completed", "According to this Wikipedia article, in what year was construction of the "
             "Eiffel Tower completed?", "year", Literal("1889"), WIKI + "Eiffel_Tower"),
        _web("web.follow_link", "Open the Wikipedia article about the engineer the Eiffel Tower is named after, "
             "and report the year he was born.", "birth_year", Literal("1832"), WIKI + "Eiffel_Tower",
             shows="Gustave Eiffel"),
        _web("web.type_url", "In Safari, go to en.m.wikipedia.org/wiki/Guido_van_Rossum and report the year "
             "Guido van Rossum was born.", "birth_year", Literal("1956"), "https://example.com", min_actions=1,
             shows="Guido van Rossum"),
        _web("web.search", "Search the web for Ada Lovelace, open her Wikipedia article, and report the year "
             "she was born.", "birth_year", Literal("1815"), "https://example.com", min_actions=2,
             shows="Lovelace"),
        _web("web.everest_elevation", "According to this Wikipedia article, what is Mount Everest's elevation in "
             "metres?", "elevation_m", Literal("8849"), WIKI + "Mount_Everest", mode="number"),
        _web("web.gold_number", "According to this Wikipedia article, what is the atomic number of gold?",
             "atomic_number", Literal("79"), WIKI + "Gold", mode="number"),
        _web("web.apollo_landing", "According to this Wikipedia article, on what date did Apollo 11 land on the "
             "Moon?", "landing_date", Literal(r"(july 20,? 1969|20 july 1969|1969-07-20)"),
             WIKI + "Apollo_11", mode="regex"),
        _web("web.mona_lisa", "According to this Wikipedia article, who painted the Mona Lisa?", "artist",
             Literal("Leonardo da Vinci"), WIKI + "Mona_Lisa", alternatives=("Leonardo",)),
        _abstain("web.absent.phone", "web", ["safari"], "Report the phone number listed on this web page. "
                 "If there is none, say so.", "phone", start_url="https://example.com",
                 safety=READ_ONLY_HISTORY),

        # ------------------------------------------------------------ scrolling to off-screen targets (7)
        _nav("scroll.privacy", "title.privacy", "In Settings, open Privacy & Security.", category="scroll"),
        _nav("scroll.apps", "title.apps", "In Settings, open Apps (near the bottom of the list).",
             category="scroll"),
        _nav("scroll.legal", "title.legal", "In Settings, open General, then Legal & Regulatory.",
             category="scroll", min_actions=2),
        _nav("scroll.per_app", "title.per_app", "In Settings, open Accessibility, then Per-App Settings "
             "(at the bottom of the Accessibility screen).", category="scroll", min_actions=2),
        _answer("scroll.modem_firmware", "scroll", ["settings"],
                "In Settings > General > About, scroll down and report the Modem Firmware version." + NO_CHANGE,
                "modem_firmware", Truth("device.modem_firmware"), min_actions=3),
        _web("scroll.eiffel_tip", "According to this article's infobox, what is the Eiffel Tower's height to its "
             "tip, in metres? Scroll if needed.", "height_m", Literal("330"), WIKI + "Eiffel_Tower",
             category="scroll", mode="number"),
        _web("scroll.everest_first_ascent", "According to this Wikipedia article's infobox, on what date was "
             "Mount Everest first climbed? Scroll if needed.", "first_ascent",
             Literal(r"(29 may 1953|may 29,? 1953|1953-05-29)"), WIKI + "Mount_Everest", category="scroll",
             mode="regex"),
    ]
    tasks += visual_tasks()
    return tuple(tasks)


def visual_tasks():
    """Visual-condition iteration in DRY-RUN mode: judge every item, act on none."""
    dry = (" This is a dry run: only look at the images (you may open each one to view it and close it "
           "again). Do not favorite, share, rename, move, edit or delete anything.")

    def folder_set(id, set_key, question, field):
        folder = set_key.split(".", 1)[1]
        return _t(id, "visual", ["files"],
                  f"In Files, open On My iPhone > MobsterBench > {folder}. {question} Report the file names."
                  + dry, [Claimed(), ItemSet(field, set_key)], answer=((field, "string_list"),),
                  fixtures=(set_key,), dry_run=True)

    def album_count(id, set_key, album, question):
        return _t(id, "visual", ["photos"],
                  f"In Photos, open the album '{album}'. {question}" + dry,
                  [Claimed(), AnswerIs("count", Fixture(set_key, "positive_count"), "number")],
                  answer=(("count", "string"),), fixtures=("photos." + set_key.split(".", 1)[1],),
                  dry_run=True)

    return [
        folder_set("visual.files_dogs", "files.dogs12",
                   "Which of the 12 images contain a real dog (not a toy, statue, drawing or sign)?", "dog_files"),
        folder_set("visual.files_receipts", "files.receipts12",
                   "Which of the 12 images are receipts (a record of a purchase or payment)?", "receipt_files"),
        folder_set("visual.files_sunglasses", "files.sunglasses12",
                   "In which of the 12 images is a person wearing sunglasses on their face?", "sunglasses_files"),
        _t("visual.files_eye_colours", "visual", ["files"],
           "In Files, open On My iPhone > MobsterBench > eyes8. For each of the 8 images, report the eye colour "
           "of the person (blue, green, grey, hazel or brown), or 'unsure' when the eyes cannot be seen "
           "clearly enough to tell." + dry,
           [Claimed(), ItemLabels("eye_colours", "files.eyes8")], answer=(("eye_colours", "labels"),),
           fixtures=("files.eyes8",), dry_run=True, abstention=True),
        album_count("visual.album_dogs", "files.dogs12", "MobsterBench Dogs",
                    "How many of its photos contain a real dog (not a toy, statue, drawing or sign)?"),
        album_count("visual.album_receipts", "files.receipts12", "MobsterBench Receipts",
                    "How many of its photos are receipts?"),
        _t("visual.eye_abstain", "visual", ["files"],
           "In Files, open On My iPhone > MobsterBench > eyes8 and view eyes8-01. What colour are the person's "
           "eyes? If they cannot be seen clearly enough to tell, say that you cannot tell." + dry,
           [Abstains()], answer=(("eye_colour", "string"),), fixtures=("files.eyes8",), dry_run=True,
           abstention=True),
        _t("visual.eye_answer", "visual", ["files"],
           "In Files, open On My iPhone > MobsterBench > eyes8 and view eyes8-06. What colour are the person's "
           "eyes (blue, green, grey, hazel or brown)?" + dry,
           [Claimed(), AnswerIs("eye_colour", Fixture("files.eyes8", "item:6:accept"), "text")],
           answer=(("eye_colour", "string"),), fixtures=("files.eyes8",), dry_run=True),
    ]


# Decision rule, pre-registered with the suite (hashed with it). See report.py.
VERDICT_RULE = {
    "accuracy_margin_pp": 5.0,
    "text": ("Accuracy: per-task success rates are paired across agents; the 95% CI of the mean paired "
             "difference (Mobster minus the best baseline, bootstrap over tasks, 10,000 resamples) decides: "
             "'better' if the lower bound > 0, 'not worse' if it is > -5 pp, else 'worse'. Speed: on tasks both "
             "agents solved at least once, the median ratio of per-task p50 agent time (Mobster/baseline) with a "
             "bootstrap 95% CI decides: 'faster' if the upper bound < 1. Safety: any unsafe attempt by an agent is "
             "reported and disqualifies a state-of-the-art claim. Verdict 'state of the art on this suite' "
             "requires 'better' or 'not worse' accuracy AND 'faster' AND zero unsafe attempts against EVERY "
             "baseline that ran. Ungraded attempts are excluded and counted."),
}


def _canonical(value):
    if is_dataclass(value):
        return {"__type__": type(value).__name__,
                **{f.name: _canonical(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    return value


def suite_document(tasks=None, fixtures=None):
    from .fixtures import Fixtures
    tasks = build_suite() if tasks is None else tasks
    fixtures = fixtures or Fixtures()
    return {"suite": SUITE_NAME, "version": SUITE_VERSION, "budgets": BUDGETS, "verdict_rule": VERDICT_RULE,
            "tasks": [_canonical(task) for task in tasks], "fixtures": fixtures.frozen()}


def suite_hash(tasks=None, fixtures=None):
    blob = json.dumps(suite_document(tasks, fixtures), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def category_counts(tasks=None):
    counts = {}
    for task in tasks or build_suite():
        counts[task.category] = counts.get(task.category, 0) + 1
    return counts
