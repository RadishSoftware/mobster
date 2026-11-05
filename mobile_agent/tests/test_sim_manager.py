
import datetime
import fcntl
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent.journal import LeaseHeld
from mobile_agent.sim import (
    AppInfo,
    SimError,
    SimLease,
    SimTarget,
    SimulatorManager,
    api,
    xcode,
)
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
            self.assertEqual(wda.http.requests, [("GET", "/alert/text", None)])
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

    def test_acquire_dismisses_alerts_stacked_behind_each_other(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            wda.http.show("Allow “Firefox” to send you notifications?", ("Don’t Allow", "Allow"))
            wda.http.show("Open in “Firefox”?", ("Cancel", "Open"))
            manager.acquire().release()
            self.assertEqual(wda.http.pressed, [("Open in “Firefox”?", "Cancel"),
                                                ("Allow “Firefox” to send you notifications?", "Don’t Allow")])
            self.assertEqual(wda.http.alerts, [])

    def test_a_lease_held_by_a_list_probe_is_looked_at_again_before_creating(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            first = manager.acquire()
            first.release()
            url, looked, sleeps = first.target.wda_url, [], []

            def lease(wanted):
                looked.append(wanted)
                if wanted == url and looked.count(url) == 1:
                    raise LeaseHeld("Another Mobster process already owns this resource")
                return leases(wanted)
            manager.lease = lease
            manager.sleep = sleeps.append
            with manager.acquire() as target:
                self.assertEqual(target.udid, first.target.udid)
            self.assertEqual(sleeps, [api.PROBE_RETRY])
            self.assertEqual(len(simctl.simctl_calls("create")), 1)
            self.assertEqual([entry["udid"] for entry in manager.registry.read()], [first.target.udid])

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

    def test_pool_busy_is_a_rejected_acquire_without_new_device_effects(self):
        from mobile_agent.verify.actions import ActionJournal, ActionNotDispatched

        with fake_manager({"MOBSTER_MAX_SIMS": "1"}) as (manager, simctl, wda, leases, root):
            held = manager.acquire()
            before = {name: list(simctl.simctl_calls(name)) for name in ("create", "boot", "install")}
            started = list(wda.started)
            run_dir = root.parent / "pool-busy"
            run_dir.mkdir()
            journal = ActionJournal(run_dir)
            try:
                with self.assertRaises(ActionNotDispatched) as caught:
                    with journal.action("ACQUIRE"):
                        manager.acquire(timeout=0)
                self.assertEqual(caught.exception.kind, "busy")
                self.assertIsNone(journal.klass)
                self.assertEqual({name: simctl.simctl_calls(name) for name in before}, before)
                self.assertEqual(wda.started, started)
                self.assertEqual(leases.held, {held.target.wda_url})
            finally:
                held.release()

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

    def test_acquire_udid_waits_for_the_holder_then_takes_it(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            held = manager.acquire()
            sleeps = []

            def sleep(seconds):
                sleeps.append(seconds)
                held.release()
            manager.sleep = sleep
            with manager.acquire_udid(held.target.udid, timeout=30) as target:
                self.assertEqual(target.udid, held.target.udid)
            self.assertEqual(len(sleeps), 1)

    def test_boot_retries_once_after_unable_to_boot(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            simctl.fail["boot"] = [Result(149, "", "Unable to boot device in current state: Shutting Down")]
            with manager.acquire() as target:
                pass
            self.assertEqual(simctl.simctl_calls("boot"), [[target.udid], [target.udid]])
            self.assertEqual(simctl.simctl_calls("shutdown"), [[target.udid]])

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

    def test_acquire_udid_is_busy_after_the_timeout_and_names_the_simulator(self):
        from mobile_agent.verify.actions import ActionJournal, ActionNotDispatched

        with fake_manager() as (manager, simctl, wda, leases, root):
            held = manager.acquire()
            before = {name: list(simctl.simctl_calls(name)) for name in ("boot", "create", "install")}
            started = list(wda.started)
            run_dir = root.parent / "busy"
            run_dir.mkdir()
            journal = ActionJournal(run_dir)
            try:
                with self.assertRaises(ActionNotDispatched) as caught:
                    with journal.action("ACQUIRE"):
                        manager.acquire_udid(held.target.udid, timeout=0)
                self.assertEqual(caught.exception.kind, "busy")
                self.assertIn(held.target.name, str(caught.exception))
                self.assertIsNone(journal.klass)
                self.assertEqual({name: simctl.simctl_calls(name) for name in before}, before)
                self.assertEqual(wda.started, started)
                self.assertEqual(leases.held, {held.target.wda_url})
                rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
                self.assertEqual(rows[-1]["outcome"], "rejected")
            finally:
                held.release()

    def test_cancel_while_waiting_for_pinned_simulator_never_starts_readiness(self):
        from mobile_agent.verify.actions import ActionJournal, ActionNotDispatched

        with fake_manager() as (manager, simctl, wda, leases, root):
            held = manager.acquire()
            stop = threading.Event()
            manager.sleep = lambda seconds: stop.set()
            started = list(wda.started)
            run_dir = root.parent / "cancel-busy"
            run_dir.mkdir()
            journal = ActionJournal(run_dir)
            try:
                with self.assertRaises(ActionNotDispatched) as caught:
                    with journal.action("ACQUIRE"):
                        manager.acquire_udid(held.target.udid, timeout=30, cancelled=stop.is_set)
                self.assertEqual(caught.exception.kind, "stopped")
                self.assertIsNone(journal.klass)
                self.assertEqual(wda.started, started)
                self.assertEqual(leases.held, {held.target.wda_url})
            finally:
                held.release()

    def test_cancel_after_pinned_lease_releases_it_before_readiness(self):
        from mobile_agent.verify.actions import ActionJournal, ActionNotDispatched

        with fake_manager() as (manager, simctl, wda, leases, root):
            previous = manager.acquire()
            previous.release()
            run_dir = root.parent / "cancel-claimed"
            run_dir.mkdir()
            journal = ActionJournal(run_dir)
            started = list(wda.started)
            with self.assertRaises(ActionNotDispatched):
                with journal.action("ACQUIRE"):
                    manager.acquire_udid(previous.target.udid, cancelled=lambda: bool(leases.held))
            self.assertIsNone(journal.klass)
            self.assertEqual(leases.held, set())
            self.assertEqual(wda.started, started)

    def test_busy_after_a_pinned_lease_is_uncertain_and_releases_it(self):
        from mobile_agent.verify.actions import ActionJournal, ActionNotDispatched

        with fake_manager() as (manager, simctl, wda, leases, root):
            first = manager.acquire()
            first.release()
            run_dir = root.parent / "late-busy"
            run_dir.mkdir()
            journal = ActionJournal(run_dir)
            with mock.patch.object(manager, "_make_ready", side_effect=SimError("busy", "late busy")):
                with self.assertRaises(SimError) as caught:
                    with journal.action("ACQUIRE"):
                        manager.acquire_udid(first.target.udid, timeout=0)
            self.assertIs(type(caught.exception), SimError)
            self.assertNotIsInstance(caught.exception, ActionNotDispatched)
            self.assertEqual(journal.klass, "unknown_outcome")
            self.assertEqual(leases.held, set())

    def test_acquire_udid_refuses_an_unknown_or_vanished_simulator_without_waiting(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            manager.sleep = lambda seconds: self.fail("waited")
            with self.assertRaises(SimError) as unknown:
                manager.acquire_udid("00000000-0000-4000-8000-000000000000", timeout=30)
            self.assertEqual(unknown.exception.kind, "simulator")
            first = manager.acquire()
            first.release()
            del simctl.devices[first.target.udid]
            with self.assertRaises(SimError) as gone:
                manager.acquire_udid(first.target.udid, timeout=30)
            self.assertIn("no longer exists", str(gone.exception))

    def test_acquire_udid_closes_the_lease_when_the_simulator_cannot_be_made_ready(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            first = manager.acquire()
            first.release()
            with mock.patch.object(manager, "_make_ready", side_effect=SimError("wda", "no", "fix")):
                with self.assertRaises(SimError):
                    manager.acquire_udid(first.target.udid)
            self.assertEqual(leases.held, set())

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
            self.assertEqual(wda.cancelled, 1)
            simctl.devices[manager.registry.read()[0]["udid"]]["state"] = "Booting"
            simctl.calls.clear()
            with manager.acquire() as target:
                pass
            self.assertEqual(simctl.simctl_calls("boot"), [])
            self.assertEqual(simctl.simctl_calls("bootstatus"), [[target.udid, "-b"]])

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

    def test_a_runner_whose_video_socket_never_opens_is_stopped_without_blaming_the_build(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            wda.mjpeg_listening = False
            with self.assertRaises(SimError) as caught:
                manager.acquire()
            self.assertEqual(caught.exception.kind, "wda")
            self.assertNotIn("beyond this Mac", str(caught.exception))
            self.assertIn("9310", str(caught.exception))
            self.assertNotIn("Delete", caught.exception.fix)
            self.assertIn("Run the command again", caught.exception.fix)
            self.assertEqual(len(wda.stopped), 1)
            self.assertEqual(leases.held, set())


def registry_locked(registry):
    registry.data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(registry.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def second_process(first):
    other = SimulatorManager(data_dir=first.data_dir, progress=None)
    other.run, other.wda, other.lease = first.run, first.wda, first.lease
    other.clock, other.sleep, other._tools = first.clock, first.sleep, first._tools
    other.governor = first.governor
    return other


class ConcurrentProcessTests(unittest.TestCase):

    def at_first_list(self, b, a_call):
        state, threads, fired = {}, [], []
        run = b.run

        def a_side():
            try:
                state["value"] = a_call()
            except BaseException as exc:
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
            with mock.patch.object(xcode, "installed_builds", return_value={TOOLS.build}):
                removed = b.prune()
            a_lease = a_result()
            self.assertEqual(removed["entries"], [])
            self.assertEqual([entry["udid"] for entry in a.registry.read()], [a_lease.target.udid])
            self.assertEqual(fired, [True], "prune's simctl list was taken outside the registry lock")
            a_lease.release()


class HousekeepingTests(unittest.TestCase):

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
            self.assertEqual(manager.delete(), [])
            self.assertEqual(simctl.simctl_calls("delete"), [])
            self.assertEqual(len(manager.registry.read()), 1)

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
            with mock.patch.object(xcode, "installed_builds", return_value={TOOLS.build}):
                removed = manager.prune()
            self.assertEqual(removed["simulators"], [old.target.udid])
            self.assertEqual(removed["entries"], ["0A1B2C3D-0000-4000-8000-00000000DEAD"])
            self.assertEqual(sorted(removed["wda_builds"]),
                             sorted([str(older), str(other_patch), str(folder / "src-00000000")]))
            self.assertTrue(current.is_dir())
            self.assertEqual([entry["udid"] for entry in manager.registry.read()], [fresh.target.udid])

    def test_list_joins_the_registry_with_simctl(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            self.assertEqual(manager.list(), [])
            held = manager.acquire()
            rows = manager.list()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(set(row), {"udid", "name", "device_type", "runtime", "state", "wda", "wda_url",
                                        "mjpeg_url", "in_use", "holder", "last_used_at"})
            self.assertEqual((row["state"], row["wda"], row["in_use"]), ("Booted", "ready", True))
            held.release()
            self.assertFalse(manager.list()[0]["in_use"])

    def test_status(self):
        with fake_manager() as (manager, simctl, wda, leases, root):
            status = manager.status()
            self.assertEqual(set(status), {"xcode", "runtime", "device_type", "wda_build", "simulators", "data_dir",
                                           "max_sims"})
            self.assertEqual(status["xcode"], {"version": "26.4", "build": "17E192", "path": "/Applications/Xcode.app"})
            self.assertEqual((status["runtime"], status["device_type"]), ("iOS 26.4", "iPhone 17 Pro"))
            self.assertEqual(status["wda_build"], {"cached": False, "path": None})
            self.assertEqual((status["simulators"], status["max_sims"]), ([], 4))


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
        (path / "Library" / "Preferences" / "org.mozilla.ios.Firefox.plist").write_text("x")
        return path

    def test_install_and_installed_app(self):
        info = self.manager.install(self.target, str(self.app))
        self.assertEqual(info, AppInfo("org.mozilla.ios.Firefox", "Firefox", "1.0 (1)", str(self.app.resolve())))
        self.assertEqual(self.simctl.simctl_calls("install"), [[self.target.udid, str(self.app.resolve())]])
        self.simctl.containers[(self.target.udid, "org.mozilla.ios.Firefox", "app")] = str(self.app)
        self.assertEqual(self.manager.installed_app(self.target, "org.mozilla.ios.Firefox"),
                         AppInfo("org.mozilla.ios.Firefox", "Firefox", "1.0 (1)", None))
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
            self.manager.reset(self.target, "org.mozilla.ios.Firefox", "reinstall", str(device))
        self.assertEqual(self.simctl.calls, [])

    def test_reset_data(self):
        data = self.container()
        group = self.container(name="GROUP")
        udid, bundle = self.target.udid, "org.mozilla.ios.Firefox"
        self.simctl.containers[(udid, bundle, "data")] = str(data)
        self.simctl.containers[(udid, bundle, "groups")] = f"group.org.mozilla.ios.Firefox\t{group}"
        self.manager.reset(self.target, bundle, "data")
        self.assertEqual(self.simctl.simctl_calls("terminate"), [[udid, bundle]])
        self.assertEqual(sorted(self.simctl.simctl_calls("spawn")),
                         [[udid, "defaults", "delete", "group.org.mozilla.ios.Firefox"],
                          [udid, "defaults", "delete", bundle]])
        self.assertEqual(self.simctl.simctl_calls("privacy"), [[udid, "reset", "all", bundle]])
        self.assertEqual(self.simctl.simctl_calls("keychain"), [[udid, "reset"]])
        for container in (data, group):
            for name in ("Documents", "Library", "tmp"):
                self.assertEqual(os.listdir(container / name), [])

    def test_reset_data_refuses_a_container_outside_the_simulator_before_changing_anything(self):
        other = "0A1B2C3D-0000-4000-8000-000000000002"
        theirs = self.container(udid=other)
        self.simctl.containers[(self.target.udid, "org.mozilla.ios.Firefox", "data")] = str(theirs)
        with self.assertRaises(SimError) as caught:
            self.manager.reset(self.target, "org.mozilla.ios.Firefox", "data")
        self.assertEqual(caught.exception.kind, "simulator")
        self.assertEqual(self.simctl.simctl_calls("spawn") + self.simctl.simctl_calls("privacy"), [])
        self.assertTrue((theirs / "Documents" / "state.json").exists())

    def test_reset_none_and_system_apps_do_nothing_but_read_for_an_alert(self):
        self.wda.http.requests.clear()
        self.manager.reset(self.target, "org.mozilla.ios.Firefox", "none")
        self.manager.reset(self.target, "com.apple.Preferences", "data")
        self.manager.reset(self.target, "com.apple.mobilesafari", "reinstall", str(self.app))
        self.assertEqual(self.simctl.calls, [])
        self.assertEqual(self.wda.http.requests, [("GET", "/alert/text", None)] * 3)
        with self.assertRaises(ValueError):
            self.manager.reset(self.target, "org.mozilla.ios.Firefox", "auto")

    def test_reinstall(self):
        udid = self.target.udid
        self.manager.reset(self.target, "org.mozilla.ios.Firefox", "reinstall", str(self.app))
        commands = [call[2] for call in self.simctl.calls]
        self.assertLess(commands.index("uninstall"), commands.index("install"))
        self.assertEqual(self.simctl.simctl_calls("uninstall"), [[udid, "org.mozilla.ios.Firefox"]])
        self.assertEqual(self.simctl.simctl_calls("privacy"), [[udid, "reset", "all", "org.mozilla.ios.Firefox"]])
        self.assertEqual(self.simctl.simctl_calls("keychain"), [[udid, "reset"]])
        with self.assertRaises(SimError) as caught:
            self.manager.reset(self.target, "org.mozilla.ios.Firefox", "reinstall")
        self.assertEqual(caught.exception.kind, "install")

    def test_a_reset_finishes_its_work_before_an_alert_that_stays_fails_it(self):
        data = self.container()
        self.simctl.containers[(self.target.udid, "org.mozilla.ios.Firefox", "data")] = str(data)
        self.wda.http.show("Software Update", ("Later",), sticky=True)
        with self.assertRaises(SimError) as caught:
            self.manager.reset(self.target, "org.mozilla.ios.Firefox", "data")
        self.assertIn("“Software Update”", str(caught.exception))
        self.assertEqual(self.simctl.simctl_calls("keychain"), [[self.target.udid, "reset"]])
        self.assertEqual(os.listdir(data / "Documents"), [])
        self.wda.http.alerts.clear()
        with self.assertRaises(SimError) as caught:
            self.manager.reset(self.target, "dev.example.missing", "data")
        self.assertEqual(caught.exception.kind, "install")

    def test_launch_passes_arguments_and_child_environment(self):
        self.manager.launch(self.target, "org.mozilla.ios.Firefox", ["-FirefoxSkipOnboarding", "YES"],
                            {"FIREFOX_SEED": "3"})
        call = self.simctl.calls[-1]
        self.assertEqual(call[2:], ["launch", "--terminate-running-process", self.target.udid,
            "org.mozilla.ios.Firefox",
                                    "-FirefoxSkipOnboarding", "YES"])
        self.assertEqual(self.simctl.envs[-1]["SIMCTL_CHILD_FIREFOX_SEED"], "3")
        self.simctl.fail["launch"] = [Result(4, "", "The request to open \"org.mozilla.ios.Firefox\" failed.")]
        with self.assertRaises(SimError) as caught:
            self.manager.launch(self.target, "org.mozilla.ios.Firefox")
        self.assertEqual(caught.exception.kind, "launch")

    def test_open_url_presses_open_on_the_prompt_ios_shows(self):
        http = self.prompt_on_openurl()
        self.manager.open_url(self.target, "firefox://paywall")
        self.assertEqual(http.pressed, [("Open in “Firefox”?", "Open")])
        self.assertEqual(http.requests, [("GET", "/alert/text", None), ("POST", "/alert/accept", {"name": "Open"}),
                                         ("GET", "/alert/text", None)])
        self.assertEqual(http.alerts, [])

    def test_open_url_leaves_any_other_alert_to_the_run(self):
        http = self.prompt_on_openurl(text="Allow “Firefox” to send you notifications?")
        self.manager.open_url(self.target, "firefox://settings")
        self.assertEqual(http.paths(), ["/alert/text"])
        self.assertEqual(http.pressed, [])
        self.assertEqual(len(http.alerts), 1)

    def test_open_url_fails_when_the_prompt_stays(self):
        http = self.prompt_on_openurl(sticky=True)
        with self.assertRaises(SimError) as caught:
            self.manager.open_url(self.target, "firefox://paywall")
        self.assertEqual(caught.exception.kind, "launch")
        self.assertIn("“Open in “Firefox”?” prompt stayed up", str(caught.exception))
        self.assertEqual(http.pressed, [("Open in “Firefox”?", "Open")] * 2)

    def test_screenshot_is_a_scaled_jpeg(self):
        from PIL import Image
        path = Path(self.folder.name) / "frames" / "01-launch.jpg"
        self.assertEqual(self.manager.screenshot(self.target, path, max_width=603, quality=75), path)
        with Image.open(path) as image:
            self.assertEqual((image.format, image.width, image.height), ("JPEG", 603, 1311))
        self.assertEqual(os.listdir(path.parent), ["01-launch.jpg"])
        self.assertEqual(self.simctl.simctl_calls("io")[0][:3], [self.target.udid, "screenshot", "--type=png"])

    def prompt_on_openurl(self, text="Open in “Firefox”?", sticky=False):
        http = self.wda.http
        self.simctl.hooks["openurl"] = lambda args: http.show(text, ("Cancel", "Open"), sticky=sticky)
        http.requests.clear()
        return http


if __name__ == "__main__":
    unittest.main()
