"""Record one phone's traffic to a pcapng file with ``mobster-pcap`` (tool.py), run as a separate process."""

import re
import signal
import subprocess
from pathlib import Path

MAX_MB = 200
MIN_SECONDS, MAX_SECONDS = 5, 3600
# Exit 5 prints its own sentence: over Wi-Fi it refuses a capture service that isn't encrypted.
TOOL_EXITS = {3: "No iPhone with that UDID is reachable. Plug it in, or turn on Wi-Fi for it.",
              4: "The iPhone doesn't trust this Mac. Unlock it and tap Trust.",
              5: "The iPhone refused the packet capture service. Unlock it and try again.",
              6: "mobster-pcap couldn't write the capture file.", 8: "The connection to the iPhone broke."}
UDID = re.compile(r"[0-9A-Fa-f][0-9A-Fa-f-]{19,44}")
STOP_WAIT = 10


class CaptureError(Exception):
    pass


def check(udid, seconds, out):
    """Refuse (CaptureError) before anything runs: a UDID that isn't one, a length outside 5 s to an hour, a file that
    exists or isn't .pcapng."""
    if not UDID.fullmatch(udid or ""):
        raise CaptureError("That isn't a device UDID. Find your phone's with `mobster devices`.")
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise CaptureError(f"Capture for {MIN_SECONDS} to {MAX_SECONDS} seconds.")
    out = Path(out)
    if out.suffix != ".pcapng":
        raise CaptureError("Name the output FILE.pcapng.")
    if out.exists():
        raise CaptureError(f"{out} exists. Network X-ray never overwrites a capture: name a new file.")
    return out


def sentence(line):
    line = " ".join(str(line).split())[:200]
    return (line[:1].upper() + line[1:] + ("." if line and line[-1] not in ".!?" else "")) if line else ""


class Recording:
    """A running mobster-pcap. The tool stops itself 5 s after the capture length, whatever happens here."""

    def __init__(self, udid, seconds, out, *, network=False, locate=None, popen=subprocess.Popen):
        self.out = check(udid, seconds, out)
        if locate is None:
            from . import tool
            locate = tool.locate
        try:
            binary = locate()
        except LookupError as error:
            raise CaptureError(str(error)) from None
        command = [binary, "--udid", udid, "--seconds", str(seconds + 5), "--max-mb", str(MAX_MB), "--out",
                   str(self.out)]
        if network:
            command.insert(3, "--network")
        self.command = command
        self.process = popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)

    def running(self):
        return self.process.poll() is None

    def stop(self, wait=STOP_WAIT):
        """Ask the tool to flush and exit; kill it if it hasn't in ``wait`` seconds. Safe to call again."""
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def finish(self):
        """Wait for the tool to exit; the size of the capture in bytes. CaptureError when it failed or wrote nothing."""
        self.process.wait()
        code = self.process.returncode
        if code not in (0, -signal.SIGTERM, -signal.SIGKILL):
            line = ""
            if self.process.stderr is not None:
                line = sentence(self.process.stderr.read(2000).decode("utf-8", "replace"))
            raise CaptureError((line if code == 5 and line else TOOL_EXITS.get(code)) or line
                               or f"mobster-pcap exited with {code}.")
        if not self.out.exists() or self.out.stat().st_size == 0:
            raise CaptureError("mobster-pcap wrote no capture. Check the phone is unlocked once and trusts this Mac, "
                               "then try again.")
        return self.out.stat().st_size
