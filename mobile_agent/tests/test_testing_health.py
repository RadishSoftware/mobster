"""testkit/health.py: scheduled health checks on the existing scheduler worker. Schedules from schedule.yaml and a
check's `schedule:`, due slots claimed once, skips recorded with a reason (never queued), results to Slack or a
macOS notification, the CLI and the API. Fakes for planning, running and notifying; offline."""

import contextlib
import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent.testkit import cli, health, register
from mobile_agent.tests.test_server_engine import Base as EngineBase
from mobile_agent.verify.checks import check_from_dict, check_to_dict

CHECK = "name: {name}\napp:\n  bundle: org.mozilla.ios.Firefox\nexpect:\n  - text: Hi\n{extra}"
NOW = 1_800_000_000.0


def doc(exit_code=0, cells=None):
    cells = cells or [{"status": "passed" if exit_code == 0 else "failed"}]
    return {"suite": "20261018-100000-abcd", "exitCode": exit_code, "summary": {"failed": int(exit_code == 1)},
            "checks": [{"name": "Feed", "file": "f.yaml", "results": cells}]}


class Fake:
    """plan/execute stand-ins that record what they were asked to run."""

    def __init__(self, result=None):
        self.options, self.result = [], result or doc()

    def plan(self, options, cwd=None):
        self.options.append(options)
        return SimpleNamespace(entries=[1], targets=[1])

    def execute(self, planned, progress=None, cwd=None, stop=None):
        return self.result, {"folder": Path("/tmp/suite")}


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "App"
        (self.root / ".mobster" / "checks").mkdir(parents=True)
        self.clock = [NOW]
        self.told, self.macos = [], []
        self.fake = Fake()

    def check(self, name, extra=""):
        (self.root / ".mobster" / "checks" / f"{name}.yaml").write_text(CHECK.format(name=name, extra=extra))

    def schedule(self, text):
        (self.root / ".mobster" / health.FILE).write_text(text)

    def runner(self, **kwargs):
        self.fake_devices = kwargs.pop("devices", {})
        return health.TestSchedules(roots=lambda: [self.root], plan=self.fake.plan, execute=self.fake.execute,
                                    clock=lambda: self.clock[0], spawn=lambda work: work(),
                                    notify=lambda *a: self.told.append(a),
                                    macos=lambda *a: self.macos.append(a) or True,
                                    devices=lambda: self.fake_devices, **kwargs)

    def advance(self, seconds):
        self.clock[0] += seconds


class Parse(Base):
    def test_every_and_cron_from_the_file_and_from_a_check(self):
        self.check("feed", "schedule: every 30m\n")
        self.schedule("timezone: UTC\nschedules:\n  - name: smoke\n    every: 1h\n    tags: [smoke]\n"
                      "    sim: iPhone 17 Pro\n  - name: nightly\n    cron: '0 3 * * *'\n")
        specs, problems = health.load(self.root)
        self.assertEqual(problems, [])
        by = {spec.name: spec for spec in specs}
        self.assertEqual((by["smoke"].every_s, by["smoke"].sim, by["smoke"].tags), (3600, "iPhone 17 Pro", ("smoke",)))
        self.assertEqual(by["nightly"].cron, "0 3 * * *")
        self.assertEqual((by["check feed"].every_s, by["check feed"].checks), (1800, (".mobster/checks/feed.yaml",)))

    def test_a_check_keeps_its_schedule_through_the_check_format(self):
        check = check_from_dict({"version": 1, "name": "A", "app": {"bundle": "org.mozilla.ios.Firefox"},
                                 "expect": [{"text": "Hi"}], "schedule": "every 1h"})
        self.assertEqual(check.schedule, "every 1h")
        self.assertEqual(check_to_dict(check)["schedule"], "every 1h")

    def test_bad_schedules_are_named_and_the_good_ones_still_load(self):
        self.schedule("schedules:\n  - name: ok\n    every: 1h\n  - name: fast\n    every: 1m\n"
                      "  - name: both\n    every: 1h\n    cron: '0 * * * *'\n  - name: phone\n    every: 1h\n"
                      "    device: Studio iPhone\n  - name: ok\n    every: 2h\n  - every: 1h\n")
        specs, problems = health.load(self.root)
        self.assertEqual([spec.name for spec in specs], ["ok"])
        text = " ".join(problems)
        for needle in ("5 minutes", "one of them", "never in a repository file", "two schedules are named", "name:"):
            self.assertIn(needle, text)

    def test_a_broken_file_is_a_problem_not_a_crash(self):
        self.schedule("schedules: [")
        specs, problems = health.load(self.root)
        self.assertEqual(specs, [])
        self.assertIn("isn't valid YAML", problems[0])
        self.schedule("tasks: []\n")
        self.assertIn("nothing else", health.load(self.root)[1][0])


class Tick(Base):
    def setUp(self):
        super().setUp()
        self.check("feed")
        self.schedule("timezone: UTC\nschedules:\n  - name: smoke\n    every: 1h\n")

    def state(self):
        return health.read_state(self.root)["smoke"]

    def test_first_sight_waits_for_the_next_slot_then_one_run_when_due(self):
        runner = self.runner()
        runner.tick_project(self.root)
        self.assertEqual(self.fake.options, [])
        self.assertEqual(self.state()["nextAt"], round((NOW + 3600) * 1000))
        self.advance(3601)
        runner.tick_project(self.root)
        runner.tick_project(self.root)       # the slot was claimed: no second run
        self.assertEqual(len(self.fake.options), 1)
        self.assertEqual(self.state()["runs"][-1]["outcome"], "ran")
        self.assertEqual(self.state()["runs"][-1]["verdict"], "passed")

    def test_a_slot_the_mac_slept_through_is_skipped_with_the_reason_and_not_made_up(self):
        runner = self.runner()
        runner.tick_project(self.root)
        self.advance(3600 * 5)
        runner.tick_project(self.root)
        self.assertEqual(self.fake.options, [])
        last = self.state()["runs"][-1]
        self.assertEqual(last["outcome"], "skipped")
        self.assertIn("slept", last["reason"])
        self.assertGreater(self.state()["nextAt"], round(self.clock[0] * 1000))

    def test_a_run_going_means_the_next_is_skipped_not_queued(self):
        queued = []
        runner = self.runner()
        runner.spawn = queued.append          # the first run is "going": its work hasn't run
        spec = health.load(self.root)[0][0]
        self.assertEqual(runner.start(spec, trigger="schedule")["outcome"], "started")
        second = runner.start(spec, trigger="schedule")
        self.assertEqual(second["outcome"], "skipped")
        self.assertIn("still going", second["reason"])
        self.assertEqual(len(queued), 1)       # nothing was queued for later
        queued[0]()
        self.assertEqual(self.state()["runs"][0]["outcome"], "skipped")
        self.assertEqual(self.state()["runs"][-1]["outcome"], "ran")

    def test_the_guard_can_refuse_a_phone_run_and_a_simulator_run_needs_no_guard(self):
        asked = []
        runner = self.runner(guard=lambda spec, device: asked.append(device) or "No plan on this Mac.")
        spec = health.load(self.root)[0][0]
        self.assertEqual(runner.start(spec, trigger="schedule", wait=True)["outcome"], "ran")   # simulator: no guard
        self.assertEqual(asked, [])
        answer = runner.start(spec, trigger="run-now", device="Studio iPhone", wait=True)
        self.assertEqual((answer["outcome"], answer["reason"]), ("skipped", "No plan on this Mac."))
        self.assertEqual(asked, ["Studio iPhone"])
        self.assertEqual(len(self.fake.options), 1)

    def test_a_device_held_by_another_process_is_a_skip_with_its_message(self):
        busy = {"status": "couldnt_run", "reason": {"class": "busy", "message": "Studio iPhone is in use."}}
        self.fake.result = doc(3, [busy])
        answer = self.runner().start(health.load(self.root)[0][0], trigger="schedule", wait=True)
        self.assertEqual((answer["outcome"], answer["reason"]), ("skipped", "Studio iPhone is in use."))
        self.assertEqual(self.macos, [])

    def test_a_failure_goes_to_the_slack_rule_when_there_is_one_else_the_mac(self):
        self.fake.result = doc(1)
        runner = self.runner()
        spec = health.load(self.root)[0][0]
        runner.start(spec, trigger="schedule", wait=True)
        self.assertEqual((len(self.told), len(self.macos)), (0, 1))
        self.assertIn("smoke didn't pass", self.macos[0][0])
        (self.root / ".mobster" / "notifications.yaml").write_text("slack:\n  destination: Team\n")
        runner.start(spec, trigger="schedule", wait=True)
        self.assertEqual((len(self.told), len(self.macos)), (1, 1))

    def test_a_pass_tells_nobody(self):
        self.runner().start(health.load(self.root)[0][0], trigger="schedule", wait=True)
        self.assertEqual((self.told, self.macos), ([], []))

    def test_a_project_that_cant_plan_is_a_skip(self):
        from mobile_agent.testkit.api import UsageError

        def refuse(options, cwd=None):
            raise UsageError("No check has the tag smoke.")
        runner = self.runner()
        runner._plan = refuse
        answer = runner.start(health.load(self.root)[0][0], trigger="schedule", wait=True)
        self.assertEqual(answer["outcome"], "skipped")
        self.assertIn("No check has the tag", answer["reason"])

    def test_a_corrupt_state_file_starts_again(self):
        path = health.state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{nope")
        self.assertEqual(health.read_state(self.root), {})
        self.runner().tick_project(self.root)
        self.assertIn("nextAt", self.state())

    def test_runs_are_bounded(self):
        for n in range(health.KEEP_RUNS + 5):
            health.record(self.root, "smoke", n, "skipped", f"r{n}")
        runs = self.state()["runs"]
        self.assertEqual((len(runs), runs[0]["reason"]), (health.KEEP_RUNS, "r5"))

    def test_a_schedule_taken_out_of_the_files_leaves_the_state(self):
        runner = self.runner()
        runner.tick_project(self.root)
        self.schedule("schedules: []\n")
        runner.tick_project(self.root)
        self.assertEqual(health.read_state(self.root), {})

    def test_the_tick_is_throttled_and_never_raises(self):
        runner = health.TestSchedules(roots=lambda: 1 / 0, clock=lambda: NOW)
        runner.tick()
        runner.tick()

    def test_a_failed_durable_claim_never_starts_the_suite(self):
        runner = self.runner()
        runner.tick_project(self.root)
        self.advance(3601)
        with mock.patch.object(health, "write_text_atomic", side_effect=OSError("disk full")):
            runner.tick_project(self.root)
        self.assertEqual(self.fake.options, [])

    def test_a_closed_scheduler_refuses_a_new_run(self):
        runner = self.runner()
        runner.close()
        answer = runner.start(health.load(self.root)[0][0], trigger="run-now", wait=True)
        self.assertEqual(answer["outcome"], "skipped")
        self.assertEqual(self.fake.options, [])

    def test_close_cancels_and_waits_for_its_background_suite(self):
        entered, finished, release = threading.Event(), threading.Event(), threading.Event()
        seen = []
        def execute(planned, progress=None, cwd=None, stop=None):
            seen.append((threading.current_thread(), stop))
            entered.set()
            (stop if stop is not None else release).wait(1)
            finished.set()
            return doc(), {}
        runner = health.TestSchedules(plan=self.fake.plan, execute=execute, devices=lambda: {},
                                      macos=lambda *a: True, clock=lambda: self.clock[0])
        try:
            runner.start(health.load(self.root)[0][0], trigger="run-now")
            self.assertTrue(entered.wait(1))
            runner.close()
            self.assertTrue(finished.is_set())
            self.assertIs(seen[0][1], runner.stop)
            self.assertFalse(seen[0][0].is_alive())
        finally:
            release.set()
            runner.stop.set()
            for thread, _ in seen:
                thread.join(2)


class Worker(unittest.TestCase):
    def test_the_workflow_scheduler_worker_runs_the_schedules_and_survives_their_errors(self):
        import threading

        from mobile_agent.workflows.scheduler import WorkflowScheduler
        ticks, done = [], threading.Event()

        def also():
            ticks.append(1)
            if len(ticks) == 1:
                raise RuntimeError("boom")
            done.set()
        workflows = SimpleNamespace(tick=lambda: None, list=lambda: [], now=lambda: 0, recover=lambda: None)
        keep = SimpleNamespace(hold=lambda: None, release=lambda: None)
        scheduler = WorkflowScheduler(workflows, keep_awake=keep, also=also)
        scheduler.stop.wait = lambda timeout=None: scheduler.stop.is_set() or (done.wait(0.01) and scheduler.stop.set())
        scheduler.start()
        self.assertTrue(done.wait(5))
        scheduler.close()
        self.assertGreaterEqual(len(ticks), 2)

    def test_the_runtime_guard_refuses_without_a_plan_and_when_a_task_holds_the_phone(self):
        gate = SimpleNamespace(refusal=lambda: "Start your trial to use a phone.")
        runtime = SimpleNamespace(plan_gate=gate, device_busy=lambda device_id: True)
        self.assertEqual(register.runtime_guard(runtime)(None, "Studio iPhone"), "Start your trial to use a phone.")
        runtime.plan_gate = SimpleNamespace(refusal=lambda: None)
        with mock.patch("mobile_agent.device_targets.named_device", return_value={"id": "usb-1"}):
            self.assertIn("not queued", register.runtime_guard(runtime)(None, "Studio iPhone"))
            runtime.device_busy = lambda device_id: False
            self.assertIsNone(register.runtime_guard(runtime)(None, "Studio iPhone"))


class Surfaces(Base):
    def setUp(self):
        super().setUp()
        self.check("feed")
        self.schedule("timezone: UTC\nschedules:\n  - name: smoke\n    every: 1h\n")

    def test_the_cli_lists_and_runs_now_on_its_own_simulator_and_never_the_attached_phone(self):
        out, err = io.StringIO(), io.StringIO()
        args = SimpleNamespace(json=False, sim=None, device=None)
        self.assertEqual(cli.schedule(args, ["list"], cwd=self.root, out=out, err=err), 0)
        self.assertIn("smoke", out.getvalue())
        self.assertIn("never ran", out.getvalue())
        runner = self.runner(devices={health.device_key(self.root, "smoke"): "Studio iPhone"})
        runner.devices = lambda: {}
        out = io.StringIO()
        code = cli.schedule(args, ["run-now", "smoke"], cwd=self.root, out=out, err=err, schedules=runner)
        self.assertEqual(code, 0)
        self.assertIn("smoke: passed", out.getvalue())
        self.assertEqual(self.fake.options[-1].devices, ())

    def test_the_cli_refuses_an_unknown_schedule_and_a_skipped_run_exits_3(self):
        err = io.StringIO()
        args = SimpleNamespace(json=False, sim=None, device=None)
        with contextlib.redirect_stderr(io.StringIO()) as usage:
            self.assertEqual(cli.schedule(args, ["run-now", "nope"], cwd=self.root, err=err), 3)
        self.assertIn("known: smoke", usage.getvalue())
        busy = {"status": "couldnt_run", "reason": {"class": "busy", "message": "In use."}}
        self.fake.result = doc(3, [busy])
        err = io.StringIO()
        code = cli.schedule(args, ["run-now", "smoke"], cwd=self.root, out=io.StringIO(), err=err,
                            schedules=self.runner())
        self.assertEqual(code, 3)
        self.assertIn("was skipped. In use.", err.getvalue())

    def test_the_api_lists_schedules_only_for_known_projects_and_runs_now(self):
        runtime = SimpleNamespace(test_schedules=self.runner())
        with mock.patch("mobile_agent.testkit.recent.known_roots", return_value=[self.root]):
            listed = register.list_schedules(SimpleNamespace(query={})).value
            self.assertEqual(listed["projects"][0]["schedules"][0]["name"], "smoke")
            other = register.list_schedules(SimpleNamespace(query={"project": ["/etc"]}))
            self.assertEqual(other.status, 404)
            ok = register.run_schedule_now(SimpleNamespace(
                runtime=runtime, body={"project": str(self.root), "schedule": "smoke"}, query={}))
            self.assertEqual(ok.status, 202)
            self.assertEqual(len(self.fake.options), 1)
            missing = register.run_schedule_now(SimpleNamespace(
                runtime=runtime, body={"project": str(self.root), "schedule": "nope"}, query={}))
            self.assertEqual(missing.status, 404)
            extra = register.run_schedule_now(SimpleNamespace(
                runtime=runtime, body={"project": str(self.root), "schedule": "smoke", "x": 1}, query={}))
            self.assertEqual(extra.status, 400)

    def test_the_attached_device_is_kept_outside_the_project(self):
        with tempfile.TemporaryDirectory() as data:
            health.set_device(self.root, "smoke", "Studio iPhone", data_dir=data)
            self.assertEqual(health.read_devices(data), {health.device_key(self.root, "smoke"): "Studio iPhone"})
            health.set_device(self.root, "smoke", None, data_dir=data)
            self.assertEqual(health.read_devices(data), {})
        self.assertNotIn("iPhone", (self.root / ".mobster" / health.FILE).read_text())

    def test_schedule_mutations_require_the_configured_private_app_session(self):
        runtime = SimpleNamespace(test_schedules=self.runner(), local_api={"app_session": "private"})
        for call, extra in ((register.run_schedule_now, {}), (register.set_schedule_device, {"device": "Test iPhone"})):
            request = SimpleNamespace(runtime=runtime, body={"project": str(self.root), "schedule": "smoke", **extra},
                                      query={}, origin="app", app_session=False)
            with self.subTest(call=call.__name__), mock.patch("mobile_agent.testkit.recent.known_roots",
                                                              return_value=[self.root]), \
                    mock.patch.object(health, "set_device") as attach:
                answer = call(request)
                self.assertEqual(answer.status, 403)
                attach.assert_not_called()
        self.assertEqual(self.fake.options, [])


class RuntimeShutdown(EngineBase):
    def test_runtime_owns_the_scheduled_check_service(self):
        runtime = self.runtime()
        self.assertIsInstance(runtime.test_schedules, health.TestSchedules)

    def test_an_unfinished_scheduled_suite_keeps_runtime_ownership_before_phone_shutdown(self):
        runtime = self.runtime()
        unfinished = SimpleNamespace(close=mock.Mock(return_value=False))
        manager = mock.Mock()
        with mock.patch.object(runtime, "test_schedules", unfinished, create=True), \
                mock.patch.object(runtime, "manager", manager):
            self.assertFalse(runtime.close(timeout=.01))
            manager.begin_close.assert_not_called()
            self.assertEqual(runtime.journal.connection.execute("SELECT 1").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
