"""verify/assertions.py: every kind and selector field, normalization, regex, equals, near misses and purity. Offline."""

import socket
import unittest
from unittest import mock

from mobile_agent.tests.test_verify_fixtures import (HIDDEN_PAGES, SETTINGS_ABOUT, SETTINGS_ROOT, TAB_VIEW, app,
                                                     node, paywall)
from mobile_agent.verify.assertions import (CheckError, evaluate_on, expand_assertion, first_number, normalize,
                                            parse_assertion, parse_selector, same_number, value_equals)
from mobile_agent.verify.checks import CheckError as ChecksCheckError
from mobile_agent.verify.tree import parse_tree


def one(assertion, xml):
    return evaluate_on([parse_assertion(assertion)], parse_tree(xml))[0]


class NormalizeTests(unittest.TestCase):
    def test_the_rules(self):
        self.assertEqual(normalize("Don’t allow"), "Don't allow")
        self.assertEqual(normalize("“Hi” — there"), '"Hi" - there')
        self.assertEqual(normalize("‎579​­"), "579")
        self.assertEqual(normalize("  a \t\n  b  "), "a b")
        self.assertEqual(normalize("ＡＢＣ"), "ABC")  # NFKC: full-width letters
        self.assertEqual(normalize(None), "")

    def test_check_error_is_one_class(self):
        self.assertIs(CheckError, ChecksCheckError)
        self.assertTrue(issubclass(CheckError, ValueError))


class TextTests(unittest.TestCase):
    def test_text_is_a_case_insensitive_substring_of_a_label_or_value(self):
        xml = paywall()
        self.assertTrue(one({"text": "choose your PLAN"}, xml).ok)
        self.assertTrue(one({"text": "$7.99"}, xml).ok)          # a value
        self.assertTrue(one({"text": "Choose your plan"}, xml).ok)
        result = one({"text": "Choose your plan"}, xml)
        self.assertEqual(result.observed, 'found in text "Choose your plan"')
        self.assertEqual(result.text, 'text "Choose your plan"')
        self.assertEqual(result.matches[0]["id"], "paywall_title")
        self.assertEqual(set(result.matches[0]), {"id", "label", "value", "role", "rect"})

    def test_matching_is_per_node(self):
        xml = app(node("StaticText", "$39.99"), node("StaticText", "/ year"))
        self.assertFalse(one({"text": "$39.99 / year"}, xml).ok)
        self.assertTrue(one({"text": "$39.99 / year"}, app(node("StaticText", "$39.99 / year"))).ok)

    def test_regex_and_its_flag(self):
        xml = paywall()
        self.assertTrue(one({"text": r"/\$\d+\.\d\d \/ year/"}, xml).ok)
        self.assertFalse(one({"text": "/choose YOUR plan/"}, xml).ok)
        self.assertTrue(one({"text": "/choose YOUR plan/i"}, xml).ok)
        self.assertTrue(one({"text": "/ year"}, xml).ok)   # not a regex: no closing slash

    def test_invalid_regexes_are_check_errors(self):
        for text, message in (("/[/", "not a valid regex"), ("//", "empty regex"),
                              ("/" + "a" * 501 + "/", "longer than 500")):
            with self.subTest(text=text[:10]):
                with self.assertRaisesRegex(CheckError, message):
                    parse_assertion({"text": text})

    def test_missing_text_names_near_misses(self):
        result = one({"text": "Genral"}, SETTINGS_ROOT)
        self.assertFalse(result.ok)
        self.assertTrue(result.observed.startswith('not on screen; closest: "General"'))
        far = one({"text": "Not a real row"}, SETTINGS_ROOT)
        self.assertIn("closest:", far.observed)
        self.assertLessEqual(far.observed.count('", "') + 1, 5)

    def test_no_text(self):
        self.assertTrue(one({"no_text": "Loading"}, paywall()).ok)
        result = one({"no_text": "Loading"}, paywall(loading=True))
        self.assertFalse(result.ok)
        self.assertIn('"Loading plans…"', result.observed)

    def test_hidden_content_is_not_text_on_screen(self):
        self.assertFalse(one({"text": "Morning list"}, HIDDEN_PAGES).ok)
        self.assertTrue(one({"no_text": "Morning list"}, HIDDEN_PAGES).ok)
        self.assertTrue(one({"text": "Weekly summary"}, HIDDEN_PAGES).ok)


class SelectorTests(unittest.TestCase):
    def test_every_field(self):
        xml = app(node("Switch", "Daily reminder", id="daily_reminder", value="1"),
                  node("Button", "Annual", id="plan_annual", traits="Button, Selected"),
                  node("Button", "Continue", enabled=False), node("Button", "Weekly", id="plan_weekly"))
        cases = [({"id": "daily_reminder"}, 1), ({"id": "/^plan_/"}, 2), ({"label": "Annual"}, 1),
                 ({"label": "annual"}, 0), ({"value": "1"}, 1), ({"role": "switch"}, 1), ({"role": "button"}, 3),
                 ({"enabled": False}, 1), ({"enabled": True, "role": "button"}, 2), ({"selected": True}, 1),
                 ({"selected": False, "id": "/^plan_/"}, 1), ({"label": "/^(Annual|Weekly)$/"}, 2),
                 ({"label": "/annual/i", "selected": True}, 1)]
        for selector, count in cases:
            with self.subTest(selector=selector):
                self.assertEqual(one({"count": selector, "equals": count}, xml).ok, True,
                                 one({"count": selector, "equals": count}, xml).observed)

    def test_id_never_matches_a_bare_label(self):
        """WDA puts the label in name when no identifier is set: that name is not an identifier."""
        xml = app(node("Button", "Continue"))
        self.assertFalse(one({"visible": {"id": "Continue"}}, xml).ok)
        self.assertTrue(one({"visible": {"label": "Continue"}}, xml).ok)

    def test_friendly_roles(self):
        rows = [("Button", "button"), ("StaticText", "text"), ("TextField", "field"), ("SecureTextField", "field"),
                ("SearchField", "field"), ("TextView", "field"), ("Switch", "switch"), ("Toggle", "toggle"),
                ("Switch", "toggle"), ("Cell", "cell"), ("Image", "image"), ("Icon", "image"), ("Link", "link"),
                ("Tab", "tab"), ("Slider", "slider"), ("Stepper", "stepper"), ("Picker", "picker"),
                ("PickerWheel", "picker"), ("DatePicker", "picker"), ("SegmentedControl", "segment"),
                ("NavigationBar", "navbar"), ("Alert", "alert")]
        for raw, friendly in rows:
            with self.subTest(role=raw, name=friendly):
                xml = app(node(raw, "Thing"))
                self.assertTrue(one({"visible": {"role": friendly, "label": "Thing"}}, xml).ok)
                self.assertTrue(one({"visible": {"role": raw, "label": "Thing"}}, xml).ok)
                self.assertTrue(one({"visible": {"role": "XCUIElementType" + raw, "label": "Thing"}}, xml).ok)

    def test_a_tab_is_a_button_in_a_tab_bar(self):
        tree_xml = TAB_VIEW
        self.assertTrue(one({"count": {"role": "tab"}, "equals": 3}, tree_xml).ok)
        self.assertTrue(one({"visible": {"role": "tab", "label": "Week", "value": "1"}}, tree_xml).ok)
        self.assertFalse(one({"visible": {"role": "tab", "label": "Share week"}}, tree_xml).ok)
        self.assertFalse(one({"visible": {"role": "Tab", "label": "Week"}}, tree_xml).ok)  # the raw type only

    def test_selector_errors(self):
        cases = [({}, "at least one"), ({"lable": "x"}, "did you mean label"), ({"role": "buton"}, "did you mean button"),
                 ({"role": "Nonsense"}, "not a role"), ({"enabled": "yes"}, "true or false"), ({"id": 3}, "must be text"),
                 ({"label": ""}, "empty"), ("Continue", "must be an object")]
        for selector, message in cases:
            with self.subTest(selector=selector):
                with self.assertRaisesRegex(CheckError, message):
                    parse_selector(selector)


class KindTests(unittest.TestCase):
    def test_visible_and_absent(self):
        xml = paywall()
        self.assertTrue(one({"visible": {"label": "Restore Purchases", "role": "button"}}, xml).ok)
        self.assertTrue(one({"absent": {"id": "spinner"}}, xml).ok)
        result = one({"absent": {"id": "paywall_cta"}}, xml)
        self.assertFalse(result.ok)
        self.assertEqual(result.observed, "found 1: paywall_cta")
        missing = one({"visible": {"label": "Restore Purchase"}}, xml)
        self.assertFalse(missing.ok)
        self.assertIn('closest: "Restore Purchases"', missing.observed)

    def test_visible_says_when_a_match_is_off_screen(self):
        xml = app(node("Button", "Wallpaper", id="wallpaper", rect=(16, 1200, 370, 50)))
        result = one({"visible": {"id": "wallpaper"}}, xml)
        self.assertFalse(result.ok)
        self.assertIn("1 more off screen: swipe to it", result.observed)

    def test_value_equals_text(self):
        xml = paywall()
        result = one({"value": {"id": "plan_annual"}, "equals": "$39.99 / year"}, xml)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, 'value id=plan_annual == "$39.99 / year"')
        wrong = one({"value": {"id": "plan_annual"}, "equals": "$39.99 / year"}, paywall(annual="$39.99 / month"))
        self.assertFalse(wrong.ok)
        self.assertEqual(wrong.observed, 'was "$39.99 / month"')
        self.assertFalse(one({"value": {"id": "plan_annual"}, "equals": "$39.99 / YEAR"}, xml).ok)  # case-sensitive
        self.assertTrue(one({"value": {"id": "plan_annual"}, "equals": "$39.99 / year"}, xml).ok)

    def test_value_needs_exactly_one_match(self):
        xml = paywall()
        ambiguous = one({"value": {"id": "/^plan_/"}, "equals": "$2.99 / week"}, xml)
        self.assertFalse(ambiguous.ok)
        self.assertEqual(ambiguous.observed, "ambiguous: 3 matches (plan_weekly, plan_monthly, plan_annual)")
        missing = one({"value": {"id": "plan_yearly"}, "equals": "x"}, xml)
        self.assertFalse(missing.ok)
        self.assertTrue(missing.observed.startswith("not found"))

    def test_value_equals_booleans(self):
        for raw, expected, ok in (("1", True, True), ("0", False, True), ("on", True, True), ("Off", False, True),
                                  ("YES", True, True), ("no", True, False), ("true", False, False),
                                  ("maybe", True, False), ("maybe", False, False)):
            with self.subTest(raw=raw, expected=expected):
                xml = app(node("Switch", "Daily reminder", id="daily_reminder", value=raw))
                self.assertEqual(one({"value": {"id": "daily_reminder"}, "equals": expected}, xml).ok, ok)

    def test_value_equals_numbers(self):
        for raw, expected, ok in (("3", 3, True), ("3 habits", 3, True), ("3.4", 3, True), ("39.99", 39, False),
                                  ("$39.99 / year", 39.99, True), ("$39.99", 39.9, False), ("1,234 steps", 1234, True),
                                  ("12,5", 12, True), ("-2", -2, True), ("none", 0, False), ("0.50", 0.5, True)):
            with self.subTest(raw=raw, expected=expected):
                xml = app(node("StaticText", "Count", id="count", value=raw))
                self.assertEqual(one({"value": {"id": "count"}, "equals": expected}, xml).ok, ok)
        self.assertEqual(first_number("Walk 5,000 steps"), 5000.0)
        self.assertTrue(same_number(8848.86, 8849))
        self.assertEqual(value_equals("abc", 1)[0], False)

    def test_an_empty_field_shows_its_placeholder(self):
        xml = app(node("TextField", "Email", id="email", value="Email", placeholder="Email"))
        self.assertTrue(one({"value": {"id": "email"}, "equals": ""}, xml).ok)
        self.assertTrue(one({"text": "Email"}, xml).ok)  # the placeholder is on screen

    def test_count_bounds(self):
        xml = paywall()
        self.assertTrue(one({"count": {"id": "/^plan_/"}, "equals": 3}, xml).ok)
        two = one({"count": {"id": "/^plan_/"}, "equals": 3}, paywall(plans=("Monthly", "Annual")))
        self.assertFalse(two.ok)
        self.assertEqual(two.observed, "found 2: plan_monthly, plan_annual")
        self.assertEqual(two.text, "count id=/^plan_/ == 3")
        self.assertTrue(one({"count": {"role": "link"}, "at_least": 2}, xml).ok)
        self.assertFalse(one({"count": {"role": "link"}, "at_least": 3}, xml).ok)
        self.assertTrue(one({"count": {"role": "link"}, "at_most": 2}, xml).ok)
        self.assertFalse(one({"count": {"role": "link"}, "at_most": 1}, xml).ok)
        self.assertEqual(parse_assertion({"count": {"role": "link"}, "at_least": 2}).render(), "count role=link >= 2")
        self.assertEqual(parse_assertion({"count": {"role": "link"}, "at_most": 2}).render(), "count role=link <= 2")

    def test_assertion_errors(self):
        cases = [({}, "exactly one of"), ({"text": "a", "no_text": "b"}, "it has text, no_text"),
                 ({"txt": "a"}, "did you mean text"), ({"text": "a", "equals": 1}, "goes with value or count"),
                 ({"visible": {"id": "x"}, "at_least": 1}, "goes with value or count"),
                 ({"value": {"id": "x"}}, "value needs equals"), ({"value": {"id": "x"}, "equals": [1]}, "must be text"),
                 ({"value": {"id": "x"}, "equals": 1, "at_most": 2}, "takes no at_most"),
                 ({"count": {"id": "x"}}, "exactly one of equals"),
                 ({"count": {"id": "x"}, "equals": 1, "at_least": 1}, "exactly one of equals"),
                 ({"count": {"id": "x"}, "equals": True}, "whole number"),
                 ({"count": {"id": "x"}, "at_least": -1}, "whole number"),
                 ({"count": {"id": "x"}, "equals": 1.5}, "whole number"),
                 ({"text": "a", "name": "x" * 121}, "1 to 120"), ({"text": ["a"]}, "one string here"),
                 ({"text": 3}, "must be text, not a number; quote it"), ("text", "must be an object"),
                 ({"visible": {"label": True}}, "must be text, not true/false; quote it"),
                 ({"visible": {"label": ["a"]}}, r"must be text, not a list$")]
        for obj, message in cases:
            with self.subTest(obj=obj):
                with self.assertRaisesRegex(CheckError, message):
                    parse_assertion(obj)

    def test_a_list_of_texts_expands(self):
        parsed = expand_assertion({"text": ["Plans", "Restore"], "name": "both"})
        self.assertEqual([a.to_dict() for a in parsed], [{"text": "Plans", "name": "both"},
                                                          {"text": "Restore", "name": "both"}])
        with self.assertRaisesRegex(CheckError, "empty list"):
            expand_assertion({"no_text": []})

    def test_to_dict_round_trips(self):
        for obj in ({"text": "/plan/i"}, {"no_text": "Loading"}, {"visible": {"label": "A", "role": "button"}},
                    {"absent": {"id": "x", "enabled": False}}, {"value": {"id": "t"}, "equals": True},
                    {"value": {"id": "t"}, "equals": 39.99}, {"count": {"id": "/^p/"}, "at_most": 2, "name": "n"}):
            with self.subTest(obj=obj):
                self.assertEqual(parse_assertion(obj).to_dict(), obj)


class RealTreeTests(unittest.TestCase):
    def test_settings(self):
        tree = parse_tree(SETTINGS_ROOT)
        results = evaluate_on([parse_assertion(a) for a in (
            {"text": "General"}, {"visible": {"id": "com.apple.settings.general", "role": "button"}},
            {"no_text": "Wallpaper"}, {"count": {"role": "cell"}, "at_least": 5},
            {"visible": {"role": "field", "label": "Search"}})], tree)
        self.assertEqual([r.ok for r in results], [True, True, True, True, True], [r.observed for r in results])

    def test_unlabelled_matches_are_named_by_the_text_inside_them(self):
        # Review round 3: each Settings row is a Cell with no label or identifier; its text sits on a child Button
        # or StaticText. observed said 'cell, cell, cell …', which can't tell the rows apart.
        tree = parse_tree(SETTINGS_ROOT)
        result, = evaluate_on([parse_assertion({"count": {"role": "cell"}, "equals": 3})], tree)
        self.assertFalse(result.ok)
        self.assertRegex(result.observed, r'^found \d+: cell "Apple Account, Sign in to access your iCloud data, ')
        self.assertNotIn("cell, cell", result.observed)

    def test_a_node_with_nothing_labelled_inside_keeps_its_role(self):
        from mobile_agent.verify.assertions import node_name
        tree = parse_tree(SETTINGS_ROOT)
        bare = next(node for node in tree.nodes if node.role == "Other" and not node.identifier and not node.label
                    and not any(other.path.startswith(node.path + "/") for other in tree.nodes))
        from mobile_agent.verify.assertions import friendly_role
        self.assertEqual(node_name(bare, tree), friendly_role(bare.role))
        self.assertEqual(node_name(bare), friendly_role(bare.role))

    def test_about(self):
        tree = parse_tree(SETTINGS_ABOUT)
        results = evaluate_on([parse_assertion(a) for a in (
            {"text": "iOS Version"}, {"visible": {"id": "SW_VERSION_SPECIFIER", "label": "/^iOS Version, 26/"}},
            {"text": "General"})], tree)
        self.assertEqual([r.ok for r in results], [True, True, True])


class PurityTests(unittest.TestCase):
    def test_evaluation_never_touches_the_network(self):
        attempts = []  # counted too, so an attempt caught by an `except Exception` still fails the test

        def refuse(*args, **kwargs):
            attempts.append(args[1:] or kwargs)
            raise AssertionError("evaluate_on opened a connection")
        tree = parse_tree(SETTINGS_ROOT)
        with mock.patch.object(socket.socket, "connect", refuse), \
                mock.patch.object(socket, "create_connection", refuse):
            results = evaluate_on([parse_assertion({"text": "General"}),
                                   parse_assertion({"count": {"role": "cell"}, "at_least": 1})], tree)
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual(attempts, [])

    def test_the_same_tree_gives_the_same_results(self):
        tree = parse_tree(paywall())
        assertions = [parse_assertion({"count": {"id": "/^plan_/"}, "equals": 3}),
                      parse_assertion({"text": "Nope"})]
        self.assertEqual(evaluate_on(assertions, tree), evaluate_on(assertions, tree))


if __name__ == "__main__":
    unittest.main()
