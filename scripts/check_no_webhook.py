#!/usr/bin/env python3
"""Prove no Discord webhook URL ships: in the repository's tracked files, or in a build.

    python3 scripts/check_no_webhook.py                 # tracked files, plus every build output present
    python3 scripts/check_no_webhook.py PATH...         # those files, folders, .app bundles, archives or .dmg files

In-app feedback goes to Discord only through the license API, whose webhook URL is a server secret
(DISCORD_FEEDBACK_WEBHOOK_URL, services/license-api/src/feedback.ts). So nothing Mobster builds may hold one, real or
example: the dashboard (dashboard/dist), the sidecar (desktop/src-tauri/binaries, a PyInstaller build whose modules are
zlib streams inside a binary) and the app's resources (Mobster.app). This looks for the path itself, `discord.com` or
`discordapp.com` then `/api/webhooks` (with or without a version, escaped slashes too), so a placeholder fails as surely
as a live URL. Inside each file it opens zip members, gzip streams and the zlib streams of a binary; a .dmg is attached
read-only.

A finding is printed as where it is and a fingerprint (first 4 characters and a SHA-256 prefix), never the value.
Exit status 0 when nothing is found, 1 when anything is, 2 on bad input. Standard library only.
"""

import gzip
import hashlib
import io
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NEEDLE = re.compile(rb"discord(?:app)?\.com\\?/api\\?/(?:v\d+\\?/)?webhooks[^\s\"'<>)]*", re.I)
ZLIB_HEADER = re.compile(rb"\x78[\x01\x5e\x9c\xda]")
MAX_DEPTH = 4
MAX_MEMBER = 512 << 20
# Build outputs, when they exist: the dashboard the app embeds, the frozen sidecar, and the bundled app.
BUILDS = ("dashboard/dist", "desktop/src-tauri/binaries", "desktop/src-tauri/target/release/bundle/macos/Mobster.app",
          "desktop/src-tauri/target/debug/bundle/macos/Mobster.app")


def fingerprint(value):
    return f"{value[:4].decode('latin-1')}… sha256:{hashlib.sha256(value).hexdigest()[:12]}"


class Finder:
    def __init__(self):
        self.found = []
        self.files = 0

    def blob(self, data, where, depth=0):
        self.files += 1
        for match in NEEDLE.finditer(data):
            self.found.append(f"{where}: a Discord webhook URL ({fingerprint(match.group(0))})")
        if depth >= MAX_DEPTH:
            return
        if data[:4] == b"PK\x03\x04":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    for info in archive.infolist():
                        if not info.is_dir() and info.file_size <= MAX_MEMBER:
                            self.blob(archive.read(info), f"{where}!{info.filename}", depth + 1)
                return
            except zipfile.BadZipFile:
                pass
        if data[:3] == b"\x1f\x8b\x08":
            try:
                self.blob(gzip.decompress(data), f"{where}@gzip", depth + 1)
            except (OSError, EOFError, zlib.error):
                pass
        if b"\0" in data[:8192]:
            for match in ZLIB_HEADER.finditer(data):
                try:
                    out = zlib.decompressobj().decompress(data[match.start():match.start() + MAX_MEMBER], MAX_MEMBER)
                except zlib.error:
                    continue
                if len(out) >= 16:
                    self.blob(out, f"{where}@zlib:{match.start()}", depth + 1)

    def path(self, path, label=None):
        label = label or path.name
        if path.is_dir():
            for file in sorted(path.rglob("*")):
                if file.is_file() and not file.is_symlink():
                    self.blob(file.read_bytes(), f"{label}/{file.relative_to(path).as_posix()}")
        elif path.suffix == ".dmg":
            mount = Path(tempfile.mkdtemp(prefix="mobster-webhook-dmg."))
            attach = subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-noverify",
                                     "-mountpoint", str(mount), str(path)], capture_output=True, text=True)
            if attach.returncode:
                self.found.append(f"{label}: could not be attached to check it")
                return
            try:
                self.path(mount, label)
            finally:
                subprocess.run(["hdiutil", "detach", "-quiet", str(mount)], capture_output=True)
                shutil.rmtree(mount, ignore_errors=True)
        elif path.is_file():
            self.blob(path.read_bytes(), label)


def tracked(root=REPO):
    """The repository's tracked files (the working tree's copies), as paths."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [root / name for name in out.decode().split("\0") if name and (root / name).is_file()]


def builds(root=REPO):
    return [root / rel for rel in BUILDS if (root / rel).exists()]


def scan_repo(root=REPO):
    """(findings, files read) over the tracked files and every build present."""
    finder = Finder()
    for file in tracked(root):
        finder.blob(file.read_bytes(), file.relative_to(root).as_posix(), depth=MAX_DEPTH)  # source: as written
    for build in builds(root):
        finder.path(build, build.relative_to(root).as_posix())
    return finder.found, finder.files


def main(argv):
    if argv:
        missing = [p for p in argv if not Path(p).exists()]
        if missing:
            print(f"check_no_webhook: no such file: {', '.join(missing)}", file=sys.stderr)
            return 2
        finder = Finder()
        for p in argv:
            finder.path(Path(p), p.rstrip("/"))
        found, files = finder.found, finder.files
    else:
        found, files = scan_repo()
    for line in found:
        print(f"check_no_webhook: {line}", file=sys.stderr)
    if found:
        print("check_no_webhook: a Discord webhook URL is in the source or the build. Feedback's webhook lives only in "
              "the license API's environment; remove it from here.", file=sys.stderr)
        return 1
    print(f"check_no_webhook: no Discord webhook URL in {files} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
