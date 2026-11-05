import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mobile_agent import deeplinks, intents
from mobile_agent.intents import atlas, callback, harvest, plan
from mobile_agent.sim.simctl import Result

AASA = (Path(__file__).parent / "fixtures" / "intents" / "aasa.json").read_text()
THINGS = "com.example.things"


def info(*schemes, name="Things"):
    return {"CFBundleIdentifier": THINGS, "CFBundleDisplayName": name,
            "CFBundleURLTypes": [{"CFBundleURLSchemes": list(schemes)}]}


def things_atlas():
    entry = atlas.entry_from_bundle(
        THINGS, "Things", info("things", "javascript", "Bad Scheme"),
        {"com.apple.developer.associated-domains": ["applinks:things.example.com", "webcredentials:x.com",
                                                    "applinks:*.wild.com"]})
    found = atlas.Atlas([entry])
    found.add_aasa(THINGS, "things.example.com", AASA)
    return found


class AtlasTests(unittest.TestCase):
    def test_a_bundle_gives_its_schemes_and_domains_and_drops_blocked_ones(self):
        entry = things_atlas().entries[THINGS]
        self.assertEqual((entry.schemes, entry.domains), (["things"], ["things.example.com"]))
        self.assertIsNone(atlas.entry_from_bundle("x", "X", {}, None))

    def test_aasa_paths_decide_which_universal_links_belong_to_the_app(self):
        found = things_atlas()
        self.assertEqual(found.find("https://things.example.com/list/42").bundle, THINGS)
        self.assertIsNone(found.find("https://things.example.com/private/1"))
        self.assertIsNone(found.find("https://things.example.com/elsewhere"))
        self.assertIsNone(found.find("https://evil.example.com/list/42"))

    def test_legacy_not_paths_exclude_and_other_apps_rules_are_ignored(self):
        self.assertEqual(atlas.parse_aasa(AASA, "com.example.other"),
                         [{"pattern": "/x/*", "exclude": True}, {"pattern": "/x/*", "exclude": False},
                          {"pattern": "/y", "exclude": False}])
        self.assertEqual(atlas.parse_aasa("not json", THINGS), [])

    def test_an_app_that_left_the_phone_claims_nothing(self):
        self.assertIsNone(things_atlas().find("things://x", installed={"other"}))
        self.assertEqual(things_atlas().find("things://x", installed={THINGS}).bundle, THINGS)

    def test_app_folder_and_simulator_harvest_read_plists_without_a_phone(self):
        with tempfile.TemporaryDirectory() as root:
            app = Path(root) / "Things.app"
            app.mkdir()
            (app / "Info.plist").write_bytes(plistlib.dumps(info("things")))
            (app / atlas.ENTITLEMENTS).write_bytes(plistlib.dumps(
                {"com.apple.developer.associated-domains": ["applinks:things.example.com"]}))
            calls = []

            def run(argv, timeout=60):
                calls.append(argv[1:3] if argv[0] == "xcrun" else argv[:2])
                if argv[0] == "xcrun":
                    return Result(0, "{ }", "")
                return Result(0, json.dumps({THINGS: {"Path": str(app)}, "x.y": {"Path": "/missing"}}), "")
            found = atlas.harvest_simulator("<UDID>", run)
        self.assertEqual(sorted(found.entries), [THINGS])
        self.assertEqual(found.entries[THINGS].domains, ["things.example.com"])
        self.assertEqual(calls[0], ["simctl", "listapps"])

    def test_the_atlas_round_trips_through_its_file(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"MOBSTER_DATA_DIR": root}):
            atlas.save("sim/1", things_atlas())
            self.assertEqual(atlas.load("sim/1").find("things://x").bundle, THINGS)
            self.assertEqual(atlas.load("none").entries, {})


class PlanTests(unittest.TestCase):
    def test_the_agent_now_takes_a_discovered_link_for_an_installed_app(self):
        found = plan.propose("open Things and add milk", things_atlas(), installed={THINGS})
        self.assertEqual([(p["layer"], p["url"], p["state"]) for p in found],
                         [("atlas", "things://", "discovered")])
        self.assertEqual(deeplinks.decide(found[0]["url"]).action, "ask")   # another app's link still asks once
        self.assertEqual(deeplinks.decide(found[0]["url"], lambda *a: True).action, "open")

    def test_verified_links_come_first_and_keep_their_state(self):
        found = plan.propose("check Instagram", atlas.Atlas())
        self.assertEqual([(p["layer"], p["state"]) for p in found], [("links", "verified")] * 2)
        self.assertNotIn("instagram://user?username={username}", [p["url"] for p in found])

    def test_it_still_refuses_what_it_must_not_open(self):
        self.assertEqual(plan.propose("open Things", things_atlas(), installed=set()), [])   # not installed
        self.assertEqual(plan.propose("open Things", atlas.Atlas()), [])                     # not in the atlas
        for url in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "itms-services://?action=x",
                    "things"):
            with self.assertRaises(deeplinks.LinkError):
                deeplinks.decide(url)
        bad = atlas.Atlas([atlas.Entry("a.b", "Evil", schemes=["javascript", "tel"])])
        self.assertEqual(plan.propose("open Evil", bad), [])                                  # tel: acts
        for url in ("shortcuts://import-shortcut?url=https://x.example/a", "tel:+15555550123"):
            self.assertTrue(deeplinks.decide(url).always)

    def test_the_tally_counts_link_first_outcomes(self):
        tally = plan.Tally()
        tally.record(True), tally.record(False), tally.record(True)
        self.assertEqual(tally.public(), {"linkFirst": {"taken": 2, "missed": 1}})


class CallbackTests(unittest.TestCase):
    def test_an_app_link_gets_return_addresses_and_parses_back(self):
        url = callback.with_callback("things:///add?title=Milk", "abc123")
        self.assertIn("title=Milk&x-success=mobster-callback%3A%2F%2Fabc123%2Fsuccess", url)
        self.assertEqual(callback.parse("mobster-callback://abc123/success?id=7"),
                         {"token": "abc123", "status": "success", "params": {"id": "7"}})
        self.assertIsNone(callback.parse("things://abc123/success"))
        self.assertIsNone(callback.parse("mobster-callback://abc123/other"))

    def test_a_running_shortcut_still_asks_and_blocked_links_get_no_callback(self):
        link = callback.shortcut_run("Morning plan", "abc123")
        self.assertTrue(link.startswith("shortcuts://x-callback-url/run-shortcut?name=Morning%20plan&x-success="))
        self.assertEqual((deeplinks.decide(link).action, deeplinks.decide(link).always), ("ask", True))
        for url in ("javascript:x", "https://example.com/"):
            with self.assertRaises(deeplinks.LinkError):
                callback.with_callback(url, "abc123")
        with self.assertRaises(deeplinks.LinkError):
            callback.with_callback("things://", "no")


class FlagTests(unittest.TestCase):
    def test_the_layer_is_dark_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(intents.ENV_FLAG, None)
            self.assertFalse(intents.enabled())
        with patch.dict(os.environ, {intents.ENV_FLAG: "1"}):
            self.assertTrue(intents.enabled())


if __name__ == "__main__":
    unittest.main()


def _agent_run(goal, link_first, approve="approved"):
    from mobile_agent.frontier import FrontierAgent
    from mobile_agent.state import Element
    from mobile_agent.tests.test_frontier import Driver, screen

    class Phone(Driver):
        capabilities = Driver.capabilities | frozenset({"call"})

        def __init__(self, screens):
            super().__init__(screens)
            self.calls = []

        def call(self, method, path, body=None, timeout=10):
            self.calls.append((method, path, body))
            self.index += 1
            return {}

    class Model:
        def __init__(self):
            self.turns = 0
            self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}

        def complete(self, messages, schema, timeout=60, reasoning=None):
            if "items" in schema["properties"]:
                return {"items": []}, {}
            self.turns += 1
            return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "answer": "done",
                    "actions": [{"operation": "DONE", "target": None, "target_label": None, "text": None,
                                 "app": None}]}, {}
    home = screen(Element("1", "Home", "NavigationBar", (0, .07, 1, .06)), bundle="com.apple.springboard")
    opened = screen(Element("1", "Today", "NavigationBar", (0, .07, 1, .06)), bundle=THINGS)
    driver, model, asked = Phone([home, opened, opened, opened]), Model(), []
    agent = FrontierAgent(driver, model, apps={THINGS: "Things"}, screenshots=False, settle_seconds=0,
                          approve=lambda request: asked.append(request) or approve)
    agent.link_first = link_first
    result = agent.run(goal)
    return result, driver, model, asked


class LinkFirstTurnTests(unittest.TestCase):
    def test_a_matching_goal_opens_the_link_through_decide_before_any_tap(self):
        first = plan.LinkFirst(things_atlas())
        result, driver, model, asked = _agent_run("Open Things and read today's list.", first)
        self.assertEqual([(m, p, b["url"]) for m, p, b in driver.calls if p == "/url"],
                         [("POST", "/url", "things://")])
        self.assertEqual([a for a in driver.actions if a[0] != "DONE"], [])      # no tap
        self.assertEqual(len(asked), 1)                                           # another app's link asked once
        self.assertEqual(result["timing"]["model_calls_saved"], 1)
        self.assertEqual((result["timing"]["link_first"], result["timing"]["link_first_landed"]), (1, 1))

    def test_a_denied_link_is_not_opened_and_a_goal_with_no_link_taps(self):
        result, driver, model, asked = _agent_run("Open Things.", plan.LinkFirst(things_atlas()), approve="denied")
        self.assertEqual([c for c in driver.calls if c[1] == "/url"], [])
        result, driver, model, asked = _agent_run("Read my unread mail.", plan.LinkFirst(things_atlas()))
        self.assertEqual(([c for c in driver.calls if c[1] == "/url"], asked), ([], []))   # no app named
        self.assertNotIn("link_first", result["timing"])
        result, driver, model, asked = _agent_run("Open Things.", None)                   # flag off: no hook
        self.assertEqual([c for c in driver.calls if c[1] == "/url"], [])


class ReportTests(unittest.TestCase):
    def test_link_first_counts_reach_the_suite_junit_and_html(self):
        from mobile_agent.testkit import reports
        from mobile_agent.verify.flow import _link_first
        self.assertEqual(_link_first({"timing": {}}), {})
        said = _link_first({"timing": {"link_first": 1, "link_first_landed": 1, "model_calls_saved": 1}})
        self.assertEqual(said, {"linkFirst": {"taken": 1, "landed": 1, "modelCallsSaved": 1}})
        result = {"deviceKey": "d", "device": "iPhone", "status": "passed", "durationMs": 1000,
                  "linkFirst": said["linkFirst"], "assertions": []}
        doc = {"suite": "s", "devices": [{"key": "d", "name": "iPhone"}], "summary": {"passed": 1, "total": 1},
               "checks": [{"name": "c", "file": "c.yaml", "results": [result]}]}
        self.assertIn(b'name="linkFirstSteps" value="1"', reports.junit(doc))
        self.assertIn(b'name="modelCallsSaved" value="1"', reports.junit(doc))


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        patcher = patch.dict(os.environ, {"MOBSTER_DATA_DIR": self.root, intents.ENV_FLAG: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = Path(self.root) / "Things.app"
        self.app.mkdir()
        (self.app / "Info.plist").write_bytes(plistlib.dumps(info("things")))
        (self.app / atlas.ENTITLEMENTS).write_bytes(plistlib.dumps(
            {"com.apple.developer.associated-domains": ["applinks:things.example.com", "applinks:down.example.com"]}))

    def run_(self, argv, timeout=60):
        if argv[0] == "xcrun":
            return Result(0, "{ }", "")
        return Result(0, json.dumps({THINGS: {"Path": str(self.app)}}), "")

    def test_harvest_reads_bundles_fetches_aasa_and_a_failed_domain_is_logged_not_fatal(self):
        logs, asked = [], []

        def fetch(domain):
            asked.append(domain)
            if domain == "down.example.com":
                raise OSError("unreachable")
            return AASA
        found = harvest.harvest("<UDID>", self.run_, fetch=fetch, log=logs.append)
        self.assertEqual(found.find("https://things.example.com/list/1").bundle, THINGS)
        self.assertTrue(any("down.example.com" in line for line in logs))
        self.assertEqual(atlas.load("<UDID>").entries[THINGS].paths["things.example.com"], found.entries[THINGS].paths[
            "things.example.com"])
        harvest.harvest("<UDID>", self.run_, fetch=fetch, log=logs.append)       # cached for a day: nothing refetched
        self.assertEqual(len(asked), 2)

    def test_the_ready_step_is_dark_and_never_raises(self):
        with patch.dict(os.environ, {intents.ENV_FLAG: "0"}):
            self.assertIsNone(harvest.on_device_ready("<UDID>", run=self.run_))
        self.assertEqual(atlas.load("<UDID>").entries, {})
        logs = []

        def broken(argv, timeout=60):
            raise RuntimeError("no xcode")
        self.assertIsNone(harvest.on_device_ready("<UDID>", logs.append, run=broken, fetch=None))
        self.assertTrue(logs)
        done = harvest.on_device_ready("<UDID>", print, run=self.run_, fetch=lambda d: AASA, background=False)
        self.assertEqual(done.entries[THINGS].domains, ["things.example.com", "down.example.com"])
        self.assertTrue(atlas.load("<UDID>").entries[THINGS].paths["down.example.com"])

    def test_the_command_is_listed_only_with_the_flag(self):
        import subprocess
        import sys
        for flag, listed in (("", False), ("1", True)):
            out = subprocess.run([sys.executable, "-c", "from mobile_agent import devtools; print(devtools.ORDER)"],
                                 capture_output=True, text=True, env={**os.environ, intents.ENV_FLAG: flag})
            self.assertEqual("intents" in out.stdout, listed, out.stderr)
