import unittest

from mobile_agent.packs import calendar as ca
from mobile_agent.packs import core, tools
from mobile_agent.packs import maps as mp
from mobile_agent.packs import notes as nt
from mobile_agent.packs import photos as ph
from mobile_agent.packs import safari as sf
from mobile_agent.packs import settings as st
from mobile_agent.tests.packs_support import Phone, el, ocr_file, rows_file, snap


def tree(app, name, pack):
    return rows_file(f"{app}/{name}.json", pack.bundle)


def picture(app, name, pack):
    return ocr_file(f"{app}/{name}.ocr.json", pack.bundle)


RECOGNIZED = {
    (st, "settings"): {"root": ("root", "root"), "about": ("about", "about"), "battery": ("battery", "battery"),
                       "storage": ("storage", "storage"), "power-mode": ("page", "page"),
                       "general": ("page", None), "display": ("page", None), "search-results": ("search", None)},
    (ca, "calendar"): {"year": ("year", "year"), "month": ("month", "month"), "list": ("list", "list"),
                       "views": ("views", None)},
    (nt, "notes"): {"folders": ("folders", "folders"), "search": ("search", "search"), "note": ("note", "note"),
                    "menu": ("menu", "menu")},
    (mp, "maps"): {"home": ("home", "home"), "place": ("place", "place")},
    (ph, "photos"): {"collections": ("collections", "collections"), "album": ("album", "album"),
                     "library": ("library", "library")},
    (sf, "safari"): {"google": ("page", "page"), "example": ("page", "page")},
}
SPRINGBOARD = snap("com.apple.springboard", [el("Settings", "Icon", (.1, .1, .2, .1))])


class ScreenTests(unittest.TestCase):
    def test_every_screen_is_recognized_on_its_recorded_tree_and_picture(self):
        for (module, app), names in RECOGNIZED.items():
            pack = module.PACK
            for name, (on_tree, on_picture) in names.items():
                seen = pack.recognize(tree(app, name, pack))
                self.assertEqual(seen.screen, on_tree, f"{app}/{name}: {seen}")
                if on_picture:
                    seen = pack.recognize(picture(app, name, pack))
                    self.assertEqual(seen.screen, on_picture, f"{app}/{name} (picture): {seen}")

    def test_a_pack_names_no_screen_of_another_app(self):
        other = tree("calendar", "month", ca.PACK)
        for pack in (st.PACK, nt.PACK, mp.PACK, ph.PACK, sf.PACK):
            self.assertIsNone(pack.recognize(other).screen, pack.name)

    def test_each_pack_says_what_it_was_checked_on(self):
        for pack in (st.PACK, ca.PACK, nt.PACK, mp.PACK, ph.PACK, sf.PACK):
            self.assertEqual(pack.verified.version, "iOS 26.0.1")
            self.assertTrue(any(s.evidence == "recorded" for s in pack.screens))


class SettingsTests(unittest.TestCase):
    def test_about_on_a_tree_and_a_picture(self):
        for snapshot in (tree("settings", "about", st.PACK), picture("settings", "about", st.PACK)):
            rows = {r["setting"]: r["value"] for r in st.page_rows(snapshot)}
            self.assertEqual(rows["iOS Version"], "26.0.1")
            self.assertEqual(rows["Videos"], "182")
            self.assertEqual(rows["Photos"], "2,543")
            self.assertFalse(any(st.PRIVATE.match(k) for k in rows))

    def test_storage_says_used_capacity_and_free(self):
        for snapshot in (tree("settings", "storage", st.PACK), picture("settings", "storage", st.PACK)):
            device = Phone({"storage": snapshot}, {}, "storage")
            result = st.PACK.read(core.PackIO(device, st.PACK, sleep=lambda s: None), "storage", 5)
            values = {r["item"]: r["gb"] for r in result.rows}
            self.assertEqual((values["Used"], values["Capacity"], values["Free"]), (125.51, 128.0, 2.49))

    def test_battery_and_a_pages_switches(self):
        battery = st.read_battery(st.PACK, None, tree("settings", "battery", st.PACK), 10, None)
        values = {r["setting"]: r["value"] for r in battery.rows}
        self.assertEqual(values["Level"], "81%")
        self.assertEqual(values["Battery Health"], "Service")
        self.assertEqual(values["Battery Percentage"], "on")
        power = {r["setting"]: r["value"] for r in st.page_rows(tree("settings", "power-mode", st.PACK))}
        self.assertEqual(power["Low Power Mode"], "off")

    def test_a_page_is_reached_through_search_with_fixed_words(self):
        root, results, about = (tree("settings", n, st.PACK) for n in ("root", "search-results", "about"))
        moves = {("*", "LAUNCH_APP", st.BUNDLE): "root", ("root", "TYPE", "Search"): "results",
                 ("results", "TAP", "About, General"): "about"}
        results = snap(st.BUNDLE, [el("Search", "SearchField", (.08, .92, .83, .05), value="About", editable=True),
                                   el("Clear text", rect=(.71, .92, .06, .03)),
                                   el("About, General", rect=(.04, .14, .92, .07)),
                                   el("Software Update, General → About", rect=(.04, .21, .92, .07))])
        device = Phone({"springboard": SPRINGBOARD, "root": root, "results": results, "about": about}, moves,
                       "springboard")
        landing = core.navigate(st.PACK, core.PackIO(device, st.PACK, sleep=lambda s: None), "about")
        self.assertTrue(landing.ok, landing.why)
        self.assertIn(("TYPE", "Search", "About"), device.actions)
        self.assertEqual(device.taps, ["About, General"])

    def test_settings_never_flips_a_switch_or_erases(self):
        io = core.PackIO(Phone({"root": tree("settings", "root", st.PACK)}, {}, "root"), st.PACK)
        for label in ("Erase All Content and Settings", "Reset", "Sign Out", "Turn Off", "Update Now", "Delete App",
                      "Offload App", "Forget This Network"):
            self.assertFalse(io.may_tap(label), label)


class CalendarTests(unittest.TestCase):
    def test_the_list_on_a_tree_and_dated_on_a_picture(self):
        rows = ca.parse_list(tree("calendar", "list", ca.PACK))
        self.assertEqual(rows[0], {"title": "MobsterBench Review", "date": None, "time": "2:00PM – 3:00PM",
                                   "place": "Room 42", "calendar": "Sam"})
        self.assertEqual(rows[1]["time"], "all-day")
        dated = ca.parse_list(picture("calendar", "list", ca.PACK))
        self.assertEqual((dated[0]["title"], dated[0]["date"], dated[0]["time"], dated[0]["place"]),
                         ("MobsterBench Review", "Thursday, Oct 15", "2:00PM – 3:00PM", "Room 42"))
        self.assertEqual([r["title"] for r in dated[1:3]], ["Music Festival", "Music Festival"])

    def test_an_events_page(self):
        event = snap(ca.BUNDLE,
                     [el("EKEventView", "NavigationBar", (0, .07, 1, .06)), el("Oct 15", rect=(.04, .07, .23, .05)),
                      el("Edit", rect=(.82, .08, .13, .04)),
                      el("MobsterBench Review", "Cell", (.04, .13, .92, .13)),
                      el("", "TextView", (.08, .18, .16, .04), value="Room 42"),
                      el("Thursday, Oct 15, 2026", "StaticText", (.08, .22, .43, .02)),
                      el("2 PM – 3 PM", "StaticText", (.08, .24, .21, .02)),
                      el("Calendar", "Cell", (.04, .5, .92, .06), value="Work"),
                      el("Alert", "Cell", (.04, .58, .92, .06), value="30 minutes before"),
                      el("Delete Event", rect=(.35, .92, .32, .05))])
        self.assertEqual(ca.PACK.recognize(event).screen, "event")
        row = ca.read_event(ca.PACK, None, event, 1, None).rows[0]
        self.assertEqual((row["title"], row["date"], row["time"], row["place"], row["calendar"], row["alert"]),
                         ("MobsterBench Review", "Thursday, Oct 15, 2026", "2 PM – 3 PM", "Room 42", "Work",
                          "30 minutes before"))
        self.assertFalse(core.PackIO(Phone({"e": event}, {}, "e"), ca.PACK).may_tap("Delete Event"))


class NotesTests(unittest.TestCase):
    def test_a_note_found_by_search_and_read(self):
        found = nt.parse_list(tree("notes", "search", nt.PACK))
        self.assertEqual((found[0]["title"], found[0]["when"]), ("MobsterBench Note", "September 24, 2026"))
        self.assertTrue(found[0]["preview"].startswith("Locker code: 4817"))
        on_picture = nt.parse_list(picture("notes", "search", nt.PACK))
        self.assertEqual((on_picture[0]["title"], on_picture[0]["when"]), ("MobsterBench Note", "9/24/26"))
        self.assertEqual(nt.note_text(tree("notes", "note", nt.PACK))[:2],
                         ["Mobster Bench Scratchpad", "Notes for the real-customer benchmark."])

    def test_folders_with_counts(self):
        rows = nt.read_folders(nt.PACK, None, tree("notes", "folders", nt.PACK), 20, None).rows
        self.assertIn({"folder": "All iCloud", "notes": 155}, rows)

    def test_opening_a_note_checks_its_title(self):
        search, note = tree("notes", "search", nt.PACK), tree("notes", "note", nt.PACK)
        folders = tree("notes", "folders", nt.PACK)
        label = next(e.label for e in search.elements if e.label.startswith("MobsterBench Note,"))
        moves = {("folders", "TYPE", None): "search", ("search", "TAP", label): "note"}
        device = Phone({"folders": folders, "search": search, "note": note}, moves, "folders")
        landing = core.navigate(nt.PACK, core.PackIO(device, nt.PACK, sleep=lambda s: None), "note",
                                "MobsterBench Note")
        self.assertFalse(landing.ok)
        self.assertIn("Mobster Bench Scratchpad", landing.why)


class MapsPhotosSafariTests(unittest.TestCase):
    def test_a_places_card(self):
        found = mp.card(tree("maps", "place", mp.PACK))
        self.assertEqual((found["Name"], found["Kind"], found["Drive time"]),
                         ("Apple Park", "Corporate Headquarters", "45 min"))
        self.assertEqual(mp.card(picture("maps", "place", mp.PACK))["Name"], "Apple Park")
        lower = snap(mp.BUNDLE, [el("Card controller", rect=(.38, .1, .24, .03)),
                                 el("Address, Apple Inc. 1 Apple Park Way Cupertino, CA 95014 United States",
                                    rect=(.05, .5, .9, .08)), el("Phone, +1 (408) 996-1010", rect=(.05, .6, .9, .06))])
        self.assertEqual(mp.card(lower)["Address"], "Apple Inc. 1 Apple Park Way Cupertino, CA 95014 United States")

    def test_a_search_opens_by_maps_link(self):
        device = Phone({"home": tree("maps", "home", mp.PACK), "place": tree("maps", "place", mp.PACK)}, {}, "home",
                       links={"maps://?q=": "place"})
        landing = core.navigate(mp.PACK, core.PackIO(device, mp.PACK, sleep=lambda s: None), "place", "Apple Park")
        self.assertTrue(landing.ok and landing.how == "link", landing.why)
        self.assertEqual(device.calls[0][2]["url"], "maps://?q=Apple%20Park")

    def test_albums_and_counts(self):
        self.assertEqual(ph.parse_tiles(tree("photos", "collections", ph.PACK))[:2],
                         [{"album": "MobsterBench Receipts", "items": 0}, {"album": "MobsterBench Dogs", "items": 0}])
        for snapshot in (tree("photos", "album", ph.PACK), picture("photos", "album", ph.PACK)):
            self.assertEqual(ph.read_count(ph.PACK, None, snapshot, 1, None).rows,
                             [{"album": "MobsterBench Dogs", "items": 0}])
        self.assertEqual(ph.count_on(picture("photos", "library", ph.PACK)), 1878)

    def test_a_web_page_and_its_address(self):
        page = tree("safari", "example", sf.PACK)
        self.assertEqual(sf.address(page), "example.com")
        self.assertTrue(sf.page_lines(page)[0]["line"].startswith("This domain is for use in"))
        self.assertEqual(sf.address(picture("safari", "example", sf.PACK)), "example.com")
        google = [r["line"] for r in sf.page_lines(picture("safari", "google", sf.PACK))]
        self.assertIn("The Golden Gate Bridge opened in 1937.", google)

    def test_safari_opens_any_https_page_and_no_other_scheme(self):
        route = sf.PACK.routes["url"]
        self.assertEqual(core.link_for(sf.PACK, route, "https://example.com/a"), "https://example.com/a")
        self.assertIsNone(core.link_for(sf.PACK, route, "javascript:alert(1)"))
        self.assertIsNone(core.link_for(sf.PACK, route, "http://example.com"))


class ToolTests(unittest.TestCase):
    PACKS = (st.PACK, ca.PACK, nt.PACK, mp.PACK, ph.PACK, sf.PACK)

    def test_prompts_and_hints_fit(self):
        for pack in self.PACKS:
            for line in pack.prompts.values():
                self.assertLessEqual(len(line), 400, pack.name)
                self.assertTrue(line.startswith(f"- {pack.prefix}_"), pack.name)
            self.assertLessEqual(len(pack.mcp_open_hint), 220)
            self.assertLessEqual(len(pack.mcp_read_hint), 220)

    def test_the_words_a_task_uses_offer_the_right_pack(self):
        cases = {"What version of iOS is my phone on?": st.PACK, "How much free storage is left on my phone?": st.PACK,
                 "When and where is my MobsterBench Review?": ca.PACK,
                 "What's the locker code in my MobsterBench note?": nt.PACK,
                 "What's the address of Apple Park?": mp.PACK,
                 "How many photos are in my MobsterBench Dogs album?": ph.PACK,
                 "How tall is the Eiffel Tower?": sf.PACK}
        for goal, pack in cases.items():
            self.assertTrue(pack.mentions.search(goal), goal)

    def test_read_words(self):
        self.assertEqual(tools.parse_read(st.PACK, "page Low Power Mode"), ("page", 10, "Low Power Mode"))
        self.assertEqual(tools.parse_read(nt.PACK, "note MobsterBench Note"), ("note", 10, "MobsterBench Note"))
        self.assertEqual(tools.parse_read(ca.PACK, "events 5"), ("events", 5, None))

    def test_no_pack_taps_an_act(self):
        common = ("Delete", "Share", "Buy", "Send", "Sign In", "Edit", "Pay", "Order")
        own = {st.PACK: ("Erase All Content and Settings", "Turn Off"), ca.PACK: ("Delete Event", "Accept", "Add"),
               nt.PACK: ("Move Note", "Lock", "Pin Note"), mp.PACK: ("Directions", "Call", "Rate This Place"),
               ph.PACK: ("Add to Album", "Hide", "Play"), sf.PACK: ("Add to Cart", "Close Tab", "Accept All")}
        for pack in self.PACKS:
            io = core.PackIO(Phone({"s": SPRINGBOARD}, {}, "s"), pack)
            for label in common + own[pack]:
                self.assertFalse(io.may_tap(label), f"{pack.name}: {label}")

if __name__ == "__main__":
    unittest.main()
