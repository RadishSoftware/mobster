import io
import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mobile_agent import listen, mirroring, watch
from mobile_agent.tests import watch_world as world

ON = {"MOBSTER_LISTEN": "on", "MOBSTER_NO_DEVICES": ""}


class FakeHelper:
    def __init__(self, answer=None, raises=None):
        self.answer = answer if answer is not None else {
            "ok": True, "text": "  Meet me at  the station at six. ", "engine": "speechanalyzer", "locale": "en-US",
            "seconds": 5, "recordMs": 5210, "waitMs": 180, "transcribeMs": 640, "peakDb": -12.5}
        self.raises = raises
        self.argv = None
        self.work_existed = None

    def __call__(self, argv, capture_output, text, timeout):
        self.argv = argv
        work = Path(argv[argv.index("--work") + 1])
        self.work_existed = work.is_dir()
        (work / "capture-1").mkdir()
        (work / "capture-1" / "audio.wav").write_bytes(b"RIFF")
        if self.raises:
            raise self.raises
        answer = dict(self.answer)
        if "--keep" in argv:
            Path(argv[argv.index("--keep") + 1]).write_bytes(b"RIFF....WAVE")
            answer["kept"] = argv[argv.index("--keep") + 1]
        return SimpleNamespace(stdout="warming up\n" + json.dumps(answer) + "\n", returncode=0)


class ListenTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, ON)
        env.start()
        self.addCleanup(env.stop)

    def work_of(self, helper):
        return Path(helper.argv[helper.argv.index("--work") + 1])

    def test_on_by_default_and_refused_when_turned_off(self):
        with mock.patch.dict(os.environ, {"MOBSTER_LISTEN": ""}):
            self.assertTrue(listen.enabled())
        for off in ("off", "0", "false", "no"):
            with mock.patch.dict(os.environ, {"MOBSTER_LISTEN": off}):
                self.assertFalse(listen.enabled())
                with self.assertRaises(listen.ListenError) as caught:
                    listen.listen(5, run=self.fail, helper="/bin/phone-audio")
        self.assertEqual(caught.exception.code, "off")
        self.assertIn("MOBSTER_LISTEN=on", str(caught.exception))
        with mock.patch.dict(os.environ, {"MOBSTER_NO_DEVICES": "1"}), self.assertRaises(listen.ListenError) as caught:
            listen.listen(5, run=self.fail, helper="/bin/phone-audio")
        self.assertEqual(caught.exception.code, "devices_off")

    def test_it_hears_the_words_and_deletes_the_recording(self):
        helper = FakeHelper()
        result = listen.listen(5, udid="<UDID>", locale="en-US", run=helper, helper="/bin/phone-audio")
        self.assertEqual(result["text"], "Meet me at the station at six.")
        self.assertEqual((result["words"], result["engine"], result["silent"], result["seconds"]),
                         (7, "speechanalyzer", False, 5))
        self.assertEqual(helper.argv[:4], ["/bin/phone-audio", "listen", "--seconds", "5"])
        self.assertIn("--udid", helper.argv)
        self.assertNotIn("--keep", helper.argv)
        self.assertTrue(helper.work_existed)
        self.assertFalse(self.work_of(helper).exists())

    def test_the_recording_is_deleted_when_the_helper_fails_or_hangs(self):
        for fake, code in ((FakeHelper({"ok": False, "error": "no_device", "message": "No iPhone to hear."}),
                            "no_device"),
                           (FakeHelper(raises=subprocess.TimeoutExpired("phone-audio", 1)), "helper_failed"),
                           (FakeHelper({"nonsense": True}), "helper_failed")):
            with self.subTest(code=code), self.assertRaises(listen.ListenError) as caught:
                listen.listen(5, run=fake, helper="/bin/phone-audio")
            self.assertEqual(caught.exception.code, code)
            self.assertFalse(self.work_of(fake).exists())

    def test_seconds_are_bounded(self):
        helper = FakeHelper()
        listen.listen(500, run=helper, helper="/bin/phone-audio")
        self.assertEqual(helper.argv[3], str(listen.MAX_SECONDS))
        listen.listen(0, run=helper, helper="/bin/phone-audio")
        self.assertEqual(helper.argv[3], "1")

    def test_digital_silence_says_the_sound_may_be_going_elsewhere(self):
        result = listen.listen(8, run=FakeHelper({"ok": True, "text": "", "peakDb": -160}), helper="/bin/phone-audio")
        self.assertEqual((result["text"], result["silent"], result["peakDb"], result["note"]),
                         ("", True, -160, listen.SILENCE))
        self.assertIn("over Bluetooth, Mobster can't hear it; play it on the iPhone's speaker", result["note"])
        quiet = listen.listen(8, run=FakeHelper({"ok": True, "text": "", "peakDb": -70}), helper="/bin/phone-audio")
        self.assertEqual((quiet["silent"], quiet["note"]), (True, listen.QUIET))
        loud = listen.listen(8, run=FakeHelper({"ok": True, "text": "", "peakDb": -20}), helper="/bin/phone-audio")
        self.assertEqual((loud["silent"], "note" in loud), (False, False))
        self.assertNotIn("note", listen.listen(5, run=FakeHelper(), helper="/bin/phone-audio"))

    def test_the_sounds_come_back_collapsed_and_a_classifier_failure_leaves_the_words(self):
        helper = FakeHelper(dict(FakeHelper().answer, soundWindows=world.sounds()["soundWindows"], classifyMs=243))
        result = listen.listen(5, run=helper, helper="/bin/phone-audio")
        self.assertEqual([(sound["label"], sound.get("instruments")) for sound in result["sounds"]],
                         [("music", ["piano", "synthesizer"]), ("speech", None)])
        self.assertEqual(result["classifyMs"], 243)
        with self.assertLogs("mobster.listen", "WARNING"):
            result = listen.listen(5, run=FakeHelper(dict(FakeHelper().answer, soundsError="The sound classifier "
                                                          "failed (busy).")), helper="/bin/phone-audio")
        self.assertEqual((result["text"], result["sounds"]), ("Meet me at the station at six.", []))

    def test_keep_moves_it_where_the_person_asked(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        result = listen.listen(5, keep=folder / "note.wav", run=FakeHelper(), helper="/bin/phone-audio")
        self.assertEqual(Path(result["kept"]).read_bytes(), b"RIFF....WAVE")


MINE = "00008110-00000000ABCD0003"
OTHER = "00008120-00000000ABCD0006"
OWN_ID = "93A8A37A-1111-2222-3333-444455556666"


class WhichIPhoneTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, ON)
        env.start()
        self.addCleanup(env.stop)

    def test_the_phone_name_and_count_go_with_the_udid(self):
        self.assertEqual(listen.capture_hints(MINE, lambda: [{"udid": MINE, "name": " iPhone 17 Pro "}]),
                         ["--name", "iPhone 17 Pro", "--phones", "1"])
        self.assertEqual(listen.capture_hints(MINE.lower(), lambda: [{"udid": MINE, "name": "iPhone 17 Pro"},
                                                                     {"udid": OTHER, "name": "iPhone 16e"}]),
                         ["--name", "iPhone 17 Pro", "--phones", "2"])

    def test_a_shared_name_is_withheld_so_it_cant_pick_the_other_phone(self):
        self.assertEqual(listen.capture_hints(MINE, lambda: [{"udid": MINE, "name": "iPhone"},
                                                             {"udid": OTHER, "name": "IPHONE"}]), ["--phones", "2"])
        self.assertEqual(listen.capture_hints(MINE, lambda: [{"udid": MINE, "name": None}]), ["--phones", "1"])

    def test_nothing_is_hinted_when_the_device_layer_cant_vouch_for_the_phone(self):
        for phones in (lambda: None, lambda: [], lambda: [{"udid": OTHER, "name": "iPhone 17 Pro"}]):
            self.assertEqual(listen.capture_hints(MINE, phones), [])

        def broken():
            raise OSError("no libimobiledevice")
        self.assertEqual(listen.capture_hints(MINE, broken), [])
        self.assertEqual(listen.capture_hints("<UDID>", self.fail), [])

    def test_usb_phones_reads_the_names_once(self):
        calls = []

        def run(argv, timeout):
            calls.append(argv)
            if argv[1:] == ["-l"]:
                return f"{MINE}\n0000FE01-0000000000000000\n{OTHER}\n{MINE}\n"
            return "iPhone 17 Pro\n" if argv[2] == MINE else None
        with mock.patch.dict(listen._names, clear=True):
            read = lambda: listen.usb_phones(tool=lambda name: f"/bin/{name}", run=run, clock=lambda: 100.0)
            self.assertEqual(read(), [{"udid": MINE, "name": "iPhone 17 Pro"}, {"udid": OTHER, "name": None}])
            self.assertEqual(read(), [{"udid": MINE, "name": "iPhone 17 Pro"}, {"udid": OTHER, "name": None}])
        named = [argv for argv in calls if "DeviceName" in argv]
        self.assertEqual(sum(MINE in argv for argv in named), 1)
        self.assertEqual(sum(OTHER in argv for argv in named), 2)
        self.assertIsNone(listen.usb_phones(tool=lambda name: None, run=self.fail))

    def test_listen_and_watch_hand_the_hints_to_the_helper_and_keep_how_it_matched(self):
        helper = FakeHelper(dict(FakeHelper().answer, match="name"))
        with self.assertLogs("mobster.listen", "INFO") as logged:
            result = listen.listen(5, udid=MINE, run=helper, helper="/bin/phone-audio",
                                   phones=lambda: [{"udid": MINE, "name": "iPhone 17 Pro"}])
        at = helper.argv.index("--udid")
        self.assertEqual(helper.argv[at:at + 6], ["--udid", MINE, "--name", "iPhone 17 Pro", "--phones", "1"])
        self.assertEqual(result["match"], "name")
        self.assertEqual(logged.output, ["INFO:mobster.listen:heard the iPhone through the capture device found by "
                                         "the phone's name"])
        self.assertNotIn("iPhone 17 Pro", logged.output[0])
        seen = []

        def popen(argv, **kwargs):
            seen.append(argv)
            raise OSError("not started")
        with self.assertRaises(listen.ListenError):
            listen.Session(10, udid=MINE, helper="/bin/phone-audio", popen=popen,
                           phones=lambda: [{"udid": MINE, "name": "iPhone 17 Pro"},
                                           {"udid": OTHER, "name": "iPhone 16e"}])
        self.assertIn("--live", seen[0])
        self.assertEqual(seen[0][seen[0].index("--udid"):][:6],
                         ["--udid", MINE, "--name", "iPhone 17 Pro", "--phones", "2"])

    def test_an_ambiguous_phone_is_refused_with_its_name(self):
        message = ("2 iPhones are plugged in, and Mobster can't tell which one is “iPhone 17 Pro”. "
                   "Unplug the others, then try again.")
        with self.assertRaises(listen.ListenError) as caught:
            listen.listen(5, udid=MINE, run=FakeHelper({"ok": False, "error": "ambiguous_device", "message": message}),
                          helper="/bin/phone-audio", phones=lambda: [{"udid": MINE, "name": "iPhone 17 Pro"}])
        self.assertEqual((caught.exception.code, str(caught.exception)), ("ambiguous_device", message))


def window(t, **labels):
    return {"t": t, "d": 3.0, "labels": labels}


class SoundTests(unittest.TestCase):
    def test_the_recorded_clip_is_music_with_its_instruments_and_speech(self):
        sounds = listen.sounds_of(world.sounds()["soundWindows"])
        self.assertEqual(sounds, [
            {"start": 0.0, "end": 12.0, "label": "music", "confidence": 0.84, "instruments": ["piano", "synthesizer"]},
            {"start": 0.0, "end": 12.0, "label": "speech", "confidence": 0.73}])
        self.assertEqual(listen.sound_lines(sounds, clock=watch.clock_text),
                         ["0:00-0:12 sound: music with piano and synthesizer (confidence 0.84)",
                          "0:00-0:12 sound: speech (confidence 0.73)"])

    def test_near_same_labels_are_one_sound(self):
        sounds = listen.sounds_of([window(0, giggling=0.7, belly_laugh=0.6, snicker=0.4),
                                   window(1.5, laughter=0.8), window(3, chuckle_chortle=0.5)])
        self.assertEqual(sounds, [{"start": 0.0, "end": 6.0, "label": "laughter", "confidence": 0.67}])
        self.assertEqual(listen.sounds_of([window(0, dog_bark=0.9), window(1.5, dog_growl=0.6)])[0]["label"], "a dog")
        self.assertEqual(listen.sounds_of([window(0, guitar_strum=0.8)]),
                         [{"start": 0.0, "end": 3.0, "label": "music", "confidence": 0.8, "instruments": ["guitar"]}])
        self.assertEqual(listen.sounds_of([window(0, sewing_machine=0.9)])[0]["label"], "sewing machine")

    def test_an_unsure_sound_never_starts_and_a_sure_one_carries_on_while_it_stays(self):
        self.assertEqual(listen.sounds_of([window(0, applause=0.45), window(1.5, applause=0.49)]), [])
        sounds = listen.sounds_of([window(0, applause=0.9), window(1.5, applause=0.4), window(3, applause=0.31),
                                   window(4.5, applause=0.8)])
        self.assertEqual([(s["start"], s["end"], s["confidence"]) for s in sounds],
                         [(0.0, 4.5, 0.65), (4.5, 7.5, 0.8)])

    def test_quiet_music_under_speech_through_the_recording_is_background_music(self):
        bed = [window(0, music=0.4, speech=0.9), window(1.5, speech=0.9), window(3, music=0.42, speech=0.9),
               window(4.5, music=0.44, speech=0.9), window(6, music=0.38, speech=0.9)]
        sounds = listen.sounds_of(bed)
        self.assertEqual([s["label"] for s in sounds], ["speech", "background music"])
        self.assertEqual((sounds[1]["start"], sounds[1]["end"], sounds[1]["confidence"]), (0.0, 9.0, 0.41))
        self.assertEqual(listen.sound_lines(sounds[1:], clock=watch.clock_text),
                         ["0:00-0:09 sound: background music (confidence 0.41)"])
        few = listen.sounds_of([window(0, music=0.4), window(1.5, speech=0.9), window(3, speech=0.9)])
        self.assertNotIn("background music", [s["label"] for s in few])
        self.assertEqual(listen.sounds_of([window(0, music=0.45)]), [])
        self.assertEqual([s["label"] for s in listen.sounds_of([window(0, speech=0.9), window(1.5, speech=0.9)])],
                         ["speech"])
        loud = listen.sounds_of([window(0, music=0.8), window(1.5, music=0.4), window(3, music=0.4)])
        self.assertEqual([s["label"] for s in loud], ["music"])

    def test_instruments_are_named_only_when_heard_through_the_music(self):
        sounds = listen.sounds_of([window(0, music=0.9, drum_kit=0.8, trumpet=0.5, violin_fiddle=0.35),
                                   window(1.5, music=0.9, snare_drum=0.7, trumpet=0.45),
                                   window(3, music=0.9, hi_hat=0.6, french_horn=0.4, saxophone=0.42)])
        self.assertEqual(sounds[0]["instruments"], ["drums", "brass"])
        self.assertEqual(listen.sound_text(sounds[0]), "music with drums and brass")
        self.assertEqual(listen.sound_text({"label": "music", "instruments": ["piano", "drums", "guitar"]}),
                         "music with piano, drums and guitar")

    def test_silence_garbage_and_too_many_are_left_out(self):
        self.assertEqual(listen.sounds_of([window(0, silence=0.99), {"t": "x", "d": 3, "labels": {}},
                                           {"t": 0, "d": 0, "labels": {"music": 1}}, window(0, **{"Bad Label": 0.9}),
                                           window(0, music="loud"), "nonsense"]), [])
        self.assertEqual(listen.sounds_of(None), [])
        many = [window(index * 1.5, **{("dog_bark" if index % 2 else "cat_meow"): 0.6 + index / 1000})
                for index in range(80)]
        sounds = listen.sounds_of(many)
        self.assertEqual(len(sounds), listen.MAX_SOUNDS)
        self.assertEqual([s["start"] for s in sounds], sorted(s["start"] for s in sounds))


def write_wav(path, samples, rate=16_000):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(int(max(-1.0, min(1.0, s)) * 32767).to_bytes(2, "little", signed=True)
                                 for s in samples))


def synthetic_music(seconds, rate=16_000):
    out = [0.0] * int(seconds * rate)
    chords = [(261.63, 329.63, 392.0), (196.0, 246.94, 293.66), (220.0, 261.63, 329.63), (174.61, 220.0, 261.63)]
    for beat in range(int(seconds / 0.5)):
        begin, chord = int(beat * 0.5 * rate), chords[(beat // 4) % 4]
        note = chord[beat % 3] * (2 if beat % 2 else 1)
        for i in range(min(int(0.45 * rate), len(out) - begin)):
            t = i / rate
            fade = math.exp(-4 * t)
            sample = sum(math.sin(2 * math.pi * note * h * t) / h for h in (1, 2, 3, 4)) * 0.25 * fade
            sample += sum(math.sin(2 * math.pi * f * t) for f in chord) * 0.06 * fade
            if beat % 2 == 0:
                sample += math.sin(2 * math.pi * (50 + 80 * math.exp(-30 * t)) * t) * math.exp(-12 * t) * 0.5
            out[begin + i] += sample
    return out


@unittest.skipUnless(shutil.which("xcrun") and os.environ.get("MOBSTER_SKIP_SWIFT") != "1",
                     "needs Xcode's command line tools")
class PhoneAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="mobster-phone-audio-"))
        cls.binary = cls.folder / "phone-audio"
        source = Path(listen.__file__).resolve().parent / "swift" / "helper_phone_audio.swift"
        try:
            subprocess.run(["xcrun", "swiftc", "-Onone", str(source), "-o", str(cls.binary)], check=True,
                           capture_output=True, timeout=300)
        except (subprocess.SubprocessError, OSError) as error:
            raise unittest.SkipTest(f"swiftc couldn't build the helper here ({type(error).__name__})")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)

    def match(self, devices, *args):
        out = subprocess.run([str(self.binary), "match", *args], input=json.dumps(devices), capture_output=True,
                             text=True, timeout=30)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def phone(self, id, name="iPhone 17 Pro"):
        return {"id": id, "name": name, "model": "iOS Device"}

    def test_the_udid_is_tried_first_in_any_spelling(self):
        devices = [self.phone(OWN_ID, "iPhone 16e"), self.phone(MINE.replace("-", ""), "iPhone 17 Pro")]
        self.assertEqual(self.match(devices, "--udid", MINE.lower(), "--name", "iPhone 16e", "--phones", "2"),
                         {"ok": True, "index": 1, "match": "udid"})

    def test_the_phone_name_finds_a_capture_device_with_an_id_of_its_own(self):
        devices = [self.phone(OWN_ID, "iPhone 16e"),
                   self.phone("5C1D2E3F-AAAA-BBBB-CCCC-DDDDEEEEFFFF", "IPHONE 17 PRO")]
        self.assertEqual(self.match(devices, "--udid", MINE, "--name", "iPhone 17 Pro", "--phones", "2"),
                         {"ok": True, "index": 1, "match": "name"})

    def test_the_only_ios_capture_device_on_the_only_usb_iphone(self):
        devices = [{"id": "camcorder", "name": "DV", "model": "Camcorder"}, self.phone(OWN_ID, "iPhone 15")]
        self.assertEqual(self.match(devices, "--udid", MINE, "--name", "iPhone 17 Pro", "--phones", "1"),
                         {"ok": True, "index": 1, "match": "only"})
        self.assertEqual(self.match([self.phone(OWN_ID)]), {"ok": True, "index": 0, "match": "only"})

    def test_several_iphones_and_none_matches_is_refused_naming_the_phone(self):
        two = [self.phone(OWN_ID, "iPhone 16e"), self.phone("5C1D2E3F-AAAA-BBBB-CCCC-DDDDEEEEFFFF", "iPhone Air")]
        answer = self.match(two, "--udid", MINE, "--name", "iPhone 17 Pro", "--phones", "2")
        self.assertEqual((answer["ok"], answer["error"]), (False, "ambiguous_device"))
        self.assertIn("can't tell which one is “iPhone 17 Pro”", answer["message"])
        self.assertTrue(answer["message"].endswith("Unplug the others, then try again."))
        answer = self.match([self.phone(OWN_ID, "iPhone 16e")], "--udid", MINE, "--name", "iPhone 17 Pro",
                            "--phones", "2")
        self.assertEqual(answer["error"], "ambiguous_device")
        self.assertEqual(self.match([self.phone(OWN_ID, "iPhone 16e")], "--udid", MINE)["error"], "no_device")
        self.assertEqual(self.match(two)["error"], "ambiguous_device")
        self.assertEqual(self.match(two + [self.phone("X")], "--udid", MINE, "--name", "iPhone 17 Pro", "--phones",
                                    "3")["match"], "name")
        self.assertEqual(self.match([self.phone("A"), self.phone("B")], "--udid", MINE, "--name", "iPhone 17 Pro",
                                    "--phones", "2")["error"], "ambiguous_device")

    def test_no_capture_device_says_to_plug_it_in(self):
        answer = self.match([], "--udid", MINE, "--name", "iPhone 17 Pro", "--phones", "1")
        self.assertEqual(answer["error"], "no_device")
        self.assertIn("“iPhone 17 Pro”", answer["message"])

    def classify(self, path):
        out = subprocess.run([str(self.binary), "classify", str(path)], capture_output=True, text=True, timeout=120)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_music_is_heard_as_music_and_merges_into_the_timeline(self):
        clip = self.folder / "music.wav"
        write_wav(clip, synthetic_music(8))
        answer = self.classify(clip)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([window["t"] for window in answer["soundWindows"]], [0, 1.5, 3, 4.5])
        self.assertTrue(all(window["labels"].get("music", 0) >= 0.5 for window in answer["soundWindows"]), answer)
        sounds = listen.sounds_of(answer["soundWindows"])
        self.assertEqual([(sound["label"], sound["start"], sound["end"]) for sound in sounds], [("music", 0.0, 7.5)])
        rows = watch.timeline([{"t": 0.0}], [["Now playing"]], [], sounds)
        self.assertRegex(watch.timeline_text(rows), r"^0:00 scene 1: on screen “Now playing”\n0:00-0:08 sound: "
                                                    r"music( with [a-z, ]+)? \(confidence 0\.\d\d\)$")

    def test_silence_is_no_sound_and_a_file_that_isnt_audio_says_why(self):
        quiet = self.folder / "quiet.wav"
        write_wav(quiet, [0.0] * 16_000 * 4)
        answer = self.classify(quiet)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(listen.sounds_of(answer["soundWindows"]), [])
        notes = self.folder / "notes.wav"
        notes.write_text("not audio")
        answer = self.classify(notes)
        self.assertEqual((answer["ok"], "soundWindows" in answer), (True, False))
        self.assertIn("couldn't read the audio", answer["soundsError"])
        self.assertEqual(self.classify(self.folder / "missing.wav")["error"], "classify_failed")


WORDS = {"ok": True, "text": "hello there", "segments": [{"start": 0, "end": 1, "text": "hello there"}],
         "engine": "speechAnalyzer", "locale": "en-US", "transcribeMs": 40}
SOUNDS = {"ok": True, "soundWindows": [{"t": 0, "d": 3, "labels": {"music": .9}}], "classifyMs": 20}


def run_with(recorded, calls):
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "audio":
            answer = recorded
        elif argv[1] == "transcribe":
            answer = WORDS
        else:
            answer = SOUNDS
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(answer) + "\n", stderr="")
    return run


class FakeProcess:
    def __init__(self, lines):
        self.stdout = io.StringIO("".join(json.dumps(line) + "\n" for line in lines))
        self.stdin = io.StringIO()
        self.code = None

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.code = 0
        return 0

    def kill(self):
        self.code = -9


class ListenMirroringTests(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.dict(os.environ, {mirroring.ENV_FLAG: "1", "MOBSTER_LISTEN": "on",
                                                 "MOBSTER_NO_DEVICES": ""}),
                   mock.patch.object(listen, "mirroring_helper", lambda: "/bin/mirroring"),
                   mock.patch.object(listen, "helper_path", lambda: "/bin/phone-audio")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_records_then_understands(self):
        calls = []
        result = listen.listen(5, run=run_with({"ok": True, "recordedSeconds": 5.0, "peakDb": -12.0,
                                                "file": "/tmp/x.caf"}, calls))
        self.assertEqual(result["text"], "hello there")
        self.assertTrue(result["sounds"])
        self.assertFalse(result["silent"])
        self.assertEqual([c[:2] for c in calls], [["/bin/mirroring", "audio"], ["/bin/phone-audio", "transcribe"],
                                                   ["/bin/phone-audio", "classify"]])

    def test_a_failed_recording_says_what_to_do(self):
        calls = []
        with self.assertRaises(listen.ListenError) as caught:
            listen.listen(5,
                          run=run_with({"ok": False, "error": "no_device", "message": "iPhone Mirroring isn't open."},
                                       calls))
        self.assertEqual(caught.exception.code, "no_device")
        self.assertEqual(len(calls), 1)

    def test_a_named_usb_iphone_still_uses_the_cable(self):
        with mock.patch.object(listen, "Session") as usb, mock.patch.object(listen, "MirroringSession") as mirrored:
            listen.session_for(5, udid="<UDID>")
            listen.session_for(5)
        usb.assert_called_once()
        mirrored.assert_called_once()

    def test_live_events_then_the_result(self):
        argvs = []

        def popen(argv, **kwargs):
            argvs.append(argv)
            return FakeProcess([{"event": "recording"}, {"event": "level", "t": .5, "db": -20},
                                {"ok": True, "recordedSeconds": 3.0, "peakDb": -10.0, "file": "/tmp/x.caf"}])
        session = listen.MirroringSession(10, popen=popen, run=run_with({}, []))
        try:
            result = session.result(timeout=5)
        finally:
            session.close()
        self.assertEqual(argvs[0][:2], ["/bin/mirroring", "audio"])
        self.assertIn("--live", argvs[0])
        self.assertEqual(result["text"], "hello there")
        self.assertEqual(result["seconds"], 3)


if __name__ == "__main__":
    unittest.main()
