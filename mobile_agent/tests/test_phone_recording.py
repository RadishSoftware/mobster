import fcntl
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent.phone_io import PhoneIOError, recording

_REAL_POPEN = subprocess.Popen


class MovieProcess:
    def __init__(self, *, error=None, answer=None, returncode=0, write=True, during=None, hangs=False):
        self.error, self.answer = error, answer if answer is not None else {"ok": True, "audio": True,
                                                                            "durationSeconds": 1.25}
        self.result_code, self.write, self.during, self.hangs = returncode, write, during, hangs
        self.returncode = None
        self.terminated = self.killed = False
        self.never_stops = False
        self.waits = []
        self.during_start = None

    def __call__(self, argv, **kwargs):
        self.argv, self.kwargs = argv, kwargs
        self.work = Path(argv[argv.index("--work") + 1])
        self.private = self.work.stat().st_mode & 0o777
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        if self.write:
            (self.work / "video.mov").write_bytes(b"fake movie data")
        if self.during_start:
            self.during_start()
        return self

    def communicate(self, timeout):
        if self.during:
            self.during()
        if self.error:
            raise self.error
        self.returncode = self.result_code
        return json.dumps(self.answer), ""

    def poll(self):
        return self.returncode

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.returncode is None:
            self.wait()
        self.stdout.close()
        self.stderr.close()
        return False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.never_stops:
            if timeout is None:
                raise AssertionError("capture cleanup must not wait without a timeout")
            raise subprocess.TimeoutExpired("helper", timeout)
        if self.hangs and not self.killed:
            raise subprocess.TimeoutExpired("helper", timeout)
        self.returncode = -9 if self.killed else -15
        return self.returncode


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.target = self.folder / "screen.mov"
        self.enterContext(mock.patch.dict(os.environ, {"MOBSTER_NO_DEVICES": ""}))
        self.enterContext(mock.patch("mobile_agent.paths.user_data_dir", return_value=self.folder / "data"))
        self.helper = self.enterContext(mock.patch.object(recording, "helper_path", return_value="/fake/helper"))
        self.hints = self.enterContext(mock.patch.object(recording, "capture_hints", return_value=[]))
        self.fake = MovieProcess()
        self.popen = self.enterContext(mock.patch.object(recording.subprocess, "Popen", side_effect=self.fake))

    def record(self, **kwargs):
        return recording.record_video("TEST-PHONE", output=self.target, **kwargs)

    def test_export_is_private_and_explicit_and_leaves_no_work_files(self):
        result = self.record(seconds=1.5)
        self.assertEqual(self.target.read_bytes(), b"fake movie data")
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.fake.private, 0o700)
        self.assertFalse(self.fake.work.exists())
        self.assertEqual(result, {"ok": True, "path": str(self.target), "seconds": 1.5, "bytes": 15,
                                  "audio": True, "transport": "usb", "format": "mov", "match": "udid",
                                  "durationSeconds": 1.25})
        self.assertEqual(self.fake.argv[:6], ["/fake/helper", "record-video", "--udid", "TEST-PHONE",
                                             "--seconds", "1.5"])
        self.assertTrue(self.fake.kwargs["start_new_session"])

    def test_devices_disabled_never_starts_helper(self):
        with mock.patch.dict(os.environ, {"MOBSTER_NO_DEVICES": "1"}), self.assertRaises(PhoneIOError) as caught:
            self.record()
        self.assertEqual(caught.exception.code, "devices_off")
        self.helper.assert_not_called()
        self.popen.assert_not_called()

    def test_capture_hints_reach_the_helper_and_its_match_is_reported(self):
        self.hints.return_value = ["--name", "Selected phone", "--phones", "1"]
        self.fake.answer["match"] = "name"
        result = self.record()
        self.hints.assert_called_once_with("TEST-PHONE")
        self.assertEqual(self.fake.argv[-4:], ["--name", "Selected phone", "--phones", "1"])
        self.assertEqual(result["match"], "name")

    def test_invalid_duration_and_missing_device_never_capture(self):
        for seconds in (0, 301, 10 ** 400, True, float("nan"), float("inf"), "10"):
            with self.subTest(seconds=seconds), self.assertRaises(PhoneIOError) as caught:
                self.record(seconds=seconds)
            self.assertEqual(caught.exception.code, "usage")
        for udid in (None, "", "--device", "two phones"):
            with self.subTest(udid=udid), self.assertRaises(PhoneIOError):
                recording.record_video(udid, output=self.target)
        self.popen.assert_not_called()

    def test_existing_output_and_dangling_symlink_are_never_overwritten(self):
        self.target.write_bytes(b"original")
        with self.assertRaises(PhoneIOError):
            self.record()
        self.assertEqual(self.target.read_bytes(), b"original")
        self.target.unlink()
        self.target.symlink_to(self.folder / "absent.mov")
        with self.assertRaises(PhoneIOError):
            self.record()
        self.assertTrue(self.target.is_symlink())
        self.popen.assert_not_called()

    def test_output_created_during_capture_wins(self):
        self.fake.during = lambda: self.target.write_bytes(b"other recording")
        with self.assertRaises(PhoneIOError) as caught:
            self.record()
        self.assertEqual(caught.exception.code, "usage")
        self.assertEqual(self.target.read_bytes(), b"other recording")
        self.assertFalse(self.fake.work.exists())

    def test_failure_and_missing_movie_never_publish(self):
        for answer, code, write in (({"ok": False, "error": "capture_busy", "message": "Busy."}, 0, True),
                                     ({"ok": True}, 1, True), ({"ok": True}, 0, False), ({}, 0, True)):
            self.fake.answer, self.fake.result_code, self.fake.write = answer, code, write
            with self.subTest(answer=answer, code=code, write=write), self.assertRaises(PhoneIOError):
                self.record()
            self.assertFalse(self.target.exists())
            self.assertFalse(self.fake.work.exists())

    def test_outdated_helper_has_update_instruction(self):
        self.fake.result_code = 2
        with self.assertRaises(PhoneIOError) as caught:
            self.record()
        self.assertEqual(caught.exception.code, "tools")
        self.assertIn("Update Mobster", str(caught.exception))
        self.assertFalse(self.fake.work.exists())

    def test_unreadable_duration_never_publishes_a_movie(self):
        for duration in (None, True, 0, -1, float("nan"), float("inf")):
            self.fake.answer["durationSeconds"] = duration
            with self.subTest(duration=duration), self.assertRaises(PhoneIOError):
                self.record()
            self.assertFalse(self.target.exists())

    def test_timeout_and_cancellation_stop_capture_and_delete_partial_movie(self):
        for error in (subprocess.TimeoutExpired("helper", 30), KeyboardInterrupt()):
            for hangs in (False, True):
                self.fake.error, self.fake.hangs = error, hangs
                self.fake.returncode = None
                self.fake.terminated = self.fake.killed = False
                expected = PhoneIOError if isinstance(error, subprocess.TimeoutExpired) else KeyboardInterrupt
                with self.subTest(error=type(error), hangs=hangs), self.assertRaises(expected):
                    self.record()
                self.assertTrue(self.fake.terminated)
                self.assertEqual(self.fake.killed, hangs)
                self.assertFalse(self.target.exists())
                self.assertFalse(self.fake.work.exists())

    def test_duplicate_capture_refuses_without_starting_second_helper(self):
        def duplicate():
            with self.assertRaises(PhoneIOError) as caught:
                recording.record_video("testphone", output=self.folder / "second.mov")
            self.assertEqual(caught.exception.code, "capture_busy")
        self.fake.during = duplicate
        self.record()
        self.assertEqual(self.popen.call_count, 1)

    def test_failed_stop_stays_bounded_and_closes_capture_pipes(self):
        self.fake.error = subprocess.TimeoutExpired("helper", 30)
        self.fake.never_stops = True
        with self.assertRaises(PhoneIOError) as caught:
            self.record()
        self.assertEqual(caught.exception.code, "capture_failed")
        self.assertTrue(self.fake.waits)
        self.assertTrue(all(timeout is not None and timeout > 0 for timeout in self.fake.waits))
        self.assertLessEqual(sum(self.fake.waits), 12)
        self.assertTrue(self.fake.stdout.closed)
        self.assertTrue(self.fake.stderr.closed)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.fake.work.exists())

    def test_signal_during_spawn_still_stops_and_reaps_the_started_helper(self):
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            self.fake.returncode = None
            self.fake.during_start = lambda: signal.raise_signal(number)
            with self.subTest(signal=number), self.assertRaises(KeyboardInterrupt):
                self.record()
            self.assertIsNotNone(self.fake.returncode)
            self.assertTrue(self.fake.terminated)
            self.assertTrue(self.fake.stdout.closed)
            self.assertTrue(self.fake.stderr.closed)
            self.assertFalse(self.target.exists())
            self.assertFalse(self.fake.work.exists())

    def test_repeated_interrupts_cannot_abort_bounded_capture_cleanup(self):
        numbers = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        previous = {number: signal.getsignal(number) for number in numbers}
        terminate = self.fake.terminate

        def interrupt_cleanup():
            terminate()
            for number in numbers:
                signal.raise_signal(number)

        for first in numbers:
            self.fake.returncode = None
            self.fake.during = lambda: signal.raise_signal(first)
            with self.subTest(first=first), mock.patch.object(self.fake, "terminate", side_effect=interrupt_cleanup):
                with self.assertRaises(KeyboardInterrupt):
                    self.record()
                self.assertIsNotNone(self.fake.returncode)
                self.assertTrue(self.fake.stdout.closed)
                self.assertTrue(self.fake.stderr.closed)
                self.assertFalse(self.target.exists())
                self.assertFalse(self.fake.work.exists())
                self.assertEqual({number: signal.getsignal(number) for number in numbers}, previous)

    def test_helper_keeps_capture_lock_after_parent_is_force_killed(self):
        ready = self.folder / "orphan.json"
        helper = self.folder / "orphan-helper"
        helper.write_text(f"#!{sys.executable}\n" + "\n".join([
            "import json, os, pathlib, sys, time",
            "work = pathlib.Path(sys.argv[sys.argv.index('--work') + 1])",
            f"pathlib.Path({str(ready)!r}).write_text(json.dumps({{'pid': os.getpid(), 'work': str(work)}}))",
            "time.sleep(30)",
        ]))
        helper.chmod(0o700)
        data = self.folder / "orphan-data"
        script = "\n".join([
            "from pathlib import Path",
            "from unittest.mock import patch",
            "from mobile_agent.phone_io.recording import record_video",
            f"with patch('mobile_agent.phone_io.recording.helper_path', return_value={str(helper)!r}), "
            "patch('mobile_agent.phone_io.recording.capture_hints', return_value=[]), "
            f"patch('mobile_agent.paths.user_data_dir', return_value=Path({str(data)!r})):",
            f"    record_video('FAKE', seconds=30, output={str(self.target)!r})",
        ])
        child_pid = None
        with mock.patch.object(recording.subprocess, "Popen", _REAL_POPEN):
            parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and parent.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "fake helper did not start")
                child_pid = json.loads(ready.read_text())["pid"]
                parent.kill()
                parent.wait(timeout=5)
                lock_path, = (data / "video-capture").glob("*.lock")
                with lock_path.open("a") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    os.kill(child_pid, signal.SIGTERM)
                    child_pid = None
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                self.fail("capture lock survived the fake helper exit")
                            time.sleep(0.01)
                self.assertFalse(self.target.exists())
            finally:
                if parent.poll() is None:
                    parent.kill()
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                parent.communicate(timeout=5)

    def test_termination_stops_real_fake_helper_and_removes_private_staging(self):
        ready = self.folder / "ready.json"
        helper = self.folder / "fake-helper"
        helper.write_text(f"#!{sys.executable}\n" + "\n".join([
            "import json, os, pathlib, sys, time",
            "work = pathlib.Path(sys.argv[sys.argv.index('--work') + 1])",
            "(work / 'video.mov').write_bytes(b'partial movie')",
            f"ready = pathlib.Path({str(ready)!r})",
            "ready.with_suffix('.tmp').write_text(json.dumps({'pid': os.getpid(), 'work': str(work)}))",
            "ready.with_suffix('.tmp').replace(ready)",
            "time.sleep(30)",
        ]))
        helper.chmod(0o700)
        script = "\n".join([
            "import json, os",
            "from pathlib import Path",
            "from unittest.mock import patch",
            "from mobile_agent.phone_io.recording import record_video",
            f"with patch('mobile_agent.phone_io.recording.helper_path', return_value={str(helper)!r}), "
            "patch('mobile_agent.phone_io.recording.capture_hints', return_value=[]), "
            f"patch('mobile_agent.paths.user_data_dir', return_value=Path({str(self.folder / 'child-data')!r})):",
            "    try:",
            f"        record_video('FAKE', seconds=30, output={str(self.target)!r})",
            "    except KeyboardInterrupt:",
            f"        captured = json.loads(Path({str(ready)!r}).read_text())",
            "        try:",
            "            os.waitpid(captured['pid'], os.WNOHANG)",
            "        except ChildProcessError:",
            "            raise SystemExit(130)",
            "        raise AssertionError('capture helper is still running or was not reaped')",
        ])
        with mock.patch.object(recording.subprocess, "Popen", _REAL_POPEN):
            with subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as process:
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(ready.exists(), "fake helper did not start")
                    captured = json.loads(ready.read_text())
                    process.send_signal(signal.SIGTERM)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 130, stdout + stderr)
                    self.assertFalse(Path(captured["work"]).exists())
                    self.assertFalse(self.target.exists())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
