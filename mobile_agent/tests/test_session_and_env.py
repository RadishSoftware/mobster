"""WDA session resolution and env files. Offline."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mobile_agent.config import load_env_file
from mobile_agent.drivers import resolve_wda_session


class FakeHTTP:
    def __init__(self, status, created="NEW", accept_preferred=True):
        self.status, self.created, self.accept_preferred, self.calls = status, created, accept_preferred, []

    def __call__(self, url, *args):
        return self

    def request(self, method, path, body=None, timeout=20):
        self.calls.append((method, path))
        if path == "/status":
            return self.status
        if path == "/session":
            return {"value": {"sessionId": self.created}, "sessionId": self.created}
        if not self.accept_preferred:
            raise RuntimeError("invalid session id")
        return {"value": {"width": 1, "height": 1}}

    def close(self):
        pass


class SessionTests(unittest.TestCase):
    def resolve(self, fake, **kwargs):
        with patch("mobile_agent.drivers.HTTP", fake):
            return resolve_wda_session("http://127.0.0.1:8100", **kwargs)

    def test_the_session_wda_reports_wins_over_a_stale_one(self):
        self.assertEqual(self.resolve(FakeHTTP({"sessionId": "CURRENT"}), preferred="STALE"), "CURRENT")

    def test_a_still_valid_preferred_session_is_kept_when_wda_reports_none(self):
        self.assertEqual(self.resolve(FakeHTTP({"sessionId": None}), preferred="KEEP"), "KEEP")

    def test_a_session_is_created_only_when_none_exists(self):
        fake = FakeHTTP({"sessionId": None}, accept_preferred=False)
        self.assertEqual(self.resolve(fake, preferred="GONE"), "NEW")
        self.assertIn(("POST", "/session"), fake.calls)
        self.assertIsNone(self.resolve(FakeHTTP({"sessionId": None}), create=False))


class EnvFileTests(unittest.TestCase):
    def test_loads_without_overriding_explicit_environment(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write('# comment\nMOBSTER_TEST_A=one\nexport MOBSTER_TEST_B="two"\nMOBSTER_TEST_C=file\n\n')
        self.addCleanup(os.unlink, handle.name)
        with patch.dict(os.environ, {"MOBSTER_TEST_C": "explicit"}, clear=False):
            loaded, warnings = load_env_file(handle.name)
            self.assertEqual(os.environ["MOBSTER_TEST_A"], "one")
            self.assertEqual(os.environ["MOBSTER_TEST_B"], "two")
            self.assertEqual(os.environ["MOBSTER_TEST_C"], "explicit")
            self.assertEqual(sorted(loaded), ["MOBSTER_TEST_A", "MOBSTER_TEST_B"])
            self.assertEqual(warnings, [])
        for key in ("MOBSTER_TEST_A", "MOBSTER_TEST_B"):
            os.environ.pop(key, None)

    def test_skips_lines_it_cannot_read_and_says_which_without_their_text(self):
        with tempfile.NamedTemporaryFile("wb", suffix=".env", delete=False) as handle:
            handle.write(b"\xef\xbb\xbfMOBSTER_TEST_D=first\nBAD NAME=secret-one\nMOBSTER_TEST_E=caf\xe9\n"
                         b"just-a-secret-two\nMOBSTER_TEST_F=last\n")
        self.addCleanup(os.unlink, handle.name)
        with patch.dict(os.environ, {}, clear=False):
            loaded, warnings = load_env_file(handle.name)
            self.assertEqual(loaded, ["MOBSTER_TEST_D", "MOBSTER_TEST_F"])
            self.assertEqual(os.environ["MOBSTER_TEST_F"], "last")
        self.assertEqual([warning.split(" line ")[1].split()[0] for warning in warnings], ["2", "3", "4"])
        for warning in warnings:
            self.assertNotIn("secret", warning)
            self.assertNotIn("BAD", warning)

    def test_an_unreadable_file_is_a_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            loaded, warnings = load_env_file(folder)  # a directory, as a mistyped envFile gives
        self.assertEqual(loaded, [])
        self.assertIn("could not be read", warnings[0])

    def test_a_missing_file_is_a_warning_except_where_it_is_expected(self):
        """A mistyped --env-file would otherwise look like a missing key. `serve` passes missing_ok: the Mac
        app's setup creates its file when it saves the first value."""
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder, "missing.env")
            self.assertEqual(load_env_file(missing, missing_ok=True), ([], []))
            loaded, warnings = load_env_file(missing)
        self.assertEqual(loaded, [])
        self.assertEqual(warnings, [f"{missing} does not exist; no keys or settings were loaded from it"])
        self.assertEqual(load_env_file(Path.home() / "nope-mobster.env").warnings,
                         ["~/nope-mobster.env does not exist; no keys or settings were loaded from it"])


if __name__ == "__main__":
    unittest.main()


class LimitationTests(unittest.TestCase):
    def reasons(self, **overrides):
        from types import SimpleNamespace
        from mobile_agent.server import Runtime
        runtime = object.__new__(Runtime)
        runtime.config = SimpleNamespace(enable_live=overrides.pop("enable_live", True),
                                         wda_url="http://127.0.0.1:8100")
        values = dict(key=True, helper=True, device="Your iPhone", ready=True, wda=True,
                      can_act=False) | overrides
        return runtime.limitations(**values)

    def test_a_healthy_wda_setup_has_no_limitations(self):
        self.assertEqual(self.reasons(), [])

    def test_actionable_reasons_come_first(self):
        self.assertEqual(self.reasons(key=False, enable_live=False, ready=False)[:3], [
            "Add TYPESAFE_API_KEY to the agent's env file",
            "Live runs are off; enable them to act on the phone",
            "WebDriverAgent is not answering at http://127.0.0.1:8100"])

    def test_an_expired_runner_and_a_missing_iphone_say_what_to_do(self):
        from types import SimpleNamespace
        from mobile_agent.server import Runtime
        runtime = object.__new__(Runtime)
        runtime.config = SimpleNamespace(enable_live=True, wda_url="http://127.0.0.1:8100")
        runtime.manager = SimpleNamespace(expired=lambda: True)
        reasons = runtime.limitations(key=True, helper=True, device="Your iPhone", ready=False, wda=True,
                                      can_act=False)
        self.assertEqual(reasons[0], "Mobster's helper needs a refresh: its signature expired. Refresh it in Setup")
        self.assertIn("--manage-device", self.reasons(wda=False, device=None)[0])

    def test_a_refused_connection_is_reported_as_nothing_sent(self):
        import socket
        from mobile_agent.transport import HTTP, TransportError
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]  # closed again before the request: nothing listens there
        client = HTTP(f"http://127.0.0.1:{port}", timeout=2)
        with self.assertRaises(TransportError) as refused:
            client.request("GET", "/status")
        client.close()
        self.assertIn("nothing sent", str(refused.exception))


class ParentWatchTests(unittest.TestCase):
    def test_orphaned_server_process_terminates(self):
        import subprocess
        import sys
        import time
        with tempfile.TemporaryDirectory() as directory:
            pid_file = os.path.join(directory, "child.pid")
            child = ("import os, time\n"
                     "from mobile_agent.server import watch_parent\n"
                     f"open({pid_file!r}, 'w').write(str(os.getpid()))\n"
                     "watch_parent(os.getppid(), interval=.05)\n"
                     "time.sleep(30)\n")
            parent = subprocess.Popen([sys.executable, "-c",
                                       "import subprocess, sys, time\n"
                                       f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                                       "time.sleep(30)\n"], cwd=os.getcwd())
            deadline = time.monotonic() + 10
            while not os.path.exists(pid_file) and time.monotonic() < deadline:
                time.sleep(.05)
            with open(pid_file) as handle:
                child_pid = int(handle.read())
            parent.kill()
            parent.wait()
            deadline = time.monotonic() + 5
            alive = True
            while alive and time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                    time.sleep(.05)
                except ProcessLookupError:
                    alive = False
            if alive:
                os.kill(child_pid, 9)
            self.assertFalse(alive, "the orphaned child kept running")
