import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent import engines, harness_api, links, result_blocks
from mobile_agent.drivers import Driver
from mobile_agent.latency_trace import Trace
from mobile_agent.plan_gate import cli_needs_plan
from mobile_agent.runtime_driver import RunSetup
from mobile_agent.state import Element, Snapshot
from mobile_agent.tests.test_server_engine import APP as SETTINGS_APP
from mobile_agent.tests.test_server_engine import Base
from mobile_agent.xray import app_cli, crawl, diff, diff_cli, job, paywall, policy, report, screens

ROW_H = .065


# Each screen: a title, rows (label, where it leads or None, kind), and extra controls. Kinds: "row" (a cell),
# "switch" (a cell holding a switch), "button" (a plain button), "option" (a row of a choice list), "text".
APP = {
    "home": {"title": "Settings", "rows": [("General", "general"), ("Privacy", "privacy"), ("Premium", "paywall"),
                                           ("Delete Account", "deleted"), ("Sign Out", "signedout"),
                                           ("Airplane Mode", None, "switch")]},
    "general": {"title": "General", "rows": [("About", "about"), ("Auto-Lock", "autolock"), ("Storage", "storage"),
                                             ("Reset", "reset")]},
    "about": {"title": "About", "rows": [("Name", None, "text"), ("Version 26.4", None, "text")]},
    "autolock": {"title": "Auto-Lock", "rows": [("30 Seconds", None, "option", True), ("1 Minute", None, "option"),
                                                ("Never", None, "option")]},
    "storage": {"title": "Storage", "rows": [("Photos 12.3 GB", None, "text"), ("Free up space", None, "button")]},
    "privacy": {"title": "Privacy", "rows": [("Location Services", "location"), ("Tracking", "tracking")]},
    "location": {"title": "Location Services", "rows": [("Location Services", None, "switch")]},
    "tracking": {"title": "Tracking", "rows": [("Allow Apps to Request to Track", None, "switch")]},
    "paywall": {"title": "Premium", "rows": [
        ("Unlock everything with Premium", None, "text"), ("Annual", None, "text"), ("$59.99/year", None, "text"),
        ("7-day free trial, then $59.99/year", None, "text"), ("Monthly", None, "text"),
        ("$9.99 per month", None, "text"), ("Start Free Trial", "bought", "button"),
        ("Restore Purchases", "restored", "button"), ("Plan details", "plandetails")]},
    "plandetails": {"title": "Plan details", "rows": []},
    "deleted": {"title": "Account deleted", "rows": []},
    "signedout": {"title": "Signed out", "rows": []},
    "reset": {"title": "Everything erased", "rows": []},
    "bought": {"title": "Purchased", "rows": []},
    "restored": {"title": "Restored", "rows": []},
}
FORBIDDEN = {"deleted", "signedout", "reset", "bought", "restored"}


def rows_of(screen, app=None):
    for index, row in enumerate((app or APP)[screen]["rows"]):
        label, target = row[0], row[1]
        kind = row[2] if len(row) > 2 else "row"
        selected = bool(row[3]) if len(row) > 3 else False
        yield index, label, target, kind, selected


class FakeApp(Driver):

    capabilities = frozenset({"relaunch_app", "edge_back"})

    def __init__(self, shape="tree", app=None, alert=None, edge=True, relaunch=True):
        self.shape, self.app = shape, app or APP
        self.stack = ["home"]
        self.taps, self.visited, self.swipes = [], ["home"], 0
        self.alert = alert            # (title, [buttons]) shown after the first tap
        self.edge = edge
        self.can_relaunch = relaunch
        self.relaunches = 0
        self.toggled = set()
        if not edge:
            self.edge_back = None

    # Driver
    def observe(self, timeout=10):
        return self._tree() if self.shape == "tree" else self._pixels()

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        if operation == "TAP":
            self.taps.append(target.label)
            if self.alert and target.locator.startswith("alert/"):
                self.alert = None
                return
            screen = self.stack[-1]
            if target.label in ("Back", "<"):
                self.stack.pop()
                return
            for index, label, dest, kind, _ in rows_of(screen, self.app):
                if label == target.label.split(", ")[0]:
                    if kind == "switch":
                        self.toggled.add(label)
                    if dest:
                        self.stack.append(dest)
                        self.visited.append(dest)
                        if self.alert == "next":
                            self.alert = ("Allow “Fake” to use your location?", ["Allow", "Don’t Allow"])
                    return
            return
        if operation in ("SWIPE_UP", "SWIPE_DOWN"):
            self.swipes += 1
            return
        if operation == "LAUNCH_APP":
            return
        raise ValueError(operation)

    def close(self):
        pass

    # optional capabilities
    def edge_back(self, snapshot):
        if len(self.stack) > 1:
            self.stack.pop()

    def relaunch_app(self, bundle, timeout=40):
        if not self.can_relaunch:
            return False
        self.relaunches += 1
        self.stack = ["home"]
        return True

    # screens
    def _tree(self):
        screen = self.stack[-1]
        els = [Element("nav", self.app[screen]["title"], "NavigationBar", (0, .06, 1, .06),
                       locator="win/XCUIElementTypeNavigationBar[1]", actions=())]
        if len(self.stack) > 1:
            els.append(Element("back", "Back", "Button", (.02, .065, .15, .05),
                               locator="win/XCUIElementTypeNavigationBar[1]/XCUIElementTypeButton[1]"))
        for index, label, dest, kind, selected in rows_of(screen, self.app):
            y = .15 + index * ROW_H
            cell = f"win/XCUIElementTypeTable[1]/XCUIElementTypeCell[{index + 1}]"
            if kind == "text":
                els.append(Element(f"r{index}", label, "StaticText", (.05, y, .9, ROW_H * .9), locator=cell + "/T"))
            elif kind == "button":
                els.append(Element(f"r{index}", label, "Button", (.2, y, .6, ROW_H * .9), locator=f"win/B{index}"))
            else:
                els.append(Element(f"r{index}", label, "Cell", (.05, y, .9, ROW_H * .9), locator=cell,
                                   selected=selected))
                if kind == "switch":
                    els.append(Element(f"s{index}", label, "Switch", (.8, y + .01, .12, .04), locator=cell + "/S",
                                       value="1" if label in self.toggled else "0"))
        if self.alert and isinstance(self.alert, tuple):
            title, buttons = self.alert
            els.append(Element("alert", title, "Alert", (.1, .35, .8, .3), locator="alert", actions=()))
            for i, name in enumerate(buttons):
                els.append(Element(f"a{i}", name, "Button", (.12 + i * .4, .55, .36, .06), locator=f"alert/{i}"))
        return Snapshot(els, "\n".join(e.label for e in els), 402, 874, "synthetic_fixture", bundle_id="com.fake.app")

    def _pixels(self):
        screen = self.stack[-1]
        els = [Element("t0", "9:41", "StaticText", (.08, .015, .1, .02), locator="ocr:0"),
               Element("t1", self.app[screen]["title"], "StaticText", (.06, .1, .4, .04), locator="ocr:1")]
        for index, label, dest, kind, selected in rows_of(screen, self.app):
            y = .17 + index * ROW_H
            if kind == "button":
                els.append(Element(f"t{index + 2}", label, "StaticText", (.3, y, .4, .025), locator=f"ocr:{index + 2}"))
            else:
                els.append(Element(f"t{index + 2}", label, "StaticText", (.08, y, .4, .025),
                                   locator=f"ocr:{index + 2}"))
        return Snapshot(els, "\n".join(e.label for e in els), 393, 852, "mirroring", bundle_id="com.fake.app")


class FakeClock:
    def __init__(self, step=.5):
        self.now, self.step = 1000.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def run(driver, **caps):
    clock = FakeClock()
    crawler = crawl.Crawler(driver, "com.fake.app", caps=crawl.Caps(**caps), clock=clock, sleep=lambda s: None)
    return crawler.run(), crawler


def by_title(result):
    return {s["title"]: s for s in result["screens"]}


class TreeCrawl(unittest.TestCase):
    def setUp(self):
        self.driver = FakeApp("tree")
        self.result, self.crawler = run(self.driver)
        self.screens = by_title(self.result)

    def test_maps_every_reachable_screen_and_how_they_connect(self):
        self.assertEqual(set(self.screens), {"Settings", "General", "About", "Auto-Lock", "Storage", "Privacy",
                                             "Location Services", "Tracking", "Premium"})
        edges = {(e["from"], e["label"], e["to"]) for e in self.result["edges"]}
        home, general = self.screens["Settings"]["id"], self.screens["General"]["id"]
        self.assertIn((home, "General", general), edges)
        self.assertIn((general, "About", self.screens["About"]["id"]), edges)
        self.assertEqual(self.screens["About"]["depth"], 2)
        self.assertEqual([step["label"] for step in self.screens["About"]["path"]], ["General", "About"])
        self.assertEqual(self.result["stats"]["model_usd"], 0.0)
        self.assertEqual(self.result["stopped"], "explored every screen it could reach")
        self.assertGreater(self.result["stats"]["backs"]["control"], 0)

    def test_never_taps_what_could_act(self):
        self.assertFalse(FORBIDDEN & set(self.driver.visited), self.driver.visited)
        for label in ("Delete Account", "Sign Out", "Reset", "Start Free Trial", "Restore Purchases",
                      "Airplane Mode", "Free up space", "1 Minute", "Never"):
            self.assertNotIn(label, self.driver.taps)
        self.assertEqual(self.driver.toggled, set())
        refused = {r["label"]: r["reason"] for s in self.result["screens"] for r in s["refused"]}
        self.assertIn("Delete Account", refused)
        self.assertIn("Sign Out", refused)
        self.assertEqual(refused["Airplane Mode"], "a switch")

    def test_reads_the_paywall_and_taps_nothing_on_it(self):
        premium = self.screens["Premium"]
        plans = {p["period"]: p for p in premium["paywall"]["plans"]}
        self.assertEqual(plans["year"]["price"], "$59.99")
        self.assertEqual(plans["year"]["trial"], "7 days")
        self.assertEqual(plans["month"]["amount"], 9.99)
        self.assertTrue(premium["paywall"]["restore"])
        self.assertNotIn("Plan details", self.driver.taps)  # a navigation row, but on a paywall
        self.assertEqual(self.result["stats"]["paywalls"], 1)

    def test_a_choice_list_is_seen_and_its_options_left_alone(self):
        autolock = self.screens["Auto-Lock"]
        self.assertTrue(autolock["choice_list"])
        reasons = {r["label"]: r["reason"] for r in autolock["refused"]}
        self.assertEqual(reasons["1 Minute"], "an option in a choice list")


class PixelCrawl(unittest.TestCase):

    def test_maps_list_rows_backs_out_by_edge_swipe_and_refuses_the_same_acts(self):
        driver = FakeApp("pixels")
        result, crawler = run(driver)
        screens_ = by_title(result)
        self.assertEqual(result["connection"], "pixels")
        self.assertTrue({"Settings", "General", "Privacy", "Premium", "About"} <= set(screens_), set(screens_))
        self.assertGreater(result["stats"]["backs"]["swipe"], 0)
        self.assertFalse(FORBIDDEN & set(driver.visited), driver.visited)
        for label in ("Delete Account", "Sign Out", "Reset", "Start Free Trial", "Restore Purchases"):
            self.assertNotIn(label, driver.taps)
        self.assertIsNotNone(screens_["Premium"]["paywall"])


class Recovery(unittest.TestCase):
    def test_relaunches_and_taps_the_path_again_when_there_is_no_way_back(self):
        driver = FakeApp("pixels", edge=False)
        result, _ = run(driver)
        self.assertGreater(driver.relaunches, 1)
        self.assertGreater(result["stats"]["backs"]["relaunch"], 0)
        self.assertIn("About", by_title(result))
        self.assertFalse(FORBIDDEN & set(driver.visited))

class Reopening(FakeApp):

    def relaunch_app(self, bundle, timeout=40):
        self.relaunches += 1
        return True


class ReopensDeep(unittest.TestCase):
    def test_goes_back_to_the_first_screen_when_the_app_reopens_where_it_was(self):
        driver = Reopening("tree")
        driver.stack = ["home", "privacy", "location"]   # it opens deep, before the crawl knows its first screen
        result, crawler = run(driver)
        self.assertEqual(result["screens"][0]["title"], "Settings")
        self.assertEqual(set(by_title(result)), set(by_title(run(FakeApp("tree"))[0])))
        self.assertFalse([s for s in result["screens"] if s["unreachable"]])
        self.assertFalse(FORBIDDEN & set(driver.visited))

    def test_maps_from_where_it_opens_when_nothing_goes_back(self):
        driver = Reopening("pixels", edge=False)
        driver.stack = ["home", "general"]
        result, _ = run(driver)
        self.assertEqual(result["screens"][0]["title"], "General")   # nothing to go back with: it maps from there
        self.assertFalse(FORBIDDEN & set(driver.visited))


class RefusedAtTheTap(unittest.TestCase):
    def test_a_control_the_fresh_read_refuses_is_skipped_and_nothing_is_tapped(self):
        driver = FakeApp("tree")
        crawler = crawl.Crawler(driver, "com.fake.app", clock=FakeClock(), sleep=lambda s: None)
        snapshot = driver.observe()
        switch = next(e for e in snapshot.elements if e.role == "Switch")
        with self.assertRaises(crawl.Gone):
            crawler.tap(switch, snapshot)
        self.assertEqual(driver.taps, [])


class Caps(unittest.TestCase):
    def test_screen_cap(self):
        result, _ = run(FakeApp("tree"), screens=3)
        self.assertEqual(len(result["screens"]), 3)
        self.assertEqual(result["stopped"], "reached 3 screens")

    def test_time_cap(self):
        result, _ = run(FakeApp("tree"), seconds=4)
        self.assertIn("time limit", result["stopped"])
        self.assertLess(len(result["screens"]), 9)

class Settling(unittest.TestCase):

    def crawler(self, driver, ticks=.05):
        clock = FakeClock(step=ticks)
        slept = []
        crawler = crawl.Crawler(driver, "com.fake.app", clock=clock, sleep=slept.append)
        crawler.started = clock()
        return crawler, slept

    def test_gives_up_after_the_cap_when_the_screen_never_settles(self):
        driver = FakeApp("tree")
        frames = iter(range(10 ** 6))
        reads = []

        def moving(timeout=10):
            n = next(frames)
            reads.append(n)
            return pixels(el(f"m{n}", f"Frame {n}"))

        driver.observe = moving
        crawler, slept = self.crawler(driver, ticks=.05)
        crawler.settle(pixels(el("x", "Before")))
        self.assertLessEqual(crawler.spent["settle"], crawl.SETTLE_TIMEOUT + .5)
        self.assertLess(len(reads), 200)

    def test_a_screen_that_changes_late_is_not_recorded_half_drawn(self):
        driver = FakeApp("tree")
        old = pixels(el("a", "Old"))
        partial = pixels(el("p", "Half drawn"))
        full = pixels(el("f", "Done"), el("g", "Row"))
        script = iter([old, old, partial, full, full, full])
        driver.observe = lambda timeout=10: next(script)
        crawler, _ = self.crawler(driver, ticks=.05)
        got = crawler.settle(old)
        self.assertEqual(got.content_fingerprint, full.content_fingerprint)

class Dialogs(unittest.TestCase):
    def test_dismisses_with_not_now(self):
        driver = FakeApp("tree")
        driver.alert = ("Turn on notifications?", ["Allow", "Not Now"])
        result, crawler = run(driver)
        self.assertIn("Not Now", driver.taps)
        self.assertNotIn("Allow", driver.taps)
        self.assertEqual(result["stats"]["dialogs_dismissed"], 1)

    def test_stops_when_only_the_user_can_answer(self):
        driver = FakeApp("tree")
        driver.alert = "next"
        result, _ = run(driver)
        self.assertIn("a dialog needs you", result["stopped"])
        self.assertNotIn("Allow", driver.taps)
        self.assertNotIn("Don’t Allow", driver.taps)


class InPlace(unittest.TestCase):
    def test_a_tap_that_changes_the_screen_in_place_leaves_the_rest_alone(self):
        app = {**APP, "home": {"title": "Home", "rows": [("Units", "units")]},
               "units": {"title": "Units", "rows": [("Metric", None), ("Imperial", None), ("Other", None)]}}

        class Toggling(FakeApp):
            def execute(self, operation, target, snapshot, text=None, timeout=10):
                if operation == "TAP" and target.label == "Metric":
                    self.taps.append(target.label)
                    self.app["units"]["rows"][0] = ("Metric ✓", None)
                    return
                return super().execute(operation, target, snapshot, text, timeout)

        driver = Toggling("tree", app={k: {"title": v["title"], "rows": list(v["rows"])} for k, v in app.items()})
        result, _ = run(driver)
        units = by_title(result)["Units"]
        self.assertEqual(units["changed_in_place"], ["Metric"])
        self.assertNotIn("Imperial", driver.taps)


def tree(*elements):
    return Snapshot(list(elements), "", 402, 874, "wda")


def pixels(*elements):
    return Snapshot(list(elements), "", 393, 852, "mirroring")


def el(id_, label, role="Cell", rect=(.05, .3, .9, .06), **kw):
    kw.setdefault("locator", f"win/XCUIElementTypeTable[1]/XCUIElementTypeCell[{id_}]" if role == "Cell"
                  else f"win/{id_}")
    return Element(str(id_), label, role, rect, **kw)


class Policy(unittest.TestCase):
    def test_taps_a_row_button_with_a_chevron_and_a_tab(self):
        row = el(1, "Wallpaper", "Button", locator="win/B1")
        chevron = el(2, "chevron.forward", "Image", (.9, .32, .02, .02), locator="win/B1/I1")
        self.assertTrue(policy.decide(row, tree(row, chevron)).ok)
        bar = el(3, "Tab Bar", "TabBar", (0, .9, 1, .1), locator="win/XCUIElementTypeTabBar[1]")
        tab = el(4, "Library", "Button", (.4, .92, .2, .06), locator="win/XCUIElementTypeTabBar[1]/B")
        self.assertEqual(policy.decide(tab, tree(bar, tab)).reason, "tab")

    def test_refuses_delete_subscribe_and_sign_out_whatever_their_shape(self):
        for label in ("Delete Account", "Subscribe", "Start Free Trial", "Sign Out", "Erase All Content and Settings",
                      "Buy Now", "Share", "Call Mom", "Restore Purchases", "Download", "AUTH_PROX_SIGN_IN_CELL"):
            cell = el(1, label)
            self.assertFalse(policy.decide(cell, tree(cell)).ok, label)

    def test_refuses_a_switch_and_a_cell_holding_one(self):
        cell = el(1, "Airplane Mode")
        switch = el(2, "Airplane Mode", "Switch", (.8, .31, .12, .04), locator=cell.locator + "/S")
        snap = tree(cell, switch)
        self.assertEqual(policy.decide(switch, snap).reason, "a switch")
        self.assertEqual(policy.decide(cell, snap).reason, "holds a switch")

    def test_refuses_a_paywall_cta_and_a_plain_button(self):
        cta = el(1, "Continue", "Button", (.1, .8, .8, .06))
        self.assertFalse(policy.decide(cta, tree(cta)).ok)
        other = el(2, "Customize", "Button", (.1, .5, .8, .06))
        self.assertEqual(policy.decide(other, tree(other)).reason, "not navigation-shaped")

    def test_refuses_an_option_in_a_choice_list(self):
        picked = el(1, "30 Seconds", selected=True)
        other = el(2, "1 Minute", rect=(.05, .37, .9, .06))
        self.assertEqual(policy.decide(other, tree(picked, other)).reason, "an option in a choice list")
        check = el(3, "selected", "Image", (.1, .32, .03, .02))
        plain = el(4, "English", rect=(.05, .45, .9, .06))
        self.assertFalse(policy.decide(plain, tree(check, plain)).ok)

    def test_refuses_fields_and_rows_under_the_toolbar(self):
        field = el(1, "Search", "SearchField", editable=True, actions=("TAP", "TYPE", "TYPE_SUBMIT"))
        self.assertFalse(policy.decide(field, tree(field)).ok)
        low = el(2, "Passcode", rect=(.05, .95, .9, .04))
        self.assertEqual(policy.decide(low, tree(low)).reason, "outside the content area")

    def test_pixels_taps_a_list_row_and_refuses_buttons_sentences_and_headings(self):
        rows = [el(i, name, "StaticText", (.08, .2 + i * .06, .3, .025), locator=f"ocr:{i}")
                for i, name in enumerate(["Wi-Fi", "Bluetooth", "General"])]
        centred = el(9, "Continue", "StaticText", (.4, .8, .2, .03), locator="ocr:9")
        sentence = el(10, "Manage your account settings here.", "StaticText", (.08, .5, .8, .03), locator="ocr:10")
        heading = el(11, "PRIVACY", "StaticText", (.08, .6, .2, .02), locator="ocr:11")
        lonely = el(12, "Customize", "StaticText", (.6, .7, .3, .03), locator="ocr:12")
        snap = pixels(*rows, centred, sentence, heading, lonely)
        self.assertEqual(policy.decide(rows[1], snap), policy.Verdict(True, "list row"))
        for refused in (centred, sentence, heading, lonely):
            self.assertFalse(policy.decide(refused, snap).ok, refused.label)

    def test_a_word_that_is_also_a_noun_counts_only_as_the_first_word(self):
        # Weather's "Feels like" and "Clear", Settings' "Software Update" and "Privacy Report" are what the app shows;
        # the same words first in a label are a command.
        for label in ("Feels like 67°", "10PM, Clear, 65°", "Software Update", "Privacy Report",
                      "Monday, Mostly Cloudy, high of 64, low of 54", "General"):
            with self.subTest(taps=label):
                self.assertEqual(policy.act_words(label), "")
        for label in ("Like", "Clear History", "Update Now", "Report a Problem", "Add to Cart", "Start Free Trial",
                      "Tap to continue", "Get Premium", "Services Included with Purchase", "Send a like", "Delete"):
            with self.subTest(refuses=label):
                self.assertNotEqual(policy.act_words(label), "")

    def test_taps_a_card_and_a_row_of_data_but_not_a_call_to_action(self):
        card = el(1, "Air Quality", "Button", (.05, .4, .43, .18), locator="win/B1")
        day = el(2, "Monday, Mostly Cloudy, high of 64, low of 54", "Button", (.05, .3, .9, .05), locator="win/B2")
        self.assertEqual(policy.decide(card, tree(card)), policy.Verdict(True, "card"))
        self.assertEqual(policy.decide(day, tree(day)), policy.Verdict(True, "card"))
        refused = {"a short button": el(3, "Learn about plans", "Button", (.2, .5, .6, .05), locator="win/B3"),
                   "an icon": el(4, "heart", "Button", (.05, .4, .43, .18), locator="win/B4"),
                   "a tall button that acts": el(5, "Subscribe", "Button", (.05, .4, .9, .2), locator="win/B5"),
                   "a tall button that empties": el(6, "Free up space", "Button", (.05, .4, .9, .2), locator="win/B6"),
                   "an id, not words": el(7, "KxLxpHjhdScjmXV", "Button", (.05, .4, .9, .2), locator="win/B7")}
        for why, button in refused.items():
            with self.subTest(why):
                self.assertFalse(policy.decide(button, tree(button)).ok)

    def test_never_taps_a_row_with_its_own_info_button(self):
        # Settings › Wi-Fi and Bluetooth: the row joins or connects, its (i) opens the details.
        network = el(1, "Cafe Guest", "Cell")
        info = el(2, "More Info", "Button", (.85, .31, .08, .04), locator=network.locator + "/B1")
        self.assertFalse(policy.decide(network, tree(network, info)).ok)
        self.assertFalse(policy.has_chevron(network, tree(network, info)))
        row = el(3, "Wi-Fi", "Cell")
        chevron = el(4, "chevron.forward", "Image", (.9, .32, .02, .02), locator=row.locator + "/I1")
        self.assertTrue(policy.decide(row, tree(row, chevron)).ok)

class Paywalls(unittest.TestCase):
    def test_plans_periods_and_trial(self):
        found = paywall.detect(["Go Pro", "Weekly", "$4.99/week", "Yearly", "$39.99/year",
                                "3-day free trial", "Restore Purchases", "Terms", "Privacy"])
        plans = {p.period: p for p in found.plans}
        self.assertEqual(set(plans), {"week", "year"})
        self.assertEqual(plans["year"].per_month, 3.33)
        self.assertEqual(found.trial, "3 days")
        self.assertTrue(found.restore)

class Identity(unittest.TestCase):
    def test_same_screen_read_twice_and_scrolled(self):
        a = {"general", "about", "storage", "fonts", "keyboard"}
        self.assertTrue(screens.same("General", a, "General", a | {"dictionary"}))
        self.assertTrue(screens.same("General", {"keyboard", "fonts", "language"}, "General", a,
                                     b_union=a | {"language"}))
        self.assertFalse(screens.same("General", a, "About", a))
        self.assertFalse(screens.same("General", {"x", "y", "z", "w"}, "General", a))

def screen(id_, title, path, tokens, parent="", paywall_=None):
    return {"id": id_, "title": title, "name": title, "path": [{"label": p, "key": p.casefold()} for p in path],
            "tokens": sorted(tokens), "parent": parent, "paywall": paywall_}


class Diff(unittest.TestCase):
    def test_price_change_new_paywall_and_new_screens(self):
        pw = lambda price: {"plans": [{"price": price, "amount": float(price[1:]), "currency": "USD",  # noqa: E731
                                       "period": "year", "name": "Annual"}], "trial": ""}
        before = {"screens": [screen("s1", "Home", [], {"home", "settings"}),
                              screen("s2", "Settings", ["Settings"], {"settings", "units"}, "s1"),
                              screen("s3", "Pro", ["Pro"], {"pro"}, "s1", pw("$59.99")),
                              screen("s4", "Old", ["Old"], {"old"}, "s1")]}
        after = {"screens": [screen("s1", "Home", [], {"home", "settings"}),
                             screen("s2", "Settings", ["Settings"], {"settings", "units", "theme"}, "s1"),
                             screen("s3", "Pro", ["Pro"], {"pro"}, "s1", pw("$69.99")),
                             screen("s5", "Theme", ["Settings", "Theme"], {"theme"}, "s2"),
                             screen("s6", "Icons", ["Settings", "Icons"], {"icons"}, "s2"),
                             screen("s7", "Upgrade", ["Settings", "Upgrade"], {"upgrade"}, "s2", pw("$4.99"))]}
        result = diff.compare(before, after)
        text = "\n".join(diff.summary(result))
        self.assertIn("Yearly price went $59.99 → $69.99 on Pro", text)
        self.assertIn("New paywall screen: Upgrade (Settings › Upgrade), $4.99", text)
        self.assertIn("3 new screens under Settings", text)
        self.assertIn("Gone: Old", text)
        self.assertIn("Settings changed: now shows “theme”", text)

class Report(unittest.TestCase):
    def test_renders_a_self_contained_escaped_page_with_wireframes(self):
        driver = FakeApp("tree")
        driver.app = {**APP, "about": {"title": "<script>alert(1)</script>", "rows": []}}
        result, _ = run(driver)
        page = report.render(result, ".")
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>alert", page)
        self.assertIn("Content-Security-Policy", page)
        self.assertIn('class="wf-row"', page)          # no frames: drawn from the layout
        self.assertIn("$59.99", page)
        self.assertNotIn("http://", page.replace("http://www.w3.org", ""))
        self.assertIn(f'<footer class="foot"><span><a href="{links.MADE_WITH_XRAY}">Mobster</a> App X-ray', page)


FAST = (patch.object(crawl, "SETTLE_SECONDS", 0), patch.object(crawl, "POLL_SECONDS", 0))


class PreviewApp(FakeApp):

    capabilities = FakeApp.capabilities | {"capture_preview"}

    def __init__(self, log=None, fail_on=None, **kwargs):
        super().__init__(**kwargs)
        self.log, self.fail_on, self.sent = log if log is not None else [], fail_on, 0

    def capture_preview(self, timeout=3):
        from PIL import Image
        out = io.BytesIO()
        Image.new("RGB", (100, 200), (30, 90, 160)).save(out, "PNG")
        return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.sent += 1
        self.log.append(("driver", operation, getattr(target, "label", None)))
        if self.fail_on == self.sent:
            raise RuntimeError("the connection dropped")
        return super().execute(operation, target, snapshot, text, timeout)


def recorder(log):
    count = [0]

    def emit(event):
        if "_frame" in event:
            event.pop("_frame")
            count[0] += 1
            event["frameId"] = f"f{count[0]}"
        log.append(("event", event["event"], dict(event)))
    return emit


def context(log, bundle="com.fake.app", extras=None, cancelled=lambda: False):
    return harness_api.RunContext(
        run_id="r1", goal="Map it", engine="smart", origin="app", app_bundle=bundle, device_id=None, device_kind=None,
        extras={"xray": job.clean({})} if extras is None else extras, data_dir=None, emit=recorder(log),
        clarify=lambda request: "", cancelled=cancelled, runtime=None)


class FastCrawl(unittest.TestCase):
    def setUp(self):
        for patcher in FAST:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.data = tempfile.TemporaryDirectory()
        self.addCleanup(self.data.cleanup)
        place = patch("mobile_agent.xray.job.user_data_dir", return_value=Path(self.data.name))
        place.start()
        self.addCleanup(place.stop)


class RunField(unittest.TestCase):
    def test_refuses_anything_else(self):
        for bad in ({"screens": 0}, {"screens": 121}, {"minutes": 0}, {"minutes": 31}, {"screens": 5.5},
                    {"screens": "9"}, {"screens": True}, {"depth": 3}, "yes", [], 7):
            with self.assertRaises(ValueError, msg=bad):
                job.clean(bad)


class Hook(FastCrawl):
    def test_no_field_no_driver_no_hook(self):
        log = []
        self.assertIsNone(job.hook(context(log, extras={}), PreviewApp(), None))
        self.assertIsNone(job.hook(context(log), None, None))
        self.assertEqual(log, [])

    def test_a_valid_result_with_a_gallery_a_table_and_the_stats(self):
        log, driver = [], PreviewApp()
        done = job.hook(context(log), driver, None).result
        self.assertEqual((done["status"], done["outcome"], done["costUsd"], done["model"]),
                         ("completed", "done", 0.0, None))
        self.assertRegex(done["answer"], r"^Mapped \d+ screens of .*, 2 taps deep, in .*, with no model\. 1 paywall\. "
                                         r"It left \d+ controls alone because they could act\.$")
        envelope, problems = result_blocks.validate(done["blocks"])
        self.assertEqual(problems, [])
        kinds = {block["id"]: block for block in envelope["blocks"]}
        self.assertEqual([block["type"] for block in envelope["blocks"]], ["gallery", "table", "markdown"])
        frames = {event[2]["frameId"] for event in log if event[1] == "step"}
        self.assertEqual({item["frameId"] for item in kinds["gallery"]["items"]}, frames)
        plans = kinds["plans"]
        self.assertEqual([column["label"]
                          for column in plans["columns"]], ["Screen", "Plan", "Price", "Period", "Trial"])
        self.assertEqual(plans["columns"][2]["type"], "money")
        self.assertEqual({row["cells"]["price"] for row in plans["rows"]}, {59.99, 9.99})
        self.assertIn("Saved on this Mac", kinds["stats"]["text"])

    def test_files_land_in_a_private_folder_under_the_bundle(self):
        done = job.hook(context([]), PreviewApp(), None).result
        folder = Path(done["xray"]["folder"])
        self.assertEqual(folder.parent, Path(self.data.name) / "xray" / "apps" / "com.fake.app")
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads((folder / "map.json").read_text())["schema"], "mobster.xray/1")
        self.assertTrue((folder / "report.html").read_text().startswith("<!doctype html>"))

    def test_every_tap_is_journaled_before_it_is_sent_and_acknowledged_after(self):
        log = []
        driver = PreviewApp(log)
        job.hook(context(log), driver, None)
        taps = [i for i, entry in enumerate(log) if entry[0] == "driver"]
        self.assertGreater(len(taps), 5)
        for i in taps:
            self.assertEqual(log[i - 1][1], "action_started", log[i - 3:i + 2])
            self.assertEqual(log[i - 1][2]["operation"], log[i][1])
            self.assertEqual(log[i + 1][1], "action_acknowledged")
        started = [e for e in log if e[1] == "action_started"]
        self.assertEqual(len(started), len(taps))

    def test_stop_ends_the_crawl_at_the_next_safe_point_with_the_partial_map(self):
        log, driver = [], PreviewApp()
        done = job.hook(context(log, cancelled=lambda: sum(e[1] == "action_acknowledged" for e in log) >= 3),
                        driver, None).result
        self.assertEqual((done["status"], done["outcome"]), ("stopped", "stopped"))
        self.assertEqual(done["reason"], "Stopped by you. This is the map so far.")
        full = job.hook(context([]), PreviewApp(), None).result
        self.assertLess(done["actions"], full["actions"])
        self.assertTrue(result_blocks.validate(done["blocks"])[0])
        self.assertTrue(Path(done["xray"]["map"]).is_file())

    def test_a_tap_whose_outcome_is_unknown_is_never_repeated(self):
        log = []
        driver = PreviewApp(log, fail_on=3)
        done = job.hook(context(log), driver, None).result
        self.assertEqual(driver.sent, 3)                      # the third send raised and was not sent again
        self.assertEqual((done["status"], done["outcome"], done["actions_may_have_run"]),
                         ("completion_not_confirmed", "check", True))
        last = [e for e in log if e[1] in ("action_started", "action_acknowledged")][-1]
        self.assertEqual(last[1], "action_started")           # journaled, never acknowledged
        self.assertIn("not repeated", done["reason"])

    def test_secrets_read_off_a_screen_are_masked_before_they_are_stored(self):
        app = {**APP,
               "about": {"title": "About", "rows": [("Key sk-ant-fake3-AAAAAAAAAAAAAAAAAAAAAAAAAAAA", None, "text")]}}
        done = job.hook(context([]), PreviewApp(app=app), None).result
        text = Path(done["xray"]["map"]).read_text()
        self.assertNotIn("sk-ant-api03-AAAA", text)


class Refused(FastCrawl):
    def test_apps_where_a_plain_tap_acts_are_not_mapped_and_nothing_is_tapped(self):
        for bundle, name in (("com.apple.mobilephone", "Phone"), ("com.apple.facetime", "FaceTime"),
                             ("com.apple.shortcuts", "Shortcuts"), ("com.apple.Home", "Home")):
            log, driver = [], PreviewApp()
            done = job.hook(context(log, bundle=bundle), driver, None).result
            self.assertEqual(done["status"], "error")
            self.assertTrue(done["reason"].startswith(f"App X-ray doesn't map {name}: a tap on a "), done["reason"])
            self.assertTrue(done["reason"].endswith("Pick another app."))
            self.assertEqual((driver.sent, log), (0, []), bundle)

    def test_settings_still_maps(self):
        self.assertIsNone(policy.app_refusal("com.apple.Preferences"))
        self.assertIsNone(policy.app_refusal("com.fake.app"))
        self.assertEqual(job.hook(context([]), PreviewApp(), None).result["status"], "completed")

    def test_the_crawler_itself_refuses_too(self):
        driver = PreviewApp()
        result = crawl.Crawler(driver, "com.apple.shortcuts").run()
        self.assertEqual(driver.sent, 0)
        self.assertIn("App X-ray doesn't map Shortcuts", result["stopped"])


class Route(Base):
    FAKE = {"id": "fake", "name": "Fake", "bundleId": "com.fake.app", "installed": True}
    PHONE = {"id": "phone", "name": "Phone", "bundleId": "com.apple.mobilephone", "installed": True}

    def setUp(self):
        super().setUp()
        key = patch.object(engines, "smart_key", return_value=None)
        key.start()
        self.addCleanup(key.stop)
        self.rt = self.runtime(apps=(SETTINGS_APP, self.FAKE, self.PHONE))

    def post(self, body, runtime=None):
        return self.request(runtime or self.rt, "POST", "/api/xray/app", body)

    def test_starts_a_task_with_the_field_and_needs_no_model_key(self):
        status, body, _ = self.post({"appId": "com.fake.app", "screens": 12})
        self.assertEqual(status, 201)
        run = self.rt.runs[body["runId"]]
        self.assertEqual(run.extras, {"xray": {"screens": 12, "minutes": 5}})
        self.assertEqual((run.engine, run.mode, run.app["bundleId"]), ("smart", "live", "com.fake.app"))
        # The same admission with no key is refused for an ordinary task.
        other = self.runtime(apps=(SETTINGS_APP, self.FAKE))
        status, body, _ = self.request(other, "POST", "/api/runs", {"appId": "fake", "goal": "Open it"})
        self.assertEqual((status, body.get("code")), (409, "engine_unavailable"))

    def test_the_phone_must_still_be_ready(self):
        self.rt.target_status = Mock(return_value={**self.rt.target_status(), "ready": False, "can_act": False})
        status, body, _ = self.post({"appId": "com.fake.app"})
        self.assertEqual((status, body["code"]), (503, "device_unavailable"))

    def test_refuses_what_the_field_and_the_app_do_not_allow(self):
        for body, status, code in (({}, 400, None), ({"appId": "com.fake.app", "screens": 0}, 400, None),
                                   ({"appId": "com.fake.app", "minutes": 31}, 400, None),
                                   ({"appId": "com.fake.app", "goal": "x"}, 400, None),
                                   ({"appId": "com.nobody.app"}, 400, None),
                                   ({"appId": "com.apple.mobilephone"}, 422, "xray_app_refused")):
            got, answer, _ = self.post(body)
            self.assertEqual(got, status, body)
            if code:
                self.assertEqual(answer["code"], code)
                self.assertEqual(answer["error"], "App X-ray doesn't map Phone: a tap on a row there places a call. "
                                                  "Pick another app.")
        self.assertEqual(self.rt.runs, {})

    def test_the_same_key_gives_the_same_run(self):
        first = self.post({"appId": "com.fake.app", "key": "mapit"})[1]["runId"]
        self.assertEqual(self.post({"appId": "com.fake.app", "key": "mapit"})[1]["runId"], first)

    def test_a_coding_agent_engine_is_refused_for_the_field(self):
        with self.assertRaises(ValueError):
            self.rt.create("fake", "Map it", "live", engine="codex", extras={"xray": {}})

    def test_no_plan_question_for_an_x_ray_run_and_still_one_for_others(self):
        run = self.rt.create("fake", "Map Fake with App X-ray", "live", origin="app", extras={"xray": {}})
        self.rt.plan_for = Mock(return_value=None)
        self.assertIsNone(self.rt.confirm_plan(run))
        self.rt.plan_for.assert_not_called()
        self.finish(self.rt, run)
        with patch.object(engines, "smart_key", return_value="sk-ant-test-key-not-real"):
            other = self.rt.create("fake", "Open Fake", "live", origin="app")
        self.rt.confirm_plan(other)
        self.rt.plan_for.assert_called()

    def test_the_run_calls_no_model_client_and_ends_with_the_map(self):
        run = self.rt.create("fake", "Map Fake with App X-ray", "live", origin="app",
                             extras={"xray": job.clean({"screens": 8})})
        self.rt.smart_client = Mock(side_effect=AssertionError("an x-ray run must not open a model client"))
        driver = PreviewApp()
        self.rt.open_driver = Mock(return_value=driver)
        gathered = Mock(side_effect=AssertionError("an x-ray run must not gather context"))
        with patch("mobile_agent.xray.job.user_data_dir", return_value=Path(self.root)), \
                patch("mobile_agent.runtime_work.ContextGather", gathered):
            for patcher in FAST:
                patcher.start()
                self.addCleanup(patcher.stop)
            result = Runtime_run_smart(self.rt, run)
        self.rt.smart_client.assert_not_called()
        gathered.assert_not_called()
        self.assertEqual(result["status"], "completed")
        # Its screens are its proof: the app shows the result as done, not as one to check on the phone.
        self.assertTrue(result["independently_verified"])
        self.assertIn("screenshot", result["proof"][0]["quote"])
        stored = [e for e in run.events if e["event"] == "result"][-1]
        self.assertEqual([b["type"] for b in stored["blocks"]["blocks"]][0], "gallery")
        frame_ids = {e["frameId"] for e in run.events if e["event"] == "step" and e.get("frameId")}
        self.assertEqual({i["frameId"] for i in stored["blocks"]["blocks"][0]["items"]}, frame_ids)
        order = [e["event"] for e in run.events]
        self.assertLess(order.index("action_started"), order.index("action_acknowledged"))
        self.assertNotIn("inference_finished", order)
        self.finish(self.rt, run)


def Runtime_run_smart(runtime, run):
    return runtime.run_smart(run, Trace(), RunSetup(phase=lambda name: None, hold=lambda value: value,
                                                   finish_phone=lambda: None))


class Command(unittest.TestCase):
    def parse(self, *argv):
        from mobile_agent.cli_parser import build_parser
        return build_parser().parse_args(["xray", *argv])

    def test_the_whole_command_line_takes_an_app_name(self):
        # The app's dest must not be "app": main's check_placement refuses the terminal UI's --app on other commands.
        from mobile_agent.__main__ import check_placement
        from mobile_agent.cli_parser import build_parser
        parser = build_parser()
        args = parser.parse_args(["xray", "app", "com.apple.Preferences"])
        check_placement(parser, args, on_error=lambda message: self.fail(message))
        self.assertEqual(args.target_app, "com.apple.Preferences")

    def test_the_plan_is_needed_to_map_an_app_and_not_to_compare_two_maps(self):
        self.assertTrue(cli_needs_plan("xray", self.parse("app", "Weather")))
        self.assertFalse(cli_needs_plan("xray", self.parse("diff", "a", "b")))
        self.assertFalse(cli_needs_plan("xray", self.parse()))

    def test_diff_reads_files_or_folders_and_says_when_it_cannot(self):
        import contextlib
        maps = {"screens": [screen("s1", "Home", [], {"home"})]}
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "map.json").write_text(json.dumps(maps))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(diff_cli.run(self.parse("diff", folder, str(Path(folder, "map.json")))), 0)
            self.assertIn("No changes", out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(diff_cli.run(self.parse("diff", folder, folder, "--json")), 0)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(diff_cli.run(self.parse("diff", folder, folder + "/nope")), 1)
            self.assertIn("couldn't read a map", err.getvalue())

    def test_app_refuses_before_it_opens_a_session(self):
        import contextlib
        with patch("mobile_agent.tui.session.Session", side_effect=AssertionError("no session")):
            for argv, code in ((("app", "com.apple.mobilephone", "--json"), 3),
                               (("app", "Weather", "--screens", "0", "--json"), 2)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(app_cli.run(self.parse(*argv)), code)
                self.assertEqual(json.loads(out.getvalue())["status"], "error")

    def test_app_runs_through_the_runtime_with_no_key_and_prints_the_result(self):
        import contextlib
        app = {"id": "fake", "name": "Fake", "bundleId": "com.fake.app", "installed": True}
        result = {"event": "result", "status": "completed", "answer": "Mapped 3 screens of Fake.",
                  "xray": {"folder": "/x", "map": "/x/map.json", "report": "/x/report.html"}}
        run = SimpleNamespace(id="r1", summary=result, status="completed", finished_at=1)
        runtime = SimpleNamespace(fleet=None, apps_for=lambda phone: [app], create=Mock(return_value=run))
        session = SimpleNamespace(runtime=runtime, origin="cli", run=None, close=Mock(), stop=Mock(),
                                  follow=lambda run, on_event: [on_event(e) for e in (
                                      {"event": "action_started", "operation": "TAP"}, result,
                                      {"event": "run_finished"})])
        out = io.StringIO()
        with patch("mobile_agent.tui.session.Session", return_value=session,
                   autospec=True), contextlib.redirect_stdout(out), \
                patch.object(engines, "smart_key", side_effect=AssertionError("xray needs no key")):
            code = app_cli.run(self.parse("app", "Fake", "--json", "--screens", "9"))
        self.assertEqual(code, 0)
        runtime.create.assert_called_once()
        self.assertEqual(runtime.create.call_args.kwargs["extras"], {"xray": {"screens": 9, "minutes": 5}})
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([line["event"] for line in lines], ["action_started", "result"])
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
