"""Sound as a verification signal (`expect: [{sound: sent}]`, experimental): matched in code, with no model.

A fingerprint is a short recording of one sound (a sent chime, an error tock) reduced to the shape of its spectrum over
time: 16 frequency bands per 64 ms frame, as shares of the frame's energy, plus each frame's loudness against the
loudest. It is recorded once per app under ``.mobster/sounds/<bundle>/<name>.json`` and matched against what the iPhone
played during a run. A match is a pass (the sound did happen) and the screen read follows as usual; a sound that isn't
heard fails the assertion and says how close the best window came. Hearing needs a real iPhone (listen.py): a
simulator's sound doesn't leave the Mac, so a sound assertion on a simulator is not met.
"""

import cmath
import json
import math
import operator
import os
import re
import threading
import wave
from array import array
from pathlib import Path

from .assertions import AssertionResult

__all__ = ["SETTING", "NAME", "enabled", "fingerprint_wav", "match_wav", "save", "load", "names", "Watch"]

SETTING = "MOBSTER_EXPERIMENTAL_SOUND"
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
FRAME = 1024
HOP = 512
BANDS = 16
LOW_HZ, HIGH_HZ = 250.0, 7000.0
TARGET_RATE = 16000
ACTIVE_DB = -35.0  # a reference keeps the frames within this many dB of its loudest
SILENT_DBFS = -55.0  # a window whose loudest frame is quieter than this heard nothing
MATCH_SCORE = 0.92
MAX_REFERENCE_SECONDS = 5
MAX_RECORDING_SECONDS = 120
WINDOW_SECONDS = 20
VERSION = 1


class SoundError(ValueError):
    """What is wrong with a sound file or fingerprint, and what to do."""


def enabled():
    return os.environ.get(SETTING, "").strip().lower() in ("1", "on", "true", "yes")


def _samples(path):
    """(mono float samples in -1..1 decimated to about 16 kHz, the rate after decimation) from a 16-bit PCM wav."""
    try:
        with wave.open(str(path), "rb") as handle:
            channels, width, rate, count = (
                handle.getnchannels(),
                handle.getsampwidth(),
                handle.getframerate(),
                handle.getnframes(),
            )
            if width != 2:
                raise SoundError(f"{Path(path).name} is not 16-bit PCM. Record it as 16-bit WAV.")
            if count > rate * MAX_RECORDING_SECONDS:
                raise SoundError(f"{Path(path).name} is longer than {MAX_RECORDING_SECONDS} s. Use a shorter one.")
            raw = array("h")
            raw.frombytes(handle.readframes(count))
    except (wave.Error, EOFError, OSError) as error:
        raise SoundError(f"{Path(path).name} can't be read as a WAV ({error}). Record it again.") from None
    if raw.itemsize != 2:
        raise SoundError("this platform's 16-bit array is not 2 bytes")
    if channels > 1:
        raw = array("h", (sum(raw[i : i + channels]) // channels for i in range(0, len(raw) - channels + 1, channels)))
    step = max(1, rate // TARGET_RATE)
    mono = [sum(raw[i : i + step]) / (step * 32768.0) for i in range(0, len(raw) - step + 1, step)]
    return mono, rate / step


def _fft(values):
    """The discrete Fourier transform of ``values`` (a power-of-two length), by the iterative radix-2 method."""
    n = len(values)
    out = [values[int(format(i, f"0{n.bit_length() - 1}b")[::-1], 2)] for i in range(n)]
    size = 2
    while size <= n:
        half = size // 2
        step = [cmath.exp(-2j * math.pi * k / size) for k in range(half)]
        for start in range(0, n, size):
            for k in range(half):
                odd = out[start + k + half] * step[k]
                out[start + k + half] = out[start + k] - odd
                out[start + k] = out[start + k] + odd
        size *= 2
    return out


def _band_of_bins(rate):
    """For each bin up to Nyquist, the band it belongs to (None outside LOW_HZ..HIGH_HZ); bands are log-spaced."""
    edges = [LOW_HZ * (HIGH_HZ / LOW_HZ) ** (b / BANDS) for b in range(BANDS + 1)]
    out = []
    for k in range(FRAME // 2):
        hz = k * rate / FRAME
        out.append(next((b for b in range(BANDS) if edges[b] <= hz < edges[b + 1]), None))
    return out


def _frames(samples, rate):
    """[(band shares, dBFS of the frame)] for every frame of ``samples``."""
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * n / (FRAME - 1)) for n in range(FRAME)]
    bins = _band_of_bins(rate)
    out = []
    for start in range(0, len(samples) - FRAME + 1, HOP):
        spectrum = _fft([x * w for x, w in zip(samples[start : start + FRAME], hann)])
        power = [0.0] * BANDS
        for k, band in enumerate(bins):
            if band is not None:
                power[band] += abs(spectrum[k]) ** 2
        total = sum(power)
        db = 10 * math.log10(total * 2 / sum(hann) ** 2 + 1e-12)
        out.append(([p / total for p in power] if total > 1e-12 else [0.0] * BANDS, db))
    return out


def fingerprint_wav(path):
    """The fingerprint (a JSON-able dict) of the sound in the WAV at ``path``, trimmed to its audible part."""
    samples, rate = _samples(path)
    if len(samples) > rate * MAX_REFERENCE_SECONDS:
        raise SoundError(
            f"{Path(path).name} is longer than {MAX_REFERENCE_SECONDS} s. A fingerprint is one short "
            "sound: cut it to the chime."
        )
    frames = _frames(samples, rate)
    if not frames:
        raise SoundError(f"{Path(path).name} is too short to fingerprint. Record at least a tenth of a second.")
    peak = max(db for _, db in frames)
    if peak < SILENT_DBFS:
        raise SoundError(f"{Path(path).name} is silent. Record the sound with the iPhone's volume up.")
    active = [i for i, (_, db) in enumerate(frames) if db >= peak + ACTIVE_DB]
    kept = frames[active[0] : active[-1] + 1]
    return {
        "version": VERSION,
        "frame": FRAME,
        "hop": HOP,
        "bands": BANDS,
        "seconds": round(len(kept) * HOP / rate, 2),
        "frames": [{"shares": [round(s, 4) for s in shares], "db": round(db - peak, 1)} for shares, db in kept],
    }


def _cosine(a, b):
    dot = sum(map(operator.mul, a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def best_score(fingerprint, recording_frames):
    """(best weighted similarity 0..1, the frame it starts at) of the fingerprint over the recording's frames; the
    window's loudest frame must clear SILENT_DBFS, so silence can't match."""
    ref = fingerprint["frames"]
    if fingerprint.get("version") != VERSION or not ref or not recording_frames:
        raise SoundError("this fingerprint is not one this version of Mobster reads. Record the sound again.")
    weights = [10 ** (frame["db"] / 20) for frame in ref]
    best, at = 0.0, 0
    for start in range(0, len(recording_frames) - len(ref) + 1):
        window = recording_frames[start : start + len(ref)]
        if max(db for _, db in window) < SILENT_DBFS:
            continue
        score = sum(w * _cosine(frame["shares"], shares) for w, frame, (shares, _) in zip(weights, ref, window)) / sum(
            weights
        )
        if score > best:
            best, at = score, start
    return best, at


def match_wav(fingerprint, path):
    """(heard, score, seconds into the recording) for ``fingerprint`` against the WAV at ``path``."""
    samples, rate = _samples(path)
    score, start = best_score(fingerprint, _frames(samples, rate))
    return score >= MATCH_SCORE, round(score, 2), round(start * HOP / rate, 1)


def folder(root, bundle):
    return Path(root) / ".mobster" / "sounds" / re.sub(r"[^A-Za-z0-9._-]", "_", bundle)


def save(root, bundle, name, wav):
    """Record the sound in ``wav`` as ``name`` for the app ``bundle`` under ``root``/.mobster/sounds; the path."""
    if not NAME.fullmatch(name):
        raise SoundError("a sound's name is lowercase letters, digits, - and _ (up to 40), such as sent")
    target = folder(root, bundle) / f"{name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fingerprint_wav(wav), separators=(",", ":")) + "\n")
    return target


def load(root, bundle, name):
    path = folder(root, bundle) / f"{name}.json"
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        have = ", ".join(names(root, bundle)) or "none"
        raise SoundError(
            f"no sound {name!r} is recorded for {bundle} (recorded: {have}). Record it with "
            "`python -m mobile_agent.verify.sound record`."
        ) from None
    except (OSError, ValueError):
        raise SoundError(f"{path} isn't a fingerprint. Record the sound again.") from None


def names(root, bundle):
    return sorted(p.stem for p in folder(root, bundle).glob("*.json"))


def evaluate(root, bundle, wanted, wav, *, why=None):
    """One AssertionResult per sound name in ``wanted`` against the recording ``wav`` (None when nothing was heard).
    Fails closed: a sound with no recording to check, or no fingerprint, is not met."""
    out = []
    for name in wanted:
        text = f"sound {name}"
        if wav is None:
            out.append(AssertionResult(False, text, "not checked: " + (why or "the iPhone's sound wasn't recorded")))
            continue
        try:
            heard, score, at = match_wav(load(root, bundle, name), wav)
        except SoundError as error:
            out.append(AssertionResult(False, text, str(error)))
            continue
        out.append(
            AssertionResult(
                heard,
                text,
                f"heard at {at} s (match {score})" if heard else f"not heard (best match {score}, needs {MATCH_SCORE})",
            )
        )
    return out


class Watch:
    """Records the iPhone's sound in a thread while a run's steps go; ``stop`` waits and gives the WAV, or None."""

    def __init__(self, capture, wav, seconds=WINDOW_SECONDS):
        self._capture, self.wav, self._seconds = capture, Path(wav), seconds
        self.error = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self._capture(self._seconds, keep=str(self.wav))
        except Exception as error:  # the run goes on; the assertions say the sound wasn't recorded
            self.error = error

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._thread.join(self._seconds + 180)
        return self.wav if self.wav.is_file() and self.error is None and not self._thread.is_alive() else None


class Signal:
    """A run's sound assertions: ``start`` after the app is up, ``results`` once the steps are done."""

    def __init__(self, wanted, run, *, capture=None):
        self.wanted, self._run, self._capture, self._watch, self._dir = wanted, run, capture, None, None
        self._why = None

    def start(self):
        from .smart import is_real_device

        if not enabled():
            self._why = f"sound checks are experimental. Set {SETTING}=1 to run them"
        elif not is_real_device(self._run._manager()):
            self._why = "a simulator's sound doesn't leave the Mac, so only a real iPhone can be heard"
        else:
            import tempfile

            capture = self._capture
            if capture is None:
                from ..listen import listen

                capture = lambda seconds, keep: listen(seconds, udid=self._run.target.udid, keep=keep)  # noqa: E731
            self._dir = tempfile.mkdtemp(prefix="mobster-sound-")
            os.chmod(self._dir, 0o700)
            self._watch = Watch(capture, Path(self._dir) / "heard.wav").start()

    def results(self):
        wav = self._watch.stop() if self._watch is not None else None
        if self._watch is not None and wav is None:
            self._why = "the iPhone's sound wasn't recorded" + (
                f" ({type(self._watch.error).__name__})" if self._watch.error else ""
            )
        run = self._run
        parent = run.runs_dir.parent
        root = (
            parent.parent
            if parent.name == ".mobster"
            else (Path(run.check.source).parent if run.check.source else Path.cwd())
        )
        try:
            return evaluate(root, run.app.bundle_id, self.wanted, wav, why=self._why)
        finally:
            if self._dir:
                import shutil

                shutil.rmtree(self._dir, ignore_errors=True)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m mobile_agent.verify.sound", description="record and list sound fingerprints"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record", help="fingerprint a short WAV of one sound")
    record.add_argument("--bundle", required=True)
    record.add_argument("--name", required=True)
    record.add_argument("--wav", required=True)
    record.add_argument("--root", default=".")
    ls = sub.add_parser("list", help="list the recorded sounds of an app")
    ls.add_argument("--bundle", required=True)
    ls.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            print(f"wrote {save(args.root, args.bundle, args.name, args.wav)}")
        else:
            print("\n".join(names(args.root, args.bundle)))
    except SoundError as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
