"""The deep-link registry (devkit/links.py, links_cli.py, links_routes.py): the four sources, merging, "open <link
name>"
in steps, learning, saving, `mobster links list|check` and the API, on recorded project files and trees. Offline: no
simulator, phone, Node or network."""

import contextlib
import io
import json
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import api_routes
from mobile_agent.api_errors import APIError
from mobile_agent.devkit import links, links_cli, links_routes
from mobile_agent.testkit import api
from mobile_agent.tests.test_verify import CLEAN, tree
from mobile_agent.tests.test_verify_fixtures import app, node
from mobile_agent.tests.test_verify_runner import BUNDLE, Driver, Manager, RunnerCase
from mobile_agent.verify import runner
from mobile_agent.verify.checks import check_from_dict
from mobile_agent.verify.runner import VerifyRun
from mobile_agent.verify.tree import parse_tree, screen_title


class Project(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mobster-links-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / ".mobster").mkdir()

    def yaml(self, text):
        (self.root / ".mobster" / links.FILE).write_text(text, encoding="utf-8")

    def xcode(self):
        folder = self.root / "ios" / "Shop"
        folder.mkdir(parents=True)
        (folder / "Info.plist").write_bytes(plistlib.dumps({"CFBundleURLTypes": [
            {"CFBundleURLSchemes": ["shop", "shop-dev", "javascript"]}]}))
        (folder / "Shop.entitlements").write_bytes(plistlib.dumps({
            "com.apple.developer.associated-domains": ["applinks:shop.example.com?mode=developer",
                                                       "webcredentials:shop.example.com", "applinks:*.example.org"]}))
        pods = self.root / "ios" / "Pods" / "X"
        pods.mkdir(parents=True)
        (pods / "Info.plist").write_bytes(plistlib.dumps({"CFBundleURLTypes": [{"CFBundleURLSchemes": ["pods"]}]}))

    def expo(self):
        (self.root / "package.json").write_text('{"name": "shop", "dependencies": {"expo": "~54.0.0"}}')
        (self.root / "app.json").write_text(json.dumps({"expo": {"scheme": ["shop", "shop-expo"], "ios": {
            "bundleIdentifier": "com.example.shop", "associatedDomains": ["applinks:expo.example.com"]}}}))

    def urls(self, found):
        return {link.url: (link.name, link.source, link.title) for link in found}


class SourceTests(Project):
    def test_nothing_is_found_in_an_empty_project(self):
        self.assertEqual(links.discover(self.root), [])

    def test_the_xcode_project_gives_url_schemes_and_associated_domains_and_skips_pods_and_blocked_schemes(self):
        self.xcode()
        self.assertEqual(self.urls(links.discover(self.root)),
                         {"shop://": ("shop", "xcode", ""), "shop-dev://": ("shop-dev", "xcode", ""),
                          "https://shop.example.com/": ("shop.example.com", "xcode", "")})

    def test_the_expo_config_gives_schemes_and_domains(self):
        self.expo()
        self.assertEqual(self.urls(links.discover(self.root)),
                         {"shop://": ("shop", "expo", ""), "shop-expo://": ("shop-expo", "expo", ""),
                          "https://expo.example.com/": ("expo.example.com", "expo", "")})

    def test_links_yaml_in_either_shape_and_the_learned_file(self):
        self.yaml("links:\n  - name: cart\n    url: shop://cart\n    title: Your cart\n")
        (self.root / ".mobster" / links.LEARNED).write_text(json.dumps({"schema": links.LEARNED_SCHEMA, "links": [
            {"name": "orders", "url": "shop://orders"}]}))
        self.assertEqual(self.urls(links.discover(self.root)),
                         {"shop://cart": ("cart", "links.yaml", "Your cart"),
                          "shop://orders": ("orders", "learned", "")})
        self.yaml("cart: shop://cart\nprofile:\n  url: shop://me\n  title: Profile\n")
        self.assertEqual(self.urls(links.discover(self.root)),
                         {"shop://cart": ("cart", "links.yaml", ""), "shop://me": ("profile", "links.yaml", "Profile"),
                          "shop://orders": ("orders", "learned", "")})

    def test_a_link_in_several_sources_is_listed_once_with_the_first_name_and_any_title(self):
        self.xcode()
        self.expo()
        self.yaml("links:\n  - name: home\n    url: shop://\n    title: Welcome\n")
        found = links.discover(self.root)
        home = next(link for link in found if link.url == "shop://")
        self.assertEqual((home.name, home.source, home.title), ("home", "links.yaml", "Welcome"))
        self.assertEqual(sum(link.url == "shop://" for link in found), 1)
        self.assertEqual({link.source for link in found}, {"links.yaml", "expo", "xcode"})

    def test_a_wrong_links_file_says_where(self):
        for text, fragment in (("links: 3\n", "list of at most"),
                               ("links:\n  - name: a\n    link: x://\n", "use name, url"),
                               ("links:\n  - name: ''\n    url: a://b\n", "`name` is 1 to 60"),
                               ("links:\n  - name: a\n    url: javascript:alert(1)\n", "never opens javascript"),
                               ("links:\n  - name: a\n    url: nowhere\n", "needs a scheme"),
                               ("a: [\n", "isn't valid YAML")):
            with self.subTest(text=text):
                self.yaml(text)
                with self.assertRaises(links.LinksError) as caught:
                    links.discover(self.root)
                self.assertIn(fragment, str(caught.exception))

    def test_a_scheme_alone_names_no_screen(self):
        self.assertEqual([links.ProjectLink("a", url, "x").screen for url in
                          ("shop://", "https://shop.example.com/", "shop://cart", "shop://?tab=1", "shop://cart/1")],
                         [False, False, True, True, True])

    def test_url_paths_and_queries_keep_their_case(self):
        self.yaml("upper: shop://item/ABC?code=Q\nlower: shop://item/abc?code=q\n")
        found = links.discover(self.root)
        self.assertEqual(len(found), 2)
        self.assertEqual(links.find(found, "shop://item/abc?code=q").name, "lower")

    def test_a_malformed_learned_list_does_not_break_discovery(self):
        (self.root / ".mobster" / links.LEARNED).write_text(json.dumps({"schema": links.LEARNED_SCHEMA, "links": 3}))
        self.assertEqual(links.discover(self.root), [])


class StepTests(Project):
    def found(self):
        return [links.ProjectLink("cart", "shop://cart", "links.yaml"),
                links.ProjectLink("My Orders", "shop://orders", "x")]

    def test_open_a_link_by_name_becomes_the_address(self):
        steps = ("open cart", "Open link cart.", "open the deep link 'my orders'", "Open Settings", "tap Buy",
                 "open the cart page")
        self.assertEqual(links.expand_steps(steps, self.found()),
                         ("Open the link shop://cart", "Open the link shop://cart", "Open the link shop://orders",
                          "Open Settings", "tap Buy", "open the cart page"))
        self.assertEqual(links.expand_steps(steps, []), steps)

    def test_a_plan_expands_steps_and_reads_the_registry_only_when_a_step_opens_something(self):
        checks = self.root / ".mobster" / "checks"
        checks.mkdir()
        (checks / "a.yaml").write_text("app:\n  bundle: com.example.shop\nsteps:\n  - open cart\n  - tap Buy\n"
                                       "expect:\n  - text: Thanks\n")
        (checks / "b.yaml").write_text("app:\n  bundle: com.example.shop\nsteps:\n  - tap Buy\n"
                                       "expect:\n  - text: Thanks\n")
        self.yaml("cart: shop://cart\n")
        with mock.patch.object(links, "discover", wraps=links.discover) as discover:
            planned = api.plan(api.Options(sims=("iPhone 17 Pro",)), cwd=self.root)
        self.assertEqual(discover.call_count, 1)
        by_file = {Path(entry.file).name: entry.check.steps for entry in planned.entries}
        self.assertEqual(by_file["a.yaml"], ("Open the link shop://cart", "tap Buy"))
        self.assertEqual(by_file["b.yaml"], ("tap Buy",))
        self.yaml("nonsense: [\n")
        with self.assertRaises(api.UsageError):
            api.plan(api.Options(sims=("iPhone 17 Pro",)), cwd=self.root)

    def test_an_ambiguous_step_name_refuses_instead_of_opening_the_first_link(self):
        found = [links.ProjectLink("cart", "shop://cart", "links.yaml"),
                 links.ProjectLink("Cart", "shop://basket", "learned")]
        with self.assertRaisesRegex(links.LinksError, "Several links"):
            links.expand_steps(("open cart",), found)


class LearnTests(Project):
    def test_a_custom_scheme_a_run_opened_is_kept_once(self):
        self.assertTrue(links.learn(self.root, "shop://orders/42"))
        self.assertFalse(links.learn(self.root, "shop://orders/42"))
        [link] = links.discover(self.root)
        self.assertEqual((link.url, link.source, link.name), ("shop://orders/42", "learned", "orders 42"))

    def test_web_pages_blocked_and_acting_links_and_a_folder_without_mobster_are_not_kept(self):
        for url in ("https://example.com/a", "javascript:alert(1)", "tel:5551234", "nowhere"):
            self.assertFalse(links.learn(self.root, url), url)
        self.assertFalse(links.learn(self.root / "elsewhere", "shop://cart"))
        self.assertEqual(links.discover(self.root), [])

    def test_it_keeps_the_newest_fifty(self):
        for number in range(links.MAX_LEARNED + 5):
            links.learn(self.root, f"shop://item/{number}")
        found = links.discover(self.root)
        self.assertEqual(len(found), links.MAX_LEARNED)
        self.assertEqual(found[0].url, f"shop://item/{links.MAX_LEARNED + 4}")

    def test_concurrent_learning_keeps_every_new_link(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        write = links.write_text_atomic
        def slow_write(*args):
            time.sleep(.01)
            return write(*args)
        with mock.patch.object(links, "write_text_atomic",
                               side_effect=slow_write), ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n: links.learn(self.root, f"shop://item/{n}"), range(12)))
        self.assertEqual({link.url for link in links.discover(self.root)}, {f"shop://item/{n}" for n in range(12)})


class SaveTests(Project):
    def test_save_replaces_by_name_and_remove_takes_it_out(self):
        links.save(self.root, "cart", "shop://cart", "Cart")
        links.save(self.root, "Cart", "shop://basket", "Basket")
        links.save(self.root, "me", "shop://me")
        self.assertEqual(self.urls(links.discover(self.root)), {"shop://basket": ("Cart", "links.yaml", "Basket"),
                                                                 "shop://me": ("me", "links.yaml", "")})
        self.assertTrue(links.remove(self.root, "cart"))
        self.assertFalse(links.remove(self.root, "cart"))
        self.assertEqual([link.name for link in links.discover(self.root)], ["me"])

    def test_save_refuses_what_it_cannot_keep(self):
        for args in (("", "shop://x"), ("a", "javascript:1"), ("a", "nowhere"), ("a", 3), ("a", "shop://x", "t" * 201)):
            with self.subTest(args=args), self.assertRaises(links.LinksError):
                links.save(self.root, *args)
        self.assertFalse((self.root / ".mobster" / links.FILE).exists())

    def urls(self, found):
        return {link.url: (link.name, link.source, link.title) for link in found}


class CheckTests(Project):
    def test_a_link_becomes_a_check_that_launches_with_it_and_expects_its_title(self):
        [(link, check)] = links.checks_for([links.ProjectLink("cart", "shop://cart", "x", "Your cart")],
                                           bundle="com.example.shop")
        self.assertEqual((check.name, check.open_url, check.bundle_id, check.tags), ("Link: cart", "shop://cart",
                                                                                     "com.example.shop", ("links",)))
        self.assertEqual([assertion.to_dict() for assertion in check.expect][0]["text"], "Your cart")
        [(_, bare)] = links.checks_for([links.ProjectLink("home", "shop://", "x")], bundle="com.example.shop")
        self.assertEqual(bare.expect, ())

    def test_the_bundle_comes_from_expo_or_the_xcode_project_and_a_missing_one_is_asked_for(self):
        self.expo()
        self.assertEqual(links.bundle_id(self.root), "com.example.shop")
        (self.root / "package.json").unlink()
        project = self.root / "Shop.xcodeproj"
        project.mkdir()
        (project / "project.pbxproj").write_text("PRODUCT_BUNDLE_IDENTIFIER = com.example.ShopTests;\n"
                                                 "PRODUCT_BUNDLE_IDENTIFIER = com.example.xcode;\n")
        self.assertEqual(links.bundle_id(self.root), "com.example.xcode")
        shutil.rmtree(project)
        with self.assertRaises(links.LinksError) as caught:
            links.plan_check(self.root, [links.ProjectLink("a", "shop://a", "x")], api.Options(sims=("iPhone 17 Pro",)))
        self.assertIn("--bundle", str(caught.exception))

    def test_plan_check_makes_one_entry_per_link(self):
        planned = links.plan_check(self.root, [links.ProjectLink("cart", "shop://cart", "x", "Your cart"),
                                               links.ProjectLink("me", "shop://me", "x")],
                                   api.Options(sims=("iPhone 17 Pro",)), bundle="com.example.shop")
        self.assertEqual([entry.file for entry in planned.entries],
                         [".mobster/links.yaml#cart", ".mobster/links.yaml#me"])
        self.assertEqual(len(planned.targets), 1)


class CliTests(Project):
    def run_cli(self, *argv, execute=None):
        import argparse
        parser = argparse.ArgumentParser(prog="mobster links")
        links_cli.add_arguments(parser, None)
        args = parser.parse_args(list(argv))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("mobile_agent.testkit.api.execute", execute or mock.Mock()):
            code = links_cli.run(args, cwd=self.root)
        return code, out.getvalue(), err.getvalue()

    def test_list_prints_each_link_and_json(self):
        self.yaml("links:\n  - name: cart\n    url: shop://cart\n    title: Your cart\n")
        self.xcode()
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertRegex(out, r'cart\s+shop://cart\s+links.yaml\s+"Your cart"')
        self.assertIn("xcode", out)
        _, raw, _ = self.run_cli("list", "--json")
        found = {item["name"]: item for item in json.loads(raw)["links"]}
        self.assertEqual((found["cart"]["screen"], found["shop"]["screen"], found["cart"]["title"]),
                         (True, False, "Your cart"))

    def test_check_runs_each_link_says_where_it_landed_and_never_opens_one_that_acts(self):
        self.yaml("links:\n  - name: cart\n    url: shop://cart\n    title: Your cart\n"
                  "  - name: call\n    url: tel:5551234\n")
        seen = {}

        def execute(planned, progress=None):
            seen["entries"] = [entry.name for entry in planned.entries]
            doc = {"schema": "mobster.test/1", "suite": "s", "exitCode": 1, "durationMs": 10, "devices": [],
                   "summary": {"passed": 0, "failed": 1}, "options": {"repeat": 1},
                   "checks": [{"name": "Link: cart", "results": [{
                       "status": "failed", "device": "iPhone", "screen": {"title": "Sign in"}, "attempts": 1,
                       "reason": {"message": "Your cart wasn't on screen", "fix": None}}]}]}
            return doc, {}
        code, out, err = self.run_cli("check", "--bundle", "com.example.shop", "--sim", "iPhone 17 Pro",
                                      execute=execute)
        self.assertEqual((code, seen["entries"]), (1, ["Link: cart"]))
        self.assertIn("Link: cart: landed on \"Sign in\"", out)
        self.assertIn("Skipped call: it acts as it opens", err)

    def test_check_names_a_missing_link_and_an_unknown_app(self):
        self.yaml("cart: shop://cart\n")
        for argv, fragment in ((("check", "nope"), "No link is called nope. You have: cart."),
                               (("check", "--sim", "iPhone 17 Pro"), "--bundle")):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 3)
                self.assertIn(fragment, err)


class RouteTests(Project):
    def call(self, method, body=None, query=None, app_session=True):
        if api_routes.match(method, "/api/tests/links") is None:
            links_routes.register_routes()
        route, params = api_routes.match(method, "/api/tests/links")
        runtime = SimpleNamespace(local_api={"app_session": True})
        return route.handler(api_routes.Request(runtime=runtime, method=method, path="/api/tests/links", params=params,
                                                query=query or {}, headers={}, body=body, content_type=None,
                                                origin="app", app_session=app_session))

    def test_list_save_and_remove(self):
        self.xcode()
        answer = self.call("GET", query={"project": [str(self.root)]}, app_session=False).value
        self.assertEqual({item["name"] for item in answer["links"]}, {"shop", "shop-dev", "shop.example.com"})
        saved = self.call("POST", {"project": str(self.root), "name": "cart", "url": "shop://cart", "title": "Cart"})
        self.assertEqual((saved.status, saved.value["link"]["title"]), (201, "Cart"))
        self.assertEqual(self.call("DELETE", query={"project": [str(self.root)], "name": ["cart"]}).value,
                         {"removed": True})

    def test_it_refuses_a_bad_project_a_bad_link_and_a_write_without_the_app(self):
        for args, code, status in (
                (("GET", None, {"project": ["relative"]}, False), "bad_project", 400),
                (("POST", {"project": str(self.root), "name": "a", "url": "javascript:1"}, None, True), "bad_link",
                 400),
                (("POST", {"project": str(self.root), "name": "a", "url": "shop://a"}, None, False), "app_only", 403),
                (("DELETE", None, {"project": [str(self.root)], "name": ["a"]}, False), "app_only", 403)):
            with self.subTest(code=code), self.assertRaises(APIError) as caught:
                self.call(*args)
            self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
        self.assertFalse((self.root / ".mobster" / links.FILE).exists())

    def test_reading_links_does_not_evaluate_dynamic_expo_config(self):
        from mobile_agent.devkit import expo
        self.expo()
        (self.root / "app.config.ts").write_text("export default {}")
        self.yaml("cart: shop://cart\n")
        with mock.patch.object(expo, "read_config", side_effect=AssertionError("project code must not execute")):
            answer = self.call("GET", query={"project": [str(self.root)]}, app_session=False).value
        self.assertEqual([link["name"] for link in answer["links"]], ["cart"])


class LandingTests(RunnerCase):
    def test_a_title_is_the_navigation_bars_else_the_top_text(self):
        bar = parse_tree(app(node("Other", children=[node("NavigationBar", "Your cart", rect=(0, 50, 402, 44)),
                                                    node("StaticText", "Subtotal", rect=(10, 200, 100, 20))],
                                  rect=(0, 0, 402, 874)), bundle=BUNDLE))
        self.assertEqual(screen_title(bar), "Your cart")
        self.assertEqual(screen_title(parse_tree(CLEAN)), "Account")
        self.assertEqual(screen_title(tree(node("StaticText", "Far down", rect=(10, 800, 100, 20)))), "")

    def test_a_run_records_the_screen_it_ended_on_and_learns_the_link_it_opened(self):
        project = Path(tempfile.mkdtemp(prefix="mobster-links-run-"))
        self.addCleanup(shutil.rmtree, project, True)
        (project / ".mobster").mkdir()
        check = check_from_dict({"app": {"bundle": BUNDLE}, "launch": {"url": "shop://cart"},
                                 "expect": [{"text": "Account"}]})
        check = check.__class__(**{**check.__dict__, "source": str(project / ".mobster" / "checks" / "a.yaml")})
        manager, driver = Manager(), Driver([CLEAN])
        self.watch(driver, manager)
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager,
                        simctl=lambda args: (0, "large" if len(args) == 3 else ""))
        with mock.patch.object(runner, "build_driver", lambda *a: driver):
            run.prepare()
        run.open_url("shop://orders")
        outcome = run.finish()
        self.assertEqual(outcome["screen"], {"title": "Account"})
        self.assertEqual({link.url for link in links.discover(project)}, {"shop://cart", "shop://orders"})


if __name__ == "__main__":
    unittest.main()
