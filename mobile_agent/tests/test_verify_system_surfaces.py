"""System surfaces (verify/system_surfaces): the check block, each surface's driver and the run's rules, on a scripted
phone that serves hand-built trees and records every action. No simulator, phone, model or network."""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent.tests.test_verify_fixtures import app, node
from mobile_agent.tests.test_verify_runner import BUNDLE, Driver, RunnerCase
from mobile_agent.transport import TransportError
from mobile_agent.verify.checks import CheckError, check_from_dict, check_to_dict
from mobile_agent.verify.system_surfaces import play, spec, surfaces
from mobile_agent.verify.system_surfaces.stage import Stage
from mobile_agent.verify.tree import EVIDENCE_SOURCE_PATH, GLANCE_SOURCE_PATH, parse_tree
from mobile_agent.verify.verdict import CouldntRun

SPRINGBOARD = "com.apple.springboard"


def board(*children):
    return app(*children, bundle=SPRINGBOARD)


def icons():
    return [node("Icon", f"App {n}", rect=(20 + 90 * (n % 4), 120 + 110 * (n // 4), 60, 60)) for n in range(12)]


HOME = board(*icons())
APP_SCREEN = app(node("StaticText", "Firefox home", rect=(20, 100, 200, 30)), bundle=BUNDLE)


def flagged():
    return mock.patch.dict(os.environ, {spec.FLAG: "1"})


class Phone(Driver):
    """A phone whose screen is ``states[n]`` after its n-th action; reads never change it."""

    def __init__(self, states, fail_at=None):
        super().__init__([states[0]])
        self.states, self.actions, self.fail_at, self.settings = list(states), [], fail_at, []

    def call(self, method, path, body=None, timeout=10):
        if path == "/appium/settings":   # which app WDA reads; the screen stays as it is
            self.settings.append(body["settings"])
            return None
        if path in (EVIDENCE_SOURCE_PATH, GLANCE_SOURCE_PATH):
            return self.states[min(len(self.actions), len(self.states) - 1)]
        if path == "/alert/text":
            return super().call(method, path, body, timeout)
        if len(self.actions) == self.fail_at:
            raise TransportError("timed out")
        self.actions.append((method, path, body))
        return None

    def kinds(self):
        """Each action as a short name."""
        out = []
        for method, path, body in self.actions:
            if path == "/actions":
                steps = body["actions"][0]["actions"]
                names = [step["type"] for step in steps]
                if {"type": "pause", "duration": 1200} in steps:
                    out.append("hold")
                elif names.count("pointerMove") > 1:
                    out.append("drag")
                else:
                    out.append("tap")
            else:
                out.append(path.rsplit("/", 1)[-1])
        return out


class Ctx:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def open_url(self, url):
        self.calls.append(("open_url", url))

    def launch(self, bundle, env):
        self.calls.append(("launch", bundle, env))

    def install(self, path):
        self.calls.append(("install", path))


def stage_for(phone):
    clock = SimpleNamespace(now=0.0)
    clock.sleep = lambda seconds: setattr(clock, "now", clock.now + seconds)
    return Stage(phone, log=lambda line: None, sleep=clock.sleep, clock=lambda: clock.now)


def one(**item):
    with flagged():
        return spec.parse([item])[0]


class SpecTests(unittest.TestCase):
    def test_off_unless_the_flag_is_on(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(spec.FLAG, None)
            self.assertEqual(spec.parse(None), ())
            with self.assertRaisesRegex(CheckError, "MOBSTER_EXPERIMENTAL_SYSTEM_SURFACES=1"):
                spec.parse([{"surface": "widget", "expect": [{"text": "x"}]}])

    def test_every_kind_parses_and_round_trips(self):
        items = [
            {"surface": "intent", "shortcut": "Ask Firefox", "input": "streak", "expect": [{"text": "12"}]},
            {"surface": "intent", "siri": "Ask Firefox my streak", "expect": [{"text": "12"}]},
            {"surface": "widget", "add": "Firefox", "name": "Streak", "expect": [{"text": "12 day streak"}]},
            {"surface": "live_activity", "where": "lock_screen", "expect": [{"text": "Round 3"}]},
            {"surface": "control_center", "pick": "Firefox", "then": [{"text": "Opened"}]},
            {"surface": "app_clip", "bundle": "org.mozilla.ios.Firefox.Clip", "url": "https://example.com/c",
             "expect": [{"text": "Order"}]},
            {"surface": "spotlight", "query": "Firefox", "pick": {"label": "Firefox", "role": "cell"},
             "expect": [{"text": "Firefox"}], "then": [{"text": "Firefox home"}]},
            {"surface": "share_sheet", "open": "Share", "pick": "Copy", "expect": [{"text": "Copy"}]},
        ]
        with flagged():
            parsed = spec.parse(items)
            self.assertEqual([item.kind for item in parsed], [item["surface"] for item in items])
            self.assertEqual(spec.parse(spec.to_dict(parsed)), parsed)

    def test_refuses_what_it_cannot_run(self):
        cases = {
            "isn't a system surface; did you mean widget": {"surface": "widgit", "expect": [{"text": "x"}]},
            "spotlight.*query is required|query is required": {"surface": "spotlight", "expect": [{"text": "x"}]},
            "exactly one of shortcut": {"surface": "intent", "shortcut": "a", "siri": "b", "expect": [{"text": "x"}]},
            "checks nothing": {"surface": "widget"},
            "no pick": {"surface": "spotlight", "query": "a", "then": [{"text": "x"}]},
            "where must be one of": {"surface": "live_activity", "where": "tv", "expect": [{"text": "x"}]},
            "via must be one of": {"surface": "app_clip", "bundle": "a.b", "url": "https://a.b", "via": "ir",
                                   "expect": [{"text": "x"}]},
            "unknown key .colour.": {"surface": "widget", "colour": "red", "expect": [{"text": "x"}]},
        }
        for pattern, item in cases.items():
            with self.subTest(pattern), flagged(), self.assertRaisesRegex(CheckError, pattern):
                spec.parse([item])

    def test_a_check_file_carries_surfaces_and_tool_arguments_do_not(self):
        data = {"app": {"bundle": BUNDLE}, "surfaces": [{"surface": "widget", "expect": [{"text": "Streak"}]}]}
        with flagged():
            check = check_from_dict(data)
            self.assertEqual(check.surfaces[0].kind, "widget")
            self.assertEqual(check_from_dict(check_to_dict(check)).surfaces, check.surfaces)
            with self.assertRaisesRegex(CheckError, "check file"):
                check_from_dict({"bundle_id": BUNDLE, "surfaces": data["surfaces"]})
        self.assertEqual(check_from_dict({"app": {"bundle": BUNDLE}}).surfaces, ())


class HomeScreenTests(unittest.TestCase):
    def test_the_empty_spot_is_clear_of_every_icon(self):
        x, y = surfaces.empty_point(parse_tree(HOME))
        for n in range(12):
            ix, iy = 20 + 90 * (n % 4), 120 + 110 * (n // 4)
            self.assertFalse(ix <= x <= ix + 60 and iy <= y <= iy + 60)

    def test_a_full_screen_has_none(self):
        full = board(node("Icon", "Wall", rect=(0, 0, 402, 874)))
        self.assertIsNone(surfaces.empty_point(parse_tree(full)))

    def test_adding_a_widget_walks_the_gallery_and_reads_the_home_screen(self):
        # The labels and order iOS 26.4's simulator shows (26 Oct 2026): edit mode's Edit button, its menu, the
        # gallery's search, the app's row in front of its own icon, and the widget page's " Add Widget".
        jiggle = board(node("Icon", "Photo Editor", rect=(20, 120, 60, 60)),
                       node("Button", "Edit", rect=(39, 19, 66, 28)))
        menu = board(node("Button", "Add Widget", rect=(37, 72, 251, 42)),
                     node("Button", "Edit Pages", rect=(37, 198, 251, 42)))
        gallery = board(node("SearchField", "Search Widgets", rect=(26, 154, 350, 44)))
        results = board(node("Icon", "Firefox", rect=(120, 288, 69, 92)),
                        node("Cell", "Firefox", rect=(0, 218, 402, 73)))
        detail = board(node("Button", " Add Widget", rect=(24, 766, 354, 50)))
        done = board(node("Button", "Done", rect=(320, 60, 60, 30)))
        shown = board(*icons(), node("Other", "12 day streak", rect=(20, 400, 160, 160)))
        phone = Phone([HOME, HOME, jiggle, menu, gallery, gallery, results, detail, done, shown, APP_SCREEN])
        results_, _ = surfaces.widget(stage_for(phone), one(surface="widget", add="Firefox",
                                                             expect=[{"text": "12 day streak"}]), Ctx())
        self.assertEqual([r.ok for r in results_], [True])
        self.assertEqual(phone.kinds(), ["pressButton", "hold", "tap", "tap", "tap", "keys", "tap", "tap", "tap"])
        self.assertEqual(phone.actions[5][2], {"value": list("Firefox")})
        taps = [step["x"] for _, path, body in phone.actions if path == "/actions"
                for step in body["actions"][0]["actions"][:1]]
        self.assertEqual(taps[1:3] + taps[4:5], [72, 162, 201])   # Edit, Add Widget, the Firefox row

    def test_a_missing_control_says_what_it_saw(self):
        phone = Phone([HOME, HOME, HOME])
        with self.assertRaisesRegex(surfaces.SurfaceMiss, r"the Home Screen's Edit button didn't show; the screen had "
                                                          r'"App 0"'):
            surfaces.widget(stage_for(phone), one(surface="widget", add="Firefox", expect=[{"text": "x"}]), Ctx())


class DriverTests(unittest.TestCase):
    def test_a_shortcut_runs_by_url_and_a_phrase_goes_to_siri(self):
        ctx, phone = Ctx(), Phone([APP_SCREEN])
        surfaces.intent(stage_for(phone), one(surface="intent", shortcut="Ask Firefox", input="my streak",
                                              expect=[{"text": "Firefox home"}]), ctx)
        self.assertEqual(ctx.calls, [("open_url", "shortcuts://run-shortcut?name=Ask%20Firefox&input=text&text=my"
                                                  "%20streak")])
        surfaces.intent(stage_for(phone), one(surface="intent", siri="Ask Firefox my streak",
                                              expect=[{"text": "Firefox home"}]), ctx)
        self.assertEqual(phone.actions, [("POST", "/wda/siri/activate", {"text": "Ask Firefox my streak"})])

    def test_the_lock_screen_is_read_locked_and_unlocked_even_when_it_fails(self):
        locked = board(node("Other", "Round 3 of 5", rect=(20, 300, 360, 90)))
        for text, ok in (("Round 3", True), ("Round 4", False)):
            phone = Phone([locked, locked, locked])
            results, _ = surfaces.live_activity(stage_for(phone), one(surface="live_activity", where="lock_screen",
                                                                      expect=[{"text": text}]), Ctx())
            self.assertEqual([r.ok for r in results], [ok])
            self.assertEqual(phone.kinds(), ["lock", "unlock"])

    def test_the_island_is_read_from_the_home_screen(self):
        phone = Phone([HOME, HOME])
        surfaces.live_activity(stage_for(phone), one(surface="live_activity", expect=[{"text": "App 1"}]), Ctx())
        self.assertEqual(phone.kinds(), ["pressButton"])

    def test_control_center_opens_from_the_top_right(self):
        phone = Phone([HOME, HOME])
        surfaces.control_center(stage_for(phone), one(surface="control_center", expect=[{"text": "App 1"}]), Ctx())
        steps = phone.actions[0][2]["actions"][0]["actions"]
        self.assertEqual((steps[0]["x"], steps[0]["y"]), (377, 3))
        self.assertGreater(steps[2]["y"], 300)

    def test_an_app_clip_launches_with_its_invocation_url(self):
        ctx, phone = Ctx(), Phone([app(node("StaticText", "Order", rect=(0, 100, 100, 30)), bundle="a.b.Clip")])
        results, _ = surfaces.app_clip(stage_for(phone), one(surface="app_clip", bundle="a.b.Clip",
                                                             url="https://example.com/c", path="/tmp/Clip.app",
                                                             expect=[{"text": "Order"}]), ctx)
        self.assertEqual(ctx.calls, [("install", "/tmp/Clip.app"),
                                     ("launch", "a.b.Clip", {"_XCAppClipURL": "https://example.com/c"})])
        self.assertEqual(phone.actions[0][2], {"bundleId": "a.b.Clip"})
        self.assertTrue(results[0].ok)

    def test_spotlight_types_the_query(self):
        search = board(node("SearchField", "Search", rect=(20, 60, 300, 40)),
                       node("Cell", "Firefox", rect=(20, 120, 360, 60)))
        phone = Phone([HOME, HOME, search, search, search])
        results, _ = surfaces.spotlight(stage_for(phone), one(surface="spotlight", query="Day",
                                                              expect=[{"text": "Firefox"}]), Ctx())
        self.assertEqual(phone.kinds(), ["pressButton", "drag", "tap", "keys"])
        self.assertTrue(results[0].ok)

    def test_the_share_sheet_opens_from_the_apps_own_control(self):
        screen = app(node("Button", "Share", rect=(300, 60, 60, 40)), node("Button", "Copy", rect=(20, 600, 80, 80)),
                     bundle=BUNDLE)
        phone = Phone([screen, screen])
        results, _ = surfaces.share_sheet(stage_for(phone), one(surface="share_sheet", open="Share",
                                                                expect=[{"text": "Copy"}]), Ctx())
        self.assertEqual(phone.kinds(), ["tap"])
        self.assertTrue(results[0].ok)


class RunFake:
    """The parts of a VerifyRun that drive() reads."""

    def __init__(self, phone, surfaces_, real=False):
        self.check = SimpleNamespace(surfaces=tuple(surfaces_))
        self.driver, self.manager = phone, SimpleNamespace(real_device=real, calls=[])
        self.app, self.target = SimpleNamespace(bundle_id=BUNDLE), SimpleNamespace(udid="U")
        self.findings, self.events, self.lines, self.stopped = [], [], [], False
        self.lock = mock.MagicMock()

    log = lambda self, line: self.lines.append(line)  # noqa: E731
    event = lambda self, event: self.events.append(event)  # noqa: E731
    _sleep = staticmethod(lambda seconds: None)
    frame = lambda self, label: Path(f"/run/frames/{label}.jpg")  # noqa: E731
    _relative = staticmethod(lambda frame: str(frame).replace("/run/", "") if frame else None)
    _check_stop = lambda self: None  # noqa: E731


class DriveTests(unittest.TestCase):
    def drive(self, phone, items, **kwargs):
        with flagged():
            run = RunFake(phone, spec.parse(items), **kwargs)
            with mock.patch.object(play, "is_real_device", lambda manager: manager.real_device), \
                    mock.patch.object(play, "Stage", lambda driver, log, sleep, clock: stage_for(driver)):
                play.drive(run)
        return run

    def test_a_failed_assertion_is_a_fail_finding_with_its_frame_and_the_app_comes_back(self):
        phone = Phone([HOME, HOME, HOME])
        run = self.drive(phone, [{"surface": "control_center", "name": "Controls", "expect": [{"text": "Mobster"}]}])
        self.assertEqual(len(run.findings), 1)
        finding = run.findings[0]
        self.assertEqual((finding["level"], finding["frame"]), ("fail", "frames/surface-control_center-1.jpg"))
        self.assertTrue(finding["message"].startswith("Controls: "), finding["message"])
        self.assertEqual(phone.actions[-1], ("POST", "/wda/apps/activate", {"bundleId": BUNDLE}))
        self.assertEqual(play.note(run), "; 0 of 1 system surfaces held")

    def test_a_surface_that_cannot_be_reached_fails_and_the_next_one_still_runs(self):
        phone = Phone([HOME] * 8)
        run = self.drive(phone, [{"surface": "widget", "add": "Firefox", "expect": [{"text": "x"}]},
                                 {"surface": "control_center", "expect": [{"text": "App 1"}]}])
        self.assertEqual(len(run.findings), 1)
        self.assertIn("Edit button didn't show", run.findings[0]["message"])
        self.assertEqual(play.note(run), "; 1 of 2 system surfaces held")

    def test_each_surface_reads_whatever_is_in_front_not_the_app_under_test(self):
        phone = Phone([APP_SCREEN, HOME, APP_SCREEN])
        run = self.drive(phone, [{"surface": "control_center", "expect": [{"text": "App 3"}]}])
        self.assertEqual(run.findings, [])
        self.assertEqual(phone.settings, [{"defaultActiveApplication": "auto"}])
        self.assertEqual(phone.kinds(), ["drag", "activate"])

    def test_a_pick_is_tapped_and_what_follows_is_read(self):
        search = board(node("SearchField", "Search", rect=(20, 60, 300, 40)),
                       node("Cell", "Firefox", rect=(20, 120, 360, 60)))
        phone = Phone([HOME, HOME, search, search, search, APP_SCREEN])
        run = self.drive(phone, [{"surface": "spotlight", "query": "Day", "expect": [{"text": "Firefox"}],
                                  "pick": {"label": "Firefox", "role": "cell"}, "then": [{"text": "Firefox home"}]}])
        self.assertEqual(run.findings, [])
        self.assertEqual(phone.kinds(), ["pressButton", "drag", "tap", "keys", "tap", "activate"])

    def test_a_share_sheet_left_open_is_closed_by_tapping_outside_it(self):
        screen = app(node("Button", "Share", rect=(300, 60, 60, 40)), node("Button", "Copy", rect=(20, 600, 80, 80)),
                     bundle=BUNDLE)
        phone = Phone([screen])
        self.drive(phone, [{"surface": "share_sheet", "open": "Share", "expect": [{"text": "Copy"}]}])
        self.assertEqual(phone.kinds(), ["tap", "tap", "activate"])
        self.assertLess(phone.actions[1][2]["actions"][0]["actions"][0]["y"], 100)

    def test_an_action_with_no_answer_ends_the_run_and_sends_nothing_after_it(self):
        phone = Phone([HOME, HOME], fail_at=0)
        with self.assertRaises(CouldntRun) as caught:
            self.drive(phone, [{"surface": "control_center", "expect": [{"text": "App 1"}]}])
        self.assertEqual(caught.exception.klass, "unknown_outcome")
        self.assertEqual(phone.actions, [])

    def test_a_phone_is_refused_before_anything_is_sent(self):
        phone = Phone([HOME])
        with self.assertRaises(CouldntRun) as caught:
            self.drive(phone, [{"surface": "control_center", "expect": [{"text": "x"}]}], real=True)
        self.assertEqual(caught.exception.klass, "environment")
        self.assertTrue(caught.exception.message.startswith("Not on a simulator"))
        self.assertEqual(phone.actions, [])

    def test_an_app_clip_card_has_no_simulator_path(self):
        phone = Phone([HOME])
        with self.assertRaisesRegex(CouldntRun, "Not on a simulator: an App Clip card opened by nfc"):
            self.drive(phone, [{"surface": "app_clip", "bundle": "a.b.Clip", "url": "https://a.b", "via": "nfc",
                                "expect": [{"text": "x"}]}])
        self.assertEqual(phone.actions, [])

    def test_off_means_off(self):
        run = RunFake(Phone([HOME]), [one(surface="widget", expect=[{"text": "x"}])])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(spec.FLAG, None)
            with self.assertRaisesRegex(CouldntRun, "off"):
                play.drive(run)


class VerifyTests(RunnerCase):
    def test_a_run_drives_its_surfaces_after_the_flow_and_decides_on_them(self):
        shown = board(*icons(), node("Other", "12 day streak", rect=(20, 400, 160, 160)))
        for expect, verdict in (("12 day streak", "passed"), ("99 day streak", "failed")):
            with self.subTest(verdict), flagged():
                check = check_from_dict({"app": {"bundle": BUNDLE},
                                         "surfaces": [{"surface": "control_center", "expect": [{"text": expect}]}]})
                phone = Phone([APP_SCREEN, shown, shown, APP_SCREEN])
                result, manager = self.verify(check, phone, mode="launch")
                self.assertEqual(result["verdict"], verdict, result)
                self.assertEqual(phone.kinds(), ["drag", "activate"])
                if verdict == "passed":
                    self.assertIn("1 of 1 system surfaces held", result["reason"]["message"])
                else:
                    self.assertEqual(result["reason"]["class"], "finding")

    def test_mobster_test_drives_surfaces_too(self):
        from mobile_agent.testkit import execute
        from mobile_agent.tests.test_verify_runner import Manager
        from mobile_agent.verify import runner
        shown = board(*icons(), node("Other", "12 day streak", rect=(20, 400, 160, 160)))
        with flagged():
            surface = {"surface": "control_center", "expect": [{"text": "99 day streak"}]}
            check = check_from_dict({"app": {"bundle": BUNDLE}, "surfaces": [surface]})
        phone, manager = Phone([APP_SCREEN, shown, shown, APP_SCREEN]), Manager()
        self.watch(phone, manager)
        with flagged(), mock.patch.object(runner, "build_driver", lambda target, m, run_dir, mode_: phone):
            result = execute.Attempts(runs_dir=self.runs, simulators=manager, keyless=True)(check, None, label="x")
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "finding"), result)
        self.assertEqual(phone.kinds(), ["drag", "activate"])

    def test_a_check_with_only_surfaces_is_not_empty(self):
        with flagged():
            check = check_from_dict({"app": {"bundle": BUNDLE},
                                     "surfaces": [{"surface": "control_center", "expect": [{"text": "nothing"}]}]})
        self.assertTrue(check.surfaces)
        self.assertFalse(check.expect)


if __name__ == "__main__":
    unittest.main()
