import io
import json
import plistlib
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from PIL import Image

from mobile_agent.testkit import reports as R
from mobile_agent.testkit import targets as T
from mobile_agent.testkit.suite import Suite
from mobile_agent.tests.test_testing import result, validate_junit
from mobile_agent.tests.test_testing import suite_entry as entry
from mobile_agent.tests.test_verify_fixtures import (
    HIDDEN_PAGES,
    SETTINGS_ROOT,
    TAB_VIEW,
    app,
    node,
    paywall,
)
from mobile_agent.tests.test_verify_runner import (
    BUNDLE,
    KEY,
    Driver,
    Manager,
    RunnerCase,
)
from mobile_agent.verify import a11y, audit, conditions, runner
from mobile_agent.verify.assertions import (
    evaluate_on,
    expand_assertion,
    normalize,
    parse_assertion,
    parse_selector,
)
from mobile_agent.verify.checks import (
    MAX_NESTING,
    Check,
    CheckError,
    check_from_dict,
    dump_check,
    load_check,
    project_root,
)
from mobile_agent.verify.overlay import MAX_PILLS, draw_overlay, pill_numbers
from mobile_agent.verify.report import CSP, render_html, write_report
from mobile_agent.verify.runner import VerifyRun
from mobile_agent.verify.tree import parse_tree


def ids(findings):
    return sorted((item["id"], item["element"]["label"] or item["element"]["identifier"]) for item in findings)


def tree(*children, **kwargs):
    return parse_tree(app(node("Other", children=list(children), rect=(0, 0, 402, 874)), **kwargs))


class RuleTests(unittest.TestCase):
    def test_a_control_with_no_label_is_found_and_an_identifier_does_not_name_it(self):
        found = a11y.audit_tree(tree(node("Button", None, id="close_button", rect=(10, 60, 48, 48)),
                                     node("Button", "Close", rect=(70, 60, 48, 48)),
                                     node("Switch", None, value="1", rect=(10, 200, 51, 48)),
                                     node("TextField", None, placeholder="Email", rect=(10, 300, 300, 48)),
                                     node("TextField", None, rect=(10, 400, 300, 48))))
        self.assertEqual(ids(found), [("control-no-label", ""), ("control-no-label", ""),
                                      ("control-no-label", "close_button")])
        button = next(item for item in found if item["element"]["identifier"] == "close_button")
        self.assertEqual(button["guideline"], "WCAG 4.1.2")
        self.assertIn("Button (id close_button) at 10, 60, 48 x 48 pt", button["message"])
        self.assertIn("Give it one", button["message"])

    def test_a_tap_target_under_44_pt_is_found_in_either_direction(self):
        found = a11y.audit_tree(tree(node("Button", "Tall", rect=(10, 10, 44, 43)),
                                     node("Button", "Wide", rect=(10, 100, 43, 44)),
                                     node("Button", "Exact", rect=(10, 200, 44, 44)),
                                     node("Link", "Terms", rect=(10, 300, 60, 30)),
                                     node("StaticText", "Heading", rect=(10, 400, 100, 20))))
        self.assertEqual(ids(found), [("target-too-small", "Tall"), ("target-too-small", "Terms"),
                                      ("target-too-small", "Wide")])
        self.assertIn("is 44 x 43 pt, under 44 x 44",
                      next(i for i in found if i["element"]["label"] == "Tall")["message"])

    def test_an_image_with_no_label_is_found_unless_a_labelled_control_holds_it(self):
        found = a11y.audit_tree(tree(
            node("Image", None, id="hero", rect=(0, 100, 402, 200)),
            node("Image", "Logo", rect=(0, 320, 100, 50)),
            node("Image", None, rect=(0, 400, 1, 1)),
            node("Button", "Continue", rect=(10, 500, 200, 50), children=[node("Image", None, id="arrow",
                                                                               rect=(20, 510, 20, 20))]),
            node("Button", None, id="x", rect=(10, 600, 50, 50), children=[node("Image", None, id="x.png",
                                                                                rect=(20, 610, 20, 20))])))
        rules = ids(found)
        self.assertIn(("image-no-label", "hero"), rules)
        self.assertIn(("image-no-label", "x.png"), rules)         # inside an unlabelled control: still found
        self.assertNotIn(("image-no-label", "arrow"), rules)
        self.assertEqual(len([r for r in rules if r[0] == "image-no-label"]), 2)

    def test_clipped_text_needs_a_large_size_and_a_frame_that_leaves_the_screen_or_its_container(self):
        screen = tree(node("StaticText", "Order total and delivery estimate", rect=(300, 100, 200, 40)),
                      node("Other", rect=(20, 200, 200, 60), children=[
                          node("StaticText", "Subscribe", rect=(20, 200, 180, 90))]),
                      node("StaticText", "Fits", rect=(20, 400, 100, 30)))
        self.assertEqual(a11y.audit_tree(screen), [])                                  # no content_size
        self.assertEqual(a11y.audit_tree(screen, content_size="large"), [])            # the default size
        found = a11y.audit_tree(screen, content_size="accessibility-extra-large", frame="frames/02-step.jpg")
        self.assertEqual(ids(found), [("text-clipped", "Order total and delivery estimate"),
                                      ("text-clipped", "Subscribe")])
        self.assertIn("wider than the screen", found[0]["message"])
        self.assertIn("outside its Other", found[1]["message"])
        self.assertEqual(found[0]["frame"], "frames/02-step.jpg")
        self.assertEqual(found[0]["guideline"], "WCAG 1.4.4")

    def test_text_in_a_scroll_view_may_run_past_the_edge(self):
        screen = tree(node("ScrollView", rect=(0, 100, 402, 100), children=[
            node("StaticText", "Carousel card one", rect=(300, 120, 200, 40))]))
        self.assertEqual(a11y.audit_tree(screen, content_size="extra-extra-large"), [])

    def test_the_collector_reports_an_element_on_a_screen_once_across_reads(self):
        collector = a11y.Collector()
        small = tree(node("Button", "Tiny", rect=(10, 10, 20, 20)))
        collector.add(small, "frames/01-launch.jpg")
        collector.add(small, "frames/02-step.jpg")
        collector.add(tree(node("Button", "Tiny", rect=(10, 10, 20, 20)),
                           node("StaticText", "More", rect=(10, 100, 50, 20))), "frames/03.jpg")
        self.assertEqual(len(collector.findings()), 1)
        self.assertEqual(collector.findings()[0]["frame"], "frames/01-launch.jpg")
        self.assertEqual(collector.screens, 2)


class CheckKeyTests(unittest.TestCase):
    def test_audit_a11y_loads_without_a_built_app_and_beside_the_static_audits(self):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "Hi"}], "audit": "a11y"})
        self.assertEqual(check.audit, ("a11y",))

    def test_a_typo_names_the_audit_to_use(self):
        with self.assertRaisesRegex(CheckError, r"audit\[0\] 'a11' is not an audit; did you mean a11y\?"):
            check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "Hi"}], "audit": ["a11"]})

    def test_throughout_somewhere_and_audit_round_trip(self):
        data = {"version": 1, "app": {"bundle": "org.mozilla.ios.Firefox"}, "steps": ["Open About"],
                "throughout": [{"no_text": "/lorem ipsum/i", "name": "2.1 no placeholder text"}],
                "somewhere": [{"visible": {"label": "Restore Purchases"}, "name": "3.1.1 restore"}],
                "audit": ["usage-descriptions"]}
        check = check_from_dict(data)
        again = check_from_dict(yaml.safe_load(dump_check(check)))
        self.assertEqual((again.throughout, again.somewhere, again.audit),
                         (check.throughout, check.somewhere, check.audit))

    def test_throughout_takes_only_what_can_hold_on_every_screen(self):
        with self.assertRaisesRegex(CheckError, "no_text or absent"):
            check_from_dict({"version": 1, "app": {"bundle": "org.mozilla.ios.Firefox"},
                             "throughout": [{"text": "Welcome"}]})


BAD = app(node("Other", children=[
    node("StaticText", "Account", id="title", rect=(24, 118, 354, 34)),
    node("Button", None, id="close", rect=(340, 60, 30, 30)),
    node("Image", None, id="banner", rect=(0, 200, 402, 120)),
    node("Button", "Save", rect=(24, 700, 354, 50))], rect=(0, 0, 402, 874)), bundle=BUNDLE)


CLEAN = app(node("Other", children=[
    node("StaticText", "Account", id="title", rect=(24, 118, 354, 34)),
    node("Button", "Save", rect=(24, 700, 354, 50))], rect=(0, 0, 402, 874)), bundle=BUNDLE)


class RunTests(RunnerCase):
    def run_check(self, screen, audit=("a11y",), conditions=None):
        data = {"app": {"bundle": BUNDLE}, "expect": [{"text": "Account"}]}
        if audit:
            data["audit"] = list(audit)
        if conditions:
            data["conditions"] = conditions
        check = check_from_dict(data)
        manager = Manager()
        driver = Driver([screen])
        self.watch(driver, manager)
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager,
                        simctl=lambda args: (0, "large" if len(args) == 3 else ""))
        with mock.patch.object(runner, "build_driver", lambda *a: driver):
            run.prepare()
        return run.finish()

    def test_a_screen_with_findings_fails_the_check_with_each_finding_typed_and_no_model(self):
        outcome = self.run_check(BAD)
        self.assertEqual((outcome["verdict"], outcome["reason"]["class"]), ("failed", "finding"))
        self.assertEqual(outcome["cost_usd"], 0)
        found = [item for item in outcome["findings"] if item.get("audit") == "a11y"]
        self.assertEqual(ids(found), [("control-no-label", "close"), ("image-no-label", "banner"),
                                      ("target-too-small", "close")])
        self.assertTrue(all(item["frame"].startswith("frames/") for item in found))
        self.assertEqual(outcome["check"]["audit"], ["a11y"])

    def test_a_clean_screen_passes_and_a_check_without_the_audit_is_left_alone(self):
        self.assertEqual(self.run_check(CLEAN)["verdict"], "passed")
        self.assertEqual(self.run_check(BAD, audit=())["verdict"], "passed")

    def test_content_size_in_the_check_turns_on_the_clipping_rule(self):
        wide = app(node("Other", children=[
            node("StaticText", "Account", rect=(300, 118, 354, 34))], rect=(0, 0, 402, 874)), bundle=BUNDLE)
        self.assertEqual(self.run_check(wide)["verdict"], "passed")
        outcome = self.run_check(wide, conditions={"content_size": "accessibility-large"})
        self.assertEqual([item["id"] for item in outcome["findings"]], ["text-clipped"])


class A11yReportTests(unittest.TestCase):
    def finding(self, rule="target-too-small", label="Not now", rect=(330, 60, 60, 30)):
        return {"id": rule, "audit": "a11y", "guideline": a11y.GUIDELINES[rule], "level": "fail",
                "frame": "frames/01-launch.jpg", "message": f"Button \"{label}\" at 330, 60, 60 x 30 pt: too small",
                "evidence": "x", "element": {"role": "Button", "label": label, "identifier": "", "path": "/a",
                                              "rect": list(rect)}}

    def doc(self, quarantined=False):
        cell = {"device": "iPhone 17 Pro · iOS 26.4", "deviceKey": "sim:a", "status": "failed", "attempts": 1,
                "flaky": False, "report": None, "durationMs": 2000, "healed": [], "quarantined": quarantined,
                "reason": {"class": "finding", "message": "2 findings: ...", "fix": None}, "frame": None,
                "video": None, "costUsd": 0, "runs": [],
                "findings": [self.finding(), self.finding("control-no-label", "", (10, 10, 50, 50))]}
        return {"schema": R.SCHEMA, "suite": "s", "startedAt": 1_791_380_000_000, "durationMs": 1000, "exitCode": 1,
                "options": {}, "devices": [{"key": "sim:a", "name": "iPhone 17 Pro · iOS 26.4"}],
                "checks": [{"name": "Paywall", "file": "p.yaml", "classname": "checks.paywall", "tags": [],
                            "quarantined": quarantined, "results": [cell]}], "summary": {}, "mobster": {"version": "x"}}

    def test_each_finding_is_its_own_junit_case_with_the_element_and_where(self):
        root = validate_junit(R.junit(self.doc()))
        cases = root[0].findall("testcase")
        self.assertEqual(len(cases), 3)                                    # the check, then one per finding
        small = cases[1]
        self.assertEqual(small.get("classname"), "checks.paywall.a11y")
        self.assertEqual(small.get("name"), "Paywall: target-too-small, Button \"Not now\"")
        failure = small.find("failure")
        self.assertEqual(failure.get("type"), "accessibility")
        self.assertIn("Where: 330, 60, 60 x 30 pt", failure.text)
        self.assertIn("Element: Button \"Not now\"", failure.text)
        props = {p.get("name"): p.get("value") for p in small.iter("property")}
        self.assertEqual((props["audit"], props["rule"], props["guideline"]), ("a11y", "target-too-small", "HIG 44 pt"))
        self.assertEqual(root.get("failures"), "3")

    def test_a_quarantined_check_skips_its_finding_cases_too(self):
        root = validate_junit(R.junit(self.doc(quarantined=True)))
        cases = root[0].findall("testcase")
        self.assertEqual([case.find("skipped") is not None for case in cases], [True, True, True])
        self.assertEqual(root.get("failures"), "0")

    def test_results_json_keeps_the_typed_findings_per_device(self):
        def attempt(check, record, *, label, video_name=None):
            return {**result("failed", "finding"), "findings": [self.finding()]}
        doc = Suite([entry("a")], [T.DEFAULT], attempt=attempt, retries=0).run()
        cell = doc["checks"][0]["results"][0]
        self.assertEqual(cell["findings"][0]["id"], "target-too-small")
        self.assertEqual(cell["findings"][0]["element"]["rect"], [330, 60, 60, 30])


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
        cases = [({}, "at least one"), ({"lable": "x"}, "did you mean label"),
                 ({"role": "buton"}, "did you mean button"),
                 ({"role": "Nonsense"}, "not a role"), ({"enabled": "yes"}, "true or false"),
                 ({"id": 3}, "must be text"),
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
                 ({"value": {"id": "x"}}, "value needs equals"),
                 ({"value": {"id": "x"}, "equals": [1]}, "must be text"),
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
        tree = parse_tree(SETTINGS_ROOT)
        result, = evaluate_on([parse_assertion({"count": {"role": "cell"}, "equals": 3})], tree)
        self.assertFalse(result.ok)
        self.assertRegex(result.observed, r'^found \d+: cell "Apple Account, Sign in to access your iCloud data, ')
        self.assertNotIn("cell, cell", result.observed)


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


PAYWALL = """\
# .mobster/checks/paywall.yaml
version: 1
name: A new user sees three plans on the paywall
app:
  bundle: org.mozilla.ios.Firefox
  path: .mobster/build/Build/Products/Debug-iphonesimulator/Firefox.app
device: iPhone 17 Pro
runtime: iOS 26.4
reset: reinstall
launch:
  args: ["-FirefoxSkipOnboarding", "YES"]
  env: { FIREFOX_SEED: "3" }
  url: firefox://paywall
steps:
  - Complete onboarding as a new user. Don't allow notifications.
  - Reach the paywall.
expect:
  - text: Choose your plan
  - count: { id: /^plan_/ }
    equals: 3
  - value: { id: plan_annual }
    equals: $39.99 / year
  - visible: { label: Restore Purchases, role: button }
  - no_text: [Loading, Error]
budget: { max_seconds: 180, max_usd: 0.25 }
"""


def minimal(**extra):
    return {"app": {"bundle": "org.mozilla.ios.Firefox"}, **extra}


class FileShapeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name).resolve()
        (self.root / ".mobster" / "checks").mkdir(parents=True)

    def tearDown(self):
        self.folder.cleanup()

    def write(self, text, name="paywall.yaml", where=".mobster/checks"):
        path = self.root / where / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_the_spec_example(self):
        path = self.write(PAYWALL)
        check = load_check(path)
        self.assertEqual(check.name, "A new user sees three plans on the paywall")
        self.assertEqual(check.bundle_id, "org.mozilla.ios.Firefox")
        self.assertEqual(check.app_path, str(self.root / ".mobster/build/Build/Products/Debug-iphonesimulator/"
                                                         "Firefox.app"))
        self.assertEqual((check.device, check.runtime, check.reset), ("iPhone 17 Pro", "iOS 26.4", "reinstall"))
        self.assertEqual(check.launch_args, ("-FirefoxSkipOnboarding", "YES"))
        self.assertEqual(check.launch_env, {"FIREFOX_SEED": "3"})
        self.assertEqual(check.open_url, "firefox://paywall")
        self.assertEqual(len(check.steps), 2)
        self.assertEqual([a.kind for a in check.expect], ["text", "count", "value", "visible", "no_text", "no_text"])
        self.assertEqual((check.max_seconds, check.max_usd), (180, 0.25))
        self.assertEqual(check.source, str(path.resolve()))

    def test_the_project_root_is_the_folder_holding_dot_mobster(self):
        self.assertEqual(project_root(self.root / ".mobster" / "checks" / "x.yaml"), self.root)
        deep = self.write("app: {bundle: a.b, path: App.app}\n", "x.yaml", "sub/dir")
        self.assertEqual(load_check(deep).app_path, str(self.root / "App.app"))
        with tempfile.TemporaryDirectory() as other:
            loose = Path(other).resolve() / "check.yaml"
            loose.write_text("app: {path: Build/App.app}\n")
            self.assertEqual(load_check(loose).app_path, str(Path(other).resolve() / "Build" / "App.app"))

    def test_json_files(self):
        path = self.write(json.dumps({"app": {"bundle": "com.apple.Preferences"}, "expect": [{"text": "General"}]}),
                          "settings.json")
        self.assertEqual(load_check(path).expect[0].to_dict(), {"text": "General"})

    def test_file_errors(self):
        cases = [("app: {bundle: a.b\n", "line 2"), ("", "is empty"), ("- a\n- b\n", "must be an object"),
                 ("app: {bundle: a.b}\nexpect: [{txt: Hi}]\n", "did you mean text"),
                 ("app: {bundle: a.b}\nlaunch: {args: [-Flag, YES]}\n", r"launch\.args\[1\] must be text; quote it")]
        for text, message in cases:
            with self.subTest(text=text):
                with self.assertRaisesRegex(CheckError, message):
                    load_check(self.write(text, "bad.yaml"))
        with self.assertRaisesRegex(CheckError, "does not exist"):
            load_check(self.root / "missing.yaml")
        big = self.write("# " + "x" * 1_000_001, "big.yaml")
        with self.assertRaisesRegex(CheckError, "larger than 1 MB"):
            load_check(big)

    def test_the_draft_round_trip(self):
        check = load_check(self.write(PAYWALL))
        text = dump_check(check, project_root=self.root)
        self.assertIn("path: .mobster/build/Build/Products/Debug-iphonesimulator/Firefox.app", text)
        first, second = text.splitlines()[:2]
        self.assertEqual(first, "# yaml-language-server: $schema=https://mobster.dev/schema/check.json")
        self.assertTrue(second.startswith("# A Mobster check."))
        again = load_check(self.write(text, "draft.yaml", ".mobster/runs/20261001-101500-3f9a"))
        self.assertEqual(again.__dict__ | {"source": None}, check.__dict__ | {"source": None})

    def test_round_trips_of_tricky_values(self):
        check = check_from_dict({
            "app_path": str(self.root / "Build" / "Weird App.app"), "bundle_id": "org.mozilla.ios.Firefox",
            "steps": ["Open: the \"paywall\"", "yes"], "launch_args": ["YES", "-1", "on"],
            "launch_env": {"FLAG": "NO", "COUNT": "007"}, "open_url": "firefox://x?a=1&b=2", "max_usd": 0.5,
            "max_seconds": 90, "expect": [{"value": {"id": "t"}, "equals": True},
                                          {"value": {"id": "n"}, "equals": 39.99},
                                          {"value": {"id": "s"}, "equals": "1"},
                                          {"text": "/^(Yes|No)$/i"}, {"absent": {"enabled": False, "role": "button"}},
                                          {"count": {"label": "null"}, "at_most": 0}]})
        for root in (self.root, None):
            with self.subTest(root=root):
                path = self.write(dump_check(check, project_root=root), "tricky.yaml")
                self.assertEqual(load_check(path).__dict__ | {"source": None}, check.__dict__)


class ToolShapeTests(unittest.TestCase):
    def test_mcp_arguments(self):
        check = check_from_dict({"app_path": "/abs/Firefox.app", "steps": ["Reach the paywall."],
                                 "expect": [{"text": "Choose your plan"}], "launch_args": ["-A"],
                                 "launch_env": {"X": "1"}, "open_url": "firefox://paywall", "max_usd": 0.1,
                                 "image": True, "reset": "data"})
        self.assertEqual(check.app_path, "/abs/Firefox.app")
        self.assertIsNone(check.bundle_id)
        self.assertEqual(check.name, "Reach the paywall.")
        self.assertEqual((check.max_usd, check.max_seconds, check.reset), (0.1, 180, "data"))

    def test_names_default(self):
        self.assertEqual(check_from_dict({"bundle_id": "a.b"}).name, "Launch check")
        long = check_from_dict({"bundle_id": "a.b", "steps": ["x" * 300]}).name
        self.assertEqual(len(long), 120)
        self.assertTrue(long.endswith("…"))

    def test_shapes_do_not_mix(self):
        with self.assertRaisesRegex(CheckError, "mixes the file format"):
            check_from_dict({"app": {"bundle": "a.b"}, "bundle_id": "a.b"})
        with self.assertRaisesRegex(CheckError, "needs an app"):
            check_from_dict({"steps": ["x"]})
        with self.assertRaisesRegex(CheckError, "did you mean bundle_id"):
            check_from_dict({"bundle_idd": "a.b"})


class ErrorTests(unittest.TestCase):
    def test_a_file_nested_too_deeply(self):
        with tempfile.TemporaryDirectory() as folder:
            # MAX_NESTING + 1 parses on every Python: the limit is the check's own, not the parser's recursion.
            for suffix, depth in ((".json", 100_000), (".yaml", 100_000), (".json", MAX_NESTING + 1)):
                with self.subTest(suffix=suffix, depth=depth):
                    path = Path(folder) / f"deep{suffix}"
                    path.write_text("[" * depth + "]" * depth)
                    with self.assertRaisesRegex(CheckError, "nested too deeply"):
                        load_check(path)
            path.write_text("[" * MAX_NESTING + "]" * MAX_NESTING)
            with self.assertRaisesRegex(CheckError, "must be an object"):
                load_check(path)

    def test_every_rule(self):
        cases = [
            (minimal(nmae="x"), "unknown key 'nmae'; did you mean name"),
            ({"app": {"bundel": "a.b"}}, "app has an unknown key 'bundel'; did you mean bundle"),
            (minimal(launch={"arg": []}), "did you mean args"),
            (minimal(budget={"max_dollars": 1}), "unknown key 'max_dollars'"),
            ({"app": {}}, "needs app.bundle or app.path"),
            ({"app": {"bundle": "not a bundle"}}, "not a bundle ID"),
            (minimal(version=2), "version 2 is not supported"),
            (minimal(version=True), "not supported"),
            (minimal(name="x" * 121), "longer than 120"),
            (minimal(name=3), "name must be text"),
            (minimal(steps=["s"] * 21), "more than 20 steps"),
            (minimal(steps=["x" * 501]), r"steps\[0\] is longer than 500"),
            (minimal(steps=[""]), r"steps\[0\] is empty"),
            (minimal(steps={"a": 1}), "list of plain-English steps"),
            (minimal(expect=[{"text": f"t{i}"} for i in range(51)]), "more than 50 assertions"),
            (minimal(expect={"text": "a"}), "expect must be a list"),
            (minimal(launch={"args": ["a"] * 21}), "more than 20 items"),
            (minimal(launch={"env": {f"K{i}": "v" for i in range(21)}}), "more than 20 entries"),
            (minimal(launch={"env": {"lower": "v"}}), "not a variable name"),
            (minimal(launch={"env": {"1X": "v"}}), "not a variable name"),
            (minimal(launch={"env": {"X": 3}}), "must be text; quote it"),
            (minimal(launch={"url": "x" * 2001}), "longer than 2,000"),
            (minimal(launch={"url": "paywall"}), "needs? a scheme|with a scheme"),
            (minimal(reset="all"), "reset must be one of none, data, reinstall"),
            (minimal(budget={"max_usd": 1.5}), "at most 1"),
            (minimal(budget={"max_usd": 0}), "more than 0"),
            (minimal(budget={"max_usd": True}), "must be a number"),
            (minimal(budget={"max_seconds": 601}), "at most 600"),
            (minimal(app="a.b"), "app must be an object"),
            ("check", "must be an object"),
        ]
        for data, message in cases:
            with self.subTest(data=str(data)[:60]):
                with self.assertRaisesRegex(CheckError, message):
                    check_from_dict(data)

    def test_falsey_non_object_sections_are_rejected(self):
        for section in ("app", "launch", "budget"):
            for value in (False, 0, "", []):
                with self.subTest(section=section, value=value), \
                        self.assertRaisesRegex(CheckError, f"{section} must be an object"):
                    check_from_dict(minimal(**{section: value}))

    def test_reset_levels(self):
        def level(**kwargs):
            return Check(name="n", steps=(), expect=(), device=None, runtime=None, launch_args=(), **kwargs)
        self.assertEqual(level(app_path="/a.app", bundle_id=None, reset="auto").reset_level(), "reinstall")
        self.assertEqual(level(app_path=None, bundle_id="a.b", reset="auto").reset_level(), "data")
        self.assertEqual(level(app_path=None, bundle_id="a.b", reset="none").reset_level(), "none")
        self.assertEqual(level(app_path=None, bundle_id="com.apple.Preferences", reset="data").reset_level(), "none")


class NoticesTests(unittest.TestCase):
    def test_every_pinned_dependency_has_a_notice(self):
        root = Path(__file__).resolve().parents[2]
        requirements, notices = root / "mobile_agent" / "requirements.txt", root / "THIRD_PARTY_NOTICES.md"
        if not (requirements.is_file() and notices.is_file()):
            self.skipTest("no source checkout")

        def key(name):
            return re.sub(r"[-_.]+", "-", name.strip()).lower()
        pins = dict(line.split("==", 1) for line in requirements.read_text().splitlines()
                    if "==" in line and not line.lstrip().startswith("#"))
        rows = {}
        for line in notices.read_text().splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if line.startswith("| ") and len(cells) >= 3:
                rows[key(cells[0])] = cells[1]
        self.assertIn("PyYAML", pins)
        self.assertEqual({key(name): version.strip() for name, version in pins.items() if rows.get(key(name)) !=
                          version.strip()}, {}, "pins with no row, or another version, in THIRD_PARTY_NOTICES.md")


PLANS = paywall()


FULL = {"appearance": "dark", "locale": "de_DE", "content_size": "accessibility-extra-large",
        "location": {"latitude": 52.52, "longitude": 13.405}, "permissions": {"photos": "grant", "location": "revoke"},
        "media": ["fixtures/receipt.jpg"], "push": "payloads/shipped.json",
        "status_bar": {"time": "9:41", "battery_level": 100, "wifi_bars": 3}}


class Files:
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        (self.base / "fixtures").mkdir()
        (self.base / "payloads").mkdir()
        (self.base / "fixtures" / "receipt.jpg").write_bytes(b"\xff\xd8\xff")
        (self.base / "payloads" / "shipped.json").write_text(json.dumps({"aps": {"alert": "Your order shipped"}}))

    def parse(self, block):
        return conditions.parse(block, self.base)


class ParseTests(Files, unittest.TestCase):
    def test_a_full_block_is_read_and_files_resolve_against_the_project(self):
        out = self.parse(FULL)
        self.assertEqual(out["locale"], "de_DE")
        self.assertEqual(out["push"], str(self.base / "payloads" / "shipped.json"))
        self.assertEqual(out["media"], [str(self.base / "fixtures" / "receipt.jpg")])
        self.assertEqual(self.parse({"location": "52.52,13.405"})["location"],
                         {"latitude": 52.52, "longitude": 13.405})
        self.assertEqual(self.parse({"locale": "fr-CA"})["locale"], "fr_CA")

    def test_the_block_survives_a_check_file_round_trip(self):
        data = {"app": {"bundle": BUNDLE}, "expect": [{"text": "Hi"}], "conditions": FULL}
        check = check_from_dict(data, base_dir=self.base)
        path = self.base / "c.yaml"
        path.write_text(dump_check(check, project_root=self.base))
        self.assertEqual(load_check(path).conditions, check.conditions)
        self.assertIn("payloads/shipped.json", path.read_text())

    def refused(self, block, *words):
        with self.assertRaises(CheckError) as caught:
            self.parse(block)
        for word in words:
            self.assertIn(word, str(caught.exception))
        return str(caught.exception)

    def test_each_bad_key_says_what_is_wrong_then_what_to_do(self):
        self.refused({"apperance": "dark"}, "conditions.apperance isn't a condition", "did you mean appearance")
        self.refused({"appearance": "darkk"}, "isn't one Mobster knows", "did you mean dark", "Use one of light, dark")
        self.refused({"content_size": "huge"}, "Use one of extra-small")
        self.refused({"locale": "German"}, "isn't a locale", "Write a language and region")
        self.refused({"location": {"latitude": 91, "longitude": 0}}, "latitude must be a number from -90 to 90")
        self.refused({"location": {"latitude": 1}}, "needs a latitude and a longitude")
        self.refused({"permissions": {"phtos": "grant"}}, "did you mean photos")
        self.refused({"permissions": {"photos": "allow"}}, "Use one of grant, revoke, reset")
        self.refused({"status_bar": {"wifi_bars": 5}}, "whole number from 0 to 3")
        self.refused({"status_bar": {"batery": 1}}, "did you mean")
        self.refused({"media": []}, "must list files")
        self.refused({"media": ["fixtures/nope.jpg"]}, "doesn't exist", "relative to the project")
        self.refused({"media": ["fixtures/receipt.jpg.txt"]}, "isn't a kind of file")
        self.refused({"push": "payloads/nope.json"}, "doesn't exist")
        self.refused({}, "must set something")

    def test_a_push_payload_is_checked_at_load(self):
        (self.base / "payloads" / "bad.json").write_text("{nope")
        (self.base / "payloads" / "noaps.json").write_text("{}")
        (self.base / "payloads" / "big.json").write_text(json.dumps({"aps": {"alert": "x" * 5000}}))
        self.refused({"push": "payloads/bad.json"}, "isn't valid JSON", "Fix the file")
        self.refused({"push": "payloads/noaps.json"}, "no \"aps\" object")
        self.refused({"push": "payloads/big.json"}, "larger than 4,096 bytes")

    def test_increase_contrast_the_route_and_the_status_bar_modes_are_read(self):
        out = self.parse({"increase_contrast": "enabled", "route": {"waypoints": ["52.52,13.40", {"latitude": 52.51,
                          "longitude": 13.38}], "speed": 5}, "status_bar": {"wifi_mode": "active",
                                                                            "cellular_mode": "notSupported",
                                                                            "data_network": "5g-uc"}})
        self.assertEqual(out["route"], {"waypoints": [{"latitude": 52.52, "longitude": 13.4},
                                                       {"latitude": 52.51, "longitude": 13.38}], "speed": 5.0})
        self.assertEqual(self.parse({"route": {"waypoints": ["1,2", "3,4"]}})["route"]["speed"], 20.0)
        self.refused({"increase_contrast": True}, "isn't one Mobster knows", "Use one of enabled, disabled")
        self.refused({"route": {"waypoints": ["1,2"]}}, "2 to 50 places")
        self.refused({"route": {"waypoints": ["1,2", "95,3"]}}, "waypoints[1].latitude must be a number from -90")
        self.refused({"route": {"waypoints": ["1,2", "3,4"], "speed": 0}}, "above 0")
        self.refused({"route": {"speed": 3}}, "needs waypoints")
        self.refused({"route": {"waypoints": ["1,2", "3,4"]}, "location": "1,2"}, "Keep one")
        self.refused({"status_bar": {"wifi_mode": "on"}}, "Use one of searching, failed, active")
        self.refused({"status_bar": {"data_network": "5g-uwc"}}, "Use one of hide")

    def test_a_network_profile_must_be_one_of_the_four(self):
        self.refused({"network": "3g"}, "Use one of offline, slow-3g, lte, flaky")


def fake_simctl(log, fail=None, current=None):
    current = current or {}

    def run(args):
        log.append(("simctl",) + tuple(args))
        if fail and args[0] == fail:
            return 1, "Invalid argument\nsimctl: the thing failed"
        if args[0] == "ui" and len(args) == 3:
            return 0, current.get(args[2], "light")
        return 0, ""
    return run


class SimulatorRunTests(Files, RunnerCase):
    def setUp(self):
        Files.setUp(self)
        RunnerCase.setUp(self)

    def prepared(self, block, manager=None, fail=None, current=None):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "Choose your plan"}],
                                 "conditions": block}, base_dir=self.base)
        manager = manager or Manager()
        log = manager.calls
        driver = Driver([PLANS])
        self.watch(driver, manager)
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager,
                        simctl=fake_simctl(log, fail, current))
        with mock.patch.object(runner, "build_driver", lambda target, m, run_dir, mode_: driver):
            try:
                run.prepare()
            except runner.CouldntRun:
                pass
        return run, manager, log

    def test_a_simulator_gets_every_condition_in_order_and_the_push_after_the_launch(self):
        run, manager, log = self.prepared(FULL, current={"appearance": "light", "content_size": "large"})
        simctl = [call[1:] for call in log if call[0] == "simctl"]
        udid = run.target.udid
        self.assertEqual(simctl[0], ("ui", udid, "appearance"))
        self.assertIn(("ui", udid, "appearance", "dark"), simctl)
        self.assertIn(("ui", udid, "content_size", "accessibility-extra-large"), simctl)
        self.assertIn(("location", udid, "set", "52.52,13.405"), simctl)
        self.assertIn(("privacy", udid, "grant", "photos", BUNDLE), simctl)
        self.assertIn(("privacy", udid, "revoke", "location", BUNDLE), simctl)
        self.assertIn(("addmedia", udid, str(self.base / "fixtures" / "receipt.jpg")), simctl)
        self.assertIn(("status_bar", udid, "override", "--time", "9:41", "--batteryLevel", "100", "--wifiBars", "3"),
                      simctl)
        names = [call[0] for call in log]
        self.assertLess(names.index("launch"), names.index("simctl", names.index("launch")))
        push = next(index for index, call in enumerate(log) if call[:2] == ("simctl", "push"))
        self.assertGreater(push, names.index("launch"))   # the push goes to an app that is already in front
        launch = next(call for call in log if call[0] == "launch")
        self.assertEqual(launch[2], ("-AppleLanguages", "(de)", "-AppleLocale", "de_DE"))
        self.assertEqual(run.finish()["conditions"]["skipped"], [])
        self.assertEqual(set(run.result["conditions"]["applied"]), set(FULL))

    def test_increase_contrast_and_a_route_reach_simctl_and_are_put_back(self):
        run, manager, log = self.prepared({"increase_contrast": "enabled", "route": {"waypoints": ["1,2", "3,4"],
                                                                                    "speed": 5},
                                           "status_bar": {"wifi_mode": "active", "cellular_mode": "notSupported"}},
                                          current={"increase_contrast": "disabled"})
        simctl = [call[1:] for call in log if call[0] == "simctl"]
        udid = run.target.udid
        self.assertIn(("ui", udid, "increase_contrast", "enabled"), simctl)
        self.assertIn(("location", udid, "start", "--speed=5", "1.0,2.0", "3.0,4.0"), simctl)
        self.assertIn(("status_bar", udid, "override", "--wifiMode", "active", "--cellularMode", "notSupported"),
                      simctl)
        before = len(log)
        run.finish()
        undo = [call[1:] for call in log[before:] if call[0] == "simctl"]
        self.assertIn(("ui", udid, "increase_contrast", "disabled"), undo)
        self.assertIn(("location", udid, "clear"), undo)

    def test_the_conditions_are_put_back_while_the_simulator_is_still_held(self):
        run, manager, log = self.prepared({"appearance": "dark", "location": {"latitude": 1, "longitude": 2},
                                           "status_bar": {"time": "9:41"}, "permissions": {"photos": "grant"}},
                                          current={"appearance": "light"})
        before = len(log)
        run.finish()
        undo = [call[1:] for call in log[before:] if call[0] == "simctl"]
        udid = run.target.udid
        self.assertEqual(sorted(undo), sorted([("ui", udid, "appearance", "light"), ("location", udid, "clear"),
                                                ("status_bar", udid, "clear"),
                                                ("privacy", udid, "reset", "photos", BUNDLE)]))
        self.assertEqual(manager.leases[0].released, 1)
        run.finish()
        self.assertEqual(len([call for call in log[before:] if call[0] == "simctl"]), len(undo))  # once

    def test_a_dispatched_condition_with_an_uncertain_outcome_never_launches_or_retries(self):
        run, manager, log = self.prepared({"location": {"latitude": 1, "longitude": 2}}, fail="location")
        result_ = run.finish()
        self.assertEqual(result_["verdict"], "couldnt_run")
        # simctl returned an error after dispatch; the condition's effect cannot be ruled out.
        self.assertEqual(result_["reason"]["class"], "unknown_outcome")
        self.assertNotIn("launch", [call[0] for call in log])
        self.assertEqual(len([call for call in log if call[:2] == ("simctl", "location")]), 1)
        self.assertEqual(result_["conditions"]["cleanup"], "needs_reconciliation")
        actions = [json.loads(line) for line in (run.run_dir / "actions.jsonl").read_text().splitlines()]
        self.assertIn({"operation": "CONDITION_SET", "outcome": "unknown"},
                      [{"operation": row.get("operation"), "outcome": row.get("outcome")}
                       for row in actions if row["event"] == "action_finished"])

    def test_a_check_without_conditions_never_calls_simctl(self):
        plain = check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "x"}]}, base_dir=self.base)
        manager2 = Manager()
        driver = Driver([PLANS])
        self.watch(driver, manager2)
        run2 = VerifyRun(plain, mode="launch", runs_dir=self.runs, manager=manager2,
                         simctl=lambda args: self.fail("simctl called"))
        with mock.patch.object(runner, "build_driver", lambda *a: driver):
            run2.prepare()
        self.assertNotIn("conditions", run2.finish())


class Phone(Manager):
    real_device = True

    def __init__(self):
        super().__init__()
        self.style, self.set_calls = "light", []

    def appearance(self, name=None):
        if name is None:
            return self.style
        self.set_calls.append(name)
        self.style = name


class RealIPhoneTests(Files, RunnerCase):
    def setUp(self):
        Files.setUp(self)
        RunnerCase.setUp(self)

    def test_a_phone_gets_appearance_and_locale_only_and_the_report_lists_the_rest(self):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "Choose your plan"}],
                                 "conditions": FULL}, base_dir=self.base)
        phone, calls = Phone(), []
        driver = Driver([PLANS])
        self.watch(driver, phone)
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=phone,
                        simctl=lambda args: calls.append(args) or (0, ""))
        with mock.patch.object(runner, "build_driver", lambda *a: driver):
            run.prepare()
        report = run.finish()["conditions"]
        self.assertEqual(calls, [])                            # no simctl: nothing but appearance and locale
        self.assertEqual(phone.set_calls, ["dark", "light"])   # set, then put back
        self.assertEqual(report["applied"], ["appearance", "locale"])
        self.assertEqual([item["key"] for item in report["skipped"]],
                         ["content_size", "location", "permissions", "media", "status_bar", "push"])
        self.assertIn("Run the check on a simulator", report["skipped"][0]["why"])
        launch = next(call for call in phone.calls if call[0] == "launch")
        self.assertEqual(launch[2][-2:], ("-AppleLocale", "de_DE"))

    def test_the_suite_lists_the_skipped_keys_on_the_device_result(self):
        skipped = [{"key": "location", "why": "location changes the state of a phone, so Mobster leaves it alone "
                                              "on a real iPhone. Run the check on a simulator to use it."}]

        def attempt(check, record, *, label, video_name=None):
            return {**result("passed"), "conditions": {"applied": ["appearance"], "skipped": skipped}}
        doc = Suite([entry("a")], [T.DEFAULT], attempt=attempt).run()
        cell = doc["checks"][0]["results"][0]
        self.assertEqual(cell["conditions"]["skipped"], skipped)
        self.assertTrue(any(note.startswith("Skipped on this device: location:") for note in cell["notes"]))


class DocsMatchCodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = (Path(__file__).resolve().parents[2] / "docs" / "testing.mdx").read_text(encoding="utf-8")
        cls.section = text.split("## Device conditions", 1)[1].split("\n## ", 1)[0]

    def test_every_key_has_a_row_and_every_row_is_a_key(self):
        rows = set(re.findall(r"^\| `([a-z_]+)` \|", self.section, re.M))
        self.assertEqual(rows, set(conditions.KEYS))

    def test_every_value_the_code_accepts_is_written_down(self):
        words = set(re.findall(r"`([^`\n]+)`", re.sub(r"```.*?```", "", self.section, flags=re.S)))
        for group in (conditions.APPEARANCES, conditions.CONTRASTS, conditions.SERVICES, conditions.ACTIONS,
                      conditions.STATUS_BAR_KEYS, conditions.BATTERY_STATES, conditions.WIFI_MODES,
                      conditions.CELLULAR_MODES, conditions.DATA_NETWORKS, conditions.MEDIA_SUFFIXES,
                      conditions.NETWORKS):
            for value in group:
                self.assertIn(value, words, value)
        self.assertIn(f"from `{conditions.CONTENT_SIZES[0]}` to `{conditions.CONTENT_SIZES[-1]}`", self.section)


EVIL = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def result_with(run_dir, **changes):
    frames = run_dir / "frames"
    frames.mkdir(exist_ok=True)
    for name in ("01-launch.jpg", "02-step.jpg", "03-verdict.jpg", "04-verdict-ax.jpg"):
        Image.new("RGB", (402, 874), (200, 200, 210)).save(frames / name, "JPEG")
    result = {
        "schema": "mobster.verify/1", "run_id": "20261001-101500-3f9a", "verdict": "failed", "exit_code": 1,
        "summary": f"text \"{EVIL}\" failed: not on screen", "reason": {"class": "assertion",
                                                                       "message": "1 of 2 expectations failed",
                                                                       "fix": None},
        "mode": "smart",
        "check": {"name": f"Paywall {EVIL}", "steps": ["Open it"], "expect": [{"text": EVIL}], "source": None},
        "app": {"bundle_id": "org.mozilla.ios.Firefox", "name": f"Day{EVIL}", "version": "1.0 (1)", "path": None},
        "device": {"name": "Mobster · iPhone 17 Pro · iOS 26.4", "udid": "U", "type": "iPhone 17 Pro",
                   "runtime": "iOS 26.4"},
        "assertions": [{"index": 0, "assertion": {"text": EVIL}, "text": f'text "{EVIL}"', "ok": False,
                        "observed": f'not on screen; closest: "{EVIL}"', "matches": [],
                        "frame": "frames/03-verdict.jpg"},
                       {"index": 1, "assertion": {"text": "Plans"}, "text": 'text "Plans"', "ok": True,
                        "observed": 'found in text "Plans"', "matches": [], "frame": "frames/03-verdict.jpg"}],
        "baseline": {"taken": True, "held": [False, False]}, "stable": True,
        "steps": [{"index": 1, "op": "TAP", "text": f"Tapped {EVIL}", "target": {"id": None, "label": EVIL,
                                                                                 "role": None},
                   "typed": None, "changed": True, "frame": "frames/02-step.jpg", "at_ms": 3120}],
        "agent": {"status": "completed", "note": f"I tapped {EVIL}", "model": "gpt-5.6-sol", "turns": 4},
        "frames": ["frames/01-launch.jpg", "frames/02-step.jpg", "frames/03-verdict.jpg", "frames/04-verdict-ax.jpg"],
        "proof": [], "alert": None, "report": str(run_dir / "report.html"), "draft_check": str(run_dir / "check.yaml"),
        "run_dir": str(run_dir), "repro": f"mobster verify --check {run_dir / 'check.yaml'}", "seconds": 12.4,
        "timing": {"simulator": 0.4, "install": 2.1, "launch": 1.3, "flow": 6.8, "assert": 1.2}, "cost_usd": 0.031,
        "mobster": {"version": "0.2.0"}}
    result.update(changes)
    return result


class ReportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.run_dir = Path(folder.name)

    def test_every_app_and_model_string_is_escaped(self):
        html = render_html(result_with(self.run_dir), self.run_dir)
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("onerror=alert", html.replace("onerror=alert(1)&gt;", ""))
        self.assertNotIn(EVIL, html)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", html)

    def test_the_apps_own_icon_beside_its_name_when_the_run_kept_one(self):
        self.assertNotIn('class="app-icon"', render_html(result_with(self.run_dir), self.run_dir))
        out = io.BytesIO()
        Image.new("RGB", (120, 120), (0, 0, 255)).save(out, "PNG")
        (self.run_dir / "app-icon.png").write_bytes(out.getvalue())
        html = render_html(result_with(self.run_dir), self.run_dir)
        self.assertIn('<img class="app-icon" src="data:image/png;base64,', html)
        (self.run_dir / "app-icon.png").write_bytes(b"<svg onload=alert(1)>")
        self.assertNotIn('class="app-icon"', render_html(result_with(self.run_dir), self.run_dir))

    def test_the_csp_and_no_external_urls(self):
        html = render_html(result_with(self.run_dir), self.run_dir)
        self.assertIn(f'<meta http-equiv="Content-Security-Policy" content="{CSP}">', html)
        self.assertEqual(CSP, "default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'")
        for attribute in re.findall(r'(?:src|href)="([^"]*)"', html):
            self.assertTrue(attribute.startswith("data:image/jpeg;base64,"), attribute[:40])
        self.assertIsNone(re.search(r"<(link|script|iframe|object|a)\b", html))
        self.assertEqual(re.findall(r"url\((data:[^;]*);", html), ["data:font/woff2"] * 2)

    def test_the_style_names_tokens_and_carries_the_brand(self):
        from mobile_agent import brand
        from mobile_agent.testkit.reports import SUITE_STYLE
        from mobile_agent.verify.report import COMPONENTS, VERIFY_STYLE
        for name, css in (("COMPONENTS", COMPONENTS), ("VERIFY_STYLE", VERIFY_STYLE), ("SUITE_STYLE", SUITE_STYLE)):
            with self.subTest(name):
                self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css), [])
                self.assertEqual(sorted(set(re.findall(r"font-weight: (\d+)", css))), [])
        html = render_html(result_with(self.run_dir), self.run_dir)
        self.assertIn(brand.STYLESHEET, html)
        self.assertIn('font-family: "Mobster Sans"', html)
        self.assertIn('font-family: "Mobster Code"', html)
        self.assertIn('<svg class="mark-light"', html)
        self.assertIn('<svg class="mark-dark"', html)
        self.assertNotIn("·", re.sub(r"<style>.*?</style>", "", html, flags=re.S).replace(EVIL, ""))

    def test_the_contents_in_order(self):
        html = render_html(result_with(self.run_dir), self.run_dir)
        order = ['class="verdict fail"', ">Expectations<", ">The verdict frame<", ">Steps<", ">Run<",
                 ">Run it again<", ">The model's note (not the verdict)<"]
        positions = [html.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("prefers-color-scheme: dark", html)
        self.assertIn("@media print", html)
        self.assertIn("$0.0310", html)
        self.assertIn("1 of 2 expectations failed", html)
        self.assertEqual(html.count("<img"), 4)  # overlay, plain verdict, launch and one step thumbnail
        self.assertIn('alt="The screen at the verdict"', html)

    def test_the_numbers_beside_the_frame_match_its_pills(self):
        result = result_with(self.run_dir)
        result["assertions"][1]["matches"] = [{"id": "plans", "label": "Plans", "value": None, "role": "StaticText",
                                               "rect": [24, 118, 354, 34]}]
        html = render_html(result, self.run_dir)
        self.assertIn('class="pill fail" role="img" aria-label="1, failed, not outlined on the frame"', html)
        self.assertIn('class="pill pass" role="img" aria-label="2, held"', html)
        self.assertLess(html.index('aria-label="1, failed'), html.index('aria-label="2, held'))
        self.assertIn("found in text &quot;Plans&quot;", html)

    def test_pills_go_to_failures_first_and_at_most_a_few(self):
        items = [(True, True)] * 8 + [(False, True), (False, False)]
        chosen = pill_numbers(items)
        self.assertEqual(len(chosen), MAX_PILLS)
        self.assertIn(8, chosen)                      # the failure with an element
        self.assertNotIn(9, chosen)                   # nothing on screen to point at
        self.assertEqual(chosen - {8}, set(range(MAX_PILLS - 1)))

    def test_verdict_chips(self):
        for verdict, tone, word in (("passed", "pass", "Passed"), ("failed", "fail", "Failed"),
                                    ("needs_review", "review", "Needs review"),
                                    ("couldnt_run", "none", "Couldn&#x27;t run")):
            with self.subTest(verdict=verdict):
                html = render_html(result_with(self.run_dir, verdict=verdict), self.run_dir)
                self.assertRegex(html, f'<span class="verdict {tone}"><svg[^>]*>.*?</svg>{word}</span>')
        self.assertIn("<title>Couldn&#x27;t run: Paywall", render_html(result_with(self.run_dir, verdict="couldnt_run"),
                                                                       self.run_dir))

    def test_a_run_that_couldnt_start(self):
        result = result_with(self.run_dir, verdict="couldnt_run", assertions=[], steps=[], frames=[], agent=None,
                             reason={"class": "busy", "message": "Every simulator is in use.", "fix": "Wait."})
        path = write_report(self.run_dir, result)
        html = path.read_text()
        self.assertIn("Every simulator is in use.", html)
        self.assertIn("Not evaluated", html)
        self.assertNotIn("<img", html)
        self.assertNotIn(">Steps<", html)  # no steps taken: no Steps section

    def test_paths_under_home_read_as_tilde(self):
        home = str(Path.home())
        result = result_with(self.run_dir, repro=f"mobster verify --check {home}/app/.mobster/runs/x/check.yaml",
                             app={"bundle_id": "org.mozilla.ios.Firefox", "name": "Firefox", "version": "1.0 (1)",
                                  "path": f"{home}/app/Firefox.app"})
        html = render_html(result, self.run_dir)
        self.assertNotIn(home + "/", html)
        self.assertIn("~/app/Firefox.app", html)
        self.assertIn("~/app/.mobster/runs/x/check.yaml", html)


class OverlayTests(unittest.TestCase):
    def test_shown_nodes_thin_and_asserted_nodes_green_or_red(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            frame = folder / "verdict.jpg"
            Image.new("RGB", (603, 1311), (255, 255, 255)).save(frame, "JPEG", quality=95)
            tree = parse_tree(paywall(plans=("Monthly", "Annual")))
            assertions = [parse_assertion({"text": "Choose your plan"}),
                          parse_assertion({"count": {"id": "/^plan_/"}, "equals": 3}),
                          parse_assertion({"text": "Missing"})]
            results = evaluate_on(assertions, tree)
            out = draw_overlay(frame, tree, [(r, list(r.paths)) for r in results], folder / "ax.jpg")
            with Image.open(out) as image:
                self.assertEqual(image.size, (603, 1311))
                scale = 603 / 402

                def edge(y):  # the strongest colour just around an element's left edge (x = 24 pt)
                    return [image.getpixel((x, y)) for x in range(round(24 * scale) - 12, round(24 * scale) + 4)]

                self.assertTrue(any(g > r + 40 for r, g, b in edge(round((118 + 17) * scale))))
                for top in (180, 270):
                    self.assertTrue(any(r > g + 40 for r, g, b in edge(round((top + 40) * scale))))
                self.assertGreater(sum(image.getpixel((301, 1290))), 740)
                self.assertGreater(min(image.getpixel((301, round(225 * scale)))), 200)

    def test_every_shown_node_is_outlined_faintly(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            frame = folder / "verdict.jpg"
            Image.new("RGB", (402, 874), (255, 255, 255)).save(frame, "JPEG", quality=95)
            tree = parse_tree(app(node("Button", "Alone", rect=(100, 300, 200, 60))))
            out = draw_overlay(frame, tree, [], folder / "ax.jpg")
            with Image.open(out) as image:
                red, green, blue = image.getpixel((200, 300))
                self.assertLess(red, 245)            # a line is drawn
                self.assertGreater(blue, red)         # in violet
                self.assertGreater(red, 120)          # at 35% opacity, not solid


GOOD_REASON = "Firefox uses the camera to scan the barcode on your receipt."


def make_app(root, info=None, binary=b"", manifest=None, frameworks=None, name="Firefox.app"):
    folder = Path(root) / name
    folder.mkdir(parents=True)
    plist = {"CFBundleIdentifier": "org.mozilla.ios.Firefox", "CFBundleExecutable": "Firefox", **(info or {})}
    (folder / "Info.plist").write_bytes(plistlib.dumps(plist))
    (folder / "Firefox").write_bytes(b"\xcf\xfa\xed\xfe\0" + b"\0".join(binary.split()) + b"\0")
    if manifest is not None:
        (folder / "PrivacyInfo.xcprivacy").write_bytes(plistlib.dumps(manifest))
    for fname, (fbin, fmanifest) in (frameworks or {}).items():
        framework = folder / "Frameworks" / f"{fname}.framework"
        framework.mkdir(parents=True)
        (framework / fname).write_bytes(b"\xcf\xfa\xed\xfe\0" + b"\0".join(fbin.split()) + b"\0")
        if fmanifest is not None:
            (framework / "PrivacyInfo.xcprivacy").write_bytes(plistlib.dumps(fmanifest))
    return folder


def declares(*categories):
    return {"NSPrivacyAccessedAPITypes": [{"NSPrivacyAccessedAPIType": c, "NSPrivacyAccessedAPITypeReasons": ["CA92.1"]}
                                          for c in categories]}


class AuditTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = folder.name

    def test_a_missing_usage_description_fails_with_the_guideline_and_the_file(self):
        built = make_app(self.root, binary=b"AVCaptureSession CLLocationManager")
        found = audit.run_audit("usage-descriptions", built)
        self.assertEqual({f.id for f in found}, {"usage-description-missing"})
        self.assertEqual({f.level for f in found}, {"fail"})
        self.assertEqual({f.guideline for f in found}, {"5.1.1"})
        camera = next(f for f in found if "NSCameraUsageDescription" in f.message)
        self.assertIn("Firefox.app/Info.plist has no NSCameraUsageDescription", camera.evidence)
        self.assertIn("AVCaptureSession is in Firefox", camera.evidence)

    def test_a_real_reason_passes_and_filler_fails(self):
        good = make_app(self.root, info={"NSCameraUsageDescription": GOOD_REASON}, binary=b"AVCaptureSession")
        self.assertEqual(audit.run_audit("usage-descriptions", good), [])
        for text in ("", "TODO", "Lorem ipsum dolor sit amet", "Camera", "test", "Privacy - Camera Usage Description"):
            built = make_app(self.root, info={"NSCameraUsageDescription": text}, binary=b"AVCaptureSession",
                             name=f"A{abs(hash(text))}.app")
            found = audit.run_audit("usage-descriptions", built)
            self.assertEqual([f.id for f in found], ["usage-description-placeholder"], text)

    def test_a_broader_location_key_satisfies_the_requirement(self):
        built = make_app(self.root, info={"NSLocationAlwaysAndWhenInUseUsageDescription":
                                          "Firefox shows the sunrise where you are."}, binary=b"CLLocationManager")
        self.assertEqual(audit.run_audit("usage-descriptions", built), [])

    def test_a_framework_that_reads_contacts_needs_the_key_too(self):
        built = make_app(self.root, frameworks={"Sdk": (b"CNContactStore", None)})
        found = audit.run_audit("usage-descriptions", built)
        self.assertIn("Frameworks/Sdk.framework/Sdk", found[0].message)

    def test_a_manifest_that_declares_what_the_binary_uses_passes(self):
        built = make_app(self.root, binary=b"NSUserDefaults mach_absolute_time",
                         manifest=declares("NSPrivacyAccessedAPICategoryUserDefaults",
                                           "NSPrivacyAccessedAPICategorySystemBootTime"))
        self.assertEqual(audit.run_audit("privacy-manifest", built), [])

    def test_a_missing_manifest_fails_when_the_binary_uses_a_required_reason_api(self):
        built = make_app(self.root, binary=b"NSUserDefaults")
        (found,) = audit.run_audit("privacy-manifest", built)
        self.assertEqual((found.id, found.level), ("privacy-manifest-missing", "fail"))
        self.assertIn("NSPrivacyAccessedAPICategoryUserDefaults", found.evidence)

    def test_a_missing_manifest_with_no_such_api_is_for_a_person(self):
        (found,) = audit.run_audit("privacy-manifest", make_app(self.root, binary=b"nothing"))
        self.assertEqual(found.level, "review")

    def test_an_undeclared_category_names_the_symbol_and_the_file(self):
        built = make_app(self.root, binary=b"NSFileModificationDate NSUserDefaults",
                         manifest=declares("NSPrivacyAccessedAPICategoryUserDefaults"))
        (found,) = audit.run_audit("privacy-manifest", built)
        self.assertEqual((found.id, found.level), ("required-reason-undeclared", "fail"))
        self.assertIn("NSFileModificationDate is in Firefox", found.evidence)

    def test_a_declared_category_without_a_reason_fails(self):
        manifest = {"NSPrivacyAccessedAPITypes": [{"NSPrivacyAccessedAPIType":
                                                   "NSPrivacyAccessedAPICategoryUserDefaults"}]}
        built = make_app(self.root, binary=b"NSUserDefaults", manifest=manifest)
        self.assertEqual([f.id for f in audit.run_audit("privacy-manifest", built)], ["privacy-reason-missing"])

    def test_an_sdk_without_its_own_manifest_is_for_a_person_and_one_with_it_passes(self):
        bare = make_app(self.root, manifest={}, frameworks={"Sdk": (b"NSUserDefaults", None)})
        (found,) = audit.run_audit("privacy-manifest", bare)
        self.assertEqual((found.id, found.level), ("required-reason-sdk", "review"))
        covered = make_app(self.root, manifest={}, name="B.app", frameworks={
            "Sdk": (b"NSUserDefaults", declares("NSPrivacyAccessedAPICategoryUserDefaults"))})
        self.assertEqual(audit.run_audit("privacy-manifest", covered), [])

    def test_an_invalid_manifest_fails(self):
        built = make_app(self.root, binary=b"x")
        (built / "PrivacyInfo.xcprivacy").write_text("not a plist")
        self.assertEqual([f.id for f in audit.run_audit("privacy-manifest", built)], ["privacy-manifest-invalid"])

    def test_a_missing_bundle_is_one_review_finding(self):
        (found,) = audit.run_audit("usage-descriptions", Path(self.root) / "Nope.app")
        self.assertEqual(found.level, "review")


LOREM = app(node("StaticText", "Lorem ipsum dolor sit amet"))


RESTORE = app(node("Button", "Restore Purchases"))


class RunnerTests(RunnerCase):
    def run_check(self, driver, **data):
        data.setdefault("steps", ["Open About"])
        check = check_from_dict({"version": 1, "app": {"bundle": BUNDLE}, **data})
        return self.verify(check, driver, key=KEY)[0]

    def test_placeholder_text_on_a_visited_screen_fails_with_that_frame(self):
        check = {"throughout": [{"no_text": "/lorem ipsum/i", "name": "2.1 no placeholder text"}]}
        driver = Driver([PLANS, LOREM, PLANS], after_flow=[PLANS, LOREM, PLANS])
        result = self.run_check(driver, **check)
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "finding"))
        (finding,) = result["findings"]
        self.assertIn("2.1 no placeholder text", finding["message"])
        self.assertTrue(finding["frame"].startswith("frames/"))

    def test_clean_screens_pass_with_no_expect(self):
        result = self.run_check(Driver([PLANS], after_flow=[PLANS]),
                                throughout=[{"no_text": "/lorem ipsum/i"}])
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["findings"], [])

    def test_somewhere_held_on_one_screen_passes_and_a_miss_needs_a_person(self):
        wanted = {"somewhere": [{"visible": {"label": "Restore Purchases"}, "name": "3.1.1 restore"}]}
        plain = app(node("StaticText", "Welcome"))
        held = self.run_check(Driver([plain, RESTORE, plain], after_flow=[plain, RESTORE, plain]), **wanted)
        self.assertEqual(held["verdict"], "passed")
        missed = self.run_check(Driver([plain], after_flow=[plain]), **wanted)
        self.assertEqual((missed["verdict"], missed["reason"]["class"]), ("needs_review", "finding"))
        self.assertIn("3.1.1 restore", missed["findings"][0]["message"])

    def test_an_audit_finding_fails_and_a_review_finding_needs_a_person(self):
        with tempfile.TemporaryDirectory() as root:
            bad = make_app(root, binary=b"AVCaptureSession")
            check = check_from_dict({"version": 1, "app": {"bundle": "org.mozilla.ios.Firefox", "path": str(bad)},
                                     "steps": ["x"], "audit": ["usage-descriptions"]})
            result = self.verify(check, Driver([PLANS], after_flow=[PLANS]), key=KEY)[0]
            self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "finding"))
            self.assertEqual(result["findings"][0]["guideline"], "5.1.1")
            self.assertIn("Info.plist", result["findings"][0]["evidence"])

            ok = make_app(root, info={"NSCameraUsageDescription": GOOD_REASON}, binary=b"AVCaptureSession",
                          name="Ok.app", manifest={})
            check = check_from_dict({"version": 1, "app": {"bundle": "org.mozilla.ios.Firefox", "path": str(ok)},
                                     "steps": ["x"], "audit": ["usage-descriptions", "privacy-manifest"]})
            self.assertEqual(self.verify(check, Driver([PLANS], after_flow=[PLANS]), key=KEY)[0]["verdict"], "passed")

            maybe = make_app(root, name="Maybe.app")
            check = check_from_dict({"version": 1, "app": {"bundle": "org.mozilla.ios.Firefox", "path": str(maybe)},
                                     "steps": ["x"], "audit": ["privacy-manifest"]})
            result = self.verify(check, Driver([PLANS], after_flow=[PLANS]), key=KEY)[0]
            self.assertEqual(result["verdict"], "needs_review")


if __name__ == "__main__":
    unittest.main()
