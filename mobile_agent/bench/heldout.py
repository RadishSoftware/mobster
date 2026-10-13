"""The frontier held-out tranche (MobsterBench-iOS H1) and its training siblings.

The 68 tasks of v1 were tuned on over six passes, so a gain measured there says little. H1 is 16 tasks no
build had run when it was frozen (2026-10-09), written before any engine idea was built:

| tranche | n | what |
| --- | ---: | --- |
| short | 6 | one or two taps, or one record read (arm-A p50 is expected under 15 s) |
| retrieval | 4 | a fact from a fixture record or a Wikipedia article |
| multi_app | 4 | a fact from one app carried into another |
| text_entry | 2 | a search typed into Maps or Settings |

Every task uses only the MobsterBench fixture records (fixtures.RECORDS) and system apps, is read-only or
reversible (a search field, the Calculator display), and is graded by the same oracles as v1 (checks.py).
Nothing sends, buys, posts, deletes, calls or changes a setting.

TRAINING holds a sibling of each H1 template: the same app and shape with other values. Anything that
learns (saved programs, app cards) learns from TRAINING and the frozen v1 tasks only, never from H1.
H1's hash is in heldout-manifest.json; ``bench run --suite heldout`` refuses a changed tranche.
"""

import dataclasses
from .checks import ActedAtLeast, AnswerIs, Claimed, Fixture, InApp, Literal, OnScreen, Truth
from .suite import (BUNDLES, NO_CHANGE, READ_ONLY, READ_ONLY_HISTORY, REVERSIBLE_RESET, SAFARI, SETTINGS, WIKI,
                    LOOK_ONLY, Task, _canonical)

HELDOUT_NAME = "MobsterBench-iOS H1"
HELDOUT_VERSION = "1.0"
TRANCHES = ("short", "retrieval", "multi_app", "text_entry")
# (max steps, max seconds) per tranche, the same for every arm.
H_BUDGETS = {"short": (12, 90), "retrieval": (20, 150), "multi_app": (35, 300), "text_entry": (20, 150)}


def _h(id, tranche, apps, goal, checks, *, answer=(), **kw):
    steps, seconds = H_BUDGETS[tranche]
    return Task(id=id, category=tranche, apps=tuple(apps), goal=goal, checks=tuple(checks), answer=tuple(answer),
                steps=steps, seconds=seconds, **kw)


def _nav(id, title, goal, tranche="short", min_actions=1, **kw):
    return _h(id, tranche, ["settings"], goal + NO_CHANGE,
              [Claimed(), ActedAtLeast(min_actions), InApp(SETTINGS), OnScreen(SETTINGS, Literal(title))], **kw)


def _ask(id, tranche, apps, goal, field, expect, *, mode="text", alternatives=(), min_actions=1, extra=(), **kw):
    checks = [Claimed(), ActedAtLeast(min_actions), AnswerIs(field, expect, mode, tuple(alternatives)), *extra]
    return _h(id, tranche, apps, goal, checks, answer=((field, "string"),), **kw)


def build_heldout():
    return (
        # ------------------------------------------------------------ short (6)
        _nav("h.short.battery", "Battery", "In Settings, open Battery."),
        _nav("h.short.storage", "iPhone Storage", "In Settings, open General, then iPhone Storage.", min_actions=2),
        _nav("h.short.screen_time", "Screen Time", "In Settings, open Screen Time."),
        _nav("h.short.camera", "Camera", "In Settings, open Camera."),
        _ask("h.short.note_link", "short", ["notes"],
             "In Notes, open the note titled 'MobsterBench Note' and report the web address written in it."
             + LOOK_ONLY, "web_address", Literal(r"(https?://)?en\.m\.wikipedia\.org/wiki/alan_turing/?"),
             mode="regex", fixtures=("notes.bench",)),
        _ask("h.short.contact_city", "short", ["contacts"],
             "In Contacts, open the contact Bench Tester and report the city in the address. Do not call, "
             "message or edit the contact.", "city", Fixture("contacts.bench", "city"),
             fixtures=("contacts.bench",)),

        # ------------------------------------------------------------ retrieval (4)
        _ask("h.ret.calendar_start", "retrieval", ["calendar"],
             "In Calendar, find the event 'MobsterBench Review' on 15 October 2026 and report its start time. "
             "Do not edit, delete or respond to any event.", "start_time",
             Literal(r"(2(:00)? ?p\.?m\.?|14[:.]00)"), mode="regex", fixtures=("calendar.bench",),
             safety=REVERSIBLE_RESET, allow=("text_entry",)),
        _ask("h.ret.reminder_books", "retrieval", ["reminders"],
             "In Reminders, open the list named MobsterBench and report the reminder that mentions books, "
             "exactly as written. Do not complete, edit or add reminders.", "reminder",
             Literal("Return library books"), fixtures=("reminders.bench",)),
        _ask("h.ret.turing_birthplace", "retrieval", ["safari"],
             "According to this Wikipedia article, in which city was Alan Turing born?", "city",
             Literal(r"(.*\b)?london(\b.*)?|maida vale.*"), mode="regex", min_actions=0,
             start_url=WIKI + "Alan_Turing", safety=READ_ONLY_HISTORY, extra=[InApp(SAFARI)]),
        _ask("h.ret.titanic_year", "retrieval", ["safari"],
             "According to this Wikipedia article, in what year did the Titanic sink?", "year", Literal("1912"),
             mode="number", min_actions=0, start_url=WIKI + "Titanic", safety=READ_ONLY_HISTORY,
             extra=[InApp(SAFARI)]),

        # ------------------------------------------------------------ multi-app (4)
        _ask("h.multi.note_plus", "multi_app", ["notes", "calculator"],
             "Read the locker code in the note 'MobsterBench Note' in Notes, then use Calculator to add 1000 to "
             "it, and report the result." + LOOK_ONLY, "result", Literal("5817"), mode="number", min_actions=4,
             fixtures=("notes.bench",), safety=REVERSIBLE_RESET, allow=("calculator_keys",)),
        _ask("h.multi.contact_county", "multi_app", ["contacts", "safari"],
             "In Contacts, find the city in Bench Tester's address, then in Safari open the English Wikipedia "
             "article about that city and report the county it is in. Do not call, message or edit the contact.",
             "county", Literal(r"santa clara( county)?(,? california)?"), mode="regex", min_actions=3,
             fixtures=("contacts.bench",), safety=READ_ONLY_HISTORY, allow=("text_entry",)),
        _ask("h.multi.room_times", "multi_app", ["calendar", "calculator"],
             "In Calendar, find the location of the event 'MobsterBench Review' on 15 October 2026, then use "
             "Calculator to multiply the room number in it by 3, and report the result. Do not edit any event.",
             "result", Literal("126"), mode="number", min_actions=4, fixtures=("calendar.bench",),
             safety=REVERSIBLE_RESET, allow=("calculator_keys",)),
        _ask("h.multi.phone_minus", "multi_app", ["contacts", "calculator"],
             "In Contacts, read the last four digits of Bench Tester's phone number, then use Calculator to "
             "subtract 77 from that four-digit number, and report the result. Do not call, message or edit the "
             "contact.", "result", Literal("4400"), mode="number", min_actions=4, fixtures=("contacts.bench",),
             safety=REVERSIBLE_RESET, allow=("calculator_keys",)),

        # ------------------------------------------------------------ text entry (2)
        _ask("h.text.maps_bridge", "text_entry", ["maps"],
             "In Maps, search for the Golden Gate Bridge and report the city it is in. Do not start directions, "
             "save, share or report anything.", "city", Literal("San Francisco"), alternatives=("SF",),
             safety=READ_ONLY_HISTORY, allow=("text_entry",)),
        _h("h.text.settings_wallpaper", "text_entry", ["settings"],
           "Use the search field in Settings to find Wallpaper and open the Wallpaper screen." + NO_CHANGE,
           [Claimed(), ActedAtLeast(2), InApp(SETTINGS), OnScreen(SETTINGS, Literal("Wallpaper"))],
           safety=REVERSIBLE_RESET, allow=("text_entry",)),
    )


def build_training():
    """Siblings of the H1 templates (other values), for anything that learns. Never graded as held-out."""
    return (
        _nav("t.short.sounds", "Sounds & Haptics", "In Settings, open Sounds & Haptics."),
        _nav("t.short.airdrop", "AirDrop", "In Settings, open General, then AirDrop.", min_actions=2),
        _nav("t.short.focus", "Focus", "In Settings, open Focus."),
        _ask("t.short.note_code", "short", ["notes"],
             "In Notes, open the note titled 'MobsterBench Note' and report the locker code written in it."
             + LOOK_ONLY, "code", Fixture("notes.bench", "code"), fixtures=("notes.bench",)),
        _ask("t.short.contact_company", "short", ["contacts"],
             "In Contacts, open the contact Bench Tester and report the company name. Do not call, message or "
             "edit the contact.", "company", Fixture("contacts.bench", "company"), fixtures=("contacts.bench",)),
        _ask("t.ret.calendar_location", "retrieval", ["calendar"],
             "In Calendar, find the event 'MobsterBench Review' on 15 October 2026 and report its location. "
             "Do not edit, delete or respond to any event.", "location", Fixture("calendar.bench", "location"),
             fixtures=("calendar.bench",), safety=REVERSIBLE_RESET, allow=("text_entry",)),
        _ask("t.ret.reminder_plants", "retrieval", ["reminders"],
             "In Reminders, open the list named MobsterBench and report the reminder that mentions plants, "
             "exactly as written. Do not complete, edit or add reminders.", "reminder",
             Literal("Water the plants"), fixtures=("reminders.bench",)),
        _ask("t.ret.hopper_birthplace", "retrieval", ["safari"],
             "According to this Wikipedia article, in which city was Grace Hopper born?", "city",
             Literal(r"(.*\b)?new york( city)?(\b.*)?"), mode="regex", min_actions=0,
             start_url=WIKI + "Grace_Hopper", safety=READ_ONLY_HISTORY),
        _ask("t.multi.note_minus", "multi_app", ["notes", "calculator"],
             "Read the locker code in the note 'MobsterBench Note' in Notes, then use Calculator to subtract 800 "
             "from it, and report the result." + LOOK_ONLY, "result", Literal("4017"), mode="number",
             min_actions=4, fixtures=("notes.bench",), safety=REVERSIBLE_RESET, allow=("calculator_keys",)),
        _ask("t.multi.room_plus", "multi_app", ["calendar", "calculator"],
             "In Calendar, find the location of the event 'MobsterBench Review' on 15 October 2026, then use "
             "Calculator to add 100 to the room number in it, and report the result. Do not edit any event.",
             "result", Literal("142"), mode="number", min_actions=4, fixtures=("calendar.bench",),
             safety=REVERSIBLE_RESET, allow=("calculator_keys",)),
        _ask("t.multi.phone_plus", "multi_app", ["contacts", "calculator"],
             "In Contacts, read the last four digits of Bench Tester's phone number, then use Calculator to add "
             "23 to that four-digit number, and report the result. Do not call, message or edit the contact.",
             "result", Literal("4500"), mode="number", min_actions=4, fixtures=("contacts.bench",),
             safety=REVERSIBLE_RESET, allow=("calculator_keys",)),
        _ask("t.text.maps_liberty", "text_entry", ["maps"],
             "In Maps, search for the Statue of Liberty and report the city it is in. Do not start directions, "
             "save, share or report anything.", "city", Literal("New York"),
             alternatives=("New York City", "NYC", "New York, NY"), safety=READ_ONLY_HISTORY,
             allow=("text_entry",)),
    )


# The hard set (H-hard, 9 Oct, owner: "harder benchmark problems ... long horizon, complex"): long multi-app chains,
# web testing and library counts, for heats between engine variants (bench/heat.py). Not held out: it is for
# telling architectures apart by progress. Each task lists milestones: (name, app or None, text seen on a screen).
HARD_BUDGET = (40, 360)


def _hard(id, apps, goal, field, expect, *, mode="text", milestones=(), **kw):
    task = Task(id=id, category="hard", apps=tuple(apps), goal=goal,
                checks=(Claimed(), AnswerIs(field, expect, mode)), answer=((field, "string"),),
                steps=HARD_BUDGET[0], seconds=HARD_BUDGET[1], tags=tuple(f"milestone:{a or '*'}:{t}"
                                                                        for a, t in milestones), **kw)
    return task


def build_hard():
    return (
        _hard("hard.four_apps", ["notes", "reminders", "calendar", "calculator"],
              "Take the locker code from the note 'MobsterBench Note' in Notes, add the number of incomplete "
              "reminders in the Reminders list MobsterBench, multiply that sum by the room number in the location "
              "of the Calendar event 'MobsterBench Review' on 15 October 2026, using Calculator for the arithmetic, "
              "and report the result. Do not change any note, reminder or event.", "result", Literal("202440"),
              mode="number", fixtures=("notes.bench", "reminders.bench", "calendar.bench"), safety=REVERSIBLE_RESET,
              allow=("calculator_keys", "text_entry"),
              milestones=(("Notes", "4817"), ("Reminders", "Water the plants"), ("Calendar", "Room 42"),
                          ("Calculator", "4,820"), ("Calculator", "202,440"))),
        _hard("hard.city_incorporated", ["contacts", "safari"],
              "In Contacts, find the city in Bench Tester's address. Then in Safari open the English Wikipedia "
              "article about that city and report the year the city was incorporated. Do not call, message or edit "
              "the contact.", "year", Literal("1955"), mode="number", fixtures=("contacts.bench",),
              safety=READ_ONLY_HISTORY, allow=("text_entry",),
              milestones=(("Contacts", "Cupertino"), ("Safari", "Cupertino"), ("Safari", "Incorporated"),
                          ("Safari", "1955"))),
        _hard("hard.turing_years", ["notes", "safari", "calculator"],
              "Open the note 'MobsterBench Note' in Notes and open the web link written in it in Safari. Find the "
              "year Alan Turing's paper 'On Computable Numbers' was published, then use Calculator to subtract that "
              "year from 2026, and report the result." + LOOK_ONLY, "result", Literal("90"), mode="number",
              fixtures=("notes.bench",), safety=REVERSIBLE_RESET, allow=("calculator_keys", "text_entry"),
              milestones=(("Notes", "Alan_Turing"), ("Safari", "Alan Turing"), ("Safari", "Computable Numbers"),
                          ("Calculator", "90"))),
        _hard("hard.files_folders", ["files"],
              "In Files, open On My iPhone, then the MobsterBench folder, and report how many folders it contains."
              + LOOK_ONLY, "count", Literal("4"), mode="number", fixtures=("files.dogs12",),
              milestones=(("Files", "On My iPhone"), ("Files", "eyes8"))),
        _hard("hard.album_count", ["photos"],
              "In Photos, find the album 'MobsterBench Receipts' and report how many photos it contains." + LOOK_ONLY,
              "count", Literal("12"), mode="number", fixtures=("photos.receipts12",),
              milestones=(("Photos", "Albums"), ("Photos", "MobsterBench Receipts"))),
        _hard("hard.reminders_sorted", ["reminders"],
              "In Reminders, open the list MobsterBench and report its incomplete reminders in alphabetical order, "
              "separated by commas. Do not complete, edit or add reminders.", "reminders",
              Literal(r"pick up dry cleaning[,;]? (and )?return library books[,;]? (and )?water the plants"),
              mode="regex", fixtures=("reminders.bench",),
              milestones=(("Reminders", "MobsterBench"), ("Reminders", "Pick up dry cleaning"))),
        _hard("hard.example_iana", ["safari"],
              "In Safari, on this page follow the 'More information' link and report the main heading of the page it "
              "opens.", "heading", Literal("Example Domains"), start_url="https://example.com",
              safety=READ_ONLY_HISTORY, milestones=(("Safari", "More information"), ("Safari", "Example Domains"))),
    )


def milestones(task):
    """[(app or None, text)] from a hard task's tags."""
    out = []
    for tag in task.tags:
        if tag.startswith("milestone:"):
            _, app, text = tag.split(":", 2)
            out.append((None if app == "*" else app, text))
    return out


COMBO_IDS = ("t.short.sounds", "t.short.contact_company", "t.ret.calendar_location", "t.ret.hopper_birthplace",
             "t.multi.note_minus", "t.text.maps_liberty")


def build_combo():
    """The small, varied set for reading engine combinations by hand (never H1): six training siblings (short,
    retrieval, web, multi-app, text entry) and one long v1 task (Settings, then Safari by URL, then a date)."""
    from .suite import build_suite
    training = {t.id: t for t in build_training()}
    return tuple(training[i] for i in COMBO_IDS) + tuple(t for t in build_suite() if t.id == "multi.ios_release")


def heldout_document(tasks=None):
    tasks = build_heldout() if tasks is None else tasks
    return {"suite": HELDOUT_NAME, "version": HELDOUT_VERSION, "budgets": H_BUDGETS, "bundles": BUNDLES,
            "tasks": [_canonical(task) for task in tasks]}


def heldout_hash(tasks=None):
    import hashlib
    import json
    blob = json.dumps(heldout_document(tasks), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def tranche_counts(tasks=None):
    counts = {}
    for task in tasks or build_heldout():
        counts[task.category] = counts.get(task.category, 0) + 1
    return counts


READ_ONLY_CLASSES = (READ_ONLY, READ_ONLY_HISTORY, REVERSIBLE_RESET)


# ---------------------------------------------------------------- H2 (frozen 2026-10-10, before any run)
# The harder held-out tranche for the engine-on A/B (owner, 10 Oct): multi-field answers, three-app chains, two-hop
# web lookups and counts, on the same fixtures and system apps. Written and hashed (h2-manifest.json) before any
# engine ran it; never tuned on.
H2_NAME, H2_VERSION = "MobsterBench-iOS H2", "1.0"
H2_BUDGET = (30, 240)


def _h2(id, apps, goal, answers, *, min_actions=1, **kw):
    """``answers``: ((field, expect, mode), ...), each checked on its own."""
    checks = [Claimed(), ActedAtLeast(min_actions)] + [AnswerIs(field, expect, mode) for field, expect, mode in answers]
    return Task(id=id, category="h2", apps=tuple(apps), goal=goal, checks=tuple(checks),
                answer=tuple((field, "string") for field, _, _ in answers), steps=H2_BUDGET[0], seconds=H2_BUDGET[1], **kw)


def build_h2():
    return (
        _h2("h2.calendar_three", ["calendar"],
            "In Calendar, find the event 'MobsterBench Review' on 15 October 2026 and report its start time, its end "
            "time and the day of the week it falls on. Do not edit, delete or respond to any event.",
            (("start_time", Literal(r"(2(:00)? ?p\.?m\.?|14[:.]00)"), "regex"),
             ("end_time", Literal(r"(3(:00)? ?p\.?m\.?|15[:.]00)"), "regex"),
             ("weekday", Literal("Thursday"), "text")),
            fixtures=("calendar.bench",), safety=REVERSIBLE_RESET, allow=("text_entry",)),
        _h2("h2.contact_two", ["contacts"],
            "In Contacts, open the contact Bench Tester and report the company name and the city in the address. "
            "Do not call, message or edit the contact.",
            (("company", Fixture("contacts.bench", "company"), "text"), ("city", Fixture("contacts.bench", "city"), "text")),
            fixtures=("contacts.bench",)),
        _h2("h2.about_two", ["settings"],
            "In Settings > General > About, report the iOS version and the Model Name." + NO_CHANGE,
            (("ios_version", Truth("device.ios_version"), "text"), ("model_name", Truth("device.model_name"), "text")),
            min_actions=2),
        _h2("h2.receipts_count", ["files"],
            "In Files, open On My iPhone > MobsterBench > receipts12 and report how many files the folder contains."
            + LOOK_ONLY, (("count", Literal("12"), "number"),), min_actions=2, fixtures=("files.receipts12",)),
        _h2("h2.room_plus_phone", ["calendar", "contacts", "calculator"],
            "Find the room number in the location of the Calendar event 'MobsterBench Review' on 15 October 2026, "
            "then read the last four digits of Bench Tester's phone number in Contacts, then use Calculator to add the "
            "two numbers, and report the sum. Do not edit, call or message anything.",
            (("sum", Literal("4519"), "number"),), min_actions=5,
            fixtures=("calendar.bench", "contacts.bench"), safety=REVERSIBLE_RESET, allow=("calculator_keys", "text_entry")),
        _h2("h2.code_digit_sum", ["notes", "calculator"],
            "Read the locker code in the note 'MobsterBench Note' in Notes, then use Calculator to add up its four "
            "digits, and report the total." + LOOK_ONLY, (("total", Literal("20"), "number"),), min_actions=5,
            fixtures=("notes.bench",), safety=REVERSIBLE_RESET, allow=("calculator_keys",)),
        _h2("h2.maps_alcatraz", ["maps"],
            "In Maps, search for Alcatraz Island and report the city it belongs to. Do not start directions, save, "
            "share or report anything.", (("city", Literal(r"(.*\b)?san francisco(\b.*)?"), "regex"),),
            safety=READ_ONLY_HISTORY, allow=("text_entry",)),
        _h2("h2.bridge_opened", ["safari"],
            "According to this Wikipedia article, in what year did the bridge open to the public?",
            (("year", Literal("1937"), "number"),), min_actions=0, start_url=WIKI + "Golden_Gate_Bridge",
            safety=READ_ONLY_HISTORY),
        _h2("h2.turing_phd_university", ["safari"],
            "From this Wikipedia article, open the article about the university where Alan Turing earned his PhD, and "
            "report the year that university was founded.", (("founded", Literal("1746"), "number"),),
            start_url=WIKI + "Alan_Turing", safety=READ_ONLY_HISTORY),
        _h2("h2.reminder_last_alpha", ["reminders"],
            "In Reminders, open the list MobsterBench and report which of its incomplete reminders comes last in "
            "alphabetical order, exactly as written. Do not complete, edit or add reminders.",
            (("reminder", Literal("Water the plants"), "text"),), fixtures=("reminders.bench",)),
    )


# H3 (10 Oct 2026): a fresh held-out tranche for the default decision (owner: the routed engine becomes the default
# only when, on a tranche it was never tuned on, its median and p90 per task are at least as fast as Sonnet's with
# success no worse). Written and hashed (h3-manifest.json) before any engine ran it; H2 is tuned on since 10 Oct.
H3_NAME, H3_VERSION = "MobsterBench-iOS H3", "1.0"
H3_BUDGET = H2_BUDGET


def _h3(id, apps, goal, answers, **kw):
    task = _h2(id, apps, goal, answers, **kw)
    return dataclasses.replace(task, category="h3", steps=H3_BUDGET[0], seconds=H3_BUDGET[1])


def build_h3():
    return (
        _h3("h3.hopper_retired", ["safari"],
            "According to this Wikipedia article, in what year did Grace Hopper finally retire from the US Navy?",
            (("year", Literal("1986"), "number"),), min_actions=0, start_url=WIKI + "Grace_Hopper",
            safety=READ_ONLY_HISTORY),
        _h3("h3.lovelace_father", ["safari"],
            "In Safari, open the English Wikipedia article about Ada Lovelace and report her father's name.",
            (("father", Literal(r"(.*\b)?byron(\b.*)?"), "regex"),), safety=READ_ONLY_HISTORY, allow=("text_entry",)),
        _h3("h3.eiffel_height", ["safari"],
            "In Safari, open the English Wikipedia article about the Eiffel Tower and report its height in metres "
            "(the total height, with antennas).", (("height_m", Literal("330"), "number"),), safety=READ_ONLY_HISTORY,
            allow=("text_entry",)),
        _h3("h3.apollo_cmp", ["safari"],
            "According to this Wikipedia article, who was the command module pilot of the mission?",
            (("pilot", Literal(r"(.*\b)?collins(\b.*)?"), "regex"),), min_actions=0, start_url=WIKI + "Apollo_11",
            safety=READ_ONLY_HISTORY),
        _h3("h3.python_designer", ["safari"],
            "In Safari, open the English Wikipedia article about the Python programming language and report who "
            "designed it.", (("designer", Literal(r"(.*\b)?guido van rossum(\b.*)?"), "regex"),),
            safety=READ_ONLY_HISTORY, allow=("text_entry",)),
        _h3("h3.coit_city", ["maps"],
            "In Maps, search for Coit Tower and report the city it is in. Do not start directions, save, share or "
            "report anything.", (("city", Literal(r"(.*\b)?san francisco(\b.*)?"), "regex"),),
            safety=READ_ONLY_HISTORY, allow=("text_entry",)),
        _h3("h3.space_needle_two", ["maps", "safari"],
            "In Maps, search for the Space Needle and report the city it is in, then in Safari open the English "
            "Wikipedia article about the Space Needle and report the year it opened. Do not start directions, save "
            "or share anything.", (("city", Literal(r"(.*\b)?seattle(\b.*)?"), "regex"),
                                    ("opened", Literal("1962"), "number")),
            min_actions=3, safety=READ_ONLY_HISTORY, allow=("text_entry",)),
        _h3("h3.sunglasses_count", ["files"],
            "In Files, open On My iPhone > MobsterBench > sunglasses12 and report how many files the folder contains."
            + LOOK_ONLY, (("count", Literal("12"), "number"),), min_actions=2, fixtures=("files.sunglasses12",)),
        _h3("h3.locker_and_room", ["notes", "calendar"],
            "Report the locker code written in the note 'MobsterBench Note' in Notes, and the room number in the "
            "location of the Calendar event 'MobsterBench Review' on 15 October 2026. Do not edit anything.",
            (("locker_code", Literal("4817"), "number"), ("room", Literal("42"), "number")), min_actions=3,
            fixtures=("notes.bench", "calendar.bench"), safety=REVERSIBLE_RESET, allow=("text_entry",)),
        _h3("h3.phone_minus_room", ["contacts", "calendar", "calculator"],
            "Read the last four digits of Bench Tester's phone number in Contacts and the room number in the location "
            "of the Calendar event 'MobsterBench Review' on 15 October 2026, then use Calculator to subtract the room "
            "number from the four digits, and report the result. Do not edit, call or message anything.",
            (("result", Literal("4435"), "number"),), min_actions=5, fixtures=("contacts.bench", "calendar.bench"),
            safety=REVERSIBLE_RESET, allow=("calculator_keys", "text_entry")),
    )


def h3_hash(tasks=None):
    import hashlib
    import json
    tasks = build_h3() if tasks is None else tasks
    blob = json.dumps({"suite": H3_NAME, "version": H3_VERSION, "budget": H3_BUDGET, "bundles": BUNDLES,
                       "tasks": [_canonical(t) for t in tasks]}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def h2_hash(tasks=None):
    import hashlib
    import json
    tasks = build_h2() if tasks is None else tasks
    blob = json.dumps({"suite": H2_NAME, "version": H2_VERSION, "budget": H2_BUDGET, "bundles": BUNDLES,
                       "tasks": [_canonical(t) for t in tasks]}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()
