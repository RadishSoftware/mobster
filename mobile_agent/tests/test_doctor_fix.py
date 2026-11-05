"""`mobster doctor --fix`: each finding is a row of the table, and each row's fix runs, asks or refuses as the row says.
Offline: fake managers, a fake clock, a fake process table; nothing touches a phone, a simulator or a process of this
Mac."""

import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import doctor, doctor_fix, states
from mobile_agent.device_manager import DeviceManager
from mobile_agent.doctor_fix import DECLINED, FAILED, FIXED, NEEDS_TERMINAL, REFUSED, YOURS, Context, Result, Step
from mobile_agent.journal import LeaseHeld
from mobile_agent.wireless import store
from mobile_agent.wireless.devicectl import Peer

PRO = "00008130-001A2B3C4D5E6F70"
IPROXY = "/opt/homebrew/bin/iproxy -s 127.0.0.1 -u <UDID> 8100:8100 9100:9100"
HEADER = ("Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)          rxbytes      txbytes  "
          "rhiwat  shiwat          process:pid    state  options\n")


def sockets(*rows):
    """`netstat -anv -p tcp` as macOS prints it, with a connection and the given (port, program, pid) listeners."""
    lines = [HEADER, "tcp4     230      0  127.0.0.1.49934        127.0.0.1.50034        ESTABLISHED          512"
                     "            0  408300  146988       python3.12:59483  00002 00000004\n"]
    for port, program, pid in rows:
        lines.append(f"tcp4       0      0  127.0.0.1.{port}         *.*                    LISTEN                 0"
                     f"            0  131072  131072           {program}:{pid}  00100 00000006\n")
    return "".join(lines)


NETSTAT_8100 = sockets((8100, "iproxy", 4321))


class Clock:
    """A clock that only ``sleep`` moves."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Build(dict):
    """A build whose state moves to its next each time it is read, and stays on the last."""

    def __init__(self, sequence, error=None):
        super().__init__()
        self.sequence, self.error = list(sequence), error

    def __getitem__(self, key):
        if key == "state":
            return self.sequence.pop(0) if len(self.sequence) > 1 else self.sequence[0]
        return self.error if key == "error" else None

    def get(self, key, default=None):
        return self.error if key == "error" else default


class FakeManager:
    wda_url, wda_port, video_port = "http://127.0.0.1:8100", 8100, 9100

    def __init__(self, *, built=True, expired=False, attached=True, answers=(False,), start_error=None, team=True,
                 builds=("running", "succeeded"), build_error=None, cause=None):
        self.is_built, self.is_expired, self.is_attached = built, expired, attached
        self.answers, self.start_error, self.team = list(answers), start_error, team
        self.builds, self.build_error = builds, build_error
        self.runner_cause = cause
        self.build = {"state": "idle", "error": None}
        self.started = 0
        self.build_calls = []

    def settings(self):
        return {"udid": "udid", "device_name": "Studio iPhone"}

    def attached(self):
        return self.is_attached

    def expired(self):
        return self.is_expired

    def built(self):
        return self.is_built

    def wda_ready(self, timeout=1.5):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def start_runner(self):
        if self.start_error:
            raise LookupError(self.start_error)
        self.started += 1
        if self.runner_cause is None:
            self.answers[:] = [True]

    def start_build(self, team=None, renewal=False):
        if not self.team:
            raise ValueError("Choose your Apple Account")
        self.build_calls.append(renewal)
        self.build = _Build(self.builds, self.build_error)

    def runner_hint(self):
        return None


def context(manager=None, *, answer=None, urls=(), read=None, netstat=None, alive=None):
    clock = Clock()
    asked, kills = [], []
    runtime = mock.Mock(manager=manager)
    opened = []
    # A port is held when the fake socket list has a listener on it.
    ctx = Context(None, ask=lambda question: asked.append(question) or answer,
                  open_runtime=lambda: opened.append(1) or runtime,
                  clock=clock, sleep=clock.sleep, netstat=netstat,
                  listening=lambda port: bool(netstat and port in dict(doctor_fix.parse_netstat(netstat()))),
                  urls=list(urls), read=read or (lambda url: None),
                  kill=lambda pid, number: kills.append((pid, number)), alive=alive or (lambda pid: False))
    ctx.asked, ctx.kills, ctx.opened = asked, kills, opened
    return ctx


class DoctorSaysTheRows(unittest.TestCase):
    """Each doctor check says a problem as its row. Together they produce exactly the rows marked for the CLI."""

    def setUp(self):
        self.causes = set()
        self.data = Path(tempfile.mkdtemp(prefix="mobster-doctor-fix-"))

    def seen(self, *checks):
        for check in checks:
            if check.cause:
                self.causes.add(check.cause)
        return checks[0] if len(checks) == 1 else checks

    def manager(self, devices, saved):
        manager = mock.Mock()
        manager.devices.return_value = devices
        manager.device.side_effect = lambda found=None: (found or [None])[0]
        manager.settings.return_value = saved
        return manager

    def scenario_a_phone_that_is_not_there_or_not_trusting(self):
        with mock.patch("mobile_agent.mac_tools.tool", lambda name: "/bin/" + name):
            gone = self.seen(doctor.check_usb(self.manager([], {"udid": "u", "device_name": "Studio iPhone"})))
            never = self.seen(doctor.check_usb(self.manager([], {})))
            untrusted = self.seen(doctor.check_usb(self.manager([{"name": "Studio iPhone", "trusted": False}], {})))
        self.assertEqual((gone.cause, gone.detail), ("unplugged", "Studio iPhone is unplugged."))
        self.assertEqual(never.cause, "none_chosen")
        self.assertEqual((untrusted.cause, untrusted.state), ("computer_untrusted", doctor.WARN))
        self.assertIn("tap Trust", untrusted.fix)

    def scenario_a_helper_that_expired_or_was_never_installed(self):
        manager = mock.Mock()
        manager.settings.return_value = {"udid": "u", "device_name": "Studio iPhone"}
        manager.signature_expiry.return_value = 1.0
        expired = self.seen(doctor.check_runner(manager))
        manager.signature_expiry.return_value = None
        manager.built.return_value = False
        missing = self.seen(doctor.check_runner(manager))
        self.assertEqual((expired.cause, expired.state), ("signature_expired", doctor.FAIL))
        self.assertIn("7-day signature ran out", expired.detail)
        self.assertEqual((missing.cause, missing.state), ("not_built", doctor.WARN))
        self.assertEqual(expired.public()["action"], {"kind": "auto", "action": "rebuild_helper"})

    def scenario_a_locked_phone_and_a_helper_that_does_not_answer(self):
        with mock.patch("mobile_agent.transport.HTTP") as http:
            http.return_value.request.return_value = {"value": True}
            locked = self.seen(doctor.check_locked("http://127.0.0.1:8100", "Studio iPhone"))
        self.assertEqual((locked.cause, locked.detail), ("locked", "Studio iPhone is locked. Mobster never enters a "
                                                                   "passcode."))
        manager = DeviceManager(self.data)
        stopped = self.seen(doctor.refine_wda("http://127.0.0.1:8100", manager))
        self.assertEqual((stopped.cause, stopped.state), ("stopped", doctor.FAIL))
        self.assertIn("Nothing answers at 127.0.0.1:8100.", stopped.detail)
        self.assertEqual(stopped.public()["action"], {"kind": "auto", "action": "start_helper"})

    def scenario_the_cause_the_helpers_log_names_while_it_is_fresh(self):
        manager = DeviceManager(self.data)
        manager.logs.mkdir(parents=True, exist_ok=True)
        log = manager.logs / "wda-runner.log"
        for line, cause in (("Developer Mode is disabled on this device", "developer_mode_off"),
                            ("The device has not been explicitly trusted", "trust_needed"),
                            ("Timed out while enabling automation mode", "ui_automation_off")):
            log.write_text(line + "\n")
            found = self.seen(doctor.refine_wda("http://127.0.0.1:8100", manager))
            self.assertEqual(found.cause, cause)
        self.assertEqual(found.public()["action"], {"kind": "yours", "picture": "ui-automation"})
        # A start from another day says nothing about this one.
        log.write_text("Developer Mode is disabled on this device\n")
        found = doctor.refine_wda("http://127.0.0.1:8100", manager, now=log.stat().st_mtime + doctor.LOG_FRESH_S + 1)
        self.assertEqual(found.cause, "stopped")

    def scenario_the_wifi_lines(self):
        os.environ["MOBSTER_WIFI_TRANSPORT"] = "1"
        self.addCleanup(os.environ.pop, "MOBSTER_WIFI_TRANSPORT")
        (self.data / "device.json").write_text(json.dumps({"udid": PRO}))
        store.put(self.data, PRO, True, "mobster")
        found = {check.key: check for check in doctor.check_wifi(
            PRO, self.data, "Studio iPhone", peers={PRO: Peer(udid=PRO, transport=None, developer_mode="disabled")},
            nwi="IPv4 network interface information\n     en0 : flags : 0x5 (IPv4,DNS)\n", gateway="192.168.1.1",
            local_network=True, network_result={"state": "ok", "interface": "en0", "peers": 4})}
        self.seen(found["wifi"], found["devmode"])
        self.assertEqual((found["wifi"].cause, found["devmode"].cause), ("wifi_lost", "developer_mode_off"))
        self.assertEqual(found["devmode"].detail, "Developer Mode is off on Studio iPhone.")

    def scenario_a_program_on_the_port_the_network_and_the_service(self):
        manager = mock.Mock(wda_port=8100, video_port=9100)
        with mock.patch("mobile_agent.supervised.command_of", return_value=IPROXY):
            port = self.seen(doctor.check_port(manager, netstat=lambda: NETSTAT_8100,
                                               listening=lambda number: number == 8100))
        self.assertEqual((port.cause, port.state), ("port_taken", doctor.WARN))
        self.assertIn("iproxy (pid 4321) holds 8100", port.detail)
        self.assertEqual(port.public()["action"], {"kind": "ask", "action": "free_port"})
        free = doctor.check_port(manager, netstat=lambda: sockets(), listening=lambda number: False)
        self.assertEqual((free.state, free.cause), (doctor.OK, ""))
        # Held, but the list couldn't say by whom (it timed out): named as held, and nothing is stopped for it.
        unread = doctor.check_port(manager, netstat=lambda: "", listening=lambda number: number == 9100)
        self.assertEqual((unread.cause, unread.detail.endswith("Something holds 9100, and Mobster couldn't read "
                                                                "which program.")), ("port_taken", True))
        gone = self.seen(doctor.check_network(interface=""))
        self.assertEqual((gone.cause, gone.state), ("network_gone", doctor.WARN))
        self.assertEqual(doctor.check_network(interface="en0").state, doctor.OK)
        frozen = self.seen(doctor.check_service(read=lambda url: eh_silence(9.0), urls=["http://127.0.0.1:8765"]))
        down = self.seen(doctor.check_service(read=lambda url: eh_silence(0.0, refused=True),
                                              urls=["http://127.0.0.1:8765"]))
        self.assertEqual((frozen.cause, down.cause, frozen.state), ("no_answer", "not_running", doctor.WARN))
        self.assertEqual(doctor.check_service(read=lambda url: None, urls=["http://127.0.0.1:8765"]).state, doctor.OK)
        # Nothing ever started it: nothing to say, and not a problem for the command line.
        self.assertEqual(doctor.check_service(urls=[]).state, doctor.SKIP)

    def scenario_the_key_and_the_simulator_runtime(self):
        key = self.seen(doctor.finding("key", "Model key", doctor.FAIL, "no_key"))
        self.assertEqual(key.public()["action"], {"kind": "guided", "command": "mobster login"})
        # `mobster sim doctor` says the runtime row (tests/test_sim_doctor.py runs it through the simulator manager).
        self.seen(doctor.finding("runtime", "iOS runtime", doctor.FAIL, "no_runtime"))

    def test_the_findings_cover_exactly_the_rows_marked_for_the_command_line(self):
        for name in dir(self):
            if name.startswith("scenario_"):
                with self.subTest(name):
                    getattr(self, name)()
        self.assertEqual(self.causes, {row.cause for row in states.ROWS if "cli" in row.surfaces})


def eh_silence(age, refused=False):
    from mobile_agent import engine_health
    return engine_health.from_silence(age, refused=refused)


class PlanTests(unittest.TestCase):
    def test_one_run_per_distinct_fix_in_the_order_they_depend_on_each_other(self):
        checks = [doctor.finding("key", "k", doctor.FAIL, "no_key"),
                  doctor.finding("usb", "u", doctor.WARN, "none_chosen"),
                  doctor.finding("runner", "r", doctor.WARN, "not_built"),
                  doctor.finding("wda", "w", doctor.FAIL, "stopped"),
                  doctor.finding("port", "p", doctor.WARN, "port_taken"),
                  doctor.finding("locked", "l", doctor.FAIL, "locked"),
                  doctor.Check("python", "p", doctor.OK, "3.12"),
                  doctor.Check("xcode", "x", doctor.WARN, "no cause")]
        causes = [row.cause for _check, row in doctor_fix.plan(checks)]
        # The port first, then the helper, then the flows that ask, then what is the person's. `mobster setup` once for
        # the two findings that name it.
        self.assertEqual(causes, ["port_taken", "stopped", "no_key", "none_chosen", "locked"])

    def test_an_ok_finding_and_a_finding_that_is_not_a_row_have_nothing_to_run(self):
        ok = doctor.Check("usb", "u", doctor.OK, "fine", cause="unplugged")
        self.assertEqual(doctor_fix.plan([ok, doctor.Check("xcode", "x", doctor.FAIL, "x")]), [])


class StartHelperTests(unittest.TestCase):
    def test_it_starts_the_helper_and_waits_for_it_to_answer(self):
        manager = FakeManager(answers=(False,))
        result = doctor_fix.start_helper(context(manager), None)
        self.assertEqual((result.outcome, manager.started), (FIXED, 1))
        self.assertIn("Started Mobster's helper on Studio iPhone", result.detail)

    def test_a_helper_that_already_answers_is_not_started_again(self):
        manager = FakeManager(answers=(True,))
        self.assertEqual(doctor_fix.start_helper(context(manager), None).outcome, FIXED)
        self.assertEqual(manager.started, 0)

    def test_it_leaves_the_phone_to_a_running_mobster_service(self):
        manager = FakeManager()
        ctx = context(manager, urls=["http://127.0.0.1:8765"])
        result = doctor_fix.start_helper(ctx, None)
        self.assertEqual((result.outcome, manager.started, ctx.opened), (REFUSED, 0, []))   # not even opened
        self.assertIn("manages the iPhone's helper itself", result.detail)
        # A service that is frozen or gone manages nothing.
        frozen = context(manager, urls=["http://127.0.0.1:8765"], read=lambda url: eh_silence(9.0))
        self.assertEqual(doctor_fix.start_helper(frozen, None).outcome, FIXED)

    def test_it_leaves_a_phone_another_mobster_is_running_a_task_on(self):
        manager = FakeManager()
        with mock.patch("mobile_agent.journal.Lease.device", side_effect=LeaseHeld("busy")):
            result = doctor_fix.start_helper(context(manager), None)
        self.assertEqual((result.outcome, manager.started), (REFUSED, 0))
        self.assertIn("running a task on Studio iPhone", result.detail)

    def test_it_refuses_what_other_rows_fix(self):
        for manager, said in ((FakeManager(attached=False), "Studio iPhone is unplugged."),
                              (FakeManager(expired=True), "signature has expired"),
                              (FakeManager(built=False), "isn't built for Studio iPhone")):
            with self.subTest(said=said):
                result = doctor_fix.start_helper(context(manager), None)
                self.assertEqual((result.outcome, manager.started), (REFUSED, 0))
                self.assertIn(said, result.detail)

    def test_it_says_why_a_start_was_refused(self):
        manager = FakeManager(start_error="Another Mobster is using port 8100 or 9100 on this Mac.")
        result = doctor_fix.start_helper(context(manager), None)
        self.assertEqual((result.outcome, result.detail),
                         (REFUSED, "Another Mobster is using port 8100 or 9100 on this Mac."))

    def test_a_helper_that_never_answers_names_the_cause_it_logged(self):
        manager = FakeManager(answers=(False,), cause="developer_mode_off")
        manager.start_runner = lambda: setattr(manager, "started", manager.started + 1)
        result = doctor_fix.start_helper(context(manager), None)
        self.assertEqual(result.outcome, FAILED)
        self.assertIn("didn't answer in 90 s. Developer Mode is off on Studio iPhone. Turn it on", result.detail)
        self.assertEqual(manager.started, 1)   # once: the outcome of a start that did not answer is not retried


class RebuildHelperTests(unittest.TestCase):
    def test_it_refreshes_an_expired_helper_with_the_saved_account_then_starts_it(self):
        manager = FakeManager(expired=True)
        result = doctor_fix.rebuild_helper(context(manager), None)
        self.assertEqual((result.outcome, manager.build_calls, manager.started), (FIXED, [True], 1))

    def test_with_no_saved_account_it_points_at_setup(self):
        manager = FakeManager(expired=True, team=False)
        result = doctor_fix.rebuild_helper(context(manager), None)
        self.assertEqual((result.outcome, result.detail), (REFUSED, "Choose your Apple Account. Run mobster setup."))

    def test_a_failed_build_says_why_and_starts_nothing(self):
        manager = FakeManager(expired=True, builds=("running", "failed"), build_error="Xcode could not sign it.")
        result = doctor_fix.rebuild_helper(context(manager), None)
        self.assertEqual((result.outcome, result.detail, manager.started), (FAILED, "Xcode could not sign it.", 0))

    def test_a_build_that_outlasts_its_time_is_left_running_and_said(self):
        manager = FakeManager(expired=True, builds=("running",))
        result = doctor_fix.rebuild_helper(context(manager), None)
        self.assertEqual((result.outcome, manager.started), (FAILED, 0))
        self.assertIn("still running after 20 minutes", result.detail)

    def test_a_helper_that_has_not_expired_is_only_started(self):
        manager = FakeManager(expired=False)
        self.assertEqual(doctor_fix.rebuild_helper(context(manager), None).outcome, FIXED)
        self.assertEqual(manager.build_calls, [])

    def test_it_leaves_a_running_service_to_refresh_its_own_helper(self):
        manager = FakeManager(expired=True)
        result = doctor_fix.rebuild_helper(context(manager, urls=["http://127.0.0.1:8765"]), None)
        self.assertEqual((result.outcome, manager.build_calls), (REFUSED, []))


class FreePortTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("mobile_agent.supervised.command_of", side_effect=lambda pid: self.commands.get(pid))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.commands = {4321: IPROXY}

    def test_a_free_port_needs_nothing(self):
        ctx = context(FakeManager(), netstat=lambda: sockets())
        self.assertEqual(doctor_fix.free_port(ctx, None).outcome, FIXED)
        self.assertEqual(ctx.kills, [])

    def test_without_a_terminal_it_names_the_program_and_stops_nothing(self):
        ctx = context(FakeManager(), netstat=lambda: NETSTAT_8100, answer=None)
        result = doctor_fix.free_port(ctx, None)
        self.assertEqual(result.outcome, NEEDS_TERMINAL)
        self.assertIn("iproxy -s 127.0.0.1", result.detail)
        self.assertIn("Port 8100 is held by /opt/homebrew/bin/iproxy", result.detail)
        self.assertIn("(pid 4321). It may be another Mobster's connection to a phone. Stop it?", result.detail)
        self.assertEqual(ctx.kills, [])

    def test_a_port_held_by_a_program_the_list_could_not_name_is_left_alone(self):
        ctx = context(FakeManager(), netstat=lambda: "", answer=True)
        ctx.listening = lambda port: port == 8100
        result = doctor_fix.free_port(ctx, None)
        self.assertEqual((result.outcome, ctx.kills, ctx.asked), (REFUSED, [], []))
        self.assertIn("couldn't read which program", result.detail)

    def test_a_no_stops_nothing(self):
        ctx = context(FakeManager(), netstat=lambda: NETSTAT_8100, answer=False)
        self.assertEqual(doctor_fix.free_port(ctx, None).outcome, DECLINED)
        self.assertEqual(ctx.kills, [])

    def test_a_yes_stops_that_one_process_and_checks_it_is_gone(self):
        ctx = context(FakeManager(), netstat=lambda: NETSTAT_8100, answer=True, alive=lambda pid: False)
        result = doctor_fix.free_port(ctx, None)
        self.assertEqual((result.outcome, ctx.kills), (FIXED, [(4321, signal.SIGTERM)]))
        self.assertIn("Stopped", result.detail)

    def test_a_process_that_ignores_the_request_is_forced_only_after_a_second_yes(self):
        answers = iter([True, False])
        ctx = context(FakeManager(), netstat=lambda: NETSTAT_8100, alive=lambda pid: True)
        ctx.ask = lambda question: ctx.asked.append(question) or next(answers)
        result = doctor_fix.free_port(ctx, None)
        self.assertEqual((result.outcome, ctx.kills), (DECLINED, [(4321, signal.SIGTERM)]))
        self.assertEqual(len(ctx.asked), 2)
        self.assertIn("Force it to stop?", ctx.asked[1])
        answers = iter([True, True])
        ctx = context(FakeManager(), netstat=lambda: NETSTAT_8100, alive=lambda pid: True)
        ctx.ask = lambda question: next(answers)
        self.assertEqual(doctor_fix.free_port(ctx, None).outcome, FIXED)
        self.assertEqual(ctx.kills, [(4321, signal.SIGTERM), (4321, signal.SIGKILL)])

    def test_a_process_that_changed_since_it_was_named_is_left_alone(self):
        ctx = context(FakeManager(), netstat=lambda: NETSTAT_8100, answer=True)
        original = ctx.ask

        def ask(question):
            self.commands[4321] = "/usr/bin/some-other-program"   # the pid was reused while the question was open
            return original(question)
        ctx.ask = ask
        result = doctor_fix.free_port(ctx, None)
        self.assertEqual((result.outcome, ctx.kills), (REFUSED, []))
        self.assertIn("no longer", result.detail)

    def test_a_process_of_another_user_is_not_forced(self):
        ctx = context(FakeManager(), netstat=lambda: NETSTAT_8100, answer=True)
        ctx.kill = mock.Mock(side_effect=PermissionError)
        result = doctor_fix.free_port(ctx, None)
        self.assertEqual(result.outcome, FAILED)
        self.assertIn("belongs to another user", result.detail)

    def test_it_leaves_the_ports_of_a_running_service_alone(self):
        ctx = context(FakeManager(), netstat=lambda: NETSTAT_8100, answer=True, urls=["http://127.0.0.1:8765"])
        self.assertEqual(doctor_fix.free_port(ctx, None).outcome, REFUSED)
        self.assertEqual(ctx.kills, [])


class RestartServiceTests(unittest.TestCase):
    def setUp(self):
        self.commands = {777: "/Applications/Mobster.app/Contents/MacOS/mobster-agent serve --manage-device"}
        patcher = mock.patch("mobile_agent.supervised.command_of", side_effect=lambda pid: self.commands.get(pid))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def netstat():
        return sockets((8765, "mobster-agent", 777))

    def test_it_names_the_service_asks_and_stops_it(self):
        ctx = context(urls=["http://127.0.0.1:8765"], netstat=self.netstat, answer=True)
        result = doctor_fix.restart_service(ctx, None)
        self.assertEqual((result.outcome, ctx.kills), (FIXED, [(777, signal.SIGTERM)]))
        self.assertIn("Mobster's service (pid 777) isn't answering", ctx.asked[0])
        self.assertIn("is kept in History and is not resent", ctx.asked[0])

    def test_without_a_terminal_it_stops_nothing(self):
        ctx = context(urls=["http://127.0.0.1:8765"], netstat=self.netstat, answer=None)
        self.assertEqual(doctor_fix.restart_service(ctx, None).outcome, NEEDS_TERMINAL)
        self.assertEqual(ctx.kills, [])

    def test_it_stops_only_a_program_with_mobster_in_its_command(self):
        self.commands[777] = "/usr/bin/python3 -m http.server 8765"
        ctx = context(urls=["http://127.0.0.1:8765"], netstat=self.netstat, answer=True)
        result = doctor_fix.restart_service(ctx, None)
        self.assertEqual((result.outcome, ctx.kills), (REFUSED, []))
        self.assertIn("not Mobster", result.detail)

    def test_a_service_that_is_gone_is_the_openers_to_start(self):
        ctx = context(urls=["http://127.0.0.1:8765"], netstat=lambda: sockets(), answer=True)
        result = doctor_fix.restart_service(ctx, None)
        self.assertEqual(result.outcome, REFUSED)
        self.assertIn("Mobster isn't running. Open the Mobster app.", result.detail)
        self.assertEqual(doctor_fix.restart_service(context(urls=[]), None).outcome, REFUSED)


class RunTests(unittest.TestCase):
    def checks(self, *causes):
        return [doctor.finding(cause, cause, doctor.WARN, cause) for cause in causes]

    def test_what_is_the_persons_is_reported_and_not_run(self):
        steps = doctor_fix.run(self.checks("locked", "developer_mode_off", "not_running", "restarted"), context())
        self.assertEqual([(step.cause, step.kind, step.outcome) for step in steps],
                         [("locked", "yours", YOURS), ("developer_mode_off", "yours", YOURS),
                          ("not_running", "run", YOURS), ("restarted", "wait", YOURS)])
        self.assertIn("Run `open -a Mobster`. Mobster doesn't run it for you.", steps[2].detail)

    def test_a_guided_flow_needs_a_terminal_and_otherwise_reports_its_exit(self):
        ctx = context()
        steps = doctor_fix.run(self.checks("no_key"), ctx, terminal=False)
        self.assertEqual((steps[0].outcome, steps[0].kind), (NEEDS_TERMINAL, "guided"))
        with mock.patch("mobile_agent.doctor_fix.guided", return_value=0) as flow:
            steps = doctor_fix.run(self.checks("no_key", "none_chosen"), ctx)
        self.assertEqual([step.outcome for step in steps], [FIXED, FIXED])
        self.assertEqual([call.args[0] for call in flow.call_args_list], ["mobster login", "mobster setup"])
        with mock.patch("mobile_agent.doctor_fix.guided", return_value=130):
            self.assertEqual(doctor_fix.run(self.checks("no_key"), ctx)[0].outcome, FAILED)

    def test_the_unasked_fixes_run_before_the_flows_and_each_action_once(self):
        calls = []
        fake = {name: (lambda ctx, check, name=name: calls.append(name) or Result(FIXED, name))
                for name in doctor_fix.ACTIONS}
        with mock.patch.dict(doctor_fix.ACTIONS, fake), mock.patch("mobile_agent.doctor_fix.guided", return_value=0):
            doctor_fix.run(self.checks("no_key", "stopped", "failing", "port_taken"), context())
        self.assertEqual(calls, ["free_port", "start_helper"])


class ReportTests(unittest.TestCase):
    def test_the_report_says_what_was_done_and_what_was_not(self):
        steps = [Step("stopped", "wda", "auto", FIXED, "Started Mobster's helper on Studio iPhone. It answers."),
                 Step("port_taken", "port", "ask", NEEDS_TERMINAL, "Port 8100 is held by iproxy (pid 4321). "
                      "Stop it? Run mobster doctor --fix in a terminal to answer."),
                 Step("locked", "locked", "yours", YOURS, "You do this one.")]
        report = doctor.plain_report([doctor.Check("python", "Python", doctor.OK, "3.12")], steps=steps)
        self.assertIn("What --fix did", report)
        self.assertIn("✓ Started Mobster's helper on Studio iPhone. It answers.", report)
        self.assertIn("– Port 8100 is held by iproxy (pid 4321).", report)
        self.assertNotIn("You do this one", report)   # the person's own step is under its check, not here
        self.assertIn("Nothing Mobster can do for you.", doctor.plain_report([], steps=[steps[2]]))
        self.assertNotIn("What --fix did", doctor.plain_report([]))


if __name__ == "__main__":
    unittest.main()
