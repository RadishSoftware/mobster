"""Save a task as a captioned GIF (run_gif.py), its API route and `mobster export`. Offline: synthetic screens, Pillow's
own font (no system font is assumed), temporary folders and journals, and no ffmpeg unless it is installed."""

import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from mobile_agent import run_gif
from mobile_agent.api_errors import APIError

FONTS = run_gif.Fonts("")   # Pillow's own font


def screen_jpeg(shade, size=(222, 480), text_box=None):
    """A synthetic phone screen: a flat colour, a dark "status bar", and optionally a black-on-white text box."""
    image = Image.new("RGB", size, (shade, 120, 255 - shade))
    image.paste((20, 20, 20), (0, 0, size[0], 24))
    if text_box is not None:
        x0, y0, x1, y1 = text_box
        image.paste((255, 255, 255), (x0, y0, x1, y1))
        for x in range(x0 + 4, x1 - 4, 6):  # stripes standing in for glyphs
            image.paste((0, 0, 0), (x, y0 + 4, x + 3, y1 - 4))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    return out.getvalue()


def make_run(steps=3, *, approval_after=None, code_after=None, frames=True, status="completed", extra=()):
    events, shots = [{"event": "run_started", "mode": "live"}], {}
    for n in range(1, steps + 1):
        frame_id = f"f{n:010x}"
        if frames:
            shots[frame_id] = screen_jpeg(40 + (n * 37) % 180)
        events.append({"event": "step", "n": n, "step": n - 1, "text": f"Tapped item {n}",
                       **({"frameId": frame_id} if frames else {})})
        if approval_after == n:
            events += [{"event": "approval_requested", "approval_id": "a1", "operation": "TAP", "label": "Send",
                        "kind": "commit", "title": "Send this message to Alex Rivera?", "act": "send_message"},
                       {"event": "approval_resolved", "approval_id": "a1", "decision": "approved"}]
        if code_after == n:
            events += [{"event": "skill_started", "op": "USE_CODE"},
                       {"event": "skill_finished", "op": "USE_CODE", "ok": True}]
    events += list(extra)
    run = {"id": "0123456789ab", "goal": "Text Alex that I'm running late", "status": status,
           "createdAt": 1_000_000, "finishedAt": 1_038_000, "events": events,
           "summary": {"answer": "Sent “Running late” to Alex Rivera."}}
    return run, shots


class Recorder:
    def __init__(self, shots):
        self.shots, self.asked = shots, []

    def __call__(self, frame_id):
        self.asked.append(frame_id)
        return self.shots.get(frame_id)


def gif_frames(data):
    image = Image.open(io.BytesIO(data))
    durations = []
    for index in range(image.n_frames):
        image.seek(index)
        durations.append(image.info.get("duration"))
    return image.size, image.n_frames, durations


class RenderTests(unittest.TestCase):
    def test_three_steps_make_a_clip_with_the_task_the_steps_and_the_result(self):
        run, shots = make_run(3)
        clip = run_gif.build_clip(run, Recorder(shots), fonts=FONTS)
        self.assertEqual(clip.captions, ["Text Alex that I'm running late", "Tapped item 1", "Tapped item 2",
                                         "Tapped item 3", "Sent “Running late” to Alex Rivera."])
        self.assertEqual([beat.kind for beat in clip.beats], ["title", "step", "step", "step", "end"])
        self.assertEqual([(beat.n, beat.total) for beat in clip.beats[1:4]], [(1, 3), (2, 3), (3, 3)])
        self.assertEqual(clip.beats[-1].note, "Done in 38 s · 3 steps")
        size, count, durations = gif_frames(clip.gif)
        self.assertEqual(size, (624, 780))
        self.assertEqual(count, clip.frames)
        self.assertEqual(durations, clip.durations)
        self.assertLess(len(clip.gif), run_gif.GIF_MAX_BYTES)
        # The title types out, every step holds 1.2 to 2.5 s, cross-fades are short, the result holds 1.5 s more.
        held = [ms for ms in durations if ms >= 1000]
        self.assertEqual(len(held), 5)
        self.assertEqual(held[0], run_gif.TITLE_HOLD_MS)
        for ms in held[1:4]:
            self.assertTrue(run_gif.STEP_MIN_MS <= ms <= run_gif.STEP_MAX_MS, ms)
        self.assertEqual(held[-1], run_gif.step_ms("Sent “Running late” to Alex Rivera.") + run_gif.END_HOLD_MS)
        self.assertEqual(durations.count(run_gif.FADE_FRAME_MS), run_gif.FADE_STEPS * 4)
        image = Image.open(io.BytesIO(clip.gif))
        self.assertEqual(image.info.get("loop"), 0)

    def test_pacing_follows_the_caption_length(self):
        self.assertEqual(run_gif.step_ms("Tapped"), run_gif.STEP_MIN_MS)
        self.assertEqual(run_gif.step_ms("x" * 200), run_gif.STEP_MAX_MS)
        self.assertLess(run_gif.step_ms("Opened Messages"),
                        run_gif.step_ms("Typed “Running 15 minutes late” in iMessage"))

    def test_without_cross_fades_each_beat_is_one_frame(self):
        run, shots = make_run(3)
        clip = run_gif.build_clip(run, Recorder(shots), fonts=FONTS, crossfade=False)
        self.assertNotIn(run_gif.FADE_FRAME_MS, clip.durations)
        self.assertFalse(clip.crossfade)

    def test_the_night_ground(self):
        run, shots = make_run(1)
        clip = run_gif.build_clip(run, Recorder(shots), fonts=FONTS, theme="night")
        first = Image.open(io.BytesIO(clip.gif)).convert("RGB")
        self.assertEqual(first.getpixel((4, 4)), (0x11, 0x10, 0x14))

    def test_an_approval_shows_the_card_it_asked_with(self):
        run, shots = make_run(3, approval_after=2)
        clip = run_gif.build_clip(run, Recorder(shots), fonts=FONTS)
        ask = next(beat for beat in clip.beats if beat.kind == "ask")
        self.assertEqual(ask.caption, "Asked before sending")
        self.assertEqual(ask.card["title"], "Send this message to Alex Rivera?")
        self.assertEqual((ask.card["yes"], ask.card["no"], ask.card["answer"]),
                         ("Send", "Don’t send", "You approved “Send”"))
        self.assertEqual(ask.frame_id, "f0000000002")  # the screen it asked over: the step before
        self.assertEqual([beat.kind for beat in clip.beats], ["title", "step", "step", "ask", "step", "end"])
        self.assertIn(run_gif.ASK_MS, clip.durations)
        self.assertIn(run_gif.ANSWERED_MS, clip.durations)

    def test_a_declined_approval_says_so(self):
        run, shots = make_run(2, approval_after=1, status="approval_denied")
        next(event for event in run["events"] if event["event"] == "approval_resolved")["decision"] = "denied"
        beats = run_gif.plan_beats(run)
        ask = next(beat for beat in beats if beat.kind == "ask")
        self.assertEqual(ask.card["answer"], "You chose “Don’t send”")
        self.assertEqual(beats[-1].caption, "You declined")  # no answer printed for a run that didn't finish

    def test_screens_stop_at_a_sign_in_code(self):
        run, shots = make_run(5, code_after=2)
        recorder = Recorder(shots)
        clip = run_gif.build_clip(run, recorder, fonts=FONTS)
        self.assertEqual([beat.kind for beat in clip.beats], ["title", "step", "step", "secret", "end"])
        self.assertEqual(clip.beats[3].caption, run_gif.SECRET_CAPTION)
        self.assertIsNone(clip.beats[-1].frame_id)
        later = {"f0000000003", "f0000000004", "f0000000005"}
        self.assertFalse(later & set(recorder.asked), recorder.asked)
        # And no frame of the GIF shows step 3's screen: its colour is nowhere in the clip.
        step3 = Image.open(io.BytesIO(shots["f0000000003"])).convert("RGB").getpixel((100, 300))
        gif = Image.open(io.BytesIO(clip.gif))
        for index in range(gif.n_frames):
            gif.seek(index)
            colors = {color for _, color in gif.convert("RGB").getcolors(1 << 20)}
            self.assertFalse(any(sum(abs(a - b) for a, b in zip(color, step3)) < 6 for color in colors), index)

    def test_no_screens_is_an_error(self):
        run, shots = make_run(3, frames=False)
        with self.assertRaisesRegex(ValueError, "no screens"):
            run_gif.build_clip(run, Recorder(shots), fonts=FONTS)
        run, shots = make_run(2)
        with self.assertRaisesRegex(ValueError, "no screens"):
            run_gif.build_clip(run, lambda frame_id: None, fonts=FONTS)

    def test_a_step_without_its_screen_keeps_the_one_before(self):
        run, shots = make_run(3)
        del run["events"][2]["frameId"]
        beats = run_gif.plan_beats(run)
        self.assertEqual([beat.frame_id for beat in beats if beat.kind == "step"],
                         ["f0000000001", "f0000000001", "f0000000003"])

    def test_a_long_task_keeps_its_first_last_and_asked_steps_and_fits(self):
        run, shots = make_run(80, approval_after=40)
        beats = run_gif.plan_beats(run)
        steps = [beat for beat in beats if beat.kind == "step"]
        self.assertEqual(len(steps), run_gif.MAX_STEPS)
        self.assertEqual((steps[0].caption, steps[-1].caption), ("Tapped item 1", "Tapped item 80"))
        index = next(i for i, beat in enumerate(beats) if beat.kind == "ask")
        self.assertEqual((beats[index - 1].caption, beats[index + 1].caption), ("Tapped item 40", "Tapped item 41"))
        self.assertEqual(steps[-1].total, run_gif.MAX_STEPS)
        clip = run_gif.build_clip(run, Recorder(shots), fonts=FONTS, crossfade=False)
        self.assertLess(len(clip.gif), run_gif.GIF_MAX_BYTES)

    def test_the_size_cap_trades_colours_and_fades_then_refuses(self):
        run, shots = make_run(2)
        full = run_gif.build_clip(run, Recorder(shots), fonts=FONTS)
        smaller = run_gif.build_clip(run, Recorder(shots), fonts=FONTS, max_bytes=len(full.gif) - 1)
        self.assertLess(len(smaller.gif), len(full.gif))
        self.assertTrue(smaller.colors < 256 or not smaller.crossfade)
        with self.assertRaisesRegex(ValueError, "too long for a GIF"):
            run_gif.build_clip(run, Recorder(shots), fonts=FONTS, max_bytes=10_000)

    def test_captions_wrap_to_two_balanced_lines(self):
        font = FONTS.get(27, 590)
        lines = run_gif._wrap("Sent “Running 15 minutes late, sorry!” to Alex Rivera.", font, 548, 2)
        self.assertEqual(len(lines), 2)
        self.assertLess(abs(font.getlength(lines[0]) - font.getlength(lines[1])), 200)
        cut = run_gif._wrap("word " * 80, font, 548, 2)
        self.assertEqual(len(cut), 2)
        self.assertTrue(cut[-1].endswith("…"))

    def test_the_file_name_comes_from_the_task(self):
        self.assertEqual(run_gif.file_stem("Text Alex that I'm running late"),
                         "Mobster – Text Alex that I'm running late")
        self.assertEqual(run_gif.file_stem("../../etc/passwd: now?"), "Mobster – etc passwd now")
        self.assertEqual(run_gif.file_stem("   "), "Mobster task")
        self.assertLessEqual(len(run_gif.file_stem("x" * 300)), len("Mobster – ") + 60)


class RedactTests(unittest.TestCase):
    def test_blur_hides_what_is_inside_a_frame_and_keeps_the_rest(self):
        data = screen_jpeg(100, text_box=(20, 200, 200, 230))
        image = Image.open(io.BytesIO(data)).convert("RGB")
        blurred = run_gif.blur_regions(image, [[20 / 222, 200 / 480, 180 / 222, 30 / 480]])
        inside = [blurred.getpixel((x, 215)) for x in range(30, 190)]
        before = [image.getpixel((x, 215)) for x in range(30, 190)]
        spread = lambda pixels: max(sum(p) for p in pixels) - min(sum(p) for p in pixels)  # noqa: E731
        self.assertGreater(spread(before), 400)       # black glyph stripes on white
        self.assertLess(spread(inside), spread(before) / 3)
        self.assertEqual(blurred.getpixel((100, 400)), image.getpixel((100, 400)))  # outside: untouched

    def test_a_screen_with_no_known_frames_is_blurred_all_over_but_its_status_bar(self):
        image = Image.open(io.BytesIO(screen_jpeg(100, text_box=(20, 200, 200, 230)))).convert("RGB")
        blurred = run_gif.blur_regions(image, None)
        self.assertEqual(blurred.getpixel((100, 10)), image.getpixel((100, 10)))
        self.assertNotEqual([blurred.getpixel((x, 215)) for x in range(30, 60)],
                            [image.getpixel((x, 215)) for x in range(30, 60)])

    def test_private_frames_are_fields_and_message_text(self):
        elements = [
            {"label": "Messages", "role": "NavigationBar", "rect": [0, .05, 1, .08]},
            {"label": "iMessage", "role": "TextField", "rect": [.1, .9, .7, .05], "editable": True, "value": ""},
            {"label": "Ok", "role": "StaticText", "rect": [.6, .3, .2, .04]},
            {"label": "Send", "role": "Button", "rect": [.85, .9, .1, .05]},
        ]
        self.assertEqual(run_gif.private_rects(elements, "com.apple.MobileSMS"),
                         [[.1, .9, .7, .05], [.6, .3, .2, .04]])
        # Outside a messaging app only text long enough to be a body counts.
        self.assertEqual(run_gif.private_rects(elements, "com.apple.Preferences"), [[.1, .9, .7, .05]])
        body = {"label": "Dinner Friday at Luca, 214 Pine St", "role": "StaticText", "rect": [0, .2, 1, .05]}
        self.assertEqual(run_gif.private_rects([body], "com.apple.Preferences"), [[0, .2, 1, .05]])
        self.assertEqual(run_gif.private_rects([{"role": "Other", "label": "x" * 40, "rect": [0, 0, 1, 1]}]), [])
        self.assertEqual(len(run_gif.private_rects([body] * 100)), run_gif.MAX_RECTS)

    def test_redact_hides_quotes_answers_and_the_approvals_wording(self):
        run, shots = make_run(2, approval_after=1)
        run["events"][1]["text"] = "Typed “Running late” in iMessage"
        run["events"][1]["redact"] = [[.1, .9, .7, .05]]
        beats = run_gif.plan_beats(run, redact=True)
        self.assertEqual(beats[1].caption, "Typed “•••” in iMessage")
        self.assertEqual(beats[1].rects, [[.1, .9, .7, .05]])
        ask = next(beat for beat in beats if beat.kind == "ask")
        self.assertEqual(ask.card["title"], "Send this message?")
        self.assertEqual(beats[-1].caption, "Done on your iPhone.")
        self.assertEqual(run_gif._mask_quotes("Read Address: 214 Pine St"), "Read Address: •••")
        self.assertEqual(run_gif._mask_quotes("Tapped Sam Lee, (415) 555-0134, mobile"), "Tapped Sam Lee, •••, mobile")
        self.assertEqual(run_gif._mask_quotes("Tapped sam.lee@example.com"), "Tapped •••")
        self.assertEqual(run_gif._mask_quotes("Opened Settings"), "Opened Settings")
        self.assertEqual(run_gif._mask_quotes("Scrolled down 2 times"), "Scrolled down 2 times")

    def test_fast_steps_take_their_frames_from_the_observations_either_side(self):
        field = {"label": "Search", "role": "SearchField", "rect": [.1, .1, .8, .05], "editable": True}
        bubble = {"label": "See you at 7", "role": "StaticText", "rect": [.5, .4, .4, .05]}
        run, shots = make_run(1, extra=())
        run["events"].insert(1, {"event": "observation", "bundle_id": "com.apple.MobileSMS", "elements": [field]})
        run["events"].append({"event": "observation", "bundle_id": "com.apple.MobileSMS", "elements": [bubble]})
        beats = run_gif.plan_beats(run, redact=True)
        self.assertEqual(beats[1].rects, [[.1, .1, .8, .05], [.5, .4, .4, .05]])
        bare, _ = make_run(1)
        self.assertIsNone(run_gif.plan_beats(bare, redact=True)[1].rects)  # unknown: the whole screen blurs

    def test_a_redacted_clip_renders(self):
        run, shots = make_run(2)
        run["events"][1]["redact"] = [[.1, .4, .8, .1]]
        clip = run_gif.build_clip(run, Recorder(shots), fonts=FONTS, redact=True)
        self.assertNotIn("Running late", " ".join(clip.captions))


class SmartStepsKeepTheirPrivateFrames(unittest.TestCase):
    def test_a_step_with_a_screen_carries_its_fields_frames_and_never_their_text(self):
        from mobile_agent import engines
        from mobile_agent.state import Element, Snapshot
        out = []
        events = engines.SmartEvents(out.append, frame=lambda: b"jpeg")
        screen = Snapshot([Element("e1", "iMessage", "TextField", (.1, .9, .7, .05), True, value="Running late",
                                   actions=("TAP", "TYPE")),
                           Element("e2", "Send", "Button", (.85, .9, .1, .05))], "", 390, 844, "test",
                          bundle_id="com.apple.MobileSMS")
        events.agent = SimpleNamespace(last_screen=screen, _contract=None)
        events._step("Tapped Send", 3)
        self.assertEqual(out[-1]["redact"], [[.1, .9, .7, .05]])
        self.assertNotIn("Running late", json.dumps({k: v for k, v in out[-1].items() if k != "_frame"}))
        events.agent = SimpleNamespace(last_screen=None, _contract=None)
        events._step("Opened Messages", 1)
        self.assertNotIn("redact", out[-1])
        frameless = engines.SmartEvents(out.append, frame=lambda: None)
        frameless.agent = SimpleNamespace(last_screen=screen, _contract=None)
        frameless._step("Tapped Send", 3)
        self.assertNotIn("redact", out[-1])


@unittest.skipUnless(run_gif.ffmpeg_path(), "ffmpeg is not installed")
class Mp4Tests(unittest.TestCase):
    def test_the_mp4_keeps_the_clips_length(self):
        run, shots = make_run(2)
        data = run_gif.build_mp4(run, Recorder(shots), fonts=FONTS, scale=1.0)
        self.assertEqual(data[4:8], b"ftyp")


class SaveTests(unittest.TestCase):
    def test_media_saves_beside_others_and_never_overwrites(self):
        from mobile_agent.server import save_export_bytes
        with tempfile.TemporaryDirectory() as folder:
            first = save_export_bytes("Mobster – Text Alex that I'm running late", "gif", b"GIF89a1", folder)
            second = save_export_bytes("Mobster – Text Alex that I'm running late", "gif", b"GIF89a2", folder)
            self.assertEqual((first["name"], second["name"]), ("Mobster – Text Alex that I'm running late.gif",
                                                             "Mobster – Text Alex that I'm running late (1).gif"))
            self.assertEqual(Path(first["path"]).read_bytes(), b"GIF89a1")
            odd = save_export_bytes("../../x/y", "gif", b"GIF89a", folder)
            self.assertEqual(Path(odd["path"]).parent, Path(folder))
            with self.assertRaises(ValueError):
                save_export_bytes("x", "sh", b"#!", folder)

    def test_a_refused_downloads_folder_is_a_403(self):
        from mobile_agent.server import save_export_bytes
        with mock.patch("pathlib.Path.mkdir", side_effect=PermissionError):
            with self.assertRaises(APIError) as caught:
                save_export_bytes("x", "gif", b"GIF89a", "/nowhere")
        self.assertEqual((caught.exception.status, caught.exception.code), (403, "downloads_denied"))


def fake_runtime(run, shots):
    finished = run["status"] not in ("queued", "running")
    record = SimpleNamespace(id=run["id"], finished_at=1.0 if finished else None, public=lambda: run)
    frames = SimpleNamespace(get=lambda run_id, frame_id: shots.get(frame_id) if run_id == run["id"] else None)
    return SimpleNamespace(lock=threading.Lock(), runs={run["id"]: record}, frames=frames)


class RuntimeSaveTests(unittest.TestCase):
    def save(self, runtime, identifier="0123456789ab", **kwargs):
        from mobile_agent.server import Runtime
        with mock.patch.object(run_gif, "Fonts", lambda: FONTS):
            return Runtime.save_run_gif(runtime, identifier, **kwargs)

    def test_a_finished_task_saves_to_downloads_and_a_second_save_gets_a_new_name(self):
        run, shots = make_run(2)
        runtime = fake_runtime(run, shots)
        with tempfile.TemporaryDirectory() as folder:
            first = self.save(runtime, directory=folder)
            second = self.save(runtime, directory=folder)
            self.assertEqual(first["name"], "Mobster – Text Alex that I'm running late.gif")
            self.assertEqual(second["name"], "Mobster – Text Alex that I'm running late (1).gif")
            self.assertEqual(Path(first["path"]).read_bytes()[:6], b"GIF89a")
            self.assertEqual(first["bytes"], Path(first["path"]).stat().st_size)
            self.assertGreater(first["frames"], 4)
            self.assertIsNone(first["mp4"])

    def test_refusals(self):
        run, shots = make_run(2, status="running")
        with self.assertRaises(APIError) as caught:
            self.save(fake_runtime(run, shots))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "run_active"))
        run, shots = make_run(2, frames=False)
        with self.assertRaises(APIError) as caught:
            self.save(fake_runtime(run, shots))
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "no_frames"))
        with self.assertRaises(APIError) as caught:
            self.save(fake_runtime(run, shots), identifier="ffffffffffff")
        self.assertEqual(caught.exception.status, 404)

    def test_one_render_at_a_time(self):
        from mobile_agent import server
        run, shots = make_run(1)
        self.assertTrue(server.GIF_RENDERS.acquire(blocking=False))
        try:
            with self.assertRaises(APIError) as caught:
                self.save(fake_runtime(run, shots))
        finally:
            server.GIF_RENDERS.release()
        self.assertEqual((caught.exception.status, caught.exception.code), (429, "export_busy"))


class RouteTests(unittest.TestCase):
    def setUp(self):
        from mobile_agent.tests.test_desktop_shell_bugs import start_server
        self.runtime = mock.Mock()
        self.runtime.runs = {"0123456789ab": object()}
        self.runtime.save_run_gif.return_value = {"name": "Mobster – x.gif", "path": "/tmp/Mobster – x.gif",
                                                  "frames": 9, "bytes": 1000, "seconds": 8.0, "mp4": None}
        self.server = start_server(self.runtime)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, path, body):
        from mobile_agent.tests.test_desktop_shell_bugs import call
        response, data = call(self.server, "POST", path, body)
        return response.status, json.loads(data)

    def test_the_route_saves_with_the_options_it_was_given(self):
        status, answer = self.call("/api/runs/0123456789ab/gif", {})
        self.assertEqual((status, answer["name"]), (200, "Mobster – x.gif"))
        self.runtime.save_run_gif.assert_called_with("0123456789ab", "paper", False, False)
        status, _ = self.call("/api/runs/0123456789ab/gif", {"theme": "night", "redact": True, "mp4": True})
        self.assertEqual(status, 200)
        self.runtime.save_run_gif.assert_called_with("0123456789ab", "night", True, True)

    def test_bad_options_unknown_tasks_and_refusals(self):
        for body in ({"theme": "sepia"}, {"redact": "yes"}, {"frames": 3}):
            with self.subTest(body=body):
                self.assertEqual(self.call("/api/runs/0123456789ab/gif", body)[0], 400)
        self.assertEqual(self.call("/api/runs/ffffffffffff/gif", {})[0], 404)
        self.runtime.save_run_gif.side_effect = APIError("This task is still running.", 409, "run_active")
        status, answer = self.call("/api/runs/0123456789ab/gif", {})
        self.assertEqual((status, answer["code"]), (409, "run_active"))


def export_args(**overrides):
    values = {"run_id": "latest", "gif": True, "mp4": False, "redact": False, "theme": "paper", "output": None,
              "json": False}
    values.update(overrides)
    return argparse.Namespace(**values)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        from mobile_agent.journal import Journal
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.folder, ignore_errors=True))
        state = self.folder / "state"
        self.journal_path = state / "mobster.sqlite3"
        journal = Journal(self.journal_path)
        run, shots = make_run(2)
        events = run.pop("events")
        meta = {**{k: v for k, v in run.items()}, "finishedAt": None}
        journal.create(meta)
        for seq, event in enumerate(events):
            journal.append(meta, {**event, "seq": seq, "timestamp": 1})
        final = {**meta, "finishedAt": run["finishedAt"]}
        journal.finish(final, {"event": "run_finished", "status": "completed", "seq": len(events), "timestamp": 2})
        journal.close()
        frames = state / "frames" / run["id"]
        frames.mkdir(parents=True)
        for frame_id, data in shots.items():
            (frames / f"{frame_id}.jpg").write_bytes(data)
        patcher = mock.patch("mobile_agent.run_export.journals", return_value=[self.journal_path])
        patcher.start()
        self.addCleanup(patcher.stop)
        fonts = mock.patch.object(run_gif, "Fonts", lambda: FONTS)
        fonts.start()
        self.addCleanup(fonts.stop)

    def export(self, **overrides):
        from mobile_agent import run_export
        out, err = io.StringIO(), io.StringIO()
        code = run_export.export(export_args(**overrides), out, err)
        return code, out.getvalue(), err.getvalue()

    def test_the_command_parses(self):
        from mobile_agent import __main__ as cli
        from mobile_agent.extensions import Hooks
        with mock.patch.object(cli, "load_extensions", return_value=Hooks()):
            args = cli.build_parser().parse_args(["export", "0123", "--gif", "--mp4", "--redact", "--theme", "night",
                                                  "-o", "~/clip.gif", "--json"])
        self.assertEqual((args.command, args.run_id, args.gif, args.mp4, args.redact, args.theme, args.json),
                         ("export", "0123", True, True, True, "night", True))
        self.assertEqual(args.output, Path("~/clip.gif").expanduser())
        with mock.patch.object(cli, "load_extensions", return_value=Hooks()), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["export", "0123", "--theme", "sepia"])

    def test_latest_saves_a_gif_to_the_folder_and_never_overwrites(self):
        out_folder = self.folder / "out"
        out_folder.mkdir()
        code, out, err = self.export(output=out_folder)
        self.assertEqual(code, 0, err)
        self.assertIn("Saved Mobster – Text Alex that I'm running late.gif", out)
        self.assertIn("--redact", out)
        code, out, err = self.export(run_id="0123", output=out_folder, json=True)
        answer = json.loads(out)
        self.assertEqual((code, answer["id"], answer["gif"]["name"]),
                         (0, "0123456789ab", "Mobster – Text Alex that I'm running late (1).gif"))
        self.assertEqual(Path(answer["gif"]["path"]).read_bytes()[:6], b"GIF89a")

    def test_a_named_file_is_written_there(self):
        target = self.folder / "clips" / "alex.gif"
        code, out, err = self.export(output=target, redact=True)
        self.assertEqual(code, 0, err)
        self.assertTrue(target.is_file())
        self.assertNotIn("--redact", out)

    def test_what_it_refuses(self):
        self.assertEqual(self.export(gif=False)[0], 2)
        code, _, err = self.export(run_id="ffff")
        self.assertEqual(code, 1)
        self.assertIn("No task ffff in your history", err)
        code, _, err = self.export(run_id="../etc")
        self.assertEqual(code, 1)
        self.assertIn("isn’t a task ID", err)
        with mock.patch.object(run_gif, "ffmpeg_path", return_value=None):
            code, _, err = self.export(mp4=True)
        self.assertEqual(code, 1)
        self.assertIn("brew install ffmpeg", err)

    def test_history_says_how_to_save_one(self):
        from mobile_agent.history import print_history
        stream = io.StringIO()
        print_history(path=self.journal_path, stream=stream)
        self.assertIn("Save one as a GIF: mobster export ID --gif", stream.getvalue())


class LiveDemoFramesTests(unittest.TestCase):
    def test_the_scripted_phone_keeps_a_screen_per_step_when_asked(self):
        from mobile_agent.frontier import video_frame
        from mobile_agent.tests import sota_world as world
        quiet = world.ScriptedPhone(world.new_phone_state())
        self.assertIsNone(video_frame(quiet))
        phone = world.ScriptedPhone(world.new_phone_state(), frames=True)
        data = video_frame(phone)
        self.assertEqual(Image.open(io.BytesIO(data)).format, "JPEG")


if __name__ == "__main__":
    unittest.main()
