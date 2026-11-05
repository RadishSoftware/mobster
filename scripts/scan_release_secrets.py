#!/usr/bin/env python3
"""Refuse a release that holds a secret: scan built artifacts, unpacked, before anything is published.

    python3 scripts/scan_release_secrets.py [--root CHECKOUT] PATH...

PATH is a folder (Mobster.app, dashboard/dist), a .dmg (attached read-only, never opened in Finder),
a .tar.gz/.tgz/.tar, a .zip/.mcpb, or any other file. Inside each one, zip and tar members, gzip
streams and zlib streams are unpacked too, which is where PyInstaller keeps the frozen Python
modules of the sidecar and the CLI. Every binary is reduced to its printable runs, so a key inside
a Mach-O file or a .pyc is text the checks can read.

The checks, each must find nothing:

  names      no .env file (an .env.example is fine), .p8, AuthKey_*, .p12, .key, updater key,
             keychain, .vercel folder or secrets folder
  patterns   no provider key, token, private key block or JSON web token (scripts/check_public_tree.py's
             SECRETS, minus the documented placeholders and Stripe's publishable pk_ keys)
  values     none of the maintainer's own secret values (check_public_tree.secret_values: the env
             files of --root, .vercel/.env*, MOBSTER_SECRET_FILES and this shell's *KEY/*TOKEN variables)
  gitleaks   `gitleaks dir` over the unpacked text with the repository's .gitleaks.toml. Without
             gitleaks installed this check fails: brew install gitleaks

A finding is printed as its kind, where it is and a fingerprint (first 4 characters and a SHA-256
prefix), never the value. Exit status 0 when every check passes, 1 when any fails, 2 on bad input.
Standard library only.
"""

import argparse
import gzip
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

FORBIDDEN_NAME = re.compile(
    r"(?:^\.env(?!\.example$)(?:\..+)?$|\.p8$|^AuthKey_|\.p12$|\.key$|^mobster-updater|\.keychain(?:-db)?$|^\.kcpw$"
    r"|^bench\.env$)")
FORBIDDEN_DIR = re.compile(r"^(?:\.vercel|secrets|\.mobster-signing|\.tauri)$")
PRINTABLE_RUN = re.compile(rb"[\x20-\x7e\t]{8,}")
ZLIB_HEADER = re.compile(rb"\x78[\x01\x5e\x9c\xda]")
GZIP_HEADER = re.compile(rb"\x1f\x8b\x08")
MAX_DEPTH = 4
MAX_MEMBER = 512 << 20


def load_checker():
    spec = importlib.util.spec_from_file_location("check_public_tree", HERE / "check_public_tree.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Scan:
    def __init__(self, checker, needles, text_dir):
        self.checker = checker
        self.needles = needles
        self.needle_re = (re.compile(b"|".join(re.escape(v)
                                               for v, _ in sorted(needles.items(), key=lambda kv: -len(kv[0]))))
                          if needles else None)
        self.text_dir = text_dir
        self.views = {}  # text file in text_dir -> where it came from
        self.files = 0
        self.problems = {"names": [], "patterns": [], "values": []}

    def blob(self, data, where, depth=0):
        self.files += 1
        binary = b"\0" in data[:8192]
        text = b"\n".join(PRINTABLE_RUN.findall(data)) if binary else data
        self.check_text(text, where)
        view = self.text_dir / f"{len(self.views):06d}.txt"
        view.write_bytes(text)
        self.views[str(view)] = where
        if depth >= MAX_DEPTH:
            return
        if data[:4] == b"PK\x03\x04":
            if self.zip_members(data, where, depth):
                return
        if data[257:262] == b"ustar":
            if self.tar_members(data, where, depth):
                return
        for match in GZIP_HEADER.finditer(data):
            try:
                out = gzip.decompress(data[match.start():])
            except (OSError, EOFError, zlib.error):
                continue
            # A .tar.gz or .gz file is its stream: its members are named after the file itself.
            self.blob(out, where if match.start() == 0 else f"{where}@gzip:{match.start()}", depth + 1)
        if binary:
            for match in ZLIB_HEADER.finditer(data):
                try:
                    out = zlib.decompressobj().decompress(data[match.start():match.start() + MAX_MEMBER], MAX_MEMBER)
                except zlib.error:
                    continue
                if len(out) >= 16:  # a stream as short as one key still counts
                    self.blob(out, f"{where}@zlib:{match.start()}", depth + 1)

    def zip_members(self, data, where, depth):
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return False
        with archive:
            for info in archive.infolist():
                self.name(info.filename, f"{where}!{info.filename}")
                if not info.is_dir() and info.file_size <= MAX_MEMBER:
                    self.blob(archive.read(info), f"{where}!{info.filename}", depth + 1)
        return True

    def tar_members(self, data, where, depth):
        try:
            archive = tarfile.open(fileobj=io.BytesIO(data))
        except tarfile.TarError:
            return False
        with archive:
            for member in archive:
                self.name(member.name, f"{where}!{member.name}")
                if member.isfile() and member.size <= MAX_MEMBER:
                    self.blob(archive.extractfile(member).read(), f"{where}!{member.name}", depth + 1)
        return True

    def name(self, path, where):
        parts = [p for p in Path(path).parts if p not in ("/", ".")]
        if parts and FORBIDDEN_NAME.search(parts[-1]):
            self.problems["names"].append(f"{where}: a key, env or keychain file by its name")
        for part in parts[:-1]:
            if FORBIDDEN_DIR.match(part):
                self.problems["names"].append(f"{where}: inside a {part} folder")
                break

    def check_text(self, text, where):
        decoded = text.decode("latin-1")
        for match in self.checker.SECRET_ANY.finditer(decoded):
            if any(word in match.group(0).lower() for word in self.checker.FAKE_KEY_WORDS):
                continue
            if match.group(0).startswith("pk_"):
                continue  # a Stripe publishable key ships in pages and apps by design
            kind = self.checker.SECRETS[int(match.lastgroup[1:])][0]
            self.problems["patterns"].append(f"{where}: {kind}: {self.checker.fingerprint(match.group(0))}")
        if self.needle_re:
            for match in self.needle_re.finditer(text):
                name, source = self.needles[match.group(0)]
                self.problems["values"].append(f"{where}: the value of {name} from {source}: "
                                               f"{self.checker.fingerprint(match.group(0))}")

    def path(self, path):
        if path.is_dir():
            for file in sorted(path.rglob("*")):
                rel = f"{path.name}/{file.relative_to(path).as_posix()}"
                if file.is_dir() and not file.is_symlink():
                    if FORBIDDEN_DIR.match(file.name):
                        self.problems["names"].append(f"{rel}: a {file.name} folder")
                    continue
                self.name(file.name, rel)
                if file.is_file() and not file.is_symlink():
                    self.blob(file.read_bytes(), rel)
        elif path.suffix == ".dmg":
            self.dmg(path)
        else:
            self.name(path.name, path.name)
            self.blob(path.read_bytes(), path.name)

    def dmg(self, path):
        mount = Path(tempfile.mkdtemp(prefix="mobster-scan-dmg."))
        attach = subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-noverify",
                                 "-mountpoint", str(mount), str(path)], capture_output=True, text=True)
        if attach.returncode:
            self.problems["names"].append(
                f"{path.name}: could not be attached to scan it: {attach.stderr.strip()[-200:]}")
            return
        try:
            for file in sorted(mount.rglob("*")):
                rel = f"{path.name}/{file.relative_to(mount).as_posix()}"
                if file.is_dir() and not file.is_symlink():
                    if FORBIDDEN_DIR.match(file.name):
                        self.problems["names"].append(f"{rel}: a {file.name} folder")
                    continue
                self.name(file.name, rel)
                if file.is_file() and not file.is_symlink():
                    self.blob(file.read_bytes(), rel)
        finally:
            subprocess.run(["hdiutil", "detach", "-quiet", str(mount)], capture_output=True)
            shutil.rmtree(mount, ignore_errors=True)


def gitleaks(scan, config):
    exe = shutil.which("gitleaks")
    if not exe:
        return ["gitleaks is not installed, so the release was not scanned with it: brew install gitleaks"]
    report = scan.text_dir.parent / "gitleaks.json"
    command = [exe, "dir", str(scan.text_dir), "--no-banner", "--redact", "--log-level", "error", "--exit-code", "1",
               "--report-format", "json", "--report-path", str(report)]
    if config and Path(config).is_file():
        command += ["--config", str(config)]
    done = subprocess.run(command, capture_output=True, text=True)
    try:
        findings = json.loads(report.read_text() or "[]")
    except (OSError, ValueError):
        findings = None
    if done.returncode == 0 and findings == []:
        return []
    if done.returncode != 1 or not findings:
        return [f"gitleaks did not finish (exit {done.returncode}): {(done.stderr or done.stdout).strip()[-300:]}"]
    return [f"{scan.views.get(f.get('File', ''), f.get('File'))}: {f.get('RuleID')} ({f.get('Description', '')})"
            for f in findings]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--root", default=str(REPO), help="the checkout whose env files hold the values to look for")
    parser.add_argument("--config", default=str(REPO / ".gitleaks.toml"), help="the gitleaks config")
    args = parser.parse_args(argv)
    missing = [str(p) for p in args.paths if not p.exists()]
    if missing:
        print(f"scan_release_secrets: no such file: {', '.join(missing)}", file=sys.stderr)
        return 2

    checker = load_checker()
    needles = {value: (name, source) for name, value, source in checker.secret_values(args.root)}
    scratch = Path(tempfile.mkdtemp(prefix="mobster-release-scan."))
    os.chmod(scratch, 0o700)
    try:
        text_dir = scratch / "text"
        text_dir.mkdir()
        scan = Scan(checker, needles, text_dir)
        for path in args.paths:
            scan.path(path.resolve())
        failed = 0
        results = list(scan.problems.items()) + [("gitleaks", gitleaks(scan, args.config))]
        for check, problems in results:
            problems = list(dict.fromkeys(problems))
            failed += bool(problems)
            print(f"{'FAIL' if problems else 'ok  '} {check:<9}")
            for problem in problems[:40]:
                print(f"       {problem}")
            if len(problems) > 40:
                print(f"       ... and {len(problems) - 40} more")
        print(f"scanned {scan.files} files, members and streams in {', '.join(p.name for p in args.paths)}; "
              f"{len(needles)} known secret values compared")
        print("PASS" if not failed else f"FAILED: {failed} check(s). Nothing may be published from these files.")
        return 1 if failed else 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
