"""The camera roll over Apple's own encrypted tunnel to the iPhone, with no cable: pymobiledevice3's ``--native``.

macOS keeps a tunnel to every iPhone it pairs with for development (``remoted``), over the cable or the local network.
pymobiledevice3 reaches the iPhone's file service through it with ``--native``: no root, no Xcode, nothing on the
phone. Mobster runs it as a separate program that you install yourself (``pipx install pymobiledevice3``,
GPL-3.0-or-later), and never imports or bundles it. Without it this route says how to get it.

What it reaches is the camera roll's files (/DCIM), as the cable does: originals, newest first by their number
(IMG_0042 after IMG_0041, the way the iPhone names what it takes or receives). Each call opens the tunnel again,
about 7 seconds on the test device (iPhone 15 Pro, 14 Oct 2026, measured over the cable's tunnel).
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import PhoneIOError

TOOL = "pymobiledevice3"
NEEDS_TOOL = ("Mobster reaches the iPhone's camera roll without a cable through pymobiledevice3, which isn't on this "
              "Mac.", "Install it with `pipx install pymobiledevice3`, then try again.")
TIMEOUT_S = 90
# remoted hadn't brought the tunnel up yet (seen 2 times in 3 on 14 Oct while another tool used the phone).
TUNNEL_DOWN = re.compile(r"could not find remoted|RSD connection", re.I)
MEDIA = re.compile(r"^/DCIM/(\d{3})[A-Z_]{0,12}/([A-Za-z_]*?)(\d+)?(?:_\d+)?\."
                   r"(heic|heif|jpe?g|png|gif|mov|mp4|m4v|dng|webp)$",
                   re.I)


def tool():
    """pymobiledevice3 on PATH, in pipx's folder (``PIPX_BIN_DIR``, else ~/.local/bin) or in Homebrew's; None when it
    isn't installed. A copy inside a Python venv whose folder isn't on PATH isn't found."""
    pipx = os.environ.get("PIPX_BIN_DIR", "").strip() or "~/.local/bin"
    for candidate in (shutil.which(TOOL), os.path.join(os.path.expanduser(pipx), TOOL), "/opt/homebrew/bin/" + TOOL,
                      "/usr/local/bin/" + TOOL):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


class TunnelRoll:
    """The camera roll of the iPhone ``record`` (devices.py's shape) over Apple's tunnel."""

    def __init__(self, record, runner=None, program=None):
        self.record = record or {}
        udid = str(self.record.get("udid") or "")
        if self.record.get("kind") == "simulator" or not re.fullmatch(r"[0-9A-Fa-f-]{24,40}", udid):
            raise PhoneIOError(f"“{self.name}” isn't an iPhone Mobster knows.", "usage")
        self.udid, self._runner, self._program = udid, runner, program

    @property
    def name(self):
        return self.record.get("name") or self.record.get("id") or "the iPhone"

    def _run(self, *command, timeout=TIMEOUT_S):
        """Reads only (a listing, a copy to the Mac), so a tunnel that wasn't up yet is tried once more."""
        code, output = self._once(*command, timeout=timeout)
        if code != 0 and TUNNEL_DOWN.search(output or ""):
            code, output = self._once(*command, timeout=timeout)
        return code, output

    def _once(self, *command, timeout=TIMEOUT_S):
        program = self._program or tool()
        if not program and self._runner is None:
            raise PhoneIOError(NEEDS_TOOL[0], "tools", NEEDS_TOOL[1])
        argv = [program or TOOL, *command, "--native", "--udid", self.udid]
        if self._runner is not None:
            return self._runner(argv, timeout)
        from ..native_helpers import require_devices
        require_devices()
        env = dict(os.environ, NO_COLOR="1", TERM="dumb", COLUMNS="200")
        try:
            done = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout,
                                  stdin=subprocess.DEVNULL, env=env)
        except subprocess.TimeoutExpired:
            raise PhoneIOError(f"{self.name} didn't answer over the tunnel within {timeout} seconds.", "failed",
                               "Keep it unlocked and on the same network as this Mac, then try again.") from None
        except OSError as error:
            raise PhoneIOError(f"pymobiledevice3 couldn't run ({type(error).__name__}).", "tools") from None
        return done.returncode, (done.stdout or "") + (done.stderr or "")

    def media(self):
        """Every photo and video file under /DCIM: [remote paths], oldest first by folder and number."""
        code, output = self._run("afc", "ls", "/DCIM", "--recursive")
        if code != 0:
            raise self._explain(output)
        found = []
        for line in output.splitlines():
            path = line.strip()
            match = MEDIA.match(path)
            if match:
                found.append((int(match.group(1)), int(match.group(3) or 0), path))
        return [path for *_, path in sorted(found)]

    def pull(self, folder, count=1):
        """Copy the newest ``count`` files (1 to 20) into ``folder``."""
        from .files import land
        from .photos import MAX_PULL, PHOTO_TYPES, digest
        count = max(1, min(int(count or 1), MAX_PULL))
        wanted = list(reversed(self.media()))[:count]
        folder = Path(folder).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        out = []
        with tempfile.TemporaryDirectory(prefix="mobster-tunnel-", dir=folder) as scratch:
            for remote in wanted:
                name = remote.rsplit("/", 1)[-1]
                code, output = self._run("afc", "pull", remote, scratch + "/", timeout=TIMEOUT_S + 300)
                partial = Path(scratch) / name
                if code != 0 or not partial.is_file():
                    raise self._explain(output)
                target = land(partial, folder, name)
                out.append({"path": str(target), "name": target.name, "bytes": target.stat().st_size,
                            "kind": PHOTO_TYPES.get(name.rsplit(".", 1)[-1].lower(), "photo"),
                            "sha256": digest(target)})
        return out

    def _explain(self, output):
        text = " ".join((output or "").split())
        if re.search(r"NoDeviceConnected|not found|No device", text, re.I):
            return PhoneIOError(f"macOS has no tunnel to {self.name} right now.", "no_device",
                                "Pair it with this Mac once over the cable (Trust), keep it on the same Wi-Fi and "
                                "unlocked, then try again.")
        if re.search(r"PasswordProtected|locked|pair", text, re.I):
            return PhoneIOError(f"{self.name} is locked or doesn't trust this Mac.", "locked",
                                "Unlock it, then try again.")
        return PhoneIOError(f"The tunnel to {self.name} failed{': ' + text[-160:] if text else ''}.", "failed")
