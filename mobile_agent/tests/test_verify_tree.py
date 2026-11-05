import unittest

from mobile_agent.state import from_wda
from mobile_agent.tests.test_verify_fixtures import (
    FIREFOX_PAYWALL,
    FIREFOX_SETTINGS,
    FIREFOX_TODAY,
    HIDDEN_PAGES,
    SETTINGS_ABOUT,
    SETTINGS_GENERAL,
    SETTINGS_ROOT,
    TAB_VIEW,
    app,
    node,
)
from mobile_agent.transport import TransportError
from mobile_agent.verify.assertions import evaluate_on, parse_assertion
from mobile_agent.verify.tree import (
    EVIDENCE_SOURCE_PATH,
    GLANCE_SOURCE_PATH,
    MAX_NODES,
    RETRY_PAUSE,
    TEXT_LIMIT,
    TreeError,
    parse_tree,
    read_source,
    read_tree,
)


def labels(tree):
    return [n.label for n in tree.shown() if n.label]


class ParseTests(unittest.TestCase):
    def test_a_real_settings_tree(self):
        tree = parse_tree(SETTINGS_ROOT)
        self.assertEqual(tree.bundle_id, "com.apple.Preferences")
        self.assertEqual(tree.size, (402.0, 874.0))
        self.assertEqual(len(tree.nodes), 167)
        general = next(n for n in tree.nodes if n.label == "General" and n.role == "Button")
        self.assertEqual(general.identifier, "com.apple.settings.general")
        self.assertEqual(general.rect, (16.0, 380.0, 370.0, 53.0))
        self.assertTrue(general.enabled and general.visible)

    def test_paths_are_the_drivers_locators(self):
        for xml in (SETTINGS_ROOT, SETTINGS_GENERAL, SETTINGS_ABOUT, HIDDEN_PAGES, TAB_VIEW, FIREFOX_SETTINGS,
                    FIREFOX_PAYWALL, FIREFOX_TODAY):
            tree = parse_tree(xml)
            by_path = {n.path: n for n in tree.nodes}
            for element in from_wda(xml).elements:
                with self.subTest(locator=element.locator):
                    self.assertIn(element.locator, by_path)
                    self.assertEqual(by_path[element.locator].role, element.role)
        tree = parse_tree(app(node("Other", children=[node("Button", "A"), node("StaticText", "B"),
                                                      node("Button", "C")])))
        self.assertEqual([n.path for n in tree.nodes][3:], [
            "/XCUIElementTypeApplication/XCUIElementTypeWindow[1]/XCUIElementTypeOther[1]/XCUIElementTypeButton[1]",
            "/XCUIElementTypeApplication/XCUIElementTypeWindow[1]/XCUIElementTypeOther[1]/XCUIElementTypeStaticText[1]",
            "/XCUIElementTypeApplication/XCUIElementTypeWindow[1]/XCUIElementTypeOther[1]/XCUIElementTypeButton[2]"])

    def test_the_identifier_is_the_name_only_when_it_differs_from_the_label(self):
        tree = parse_tree(app(node("Button", "Continue"), node("Button", "Continue", id="onboarding_continue"),
                              node("Image", None, id="logo")))
        self.assertEqual([n.identifier for n in tree.nodes[2:]], ["", "onboarding_continue", "logo"])

    def test_values_placeholders_traits_and_enabled(self):
        tree = parse_tree(app(
            node("Switch", "Daily reminder", id="daily_reminder", value="1"),
            node("Button", "Annual", traits="Button, Selected"),
            node("TextField", "Email", value="Email", placeholder="Email"),
            node("Button", "Continue", enabled=False)))
        switch, annual, field, button = tree.nodes[2:]
        self.assertEqual(switch.value, "1")
        self.assertTrue(annual.selected)
        self.assertFalse(switch.selected)
        self.assertEqual(field.placeholder, "Email")
        self.assertFalse(button.enabled)

    def test_the_root_may_be_wrapped(self):
        tree = parse_tree('<AppiumAUT><XCUIElementTypeApplication width="390" height="844">'
                          '<XCUIElementTypeButton label="Go" x="1" y="2" width="50" height="40"/>'
                          '</XCUIElementTypeApplication></AppiumAUT>')
        self.assertEqual(tree.size, (390.0, 844.0))
        self.assertEqual(tree.nodes[-1].path, "/AppiumAUT/XCUIElementTypeApplication[1]/XCUIElementTypeButton[1]")
        self.assertEqual(labels(tree), ["Go"])

    def test_non_finite_frames_read_as_zero(self):
        tree = parse_tree(app(node("Other", "Spacer", rect=("inf", 0, "nan", 10))))
        self.assertEqual(tree.nodes[-1].rect, (0.0, 0.0, 0.0, 10.0))


class ShownTests(unittest.TestCase):
    def test_settings_rows_inside_a_collection_view_wda_calls_not_visible(self):
        tree = parse_tree(SETTINGS_ROOT)
        collection = next(n for n in tree.nodes if n.role == "CollectionView")
        self.assertFalse(collection.visible)
        self.assertIn("General", labels(tree))
        self.assertIn("StandBy", labels(tree))
        self.assertNotIn("Screen Time", labels(tree))

    def test_a_zero_size_wrapper_does_not_hide_its_content(self):
        tree = parse_tree(SETTINGS_ROOT)
        field = next(n for n in tree.nodes if n.role == "SearchField")
        self.assertIn(field, tree.shown())

    def test_hidden_swiftui_pages_are_not_shown(self):
        tree = parse_tree(HIDDEN_PAGES)
        self.assertIn("Weekly summary", labels(tree))
        self.assertIn("Share week", labels(tree))
        for hidden in ("Morning list", "Add habit", "Preferences", "Reset data"):
            self.assertNotIn(hidden, labels(tree))
            self.assertTrue(any(n.label == hidden for n in tree.nodes))

    def test_a_tab_view_shows_its_selected_page(self):
        tree = parse_tree(TAB_VIEW)
        self.assertIn("Weekly summary", labels(tree))
        self.assertIn("Today", labels(tree))

    def test_a_plain_form_shows_its_rows(self):
        tree = parse_tree(FIREFOX_SETTINGS)
        collection = next(n for n in tree.nodes if n.role == "CollectionView")
        self.assertFalse(collection.visible)
        for row in ("Reminders", "Daily reminder", "Reminder time", "Show paywall", "Version, 1.0 (1)"):
            self.assertIn(row, labels(tree))
        self.assertEqual([n.identifier for n in tree.select({"role": "switch"})], ["daily_reminder"])
        results = evaluate_on([parse_assertion({"visible": {"id": "daily_reminder"}}),
                               parse_assertion({"value": {"role": "switch"}, "equals": False}),
                               parse_assertion({"visible": {"id": "settings_paywall"}})], tree)
        self.assertEqual([r.ok for r in results], [True, True, True])

    def test_a_full_screen_cover_hides_the_rows_it_covers(self):
        tree = parse_tree(FIREFOX_PAYWALL)
        shown = labels(tree)
        for front in ("Choose your plan", "Weekly", "Monthly", "Annual", "Start free trial", "Restore Purchases"):
            self.assertIn(front, shown)
        for covered in ("Drink water", "Read 10 pages", "Build habits that stick", "Continue", "Page 1 of 3"):
            self.assertNotIn(covered, shown)
            self.assertTrue(any(n.label == covered for n in tree.nodes))
        results = evaluate_on([parse_assertion({"count": {"id": "/^plan_/"}, "equals": 3}),
                               parse_assertion({"no_text": "Build habits that stick"}),
                               parse_assertion({"absent": {"id": "onboarding_continue"}})], tree)
        self.assertEqual([r.ok for r in results], [True, True, True])

    def test_an_ancestors_visible_false_is_not_inherited(self):
        page = node("Other", children=[node("StaticText", "2:30 PM", rect=(55, 22, 38, 21))], rect=(0, 0, 402, 874),
                    visible=False)
        tree = parse_tree(app(page))
        clock = tree.nodes[-1]
        self.assertTrue(clock.visible and clock.hidden_by_ancestor)
        self.assertEqual(labels(tree), ["2:30 PM"])

    def test_a_toggle_counts_once(self):
        tree = parse_tree(FIREFOX_TODAY)
        self.assertEqual(len([n for n in tree.nodes if n.role == "Switch"]), 6)
        self.assertEqual([n.identifier for n in tree.select({"role": "switch"})],
                         ["habit_water", "habit_read", "habit_walk"])
        knobs = [n for n in tree.nodes if n.inner_toggle]
        self.assertEqual(len(knobs), 3)
        self.assertTrue(all(n.visible and n.role == "Switch" and not n.label for n in knobs))
        results = evaluate_on([parse_assertion({"count": {"role": "switch"}, "equals": 3}),
                               parse_assertion({"value": {"id": "habit_read"}, "equals": False})], tree)
        self.assertEqual([r.ok for r in results], [True, True])
        tree = parse_tree(app(node("Switch", "Wi-Fi", rect=(16, 100, 370, 44), children=[
            node("Switch", "Ask to Join", rect=(300, 105, 60, 30)),
            node("Switch", None, id="wifi_knob", rect=(300, 105, 60, 30)),
            node("Switch", None, rect=(300, 105, 60, 30))])))
        self.assertEqual([n.label or n.identifier for n in tree.select({"role": "switch"})],
                         ["Wi-Fi", "Ask to Join", "wifi_knob"])

    def test_a_node_off_screen_is_not_shown(self):
        tree = parse_tree(app(node("Button", "Wallpaper", rect=(16, 900, 370, 50)),
                              node("Button", "Left", rect=(-300, 100, 200, 50)),
                              node("Button", "Edge", rect=(0, 874, 100, 40)),
                              node("Button", "Partly", rect=(16, 850, 370, 50))))
        self.assertEqual(labels(tree), ["Partly"])
        self.assertFalse(tree.on_screen(tree.nodes[2]))

    def test_the_application_and_windows_are_never_shown(self):
        tree = parse_tree(app(node("Button", "Go")))
        self.assertEqual([n.role for n in tree.shown()], ["Button"])

    def test_secure_fields_are_shown(self):
        tree = parse_tree(app(node("SecureTextField", "Password", value="••••••••")))
        self.assertEqual([(n.role, n.value) for n in tree.shown()], [("SecureTextField", "••••••••")])


class LimitTests(unittest.TestCase):
    def test_long_evidence_is_refused_instead_of_making_negative_assertions_pass(self):
        harmless = "a" * TEXT_LIMIT
        self.assertEqual(parse_tree(app(node("StaticText", harmless))).nodes[-1].label, harmless)
        for source in (node("StaticText", harmless + "Payment failed"),
                       node("Button", "Continue", id=harmless + "_danger"),
                       node("TextField", "Email", value=harmless + "declined"),
                       node("TextField", "Email", placeholder=harmless + "private")):
            with self.subTest(source=source[-60:]), self.assertRaisesRegex(TreeError, "longer than 4,000"):
                parse_tree(app(source))

    def test_unsafe_or_broken_sources_are_refused(self):
        for xml in ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><XCUIElementTypeApplication/>',
                    "<XCUIElementTypeApplication", "<Other/>", "x" * 8_000_001, None,
                    '<XCUIElementTypeApplication width="0" height="10"/>'):
            with self.subTest(xml=str(xml)[:40]):
                with self.assertRaises(TreeError):
                    parse_tree(xml)

    def test_depth_and_node_limits(self):
        deep = node("Button", "Bottom")
        for _ in range(70):
            deep = node("Other", children=[deep])
        with self.assertRaisesRegex(TreeError, "deeper than 64"):
            parse_tree(app(deep))
        wide = app(*[node("StaticText", f"Row {i}") for i in range(MAX_NODES)])
        with self.assertRaisesRegex(TreeError, "more than 5,000"):
            parse_tree(wide)
        self.assertEqual(len(parse_tree(app(*[node("StaticText", "r")] * (MAX_NODES - 2))).nodes), MAX_NODES)


class FingerprintTests(unittest.TestCase):
    def test_what_changes_it(self):
        base = parse_tree(app(node("StaticText", "Loading", rect=(10, 10, 100, 20)))).fingerprint()
        self.assertNotEqual(base, parse_tree(app(node("StaticText", "Plans", rect=(10, 10, 100, 20)))).fingerprint())
        self.assertNotEqual(base, parse_tree(app(node("StaticText", "Loading", rect=(10, 40, 100, 20)))).fingerprint())
        self.assertNotEqual(base,
                            parse_tree(app(node("StaticText", "Loading", rect=(10.2, 10, 100, 20.3)))).fingerprint())
        self.assertEqual(base, parse_tree(app(node("StaticText", "Loading", rect=(10, 10, 100, 20)),
                                              node("Button", "Hidden", visible=False))).fingerprint())

    def test_assertion_identity_includes_selector_state_placeholder_and_exact_geometry(self):
        plain = parse_tree(app(node("TextField", "Email", placeholder="Email", rect=(10, 10, 100, 44))))
        changed = (node("TextField", "Email", placeholder="Other", rect=(10, 10, 100, 44)),
                   node("TextField", "Email", placeholder="Email", rect=(10, 10, 100, 43.9)),
                   node("TextField", "Email", placeholder="Email", rect=(10, 10, 100, 44), enabled=False),
                   node("TextField", "Email", placeholder="Email", rect=(10, 10, 100, 44),
                        traits="Selected"))
        for field in changed:
            with self.subTest(field=field[-100:]):
                self.assertNotEqual(plain.fingerprint(), parse_tree(app(field)).fingerprint())

    def test_audit_identity_includes_offscreen_nodes_and_screen_size(self):
        visible = node("Button", "Continue", rect=(10, 10, 100, 44))
        offscreen = node("Button", "Hidden", rect=(10, 900, 100, 44), enabled=False)
        clean = parse_tree(app(visible, offscreen))
        enabled = parse_tree(app(visible, node("Button", "Hidden", rect=(10, 900, 100, 44))))
        resized = parse_tree(app(visible, offscreen, width=390))
        self.assertEqual(clean.fingerprint(), enabled.fingerprint())
        self.assertNotEqual(clean.audit_fingerprint(), enabled.audit_fingerprint())
        self.assertNotEqual(clean.audit_fingerprint(), resized.audit_fingerprint())


class LayoutTests(unittest.TestCase):
    def test_a_glance_and_an_evidence_read_of_the_same_screen_have_equal_layouts(self):
        screen = (node("StaticText", "Plans", rect=(10, 10, 100, 20)),
                  node("Button", "Covered", rect=(10, 40, 100, 20), visible=False))
        evidence = parse_tree(app(*screen))
        glance = parse_tree(app(*screen).replace(' visible="false"', "").replace(' visible="true"', ""))
        self.assertEqual(evidence.layout(), glance.layout())
        self.assertNotEqual(glance.layout(), glance.fingerprint())
        self.assertNotEqual(evidence.fingerprint(), glance.fingerprint())

    def test_layout_still_follows_what_moved(self):
        a = parse_tree(app(node("StaticText", "A", rect=(10, 10, 100, 20))))
        b = parse_tree(app(node("StaticText", "A", rect=(10, 50, 100, 20))))
        c = parse_tree(app(node("StaticText", "A", rect=(10, 10, 100, 20)),
                           node("StaticText", "B", rect=(10, 60, 9, 9))))
        self.assertNotEqual(a.layout(), b.layout())
        self.assertNotEqual(a.layout(), c.layout())


class ReadTests(unittest.TestCase):
    class Driver:
        def __init__(self, answers):
            self.answers, self.paths = list(answers), []

        def call(self, method, path, body=None, timeout=10):
            self.paths.append((method, path))
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    def test_the_evidence_read_keeps_visible_and_traits_and_the_glance_read_drops_visible(self):
        self.assertEqual(EVIDENCE_SOURCE_PATH, "/source?format=xml&excluded_attributes=accessible,index")
        self.assertEqual(GLANCE_SOURCE_PATH, "/source?format=xml&excluded_attributes=visible,accessible,index")
        driver = self.Driver([SETTINGS_ROOT, SETTINGS_ROOT])
        self.assertEqual(len(read_tree(driver).nodes), 167)
        read_tree(driver, path=GLANCE_SOURCE_PATH)
        self.assertEqual(driver.paths, [("GET", EVIDENCE_SOURCE_PATH), ("GET", GLANCE_SOURCE_PATH)])

    def test_one_retry_on_a_transport_error_after_a_pause(self):
        pauses = []
        driver = self.Driver([TransportError("reset"), SETTINGS_ROOT])
        self.assertTrue(read_source(driver, pause=pauses.append).startswith("<?xml"))
        self.assertEqual(pauses, [RETRY_PAUSE])
        driver = self.Driver([TransportError("reset"), ConnectionRefusedError("gone")])
        with self.assertRaises(ConnectionRefusedError):
            read_source(driver, pause=pauses.append)
        self.assertEqual(len(driver.paths), 2)
        self.assertEqual(pauses, [RETRY_PAUSE] * 2)
        driver = self.Driver([SETTINGS_ROOT])
        read_source(driver, pause=pauses.append)
        self.assertEqual(len(pauses), 2)
        self.assertGreaterEqual(RETRY_PAUSE, .5)


if __name__ == "__main__":
    unittest.main()
