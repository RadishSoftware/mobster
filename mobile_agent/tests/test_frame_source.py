import base64
import io
import struct
import sys
import threading
import time
import unittest

from mobile_agent.cable_bt_runtime import CableVideo
from mobile_agent.wda import frame_source
from mobile_agent.wda.video import VideoUnavailable

HELPER = ("import struct,sys,time\n"
          "for i in range(int(sys.argv[1])):\n"
          "    data=bytes([0xff,0xd8])+bytes([i])*50\n"
          "    sys.stdout.buffer.write(struct.pack('>I',len(data))+data); sys.stdout.buffer.flush(); time.sleep(.02)\n"
          "time.sleep(30)\n")


class ReadFramesTests(unittest.TestCase):
    def test_a_short_or_absurd_frame_ends_the_stream(self):
        self.assertEqual(list(frame_source.read_frames(io.BytesIO(struct.pack(">I", 5) + b"ab"))), [])
        self.assertEqual(list(frame_source.read_frames(io.BytesIO(struct.pack(">I", 0)))), [])


class ProcessFrameSourceTests(unittest.TestCase):
    def source(self, frames=5):
        calls = []

        def command(fps, detail):
            calls.append((fps, detail))
            return [sys.executable, "-c", HELPER, str(frames)]
        source = frame_source.ProcessFrameSource(command, source="test_frames")
        self.addCleanup(source.close)
        return source, calls

    def test_a_viewer_gets_the_helpers_frames(self):
        source, calls = self.source()
        viewer = source.subscribe()
        frame = source.next_frame(viewer, 0, timeout=10)
        self.assertIsNotNone(frame)
        self.assertEqual(frame[1], "image/jpeg")
        self.assertEqual(frame[5], "test_frames")
        deadline = time.monotonic() + 10
        while (source.latest() or (0,))[0] < 5 and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(source.latest()[0], 5)
        self.assertEqual(source.status()["kind"], "process")
        self.assertEqual(calls, [(30, "medium")])
        source.unsubscribe(viewer)

    def test_a_quality_change_restarts_the_helper_at_the_new_quality(self):
        source, calls = self.source()
        viewer = source.subscribe()
        self.assertIsNotNone(source.next_frame(viewer, 0, timeout=10))
        source.set_quality(15, "low")
        deadline = time.monotonic() + 10
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(calls[-1], (15, "low"))
        with self.assertRaises(ValueError):
            source.set_quality(24, "low")
        source.unsubscribe(viewer)

    def test_the_helper_stops_when_the_source_closes(self):
        source, _ = self.source()
        viewer = source.subscribe()
        self.assertIsNotNone(source.next_frame(viewer, 0, timeout=10))
        process = source.process
        source.close()
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertIsNotNone(process.poll())


class FakePull:
    def __init__(self):
        self.version = 1
        self.calls = []
        self.lock = threading.Lock()
        self.fail = None

    def change(self):
        with self.lock:
            self.version += 1

    def __call__(self, width, quality, after):
        with self.lock:
            self.calls.append((width, quality, after))
            if self.fail:
                raise self.fail
            key = f"v{self.version}/{width}"
            if after == key:
                return None
            return f"jpeg {key}".encode(), key


def wait_for(condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(.01)
    return False


class FrameRelayTests(unittest.TestCase):
    def relay(self, pull):
        relay = frame_source.FrameRelay(pull, source="test", quality=(60, "high"))
        self.addCleanup(relay.close)
        return relay

    def test_a_focused_viewer_gets_the_full_picture_and_a_peek_a_tile(self):
        pull = FakePull()
        relay = self.relay(pull)
        frame = relay.peek(wait=2)
        self.assertEqual(frame[2], f"jpeg v1/{frame_source.TILE_WIDTH}".encode())
        self.assertEqual(frame[1], "image/jpeg")
        viewer = relay.subscribe()
        self.assertTrue(wait_for(lambda: relay.latest()[2] == b"jpeg v1/590"))
        relay.unsubscribe(viewer)
        widths = {width for width, _, _ in pull.calls}
        self.assertEqual(widths, {frame_source.TILE_WIDTH, 590})

    def test_paced_frames_yield_changes_to_a_viewer(self):
        pull = FakePull()
        relay = self.relay(pull)
        viewer = relay.subscribe()
        got = []

        def read():
            for frame in relay.paced_frames(viewer, rate=lambda: 30, heartbeat=5):
                got.append(frame[2])
                if len(got) == 2:
                    break

        reader = threading.Thread(target=read)
        reader.start()
        self.assertTrue(wait_for(lambda: len(got) == 1))
        pull.change()
        reader.join(3)
        self.assertEqual(got, [b"jpeg v1/590", b"jpeg v2/590"])
        relay.unsubscribe(viewer)

    def test_a_failing_pull_is_reported_not_raised(self):
        pull = FakePull()
        pull.fail = RuntimeError("The iPhone's screen isn't open")
        relay = self.relay(pull)
        viewer = relay.subscribe()
        self.assertTrue(wait_for(lambda: relay.status()["error"] == "The iPhone's screen isn't open"))
        self.assertIsNone(relay.latest())
        relay.unsubscribe(viewer)

    def test_quality_and_status(self):
        relay = self.relay(FakePull())
        relay.set_quality(15, "low")
        self.assertEqual(relay.mode(), (frame_source.TILE_WIDTH, frame_source.TILE_QUALITY, frame_source.TILE_FPS))
        viewer = relay.subscribe()
        self.assertEqual(relay.mode(), (295, .4, 15))
        relay.unsubscribe(viewer)
        with self.assertRaises(ValueError):
            relay.set_quality(24, "low")
        self.assertEqual(relay.status()["targetFps"], 15)
        self.assertIsNone(relay.reduced_fps())

    def test_closed_or_full_refuses_viewers(self):
        relay = self.relay(FakePull())
        viewers = [relay.subscribe(focus=False) for _ in range(8)]
        with self.assertRaises(VideoUnavailable):
            relay.subscribe()
        for viewer in viewers:
            relay.unsubscribe(viewer)
        relay.close()
        with self.assertRaises(VideoUnavailable):
            relay.subscribe()

    def test_streams_names_both_kinds(self):
        from mobile_agent.runtime_driver import NoVideo
        from mobile_agent.wda.video import WdaVideo
        self.assertTrue(frame_source.streams(frame_source.FrameRelay(FakePull())))
        self.assertTrue(frame_source.streams(WdaVideo("http://127.0.0.1:8100")))
        self.assertFalse(frame_source.streams(NoVideo()))
        self.assertFalse(frame_source.streams(None))


class EndpointTests(unittest.TestCase):
    def test_stream_and_frame_serve_a_relay(self):
        import http.client
        from types import SimpleNamespace
        from unittest.mock import Mock

        from mobile_agent.server import BoundedServer, make_handler
        from mobile_agent.wda.video import read_mjpeg_frames

        def pull(width, quality, after):
            return (None if after == "k" else (b"\xff\xd8 relay frame", "k"))

        runtime = Mock()
        runtime.config = SimpleNamespace(port=8765)
        runtime.stream_fps = lambda phone: 30
        runtime.video = frame_source.FrameRelay(pull, source="test")
        self.addCleanup(runtime.video.close)
        server = BoundedServer(("127.0.0.1", 0), make_handler(runtime))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        connection.request("GET", "/api/device/stream", headers={"Host": "127.0.0.1:8765"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(next(read_mjpeg_frames(response)), b"\xff\xd8 relay frame")
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        connection.request("GET", "/api/device/frame", headers={"Host": "127.0.0.1:8765"})
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b"\xff\xd8 relay frame"))
        connection.close()


class CableVideoTests(unittest.TestCase):
    class Screen:
        def __init__(self):
            self.requests = []

        def request(self, op, timeout=15, **fields):
            self.requests.append((op, fields))
            key = f"k/{fields['width']}"
            if fields.get("after") == key:
                return {"ok": True, "same": True, "key": key}
            return {"ok": True, "key": key, "jpeg": base64.b64encode(b"\xff\xd8 tile").decode(), "w": fields["width"]}

    def test_pulls_from_the_phones_own_capture_helper(self):
        screen = self.Screen()
        phone = type("Phone", (), {"screen": screen})()
        video = CableVideo(lambda: phone)
        self.addCleanup(video.close)
        frame = video.peek(wait=2)
        self.assertEqual(frame[2], b"\xff\xd8 tile")
        self.assertEqual(screen.requests[0], ("frame", {"width": frame_source.TILE_WIDTH,
                                                        "quality": frame_source.TILE_QUALITY, "after": None}))
        self.assertEqual(video.status()["kind"], "cable")


if __name__ == "__main__":
    unittest.main()
