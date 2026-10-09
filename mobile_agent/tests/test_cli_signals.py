"""Signals and the processes Mobster starts: a stopped `verify` or `sim` never leaves xcodebuild behind.

The first WebDriverAgent build runs in its own process group for up to 20 minutes, so neither ctrl+c nor the end
of Mobster's process reaches it on its own. These run a real child (`sleep`, standing in for xcodebuild) in a
real Python process and check the child is gone afterwards.
"""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for(predicate, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.05)
    return predicate()


class OrphanBuildTests(unittest.TestCase):
    """The child stands in for xcodebuild: a shell that writes its pid, then sleeps in its own process group."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.folder)], check=False))
        self.pidfile = self.folder / "child.pid"

    def script(self, body):
        prelude = f"""
            import sys, threading, time
            from pathlib import Path
            from types import SimpleNamespace
            from mobile_agent import devtools
            from mobile_agent.sim.wda import WebDriverAgent
            folder = Path({str(self.folder)!r})
            service = WebDriverAgent(folder)
            command = ["/bin/sh", "-c", "echo $$ > {self.pidfile}; exec sleep 300"]

            def build():
                service.run_logged(command, folder / "build.log", 600, None)
        """
        return textwrap.dedent(prelude) + textwrap.dedent(body)

    def start(self, body):
        errors = self.folder / "stderr.txt"
        with open(errors, "wb") as stream:
            process = subprocess.Popen([sys.executable, "-c", self.script(body)], cwd=ROOT,
                                       env={**os.environ, "PYTHONPATH": str(ROOT)},
                                       stdout=subprocess.DEVNULL, stderr=stream)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertTrue(wait_for(lambda: self.pidfile.is_file() and self.pidfile.read_text().strip()),
                        errors.read_text() or "no child pid")
        child = int(self.pidfile.read_text())
        self.addCleanup(lambda: alive(child) and os.killpg(child, signal.SIGKILL))
        return process, child

    def test_the_build_ends_with_the_process_whose_daemon_thread_ran_it(self):
        process, child = self.start("""
            threading.Thread(target=build, daemon=True).start()
            while not Path(%r).is_file():
                time.sleep(.02)
            time.sleep(.2)
            sys.exit(0)
        """ % str(self.pidfile))
        self.assertEqual(process.wait(15), 0)
        self.assertTrue(wait_for(lambda: not alive(child)), "xcodebuild's stand-in outlived Mobster")

    def test_sigterm_and_sighup_stop_sim_like_ctrl_c_and_exit_128_plus_the_signal(self):
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number.name):
                self.pidfile.unlink(missing_ok=True)
                process, child = self.start("""
                    class Module:
                        @staticmethod
                        def run(args):
                            thread = threading.Thread(target=build, daemon=True)
                            thread.start()
                            thread.join()
                            return 0
                    devtools.load = lambda name: Module
                    sys.exit(devtools.run(SimpleNamespace(command="sim")))
                """)
                process.send_signal(number)
                self.assertEqual(process.wait(15), 128 + number)
                self.assertTrue(wait_for(lambda: not alive(child)), "xcodebuild's stand-in outlived Mobster")


SLOW_RUN = """
import sys, time
import mobile_agent.__main__ as cli, mobile_agent.compose as compose, mobile_agent.drivers as drivers
from mobile_agent.demo import DemoDriver, DemoModel, DemoHelper
class SlowModel(DemoModel):
    def decide(self, *a, **k):
        print("deciding", file=sys.stderr, flush=True)
        time.sleep(1.5)
        return super().decide(*a, **k)
cli.run_models = lambda args, observe: (SlowModel(), DemoHelper())
compose.build_target_driver = lambda **kw: DemoDriver()
drivers.resolve_wda_session = lambda url, preferred=None, **k: "fake-session"
sys.exit(cli.main(["run", "Search for coffee", "--execute", "--json", "--engine", "fast", "--wda-url", "http://127.0.0.1:9"]))
"""


class RunStopTests(unittest.TestCase):
    """`run --json` stopped mid-task still ends with its `result` event (review correctness-3): the first signal
    stops at the next safe point, a second forces it."""

    def stopped(self, signals):
        with tempfile.TemporaryDirectory() as home:
            process = subprocess.Popen([sys.executable, "-c", SLOW_RUN], cwd=ROOT,
                                       env={**os.environ, "PYTHONPATH": str(ROOT), "HOME": home,
                                            "MOBSTER_DATA_DIR": home},
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
            self.addCleanup(lambda: process.poll() is None and process.kill())
            line = b""
            while b"deciding" not in line:
                line = process.stderr.readline()
                self.assertTrue(line, "the run never reached a decision")
            for number in signals:
                process.send_signal(number)
                time.sleep(.1)
            out, err = process.communicate(timeout=30)
            process.stderr.close()
            process.stdout.close()
        import json
        return process.returncode, [json.loads(line) for line in out.decode().splitlines()], err.decode()

    def test_the_first_signal_stops_at_the_next_safe_point_with_a_result(self):
        for number, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=number.name):
                returncode, events, err = self.stopped([number])
                self.assertEqual(returncode, code, err)
                self.assertEqual((events[-1]["event"], events[-1]["status"]), ("result", "stopped"))
                self.assertIn("stopping at the next safe point", err)

    def test_a_second_signal_forces_the_stop_and_still_prints_a_result(self):
        returncode, events, err = self.stopped([signal.SIGINT, signal.SIGINT])
        self.assertEqual(returncode, 130, err)
        self.assertEqual((events[-1]["event"], events[-1]["status"], events[-1]["exit_code"]),
                         ("result", "stopped", 130))


class TuiSignalTests(unittest.TestCase):
    """The terminal UI in a real pty: SIGTERM and SIGHUP close it the way ctrl+d does, so the terminal is usable
    afterwards (review correctness-1: it was left raw, on the alternate screen, with mouse reporting on)."""

    def run_and_signal(self, number):
        import pty
        import select
        import termios
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", home], check=False))
        pid, fd = pty.fork()
        if pid == 0:  # the child: the real `mobster --demo`, no phone, no keys
            os.environ.update(TERM="xterm-256color", COLUMNS="100", LINES="30", HOME=home, MOBSTER_DATA_DIR=home,
                              PYTHONPATH=str(ROOT))
            for name in ("MOBSTER_WDA_URL", "MOBSTER_ENV_FILE", "MOBSTER_WDA_DEVICES"):
                os.environ.pop(name, None)
            os.chdir(ROOT)
            os.execv(sys.executable, [sys.executable, "-m", "mobile_agent", "--demo",
                                      "--wda-url", "http://127.0.0.1:59999"])
        self.addCleanup(os.close, fd)
        output = b""

        def pump(seconds, until=None):
            nonlocal output
            end = time.monotonic() + seconds
            while time.monotonic() < end and not (until and until in output):
                ready, _, _ = select.select([fd], [], [], .05)
                if ready:
                    try:
                        output += os.read(fd, 65536)
                    except OSError:
                        return
        pump(30, until=b"?1049h")
        self.assertIn(b"?1049h", output, "the UI never started")
        pump(1.0)
        os.kill(pid, number)
        pump(10, until=b"?1049l")
        pump(.3)
        _, status = os.waitpid(pid, 0)
        flags = termios.tcgetattr(fd)[3]
        return os.waitstatus_to_exitcode(status), flags, output

    def test_sigterm_and_sighup_restore_the_terminal(self):
        import termios
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number.name):
                code, flags, output = self.run_and_signal(number)
                self.assertEqual(code, 128 + number)
                self.assertTrue(flags & termios.ICANON and flags & termios.ECHO, "the terminal was left raw")
                for on, off in ((b"?1049h", b"?1049l"), (b"?1000h", b"?1000l"), (b"?2004h", b"?2004l"),
                                (b"?25l", b"?25h")):
                    if on in output:
                        self.assertGreater(output.rfind(off), output.rfind(on), off)


class SignalsAsInterruptTests(unittest.TestCase):
    def test_handlers_are_restored_and_mcp_keeps_its_own(self):
        from mobile_agent import devtools
        before = signal.getsignal(signal.SIGTERM)
        with devtools.signals_as_interrupt() as caught:
            self.assertIsNot(signal.getsignal(signal.SIGTERM), before)
            self.assertEqual(caught, [])
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        seen = []
        module = SimpleNamespace(run=lambda args: seen.append(signal.getsignal(signal.SIGTERM)) or 0)
        with mock.patch.object(devtools, "load", lambda name: module):
            self.assertEqual(devtools.run(SimpleNamespace(command="mcp")), 0)
        self.assertEqual(seen, [before])

    def test_a_terminated_run_is_a_keyboard_interrupt_with_its_signal(self):
        from mobile_agent import devtools
        error = devtools.Terminated(signal.SIGTERM)
        self.assertIsInstance(error, KeyboardInterrupt)
        self.assertEqual(error.signum, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
