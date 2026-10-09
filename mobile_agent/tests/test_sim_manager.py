"""SimulatorManager over faked simctl, WebDriverAgent and leases: acquire, reuse, busy, housekeeping, apps."""

import datetime
import fcntl
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from mobile_agent.journal import LeaseHeld
from mobile_agent.sim import AppInfo, SimError, SimLease, SimTarget, SimulatorManager, api
from mobile_agent.sim.simctl import Result
from mobile_agent.sim.wda import build_dir
from mobile_agent.tests.test_sim_fakes import RUNTIME_ID, TOOLS, fake_manager, make_app

NAME = "Mobster · iPhone 17 Pro · iOS 26.4"


class InterfaceTests(unittest.TestCase):
    def test_the_package_exports_the_spec_names(self):
        import mobile_agent.sim as sim
        for name in ("SimError", "AppInfo", "SimTarget", "SimLease", "SimulatorManager", "SIM_KINDS"):
            self.assertIs(getattr(sim, name), getattr(api, name))
        self.assertEqual(api.SIM_KINDS, ("environment", "simulator", "wda", "install", "launch", "busy"))

    def test_sim_error_carries_kind_and_fix(self):
        error = SimError("busy", "All are running checks.", "Wait.")
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual((error.kind, error.fix, str(error)), ("busy", "Wait.", "All are running checks."))
        self.assertEqual(SimError("wda", "x").fix, "")

    def test_signatures(self):
        import inspect
        expected = {
            "__init__": ["self", "data_dir", "progress"],
            "acquire": ["self", "device", "runtime", "timeout"],
            "app_info": ["self", "app_path"], "installed_app": ["self", "target", "bundle_id"],
            "install": ["self", "target", "app_path"], "reset": ["self", "target", "bundle_id", "level", "app_path"],
            "launch": ["self", "target", "bundle_id", "args", "env"], "terminate": ["self", "target", "bundle_id"],
            "open_url": ["self", "target", "url"], "screenshot": ["self", "target", "path", "max_width", "quality"],
            "restarter": ["self", "target"], "status": ["self"], "doctor": ["self", "fix"], "list": ["self"],
            "shutdown": ["self", "udid"], "erase": ["self", "udid"], "delete": ["self", "udid"], "prune": ["self"]}
        for name, parameters in expected.items():
            with self.subTest(method=name):
                self.assertEqual(list(inspect.signature(getattr(SimulatorManager, name)).parameters), parameters)
        acquire = inspect.signature(SimulatorManager.acquire).parameters
        self.assertEqual((acquire["timeout"].kind, acquire["timeout"].default), (inspect.Parameter.KEYWORD_ONLY, 600))

    def test_lease_release_is_idempotent_and_a_context_manager(self):
        closed = []

        class Lease:
            def close(self):
                closed.append(1)
        target = SimTarget("U", NAME, "iPhone 17 Pro", "iOS 26.4", "http://127.0.0.1:8310", "http://127.0.0.1:9310",
                           "/x.xctestrun")
        lease = SimLease(target, [Lease()])
        with lease as entered:
            self.assertIs(entered, target)
        lease.release()
        self.assertEqual(closed, [1])
        self.assertTrue(lease.released)


class AcquireTests(unittest.TestCase):
    def test_cold_acquire_creates_boots_configures_builds_and_starts(self):
        messages = []
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.progress = messages.append
            lease = manager.acquire()
            target = lease.target
            self.assertEqual(target.name, NAME)
            self.assertEqual((target.device_type, target.runtime), ("iPhone 17 Pro", "iOS 26.4"))
            self.assertEqual((target.wda_url, target.mjpeg_url), ("http://127.0.0.1:8310", "http://127.0.0.1:9310"))
            self.assertEqual(target.xctestrun, str(wda.xctestrun))
            self.assertEqual(simctl.simctl_calls("create"),
                             [[NAME, "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro", RUNTIME_ID]])
            self.assertEqual(simctl.simctl_calls("boot"), [[target.udid]])
            self.assertEqual(simctl.simctl_calls("bootstatus"), [[target.udid, "-b"]])
            self.assertEqual(simctl.simctl_calls("status_bar"), [[target.udid, "override", *api.STATUS_BAR]])
            self.assertEqual(sorted(simctl.simctl_calls("spawn")), sorted(
                [[target.udid, "defaults", "write", "com.apple.Preferences", key, "-bool", "false"]
                 for key in ("KeyboardAutocorrection", "KeyboardPrediction", "SmartQuotesEnabled",
                             "SmartDashesEnabled")] +
                [[target.udid, "defaults", "write", "com.apple.keyboard.preferences", key, "-bool", value]
                 for key, value in (("DidShowContinuousPathIntroduction", "true"),
                                    ("DidShowGestureKeyboardIntroduction", "true"),
                                    ("KeyboardDidShowProductivityTutorial", "true"),
                                    ("KeyboardContinuousPathEnabled", "false"))]))
            self.assertEqual(wda.http.requests, [("GET", "/alert/text", None)])  # the sweep: one read, no alert
            self.assertEqual(wda.built, 1)
            self.assertEqual(wda.started, [(target.udid, 8310, 9310)])
            self.assertEqual(leases.held, {"http://127.0.0.1:8310"})
            self.assertEqual(set(lease.timing), {"create", "boot", "configure", "wda_build", "wda_start", "total"})
            self.assertTrue(any(message.startswith(f"Simulator: {NAME} (booted in ") for message in messages))
            entry = manager.registry.read()[0]
            self.assertEqual(list(entry), ["udid", "name", "device_type", "runtime", "wda_port", "mjpeg_port",
                                           "created_at", "last_used_at"])
            self.assertEqual((entry["udid"], entry["wda_port"], entry["mjpeg_port"]), (target.udid, 8310, 9310))
            lease.release()
            lease.release()
            self.assertEqual(leases.held, set())
            self.assertFalse(any("Simulator.app" in " ".join(call) or call[:1] == ["open"] for call in simctl.calls))

    def test_warm_acquire_reuses_without_boot_or_start(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.acquire().release()
            simctl.calls.clear()
            wda.http.requests.clear()
            wda.plans = ["reuse"]
            with manager.acquire() as target:
                self.assertEqual(target.wda_url, "http://127.0.0.1:8310")
            self.assertEqual(simctl.simctl_calls("create") + simctl.simctl_calls("boot"), [])
            self.assertEqual(simctl.calls[1:], [])  # the one `simctl list`, and no settings: the runner was up
            self.assertEqual(len(wda.started), 1)
            self.assertEqual(wda.http.requests, [("GET", "/alert/text", None)])

    def test_a_booted_simulator_that_never_got_its_settings_gets_them_when_its_runner_starts(self):
        # The first boot ran out of time; the simulator finished booting afterwards, never configured.
        with fake_manager() as (manager, simctl, wda, leases, root):
            simctl.fail["bootstatus"] = [Result(None, "", "/usr/bin/xcrun did not finish in 240 s")]
            with self.assertRaises(SimError):
                manager.acquire()
            identifier = manager.registry.read()[0]["udid"]
            self.assertEqual(simctl.devices[identifier]["state"], "Booted")
            self.assertEqual(simctl.simctl_calls("status_bar") + simctl.simctl_calls("spawn"), [])
            simctl.calls.clear()
            lease = manager.acquire()
            lease.release()
            self.assertEqual(simctl.simctl_calls("boot"), [])
            self.assertEqual(simctl.simctl_calls("status_bar"), [[identifier, "override", *api.STATUS_BAR]])
            self.assertEqual(len(simctl.simctl_calls("spawn")), 8)
            self.assertIn("configure", lease.timing)
            simctl.calls.clear()
            wda.plans = ["restart"]  # a runner restarted on a booted simulator writes them again
            manager.acquire().release()
            self.assertEqual(len(simctl.simctl_calls("status_bar") + simctl.simctl_calls("spawn")), 9)

    def test_acquire_dismisses_an_alert_left_on_screen(self):
        for plan in ("reuse", "start", "restart"):
            with self.subTest(plan=plan), fake_manager() as (manager, simctl, wda, leases, root):
                manager.acquire().release()
                wda.http.requests.clear()
                wda.http.show("Open in “Daybreak”?", ("Cancel", "Open"))
                messages = []
                manager.progress = messages.append
                wda.plans = [plan]
                with manager.acquire():
                    self.assertEqual(wda.http.alerts, [])
                self.assertEqual(wda.http.pressed, [("Open in “Daybreak”?", "Cancel")])
                self.assertEqual(wda.http.paths(), ["/alert/text", "/alert/dismiss", "/alert/text"])
                self.assertIn(f"Dismissing an alert left on {NAME}: “Open in “Daybreak”?”", messages)

    def test_acquire_dismisses_alerts_stacked_behind_each_other(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            wda.http.show("Allow “Daybreak” to send you notifications?", ("Don’t Allow", "Allow"))
            wda.http.show("Open in “Daybreak”?", ("Cancel", "Open"))
            manager.acquire().release()
            self.assertEqual(wda.http.pressed, [("Open in “Daybreak”?", "Cancel"),
                                                ("Allow “Daybreak” to send you notifications?", "Don’t Allow")])
            self.assertEqual(wda.http.alerts, [])

    def test_an_alert_that_stays_fails_acquire_and_frees_the_lease(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            wda.http.show("Software Update", ("Later",), sticky=True)
            with self.assertRaises(SimError) as caught:
                manager.acquire()
            self.assertEqual(caught.exception.kind, "simulator")
            self.assertIn("“Software Update”", str(caught.exception))
            self.assertIn("mobster sim erase", caught.exception.fix)
            self.assertEqual(len(wda.http.pressed), api.SWEEP_ROUNDS)
            self.assertEqual(leases.held, set())

    def test_a_runner_that_does_not_answer_the_alert_read_is_not_an_alert(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            wda.http.answering = False
            with manager.acquire() as target:
                self.assertEqual(target.wda_url, "http://127.0.0.1:8310")
            self.assertEqual(wda.http.paths(), ["/alert/text"])

    def test_a_second_run_gets_a_second_simulator_on_the_next_slot(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            first = manager.acquire()
            second = manager.acquire()
            self.assertEqual(second.target.name, NAME + " · 2")
            self.assertEqual(second.target.wda_url, "http://127.0.0.1:8311")
            self.assertEqual(second.target.mjpeg_url, "http://127.0.0.1:9311")
            self.assertNotEqual(first.target.udid, second.target.udid)
            first.release()
            second.release()

    def test_a_lease_held_by_a_list_probe_is_looked_at_again_before_creating(self):
        # Review round 3: `list()` (and doctor, and an MCP status call) takes and releases each lease to see
        # whether it is in use. An acquire landing in those milliseconds must not cold-boot a second simulator.
        with fake_manager() as (manager, simctl, wda, leases, root):
            first = manager.acquire()
            first.release()
            url, looked, sleeps = first.target.wda_url, [], []

            def lease(wanted):
                looked.append(wanted)
                if wanted == url and looked.count(url) == 1:  # the probe holds it during the first look
                    raise LeaseHeld("Another Mobster process already owns this resource")
                return leases(wanted)
            manager.lease = lease
            manager.sleep = sleeps.append
            with manager.acquire() as target:
                self.assertEqual(target.udid, first.target.udid)
            self.assertEqual(sleeps, [api.PROBE_RETRY])
            self.assertEqual(len(simctl.simctl_calls("create")), 1)
            self.assertEqual([entry["udid"] for entry in manager.registry.read()], [first.target.udid])

    def test_a_lease_a_run_holds_costs_one_short_look_before_the_second_simulator(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            first = manager.acquire()
            sleeps = []
            manager.sleep = sleeps.append
            second = manager.acquire()
            self.assertEqual(sleeps, [api.PROBE_RETRY])
            self.assertLessEqual(api.PROBE_RETRY, 0.2)
            self.assertNotEqual(second.target.udid, first.target.udid)
            first.release()
            second.release()

    def test_no_second_look_when_nothing_would_be_created(self):
        with fake_manager({"MOBSTER_MAX_SIMS": "1"}) as (manager, simctl, wda, leases, root):
            sleeps = []
            manager.sleep = sleeps.append
            held = manager.acquire()  # an empty registry: nothing to look at twice
            with self.assertRaises(SimError):
                manager.acquire(timeout=0)  # all busy at the limit: acquire's own wait, not the second look
            held.release()
            self.assertEqual(sleeps, [])

    def test_busy_when_every_simulator_is_in_use(self):
        with fake_manager({"MOBSTER_MAX_SIMS": "1"}) as (manager, simctl, wda, leases, root):
            held = manager.acquire()
            with self.assertRaises(SimError) as caught:
                manager.acquire(timeout=0)
            self.assertEqual(caught.exception.kind, "busy")
            self.assertIn("MOBSTER_MAX_SIMS", caught.exception.fix)
            self.assertEqual(len(simctl.simctl_calls("create")), 1)
            held.release()
            with manager.acquire(timeout=0) as target:
                self.assertEqual(target.udid, held.target.udid)

    def test_waits_for_a_lease_then_takes_it(self):
        with fake_manager({"MOBSTER_MAX_SIMS": "1"}) as (manager, simctl, wda, leases, root):
            held = manager.acquire()
            sleeps = []

            def sleep(seconds):
                sleeps.append(seconds)
                held.release()
            manager.sleep = sleep
            with manager.acquire(timeout=30) as target:
                self.assertEqual(target.udid, held.target.udid)
            self.assertEqual(len(sleeps), 1)

    def test_default_max_is_two(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            self.assertEqual(manager.max_sims, 2)
            leases_ = [manager.acquire(), manager.acquire()]
            with self.assertRaises(SimError):
                manager.acquire(timeout=0)
            for lease in leases_:
                lease.release()
        with fake_manager({"MOBSTER_MAX_SIMS": "zero"}) as (manager, *_):
            with self.assertRaises(SimError) as caught:
                manager.max_sims
            self.assertEqual(caught.exception.kind, "environment")

    def test_entries_for_vanished_simulators_are_dropped(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            first = manager.acquire()
            first.release()
            del simctl.devices[first.target.udid]
            with manager.acquire() as target:
                self.assertNotEqual(target.udid, first.target.udid)
            self.assertEqual([entry["udid"] for entry in manager.registry.read()], [target.udid])

    def test_booted_simulators_are_preferred(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            first, second = manager.acquire(), manager.acquire()
            first.release()
            second.release()
            simctl.devices[first.target.udid]["state"] = "Shutdown"
            with manager.acquire() as target:
                self.assertEqual(target.udid, second.target.udid)

    def test_boot_retries_once_after_unable_to_boot(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            simctl.fail["boot"] = [Result(149, "", "Unable to boot device in current state: Shutting Down")]
            with manager.acquire() as target:
                pass
            self.assertEqual(simctl.simctl_calls("boot"), [[target.udid], [target.udid]])
            self.assertEqual(simctl.simctl_calls("shutdown"), [[target.udid]])

    def test_a_boot_that_runs_out_of_time_says_to_run_again_and_frees_the_lease(self):
        self.assertEqual(api.BOOT_TIMEOUT, 240)
        with fake_manager() as (manager, simctl, wda, leases, root):
            simctl.fail["bootstatus"] = [Result(None, "", "/usr/bin/xcrun did not finish in 240 s")]
            with self.assertRaises(SimError) as caught:
                manager.acquire()
            self.assertEqual(caught.exception.kind, "simulator")
            self.assertEqual(str(caught.exception),
                             f"{NAME} is still booting after 240 s, which happens when this Mac is busy.")
            self.assertEqual(caught.exception.fix, "Run the command again; the simulator keeps booting.")
            self.assertEqual(leases.held, set())
            self.assertEqual(wda.cancelled, 1)  # the WebDriverAgent build started alongside is stopped
            simctl.devices[manager.registry.read()[0]["udid"]]["state"] = "Booting"
            simctl.calls.clear()
            with manager.acquire() as target:  # run again: it waits for the same simulator's boot
                pass
            self.assertEqual(simctl.simctl_calls("boot"), [])
            self.assertEqual(simctl.simctl_calls("bootstatus"), [[target.udid, "-b"]])

    def test_a_real_boot_failure_suggests_erasing(self):
        for command, result in (("bootstatus", Result(1, "", "An error was encountered processing the command")),
                                ("boot", Result(1, "", "Failed to start launchd_sim: could not bind"))):
            with self.subTest(command=command), fake_manager() as (manager, simctl, wda, leases, root):
                simctl.fail[command] = [result]
                with self.assertRaises(SimError) as caught:
                    manager.acquire()
                self.assertEqual(caught.exception.kind, "simulator")
                self.assertIn(result.err, str(caught.exception))
                self.assertIn("mobster sim erase", caught.exception.fix)
                self.assertEqual(leases.held, set())

    def test_create_failure(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            simctl.fail["create"] = [Result(1, "", "Invalid runtime")]
            with self.assertRaises(SimError) as caught:
                manager.acquire()
            self.assertEqual(caught.exception.kind, "simulator")
            self.assertEqual(manager.registry.read(), [])

    def test_a_simulator_created_without_a_port_is_deleted_again(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            wda.busy = set(range(8310, 8350))
            with self.assertRaises(SimError):
                manager.acquire()
            self.assertEqual(len(simctl.simctl_calls("delete")), 1)
            self.assertEqual(simctl.devices, {})
            self.assertEqual(manager.registry.read(), [])

    def test_unknown_device_or_runtime(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            for kwargs in ({"device": "iPhone 99"}, {"runtime": "iOS 30.0"}):
                with self.subTest(**kwargs), self.assertRaises(SimError) as caught:
                    manager.acquire(**kwargs)
                self.assertEqual(caught.exception.kind, "environment")
            self.assertEqual(simctl.simctl_calls("create"), [])

    def test_isolation_check_needs_the_simulators_own_folder(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.acquire().release()
            identifier = manager.registry.read()[0]["udid"]
            (root / identifier / "data").rmdir()
            (root / identifier).rmdir()
            with self.assertRaises(SimError) as caught:
                manager.acquire()
            self.assertEqual(caught.exception.kind, "simulator")
            self.assertIn("is missing", str(caught.exception))

    def test_move_to_the_next_free_port_when_another_program_holds_it(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.acquire().release()
            wda.plans = ["move"]
            wda.busy = {8310}
            with manager.acquire() as target:
                self.assertEqual(target.wda_url, "http://127.0.0.1:8311")
                self.assertEqual(leases.held, {"http://127.0.0.1:8310", "http://127.0.0.1:8311"})
            self.assertEqual(leases.held, set())
            entry = manager.registry.read()[0]
            self.assertEqual((entry["wda_port"], entry["mjpeg_port"]), (8311, 9311))
            self.assertEqual(wda.started[-1][1:], (8311, 9311))

    def test_restart_stops_the_old_runner_first(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.acquire().release()
            wda.plans = ["restart"]
            with manager.acquire() as target:
                pass
            self.assertEqual(wda.stopped, [target.udid])
            self.assertEqual(len(wda.started), 2)

    def test_a_runner_listening_beyond_loopback_is_stopped_and_refused(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            wda.loopback = False
            with self.assertRaises(SimError) as caught:
                manager.acquire()
            self.assertEqual(caught.exception.kind, "wda")
            self.assertEqual(str(caught.exception), "WebDriverAgent listened beyond this Mac")
            self.assertIn("*:9310", caught.exception.fix)
            self.assertEqual(len(wda.stopped), 1)
            self.assertEqual(leases.held, set())

    def test_a_cached_build_is_not_built_again(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            products = build_dir(manager.data_dir, TOOLS.build) / "Build" / "Products"
            products.mkdir(parents=True)
            (products / "WebDriverAgentRunner_iphonesimulator26.4-arm64.xctestrun").write_text("")
            lease = manager.acquire()
            lease.release()
            self.assertEqual(wda.built, 1)  # ensure_build is asked, and returns the cache
            self.assertNotIn("wda_build", lease.timing)

    def test_restarter_is_the_benchmarks_simulator_restarter(self):
        from mobile_agent.bench.sim_wda import SimulatorWDA
        with fake_manager() as (manager, simctl, wda, leases, root):
            with manager.acquire() as target:
                restarter = manager.restarter(target)
            self.assertIsInstance(restarter, SimulatorWDA)
            self.assertEqual((restarter.udid, restarter.port, restarter.mjpeg_port), (target.udid, 8310, 9310))
            self.assertEqual(restarter.xctestrun, target.xctestrun)
            self.assertTrue(restarter.log_path.endswith(f"wda-{target.udid}.log"))

    def test_the_restarter_starts_xcode_as_acquire_does_when_the_command_line_tools_are_selected(self):
        xcode = "/Applications/Xcode.app/Contents/Developer"
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager._tools = TOOLS._replace(env={"PATH": "/usr/bin:/bin", "DEVELOPER_DIR": xcode})
            with manager.acquire() as target:
                restarter = manager.restarter(target)
            Path(restarter.log_path).parent.mkdir(parents=True, exist_ok=True)
            with mock.patch("mobile_agent.sim.wda.subprocess.Popen") as popen:
                restarter.start()
            command, kwargs = popen.call_args.args[0], popen.call_args.kwargs
            self.assertEqual(command, [TOOLS.xcodebuild, "test-without-building", "-xctestrun", target.xctestrun,
                                       "-destination", f"id={target.udid}"])
            self.assertEqual({key: kwargs["env"].get(key) for key in ("DEVELOPER_DIR", "TEST_RUNNER_USE_PORT",
                                                                      "TEST_RUNNER_MJPEG_SERVER_PORT",
                                                                      "TEST_RUNNER_USE_IP")},
                             {"DEVELOPER_DIR": xcode, "TEST_RUNNER_USE_PORT": "8310",
                              "TEST_RUNNER_MJPEG_SERVER_PORT": "9310", "TEST_RUNNER_USE_IP": "127.0.0.1"})
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["cwd"], str(Path(target.xctestrun).resolve().parent))


def registry_locked(registry):
    """Whether someone holds the registry's lock right now (probed from a descriptor of its own, as another
    process would)."""
    registry.data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(registry.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)  # releases the probe's lock when it got one


def second_process(first):
    """Another SimulatorManager on the same data folder and the same fakes, standing in for a second process."""
    other = SimulatorManager(data_dir=first.data_dir, progress=None)
    other.run, other.wda, other.lease = first.run, first.wda, first.lease
    other.clock, other.sleep, other._tools = first.clock, first.sleep, first._tools
    return other


class ConcurrentProcessTests(unittest.TestCase):
    """Review round 2: B lists simctl's devices, A creates and registers a simulator, then B edits the registry.
    With the list taken before the registry lock, B forgot A's simulator as gone (left booted with its runner
    up, in no list, so no command could reach it) and gave its name out again."""

    def at_first_list(self, b, a_call):
        """Run ``a_call`` (all of process A's call) at B's first `simctl list`: straight away when B lists
        outside the registry lock, else on a thread that waits for the lock as a second process would.
        Returns (whether B held the lock, a function that waits for A and returns its result)."""
        state, threads, fired = {}, [], []
        run = b.run

        def a_side():
            try:
                state["value"] = a_call()
            except BaseException as exc:  # handed to the test below
                state["error"] = exc

        def b_run(argv, timeout=60, env=None):
            result = run(argv, timeout=timeout, env=env)
            if not fired and [str(part) for part in argv[:3]] == [api.XCRUN, "simctl", "list"]:
                fired.append(registry_locked(b.registry))
                if fired[0]:
                    threads.append(threading.Thread(target=a_side, daemon=True))
                    threads[0].start()
                else:
                    a_side()
            return result
        b.run = b_run

        def a_result():
            for thread in threads:
                thread.join(timeout=30)
                self.assertFalse(thread.is_alive(), "A's call never got the registry lock")
            if "error" in state:
                raise state["error"]
            return state["value"]
        return fired, a_result

    def test_two_acquires_at_once_keep_both_simulators(self):
        with fake_manager() as (a, simctl, wda, leases, root):
            b = second_process(a)
            fired, a_result = self.at_first_list(b, a.acquire)
            b_lease = b.acquire()
            a_lease = a_result()
            x, y = a_lease.target, b_lease.target
            self.assertNotEqual(x.udid, y.udid)
            self.assertEqual({entry["udid"] for entry in a.registry.read()}, {x.udid, y.udid})
            self.assertEqual(sorted([x.name, y.name]), [NAME, NAME + " · 2"])
            self.assertEqual(fired, [True], "B's simctl list was taken outside the registry lock")
            self.assertNotEqual(x.wda_url, y.wda_url)
            self.assertEqual(len(simctl.simctl_calls("create")), 2)
            self.assertEqual({row["udid"] for row in a.list()}, {x.udid, y.udid})
            a_lease.release()
            b_lease.release()
            self.assertEqual(sorted(a.delete()), sorted([x.udid, y.udid]))
            self.assertEqual(simctl.devices, {})
            self.assertEqual(a.registry.read(), [])

    def test_prune_during_an_acquire_keeps_the_new_simulator(self):
        with fake_manager() as (a, simctl, wda, leases, root):
            b = second_process(a)
            fired, a_result = self.at_first_list(b, a.acquire)
            with mock.patch.object(api, "installed_xcode_builds", return_value={TOOLS.build}):
                removed = b.prune()
            a_lease = a_result()
            self.assertEqual(removed["entries"], [])
            self.assertEqual([entry["udid"] for entry in a.registry.read()], [a_lease.target.udid])
            self.assertEqual(fired, [True], "prune's simctl list was taken outside the registry lock")
            a_lease.release()

    def test_the_busy_fix_names_no_memory_figure(self):
        with fake_manager({"MOBSTER_MAX_SIMS": "1"}) as (manager, simctl, wda, leases, root):
            with manager.acquire():
                with self.assertRaises(SimError) as caught:
                    manager.acquire(timeout=0)
            self.assertEqual(caught.exception.fix, "Wait for one to finish, or set MOBSTER_MAX_SIMS higher.")


class HousekeepingTests(unittest.TestCase):
    def test_list_joins_the_registry_with_simctl(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            self.assertEqual(manager.list(), [])
            held = manager.acquire()
            rows = manager.list()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(set(row), {"udid", "name", "device_type", "runtime", "state", "wda", "wda_url",
                                        "mjpeg_url", "in_use", "last_used_at"})
            self.assertEqual((row["state"], row["wda"], row["in_use"]), ("Booted", "ready", True))
            held.release()
            self.assertFalse(manager.list()[0]["in_use"])

    def test_delete_refuses_a_udid_outside_the_registry_whatever_its_name(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            stranger = simctl.add("Mobster · iPhone 17 Pro · iOS 26.4 · 9", state="Booted")
            benchmark = simctl.add("iOSWorld Mobster", state="Booted")
            for udid in (stranger, benchmark, "0A1B2C3D-0000-4000-8000-00000000FFFF"):
                with self.subTest(udid=udid), self.assertRaises(SimError) as caught:
                    manager.delete(udid)
                self.assertEqual(caught.exception.kind, "simulator")
            self.assertEqual(manager.delete(), [])
            self.assertEqual(simctl.simctl_calls("delete") + simctl.simctl_calls("shutdown"), [])
            self.assertIn(stranger, simctl.devices)

    def test_delete_refuses_a_registry_simulator_renamed_away_from_mobster(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            with manager.acquire() as target:
                pass
            simctl.devices[target.udid]["name"] = "My test phone"
            with self.assertRaises(SimError) as caught:
                manager.delete(target.udid)
            self.assertIn("not a Mobster simulator name", str(caught.exception))
            self.assertEqual(manager.delete(), [])  # skipped, and kept in the registry
            self.assertEqual(simctl.simctl_calls("delete"), [])
            self.assertEqual(len(manager.registry.read()), 1)

    def test_delete_all_removes_mobsters_simulators_and_their_entries(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            first, second = manager.acquire(), manager.acquire()
            first.release()
            second.release()
            other = simctl.add("iOSWorld Mobster 2", state="Booted")
            deleted = manager.delete()
            self.assertEqual(sorted(deleted), sorted([first.target.udid, second.target.udid]))
            self.assertEqual(list(simctl.devices), [other])
            self.assertEqual(manager.registry.read(), [])
            self.assertEqual(sorted(wda.stopped), sorted(deleted))

    def test_delete_refuses_a_simulator_in_use(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            held = manager.acquire()
            with self.assertRaises(SimError) as caught:
                manager.delete(held.target.udid)
            self.assertEqual(caught.exception.kind, "busy")
            self.assertEqual(manager.delete(), [])
            self.assertIn(held.target.udid, simctl.devices)
            held.release()

    def test_shutdown_and_erase_only_touch_the_registry(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            with manager.acquire() as target:
                pass
            other = simctl.add("mobster-smoke", state="Booted")
            manager.shutdown()
            self.assertEqual(simctl.devices[target.udid]["state"], "Shutdown")
            self.assertEqual(simctl.devices[other]["state"], "Booted")
            self.assertEqual(wda.stopped, [target.udid])
            for action in (manager.shutdown, manager.erase):
                with self.subTest(action=action.__name__), self.assertRaises(SimError):
                    action(other)
            manager.erase(target.udid)
            self.assertEqual(simctl.simctl_calls("erase"), [[target.udid]])

    def test_prune(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            old, fresh = manager.acquire(), manager.acquire()
            old.release()
            fresh.release()
            with manager.registry.edit() as entries:
                stale = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=15))
                for entry in entries:
                    if entry["udid"] == old.target.udid:
                        entry["last_used_at"] = stale.strftime("%Y-%m-%dT%H:%M:%SZ")
                entries.append({**entries[0], "udid": "0A1B2C3D-0000-4000-8000-00000000DEAD", "wda_port": 8330,
                                "mjpeg_port": 9330})
            folder = manager.data_dir / "wda"
            current = build_dir(manager.data_dir, TOOLS.build)
            older = folder / f"build-16C5032a-{current.name.split('-', 2)[2]}"
            other_patch = folder / (current.name[:-8] + "00000000")
            for path in (current, older, other_patch, folder / "src-00000000"):
                path.mkdir(parents=True)
            with mock.patch.object(api, "installed_xcode_builds", return_value={TOOLS.build}):
                removed = manager.prune()
            self.assertEqual(removed["simulators"], [old.target.udid])
            self.assertEqual(removed["entries"], ["0A1B2C3D-0000-4000-8000-00000000DEAD"])
            self.assertEqual(sorted(removed["wda_builds"]),
                             sorted([str(older), str(other_patch), str(folder / "src-00000000")]))
            self.assertTrue(current.is_dir())
            self.assertEqual([entry["udid"] for entry in manager.registry.read()], [fresh.target.udid])

    def test_status(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            status = manager.status()
            self.assertEqual(set(status), {"xcode", "runtime", "device_type", "wda_build", "simulators", "data_dir",
                                           "max_sims"})
            self.assertEqual(status["xcode"], {"version": "26.4", "build": "17E192", "path": "/Applications/Xcode.app"})
            self.assertEqual((status["runtime"], status["device_type"]), ("iOS 26.4", "iPhone 17 Pro"))
            self.assertEqual(status["wda_build"], {"cached": False, "path": None})
            self.assertEqual((status["simulators"], status["max_sims"]), ([], 2))

    def test_status_without_xcode(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager._tools = None
            with mock.patch("mobile_agent.device_manager.developer_dir", return_value=(None, None)):
                status = manager.status()
            self.assertIn("Xcode isn't installed", status["xcode"]["error"])
            self.assertEqual(status["simulators"], [])


class AppTests(unittest.TestCase):
    def setUp(self):
        self.context = fake_manager()
        self.manager, self.simctl, self.wda, self.leases, self.root = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.lease = self.manager.acquire()
        self.addCleanup(self.lease.release)
        self.target = self.lease.target
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.app = make_app(self.folder.name)
        self.simctl.calls.clear()

    def container(self, udid=None, name="APP"):
        path = self.root / (udid or self.target.udid) / "data" / "Containers" / "Data" / "Application" / name
        for folder in ("Documents", "Library/Preferences", "tmp"):
            (path / folder).mkdir(parents=True, exist_ok=True)
        (path / "Documents" / "state.json").write_text("{}")
        (path / "Library" / "Preferences" / "dev.mobster.daybreak.plist").write_text("x")
        return path

    def test_install_and_installed_app(self):
        info = self.manager.install(self.target, str(self.app))
        self.assertEqual(info, AppInfo("dev.mobster.daybreak", "Daybreak", "1.0 (1)", str(self.app.resolve())))
        self.assertEqual(self.simctl.simctl_calls("install"), [[self.target.udid, str(self.app.resolve())]])
        self.simctl.containers[(self.target.udid, "dev.mobster.daybreak", "app")] = str(self.app)
        self.assertEqual(self.manager.installed_app(self.target, "dev.mobster.daybreak"),
                         AppInfo("dev.mobster.daybreak", "Daybreak", "1.0 (1)", None))
        with self.assertRaises(SimError) as caught:
            self.manager.installed_app(self.target, "dev.example.missing")
        self.assertEqual((caught.exception.kind, str(caught.exception)),
                         ("install", "dev.example.missing isn't installed on the simulator. Pass --app."))

    def test_a_device_build_never_reaches_simctl(self):
        device = make_app(Path(self.folder.name) / "device", platforms=("iPhoneOS",))
        with self.assertRaises(SimError) as caught:
            self.manager.install(self.target, str(device))
        self.assertEqual(caught.exception.kind, "install")
        with self.assertRaises(SimError):
            self.manager.reset(self.target, "dev.mobster.daybreak", "reinstall", str(device))
        self.assertEqual(self.simctl.calls, [])  # not even the uninstall

    def test_install_failure(self):
        self.simctl.fail["install"] = [Result(1, "", "Failed to install the app: bad signature")]
        with self.assertRaises(SimError) as caught:
            self.manager.install(self.target, str(self.app))
        self.assertIn("bad signature", str(caught.exception))

    def test_reset_none_and_system_apps_do_nothing_but_read_for_an_alert(self):
        self.wda.http.requests.clear()
        self.manager.reset(self.target, "dev.mobster.daybreak", "none")
        self.manager.reset(self.target, "com.apple.Preferences", "data")
        self.manager.reset(self.target, "com.apple.mobilesafari", "reinstall", str(self.app))
        self.assertEqual(self.simctl.calls, [])
        self.assertEqual(self.wda.http.requests, [("GET", "/alert/text", None)] * 3)
        with self.assertRaises(ValueError):
            self.manager.reset(self.target, "dev.mobster.daybreak", "auto")

    def test_every_reset_dismisses_an_alert_left_on_screen(self):
        data = self.container()
        self.simctl.containers[(self.target.udid, "dev.mobster.daybreak", "data")] = str(data)
        for level, app in (("data", None), ("reinstall", str(self.app)), ("none", None)):
            with self.subTest(level=level):
                self.wda.http.pressed.clear()
                self.wda.http.show("Open in “Daybreak”?", ("Cancel", "Open"))
                self.manager.reset(self.target, "dev.mobster.daybreak", level, app)
                self.assertEqual(self.wda.http.pressed, [("Open in “Daybreak”?", "Cancel")])
                self.assertEqual(self.wda.http.alerts, [])

    def test_reset_data(self):
        data = self.container()
        group = self.container(name="GROUP")
        udid, bundle = self.target.udid, "dev.mobster.daybreak"
        self.simctl.containers[(udid, bundle, "data")] = str(data)
        self.simctl.containers[(udid, bundle, "groups")] = f"group.dev.mobster.daybreak\t{group}"
        self.manager.reset(self.target, bundle, "data")
        self.assertEqual(self.simctl.simctl_calls("terminate"), [[udid, bundle]])
        self.assertEqual(sorted(self.simctl.simctl_calls("spawn")),
                         [[udid, "defaults", "delete", bundle],
                          [udid, "defaults", "delete", "group.dev.mobster.daybreak"]])
        self.assertEqual(self.simctl.simctl_calls("privacy"), [[udid, "reset", "all", bundle]])
        self.assertEqual(self.simctl.simctl_calls("keychain"), [[udid, "reset"]])
        for container in (data, group):
            for name in ("Documents", "Library", "tmp"):
                self.assertEqual(os.listdir(container / name), [])

    def test_reset_data_refuses_a_container_outside_the_simulator_before_changing_anything(self):
        other = "0A1B2C3D-0000-4000-8000-000000000002"
        theirs = self.container(udid=other)
        self.simctl.containers[(self.target.udid, "dev.mobster.daybreak", "data")] = str(theirs)
        with self.assertRaises(SimError) as caught:
            self.manager.reset(self.target, "dev.mobster.daybreak", "data")
        self.assertEqual(caught.exception.kind, "simulator")
        self.assertEqual(self.simctl.simctl_calls("spawn") + self.simctl.simctl_calls("privacy"), [])
        self.assertTrue((theirs / "Documents" / "state.json").exists())

    def test_reset_data_of_an_app_that_isnt_installed(self):
        for output in (None, "(null)"):
            if output:
                self.simctl.containers[(self.target.udid, "dev.mobster.daybreak", "data")] = output
            with self.subTest(output=output), self.assertRaises(SimError) as caught:
                self.manager.reset(self.target, "dev.mobster.daybreak", "data")
            self.assertEqual(caught.exception.kind, "install")

    def test_reinstall(self):
        udid = self.target.udid
        self.manager.reset(self.target, "dev.mobster.daybreak", "reinstall", str(self.app))
        commands = [call[2] for call in self.simctl.calls]
        self.assertLess(commands.index("uninstall"), commands.index("install"))
        self.assertEqual(self.simctl.simctl_calls("uninstall"), [[udid, "dev.mobster.daybreak"]])
        self.assertEqual(self.simctl.simctl_calls("privacy"), [[udid, "reset", "all", "dev.mobster.daybreak"]])
        self.assertEqual(self.simctl.simctl_calls("keychain"), [[udid, "reset"]])
        with self.assertRaises(SimError) as caught:
            self.manager.reset(self.target, "dev.mobster.daybreak", "reinstall")
        self.assertEqual(caught.exception.kind, "install")

    def test_launch_passes_arguments_and_child_environment(self):
        self.manager.launch(self.target, "dev.mobster.daybreak", ["-DaybreakSkipOnboarding", "YES"],
                            {"DAYBREAK_SEED": "3"})
        call = self.simctl.calls[-1]
        self.assertEqual(call[2:], ["launch", "--terminate-running-process", self.target.udid, "dev.mobster.daybreak",
                                    "-DaybreakSkipOnboarding", "YES"])
        self.assertEqual(self.simctl.envs[-1]["SIMCTL_CHILD_DAYBREAK_SEED"], "3")
        self.simctl.fail["launch"] = [Result(4, "", "The request to open \"dev.mobster.daybreak\" failed.")]
        with self.assertRaises(SimError) as caught:
            self.manager.launch(self.target, "dev.mobster.daybreak")
        self.assertEqual(caught.exception.kind, "launch")

    def test_a_reset_reads_for_alerts_only_after_its_work(self):
        data = self.container()
        self.simctl.containers[(self.target.udid, "dev.mobster.daybreak", "data")] = str(data)
        order = []
        self.simctl.hooks["keychain"] = lambda args: order.append("keychain")
        http = self.wda.http

        def read(method, url, body=None, timeout=10):
            order.append(method)
            return http(method, url, body, timeout)
        self.wda.http = read
        self.manager.reset(self.target, "dev.mobster.daybreak", "data")
        self.assertEqual(order, ["keychain", "GET"])

    def test_a_reset_finishes_its_work_before_an_alert_that_stays_fails_it(self):
        data = self.container()
        self.simctl.containers[(self.target.udid, "dev.mobster.daybreak", "data")] = str(data)
        self.wda.http.show("Software Update", ("Later",), sticky=True)
        with self.assertRaises(SimError) as caught:
            self.manager.reset(self.target, "dev.mobster.daybreak", "data")
        self.assertIn("“Software Update”", str(caught.exception))
        self.assertEqual(self.simctl.simctl_calls("keychain"), [[self.target.udid, "reset"]])
        self.assertEqual(os.listdir(data / "Documents"), [])
        self.wda.http.alerts.clear()
        with self.assertRaises(SimError) as caught:  # and a reset's own error comes first
            self.manager.reset(self.target, "dev.example.missing", "data")
        self.assertEqual(caught.exception.kind, "install")

    def test_terminate_ignores_not_running_and_open_url_reports_failure(self):
        self.simctl.fail["terminate"] = [Result(3, "", "found nothing to terminate")]
        self.manager.terminate(self.target, "dev.mobster.daybreak")
        self.manager.open_url(self.target, "daybreak://paywall")
        self.assertEqual(self.simctl.simctl_calls("openurl"), [[self.target.udid, "daybreak://paywall"]])
        self.simctl.fail["openurl"] = [Result(1, "", "No application is registered")]
        self.wda.http.requests.clear()
        with self.assertRaises(SimError) as caught:
            self.manager.open_url(self.target, "nowhere://x")
        self.assertEqual(caught.exception.kind, "launch")
        self.assertEqual(self.wda.http.requests, [])

    def prompt_on_openurl(self, text="Open in “Daybreak”?", after=0, sticky=False):
        http = self.wda.http
        self.simctl.hooks["openurl"] = lambda args: http.show(text, ("Cancel", "Open"), sticky=sticky, after=after)
        http.requests.clear()
        return http

    def test_open_url_presses_open_on_the_prompt_ios_shows(self):
        http = self.prompt_on_openurl()
        self.manager.open_url(self.target, "daybreak://paywall")
        self.assertEqual(http.pressed, [("Open in “Daybreak”?", "Open")])
        self.assertEqual(http.requests, [("GET", "/alert/text", None), ("POST", "/alert/accept", {"name": "Open"}),
                                         ("GET", "/alert/text", None)])
        self.assertEqual(http.alerts, [])

    def test_open_url_waits_for_a_prompt_that_shows_late(self):
        http = self.prompt_on_openurl(after=4)
        started = self.manager.clock()
        self.manager.open_url(self.target, "daybreak://paywall")
        self.assertEqual(http.pressed, [("Open in “Daybreak”?", "Open")])
        self.assertLess(self.manager.clock() - started, api.OPEN_PROMPT_WAIT)

    def test_open_url_without_a_prompt_looks_for_three_seconds_and_presses_nothing(self):
        http = self.prompt_on_openurl()
        self.simctl.hooks.clear()
        started = self.manager.clock()
        self.manager.open_url(self.target, "https://example.com/daybreak")
        self.assertEqual(http.paths("POST"), [])
        self.assertTrue(http.paths("GET"))
        self.assertAlmostEqual(self.manager.clock() - started, api.OPEN_PROMPT_WAIT, delta=api.ALERT_POLL)

    def test_open_url_keeps_looking_while_the_runner_does_not_answer(self):
        http = self.prompt_on_openurl(after=30)
        http.silent = 29  # a loaded Mac: the runner answers nothing for a while, and the prompt is up by then
        started = self.manager.clock()
        self.manager.open_url(self.target, "daybreak://paywall")
        self.assertEqual(http.pressed, [("Open in “Daybreak”?", "Open")])
        self.assertGreater(self.manager.clock() - started, api.OPEN_PROMPT_WAIT)
        http = self.prompt_on_openurl()
        self.simctl.hooks.clear()
        http.answering = False  # and a runner that never answers ends the wait at the limit
        started = self.manager.clock()
        self.manager.open_url(self.target, "otherapp://home")
        self.assertAlmostEqual(self.manager.clock() - started, api.OPEN_PROMPT_LIMIT, delta=api.ALERT_POLL)

    def test_install_answers_open_for_the_apps_schemes_so_its_links_skip_the_prompt(self):
        app = make_app(Path(self.folder.name) / "linked", CFBundleURLTypes=[
            {"CFBundleURLName": "main", "CFBundleURLSchemes": ["daybreak", "Daybreak-Beta"]},
            {"CFBundleURLName": "again", "CFBundleURLSchemes": ["daybreak"]}, "not a dict"])
        self.manager.install(self.target, str(app))
        key = "com.apple.CoreSimulator.CoreSimulatorBridge-->{}"
        self.assertEqual(sorted(self.simctl.simctl_calls("spawn")), sorted(
            [self.target.udid, "defaults", "write", "com.apple.launchservices.schemeapproval", key.format(scheme),
             "-string", "dev.mobster.daybreak"] for scheme in ("daybreak", "Daybreak-Beta", "daybreak-beta")))
        http = self.prompt_on_openurl()  # were iOS to ask, the manager would not be looking
        for link in ("daybreak://paywall", "Daybreak-Beta://settings"):
            self.manager.open_url(self.target, link)
        self.assertEqual(http.requests, [])
        self.assertEqual(len(self.simctl.simctl_calls("openurl")), 2)

    def test_a_scheme_ios_remembers_on_disk_skips_the_prompt_wait(self):
        import plistlib
        folder = self.root / self.target.udid / "data" / "Library" / "Preferences"
        folder.mkdir(parents=True)
        with open(folder / "com.apple.launchservices.schemeapproval.plist", "wb") as stream:
            plistlib.dump({"com.apple.CoreSimulator.CoreSimulatorBridge-->daybreak": "dev.mobster.daybreak"}, stream)
        http = self.prompt_on_openurl()
        self.simctl.hooks.clear()
        started = self.manager.clock()
        self.manager.open_url(self.target, "daybreak://paywall")
        self.assertEqual((http.requests, self.manager.clock() - started), ([], 0))
        self.manager.open_url(self.target, "weather://today")  # not remembered: looks for the prompt
        self.assertTrue(http.paths("GET"))

    def test_after_open_is_pressed_the_next_link_does_not_wait(self):
        http = self.prompt_on_openurl()
        self.manager.open_url(self.target, "daybreak://paywall")
        self.assertEqual(http.pressed, [("Open in “Daybreak”?", "Open")])
        self.simctl.hooks.clear()
        http.requests.clear()
        started = self.manager.clock()
        self.manager.open_url(self.target, "daybreak://settings")
        self.assertEqual((http.requests, self.manager.clock() - started), ([], 0))

    def test_a_failed_approval_write_leaves_the_prompt_to_open_url(self):
        app = make_app(Path(self.folder.name) / "linked", CFBundleURLTypes=[{"CFBundleURLSchemes": ["daybreak"]}])
        self.simctl.fail["spawn"] = [Result(1, "", "defaults: could not write")]
        self.manager.install(self.target, str(app))
        http = self.prompt_on_openurl()
        self.manager.open_url(self.target, "daybreak://paywall")
        self.assertEqual(http.pressed, [("Open in “Daybreak”?", "Open")])

    def test_open_url_leaves_any_other_alert_to_the_run(self):
        http = self.prompt_on_openurl(text="Allow “Daybreak” to send you notifications?")
        self.manager.open_url(self.target, "daybreak://settings")
        self.assertEqual(http.paths(), ["/alert/text"])
        self.assertEqual(http.pressed, [])
        self.assertEqual(len(http.alerts), 1)

    def test_open_url_fails_when_the_prompt_stays(self):
        http = self.prompt_on_openurl(sticky=True)
        with self.assertRaises(SimError) as caught:
            self.manager.open_url(self.target, "daybreak://paywall")
        self.assertEqual(caught.exception.kind, "launch")
        self.assertIn("“Open in “Daybreak”?” prompt stayed up", str(caught.exception))
        self.assertEqual(http.pressed, [("Open in “Daybreak”?", "Open")] * 2)

    def test_screenshot_is_a_scaled_jpeg(self):
        from PIL import Image
        path = Path(self.folder.name) / "frames" / "01-launch.jpg"
        self.assertEqual(self.manager.screenshot(self.target, path, max_width=603, quality=75), path)
        with Image.open(path) as image:
            self.assertEqual((image.format, image.width, image.height), ("JPEG", 603, 1311))
        self.assertEqual(os.listdir(path.parent), ["01-launch.jpg"])
        self.assertEqual(self.simctl.simctl_calls("io")[0][:3], [self.target.udid, "screenshot", "--type=png"])


if __name__ == "__main__":
    unittest.main()
