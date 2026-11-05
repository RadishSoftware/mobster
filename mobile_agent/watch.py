"""Watching what the iPhone plays (WATCH): a video, a Reel, a story, a voice note."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import listen, native_helpers
from .longform.frames import as_bytes

MAX_SECONDS = 120
DEFAULT_SECONDS = 15
# How often the screen is looked at while watching.
SAMPLE_HZ = 4
# The most frames the model is shown (one contact sheet).
MAX_FRAMES = 12
# Mean colour difference (0 to 1) from the last kept frame that makes a new scene, and the least time between two.
SCENE_CHANGE = 0.08
MIN_SCENE_GAP = 0.75
# A frame is kept at least this often while the picture moves at all, so a slow pan is still seen.
MAX_SCENE_GAP = 8.0
# Between two samples: nothing moved.
STILL = 0.012
# A frame this close to the first one, after the picture moved away from it: the video started over.
LOOP_MATCH = 0.035
MIN_LOOP_SECONDS = 3.0
# until_end: the picture still this long, and the sound quiet this long (when heard), ends the watch.
END_STILL_SECONDS = 3.0
END_QUIET_SECONDS = 2.5
# ...but never before this, and when nothing has been heard yet, not before HEAR_GRACE_SECONDS.
MIN_END_SECONDS = 4.0
HEAR_GRACE_SECONDS = 8.0
# How long to wait for the iPhone's audio to start recording before watching without it.
AUDIO_START_TIMEOUT = 8.0
# Opening and closing the phone's capture resets its USB network link, which ends WebDriverAgent's runner; a watch
# that captured brings it back before it returns (driver.after_capture), waiting at most this long.
LINK_SETTLE_SECONDS = 75.0
# The first time, macOS asks the person whether Mobster may capture the iPhone (listen.Session.asking): the watch
# waits this much longer for their answer, then watches without the sound (the prompt stays up for the next WATCH).
PERMISSION_WAIT = 60.0
# On-screen text: lines in the status bar (the top 6%) are left out, and lines shown in this share of the frames
# (with 3 or more) are the app around the media ("Following", a username), said once, not at every scene.
STATUS_BAR = 0.06
THROUGHOUT = 0.6
MIN_CONFIDENCE = 0.4
# Contact sheet: thumbnails this wide, at most this many a row.
SHEET_THUMB = 210
SHEET_COLUMNS = 6

THUMB_SIZE = (16, 32)

ENDED = {"time": "the time asked for ran out", "looped": "the video started over", "still": "the media stopped",
         "stopped": "the task was stopped", "no_frames": "no picture came from the phone"}


class WatchError(RuntimeError):
    """``code``: devices_off or no_frames; the message says what happened and what to do."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def thumbnail(data):
    """A JPEG or PNG as THUMB_SIZE colour bytes (grey misses a cut between scenes of one brightness), or None."""
    from PIL import Image
    try:
        image = Image.open(io.BytesIO(data))
        image.draft("RGB", (THUMB_SIZE[0] * 4, THUMB_SIZE[1] * 4))  # JPEG: decoded at 1/8 scale
        return image.convert("RGB").resize(THUMB_SIZE).tobytes()
    except Exception:
        return None


def difference(a, b):
    """Mean absolute difference of two thumbnails, 0 (the same) to 1."""
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(abs(x - y) for x, y in zip(a, b)) / (255 * len(a))


class PhoneFrames:
    """The phone's newest picture: the capture's while it records (WebDriverAgent is unreliable then), else live."""

    FRESH = 1.0

    def __init__(self, driver, clock=time.monotonic, capture=None):
        self.driver, self.clock, self.capture = driver, clock, capture
        self.last_capture = None
        self.captured = 0
        self.video = getattr(getattr(driver, "frame_clock", None), "video", None)
        self.viewer = None
        if self.video is not None and callable(getattr(self.video, "subscribe", None)):
            try:
                self.viewer = self.video.subscribe()  # keeps the stream running while watching
            except Exception:
                self.viewer = None
        self.last_sequence = None
        self.screenshots = 0

    def next(self):
        """(bytes, new) for the newest picture (``new`` False when the stream has nothing newer), or None."""
        latest_frame = getattr(self.capture, "latest_frame", None)
        if callable(latest_frame):
            frame = latest_frame()
            if frame is not None and self.clock() - frame[2] <= self.FRESH:
                new = frame[0] != self.last_capture
                self.last_capture = frame[0]
                self.captured += 1 if new else 0
                return bytes(frame[1]), new
            if self.captured:
                # The capture's picture stopped: WebDriverAgent is going down with the phone's link, so asking it
                # would only wait out its timeouts. The last picture stands.
                return None
        latest = getattr(self.video, "latest", None)
        if callable(latest):
            try:
                frame = latest()
            except Exception:
                frame = None
            if (isinstance(frame, (tuple, list)) and len(frame) > 4 and isinstance(frame[2], (bytes, bytearray))
                    and isinstance(frame[4], (int, float)) and self.clock() - frame[4] <= self.FRESH):
                new = frame[0] != self.last_sequence
                self.last_sequence = frame[0]
                return bytes(frame[2]), new
        capture = getattr(self.driver, "capture_preview", None)
        if not callable(capture):
            return None
        try:
            data = capture(timeout=3)
        except Exception:
            return None
        data = as_bytes(data)
        if not data:
            return None
        self.screenshots += 1
        return bytes(data), True

    def close(self):
        if self.viewer is not None:
            try:
                self.video.unsubscribe(self.viewer)
            except Exception:
                pass
            self.viewer = None


def pick(frames):
    """At most MAX_FRAMES frames in time order: the first, then those that changed the picture most."""
    if len(frames) <= MAX_FRAMES:
        return list(frames)
    rest = sorted(frames[1:], key=lambda frame: frame["change"], reverse=True)[:MAX_FRAMES - 1]
    return [frames[0]] + sorted(rest, key=lambda frame: frame["t"])


def sample(source, seconds, *, until_end=False, audio=None, cancelled=lambda: False, clock=time.monotonic,
           sleep=time.sleep):
    """Look at the screen SAMPLE_HZ times a second for up to ``seconds``: (frames, ended, samples)."""
    start = clock()
    frames, samples, ended = [], 0, "time"
    first = previous = None
    moved_away, still_since = False, None
    tick = 1.0 / SAMPLE_HZ
    while True:
        now = clock() - start
        if now >= seconds:
            break
        if cancelled():
            ended = "stopped"
            break
        got = source.next()
        if got is not None:
            data, new = got
            # A stream with no newer frame is a picture that didn't change.
            thumb = thumbnail(data) if new or previous is None else previous
            if thumb is not None:
                samples += 1 if new else 0
                if first is None:
                    first = thumb
                    frames.append({"t": round(now, 2), "data": data, "thumb": thumb, "change": 1.0})
                else:
                    away = difference(thumb, first)
                    if until_end and moved_away and away < LOOP_MATCH and now >= MIN_LOOP_SECONDS:
                        ended = "looped"  # the first picture again: what follows was seen already
                        break
                    moved_away = moved_away or away >= SCENE_CHANGE
                    change = difference(thumb, frames[-1]["thumb"])
                    gap = now - frames[-1]["t"]
                    if (change >= SCENE_CHANGE and gap >= MIN_SCENE_GAP) or (gap >= MAX_SCENE_GAP and change >= STILL):
                        frames.append({"t": round(now, 2), "data": data, "thumb": thumb, "change": change})
                    if difference(thumb, previous) < STILL:
                        still_since = now if still_since is None else still_since
                    else:
                        still_since = None
                    if until_end and now >= MIN_END_SECONDS and still_since is not None and \
                            now - still_since >= END_STILL_SECONDS and _quiet(audio, now):
                        ended = "still"
                        break
                previous = thumb
        wait = start + (int(now / tick) + 1) * tick - clock()
        if wait > 0:
            sleep(wait)
    return frames, ended, samples


def _quiet(audio, now):
    """Whether the sound lets an until_end watch end"""
    if audio is None:
        return True
    quiet = audio.quiet_for()
    if quiet is None:
        return now >= HEAR_GRACE_SECONDS
    return quiet >= END_QUIET_SECONDS


def read_lines(frames, *, helper=None, run=subprocess.run):
    """Each frame's lines of text, [[{text, rect, confidence}]], read on this Mac; None when it can't read them."""
    from .attachments.extract import helper_path
    executable = helper or helper_path()
    if executable is None or not frames:
        return None
    work = Path(tempfile.mkdtemp(prefix="mobster-watch-text-"))
    os.chmod(work, 0o700)
    try:
        paths = []
        for index, frame in enumerate(frames):
            path = work / f"{index}.img"
            path.write_bytes(frame["data"])
            paths.append(str(path))
        try:
            completed = run([str(executable), "lines", *paths], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        for line in reversed((completed.stdout or "").strip().splitlines()):
            try:
                answer = json.loads(line)
            except ValueError:
                continue
            images = answer.get("images") if isinstance(answer, dict) and answer.get("ok") else None
            if isinstance(images, list) and len(images) == len(frames):
                return images
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _plain(text):
    return " ".join(str(text or "").split())


def captions(lines_per_frame):
    """(new text per frame, text on screen the whole time)"""
    kept = []
    for rows in lines_per_frame:
        texts = []
        for row in rows if isinstance(rows, list) else ():
            if not isinstance(row, dict):
                continue
            text, rect = _plain(row.get("text")), row.get("rect")
            confidence = row.get("confidence")
            if len(text) < 2 or (isinstance(confidence, (int, float)) and confidence < MIN_CONFIDENCE):
                continue
            if isinstance(rect, list) and len(rect) == 4 and isinstance(rect[1], (int, float)) and \
                    rect[1] + (rect[3] if isinstance(rect[3], (int, float)) else 0) <= STATUS_BAR:
                continue
            if text.casefold() not in (t.casefold() for t in texts):
                texts.append(text)
        kept.append(texts)
    counts = {}
    for texts in kept:
        for text in {t.casefold() for t in texts}:
            counts[text] = counts.get(text, 0) + 1
    always = set()
    if len(kept) >= 3:
        always = {text for text, count in counts.items() if count >= THROUGHOUT * len(kept)}
        # Vision may read "Following For You" as one line in one frame and two in another: parts of the app's own
        # text are the app's too.
        always |= {text for text in counts if any(text in whole for whole in always)}
    throughout = []
    for texts in kept:
        for text in texts:
            if text.casefold() in always and text not in throughout:
                throughout.append(text)
    throughout = [text for text in throughout
                  if not any(text.casefold() in other.casefold() and text != other for other in throughout)]
    new, before = [], set()
    for texts in kept:
        shown = {t.casefold() for t in texts}
        new.append([t for t in texts if t.casefold() not in always and t.casefold() not in before])
        before = shown
    return new, throughout


def clock_text(seconds):
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


def contact_sheet(frames, *, hide_status_bar=False):
    """The frames side by side, each labelled with its time, as one JPEG; None without frames."""
    from PIL import Image, ImageDraw, ImageFont
    images = []
    for frame in frames:
        try:
            image = Image.open(io.BytesIO(frame["data"])).convert("RGB")
        except Exception:
            continue
        height = max(1, round(image.height * SHEET_THUMB / max(1, image.width)))
        image = image.resize((SHEET_THUMB, height))
        if hide_status_bar:  # a capture shows iOS's placeholder status bar (9:41, full battery): never read as real
            ImageDraw.Draw(image).rectangle((0, 0, SHEET_THUMB, round(height * STATUS_BAR)), fill=(0, 0, 0))
        images.append((frame["t"], image))
    if not images:
        return None
    columns = min(SHEET_COLUMNS, len(images))
    rows = -(-len(images) // columns)
    cell = max(image.height for _, image in images)
    gap = 6
    sheet = Image.new("RGB", (columns * SHEET_THUMB + (columns + 1) * gap, rows * cell + (rows + 1) * gap),
                      (17, 17, 17))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=22)
    except TypeError:  # Pillow before 10.1
        font = ImageFont.load_default()
    for index, (t, image) in enumerate(images):
        x = gap + (index % columns) * (SHEET_THUMB + gap)
        y = gap + (index // columns) * (cell + gap)
        sheet.paste(image, (x, y))
        label = clock_text(t)
        box = draw.textbbox((0, 0), label, font=font)
        draw.rectangle((x, y, x + box[2] - box[0] + 14, y + box[3] - box[1] + 12), fill=(0, 0, 0))
        draw.text((x + 7 - box[0], y + 6 - box[1]), label, fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=72)
    return out.getvalue()


ORDER = {"scene": 0, "sound": 1, "said": 2}


def timeline(frames, new_text, segments, sounds=()):
    """Scenes, the text that appeared with them, the sounds heard and what was said, in time order"""
    rows = []
    for index, frame in enumerate(frames):
        shown = new_text[index] if new_text and index < len(new_text) else []
        rows.append({"t": round(frame["t"], 1), "kind": "scene", "n": index + 1, "text": " | ".join(shown)})
    for sound in sounds or ():
        if sound["label"] != "speech" or not segments:
            rows.append({"t": sound["start"], "end": sound["end"], "kind": "sound", "text": listen.sound_text(sound),
                         "confidence": sound["confidence"]})
    for piece in segments or ():
        rows.append({"t": piece["start"], "end": piece["end"], "kind": "said", "text": piece["text"]})
    rows.sort(key=lambda row: (row["t"], ORDER[row["kind"]]))
    return rows


def timeline_text(rows, throughout=()):
    """The timeline as the lines the model reads."""
    lines = []
    for row in rows:
        if row["kind"] == "scene":
            lines.append(f"{clock_text(row['t'])} scene {row['n']}" + (f": on screen “{row['text']}”" if row["text"]
                                                                        else ""))
        elif row["kind"] == "sound":
            lines.append(f"{clock_text(row['t'])}-{clock_text(row['end'])} sound: {row['text']} "
                         f"(confidence {row['confidence']:.2f})")
        else:
            lines.append(f"{clock_text(row['t'])}-{clock_text(row['end'])} said: “{row['text']}”")
    if throughout:
        lines.append("On screen the whole time: " + " | ".join(f"“{text}”" for text in throughout))
    return "\n".join(lines)


def _audio_started(audio, cancelled, clock):
    """Whether the recording started: within AUDIO_START_TIMEOUT"""
    if audio.wait_started(AUDIO_START_TIMEOUT):
        return True
    deadline = clock() + PERMISSION_WAIT
    while audio.asking() and not cancelled() and clock() < deadline:
        if audio.wait_started(0.5):
            return True
    return False


def watch(driver, seconds=DEFAULT_SECONDS, *, until_end=False, hear=True, udid=None, locale=None,
          cancelled=lambda: False, session=None, ocr_helper=None, run=subprocess.run, clock=time.monotonic,
          sleep=time.sleep):
    """Watch (and, on a USB iPhone with ``hear``, listen to) what the phone plays for ``seconds``"""
    if not native_helpers.devices_allowed():
        raise WatchError("devices_off", "Devices are off in this process (MOBSTER_NO_DEVICES=1).")
    seconds = max(1, min(MAX_SECONDS, int(seconds)))
    audio, hearing, reason = None, None, None
    if hear:
        try:
            audio = (session(seconds) if session is not None
                     else listen.session_for(seconds, udid=udid, locale=locale, frames=True, clock=clock))
        except listen.ListenError as error:
            hearing, reason = str(error), error.code
    try:
        if audio is not None and not _audio_started(audio, cancelled, clock):
            # It failed before recording (no iPhone on the cable, permission refused): watch without it.
            try:
                audio.result(timeout=10)
                hearing, reason = "The iPhone's sound didn't start recording.", "not_started"
            except listen.ListenError as error:
                hearing, reason = str(error), error.code
            audio.close()
            audio = None
        # The USB phone's capture runs from here: iOS shows a placeholder status bar (9:41) until it stops. Hearing
        # through iPhone Mirroring opens no capture.
        captured = audio is not None and getattr(audio, "opens_capture", True)
        source = PhoneFrames(driver, clock=clock, capture=audio)
        started = clock()
        try:
            frames, ended, samples = sample(source, seconds, until_end=until_end, audio=audio, cancelled=cancelled,
                                            clock=clock, sleep=sleep)
        finally:
            source.close()
        watched = round(clock() - started, 1)
        heard_result = None
        hear_ms = None
        if audio is not None:
            begun = clock()
            try:
                heard_result = audio.result()
            except listen.ListenError as error:
                hearing, reason = str(error), error.code
            hear_ms = round((clock() - begun) * 1000)
    finally:
        if audio is not None:
            audio.close()
    settling = _Settle(driver, clock) if captured else None
    try:
        result = _finish(frames, ended, samples, watched, heard_result, hearing, reason, captured, source, hear_ms,
                         ocr_helper, run, clock)
    finally:
        if settling is not None:
            settling.join(LINK_SETTLE_SECONDS)
    result["captureFrames"] = source.captured
    if settling is not None:
        result["ms"]["settle"], result["phoneBack"] = settling.ms, settling.ok
    return result


class _Settle:
    """Brings WebDriverAgent back after the capture closed (it resets the phone's USB network link)."""

    def __init__(self, driver, clock):
        self.ok, self.ms, self.thread = None, None, None
        after = getattr(driver, "after_capture", None)
        if not callable(after):
            return

        def bring_back():
            begun = clock()
            try:
                after()
                self.ok = True
            except Exception:  # noqa: BLE001 -- the next step's own recovery takes over
                self.ok = False
            self.ms = round((clock() - begun) * 1000)
        self.thread = threading.Thread(target=bring_back, name="mobster-watch-settle", daemon=True)
        self.thread.start()

    def join(self, timeout):
        if self.thread is not None:
            self.thread.join(timeout)


def _finish(frames, ended, samples, watched, heard_result, hearing, reason, captured, source, hear_ms, ocr_helper,
            run, clock):
    if not frames:
        raise WatchError("no_frames", "No picture came from the iPhone while watching. Check that it's connected and "
                                      "unlocked, then try again.")
    shown = pick(frames)
    begun = clock()
    lines = read_lines(shown, helper=ocr_helper, run=run)
    text_ms = round((clock() - begun) * 1000)
    new_text, throughout = captions(lines) if lines is not None else ([[] for _ in shown], [])
    segments = heard_result.get("segments", []) if heard_result else []
    if heard_result and heard_result.get("text") and not segments:
        segments = [{"start": 0.0, "end": watched, "text": heard_result["text"]}]
    sounds = heard_result.get("sounds", []) if heard_result else []
    rows = timeline(shown, new_text, segments, sounds)
    return {"seconds": watched, "ended": ended, "frames": [{"t": frame["t"]} for frame in shown],
            "samples": samples, "sheet": contact_sheet(shown, hide_status_bar=captured),
            "last": frames[-1]["data"], "timeline": rows, "captured": captured,
            "throughout": throughout, "text": timeline_text(rows, throughout),
            "transcript": heard_result.get("text", "") if heard_result else "",
            "words": heard_result.get("words", 0) if heard_result else 0, "sounds": sounds,
            "heard": heard_result is not None,
            "silent": bool(heard_result and heard_result.get("silent")), "hearing": hearing,
            "reason": None if heard_result is not None else reason,
            **({"note": heard_result["note"]} if heard_result and heard_result.get("note") else {}),
            "textRead": lines is not None, "screenshots": source.screenshots,
            "ms": {"watch": round(watched * 1000), "text": text_ms, "hear": hear_ms}}
