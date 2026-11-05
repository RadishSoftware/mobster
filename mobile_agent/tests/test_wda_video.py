import base64
import http.client
import io
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mobile_agent.tests.fakes import Clock
from mobile_agent.wda.video import IDLE_GRACE_SECONDS, MAX_VIEWERS, WdaVideo, read_mjpeg_frames

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


class TransportTests(unittest.TestCase):
    def test_api_requests_keep_the_canonical_bounded_transport(self):
        for base in ("http://wda.example/proxy/", "https://wda.example/proxy/", "https://wda.example:8443/proxy/"):
            with self.subTest(base=base), patch("mobile_agent.transport.HTTP") as transport:
                transport.return_value.request.return_value = {"value": True}
                self.assertEqual(WdaVideo._request(base, "GET", "/status", timeout=120), {"value": True})
                transport.assert_called_once_with(base)
                transport.return_value.request.assert_called_once_with("GET", "/status", None, timeout=60.0)
                transport.return_value.close.assert_called_once()

    def test_stream_requests_use_https_without_replacing_certificate_validation(self):
        video = WdaVideo("https://wda.example", mjpeg_url="https://video.example/stream")
        self.addCleanup(video.close)
        with patch("mobile_agent.wda.video.http.client.HTTPConnection") as plain, \
                patch("mobile_agent.wda.video.http.client.HTTPSConnection") as secure, \
                patch("mobile_agent.wda.video.read_mjpeg_frames", return_value=iter([JPEG])):
            secure.return_value.getresponse.return_value.status = 200
            video._stream_mjpeg()
        plain.assert_not_called()
        secure.assert_called_once_with("video.example", None, timeout=5.0)
        secure.return_value.request.assert_called_once_with("GET", "/stream")
        secure.return_value.close.assert_called_once()
        self.assertEqual(video.latest()[2], JPEG)


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

    def test_private_consumers_never_poll_screenshots(self):
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
        with patch("mobile_agent.wda.video.MJPEG_READ_SECONDS", .3):
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
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        connection.request("GET", "/api/device/stream?framing=raw", headers={"Host": "127.0.0.1:8765"})
        response = connection.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "application/octet-stream")
        self.assertTrue(next(read_mjpeg_frames(response)).startswith(b"\xff\xd8"))
        connection.close()


class Synthetic(WdaVideo):
    def __init__(self, screen, fps=50):
        super().__init__("http://127.0.0.1:9", mjpeg_url=None)
        self.screen, self.rate, self.published = screen, fps, 0

    def _configure(self):
        return True

    def _run(self):
        try:
            while self._should_run():
                self._publish("image/jpeg", self.screen(), "wda_mjpeg")
                self.published += 1
                time.sleep(1 / self.rate)
        finally:
            with self.condition:
                self.worker = None


class VirtualVideo(WdaVideo):
    def __init__(self, screen, fps=50):
        self.now, self.screen, self.rate, self.published = 0.0, screen, fps, 0
        super().__init__("http://127.0.0.1:9", mjpeg_url=None, clock=lambda: self.now)

    def _start_worker(self):
        pass

    def advance(self, seconds):
        target = self.now + seconds
        while (self.published + 1) / self.rate <= target + 1e-9:
            self.now = (self.published + 1) / self.rate
            self._publish("image/jpeg", self.screen(), "wda_mjpeg")
            self.published += 1
        self.now = target

    def next_frame(self, viewer, after, timeout=5.0):
        if self.closed or viewer not in self.viewers:
            return None
        if self.frame is None or self.frame[0] <= after:
            self.advance(max(0, (self.published + 1) / self.rate - self.now))
        return super().next_frame(viewer, after, timeout=0)

    def _sleep(self, viewer, seconds):
        self.advance(seconds)
        return self.closed or viewer not in self.viewers


class PeekTests(unittest.TestCase):
    def test_a_peek_starts_a_stopped_relay_and_leaves_it_to_stop(self):
        video = Synthetic(lambda: b"\xff\xd8frame")
        self.addCleanup(video.close)
        self.assertIsNone(video.latest())
        frame = video.peek(wait=3)
        self.assertIsNotNone(frame)
        self.assertEqual(frame[2], b"\xff\xd8frame")
        self.assertEqual(video.status()["viewers"], 0)
        deadline = time.monotonic() + IDLE_GRACE_SECONDS + 3
        while video.worker is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIsNone(video.worker)

    def test_a_peek_at_a_full_relay_returns_the_newest_frame(self):
        video = Synthetic(lambda: b"\xff\xd8frame")
        self.addCleanup(video.close)
        viewers = [video.subscribe() for _ in range(MAX_VIEWERS)]
        self.assertIsNotNone(video.next_frame(viewers[0], 0, timeout=3))
        self.assertIsNotNone(video.peek(wait=0.5))
        for viewer in viewers:
            video.unsubscribe(viewer)


class LifecycleTests(unittest.TestCase):
    def test_a_viewer_arriving_during_idle_shutdown_gets_a_new_worker(self):
        video = WdaVideo("http://127.0.0.1:9", mjpeg_url="http://127.0.0.1:10")
        self.addCleanup(video.close)
        stream_done, stopping, finish_stop = (threading.Event() for _ in range(3))
        self.addCleanup(stream_done.set)
        self.addCleanup(finish_stop.set)
        should_run = video._should_run

        def check():
            running = should_run()
            if not running and not video.closed and not stopping.is_set():
                stopping.set()
                finish_stop.wait(5)
            return running

        def stream():
            video._publish("image/jpeg", JPEG, "wda_mjpeg")
            video._streamed = 1
            stream_done.wait(5)
            stream_done.clear()

        video._should_run, video._stream_mjpeg = check, stream
        with patch("mobile_agent.wda.video.IDLE_GRACE_SECONDS", 0):
            first = video.subscribe()
            frame = video.next_frame(first, 0, timeout=2)
            self.assertIsNotNone(frame)
            video.unsubscribe(first)
            stream_done.set()
            self.assertTrue(stopping.wait(2))
            second = video.subscribe()
            finish_stop.set()
            self.assertIsNotNone(video.next_frame(second, frame[0], timeout=2))
            video.close()
            stream_done.set()


def collect(video, viewer, rate, seconds):
    sent, stop = [], video.clock() + seconds
    frames = video.paced_frames(viewer, rate)
    for frame in frames:
        sent.append((video.clock(), frame))
        if video.clock() >= stop:
            break
    return sent


class PacingTests(unittest.TestCase):
    def video(self, screen, fps=50):
        video = VirtualVideo(screen, fps)
        self.addCleanup(video.close)
        return video

    def test_a_still_screen_is_sent_once_a_second_not_fifty_times(self):
        video = self.video(lambda: JPEG)
        viewer = video.subscribe()
        sent = collect(video, viewer, lambda: None, 2.5)
        video.unsubscribe(viewer)
        self.assertIn(len(sent), (3, 4))
        self.assertGreater(video.published, 60)
        gaps = [b[0] - a[0] for a, b in zip(sent, sent[1:])]
        self.assertTrue(all(.9 <= gap <= 1.3 for gap in gaps), gaps)

    def test_a_changing_screen_is_sent_at_the_viewers_rate_and_every_frame_without_one(self):
        count = iter(range(10**6))
        video = self.video(lambda: JPEG + bytes(str(next(count)), "ascii"))
        viewer = video.subscribe()
        sent = collect(video, viewer, lambda: 5, 2.0)
        self.assertLessEqual(len(sent), 12)
        self.assertGreaterEqual(len(sent), 8)
        self.assertGreaterEqual(sent[-1][1][0] - sent[0][1][0], 60)
        sent = collect(video, viewer, lambda: None, 1.0)
        self.assertGreaterEqual(len(sent), 35)
        video.unsubscribe(viewer)

    def test_the_rate_is_read_for_every_frame(self):
        count = iter(range(10**6))
        video = self.video(lambda: JPEG + bytes(str(next(count)), "ascii"))
        viewer = video.subscribe()
        started = video.clock()
        busy = lambda: None if video.clock() - started > 1 else 5  # noqa: E731 -- a task starts after 1 s
        sent = collect(video, viewer, busy, 2.0)
        video.unsubscribe(viewer)
        before = sum(1 for at, _ in sent if at - started <= 1)
        after = sum(1 for at, _ in sent if at - started > 1.1)
        self.assertLessEqual(before, 7)
        self.assertGreaterEqual(after, 30)

    def test_the_frame_clock_still_reads_every_frame_of_a_still_screen(self):
        video = self.video(lambda: JPEG)
        paced, clock = video.subscribe(), video.subscribe()
        sequence, seen = 0, 0
        for _ in range(75):
            video.advance(1 / 50)
            frame = video.next_frame(clock, sequence, timeout=0)
            self.assertIsNotNone(frame)
            self.assertEqual(frame[0], sequence + 1)
            sequence, seen = frame[0], seen + 1
        self.assertEqual(seen, 75)
        self.assertLessEqual(len(collect(video, paced, lambda: 5, 1.5)), 3)
        video.unsubscribe(paced)
        video.unsubscribe(clock)

    def test_a_paced_viewer_ends_when_it_leaves_or_the_relay_closes(self):
        video = self.video(lambda: JPEG)
        viewer = video.subscribe()
        frames = video.paced_frames(viewer, lambda: 2)
        self.assertIsNotNone(next(frames))
        video.unsubscribe(viewer)
        self.assertEqual(list(frames), [])
        viewer = video.subscribe()
        frames = video.paced_frames(viewer, lambda: None)
        next(frames)
        video.close()
        self.assertEqual(list(frames), [])


class QualityTests(unittest.TestCase):
    def test_idle_video_keeps_new_quality_for_the_next_subscription(self):
        video = WdaVideo("http://127.0.0.1:8100")
        video.worker = Mock(is_alive=lambda: False)
        with patch("mobile_agent.wda.video.threading.Thread") as thread:
            video.set_quality(15, "low")
            thread.assert_not_called()
        self.assertEqual(video.settings["mjpegServerFramerate"], 15)

    def test_quality_changes_during_a_request_apply_the_latest_on_the_same_worker(self):
        video = WdaVideo("http://127.0.0.1:8100", session=lambda: "S1")
        video.viewers.add(object())
        video.worker = Mock(is_alive=lambda: True)
        calls = []

        def request(url, method, path, body, timeout):
            calls.append(body["settings"]["mjpegServerFramerate"])
            if len(calls) == 1:
                video.set_quality(15, "low")
                video.set_quality(60, "high")

        def frames(response, **_):
            yield JPEG
            video.close()

        video._request = request
        with patch("mobile_agent.wda.video.threading.Thread") as thread, \
                patch("mobile_agent.wda.video.http.client.HTTPConnection") as connection, \
                patch("mobile_agent.wda.video.read_mjpeg_frames", frames):
            connection.return_value.getresponse.return_value.status = 200
            video._run()
            thread.assert_not_called()
        self.assertEqual(calls, [30, 60])
        self.assertFalse(video.quality_changed.is_set())

    def test_presets_and_validation(self):
        from mobile_agent.wda.video import mjpeg_settings
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
        self.assertFalse(video._configure())
        self.assertTrue(video._configure())
        self.assertTrue(video._configure())
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


def feed(governor, clock, seconds, body, interval):
    steps, end = [], clock.now + seconds
    while clock.now < end:
        clock.now += interval
        step = governor.frame(body, interval)
        if step:
            steps.append((round(clock.now, 2), step))
    return steps


class LinkTests(unittest.TestCase):
    def governor(self, levels=6):
        from mobile_agent.wda.video import LinkGovernor
        clock = Clock(0.0)
        governor = LinkGovernor(clock)
        governor.reset(levels)
        return governor, clock

    def test_each_step_is_capped_at_the_users_choice(self):
        from mobile_agent.wda.video import link_ladder, mjpeg_settings
        self.assertEqual(link_ladder(mjpeg_settings(30, "medium")),
                         [(30, 50, 50), (20, 45, 45), (15, 40, 40), (10, 35, 40), (6, 30, 35), (3, 25, 30)])
        self.assertEqual(link_ladder(mjpeg_settings(15, "low")), [(15, 35, 40), (10, 35, 40), (6, 30, 35), (3, 25, 30)])
        self.assertEqual(link_ladder(mjpeg_settings(60, "high"))[:2], [(60, 75, 65), (30, 50, 50)])

    def test_a_saturated_link_steps_down_once_a_second_to_the_bottom(self):
        governor, clock = self.governor(levels=4)
        steps = feed(governor, clock, 10, body=.1, interval=.1)
        self.assertEqual([step for _, step in steps], ["down"] * 3)
        self.assertLessEqual(steps[0][0], 1.1)
        self.assertEqual(governor.level, 3)

    def test_room_on_the_link_steps_up_after_a_wait_and_a_step_that_fails_doubles_it(self):
        from mobile_agent.wda.video import LINK_RECOVER_SECONDS
        governor, clock = self.governor()
        feed(governor, clock, 2.2, body=.1, interval=.1)
        self.assertEqual(governor.level, 2)
        steps = feed(governor, clock, LINK_RECOVER_SECONDS + 2, body=.01, interval=.1)
        self.assertEqual([step for _, step in steps], ["up"])
        self.assertEqual(governor.level, 1)
        self.assertEqual([step for _, step in feed(governor, clock, 2.3, body=.1, interval=.1)][:1], ["down"])
        self.assertEqual(governor.recover, LINK_RECOVER_SECONDS * 2)
        self.assertEqual(feed(governor, clock, LINK_RECOVER_SECONDS + 2, body=.01, interval=.1), [])
        self.assertEqual([step
                          for _, step in feed(governor, clock, LINK_RECOVER_SECONDS, body=.01, interval=.1)], ["up"])

    def test_a_middling_load_holds_the_level(self):
        governor, clock = self.governor()
        feed(governor, clock, 1.1, body=.1, interval=.1)
        self.assertEqual(feed(governor, clock, 60, body=.06, interval=.1), [])
        self.assertEqual(governor.level, 1)

    def test_a_new_choice_starts_at_its_top(self):
        video = WdaVideo("http://127.0.0.1:1", session=lambda: "S1")
        video.link.level = 2
        self.assertEqual(video.reduced_fps(), 15)
        self.assertEqual(video.applied_settings()["mjpegScalingFactor"], 40)
        video.set_quality(60, "high")
        self.assertIsNone(video.reduced_fps())
        self.assertEqual(video.applied_settings()["mjpegServerFramerate"], 60)


class SlowLinkRelayTests(unittest.TestCase):
    def test_a_step_down_applies_the_lower_level_reconnects_and_marks_each_part(self):
        from mobile_agent.server import BoundedServer, make_handler
        posted, connections = [], []

        class Settings(WdaHandler):
            def do_POST(self):
                posted.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))["settings"])
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        class Counting(MjpegHandler):
            def do_GET(self):
                connections.append(time.monotonic())
                super().do_GET()

        wda, mjpeg = serve(Settings), serve(Counting)
        self.addCleanup(wda.shutdown)
        self.addCleanup(mjpeg.shutdown)
        video = WdaVideo(f"http://127.0.0.1:{wda.server_address[1]}",
                         mjpeg_url=f"http://127.0.0.1:{mjpeg.server_address[1]}", session=lambda: "s")
        self.addCleanup(video.close)
        frames = iter(range(10**6))
        original = video.link.frame

        def frame(body, interval):
            original(body, interval)
            if next(frames) == 4:
                video.link.level += 1
                return "down"
            return None
        video.link.frame = frame
        runtime = Mock()
        runtime.config = SimpleNamespace(port=8765)
        runtime.video = video
        runtime.stream_fps = Mock(return_value=None)
        server = BoundedServer(("127.0.0.1", 0), make_handler(runtime))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/device/stream?framing=raw", headers={"Host": "127.0.0.1:8765"})
        response = connection.getresponse()
        deadline, marked = time.monotonic() + 8, False
        while time.monotonic() < deadline and not marked:
            line = response.readline()
            marked = line.lower().startswith(b"x-mobster-reduced: 20")
        self.assertTrue(marked, "a part says the stream is at 20 frames a second")
        while time.monotonic() < deadline and len(connections) < 2:
            time.sleep(.05)
        self.assertEqual(len(connections), 2, "the relay reconnected, dropping what queued on the phone")
        self.assertEqual(posted[0]["mjpegServerFramerate"], 30)
        self.assertEqual((posted[-1]["mjpegServerFramerate"], posted[-1]["mjpegScalingFactor"],
                          posted[-1]["mjpegServerScreenshotQuality"]), (20, 45, 45))
        self.assertEqual(video.status()["reducedFps"], 20)

    def test_at_the_users_choice_no_part_is_marked(self):
        from mobile_agent.server import BoundedServer, make_handler
        video = Synthetic(lambda: JPEG)
        self.addCleanup(video.close)
        runtime = Mock()
        runtime.config = SimpleNamespace(port=8765)
        runtime.video = video
        runtime.stream_fps = Mock(return_value=None)
        server = BoundedServer(("127.0.0.1", 0), make_handler(runtime))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/device/stream?framing=raw", headers={"Host": "127.0.0.1:8765"})
        response = connection.getresponse()
        head = b"".join(response.readline() for _ in range(4))
        self.assertIn(b"Content-Length", head)
        self.assertNotIn(b"X-Mobster-Reduced", head)
        self.assertIsNone(video.status()["reducedFps"])


if __name__ == "__main__":
    unittest.main()
