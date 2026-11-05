import contextlib
import importlib.util
import io
import json
import os
import plistlib
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from mobile_agent.sim import (
    SimError,
    SimulatorManager,
    registry,
    simctl,
    wda,
)
from mobile_agent.sim.registry import Registry
from mobile_agent.sim.simctl import Result
from mobile_agent.sim.wda import build_dir
from mobile_agent.tests.sim_fixtures import ENTRY, OTHER, UDID
from mobile_agent.tests.test_sim_fakes import (
    DEVICE_TYPES,
    RUNTIMES,
    TOOLS,
    fake_manager,
)
from mobile_agent.wda import patches as wda_patches
from mobile_agent.wda import source as wda_source


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.registry = Registry(Path(self.folder.name) / "dev")

    def test_write_then_read_keeps_exactly_the_fields(self):
        with self.registry.edit() as entries:
            entries.append({**ENTRY, "extra": "dropped"})
        self.assertEqual(self.registry.read(), [ENTRY])
        data = json.loads(self.registry.path.read_text())
        self.assertEqual(list(data["simulators"][0]), list(registry.FIELDS))
        self.assertEqual(oct(self.registry.path.stat().st_mode & 0o777), "0o600")

    def test_a_failed_write_keeps_the_old_file_and_leaves_no_temporary(self):
        with self.registry.edit() as entries:
            entries.append(dict(ENTRY))
        with mock.patch(
            "mobile_agent.sim.registry.os.replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError), self.registry.edit() as entries:
                entries.append(
                    {**ENTRY, "udid": "0A1B2C3D-0000-4000-8000-000000000002"}
                )
        self.assertEqual(self.registry.read(), [ENTRY])
        self.assertEqual(
            sorted(path.name for path in self.registry.data_dir.iterdir()),
            ["simulators.json", "simulators.lock"],
        )

    def test_a_damaged_file_is_an_error_not_an_empty_registry(self):
        self.registry.data_dir.mkdir(parents=True)
        for text in (
            "{not json",
            '{"simulators": {}}',
            '{"simulators": [{"name": "no udid"}]}',
            "[]",
        ):
            with self.subTest(text=text):
                self.registry.path.write_text(text)
                with self.assertRaises(SimError) as caught:
                    self.registry.read()
                self.assertEqual(caught.exception.kind, "simulator")
                self.assertIn("Mobster · ", caught.exception.fix)

    def test_the_lock_excludes_a_second_holder(self):
        with self.registry.locked():
            with self.assertRaises(SimError) as caught:
                with self.registry.locked(timeout=0.05):
                    pass
        self.assertEqual(caught.exception.kind, "busy")
        with self.registry.locked(timeout=0.05):
            pass

    def test_concurrent_edits_lose_nothing(self):
        def add(number):
            with self.registry.edit() as entries:
                entries.append(
                    {**ENTRY, "udid": f"0A1B2C3D-0000-4000-8000-{number:012d}"}
                )

        threads = [threading.Thread(target=add, args=(number,)) for number in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.registry.read()), 24)

    def test_reserved_ports_are_never_given_out(self):
        for api, mjpeg in (
            (8100, 9100),
            (8201, 9201),
            (8299, 9299),
            (8765, 9765),
            (7200, 8200),
            (7765, 8765),
        ):
            with self.subTest(api=api):
                self.assertFalse(registry.allowed(api, mjpeg))
        self.assertTrue(registry.allowed(8310, 9310))
        self.assertEqual(
            registry.free_slot(8280, busy=lambda port: False), (8300, 9300)
        )

    def test_free_slot_skips_taken_and_busy_ports(self):
        self.assertEqual(
            registry.free_slot(8310, busy=lambda port: False), (8310, 9310)
        )
        self.assertEqual(
            registry.free_slot(8310, taken={8310}, busy=lambda port: False),
            (8311, 9311),
        )
        self.assertEqual(
            registry.free_slot(8310, taken={9311}, busy=lambda port: False),
            (8310, 9310),
        )
        self.assertEqual(
            registry.free_slot(8310, taken={9310}, busy=lambda port: False),
            (8311, 9311),
        )
        busy = {8310, 8311, 9312}
        self.assertEqual(registry.free_slot(8310, busy=busy.__contains__), (8313, 9313))

    def test_base_default_and_environment(self):
        self.assertEqual(registry.port_base({}), 8310)
        self.assertEqual(registry.port_base({"MOBSTER_SIM_PORT_BASE": "8320"}), 8320)
        for bad in ("abc", "80", "65000"):
            with self.subTest(bad=bad), self.assertRaises(SimError) as caught:
                registry.port_base({"MOBSTER_SIM_PORT_BASE": bad})
            self.assertEqual(caught.exception.kind, "environment")


LIST = {
    "devicetypes": DEVICE_TYPES,
    "runtimes": RUNTIMES,
    "devices": {
        "com.apple.CoreSimulator.SimRuntime.iOS-26-4": [
            {
                "udid": "0A1B2C3D-0000-4000-8000-000000000001",
                "name": "Mobster · iPhone 17 Pro · iOS 26.4",
                "state": "Booted",
                "isAvailable": True,
                "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
                "dataPath": "/Users/example/Library/Developer/CoreSimulator/Devices/"
                "0A1B2C3D-0000-4000-8000-000000000001/data",
            },
            {
                "udid": "0A1B2C3D-0000-4000-8000-000000000002",
                "name": "Another simulator",
                "state": "Shutdown",
                "isAvailable": False,
                "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-Air",
            },
            {"name": "no udid"},
            "not a dict",
        ],
        "com.apple.CoreSimulator.SimRuntime.watchOS-26-4": [],
    },
}


class ParsingTests(unittest.TestCase):
    def test_runtimes_keep_ios_only_with_versions_and_device_types(self):
        runtimes = simctl.parse_runtimes(LIST)
        self.assertEqual(
            [runtime.name for runtime in runtimes], ["iOS 26.4", "iOS 18.6", "iOS 27.0"]
        )
        self.assertEqual(runtimes[0].version, (26, 4))
        self.assertTrue(runtimes[0].available)
        self.assertFalse(runtimes[2].available)
        self.assertIn("iPhone 17 Pro", runtimes[0].device_types)

    def test_created_udid_and_groups(self):
        self.assertEqual(
            simctl.parse_created("0a1b2c3d-0000-4000-8000-000000000009\n"),
            "0A1B2C3D-0000-4000-8000-000000000009",
        )
        self.assertIsNone(simctl.parse_created("An error was encountered"))
        self.assertEqual(
            simctl.parse_groups(
                "group.dev.example.shared\t/path/to/Shared Group/X\n\n"
            ),
            [("group.dev.example.shared", "/path/to/Shared Group/X")],
        )
        self.assertEqual(simctl.parse_groups(""), [])

    def test_run_never_raises(self):
        result = simctl.run(["/nonexistent/tool"])
        self.assertIsNone(result.code)
        self.assertIn("could not run", result.err)


class DefaultTests(unittest.TestCase):
    def setUp(self):
        self.runtimes = simctl.parse_runtimes(LIST)
        self.kinds = simctl.parse_device_types(LIST)

    def test_missing_runtime_names_the_installed_ones_and_the_download(self):
        with self.assertRaises(SimError) as caught:
            simctl.choose_runtime(self.runtimes, "iOS 27.0")
        self.assertEqual(caught.exception.kind, "environment")
        self.assertIn("Installed: iOS 26.4, iOS 18.6", str(caught.exception))
        self.assertEqual(caught.exception.fix, "xcodebuild -downloadPlatform iOS")
        with self.assertRaises(SimError) as caught:
            simctl.choose_runtime([])
        self.assertEqual(caught.exception.kind, "environment")


class NamingTests(unittest.TestCase):
    def test_first_name_then_numbered(self):
        base = "Mobster · iPhone 17 Pro · iOS 26.4"
        self.assertEqual(simctl.sim_name("iPhone 17 Pro", "iOS 26.4"), base)
        self.assertEqual(
            simctl.sim_name("iPhone 17 Pro", "iOS 26.4", {base}), base + " · 2"
        )
        self.assertEqual(
            simctl.sim_name("iPhone 17 Pro", "iOS 26.4", {base, base + " · 2"}),
            base + " · 3",
        )
        self.assertEqual(
            simctl.sim_name("iPhone 17 Pro", "iOS 26.4", {base + " · 2"}), base
        )


def png(path, size=(4, 4)):
    Image.new("RGB", size).save(path, "PNG")
    return Path(path)


class DoneTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "shot.png"

    def child(self, code):
        return [sys.executable, "-c", code + "\nimport time; time.sleep(60)"]

    def test_a_whole_file_ends_the_command_and_counts_as_done(self):
        source = png(Path(self.folder.name) / "source.png")
        started = time.monotonic()
        result = simctl.run(
            self.child(
                f"import shutil; shutil.copy({str(source)!r}, {str(self.path)!r})"
            ),
            timeout=30,
            done=lambda: simctl.whole_png(self.path),
        )
        self.assertTrue(result.ok)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_half_written_file_is_not_done_and_the_run_times_out(self):
        whole = png(Path(self.folder.name) / "source.png").read_bytes()
        result = simctl.run(
            self.child(f"open({str(self.path)!r}, 'wb').write({whole[:-12]!r})"),
            timeout=1.5,
            done=lambda: simctl.whole_png(self.path),
        )
        self.assertIsNone(result.code)
        self.assertTrue(result.timed_out)

    def test_a_command_that_fails_before_it_is_done_reports_its_failure(self):
        result = simctl.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('Invalid device'); sys.exit(148)",
            ],
            timeout=10,
            done=lambda: False,
        )
        self.assertEqual((result.code, result.last_line()), (148, "Invalid device"))

    def test_screenshot_never_counts_a_stale_file(self):
        png(self.path)
        calls = []

        def runner(argv, timeout=60, env=None, done=None):
            calls.append(argv)
            return simctl.Result(0, "", "")

        result = simctl.screenshot("U1", self.path, runner=runner)
        self.assertFalse(result.ok)
        self.assertEqual(result.last_line(), "simctl wrote no whole PNG")
        self.assertEqual(
            calls,
            [
                [
                    simctl.XCRUN,
                    "simctl",
                    "io",
                    "U1",
                    "screenshot",
                    "--type=png",
                    str(self.path),
                ]
            ],
        )


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sim_smoke.py"


CLEANUP_LEG = "cleanup deleted exactly the run's simulators"


def load_smoke():
    spec = importlib.util.spec_from_file_location("sim_smoke_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SKIP_WITHOUT_SCRIPT = unittest.skipUnless(
    SCRIPT.is_file(), "the simulator smoke is not part of this copy"
)


@SKIP_WITHOUT_SCRIPT
class SmokeCleanupTests(unittest.TestCase):
    def run_smoke(self, argv, fail_fresh=False):
        smoke = load_smoke()
        with (
            fake_manager() as (first, simctl, wda, leases, root),
            tempfile.TemporaryDirectory() as folder,
        ):
            with first.acquire("iPhone Air") as kept:
                pass
            made = []

            def manager(*args, **kwargs):
                other = SimulatorManager(
                    data_dir=first.data_dir, progress=kwargs.get("progress")
                )
                other.run, other.wda, other.lease = first.run, first.wda, first.lease
                other.governor = first.governor
                other.clock, other.sleep, other._tools = (
                    first.clock,
                    first.sleep,
                    first._tools,
                )
                if fail_fresh and made:
                    other.acquire = mock.Mock(side_effect=RuntimeError("leg 2 broke"))
                made.append(other)
                return other

            report = Path(folder) / "smoke.json"
            with (
                mock.patch("mobile_agent.sim.SimulatorManager", manager),
                mock.patch.object(smoke, "simctl_udids", lambda: set(simctl.devices)),
                mock.patch.dict(os.environ, {"MOBSTER_DATA_DIR": str(first.data_dir)}),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = smoke.main([*argv, "--repeat", "1", "--json", str(report)])
            results = json.loads(report.read_text())["results"]
            registered = [entry["udid"] for entry in first.registry.read()]
            return code, results, registered, set(simctl.devices), kept.udid

    def test_fail_after_deletes_only_the_run_simulator(self):
        code, results, registered, listed, kept = self.run_smoke(["--fail-after", "1"])
        self.assertEqual(
            [result["leg"] for result in results],
            ["prepare", "stopped after leg 1 (--fail-after)", CLEANUP_LEG],
        )
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(code, 0)
        self.assertEqual(registered, [kept])
        self.assertEqual(listed, {kept})
        self.assertIn("deleted 1 the run added", results[-1]["detail"])
        self.assertIn(
            "kept 1 that were there before, lost: none", results[-1]["detail"]
        )

    def test_an_unexpected_error_still_deletes_only_the_run_simulator(self):
        code, results, registered, listed, kept = self.run_smoke([], fail_fresh=True)
        self.assertEqual(
            [(result["leg"], result["ok"]) for result in results],
            [("prepare", True), ("unexpected error", False), (CLEANUP_LEG, True)],
        )
        self.assertIn("leg 2 broke", results[1]["detail"])
        self.assertEqual(code, 1)
        self.assertEqual(registered, [kept])
        self.assertEqual(listed, {kept})

    def test_the_registry_snapshot_is_never_reassigned(self):
        import ast

        tree = ast.parse(SCRIPT.read_text())
        assigned = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AugAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            for name in ast.walk(target)
            if isinstance(name, ast.Name) and name.id == "registry_before"
        ]
        self.assertEqual(
            len(assigned), 1, f"registry_before is assigned on lines {assigned}"
        )


@SKIP_WITHOUT_SCRIPT
class SecondPrepareTests(unittest.TestCase):
    def second_prepare(self, shutdown=True):
        smoke = load_smoke()
        with fake_manager() as (manager, simctl, wda, leases, root):
            products = build_dir(manager.data_dir, TOOLS.build) / "Build" / "Products"
            products.mkdir(parents=True)
            (
                products / "WebDriverAgentRunner_iphonesimulator26.4-arm64.xctestrun"
            ).write_text("")
            if not shutdown:
                manager.shutdown = lambda udid=None: None
            recorder = smoke.Smoke(1)
            with contextlib.redirect_stdout(io.StringIO()):
                ok = smoke.second_prepare(recorder, manager)
            (udid,) = [entry["udid"] for entry in manager.registry.read()]
            return (
                ok,
                recorder.results,
                simctl.devices[udid]["state"],
                wda,
                leases,
                udid,
            )

    def test_the_second_prepare_is_shut_down_before_leg_15(self):
        ok, results, state, wda, leases, udid = self.second_prepare()
        self.assertTrue(ok, results)
        self.assertEqual(
            [result["leg"] for result in results],
            ["second prepare reuses the WebDriverAgent build"],
        )
        self.assertEqual(state, "Shutdown")
        self.assertIn(udid, wda.stopped)
        self.assertEqual(leases.held, set())
        self.assertIn("its simulator is Shutdown before leg 15", results[0]["detail"])

    def test_a_second_prepare_left_booted_fails_its_leg(self):
        ok, results, state, wda, leases, udid = self.second_prepare(shutdown=False)
        self.assertFalse(ok)
        self.assertEqual(state, "Booted")
        self.assertIn("its simulator is Booted before leg 15", results[0]["detail"])


PLAN = (
    "/Users/example/Library/Application Support/app.mobster.desktop/dev/wda/build-17E192-00c38220-0000aaaa/"
    "Build/Products/WebDriverAgentRunner_iphonesimulator26.4-arm64-x86_64.xctestrun"
)


HEADER = "COMMAND     PID USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME\n"


def lsof_line(host_port, pid=4242):
    return f"WebDriver {pid} someone    7u  IPv4 0x75e325d2062be109      0t0  TCP {host_port} (LISTEN)\n"


def ps_line(pid, udid, plan=PLAN):
    return (
        f"{pid} /Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild test-without-building "
        f"-xctestrun {plan} -destination id={udid}\n"
    )


RUNNER_PATH = (
    "/Users/example/Library/Developer/CoreSimulator/Devices/{udid}/data/Containers/Bundle/Application/"
    "998F1166-50C9-4ADF-B10A-F6B77AD64B9D/WebDriverAgentRunner-Runner.app/WebDriverAgentRunner-Runner\n"
)


class LsofTests(unittest.TestCase):
    def test_nothing_listening_is_not_loopback_only(self):
        self.assertEqual(wda.parse_lsof(""), [])
        self.assertEqual(wda.parse_lsof(HEADER), [])
        self.assertFalse(wda.loopback_only([]))

    def test_every_interface_or_another_address_is_exposed(self):
        for name, host in (
            ("*:9310", "*"),
            ("[::1]:9310", "::1"),
            ("192.168.1.20:9310", "192.168.1.20"),
            ("[::]:9310", "::"),
        ):
            with self.subTest(name=name):
                listeners = wda.parse_lsof(
                    HEADER + lsof_line("127.0.0.1:9310") + lsof_line(name)
                )
                self.assertEqual(listeners[1].host, host)
                self.assertFalse(wda.loopback_only(listeners))


class RunnerTests(unittest.TestCase):
    def test_runner_commands_only_for_this_simulator(self):
        output = (
            ps_line(11, UDID)
            + ps_line(12, OTHER)
            + "13 /bin/zsh\n"
            + f"14 xcodebuild build-for-testing -destination id={UDID}\n"
        )
        self.assertEqual([pid for pid, _ in wda.runner_commands(UDID, output)], [11])

    def test_the_test_plan_of_a_runner_with_spaces_in_its_path(self):
        command = wda.runner_commands(UDID, ps_line(11, UDID))[0][1]
        self.assertEqual(wda.xctestrun_of(command), PLAN)
        self.assertIsNone(wda.xctestrun_of("xcodebuild test-without-building"))

    def plan(self, **state):
        base = {
            "udid": UDID,
            "listening": True,
            "owner": UDID,
            "ready": True,
            "runners": wda.runner_commands(UDID, ps_line(11, UDID)),
            "xctestrun": PLAN,
        }
        return wda.runner_plan(**{**base, **state})

    def test_reuse_a_ready_runner_from_this_build(self):
        self.assertEqual(self.plan(), wda.REUSE)
        self.assertEqual(self.plan(owner=wda.UNCHECKED), wda.REUSE)

    def test_move_off_a_port_another_program_holds(self):
        self.assertEqual(self.plan(owner=None), wda.MOVE)
        self.assertEqual(self.plan(owner=OTHER), wda.MOVE)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        self.service = wda.WebDriverAgent(self.data)
        self.listening, self.lsof, self.ps, self.ready = set(), {}, "", set()
        self.commands = {}

        def run(argv, timeout=60, env=None):
            argv = [str(part) for part in argv]
            if argv[0] == "/usr/sbin/lsof":
                return Result(0, self.lsof.get(int(argv[2].split(":")[1]), ""), "")
            if argv[:2] == ["/bin/ps", "-axo"]:
                return Result(0, self.ps, "")
            if argv[:2] == ["/bin/ps", "-o"]:
                return Result(0, self.commands.get(int(argv[-1]), ""), "")
            raise AssertionError(argv)

        self.service.run = run
        self.service.listening = lambda port: port in self.listening
        self.service.ready = lambda url: url in self.ready
        self.service.sleep = lambda seconds: None

    def serve(self, port, udid, pid=4242, ready=True):
        self.listening.add(port)
        self.lsof[port] = HEADER + lsof_line(f"127.0.0.1:{port}", pid)
        self.commands[pid] = RUNNER_PATH.format(udid=udid)
        if ready:
            self.ready.add(f"http://127.0.0.1:{port}")

    def test_start(self):
        self.assertEqual(self.service.plan(UDID, 8310, 9310, PLAN), wda.START)

    def test_reuse_takes_one_ps_and_one_status_and_no_lsof(self):
        self.serve(8310, UDID)
        self.ps = ps_line(11, UDID)
        self.lsof = {}
        self.assertEqual(self.service.plan(UDID, 8310, 9310, PLAN), wda.REUSE)

    def test_a_stale_build_on_the_port_is_restarted_after_the_owner_check(self):
        self.serve(8310, UDID)
        self.serve(9310, UDID)
        self.ps = ps_line(11, UDID, PLAN.replace("17E192", "17F100"))
        self.assertEqual(self.service.plan(UDID, 8310, 9310, PLAN), wda.RESTART)

    def test_a_stranger_on_the_mjpeg_port_moves_even_when_the_api_port_is_ours(self):
        self.serve(8310, UDID, ready=False)
        self.serve(9310, OTHER, pid=5151)
        self.ps = ps_line(11, UDID)
        self.assertEqual(self.service.plan(UDID, 8310, 9310, PLAN), wda.MOVE)

    def test_loopback_check_waits_for_mjpeg_and_rejects_exposure(self):
        self.lsof[8310] = HEADER + lsof_line("127.0.0.1:8310")
        answers = iter(["", HEADER + lsof_line("127.0.0.1:9310")])
        original = self.service.run

        def run(argv, timeout=60, env=None):
            if argv[0] == "/usr/sbin/lsof" and argv[2] == "-iTCP:9310":
                return Result(
                    0, next(answers, HEADER + lsof_line("127.0.0.1:9310")), ""
                )
            return original(argv, timeout, env)

        self.service.run = run
        ok, found = self.service.check_loopback(8310, 9310)
        self.assertTrue(ok)
        self.assertEqual(found[9310][0].host, "127.0.0.1")
        self.service.run = original
        self.lsof[9310] = HEADER + lsof_line("*:9310")
        ok, found = self.service.check_loopback(8310, 9310)
        self.assertFalse(ok)

    def test_start_passes_ports_and_loopback(self):
        popen = mock.Mock()
        self.service.popen = popen
        self.service.start(UDID, 8310, 9310, PLAN, TOOLS)
        command = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(
            command,
            [
                TOOLS.xcodebuild,
                "test-without-building",
                "-xctestrun",
                PLAN,
                "-destination",
                f"id={UDID}",
            ],
        )
        self.assertEqual(
            {
                key: kwargs["env"][key]
                for key in (
                    "TEST_RUNNER_USE_PORT",
                    "TEST_RUNNER_MJPEG_SERVER_PORT",
                    "TEST_RUNNER_USE_IP",
                )
            },
            {
                "TEST_RUNNER_USE_PORT": "8310",
                "TEST_RUNNER_MJPEG_SERVER_PORT": "9310",
                "TEST_RUNNER_USE_IP": "127.0.0.1",
            },
        )
        self.assertTrue(kwargs["start_new_session"])
        self.assertIn(
            "TEST_RUNNER_USE_IP=127.0.0.1", self.service.runner_log(UDID).read_text()
        )

    def test_stop_signals_only_this_simulators_runner(self):
        self.ps = ps_line(11, UDID) + ps_line(12, OTHER)
        with (
            mock.patch.object(wda, "_signal") as signal,
            mock.patch.object(wda, "_alive", return_value=False),
        ):
            self.assertEqual(self.service.stop(UDID), [11])
        self.assertEqual({call.args[0] for call in signal.call_args_list}, {11})

    def test_wait_ready_reports_an_early_exit_with_the_log(self):
        self.service.logs.mkdir(parents=True)
        self.service.runner_log(UDID).write_text(
            "Testing started\nerror: Failed to install the runner\n"
        )
        process = mock.Mock()
        process.poll.return_value = 65
        with self.assertRaises(SimError) as caught:
            self.service.wait_ready(UDID, 8310, process, timeout=10)
        self.assertEqual(caught.exception.kind, "wda")
        self.assertIn("Failed to install the runner", str(caught.exception))

    def test_wait_ready_times_out(self):
        self.assertEqual(wda.READY_TIMEOUT, 240)
        clock = iter(range(0, 1000, 5))
        self.service.clock = lambda: next(clock)
        with self.assertRaises(SimError) as caught:
            self.service.wait_ready(UDID, 8310, None)
        self.assertEqual(
            str(caught.exception),
            "WebDriverAgent didn't answer on http://127.0.0.1:8310 within 240 s, "
            "which happens when this Mac is busy.",
        )
        self.assertTrue(caught.exception.fix.startswith("Run the command again."))


class BuildTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        self.service = wda.WebDriverAgent(self.data)

    def cached_plan(self, use_ip):
        products = wda.build_dir(self.data, TOOLS.build) / "Build" / "Products"
        products.mkdir(parents=True)
        plan = products / "WebDriverAgentRunner_iphonesimulator26.4-arm64.xctestrun"
        with open(plan, "wb") as stream:
            plistlib.dump(
                {
                    "TestConfigurations": [
                        {
                            "TestTargets": [
                                {
                                    "BlueprintName": "WebDriverAgentRunner",
                                    "EnvironmentVariables": {"USE_IP": use_ip},
                                }
                            ]
                        }
                    ],
                    "__xctestrun_metadata__": {"FormatVersion": 2},
                },
                stream,
            )
        self.service.run_logged = mock.Mock(side_effect=AssertionError("built again"))
        return plan

    def test_a_cached_build_is_reused_without_xcodebuild_or_the_build_lock(self):
        plan = self.cached_plan("127.0.0.1")
        with mock.patch.object(
            self.service, "build_lock", side_effect=AssertionError("locked")
        ):
            self.assertEqual(self.service.ensure_build(TOOLS), plan)

    def test_a_cached_build_that_was_never_pinned_is_pinned_before_it_is_used(self):
        plan = self.cached_plan("")
        self.assertFalse(wda.plan_is_pinned(plan))
        self.assertEqual(self.service.ensure_build(TOOLS), plan)
        self.assertTrue(wda.plan_is_pinned(plan))
        self.service.run_logged.assert_not_called()

    def test_pin_loopback_in_both_plan_formats(self):
        products = self.data / "Products"
        products.mkdir()
        one = products / "one_iphonesimulator.xctestrun"
        two = products / "two_iphonesimulator.xctestrun"
        with open(one, "wb") as stream:
            plistlib.dump(
                {
                    "WebDriverAgentRunner": {
                        "TestHostPath": "x",
                        "EnvironmentVariables": {"USE_IP": ""},
                    },
                    "__xctestrun_metadata__": {"FormatVersion": 1},
                },
                stream,
            )
        with open(two, "wb") as stream:
            plistlib.dump(
                {
                    "TestConfigurations": [
                        {"TestTargets": [{"BlueprintName": "WebDriverAgentRunner"}]}
                    ],
                    "__xctestrun_metadata__": {"FormatVersion": 2},
                },
                stream,
            )
        self.assertFalse(wda.plan_is_pinned(one))
        self.assertFalse(wda.plan_is_pinned(two))
        wda.pin_loopback(products)
        self.assertTrue(wda.plan_is_pinned(one))
        self.assertTrue(wda.plan_is_pinned(two))
        plan = plistlib.loads(two.read_bytes())
        plan["TestConfigurations"][0]["TestTargets"][0][
            "TestingEnvironmentVariables"
        ] = {"USE_IP": "0.0.0.0"}
        two.write_bytes(plistlib.dumps(plan))
        self.assertFalse(wda.plan_is_pinned(two))
        wda.pin_loopback(products)
        self.assertTrue(wda.plan_is_pinned(two))

    def test_the_source_is_cloned_pinned_verified_and_patched(self):
        calls = []

        def run(argv, timeout=60, env=None):
            calls.append(argv)
            Path(argv[-1]).mkdir(parents=True)
            return Result(0, "", "")

        self.service.run = run
        with (
            mock.patch.object(wda_source, "is_pinned", return_value=False),
            mock.patch.object(wda_source, "verify") as verify,
            mock.patch.object(wda_patches, "patch_wda") as patch,
        ):
            source = self.service.ensure_source(TOOLS)
        self.assertEqual(calls[0][:-1], wda_source.clone_command(TOOLS.git, "x")[:-1])
        verify.assert_called_once()
        patch.assert_called_once_with(source)
        self.assertTrue(source.is_dir())
        self.assertEqual(list(source.parent.glob(".src-*")), [])

    def test_a_long_build_times_out_and_can_be_cancelled(self):
        log = self.data / "build.log"
        self.assertIsNone(self.service.run_logged(["/bin/sleep", "30"], log, 0.2, None))
        self.assertIn("did not finish in 0.2 s", log.read_text())
        codes = []
        thread = threading.Thread(
            target=lambda: codes.append(
                self.service.run_logged(["/bin/sleep", "30"], log, 60, None)
            )
        )
        started = time.monotonic()
        thread.start()
        while not self.service._running and time.monotonic() - started < 5:
            time.sleep(0.01)
        self.service.cancel()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertNotEqual(codes, [0])
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.service._running, set())


class AlertTests(unittest.TestCase):
    def test_http_json_against_a_local_server(self):
        import http.server

        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def reply(self, status, value):
                body = json.dumps({"value": value, "sessionId": None}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                seen.append(("GET", self.path, None))
                if self.path == "/alert/text":
                    self.reply(200, "Open in “Sim Smoke”?")
                else:
                    self.reply(404, {"error": "unknown command"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                seen.append(
                    ("POST", self.path, json.loads(self.rfile.read(length) or b"null"))
                )
                self.reply(200, None)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        service = wda.WebDriverAgent(Path(folder.name))
        self.assertEqual(service.alert_text(url), "Open in “Sim Smoke”?")
        self.assertTrue(service.alert_action(url, "accept", "Open"))
        self.assertEqual(
            wda.http_json("GET", url + "/nothing"), (404, {"error": "unknown command"})
        )
        self.assertEqual(
            seen,
            [
                ("GET", "/alert/text", None),
                ("POST", "/alert/accept", {"name": "Open"}),
                ("GET", "/nothing", None),
            ],
        )
        server.shutdown()
        self.assertEqual(
            wda.http_json("GET", url + "/alert/text", timeout=1), (None, None)
        )
