import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import attach
from mobile_agent.attach import flutter, score
from mobile_agent.tests.test_verify_fixtures import app, node
from mobile_agent.verify.tree import parse_tree


def shop_tree():
    return parse_tree(app(
        node("NavigationBar", "Cart", rect=(0, 50, 402, 44)),
        node("WebView", "Cart", rect=(0, 100, 400, 700), children=[node("Other", "stale", rect=(0, 110, 100, 20))]),
        node("Button", "Back", rect=(10, 810, 80, 40)), bundle="dev.shop"))


def flutter_tree(marked=False):
    rows = [node("Other", "Sign in", id="btn_sign_in", rect=(20, 600, 360, 48)),
            node("Other", "Email", id="field_email", rect=(20, 200, 360, 48)),
            node("Other", "Password", id="field_password", rect=(20, 260, 360, 48)),
            node("Other", "Forgot password?", rect=(20, 320, 200, 30)),
            node("StaticText", "Welcome back", rect=(20, 120, 300, 40)),
            node("Other", "Create account", id="btn_create", rect=(20, 660, 360, 48)),
            node("Other", "Terms", rect=(20, 720, 100, 30), enabled=False)]
    if marked:
        rows.insert(0, node("Other", "", id="FlutterSemanticsScrollView", rect=(0, 0, 402, 874)))
    return parse_tree(app(*rows, bundle="dev.flutter.demo"))


class FlutterTests(unittest.TestCase):
    def test_recognises_the_shape_with_and_without_engine_marks(self):
        self.assertTrue(flutter.is_flutter(flutter_tree()))
        self.assertTrue(flutter.is_flutter(flutter_tree(marked=True)))

    def test_native_trees_are_not_flutter(self):
        native = parse_tree(app(*[node("Button", f"B{i}") for i in range(8)]))
        self.assertFalse(flutter.is_flutter(native))
        self.assertFalse(flutter.is_flutter(parse_tree(app(node("Other", "A"), node("Other", "B")))))


class ScoreTests(unittest.TestCase):
    def test_native_app_with_labelled_controls_is_good(self):
        tree = parse_tree(app(*[node("Button", f"Item {i}", rect=(0, 60 * i + 60, 300, 44)) for i in range(10)]))
        got = score.score_tree(tree, now=1)
        self.assertEqual((got.rating, got.labelled_share, got.actionable), ("good", 1.0, 10))

    def test_unlabelled_controls_are_poor_and_partial_in_between(self):
        def tree(labelled, blank):
            return parse_tree(app(*[node("Button", f"L{i}", rect=(0, 50 * i + 50, 300, 40)) for i in range(labelled)],
                                  *[node("Button", None, id=None, rect=(0, 50 * (labelled + i) + 50, 300, 40))
                                    for i in range(blank)]))
        self.assertEqual(score.score_tree(tree(3, 7)).rating, "poor")
        self.assertEqual(score.score_tree(tree(7, 3)).rating, "partial")

    def test_webview_flutter_and_drawn_screens_say_why(self):
        web_score = score.score_tree(shop_tree())
        self.assertTrue(web_score.webview)
        self.assertEqual(web_score.rating, "partial")
        self.assertTrue(any("WebView" in r for r in web_score.reasons))
        self.assertTrue(score.score_tree(flutter_tree()).flutter)
        drawn = score.score_tree(parse_tree(app(node("Other", "Game"))))
        self.assertEqual(drawn.rating, "poor")
        self.assertIn("drawn", drawn.reasons[0])

    def test_webview_score_counts_only_elements_inside_the_largest_view(self):
        tree = parse_tree(app(
            node("WebView", "Small", rect=(0, 0, 100, 50)),
            node("WebView", "Large", rect=(0, 100, 400, 600), children=[
                node("Button", "Inside", rect=(20, 120, 100, 40))]),
            node("Button", "Outside", rect=(20, 800, 100, 40))))
        got = score.score_tree(tree)
        self.assertTrue(got.webview)
        self.assertIn("a WebView holds the content (1 accessibility elements inside it); "
                      "only its published accessibility elements are scored", got.reasons)

    def test_one_unreadable_app_does_not_stop_the_survey(self):
        trees = {"a": parse_tree(app(*[node("Button", f"B{i}", rect=(0, 50 * i + 50, 300, 40)) for i in range(9)]))}
        current = []

        def read():
            return trees[current[-1]]
        got = score.score_apps(read, current.append, ["a", "b"])
        self.assertEqual([s.rating for s in got], ["good", "poor"])
        self.assertIn("KeyError", got[1].reasons[0])

    def test_store_merges_by_bundle_and_survives_a_bad_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "attach" / "scores.json"
            self.assertEqual(score.load(path), [])
            one = score.score_tree(flutter_tree(), now=1)
            score.save([one], path)
            score.save([score.score_tree(shop_tree(), now=2)], path)
            score.save([score.score_tree(flutter_tree(), now=3)], path)
            rows = score.load(path)
            self.assertEqual([r["bundle_id"] for r in rows], ["dev.flutter.demo", "dev.shop"])
            self.assertEqual(rows[0]["scored_at"], 3)
            path.write_text("{not json")
            self.assertEqual(score.load(path), [])
            self.assertIn("dev.shop", score.report([score.score_tree(shop_tree())]))


class FlagTests(unittest.TestCase):
    def test_dark_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(attach.ENV_FLAG, None)
            self.assertFalse(attach.enabled())
        with mock.patch.dict(os.environ, {attach.ENV_FLAG: "1"}):
            self.assertTrue(attach.enabled())

    def test_doctor_apps_is_hidden_and_refuses_without_the_flag(self):
        from mobile_agent import cli_parser
        from mobile_agent.attach import cli
        parser = cli_parser.build_parser()
        self.assertNotIn("--apps", parser.commands["doctor"].format_help())
        args = parser.parse_args(["doctor", "--apps"])
        self.assertTrue(args.apps)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(attach.ENV_FLAG, None)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.run(args, "http://127.0.0.1:8100"), 2)


if __name__ == "__main__":
    unittest.main()
