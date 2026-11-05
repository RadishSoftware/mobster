import io
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from mobile_agent.testkit import execute as E
from mobile_agent.testkit import phone_video
from mobile_agent.tests.test_verify_runner import Manager
from mobile_agent.verify.checks import check_from_dict


class PhoneVideoTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)

    class Helper:
        def __init__(self, argv, **kwargs):
            self.argv, self.stdin, self.stderr, self.returncode = argv, io.BytesIO(), io.BytesIO(), None
            self.stdin.close = lambda: None
            Path(argv[1]).write_bytes(b"mp4")

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    def records(self, data):
        out, at = [], 0
        while at < len(data):
            ms, length = struct.unpack(">QI", data[at:at + 12])
            out.append((ms, data[at + 12:at + 12 + length]))
            at += 12 + length
        return out

    def test_frames_go_to_the_helper_once_each_with_their_time(self):
        made = []
        clock = SimpleNamespace(now=100.0)

        def popen(argv, **kwargs):
            made.append(self.Helper(argv))
            return made[-1]
        frames = [b"A", b"A", b"B", b"C"]
        done, closed = threading.Event(), threading.Event()
        sock = SimpleNamespace(shutdown=lambda how: closed.set(), close=lambda: None)

        def connect(url):
            def gen():
                for frame in frames:
                    clock.now += .1
                    yield frame
                done.set()
                closed.wait(5)  # a still screen: no frame until the socket is shut down
            return sock, gen()
        started = []
        recorder = phone_video.Recorder("http://127.0.0.1:9100", self.dir / "v.mp4", log=lambda line: None,
                                        clock=lambda: clock.now, helper="/bin/mjpeg-mp4", popen=popen,
                                        connect=connect, on_first=started.append)
        recorder.start()
        done.wait(2)
        clock.now += 1
        recorder.stop()
        sent = self.records(made[0].stdin.getvalue())
        self.assertEqual([jpeg for _, jpeg in sent], [b"A", b"B", b"C", b"C"])  # a repeat is skipped; the last
        self.assertEqual([ms for ms, _ in sent], [0, 200, 300, 1300])             # frame shows until the end
        self.assertEqual(made[0].argv[:2], ["/bin/mjpeg-mp4", str(self.dir / "v.mp4")])
        self.assertEqual(started, [100.1])
        self.assertTrue((self.dir / "v.mp4").is_file())

    def test_no_frames_no_file(self):
        def connect(url):
            raise ConnectionRefusedError
        made = []
        recorder = phone_video.Recorder("http://127.0.0.1:9100", self.dir / "v.mp4", log=lambda line: None,
                                        clock=time.monotonic, helper="/bin/h",
                                        popen=lambda argv, **kw: made.append(self.Helper(argv)) or made[-1],
                                        connect=connect, on_first=lambda at: None)
        recorder.start()
        recorder.stop()
        self.assertFalse((self.dir / "v.mp4").exists())

    def test_only_usb_selects_phone_recording_before_device_gate_refusal(self):
        attempts = E.Attempts(runs_dir=self.dir / "runs", video_dir=self.dir / "videos", keyless=True,
                              devices=lambda record: Manager())
        seen = {}

        class Run:
            def __init__(self, check, *, manager, **kwargs):
                seen["manager"] = manager
                self.error, self.state, self.result = None, "new", None

            def abort(self, klass, message):
                return {"verdict": "couldnt_run", "reason": {"class": klass, "message": message}}
        attempts.run_class = Run
        check = check_from_dict({"app": {"bundle": "com.example.app"}})
        for kind, manager in (("usb", phone_video.PhoneRecording), ("wda", Manager)):
            with self.subTest(kind=kind):
                result = attempts(check, {"kind": kind, "udid": "U", "wdaUrl": "http://127.0.0.1:8100"},
                                  label="x", video_name=f"001-x-{kind}")
                self.assertIsInstance(seen["manager"], manager)
                self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "device"))
                self.assertIn("device gate", result["reason"]["message"])
                self.assertNotIn("video", result)


if __name__ == "__main__":
    unittest.main()
