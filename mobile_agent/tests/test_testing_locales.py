"""`mobster test --locale de,ja,ar`: the flag's parsing, a check run once per language with the language in its
conditions, each rule of the locale audit on recorded trees (where it acts and where it stays quiet), a run that
audits the screens it reads with no model, and the per-language results in results.json, JUnit and the history.
Offline: simulators and fake attempts only."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from mobile_agent.testkit import history
from mobile_agent.testkit import reports as R
from mobile_agent.testkit import targets as T
from mobile_agent.testkit.api import Options, UsageError, parse_locales, plan
from mobile_agent.testkit.discover import in_locales
from mobile_agent.testkit.suite import Suite
from mobile_agent.tests.test_testing import Script, validate_junit
from mobile_agent.tests.test_testing import suite_entry as entry
from mobile_agent.tests.test_verify_fixtures import app, node
from mobile_agent.tests.test_verify_runner import BUNDLE, Driver, Manager, RunnerCase
from mobile_agent.verify import l10n, runner
from mobile_agent.verify.checks import check_from_dict
from mobile_agent.verify.runner import VerifyRun
from mobile_agent.verify.tree import parse_tree


def tree(*children):
    return parse_tree(app(node("Other", children=list(children), rect=(0, 0, 402, 874)), bundle=BUNDLE))


def rules(findings):
    return sorted((item["id"], item["element"]["label"]) for item in findings)


class FlagTests(unittest.TestCase):
    def test_languages_are_split_normalised_and_listed_once(self):
        self.assertEqual(parse_locales(["de,ja_JP", "ar", "de", " pt-BR "]), ("de", "ja_JP", "ar", "pt_BR"))
        self.assertEqual(parse_locales(None), ())

    def test_a_word_that_is_not_a_locale_and_too_many_are_usage_errors(self):
        for bad in (["klingon!"], ["de,1"], ["../x"]):
            with self.assertRaises(UsageError) as caught:
                parse_locales(bad)
            self.assertIn("such as de", str(caught.exception))
        with self.assertRaises(UsageError):
            parse_locales([",".join(f"a{c}" for c in "bcdefghijklmn")])


class ExpansionTests(unittest.TestCase):
    def test_each_check_runs_once_per_language_with_the_language_set_and_the_audit_on(self):
        out = in_locales([entry("home", quarantined="flaky"), entry("broken", error="bad yaml")], ("de", "ar"))
        self.assertEqual([item.name for item in out], ["home [de]", "home [ar]", "broken"])
        german = out[0]
        self.assertEqual((german.locale, german.classname, german.file), ("de", "checks.home.de",
                                                                          ".mobster/checks/home.yaml"))
        self.assertEqual((german.check.conditions["locale"], german.check.l10n), ("de", True))
        self.assertEqual(german.quarantined, "flaky")
        self.assertIsNone(out[2].locale)

    def test_a_checks_own_conditions_stay_and_only_the_language_changes(self):
        base = entry("home")
        base.check = check_from_dict({"version": 1, "name": "home", "app": {"bundle": BUNDLE},
                                      "expect": [{"text": "Hi"}],
                                      "conditions": {"appearance": "dark", "locale": "en_US"}})
        out = in_locales([base], ("ja",))
        self.assertEqual(out[0].check.conditions, {"appearance": "dark", "locale": "ja"})
        self.assertEqual(base.check.conditions["locale"], "en_US")

    def test_plan_expands_after_the_tags_and_the_shard_choose(self):
        with tempfile.TemporaryDirectory() as tmp:
            checks = Path(tmp) / ".mobster" / "checks"
            checks.mkdir(parents=True)
            for name, tags in (("a", "[smoke]"), ("b", "[]")):
                (checks / f"{name}.yaml").write_text(
                    f"version: 1\nname: {name}\napp: {{bundle: org.mozilla.ios.Firefox}}\ntags: {tags}\n"
                    "expect: [{text: Hi}]\n")
            planned = plan(Options(tags=("smoke",), locales=("de", "ja")), cwd=tmp)
        self.assertEqual([item.name for item in planned.entries], ["a [de]", "a [ja]"])
        self.assertEqual(planned.options.public()["locales"], ["de", "ja"])


class RuleTests(unittest.TestCase):
    def test_truncated_text_is_an_ellipsis_or_a_frame_that_leaves_the_screen(self):
        found = l10n.audit_tree(tree(
            node("StaticText", "Benachrichtigungseinstell…", rect=(16, 100, 300, 20)),
            node("Button", "Weiter...", rect=(16, 200, 120, 44)),
            node("StaticText", "Kurz", rect=(16, 300, 100, 20)),
            node("StaticText", "Datenschutzerklärung und Nutzungsbedingungen", rect=(200, 400, 400, 20))), "de")
        self.assertEqual([item["id"] for item in found], ["text-truncated"] * 3)
        self.assertEqual({item["element"]["label"] for item in found},
                         {"Benachrichtigungseinstell…", "Weiter...", "Datenschutzerklärung und Nutzungsbedingungen"})
        self.assertTrue(all(item["level"] == "warn" and item["audit"] == "l10n" for item in found))

    def test_english_in_a_non_latin_language_is_untranslated_and_in_german_it_is_not_judged(self):
        screen = tree(node("StaticText", "Welcome back", rect=(16, 100, 200, 20)),
                      node("StaticText", "ようこそ", rect=(16, 140, 200, 20)),
                      node("StaticText", "3 items", rect=(16, 180, 200, 20)),
                      node("StaticText", "OK", rect=(16, 220, 200, 20)))
        self.assertEqual(rules(l10n.audit_tree(screen, "ja")), [("untranslated-string", "Welcome back")])
        self.assertEqual(rules(l10n.audit_tree(screen, "ar_SA")), [("untranslated-string", "Welcome back")])
        self.assertEqual(l10n.audit_tree(screen, "de"), [])

    def test_a_right_to_left_layout_that_did_not_mirror_is_found_and_a_mirrored_one_is_quiet(self):
        def screen(back_x, text_x):
            return tree(node("NavigationBar", "Settings", rect=(0, 50, 402, 44), children=[
                node("Button", "Back", rect=(back_x, 50, 60, 44))]),
                node("Other", rect=(0, 120, 402, 60), children=[
                    node("StaticText", "الإعدادات", rect=(text_x, 130, 120, 30))]))
        flipped = l10n.audit_tree(screen(330, 270), "ar")
        self.assertEqual(flipped, [])
        stuck = l10n.audit_tree(screen(10, 16), "ar")
        self.assertEqual([item["id"] for item in stuck], ["rtl-not-mirrored", "rtl-not-mirrored"])
        self.assertEqual(l10n.audit_tree(screen(10, 16), "de"), [])  # left to right: left is right

    def test_the_collector_reports_an_element_on_a_screen_once(self):
        collector = l10n.Collector("ja")
        screen = tree(node("StaticText", "Welcome back", rect=(16, 100, 200, 20)))
        collector.add(screen, "frames/01.jpg")
        collector.add(screen, "frames/02.jpg")
        collector.add(tree(node("StaticText", "Welcome back", rect=(16, 100, 200, 20)),
                           node("Button", "Sign in", rect=(16, 160, 100, 44))), "frames/03.jpg")
        self.assertEqual(rules(collector.findings()), [("untranslated-string", "Sign in"),
                                                       ("untranslated-string", "Welcome back")])

    def test_language_helpers(self):
        self.assertTrue(l10n.is_rtl("ar_SA") and l10n.is_rtl("he") and not l10n.is_rtl("de_DE"))


class RunTests(RunnerCase):
    def run_check(self, screen, locale="ja", on=True):
        check = check_from_dict({"app": {"bundle": BUNDLE}, "expect": [{"text": "Account"}],
                                 "conditions": {"locale": locale}})
        check = replace(check, l10n=on)
        manager, driver = Manager(), Driver([screen])
        self.watch(driver, manager)
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager, simctl=lambda args: (0, ""))
        with mock.patch.object(runner, "build_driver", lambda *a: driver):
            run.prepare()
        return run.finish()

    def test_a_run_in_a_language_audits_its_screens_with_no_model_and_still_passes(self):
        screen = app(node("StaticText", "Account", rect=(16, 100, 200, 20)),
                     node("StaticText", "Sign in to continue", rect=(16, 140, 300, 20)), bundle=BUNDLE)
        outcome = self.run_check(screen)
        self.assertEqual(outcome["verdict"], "passed")
        self.assertEqual(outcome["cost_usd"], 0)
        self.assertEqual([item["id"] for item in outcome["findings"]], ["untranslated-string"] * 2)
        self.assertTrue(all(item["level"] == "warn" for item in outcome["findings"]))
        self.assertEqual(self.run_check(screen, on=False)["findings"], [])

    def test_the_launch_arguments_carry_the_language_and_region(self):
        from mobile_agent.verify.conditions import Applier
        applier = Applier({"locale": "pt_BR"}, udid="U", bundle=BUNDLE)
        self.assertEqual(applier.launch_args(), ("-AppleLanguages", "(pt)", "-AppleLocale", "pt_BR"))


class ResultTests(unittest.TestCase):
    def suite(self, script=None):
        entries = in_locales([entry("home")], ("de", "ja"))
        return Suite(entries, [T.simulator("iPhone 17 Pro")], attempt=Script(script or {}), cap=8, retries=0)

    def test_each_language_is_its_own_check_in_results_junit_and_history(self):
        results = self.suite({"home [ja]": ["failed"]}).run()
        self.assertEqual([(c["name"], c["locale"], c["results"][0]["status"]) for c in results["checks"]],
                         [("home [de]", "de", "passed"), ("home [ja]", "ja", "failed")])
        doc = R.document(results, suite_id="s1", options={}, version="9")
        xml = R.junit(doc).decode()
        validate_junit(R.junit(doc))
        self.assertIn('name="home [de]"', xml)
        self.assertIn('classname="checks.home.ja"', xml)
        with tempfile.TemporaryDirectory() as tmp:
            history.record(Path(tmp), {**doc, "suite": "s1"})
            found, ok = history.load(Path(tmp))
        self.assertTrue(ok)
        self.assertEqual(len(found), 2)  # one history per language, not one for "home"
        self.assertEqual(sorted(entry_["name"] for entry_ in found.values()), ["home [de]", "home [ja]"])

    def test_a_finding_becomes_its_own_junit_case_named_for_the_audit(self):
        results = self.suite().run()
        finding = {"id": "untranslated-string", "audit": "l10n", "guideline": "g", "level": "warn", "frame": None,
                   "message": "m", "element": {"role": "StaticText", "label": "Welcome", "identifier": "",
                                               "rect": [1, 2, 3, 4]}}
        results["checks"][1]["results"][0]["findings"] = [finding]
        xml = R.junit(R.document(results, suite_id="s1", options={}, version="9")).decode()
        self.assertIn("checks.home.ja.l10n", xml)
        self.assertIn('name="audit" value="l10n"', xml)


if __name__ == "__main__":
    unittest.main()
