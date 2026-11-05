"""Hearing what an iPhone plays (MOBSTER_LISTEN, on unless set to off): a voice note, a video, a reel."""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import native_helpers

log = logging.getLogger("mobster.listen")
SETTING = "MOBSTER_LISTEN"
MAX_SECONDS = 60
DEFAULT_SECONDS = 10
# A recording whose loudest moment is quieter than this heard nothing: the phone played no sound.
SILENT_DB = -50.0
# A recording that never left digital zero (the helper reports -160 dB) got no audio at all. On an iPhone that is
# playing, its sound is going somewhere else: Bluetooth or AirPlay, which a USB capture never hears.
DIGITAL_SILENCE_DB = -120.0
SILENCE = ("Mobster heard silence. If your iPhone's sound is going to headphones, a car or a speaker over Bluetooth, "
           "Mobster can't hear it; play it on the iPhone's speaker.")
QUIET = "Mobster heard almost nothing: check that the media is playing and the iPhone isn't muted."
# A WATCH hears for at most this long (the helper takes up to 120 s).
LIVE_MAX_SECONDS = 120
OFF = (f"Hearing the iPhone is off. Turn it on in Settings, or with {SETTING}=on in the env file, then try again.")
# macOS doesn't let Mobster capture the iPhone (the helper's "permission", or an older helper's "capture_denied").
# The helper's own words come first; this is for an answer without them.
PERMISSION = ("Mobster needs permission to hear your iPhone. Allow it in System Settings › Privacy & Security › "
              "Microphone/Camera.")
PERMISSION_CODES = ("permission", "capture_denied")
# Failures that come after the helper opened the phone's capture device, which resets the phone's USB network link
# (drivers.WDA_LINK_BLIP_SECONDS): the caller still brings WebDriverAgent back. Earlier failures opened nothing.
AFTER_CAPTURE = ("capture_failed", "no_engine", "speech_denied", "transcribe_failed")


class ListenError(RuntimeError):
    """``code`` names what failed; the message says what happened and what to do."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def enabled():
    """On unless MOBSTER_LISTEN is off (0, off, false or no): the Mac app's Settings › Hear the iPhone."""
    return os.environ.get(SETTING, "").strip().lower() not in ("0", "off", "false", "no")


def helper_path():
    """The phone-audio helper: the Mac app's, else one built from this checkout (needs Xcode's tools)."""
    found = native_helpers.find("phone-audio")
    if found is not None:
        return found
    try:
        return native_helpers.build("phone_audio")
    except (LookupError, OSError, subprocess.SubprocessError) as error:
        raise ListenError("no_helper", "Mobster's audio helper isn't built, and this Mac can't build it: "
                                       f"{error}. Install Xcode's command line tools, then try again.") from None


def listen(seconds=DEFAULT_SECONDS, *, udid=None, locale=None, keep=None, run=subprocess.run, helper=None,
           phones=None):
    """What the USB iPhone ``udid`` (else the only one) plays for ``seconds`` (1 to 60)"""
    if not enabled():
        raise ListenError("off", OFF)
    if not native_helpers.devices_allowed():
        raise ListenError("devices_off", "Devices are off in this process (MOBSTER_NO_DEVICES=1).")
    seconds = max(1, min(MAX_SECONDS, int(seconds)))
    if udid is None and mirroring_on():
        return listen_mirroring(seconds, locale=locale, keep=keep, run=run)
    executable = helper or helper_path()
    work = Path(tempfile.mkdtemp(prefix="mobster-listen-"))
    os.chmod(work, 0o700)
    try:
        kept = work / "audio.wav" if keep else None
        argv = [str(executable), "listen", "--seconds", str(seconds), "--work", str(work)]
        argv += ["--udid", udid] + capture_hints(udid, phones) if udid else []
        argv += ["--locale", locale] if locale else []
        argv += ["--keep", str(kept)] if kept else []
        try:
            completed = run(argv, capture_output=True, text=True, timeout=seconds + 120)
        except subprocess.TimeoutExpired:
            raise ListenError("helper_failed", "Mobster's audio helper didn't finish. Try again.") from None
        return _finish(_answer(completed.stdout), seconds, keep=keep)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def mirroring_on():
    """Whether this process hears the phone through iPhone Mirroring (its experimental connection is on)."""
    from . import mirroring
    return mirroring.enabled()


def mirroring_helper():
    from . import mirroring
    try:
        return mirroring.helper_path()
    except (LookupError, OSError, subprocess.SubprocessError, RuntimeError) as error:
        raise ListenError("no_helper",
                          f"Mobster's Mirroring helper isn't built, and this Mac can't build it: {error}.")\
            from None


def understood(recorded, *, locale=None, run=subprocess.run):
    """phone-audio's answer for a Mirroring recording"""
    if not recorded.get("ok"):
        return recorded
    audio = str(recorded.get("file") or "")
    executable = str(helper_path())
    answer = {"ok": True, "recordedSeconds": recorded.get("recordedSeconds"), "peakDb": recorded.get("peakDb"),
              "kept": audio}
    try:
        words = _answer(run([executable, "transcribe", audio] + (["--locale", locale] if locale else []),
                            capture_output=True, text=True, timeout=180).stdout)
        sounds = _answer(run([executable, "classify", audio], capture_output=True, text=True, timeout=120).stdout)
    except subprocess.TimeoutExpired:
        raise ListenError("helper_failed", "Mobster's audio helper didn't finish. Try again.") from None
    if not words.get("ok"):
        return words
    answer.update({key: words.get(key) for key in ("text", "segments", "engine", "locale", "transcribeMs")})
    if sounds.get("ok"):
        answer.update({key: sounds.get(key) for key in ("soundWindows", "classifyMs")})
    else:
        answer["soundsError"] = sounds.get("message") or "the sounds weren't told apart"
    return answer


def listen_mirroring(seconds, *, locale=None, keep=None, run=subprocess.run):
    """listen() over iPhone Mirroring: its sound recorded on this Mac (``mirroring audio``), then understood."""
    work = Path(tempfile.mkdtemp(prefix="mobster-listen-"))
    os.chmod(work, 0o700)
    try:
        argv = [str(mirroring_helper()), "audio", "--seconds", str(seconds), "--out", str(work / "audio.caf")]
        try:
            recorded = _answer(run(argv, capture_output=True, text=True, timeout=seconds + 30).stdout)
        except subprocess.TimeoutExpired:
            raise ListenError("helper_failed", "Mobster's Mirroring helper didn't finish. Try again.") from None
        return _finish(understood(recorded, locale=locale, run=run), seconds, keep=keep)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _finish(answer, seconds, *, keep=None):
    """The helper's answer as listen()'s result; raises ListenError for a failure it reports."""
    if not answer.get("ok"):
        code = str(answer.get("error") or "helper_failed")
        if code in PERMISSION_CODES:
            raise ListenError("permission", str(answer.get("message") or PERMISSION))
        raise ListenError(code, str(answer.get("message") or "Mobster couldn't hear the iPhone. Try again."))
    text = " ".join(str(answer.get("text") or "").split())
    peak = answer.get("peakDb")
    result = {"text": text, "words": len(text.split()), "engine": answer.get("engine"),
              "locale": answer.get("locale"), "seconds": seconds, "findMs": answer.get("findMs"),
              "recordMs": answer.get("recordMs"), "transcribeMs": answer.get("transcribeMs"),
              "segments": segments_of(answer.get("segments")), "sounds": sounds_of(answer.get("soundWindows")),
              "classifyMs": answer.get("classifyMs"),
              "silent": isinstance(peak, (int, float)) and peak < SILENT_DB}
    if answer.get("soundsError"):
        log.warning("the sounds weren't told apart: %s", answer["soundsError"])
    if answer.get("match") in MATCHES:
        result["match"] = answer["match"]
        log.info("heard the iPhone through the capture device found by %s", MATCHES[answer["match"]])
    if isinstance(answer.get("recordedSeconds"), (int, float)):
        result["recordedSeconds"] = round(float(answer["recordedSeconds"]), 1)
    if isinstance(peak, (int, float)):
        result["peakDb"] = peak
    note = silence_note(result)
    if note:
        result["note"] = note
    audio = Path(answer["kept"]) if isinstance(answer.get("kept"), str) else None
    if keep and audio and audio.is_file():
        target = Path(keep).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(audio), target)
        result["kept"] = str(target)
    return result


def silence_note(result):
    """What to say about a recording with no words in it that was silent, else None"""
    if not result or not result.get("silent") or (result.get("text") or result.get("transcript")):
        return None
    peak = result.get("peakDb")
    return SILENCE if isinstance(peak, (int, float)) and peak <= DIGITAL_SILENCE_DB else QUIET


def segments_of(value):
    """The helper's timed pieces of speech as [{start, end, text}] (seconds, one decimal), dropping any it garbled."""
    out = []
    for piece in value if isinstance(value, list) else ():
        if not isinstance(piece, dict):
            continue
        text = " ".join(str(piece.get("text") or "").split())
        start, end = piece.get("start"), piece.get("end")
        if text and isinstance(start, (int, float)) and isinstance(end, (int, float)):
            out.append({"start": round(float(start), 1), "end": round(float(max(start, end)), 1), "text": text})
    return out


# A sound starts when the classifier is this sure of it in a window, and carries on through the next windows while it
# stays at least SOUND_STAY sure. The helper reports nothing below 0.3.
SOUND_ENTER = 0.5
SOUND_STAY = 0.35
# An instrument is named with the music when it was this sure on average over the music's windows; at most 3 are.
INSTRUMENT_MEAN = 0.4
INSTRUMENTS_NAMED = 3
# Quiet music under speech: labelled "background music" when this share of the windows hear it (_background_music).
BACKGROUND_SHARE = 0.6
# At most this many sounds for a recording (120 s at most): the surest and longest are kept.
MAX_SOUNDS = 24
# Apple's labels that read as one sound: laughter, not giggling and a snicker besides. A label not here reads as
# itself, its underscores as spaces. Silence isn't a sound.
FAMILIES = {
    "laughter": "laughter baby_laughter giggling snicker belly_laugh chuckle_chortle",
    "shouting": "shout yell battle_cry children_shouting screaming",
    "crying": "crying_sobbing baby_crying",
    "singing": "singing choir_singing yodeling humming",
    "applause": "clapping applause",
    "crowd": "chatter crowd babble",
    "a dog": "dog dog_bark dog_howl dog_bow_wow dog_growl dog_whimper",
    "a cat": "cat cat_purr cat_meow",
    "birds": "bird bird_vocalization bird_chirp_tweet bird_squawk pigeon_dove_coo crow_caw owl_hoot bird_flapping",
    "a siren": "siren police_siren ambulance_siren fire_engine_siren civil_defense_siren emergency_vehicle",
    "a phone ringing": "telephone telephone_bell_ringing ringtone",
    "an alarm": "alarm_clock smoke_detector",
    "a bell": "bell church_bell bicycle_bell cowbell chime wind_chime tuning_fork singing_bowl",
    "rain": "rain raindrop thunderstorm thunder",
    "water": "water stream_burbling waterfall ocean sea_waves gurgling",
    "wind": "wind wind_rustling_leaves wind_noise_microphone",
    "traffic": "car_passing_by traffic_noise vehicle_skidding race_car truck bus motorcycle engine engine_starting "
               "engine_idling engine_accelerating_revving engine_knocking car_horn air_horn reverse_beeps",
    "gunshots": "gunshot_gunfire artillery_fire",
    "fireworks": "fireworks firecracker",
    "typing": "typing typewriter typing_computer_keyboard",
    "footsteps": "person_running person_shuffling person_walking",
    "a door": "door door_slam door_sliding knock door_bell drawer_open_close",
}
# Instruments are music; which ones is said with it: "music with piano and drums".
INSTRUMENTS = {
    "guitar": "plucked_string_instrument guitar electric_guitar bass_guitar acoustic_guitar steel_guitar_slide_guitar "
              "guitar_tapping guitar_strum banjo sitar mandolin zither ukulele",
    "piano": "keyboard_musical piano electric_piano harpsichord",
    "organ": "organ electronic_organ hammond_organ",
    "synthesizer": "synthesizer theremin",
    "drums": "percussion drum_kit drum snare_drum bass_drum timpani tabla cymbal hi_hat tambourine rattle_instrument "
             "gong disc_scratching",
    "mallets": "mallet_percussion marimba_xylophone glockenspiel vibraphone steelpan",
    "strings": "orchestra bowed_string_instrument violin_fiddle cello double_bass harp",
    "brass": "brass_instrument french_horn trumpet trombone shofar didgeridoo",
    "woodwinds": "wind_instrument flute saxophone clarinet oboe bassoon harmonica accordion bagpipes",
}
_FAMILY = {label: name for name, labels in FAMILIES.items() for label in labels.split()}
_INSTRUMENT = {label: name for name, labels in INSTRUMENTS.items() for label in labels.split()}
_LABEL = re.compile(r"[a-z][a-z_]{0,39}")


def _sound_of(label):
    """(sound, instrument) for one of Apple's labels: ("music", "piano") for piano; (None, None) for silence."""
    if label == "silence":
        return None, None
    if label in _INSTRUMENT:
        return "music", _INSTRUMENT[label]
    return _FAMILY.get(label, label.replace("_", " ")), None


def sounds_of(value):
    """The helper's classified windows"""
    windows = []
    for window in value if isinstance(value, list) else ():
        if not isinstance(window, dict) or not isinstance(window.get("labels"), dict):
            continue
        start, length = window.get("t"), window.get("d")
        if not isinstance(start, (int, float)) or not isinstance(length, (int, float)) or length <= 0:
            continue
        sounds, instruments = {}, {}
        for label, confidence in window["labels"].items():
            if not isinstance(label, str) or not _LABEL.fullmatch(label) or not isinstance(confidence, (int, float)):
                continue
            sound, instrument = _sound_of(label)
            if sound is None:
                continue
            sounds[sound] = max(sounds.get(sound, 0.0), float(confidence))
            if instrument:
                instruments[instrument] = max(instruments.get(instrument, 0.0), float(confidence))
        windows.append((float(start), float(start) + float(length), sounds, instruments))
    windows.sort(key=lambda window: window[0])
    found = []
    for sound in sorted({sound for window in windows for sound in window[2]}):
        run = []
        for window in windows + [None]:
            confidence = window[2].get(sound, 0.0) if window else 0.0
            if run and confidence >= SOUND_STAY:
                run.append(window)
                continue
            if run:
                found.append(_sound(sound, run))
            run = [window] if confidence >= SOUND_ENTER else []
    if windows and not any(item["label"] == "music" for item in found):
        background = _background_music(windows)
        if background:
            found.append(background)
    found.sort(key=lambda item: -item["confidence"] * (item["end"] - item["start"]))
    return sorted(found[:MAX_SOUNDS], key=lambda item: (item["start"], -item["confidence"]))


def _background_music(windows):
    """Music too quiet to be sure of in any one window (under SOUND_ENTER), but there through the recording"""
    heard = [window for window in windows if window[2].get("music", 0.0) >= SOUND_STAY]
    if len(windows) < 2 or len(heard) < BACKGROUND_SHARE * len(windows):
        return None
    item = _sound("music", heard)
    item["label"] = "background music"
    return item


def _sound(sound, run):
    item = {"start": round(run[0][0], 1), "end": round(max(window[1] for window in run), 1), "label": sound,
            "confidence": round(sum(window[2][sound] for window in run) / len(run), 2)}
    if sound == "music":
        means = {name: sum(window[3].get(name, 0.0) for window in run) / len(run)
                 for name in {name for window in run for name in window[3]}}
        named = sorted((name for name, mean in means.items() if mean >= INSTRUMENT_MEAN), key=lambda n: -means[n])
        if named[:INSTRUMENTS_NAMED]:
            item["instruments"] = named[:INSTRUMENTS_NAMED]
    return item


def sound_text(sound):
    """One sound as words: "music with piano and synthesizer"."""
    instruments = sound.get("instruments") or []
    if not instruments:
        return sound["label"]
    listed = instruments[0] if len(instruments) == 1 else ", ".join(instruments[:-1]) + " and " + instruments[-1]
    return f"{sound['label']} with {listed}"


def sound_lines(sounds, *, clock):
    """The sounds as the lines the model reads: "0:00-0:12 sound: music with piano (confidence 0.85)"."""
    return [f"{clock(sound['start'])}-{clock(sound['end'])} sound: {sound_text(sound)} "
            f"(confidence {sound['confidence']:.2f})" for sound in sounds]


class Session:
    """A recording of the iPhone's audio running in the background while WATCH samples the screen."""

    QUIET_DB = -45.0
    # It opens the USB iPhone's capture device: iOS shows a 9:41 status bar until it closes, and closing resets the
    # phone's link (watch.py).
    opens_capture = True
    helper_name, margin = "audio", 120
    no_answer = "Mobster's audio helper gave no answer. Try again."

    def __init__(self, seconds, *, udid=None, locale=None, helper=None, popen=subprocess.Popen, phones=None,
                 frames=False, clock=None):
        if not enabled():
            raise ListenError("off", OFF)
        if not native_helpers.devices_allowed():
            raise ListenError("devices_off", "Devices are off in this process (MOBSTER_NO_DEVICES=1).")
        self.seconds = max(1, min(LIVE_MAX_SECONDS, int(seconds)))
        executable = helper or helper_path()
        self.work = Path(tempfile.mkdtemp(prefix="mobster-watch-"))
        os.chmod(self.work, 0o700)
        argv = [str(executable), "listen", "--seconds", str(self.seconds), "--work", str(self.work), "--live"]
        argv += ["--udid", udid] + capture_hints(udid, phones) if udid else []
        argv += ["--locale", locale] if locale else []
        argv += ["--frames"] if frames else []
        self._clock = clock or time.monotonic
        self._frame = None        # (seq, JPEG bytes, clock() when it came): the phone's picture from the capture
        self._started = threading.Event()
        self._lock = threading.Lock()
        self._lines = []
        self._asking = False      # macOS is asking for the camera or the microphone
        self._loud_at = None      # recording time of the last level above QUIET_DB
        self._level_at = None     # recording time of the newest level
        try:
            self.process = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, bufsize=1)
        except OSError as error:
            shutil.rmtree(self.work, ignore_errors=True)
            raise ListenError("helper_failed", f"Mobster's audio helper couldn't start ({error}).") from None
        self._reader = threading.Thread(target=self._read, name="mobster-watch-audio", daemon=True)
        self._reader.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            with self._lock:
                if value.get("event") == "asking":
                    self._asking = True
                elif value.get("event") == "recording":
                    self._asking = False
                    self._started.set()
                elif value.get("event") == "frame":
                    self._take_frame(value)
                elif value.get("event") == "level":
                    t, db = value.get("t"), value.get("db")
                    if isinstance(t, (int, float)) and isinstance(db, (int, float)):
                        self._level_at = float(t)
                        if db > self.QUIET_DB:
                            self._loud_at = float(t)
                else:
                    self._lines.append(value)
        self._started.set()  # the helper ended (a failure before recording, too): nobody waits on it any longer

    def _take_frame(self, value):
        """Keep the helper's newest picture (only in this session's folder), read at once. Called with the lock held."""
        path, seq = value.get("path"), value.get("seq")
        if not isinstance(path, str) or not isinstance(seq, int):
            return
        try:
            target = Path(path).resolve()
            if self.work.resolve() not in target.parents:
                return
            data = target.read_bytes()
        except OSError:
            return
        if data:
            self._frame = (seq, data, self._clock())

    def latest_frame(self):
        """(seq, JPEG bytes, when) of the capture's newest picture, else None; WDA is unreliable during a capture."""
        with self._lock:
            return self._frame

    def wait_started(self, timeout):
        """True once the recording runs; False when the helper ended first or ``timeout`` passed."""
        self._started.wait(timeout)
        with self._lock:
            return self._started.is_set() and not self._lines and self.process.poll() is None

    def asking(self):
        """True while macOS asks the person whether Mobster may capture the iPhone, and the helper waits for them."""
        with self._lock:
            return self._asking and not self._started.is_set()

    def quiet_for(self):
        """Seconds since the phone last made a sound above QUIET_DB; None before it made any."""
        with self._lock:
            if self._loud_at is None or self._level_at is None:
                return None
            return max(0.0, self._level_at - self._loud_at)

    def stop(self):
        """End the recording now; what it has is transcribed."""
        try:
            self.process.stdin.write("stop\n")
            self.process.stdin.flush()
            self.process.stdin.close()
        except (OSError, ValueError, AttributeError):
            pass

    def result(self, timeout=None):
        """listen()'s result for what was recorded (``seconds`` is how long it ran); raises ListenError."""
        self.stop()
        try:
            self.process.wait(timeout=timeout if timeout is not None else self.seconds + self.margin)
        except subprocess.TimeoutExpired:
            self.close()
            raise ListenError("helper_failed",
                              f"Mobster's {self.helper_name} helper didn't finish. Try again.") from None
        self._reader.join(timeout=5)
        with self._lock:
            answer = self._lines[-1] if self._lines else {
                "ok": False, "error": "helper_failed", "message": self.no_answer}
        heard = answer.get("recordedSeconds") if isinstance(answer.get("recordedSeconds"), (int, float)) else None
        return _finish(self._understand(answer), round(heard) if heard else self.seconds)

    def _understand(self, answer):
        return answer

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        shutil.rmtree(self.work, ignore_errors=True)


class MirroringSession(Session):
    """Session over iPhone Mirroring: the Mirroring app's sound, recorded on this Mac"""

    opens_capture = False  # the phone's own status bar shows, and nothing on the phone resets
    helper_name, margin = "Mirroring", 30
    no_answer = "Mobster's Mirroring helper gave no answer."

    def __init__(self, seconds, *, locale=None, popen=subprocess.Popen, run=subprocess.run):
        self.locale, self._run = locale, run
        super().__init__(seconds, locale=locale, helper="mirroring", popen=self._popen(popen))

    def _popen(self, popen):
        def start(argv, **kwargs):
            work = argv[argv.index("--work") + 1]
            mirrored = [str(mirroring_helper()), "audio", "--seconds", argv[argv.index("--seconds") + 1],
                        "--out", str(Path(work) / "audio.caf"), "--live"]
            return popen(mirrored, **kwargs)
        return start

    def _understand(self, recorded):
        return understood(recorded, locale=self.locale, run=self._run)


def session_for(seconds, *, udid=None, locale=None, frames=False, clock=None):
    """The recording WATCH starts: over iPhone Mirroring when that connection is on and no USB iPhone is named"""
    if udid is None and mirroring_on():
        return MirroringSession(seconds, locale=locale)
    return Session(seconds, udid=udid, locale=locale, frames=frames, clock=clock)


# How the helper told the iPhone's capture device, as the log says it: never the phone's name or UDID.
MATCHES = {"udid": "its UDID", "name": "the phone's name", "only": "being the only iPhone"}
# How long a phone's name, once read, is used without reading it again.
NAME_TTL = 600.0
_names = {}
_names_lock = threading.Lock()


def usb_phones(*, tool=None, run=None, clock=None):
    """The iPhones on this Mac's USB as the device layer reads them (libimobiledevice)"""
    from . import mac_tools
    from .devices import UDID_PATTERN
    tool, run, clock = tool or mac_tools.tool, run or mac_tools.run, clock or time.monotonic
    listing = tool("idevice_id")
    output = run([listing, "-l"], timeout=3) if listing else None
    if output is None:
        return None
    udids = []
    for line in output.splitlines():
        udid = line.strip()
        # 0000FE01-… entries are not physical USB phones (device_manager.devices).
        if UDID_PATTERN.fullmatch(udid) and not udid.startswith("0000FE01") and udid not in udids:
            udids.append(udid)
    info = tool("ideviceinfo")
    phones = []
    for udid in udids:
        with _names_lock:
            cached = _names.get(udid)
        if cached is None or clock() - cached[0] >= NAME_TTL:
            value = run([info, "-u", udid, "-k", "DeviceName"], timeout=5) if info else None
            cached = (clock(), " ".join((value or "").split()) or None)
            if cached[1]:
                with _names_lock:
                    _names[udid] = cached
        phones.append({"udid": udid, "name": cached[1]})
    return phones


def capture_hints(udid, phones=None):
    """``--name`` and ``--phones``: how the helper finds ``udid`` when its capture device has an id of its own."""
    from .devices import UDID_PATTERN
    if not isinstance(udid, str) or not UDID_PATTERN.fullmatch(udid):
        return []
    try:
        attached = (phones or usb_phones)()
    except Exception:  # noqa: BLE001 -- without the hints the UDID still works
        attached = None
    if not attached:
        return []
    this = next((phone for phone in attached if str(phone.get("udid") or "").upper() == udid.upper()), None)
    if this is None:
        return []
    hints = []
    name = " ".join(str(this.get("name") or "").split())
    if name and sum(" ".join(str(phone.get("name") or "").split()).casefold() == name.casefold()
                    for phone in attached) == 1:
        hints += ["--name", name]
    return hints + ["--phones", str(len(attached))]


def _answer(stdout):
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {"ok": False, "error": "helper_failed", "message": "Mobster's audio helper gave no answer. Try again."}


def phone_udid(run_context):
    """The UDID of a run's USB iPhone, or None (a simulator, an address, or one Mobster can't name)."""
    if run_context is None:
        return None
    kind = getattr(run_context, "device_kind", None)
    if kind not in (None, "usb"):
        return None
    device = getattr(run_context, "device_id", None)
    if device and kind == "usb":
        return device
    manager = getattr(getattr(run_context, "runtime", None), "manager", None)
    try:
        return (getattr(manager, "pinned", None) or manager.settings().get("udid")) if manager is not None else None
    except Exception:  # noqa: BLE001
        return None
