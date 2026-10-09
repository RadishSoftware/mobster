#!/bin/bash
# Build the standalone `mobster` command for Apple silicon Macs.
#
# Output, in dist/ at the repository root (gitignored):
#   mobster-macos-arm64.tar.gz          mobster-macos-arm64/{mobster, _internal/, LICENSE, THIRD_PARTY_NOTICES.md, VERSION}
#   mobster-macos-arm64.tar.gz.sha256   `shasum -a 256` of the tarball, checked by install.sh
#   VERSION                             the version inside, from mobile_agent/__init__.py
#
# It is frozen like the desktop sidecar (desktop/scripts/build-sidecar.sh): the same pinned
# PyInstaller, the same entry, one-dir so it starts in place. Unlike the sidecar it keeps
# the terminal UI and has no app bundle around it.
#
# The build venv is $MOBSTER_BUILD_VENV, else mobile_agent/.venv. Release builds use uv's
# CPython 3.12.13 (`uv venv --seed --python 3.12.13 <venv>`), which links the standard library's
# extension modules into libpython: 29 Mach-O files, about 21 MB packed and 40 MB unpacked. Other
# Pythons work but lay the folder out differently; see the release runbook. Every Mach-O must load
# on macOS $MIN_MACOS, and the check below fails the build otherwise.
#
# Signing: with APPLE_SIGNING_IDENTITY (a "Developer ID Application: …" identity in the
# keychain), every Mach-O is signed with the hardened runtime and a secure timestamp.
# Without it, everything is signed ad hoc, which runs on this Mac and installs through
# brew or install.sh, since neither quarantines the download.
# Notarization: with APPLE_API_ISSUER, APPLE_API_KEY_PATH (the .p8 file) and the key id in
# APPLE_API_KEY_ID or APPLE_API_KEY (the names desktop/scripts/ci-signing.sh exports), through
# notarize.sh, which fails the build unless Apple's status is Accepted.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="${MOBSTER_BUILD_VENV:-$ROOT/mobile_agent/.venv}"
PYTHON="$VENV/bin/python"
NAME="mobster-macos-arm64"
DIST="$ROOT/dist"
WORK="$ROOT/build/mobster-cli"
STAGE="$DIST/$NAME"
TARBALL="$DIST/$NAME.tar.gz"
MIN_MACOS="13.0"
ENTITLEMENTS="$ROOT/desktop/src-tauri/entitlements.plist"

say() { printf 'build: %s\n' "$*" >&2; }
fail() { printf 'build: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || fail "build on an Apple silicon Mac; the binary is arm64 only"
[ -x "$PYTHON" ] || fail "missing $PYTHON. Create the venv with a Python 3.12 for old macOS releases, e.g. \`uv venv --seed --python 3.12.13 $VENV\`, or set MOBSTER_BUILD_VENV"
VERSION="$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$ROOT/mobile_agent/__init__.py")"
[ -n "$VERSION" ] || fail "no __version__ in mobile_agent/__init__.py"

say "mobster $VERSION with $("$PYTHON" -V 2>&1) from $VENV"
"$PYTHON" -m pip install -q -r "$ROOT/mobile_agent/requirements.txt" -r "$ROOT/desktop/scripts/sidecar-requirements.txt"
# PyInstaller only warns about a hidden import it can't find, so a module missing from the venv
# would leave the binary without it. Check files are YAML: without PyYAML, `verify --check` fails.
"$PYTHON" -c 'import yaml' 2>/dev/null \
  || fail "PyYAML isn't installed in $VENV. mobile_agent/requirements.txt must list it (PyYAML==6.0.3)"

rm -rf "$WORK" "$STAGE" "$TARBALL" "$TARBALL.sha256" "$DIST/VERSION"
mkdir -p "$WORK" "$DIST"
cd "$ROOT"
"$PYTHON" -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath "$WORK/dist" --workpath "$WORK/build" packaging/cli/mobster.spec

# The folder the tarball holds.
ditto "$WORK/dist/mobster" "$STAGE"
cp "$ROOT/LICENSE" "$ROOT/THIRD_PARTY_NOTICES.md" "$STAGE/"
printf '%s\n' "$VERSION" > "$STAGE/VERSION"

# Every Mach-O, deepest first, the bootloader last.
machos() {
  find "$STAGE/_internal" -type f -print0 | while IFS= read -r -d '' file; do
    case "$(file -b "$file")" in *Mach-O*) printf '%s\n' "$file" ;; esac
  done | awk '{ print gsub("/", "/"), $0 }' | sort -rn | cut -d' ' -f2-
  printf '%s\n' "$STAGE/mobster"
}
if [ -n "${APPLE_SIGNING_IDENTITY:-}" ]; then
  say "signing with $APPLE_SIGNING_IDENTITY (hardened runtime, timestamp)"
  sign() { codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" --sign "$APPLE_SIGNING_IDENTITY" "$1"; }
  SIGNING="Developer ID"
else
  say "no APPLE_SIGNING_IDENTITY: signing ad hoc"
  sign() { codesign --force --sign - "$1"; }
  SIGNING="ad hoc"
fi
count=0
while IFS= read -r file; do
  sign "$file" 2>/dev/null || { sign "$file"; exit 1; }
  count=$((count + 1))
done < <(machos)
codesign --verify --strict --verbose=1 "$STAGE/mobster"
say "signed $count Mach-O files ($SIGNING)"

# A Mach-O built for a newer macOS than MIN_MACOS fails to load there.
too_new=""
while IFS= read -r file; do
  minos="$(vtool -show-build "$file" 2>/dev/null | awk '$1 == "minos" { print $2; exit }')"
  if [ -n "$minos" ] && [ "$(printf '%s\n%s\n' "$minos" "$MIN_MACOS" | sort -V | tail -1)" != "$MIN_MACOS" ]; then
    too_new="$too_new ${file#"$STAGE/"} (minos $minos)"
  fi
done < <(machos)
[ -z "$too_new" ] || fail "built for a macOS newer than $MIN_MACOS:$too_new"

# Notarize the folder. A bare Mach-O can't be stapled; Gatekeeper checks it online.
KEY_ID="${APPLE_API_KEY_ID:-${APPLE_API_KEY:-}}"
if [ -n "${APPLE_API_ISSUER:-}" ] && [ -n "$KEY_ID" ] && [ -n "${APPLE_API_KEY_PATH:-}" ]; then
  [ "$SIGNING" = "Developer ID" ] || fail "notarization needs APPLE_SIGNING_IDENTITY"
  zip="$WORK/$NAME-notarize.zip"
  ditto -c -k --keepParent "$STAGE" "$zip"
  say "notarizing (this waits for Apple)"
  # notarytool submit --wait can exit 0 on a rejected submission: notarize.sh reads the JSON status,
  # prints `notarytool log <id>` unless it is Accepted, and fails.
  "$ROOT/packaging/cli/notarize.sh" "$zip" || fail "notarization wasn't Accepted; nothing was packed"
  NOTARIZED=yes
else
  NOTARIZED=no
fi

# Smoke: the frozen command answers each of its entry points. Its data goes to a temporary
# folder, so the smoke never touches this Mac's Mobster simulators or settings.
SMOKE="$(mktemp -d)"
trap 'rm -rf "$SMOKE"' EXIT
export MOBSTER_DATA_DIR="$SMOKE/data" MOBSTER_RUNS_DIR="$SMOKE/runs"
BIN="$STAGE/mobster"
say "smoke: version --json"
"$BIN" version --json > "$SMOKE/version.json"
"$PYTHON" - "$SMOKE/version.json" "$VERSION" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
found = data.get("version") or data.get("mobster")
assert found == sys.argv[2], f"version --json says {found!r}, expected {sys.argv[2]!r}"
PY
say "smoke: verify --help"
"$BIN" verify --help > "$SMOKE/verify-help.txt"
grep -q -- "--check" "$SMOKE/verify-help.txt" || fail "verify --help has no --check: is mobile_agent.verify in this build?"
say "smoke: verify --check reads YAML"
# A check with an unknown key is a usage error (exit 3), found while parsing, before any simulator.
printf 'version: 1\nname: Build smoke\napp: {bundle: com.example.smoke}\nexpect:\n  - text: Smoke\nnot_a_key: 1\n' \
  > "$SMOKE/check.yaml"
status=0
"$BIN" verify --check "$SMOKE/check.yaml" --json > "$SMOKE/check-result.json" 2> "$SMOKE/check-stderr.txt" || status=$?
[ "$status" -eq 3 ] || { cat "$SMOKE/check-stderr.txt" >&2; fail "verify --check on a bad YAML check exited $status, not 3"; }
"$PYTHON" - "$SMOKE/check-result.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))
assert result["verdict"] == "couldnt_run" and result["reason"]["class"] == "usage", result["reason"]
assert "not_a_key" in json.dumps(result["reason"]), f"the error doesn't name the unknown key: {result['reason']}"
PY
say "smoke: mcp initialize and tools/list"
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"build-smoke","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | (cd "$SMOKE" && "$BIN" mcp --keyless) > "$SMOKE/mcp.jsonl"
"$PYTHON" - "$SMOKE/mcp.jsonl" "$VERSION" <<'PY'
import json, sys
replies = {m.get("id"): m for m in map(json.loads, filter(str.strip, open(sys.argv[1])))}
info = replies[1]["result"]["serverInfo"]
assert info == {"name": "mobster", "version": sys.argv[2]}, info
tools = {tool["name"] for tool in replies[2]["result"]["tools"]}
missing = {"status", "verify_start", "screen", "tap", "verify_finish"} - tools
assert not missing, f"tools/list lacks {sorted(missing)}"
assert "verify" not in tools, "verify is listed with --keyless"
PY
say "smoke: --help, demo --json and the bundled ocr.swift"
"$BIN" --help > "$SMOKE/help.txt"
grep -q "mobster verify" "$SMOKE/help.txt" || fail "--help doesn't list verify"
"$BIN" demo --json > "$SMOKE/demo.jsonl" || fail "demo --json failed"
"$PYTHON" - "$SMOKE/demo.jsonl" <<'PY'
import json, sys
lines = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
assert lines[0].get("event") == "start", f"demo's first line is {lines[0]}"
assert lines[-1].get("event") == "result", f"demo's last line is {lines[-1]}"
PY
[ -f "$STAGE/_internal/mobile_agent/ocr.swift" ] || fail "ocr.swift isn't in the bundle, so build-ocr can't work"
say "smoke: the terminal UI starts in a pty and leaves on ctrl+d"
"$PYTHON" - "$BIN" <<'PY'
import os, pty, select, sys, time
pid, fd = pty.fork()
if pid == 0:
    os.environ.update(TERM="xterm-256color", COLUMNS="100", LINES="30")
    os.execv(sys.argv[1], [sys.argv[1], "--demo"])
seen, end = b"", time.time() + 60
while time.time() < end and b"?1049h" not in seen:
    if select.select([fd], [], [], .1)[0]:
        seen += os.read(fd, 65536)
assert b"?1049h" in seen, "the terminal UI didn't start"
time.sleep(1)
os.write(fd, b"\x04")  # ctrl+d
end = time.time() + 30
while time.time() < end:
    done, status = os.waitpid(pid, os.WNOHANG)
    if done:
        break
    if select.select([fd], [], [], .1)[0]:
        try:
            os.read(fd, 65536)
        except OSError:
            pass
else:
    os.kill(pid, 9)
    sys.exit("the terminal UI didn't leave on ctrl+d")
assert os.waitstatus_to_exitcode(status) == 0, f"the terminal UI exited {os.waitstatus_to_exitcode(status)}"
PY
say "smoke: sim doctor --json"
status=0
"$BIN" sim doctor --json > "$SMOKE/doctor.json" || status=$?
[ "$status" -le 1 ] || fail "sim doctor exited $status"
"$PYTHON" -c 'import json, sys; checks = json.load(open(sys.argv[1])); assert isinstance(checks, (list, dict)) and checks' "$SMOKE/doctor.json"

# The tarball and its checksum, written from inside dist/ so `shasum -c` works beside them.
(cd "$DIST" && COPYFILE_DISABLE=1 tar -czf "$NAME.tar.gz" "$NAME" && shasum -a 256 "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
printf '%s\n' "$VERSION" > "$DIST/VERSION"
size="$(du -sh "$STAGE" | cut -f1)"
tar_size="$(du -h "$TARBALL" | cut -f1)"
say "done: $TARBALL ($tar_size; $size unpacked), $count Mach-O files, signed $SIGNING, notarized $NOTARIZED"
# Who signed it, as codesign reports it: Authority lines and TeamIdentifier for Developer ID,
# Signature=adhoc and TeamIdentifier=not set for an ad hoc build.
codesign -dv --verbose=2 "$STAGE/mobster" 2>&1 | grep -E '^(Authority|TeamIdentifier|Signature)=' | sed 's/^/build: /' >&2
cat "$TARBALL.sha256"
