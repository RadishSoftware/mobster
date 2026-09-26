#!/usr/bin/env bash
# Keep WebDriverAgent serving a USB iPhone on localhost:8100, restarting the
# runner and the iproxy relay whenever either exits. Prints the session id once
# WDA is ready. Setup (build-for-testing, signing) is in mobile_agent/docs/usb-wda.md.
#
#   UDID=<device> TEAM=<team id> WDA_DIR=/path/to/WebDriverAgent scripts/usb-wda.sh
set -u
iproxy -h 2>&1 | grep -q -- --source || { echo "iproxy is too old to bind 127.0.0.1 only: brew upgrade libimobiledevice libusbmuxd" >&2; exit 1; }

WDA_DIR=${WDA_DIR:-/tmp/mobster-wda}
DERIVED=${WDA_DERIVED_DATA:-/tmp/mobster-wda-dd}
PORT=${WDA_PORT:-8100}
LOG_DIR=${WDA_LOG_DIR:-${TMPDIR:-/tmp}/mobster-wda-logs}
: "${TEAM:?Set TEAM to your 10-character Apple team id}"
if [ -z "${UDID:-}" ]; then
  # Only physical devices: simulator-style 0000FE01 identifiers are skipped.
  UDID=$(idevice_id -l | sort -u | grep -v '^0000FE01' | head -1)
fi
: "${UDID:?No USB iPhone found; set UDID}"
mkdir -p "$LOG_DIR"

runner() {
  xcodebuild -project "$WDA_DIR/WebDriverAgent.xcodeproj" -scheme WebDriverAgentRunner \
    -destination "id=$UDID" -derivedDataPath "$DERIVED" "DEVELOPMENT_TEAM=$TEAM" \
    USE_IP=127.0.0.1 test-without-building >>"$LOG_DIR/runner.log" 2>&1
}

relay() {
  # 8100: WebDriverAgent API; 9100: its MJPEG video stream. -s: this Mac only
  # (without it iproxy 2.1.1 listens on every interface, i.e. the LAN).
  iproxy -s 127.0.0.1 -u "$UDID" "$PORT:8100" "9100:9100" >>"$LOG_DIR/iproxy.log" 2>&1
}

supervise() {  # name, function
  while true; do
    "$2"
    echo "$(date '+%F %T') $1 exited ($?); restarting in 2 s" >>"$LOG_DIR/supervisor.log"
    sleep 2
  done
}

trap 'kill 0' EXIT INT TERM
supervise runner runner &
supervise iproxy relay &

for _ in $(seq 1 120); do
  status=$(curl -s -m 2 "http://127.0.0.1:$PORT/status" || true)
  if printf '%s' "$status" | grep -q '"ready" : true'; then
    session=$(printf '%s' "$status" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("sessionId") or "")')
    if [ -z "$session" ]; then
      session=$(curl -s -X POST "http://127.0.0.1:$PORT/session" -H 'Content-Type: application/json' \
        -d '{"capabilities":{"alwaysMatch":{"platformName":"iOS"}}}' |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["sessionId"])')
    fi
    echo "WDA ready on http://127.0.0.1:$PORT  device=$UDID  session=$session"
    break
  fi
  sleep 1
done
wait
