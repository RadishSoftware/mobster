#!/bin/sh
# Notarize a zip with notarytool and succeed only when Apple's status is Accepted.
#
#   packaging/cli/notarize.sh dist/…/mobster-macos-arm64-notarize.zip
#
# Needs APPLE_API_ISSUER, APPLE_API_KEY_PATH (the .p8 file) and the key id in APPLE_API_KEY_ID or
# APPLE_API_KEY (the names desktop/scripts/ci-signing.sh exports). `notarytool submit --wait` can exit 0
# on a submission Apple rejected, so, like desktop/scripts/finish-release.sh, this reads the status
# from its JSON, prints `notarytool log <id>` when the status isn't Accepted, and exits 1.
# Exit codes: 0 Accepted, 1 anything else, 2 usage.
set -eu

zip="${1:-}"
key_id="${APPLE_API_KEY_ID:-${APPLE_API_KEY:-}}"
if [ -z "$zip" ] || [ ! -f "$zip" ]; then
  echo "notarize: give the zip to notarize" >&2
  exit 2
fi
if [ -z "${APPLE_API_ISSUER:-}" ] || [ -z "$key_id" ] || [ -z "${APPLE_API_KEY_PATH:-}" ]; then
  echo "notarize: set APPLE_API_ISSUER, APPLE_API_KEY_PATH and APPLE_API_KEY_ID (or APPLE_API_KEY)" >&2
  exit 2
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
# field NAME: a top-level string from notarytool's JSON, empty when there is none or it isn't JSON.
field() { plutil -extract "$1" raw -o - "$work/result.json" 2>/dev/null || true; }

xcrun notarytool submit "$zip" --key "$APPLE_API_KEY_PATH" --key-id "$key_id" --issuer "$APPLE_API_ISSUER" \
  --wait --output-format json > "$work/result.json" || true
cat "$work/result.json" >&2
status="$(field status)"
id="$(field id)"
if [ "$status" != Accepted ]; then
  if [ -n "$id" ]; then
    xcrun notarytool log "$id" --key "$APPLE_API_KEY_PATH" --key-id "$key_id" --issuer "$APPLE_API_ISSUER" >&2 || true
  fi
  echo "notarize: the submission${id:+ $id} ended with status '${status:-none}', not Accepted" >&2
  exit 1
fi
echo "notarize: Accepted, submission $id" >&2
