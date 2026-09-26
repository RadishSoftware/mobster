"""Route compiler: hops parsed from the request, taken without a model call. Offline."""

import unittest

from mobile_agent.agent import Agent
from mobile_agent.drivers import Driver
from mobile_agent.models import Decision
from mobile_agent.routes import Route, compile_route, request_hops, request_targets, row_name
from mobile_agent.state import Element, Snapshot
from mobile_agent.task_policy import ActionSupport, OutputIntent, StopGate

ROOT_PAGES = [["Wi-Fi, Home", "Bluetooth, On", "Cellular", "Battery"],
              ["General", "Accessibility", "Camera", "Display & Brightness"],
              ["Privacy & Security", "Apps", "Passwords", "Game Center"]]
SCREENS = {"General": ["About", "Software Update", "Keyboard", "Date & Time"],
           "About": ["Name, iPhone", "iOS Version, 26.0.1", "Model Name, iPhone 15 Pro", "Capacity, 128 GB"]}


def snap(title, rows):
    elements = [Element("0", title, "NavigationBar", (0, .05, 1, .05))]
    for index, label in enumerate(rows, 1):
        elements.append(Element(str(index), label, "Button", (0, .1 + index * .1, 1, .08)))
    return Snapshot(elements, "\n".join([title, *rows]), 393, 852, "synthetic_fixture",
                    bundle_id="com.apple.Preferences")


class SettingsPhone(Driver):
    """Settings with its rows three screens long; only the visible page is published."""

    can_type = False

    def __init__(self):
        self.page, self.path, self.actions = 0, [], []

    def observe(self, timeout=10):
        if self.path:
            return snap(self.path[-1], SCREENS[self.path[-1]])
        return snap("Settings", ROOT_PAGES[self.page])

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.actions.append((operation, target.label if target is not None else None))
        if operation == "SWIPE_UP" and not self.path:
            self.page = min(self.page + 1, len(ROOT_PAGES) - 1)
        elif operation == "TAP" and row_name(target.label) in {name.casefold() for name in SCREENS}:
            self.path.append(next(name for name in SCREENS if name.casefold() == row_name(target.label)))

    def close(self):
        pass


class CountingModel:
    """Jev stand-in: DONE once About is open; counts every decision it is asked for."""

    def __init__(self):
        self.decisions = 0

    def decide(self, snapshot, goal, history, **kwargs):
        self.decisions += 1
        done = snapshot.text.startswith("About")
        intent = OutputIntent.ACTION_ONLY if kwargs.get("classify_output") else None
        return Decision("DONE" if done else "WAIT", None, .95, .95 if done else .01, .01, "test", 0, {},
                        StopGate.CONTINUE, intent)

    def verify_action(self, *args, **kwargs):
        return ActionSupport.ALLOWED


class RequestHopsTests(unittest.TestCase):
    def test_breadcrumbs_and_then_chains(self):
        cases = {
            "In Settings: In Settings > General > About, report the Model Name.": ["General", "About"],
            "In Settings: In Settings, open General, then Keyboard. Do not change any setting.": ["General", "Keyboard"],
            "In Settings: In Settings, open Privacy & Security, then Location Services.":
                ["Privacy & Security", "Location Services"],
            "In Settings: In Settings, open Apps (near the bottom of the list).": ["Apps"],
            "In Files: In Files, open On My iPhone > MobsterBench > eyes8 and view eyes8-01.":
                ["On My iPhone", "MobsterBench", "eyes8"],
            "In Contacts: In Contacts, open the contact Bench Tester and report the company name.": ["Bench Tester"],
            "In Notes: In Notes, open the note titled 'MobsterBench Note' and report the code.": ["MobsterBench Note"],
            "In Clock: In Clock, switch to the Stopwatch tab.": ["Stopwatch"],
        }
        for goal, hops in cases.items():
            with self.subTest(goal=goal):
                self.assertEqual(request_hops(goal), hops)

    def test_requests_without_trustworthy_screen_names_compile_nothing(self):
        for goal in ("In Safari: In Safari, go to en.m.wikipedia.org/wiki/Grace_Hopper and report the year.",
                     "In Safari: Search the web for Hedy Lamarr, open her Wikipedia article, and report the year.",
                     "In Settings: In Settings, is Bluetooth on or off?",
                     "In Notes: Open the note 'X' in Notes, open the web link written in it in Safari."):
            with self.subTest(goal=goal):
                self.assertEqual(request_hops(goal), [])


class RouteTests(unittest.TestCase):
    def test_a_hidden_hop_is_searched_for_then_tapped_and_the_title_confirms_it(self):
        route = Route(["General", "About"])
        self.assertEqual(route.step(snap("Settings", ROOT_PAGES[0]))[0], "SWIPE_UP")
        route.swiped(True)
        operation, target = route.step(snap("Settings", ROOT_PAGES[1]))
        self.assertEqual((operation, target.label), ("TAP", "General"))
        route.tapped(True)
        operation, target = route.step(snap("General", SCREENS["General"]))
        self.assertEqual((operation, target.label), ("TAP", "About"))
        route.tapped(True)
        self.assertIsNone(route.step(snap("About", SCREENS["About"])))
        self.assertIsNone(route.failed)

    def test_a_hop_that_never_appears_hands_over_to_the_model(self):
        route = Route(["Nowhere"])
        for _ in range(5):
            self.assertEqual(route.step(snap("Settings", ROOT_PAGES[2]))[0], "SWIPE_UP")
        self.assertIsNone(route.step(snap("Settings", ROOT_PAGES[2])))
        self.assertEqual(route.failed, "not_found")

    def test_the_end_of_a_list_stops_the_scroll_search(self):
        route = Route(["Nowhere"])
        route.step(snap("Settings", ROOT_PAGES[2]))
        route.swiped(False)
        self.assertIsNone(route.step(snap("Settings", ROOT_PAGES[2])))
        self.assertEqual(route.failed, "not_found")

    def test_the_end_of_a_list_then_uses_the_apps_search(self):
        route = Route(["MobsterBench Note"])
        folders = snap("Notes", ["Quick Notes", "Shared", "iCloud", "Notes"])
        folders.elements.append(Element("9", "Search", "SearchField", (.05, .9, .9, .05), True,
                                        actions=("TAP", "TYPE", "TYPE_SUBMIT")))
        self.assertEqual(route.step(folders)[0], "SWIPE_UP")
        route.swiped(False)
        operation, field, text = route.step(folders)
        self.assertEqual((operation, text), ("TYPE_SUBMIT", "MobsterBench Note"))

    def test_a_search_refused_before_dispatch_is_offered_once_more(self):
        route = Route(["MobsterBench Note"])
        folders = snap("Notes", ["Quick Notes", "Shared", "iCloud", "Notes"])
        folders.elements.append(Element("9", "Search", "SearchField", (.05, .9, .9, .05), True,
                                        actions=("TAP", "TYPE", "TYPE_SUBMIT")))
        route.step(folders)
        route.swiped(False)
        self.assertEqual(route.step(folders)[0], "TYPE_SUBMIT")
        route.not_sent()  # a stale screen refused it: nothing was typed
        self.assertEqual(route.step(folders)[0], "TYPE_SUBMIT")
        route.not_sent()  # only once
        self.assertIsNone(route.step(folders))
        self.assertEqual(route.failed, "not_found")

    def test_a_contact_named_as_someones_contact_is_a_hop(self):
        self.assertEqual(request_hops("Find Bench Tester's contact and get the city from their address."),
                         ["Bench Tester"])
        self.assertEqual(request_hops("find her contact and report the city"), [])

    def test_a_switch_is_never_a_hop(self):
        screen = Snapshot([Element("0", "Settings", "NavigationBar", (0, .05, 1, .05)),
                           Element("1", "General", "Switch", (0, .2, 1, .08), value="0")],
                          "Settings", 393, 852, "synthetic_fixture")
        self.assertIsNone(Route(["General"]).step(screen))


class RevealTests(unittest.TestCase):
    def test_the_asked_about_row_is_extracted(self):
        cases = {
            "In Settings: In Settings > General > Keyboard, is Auto-Correction on or off? Answer 'on' or 'off'.":
                ["Auto-Correction"],
            "In Settings: In Settings > General > Date & Time, is 'Set Automatically' on or off?": ["Set Automatically"],
            "In Settings: In Settings > General > About, report the total storage Capacity (not Available).":
                ["Capacity"],
            "In Settings: In Settings, open General, then Keyboard. Do not change any setting.": [],
        }
        for goal, targets in cases.items():
            with self.subTest(goal=goal):
                self.assertEqual(request_targets(goal, request_hops(goal)), targets)

    def test_after_the_hops_the_route_scrolls_until_the_row_shows(self):
        route = compile_route("In Settings: In Settings > General, is Date & Time shown?")
        self.assertEqual(route.hops, ["General"])
        route.next = 1  # arrived
        hidden = snap("General", ["About", "Software Update", "Keyboard", "Fonts"])
        self.assertEqual(route.step(hidden)[0], "SWIPE_UP")
        route.swiped(True)
        self.assertIsNone(route.step(snap("General", SCREENS["General"])))
        self.assertTrue(route.revealed)
        self.assertIsNone(route.failed)

    def test_a_statement_without_screens_compiles_nothing(self):
        self.assertIsNone(compile_route("In Contacts: In Contacts, report the first contact's number."))

    def test_an_app_search_types_the_requests_own_text(self):
        route = compile_route("In Notes: In Notes, use search to find the note containing 'Locker code' and "
                              "report the code.")
        self.assertEqual(route.search, "Locker code")
        screen = Snapshot([Element("0", "Folders", "NavigationBar", (0, .05, 1, .05)),
                           Element("1", "Search", "SearchField", (.05, .9, .9, .05), True,
                                   actions=("TAP", "TYPE", "TYPE_SUBMIT"))],
                          "Folders", 393, 852, "synthetic_fixture")
        operation, field, text = route.step(screen)
        self.assertEqual((operation, field.id, text), ("TYPE_SUBMIT", "1", "Locker code"))
        self.assertNotEqual((route.step(screen) or ("",))[0], "TYPE_SUBMIT")  # searched once
        self.assertIsNone(compile_route("In Safari: Search the web for Ada Lovelace, open her article."))

    def test_a_selected_tab_is_a_hop_already_taken_and_a_dead_tap_hands_over(self):
        tab = Snapshot([Element("0", "Stopwatch", "Button", (.6, .9, .2, .05), value="1")],
                       "Stopwatch", 393, 852, "synthetic_fixture")
        route = Route(["Stopwatch"])
        self.assertIsNone(route.step(tab))
        route = Route(["General"])
        route.step(snap("Settings", ROOT_PAGES[1]))
        route.tapped(False)
        self.assertEqual(route.failed, "tap_no_effect")


class AgentRouteTests(unittest.TestCase):
    def test_named_hops_cost_no_model_decision(self):
        phone, model, events = SettingsPhone(), CountingModel(), []
        result = Agent(phone, model, emit=events.append, settle_seconds=0).run(
            "In Settings: In Settings > General > About, show the About screen.", execute=True)
        self.assertEqual(result["status"], "completed_unverified")
        self.assertEqual(phone.actions, [("SWIPE_UP", None), ("TAP", "General"), ("TAP", "About")])
        # The auto-output classification asks Jev once on the first screen; the rest is the route.
        self.assertLessEqual(model.decisions, 3)
        self.assertEqual(sum(e["event"] == "route_decision" for e in events), 3)


if __name__ == "__main__":
    unittest.main()
