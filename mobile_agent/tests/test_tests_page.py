"""A project's testing sections in the Mac app, over the local API: heals to review with their frames
(testkit/heal_review.py), the snapshot rows' stub, a schedule turned off on this Mac (testkit/health.py), fixtures
listed and applied to a simulator (devkit/fixtures_routes.py) and each deep link's last check
(devkit/links_routes.py). Each route that acts refuses without the Mac app's session and while
MOBSTER_EXPERIMENTAL_TESTS_PAGE is off. Files in a temporary project, a fake
simulator manager; no simulator, no phone, no network."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import api_routes
from mobile_agent.api_errors import APIError
from mobile_agent.devkit import fixtures_routes, links_routes
from mobile_agent.replay_core import heal as heals
from mobile_agent.testkit import heal_review, health, register

CHECK = "name: {name}\napp:\n  bundle: dev.mobster.daybreak\nsteps:\n  - Tap Continue\nexpect:\n  - text: Hi\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
CACHE = "0123456789abcdef-01234567.json"
ON = {register.PAGE_FLAG: "1"}


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = (Path(folder.name) / "Daybreak").resolve()
        (self.root / ".mobster" / "checks").mkdir(parents=True)
        self.data = Path(folder.name) / "data"
        environ = mock.patch.dict(os.environ, {"MOBSTER_DATA_DIR": str(self.data), **ON})
        environ.start()
        self.addCleanup(environ.stop)
        known = mock.patch("mobile_agent.testkit.recent.known_roots", return_value=[self.root])
        known.start()
        self.addCleanup(known.stop)
        self.runtime = SimpleNamespace(local_api={"app_session": "private"})

    def check(self, name):
        path = self.root / ".mobster" / "checks" / f"{name}.yaml"
        path.write_text(CHECK.format(name=name.capitalize()))
        return path

    def healed(self, name, step=1):
        """A check with one heal waiting, its two frames on disk: an approved recording in the project's replay cache
        and the healed candidate staged against it, as replay_core/checks.py stages one."""
        from mobile_agent.replay_core.checks import identity
        from mobile_agent.replay_core.path import new_path
        from mobile_agent.replay_core.store import Cache, env_of, revision
        from mobile_agent.verify.checks import load_check
        frames = self.root / ".mobster" / "runs" / f"20261024-1000{step:02d}-abcd" / "frames"
        frames.mkdir(parents=True, exist_ok=True)
        (frames / "before.png").write_bytes(PNG)
        (frames / "after.png").write_bytes(PNG)
        check = self.check(name)
        loaded = load_check(check)
        # Its own build number per step, so two checks healed here keep apart recordings.
        cache = Cache(self.root / ".mobster" / "cache" / "replay")
        approved = new_path([{"op": "HOME"}] * step, kind="check", identity=identity(loaded),
                            env=env_of(f"2.1 ({step})", "iOS 26.4", "simulator", device_type="iPhone 17 Pro"))
        cache_file = cache.save(approved)
        candidate = {**approved, "steps": [{"op": "HOME"}] * (step + 1)}
        heals.record(check, [{"step": step, "from": 'Tap "Continue"', "to": 'Tap "Next"', "why": "the label changed",
                              "old": [{"op": "HOME"}], "new": [{"op": "HOME"}, {"op": "HOME"}],
                              "frames": {"before": str(frames / "before.png"), "after": str(frames / "after.png")}}],
                     run_id="20261024-100001-abcd", cache_file=cache_file, candidate=candidate,
                     base_revision=revision(approved), check_sha256=heals._check_digest(check),
                     effective_sha256=heals.effective_digest(loaded), proof=heals.proof_assets(loaded),
                     app={"version": "2.1"}, ios="26.4", device="iPhone 17 Pro")
        return check

    def request(self, body=None, query=None, app_session=True, method="GET"):
        return SimpleNamespace(runtime=self.runtime, method=method, body=body, app_session=app_session,
                               query={key: [value] for key, value in (query or {}).items()})


class Heals(Base):
    def test_the_page_lists_each_waiting_heal_with_its_step_before_and_after(self):
        self.healed("paywall")
        self.check("settings")
        [project] = heal_review.listed(self.request()).value["projects"]
        self.assertEqual((project["project"], project["root"]), ("Daybreak", str(self.root)))
        [check] = project["checks"]
        self.assertEqual((check["name"], check["file"], check["diff"]),
                         ("Paywall", ".mobster/checks/paywall.yaml", ".mobster/checks/paywall.heal.yaml"))
        self.assertEqual((check["app"], check["ios"], check["device"]), ("2.1", "26.4", "iPhone 17 Pro"))
        [step] = check["heals"]
        self.assertEqual((step["step"], step["from"], step["to"], step["why"]),
                         (1, 'Tap "Continue"', 'Tap "Next"', "the label changed"))
        self.assertEqual(step["frames"]["before"], ".mobster/runs/20261024-100001-abcd/frames/before.png")

    def test_a_heal_file_that_cant_be_read_is_named_and_lists_none(self):
        check = self.healed("paywall")
        heals.heal_file(check).write_text("heals: [not, closed")
        [project] = heal_review.listed(self.request()).value["projects"]
        self.assertEqual(project["checks"], [])
        self.assertIn("paywall.heal.yaml can't be read", project["problem"])

    def test_a_frame_is_served_only_when_a_waiting_heal_names_it(self):
        self.healed("paywall")
        named = ".mobster/runs/20261024-100001-abcd/frames/after.png"
        answer = heal_review.frame(self.request(query={"project": str(self.root), "path": named}))
        self.assertEqual((answer.status, answer.content_type, answer.data), (200, "image/png", PNG))
        (self.root / ".mobster" / "runs" / "other.png").write_bytes(PNG)
        (self.root / "secret.png").write_bytes(PNG)
        for path in (".mobster/runs/other.png", "secret.png", "../secret.png", ".mobster/checks/paywall.yaml"):
            with self.subTest(path=path):
                refused = heal_review.frame(self.request(query={"project": str(self.root), "path": path}))
                self.assertEqual((refused.status, refused.value["code"]), (404, "frame_not_found"))
        heals.heal_file(self.root / ".mobster" / "checks" / "paywall.yaml").unlink()
        gone = heal_review.frame(self.request(query={"project": str(self.root), "path": named}))
        self.assertEqual(gone.status, 404)

    def test_accept_settles_the_checks_named_and_reject_the_rest(self):
        paywall, settings = self.healed("paywall"), self.healed("settings", step=2)
        body = {"project": str(self.root), "action": "accept", "checks": [".mobster/checks/paywall.yaml"]}
        done = heal_review.settled(self.request(body, method="POST")).value
        self.assertEqual(done, {"action": "accept", "settled": [
            {"name": "Paywall", "file": ".mobster/checks/paywall.yaml", "steps": 1}]})
        self.assertFalse(heals.heal_file(paywall).exists())
        self.assertTrue(heals.heal_file(settings).exists())
        rest = heal_review.settled(self.request({"project": str(self.root), "action": "reject"}, method="POST"))
        self.assertEqual([item["file"] for item in rest.value["settled"]], [".mobster/checks/settings.yaml"])
        self.assertFalse(heals.heal_file(settings).exists())
        self.assertEqual(heal_review.listed(self.request()).value["projects"][0]["checks"], [])

    def test_settling_refuses_without_the_apps_session_and_for_a_check_with_no_heal(self):
        check = self.healed("paywall")
        body = {"project": str(self.root), "action": "accept"}
        denied = heal_review.settled(self.request(body, app_session=False, method="POST"))
        self.assertEqual((denied.status, denied.value["code"]), (403, "app_only"))
        for wrong in ({**body, "action": "keep"}, {**body, "checks": ["/abs/paywall.yaml"]}, {**body, "extra": 1},
                      {"action": "accept"}):
            with self.subTest(body=wrong):
                self.assertEqual(heal_review.settled(self.request(wrong, method="POST")).status, 400)
        missing = heal_review.settled(self.request({**body, "checks": [".mobster/checks/nope.yaml"]}, method="POST"))
        self.assertEqual((missing.status, missing.value["code"]), (404, "heal_not_found"))
        elsewhere = heal_review.settled(self.request({**body, "project": "/etc"}, method="POST"))
        self.assertEqual(elsewhere.status, 404)
        self.assertTrue(heals.heal_file(check).exists())

    def test_every_route_of_the_page_is_off_without_the_flag(self):
        check = self.healed("paywall")
        with mock.patch.dict(os.environ, {register.PAGE_FLAG: ""}):
            calls = (heal_review.listed(self.request()),
                     heal_review.frame(self.request(query={"project": str(self.root), "path": "x.png"})),
                     heal_review.settled(self.request({"project": str(self.root), "action": "accept"}, method="POST")),
                     register.snapshots(self.request()),
                     register.switch_schedule(self.request({"project": str(self.root), "schedule": "smoke",
                                                            "on": False}, method="POST")),
                     fixtures_routes.applied(self.request({"project": str(self.root), "names": ["clean"]},
                                                          method="POST")))
        for answer in calls:
            self.assertEqual((answer.status, answer.value["code"]), (404, "tests_page_off"))
        self.assertTrue(heals.heal_file(check).exists())

    def test_review_heals_over_mcp_reads_and_settles_the_same_files(self):
        from mobile_agent.testkit.mcp import TestTools
        check = self.healed("paywall")
        toolset = SimpleNamespace(runs_dir=str(self.root / ".mobster" / "runs"))
        listed = TestTools().review_heals({"path": str(self.root / ".mobster" / "checks")}, toolset)
        self.assertEqual(listed.structured["heals"][0]["check"], str(check))
        settled = TestTools().review_heals({"action": "reject"}, toolset)
        self.assertEqual(settled.structured["settled"], [{"check": str(check), "steps": 1}])
        self.assertFalse(heals.heal_file(check).exists())


class Snapshots(Base):
    def test_snapshot_rows_are_empty_until_the_snapshot_assertion_writes_them(self):
        answer = register.snapshots(self.request()).value
        self.assertEqual(answer, {"projects": [{"project": "Daybreak", "root": str(self.root), "snapshots": []}]})
        self.assertEqual(register.snapshots(self.request(query={"project": "/etc"})).status, 404)


class ScheduleSwitch(Base):
    def setUp(self):
        super().setUp()
        (self.root / ".mobster" / health.FILE).write_text("schedules:\n  - name: smoke\n    every: 1h\n")
        self.clock = [1_800_000_000.0]
        self.ran = []

    def runner(self):
        def execute(planned, progress=None, cwd=None, stop=None):
            self.ran.append(planned)
            return {"suite": "20261024-100000-abcd", "exitCode": 0, "summary": {}, "checks": []}, {"folder": None}
        return health.TestSchedules(roots=lambda: [self.root], plan=lambda options, cwd=None: SimpleNamespace(
            entries=[1], targets=[1]), execute=execute, clock=lambda: self.clock[0], spawn=lambda work: work(),
            macos=lambda *a: True, devices=lambda: {})

    def switch(self, on, **kwargs):
        return register.switch_schedule(self.request({"project": str(self.root), "schedule": "smoke", "on": on},
                                                     method="POST", **kwargs))

    def test_a_schedule_turned_off_never_runs_and_turned_on_waits_for_its_next_slot(self):
        runner = self.runner()
        runner.tick_project(self.root)                      # first seen: the slot is an hour away
        self.assertEqual(self.switch(False).value, {"on": False})
        listed = health.listing(self.root, round(self.clock[0] * 1000))["schedules"][0]
        self.assertEqual((listed["on"], listed["nextAt"]), (False, None))
        self.clock[0] += 7200
        runner.tick_project(self.root)
        self.assertEqual(self.ran, [])
        self.assertEqual(self.switch(True).value, {"on": True})
        runner.tick_project(self.root)
        self.assertEqual(self.ran, [], "turned on again, it waits for its next slot")
        self.clock[0] += 3601
        runner.tick_project(self.root)
        self.assertEqual(len(self.ran), 1)
        self.assertNotIn("off", (self.root / ".mobster" / health.FILE).read_text(), "the repository file is unchanged")

    def test_an_unreadable_switch_file_runs_no_schedule(self):
        runner = self.runner()
        runner.tick_project(self.root)
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / health.OFF).write_text("{not json")
        self.clock[0] += 7200
        runner.tick_project(self.root)
        self.assertEqual(self.ran, [])
        found = health.listing(self.root, round(self.clock[0] * 1000))
        self.assertFalse(found["schedules"][0]["on"])
        self.assertIn("no schedule runs", found["problems"][-1])

    def test_switching_needs_the_apps_session_a_known_schedule_and_a_boolean(self):
        self.assertEqual(self.switch(False, app_session=False).status, 403)
        self.assertEqual(self.switch("off").status, 400)
        unknown = register.switch_schedule(self.request({"project": str(self.root), "schedule": "nope", "on": False},
                                                        method="POST"))
        self.assertEqual(unknown.status, 404)
        self.assertEqual(health.read_off(), set())


class Fixtures(Base):
    def setUp(self):
        super().setUp()
        folder = self.root / ".mobster" / "fixtures"
        folder.mkdir()
        (folder / "clean.yaml").write_text("reset: data\n")
        (folder / "args.yaml").write_text("launch:\n  args: [-UITests]\n")

    def test_the_fixtures_route_is_registered_with_the_testkit_routes(self):
        from mobile_agent import harness_api
        found = {}

        def keep(route):
            found[(route.method, route.pattern)] = route
        with mock.patch.object(api_routes, "register", keep), mock.patch.object(fixtures_routes, "register", keep), \
                mock.patch.object(links_routes, "register", keep), \
                mock.patch("mobile_agent.devkit.expo_routes.register_routes"), \
                mock.patch("mobile_agent.mcp_server.registry.register_provider"), \
                mock.patch.object(harness_api, "register_service"):
            register.register(harness_api)
        for key in (("GET", "/api/tests/fixtures"), ("POST", "/api/tests/fixtures/apply"), ("GET", "/api/tests/heals"),
                    ("POST", "/api/tests/heals"), ("GET", "/api/tests/heals/frame"), ("GET", "/api/tests/snapshots"),
                    ("POST", "/api/tests/schedules/switch")):
            self.assertIn(key, found)
        names = [item["name"] for item in fixtures_routes.listed(self.request(query={"project": str(self.root)}))
                 .value["fixtures"]]
        self.assertEqual(sorted(names), ["args", "clean"])

    def test_apply_resets_the_app_on_a_simulator_and_never_waits_for_a_busy_one(self):
        manager = FakeManager()
        with mock.patch("mobile_agent.sim.api.SimulatorManager", return_value=manager), \
                mock.patch("mobile_agent.verify.fixtures.seed_simulator", return_value=[]):
            body = {"project": str(self.root), "names": ["clean"], "bundle": "dev.mobster.daybreak"}
            answer = fixtures_routes.applied(self.request(body, method="POST"))
        self.assertEqual(answer.value["applied"], "Applied clean to dev.mobster.daybreak on iPhone 17 Pro: reset data.")
        self.assertEqual(manager.resets, [("dev.mobster.daybreak", "data")])
        self.assertEqual(manager.timeouts, [0])

    def test_apply_refuses_without_the_apps_session_or_an_app_and_while_another_runs(self):
        body = {"project": str(self.root), "names": ["clean"]}
        denied = fixtures_routes.applied(self.request({**body, "bundle": "x.y.z"}, app_session=False, method="POST"))
        self.assertEqual(denied.status, 403)
        with self.assertRaises(APIError) as caught:
            fixtures_routes.applied(self.request(body, method="POST"))
        self.assertEqual(caught.exception.code, "no_bundle")
        for wrong in ({**body, "names": []}, {**body, "device": "Sam's iPhone"}, {**body, "names": "clean"}):
            with self.subTest(body=wrong), self.assertRaises(APIError) as caught:
                fixtures_routes.applied(self.request(wrong, method="POST"))
            self.assertEqual(caught.exception.code, "bad_request")
        with fixtures_routes._applying, self.assertRaises(APIError) as caught:
            fixtures_routes.applied(self.request({**body, "bundle": "x.y.z"}, method="POST"))
        self.assertEqual(caught.exception.code, "busy")


class FakeManager:
    def __init__(self):
        self.resets, self.timeouts = [], []

    def acquire(self, device=None, runtime=None, *, timeout=600):
        self.timeouts.append(timeout)
        manager = self

        class Lease:
            target = SimpleNamespace(udid="SIM-UDID", name="iPhone 17 Pro")

            def __enter__(self):
                return self.target

            def __exit__(self, *exc):
                return False
        manager.lease = Lease()
        return manager.lease

    def reset(self, target, bundle, how, app_path=None):
        self.resets.append((bundle, how))


class Links(Base):
    def test_each_link_says_how_its_newest_check_went(self):
        (self.root / ".mobster" / "links.yaml").write_text(
            "links:\n  - name: cart\n    url: daybreak://cart\n    title: Your cart\n  - name: profile\n"
            "    url: daybreak://profile\n")
        history = {"schema": "mobster.test-history/1", "checks": {
            ".mobster/links.yaml#cart|sim-a": {"name": "Link: cart", "file": ".mobster/links.yaml#cart",
                                              "device": "iPhone SE", "runs": [{"at": 100, "status": "passed"}]},
            ".mobster/links.yaml#cart|sim-b": {"name": "Link: cart", "file": ".mobster/links.yaml#cart",
                                              "device": "iPhone 17 Pro", "runs": [{"at": 200, "status": "failed"}]}}}
        (self.root / ".mobster" / "test-results").mkdir()
        (self.root / ".mobster" / "test-results" / "history.json").write_text(json.dumps(history))
        links = {item["name"]: item for item in links_routes.listed(
            SimpleNamespace(runtime=None, query={"project": [str(self.root)]})).value["links"]}
        self.assertEqual(links["cart"]["last"], {"status": "failed", "device": "iPhone 17 Pro", "at": 200})
        self.assertIsNone(links["profile"]["last"])


if __name__ == "__main__":
    unittest.main()
