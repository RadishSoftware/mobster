"""WDA MJPEG relay with screenshot fallback. Local fake servers, no device."""

import base64
import http.client
import io
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

from mobile_agent.wda_video import WdaVideo, read_mjpeg_frames

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-body\xff\xd9"


def part(data, length=True):
    head = b"--BoundaryString\r\nContent-type: image/jpg\r\n"
    if length:
        head += f"Content-Length: {len(data)}\r\n".encode()
    return head + b"\r\n" + data + b"\r\n"


class Served(ThreadingHTTPServer):
    daemon_threads = True

    def shutdown(self):
        super().shutdown()
        self.server_close()


def serve(handler):
    server = Served(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class MjpegHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=BoundaryString")
        self.end_headers()
        try:
            for index in range(200):
                self.wfile.write(part(JPEG + bytes([index % 250])))
                self.wfile.flush()
                time.sleep(.02)
        except OSError:
            pass


class WdaHandler(BaseHTTPRequestHandler):
    png = b"\x89PNG\r\n\x1a\nfake"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"value": base64.b64encode(self.png).decode()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


class ParserTests(unittest.TestCase):
    def test_parts_with_and_without_length(self):
        stream = io.BytesIO(part(JPEG) + part(JPEG + b"x", length=False))
        frames = list(read_mjpeg_frames(stream))
        self.assertEqual(frames[0], JPEG)
        self.assertTrue(frames[1].endswith(b"\xff\xd9"))

    def test_absurd_lengths_are_refused(self):
        stream = io.BytesIO(b"--B\r\nContent-Length: 999999999\r\n\r\n")
        with self.assertRaises(ValueError):
            list(read_mjpeg_frames(stream))


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.wda = serve(WdaHandler)
        self.addCleanup(self.wda.shutdown)

    def video(self, mjpeg_port):
        video = WdaVideo(f"http://127.0.0.1:{self.wda.server_address[1]}",
                         mjpeg_url=f"http://127.0.0.1:{mjpeg_port}", session=lambda: "s")
        self.addCleanup(video.close)
        return video

    def test_mjpeg_frames_reach_every_viewer(self):
        mjpeg = serve(MjpegHandler)
        self.addCleanup(mjpeg.shutdown)
        video = self.video(mjpeg.server_address[1])
        first, second = video.subscribe(), video.subscribe()
        frame = video.next_frame(first, 0, timeout=5)
        self.assertIsNotNone(frame)
        self.assertEqual(frame[1], "image/jpeg")
        self.assertTrue(frame[2].startswith(b"\xff\xd8"))
        later = video.next_frame(second, frame[0], timeout=5)
        self.assertGreater(later[0], frame[0])
        self.assertEqual(video.status()["sourceKind"], "wda_mjpeg")
        video.unsubscribe(first)
        video.unsubscribe(second)

    def test_falls_back_to_screenshots_without_an_mjpeg_server(self):
        probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        unused = probe.server_address[1]
        probe.server_close()
        video = self.video(unused)
        viewer = video.subscribe()
        frame = video.next_frame(viewer, 0, timeout=8)
        self.assertIsNotNone(frame)
        self.assertEqual(frame[1], "image/png")
        self.assertEqual(video.status()["sourceKind"], "wda_screenshot")
        video.unsubscribe(viewer)

    def test_frames_carry_their_source(self):
        mjpeg = serve(MjpegHandler)
        self.addCleanup(mjpeg.shutdown)
        video = self.video(mjpeg.server_address[1])
        viewer = video.subscribe()
        frame = video.next_frame(viewer, 0, timeout=5)
        self.assertEqual(frame[5], "wda_mjpeg")
        video.unsubscribe(viewer)

    def test_private_consumers_never_poll_screenshots(self):
        # The FrameClock's own connection must not queue /screenshot requests
        # in front of the agent's WDA calls when MJPEG is unreachable.
        probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        unused = probe.server_address[1]
        probe.server_close()
        video = WdaVideo(f"http://127.0.0.1:{self.wda.server_address[1]}",
                         mjpeg_url=f"http://127.0.0.1:{unused}", session=lambda: "s",
                         screenshot_fallback=False)
        self.addCleanup(video.close)
        video._screenshot_once = Mock(return_value=True)
        viewer = video.subscribe()
        self.assertIsNone(video.next_frame(viewer, 0, timeout=1))
        video._screenshot_once.assert_not_called()
        video.unsubscribe(viewer)

    def test_a_stalled_stream_reconnects_at_once(self):
        # WDA sends nothing while it serves a long request; the relay used to wait
        # MJPEG_RETRY_SECONDS (5 s) after the read timeout before reconnecting.
        connections = []

        class Stalling(MjpegHandler):
            def do_GET(self):
                connections.append(time.monotonic())
                if len(connections) > 1:
                    return super().do_GET()
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=BoundaryString")
                self.end_headers()
                self.wfile.write(part(JPEG))
                self.wfile.flush()
                time.sleep(2)

        mjpeg = serve(Stalling)
        self.addCleanup(mjpeg.shutdown)
        from unittest.mock import patch
        with patch("mobile_agent.wda_video.MJPEG_READ_SECONDS", .3):
            video = self.video(mjpeg.server_address[1])
            viewer = video.subscribe()
            first = video.next_frame(viewer, 0, timeout=5)
            started = time.monotonic()
            later = video.next_frame(viewer, first[0], timeout=5)
        self.assertIsNotNone(later)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(connections), 2)
        video.unsubscribe(viewer)

    def test_the_relay_stops_when_nobody_watches(self):
        mjpeg = serve(MjpegHandler)
        self.addCleanup(mjpeg.shutdown)
        video = self.video(mjpeg.server_address[1])
        activity = []
        video.on_activity = activity.append
        viewer = video.subscribe()
        video.next_frame(viewer, 0, timeout=5)
        video.unsubscribe(viewer)
        deadline = time.monotonic() + 10
        while video.worker is not None and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertIsNone(video.worker)
        self.assertEqual(activity, [True, False])


class EndpointTests(unittest.TestCase):
    def test_stream_endpoint_serves_multipart_jpeg(self):
        from mobile_agent.server import BoundedServer, make_handler
        mjpeg, wda = serve(MjpegHandler), serve(WdaHandler)
        self.addCleanup(mjpeg.shutdown)
        self.addCleanup(wda.shutdown)
        runtime = Mock()
        runtime.config = SimpleNamespace(port=8765)
        runtime.video = WdaVideo(f"http://127.0.0.1:{wda.server_address[1]}",
                                 mjpeg_url=f"http://127.0.0.1:{mjpeg.server_address[1]}")
        self.addCleanup(runtime.video.close)
        server = BoundedServer(("127.0.0.1", 0), make_handler(runtime))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        connection.request("GET", "/api/device/stream", headers={"Host": "127.0.0.1:8765"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("multipart/x-mixed-replace", response.getheader("Content-Type"))
        frames = read_mjpeg_frames(response)
        self.assertTrue(next(frames).startswith(b"\xff\xd8"))
        connection.close()
        # The desktop webview's fetch reader: the same parts, not typed as multipart,
        # because WebKit fails fetch() of multipart/x-mixed-replace outright.
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        connection.request("GET", "/api/device/stream?framing=raw", headers={"Host": "127.0.0.1:8765"})
        response = connection.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "application/octet-stream")
        self.assertTrue(next(read_mjpeg_frames(response)).startswith(b"\xff\xd8"))
        connection.close()


if __name__ == "__main__":
    unittest.main()


class QualityTests(unittest.TestCase):
    def test_presets_and_validation(self):
        from mobile_agent.wda_video import mjpeg_settings
        self.assertEqual(mjpeg_settings(60, "low"), {"mjpegServerFramerate": 60, "mjpegScalingFactor": 35,
                                                     "mjpegServerScreenshotQuality": 40})
        for bad in ((45, "medium"), (30, "ultra"), ("30", "medium")):
            with self.assertRaises(ValueError):
                mjpeg_settings(*bad)

    def test_settings_are_retried_until_they_stick_and_reapplied_on_change(self):
        video = WdaVideo("http://127.0.0.1:1", session=lambda: "S1")
        calls = []

        def request(url, method, path, body, timeout):
            calls.append(body["settings"]["mjpegServerFramerate"])
            if len(calls) == 1:
                raise TimeoutError()
        video._request = request
        self.assertFalse(video._configure())   # WDA busy: not applied
        self.assertTrue(video._configure())    # retried
        self.assertTrue(video._configure())    # already applied: no request
        self.assertEqual(calls, [30, 30])
        video.set_quality(60, "high")
        self.assertTrue(video._configure())
        self.assertEqual(calls[-1], 60)
        self.assertEqual(video.status()["targetFps"], 60)


class VideoSettingsRouteTests(unittest.TestCase):
    def test_quality_is_saved_and_applied(self):
        import os
        import tempfile
        from unittest.mock import patch
        from mobile_agent.server import Runtime
        runtime = object.__new__(Runtime)
        env_file = os.path.join(tempfile.mkdtemp(), "agent.env")
        runtime.config, runtime.setup = SimpleNamespace(env_file=env_file), None
        runtime.video = WdaVideo("http://127.0.0.1:1")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOBSTER_VIDEO_FPS", None)
            os.environ.pop("MOBSTER_VIDEO_DETAIL", None)
            self.assertEqual(runtime.settings()["videoFps"], 30)
            state = runtime.update_settings({"videoFps": 60, "videoDetail": "low"})
            self.assertEqual((state["videoFps"], state["videoDetail"]), (60, "low"))
            self.assertEqual(runtime.video.settings["mjpegScalingFactor"], 35)
            with open(env_file) as stream:
                self.assertIn("MOBSTER_VIDEO_FPS=60", stream.read())
            with self.assertRaises(ValueError):
                runtime.update_settings({"videoFps": 45})
