import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import attach
from mobile_agent.attach import flutter, score, web
from mobile_agent.tests.test_verify_fixtures import app, node
from mobile_agent.verify.tree import parse_tree

WEB_DOM = {"width": 400, "height": 700, "url": "https://example.test/cart", "nodes": [
    {"selector": "#checkout", "tag": "button", "role": "", "type": "submit", "text": "Checkout",
     "rect": [20, 100, 200, 40]},
    {"selector": "[data-testid=\"email\"]", "tag": "input", "type": "email", "text": "", "value": "a@b.test",
     "rect": [20, 160, 300, 40]},
    {"selector": "body > p:nth-of-type(1)", "tag": "p", "text": "2 items", "rect": [20, 220, 100, 20]},
    {"selector": "bad", "rect": [1, 2]},
    "junk",
]}


class FakeBridge(web.WebBridge):
    def __init__(self, dom=WEB_DOM, pages=None, result="ok"):
        self.dom, self.result, self.ran = dom, result, []
        self.pages = [{"id": "p1", "bundle_id": "dev.shop"}] if pages is None else pages

    def contexts(self):
        return self.pages

    def evaluate(self, context_id, script, timeout=10):
        self.ran.append((context_id, script))
        if script == web.DOM_SCRIPT:
            if self.dom is None:
                raise web.WebUnreachable("gone")
            return self.dom
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def shop_tree():
    return parse_tree(app(
        node("NavigationBar", "Cart", rect=(0, 50, 402, 44)),
        node("WebView", "Cart", rect=(0, 100, 400, 700), children=[node("Other", "stale", rect=(0, 110, 100, 20))]),
        node("Button", "Back", rect=(10, 810, 80, 40)), bundle="dev.shop"))


class WebAttachTests(unittest.TestCase):
    def test_reads_the_dom_as_nodes_with_selectors(self):
        got = web.read(shop_tree(), FakeBridge())
        self.assertEqual(got.source, "web")
        by_label = {n.label: n for n in got.nodes}
        self.assertEqual(by_label["Checkout"].role, "Button")
        self.assertEqual(by_label["Checkout"].rect, (0 + 20, 100 + 100, 200, 40))
        self.assertEqual(by_label["Checkout"].identifier, "checkout")
        self.assertEqual({n.role for n in got.nodes if n.value == "a@b.test"}, {"TextField"})
        self.assertIn("Back", by_label)                       # native chrome outside the WebView stays
        self.assertNotIn("stale", by_label)                   # the WebView's own AX children are replaced
        self.assertEqual(len(got.page.nodes), 3)              # malformed rows are dropped
        self.assertEqual(got.page.selectors["web:#checkout"], "#checkout")

    def test_falls_back_to_the_ax_tree_and_says_why(self):
        for bridge, why in ((None, "no web inspector bridge"), (FakeBridge(pages=[]), "not inspectable"),
                            (FakeBridge(dom=None), "gone"), (FakeBridge(dom={"nodes": "x"}), "no DOM")):
            got = web.read(shop_tree(), bridge)
            self.assertEqual(got.source, "ax")
            self.assertIn(why, got.reason)
            self.assertIn("stale", {n.label for n in got.nodes})
        plain = parse_tree(app(node("Button", "Go")))
        self.assertEqual(web.read(plain, FakeBridge()).reason, "no WebView on screen")

    def test_page_of_another_app_is_not_read(self):
        bridge = FakeBridge(pages=[{"id": "x", "bundle_id": "other.app"}])
        self.assertEqual(web.read(shop_tree(), bridge).source, "ax")
        self.assertEqual(bridge.ran, [])

    def test_an_app_with_several_pages_is_not_read_at_a_guess(self):
        # Safari on the iOS 26.4 simulator listed three tabs, the one on screen last (26 Oct 2026)
        bridge = FakeBridge(pages=[{"id": "p1", "bundle_id": "dev.shop"}, {"id": "p2", "bundle_id": "dev.shop"}])
        got = web.read(shop_tree(), bridge)
        self.assertEqual(got.source, "ax")
        self.assertIn("2 inspectable pages", got.reason)
        self.assertEqual(bridge.ran, [])

    def test_click_and_type_target_the_selector_once(self):
        bridge = FakeBridge()
        page = web.read(shop_tree(), bridge).page
        web.click(bridge, page, "web:#checkout")
        web.type_text(bridge, page, "web:#checkout", 'x"); alert(1); ("')
        self.assertEqual(len(bridge.ran), 3)                  # the read, then one script per action
        self.assertIn('"#checkout"', bridge.ran[1][1])
        self.assertIn(r'x\"); alert(1); (\"', bridge.ran[2][1])  # text is a JSON string, never code

    def test_actions_refuse_when_unsure_and_never_retry(self):
        bridge = FakeBridge()
        page = web.read(shop_tree(), bridge).page
        with self.assertRaises(web.WebActionError):
            web.click(bridge, page, "web:#nothing")
        self.assertEqual(len(bridge.ran), 1)                  # nothing was sent
        for failure in ("missing", web.WebUnreachable("dropped")):
            bridge.result, bridge.ran = failure, []
            with self.assertRaises(web.WebActionError):
                web.click(bridge, page, "web:#checkout")
            self.assertEqual(len(bridge.ran), 1)


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
        self.assertTrue(flutter.read(flutter_tree(marked=True)).marked)

    def test_native_trees_are_not_flutter(self):
        native = parse_tree(app(*[node("Button", f"B{i}") for i in range(8)]))
        self.assertFalse(flutter.is_flutter(native))
        self.assertIsNone(flutter.read(native))
        self.assertFalse(flutter.is_flutter(parse_tree(app(node("Other", "A"), node("Other", "B")))))

    def test_maps_identifiers_and_roles_to_the_node_model(self):
        screen = flutter.read(flutter_tree(marked=True))
        self.assertEqual(screen.identifiers["btn_sign_in"].role, "Button")
        self.assertEqual(screen.identifiers["btn_sign_in"].label, "Sign in")
        self.assertEqual({n.role for n in screen.nodes if n.label == "Welcome back"}, {"StaticText"})
        self.assertEqual({n.role for n in screen.nodes if n.label == "Terms"}, {"Other"})   # disabled stays inert
        self.assertNotIn("FlutterSemanticsScrollView", screen.identifiers)

    def test_a_container_with_labelled_children_is_not_a_button(self):
        inner = node("Other", "Row", rect=(0, 100, 400, 60), children=[node("Other", "Title", rect=(0, 100, 200, 30))])
        screen = flutter.read(parse_tree(app(inner, *[node("Other", f"X{i}") for i in range(7)], node(
            "Other", "", id="FlutterView"))))
        roles = {n.label: n.role for n in screen.nodes}
        self.assertEqual((roles["Row"], roles["Title"]), ("Other", "Button"))


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
