"""The USB helper supervisor stops only its own children. All device commands are fake processes."""

import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "usb-wda.sh"


@unittest.skipUnless(SCRIPT.is_file(), "the USB helper script isn't in this checkout")
class ShutdownTests(unittest.TestCase):
    def test_shutdown_reaps_both_helpers_and_leaves_other_jobs_alive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            helper = (f"#!{sys.executable}\n"
                      "import os, pathlib, sys, time\n"
                      "name = pathlib.Path(sys.argv[0]).name\n"
                      "if '-h' in sys.argv:\n"
                      "    print('--source')\n"
                      "else:\n"
                      "    pathlib.Path(os.environ['WDA_LOG_DIR'], name + '.pid').write_text(str(os.getpid()))\n"
                      "    time.sleep(60)\n")
            for name in ("xcodebuild", "iproxy"):
                path = root / name
                path.write_text(helper)
                path.chmod(0o700)
            curl = root / "curl"
            curl.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"value":{"ready":true},"sessionId":"fake"}\'\n')
            curl.chmod(0o700)
            # A disposable process group contains the script and an unrelated job. The old `kill 0`
            # trap killed this wrapper and that job too; the test runner stays outside the group.
            wrapper = """
import os, pathlib, signal, subprocess, sys, time
root, script = map(pathlib.Path, sys.argv[1:])
sibling = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
process = subprocess.Popen(['bash', str(script)], stdout=subprocess.DEVNULL)
try:
    end = time.monotonic() + 5
    paths = [root / (name + '.pid') for name in ('xcodebuild', 'iproxy')]
    while not all(path.exists() for path in paths) and time.monotonic() < end:
        time.sleep(.02)
    assert all(path.exists() for path in paths), 'fake helpers never started'
    helpers = [int(path.read_text()) for path in paths]
    process.terminate()
    assert process.wait(timeout=5) == 143
    assert sibling.poll() is None, 'shutdown killed an unrelated job'
    for pid in helpers:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        raise AssertionError('shutdown left a helper running')
finally:
    sibling.terminate()
    sibling.wait()
"""
            env = {**os.environ, "PATH": f"{root}:/usr/bin:/bin", "UDID": "<UDID>", "TEAM": "TEAM123456",
                   "WDA_LOG_DIR": folder}
            process = subprocess.Popen([sys.executable, "-c", wrapper, folder, str(SCRIPT)], env=env,
                                       start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True)
            try:
                out, err = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, out + err)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()


if __name__ == "__main__":
    unittest.main()
