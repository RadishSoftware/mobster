import math
import os
import random
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from unittest import mock

from mobile_agent.verify import sound
from mobile_agent.verify.checks import CheckError, check_from_dict, check_to_dict

RATE = 16000


def write(path, samples):
    data = array("h", (max(-32767, min(32767, int(s * 32767))) for s in samples))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(data.tobytes())


def chime(seconds=0.6, gain=0.5):
    return [
        gain * math.exp(-5 * t / seconds) * (math.sin(2 * math.pi * 880 * t) + 0.6 * math.sin(2 * math.pi * 1320 * t))
        for t in (n / RATE for n in range(int(RATE * seconds)))
    ]


def tock(seconds=0.3, gain=0.5):
    return [
        gain * math.exp(-30 * t) * math.sin(2 * math.pi * 220 * t)
        for t in (n / RATE for n in range(int(RATE * seconds)))
    ]


def room(parts, seconds=6, noise=0.003):
    """A recording of ``seconds`` with each (start second, samples) mixed over faint noise."""
    rng = random.Random(7)
    out = [rng.uniform(-noise, noise) for _ in range(int(RATE * seconds))]
    for start, samples in parts:
        for i, value in enumerate(samples):
            out[int(start * RATE) + i] += value
    return out


class SoundTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        write(self.dir / "ref.wav", chime())
        self.print_ = sound.fingerprint_wav(self.dir / "ref.wav")

    def heard(self, samples):
        write(self.dir / "rec.wav", samples)
        return sound.match_wav(self.print_, self.dir / "rec.wav")

    def test_the_fingerprint_is_short_and_trimmed(self):
        self.assertEqual(self.print_["version"], 1)
        self.assertLessEqual(self.print_["seconds"], 0.7)
        self.assertEqual(len(self.print_["frames"][0]["shares"]), sound.BANDS)

    def test_the_chime_is_heard_at_its_time_and_at_another_volume(self):
        for gain in (0.5, 0.1):
            ok, score, at = self.heard(room([(2.0, chime(gain=gain))]))
            self.assertTrue(ok, score)
            self.assertAlmostEqual(at, 2.0, delta=0.15)

    def test_other_sounds_and_silence_are_not_the_chime(self):
        self.assertFalse(self.heard(room([(2.0, tock())]))[0])
        self.assertFalse(self.heard(room([], noise=0.002))[0])
        self.assertFalse(self.heard(room([], noise=0.3))[0])
        self.assertFalse(self.heard([0.0] * RATE * 4)[0])

    def test_refusals(self):
        write(self.dir / "quiet.wav", [0.0] * RATE)
        with self.assertRaisesRegex(sound.SoundError, "silent"):
            sound.fingerprint_wav(self.dir / "quiet.wav")
        write(self.dir / "long.wav", [0.1] * RATE * 8)
        with self.assertRaisesRegex(sound.SoundError, "longer"):
            sound.fingerprint_wav(self.dir / "long.wav")
        (self.dir / "bad.wav").write_text("nope")
        with self.assertRaisesRegex(sound.SoundError, "can't be read"):
            sound.fingerprint_wav(self.dir / "bad.wav")

    def test_save_load_and_evaluate_fail_closed(self):
        sound.save(self.dir, "com.example.app", "sent", self.dir / "ref.wav")
        self.assertEqual(sound.names(self.dir, "com.example.app"), ["sent"])
        write(self.dir / "rec.wav", room([(1.0, chime())]))
        (yes,) = sound.evaluate(self.dir, "com.example.app", ["sent"], self.dir / "rec.wav")
        self.assertTrue(yes.ok)
        self.assertIn("heard at", yes.observed)
        (missing,) = sound.evaluate(self.dir, "com.example.app", ["error"], self.dir / "rec.wav")
        self.assertFalse(missing.ok)
        self.assertIn("sent", missing.observed)
        (nothing,) = sound.evaluate(self.dir, "com.example.app", ["sent"], None, why="a simulator can't be heard")
        self.assertFalse(nothing.ok)
        self.assertIn("simulator", nothing.observed)
        with self.assertRaises(sound.SoundError):
            sound.save(self.dir, "x", "Bad Name", self.dir / "ref.wav")

    def test_a_watch_gives_the_recording_or_none(self):
        def capture(seconds, keep):
            write(keep, chime())

        wav = sound.Watch(capture, self.dir / "w.wav", 1).start().stop()
        self.assertEqual(wav, self.dir / "w.wav")

        def broken(seconds, keep):
            raise RuntimeError("no phone")

        watch = sound.Watch(broken, self.dir / "w2.wav", 1).start()
        self.assertIsNone(watch.stop())
        self.assertIsInstance(watch.error, RuntimeError)

    def test_enabled_only_by_the_flag(self):
        with mock.patch.dict(os.environ, {sound.SETTING: ""}):
            self.assertFalse(sound.enabled())
        with mock.patch.dict(os.environ, {sound.SETTING: "1"}):
            self.assertTrue(sound.enabled())


class SignalTests(unittest.TestCase):
    def run_of(self, root, real):
        manager = mock.Mock(real_device=real)
        return mock.Mock(
            _manager=lambda: manager,
            runs_dir=Path(root) / ".mobster" / "runs",
            app=mock.Mock(bundle_id="com.example.app"),
            target=mock.Mock(udid="<UDID>"),
            check=mock.Mock(source=None),
        )

    def test_a_phone_run_records_then_matches(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {sound.SETTING: "1"}):
            write(Path(root) / "ref.wav", chime())
            sound.save(root, "com.example.app", "sent", Path(root) / "ref.wav")
            signal = sound.Signal(
                ("sent",), self.run_of(root, True), capture=lambda seconds, keep: write(keep, room([(1.5, chime())]))
            )
            signal.start()
            (result,) = signal.results()
            self.assertTrue(result.ok, result.observed)

    def test_a_simulator_or_the_flag_off_fails_closed_without_capturing(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            for env, real, word in (({sound.SETTING: "1"}, False, "simulator"), ({sound.SETTING: ""}, True, "Set")):
                with mock.patch.dict(os.environ, env):
                    signal = sound.Signal(("sent",), self.run_of(root, real), capture=lambda s, keep: calls.append(1))
                    signal.start()
                    (result,) = signal.results()
                self.assertFalse(result.ok)
                self.assertIn(word, result.observed)
        self.assertEqual(calls, [])


class CheckFileTests(unittest.TestCase):
    BASE = {"name": "send", "app": {"path": "A.app"}, "steps": ["Send it"]}

    def test_sound_items_are_peeled_off_expect_and_round_trip(self):
        check = check_from_dict({**self.BASE, "expect": [{"text": "Sent"}, {"sound": "sent"}, {"sound": "sent"}]})
        self.assertEqual(check.sound, ("sent",))
        self.assertEqual([a.kind for a in check.expect], ["text"])
        self.assertEqual(check_to_dict(check)["expect"], [{"text": "Sent"}, {"sound": "sent"}])

    def test_a_bad_sound_item_is_refused(self):
        for item in ({"sound": "Sent!"}, {"sound": "sent", "text": "x"}, {"sound": 3}):
            with self.assertRaises(CheckError):
                check_from_dict({**self.BASE, "expect": [item]})


if __name__ == "__main__":
    unittest.main()
