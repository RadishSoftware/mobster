# USB iPhone via WebDriverAgent

Proven 2026-09-22 on a physical iPhone 15 Pro (iOS 26.0.1) over USB:
observe, app launch, and home key all dispatch through `mobile_agent`'s WDA
driver end to end. No device modification, no paid account required (free provisioning
works; a membership team was used here).

## One-time setup

Prerequisites: Xcode signed into an Apple ID (Settings → Accounts), iPhone in
Developer Mode (Settings → Privacy & Security), USB cable, libimobiledevice
(`brew install libimobiledevice` for `iproxy`).

```sh
git clone --depth 1 --branch v16.12.10 https://github.com/appium/WebDriverAgent.git wda  # the pin in mobile_agent/wda_source.py
cd wda
```

Bundle IDs must be unique within your team — the stock `com.facebook.*` IDs
fail provisioning. Rebrand the two built targets plus your team (this exact
recipe was used live):

```sh
sed -i '' 's/com\.facebook\.WebDriverAgentRunner/app.mobster.wda.runner/g; s/com\.facebook\.WebDriverAgentLib/app.mobster.wda.lib/g' \
  WebDriverAgent.xcodeproj/project.pbxproj
TEAM=<your 10-char team id>   # Xcode → Settings → Accounts → team details
UDID=<device udid>            # idevice_id -l
xcodebuild -project WebDriverAgent.xcodeproj -scheme WebDriverAgentRunner \
  -destination "id=$UDID" -allowProvisioningUpdates \
  -derivedDataPath ./dd "DEVELOPMENT_TEAM=$TEAM" USE_IP=127.0.0.1 build-for-testing
```

`USE_IP=127.0.0.1` makes WDA listen on the phone's loopback, which USB reaches,
instead of every interface including Wi-Fi. WDA has no authentication. This
manual recipe does not apply the source patches `serve --manage-device` makes
(`WDA_PATCHES` in `device_manager.py`), so here the MJPEG port 9100 still
listens on the phone's Wi-Fi address and WDA answers browser requests. Prefer
the managed setup.

The device must be registered on the team or provisioning fails with
"isn't registered in your developer account" — register it in the developer
portal (or let Xcode do it from an Admin/ Agent account).

## Each session

The runner serves only while `xcodebuild` runs; the relay maps the device
port to localhost:

```sh
xcodebuild -project WebDriverAgent.xcodeproj -scheme WebDriverAgentRunner \
  -destination "id=$UDID" -derivedDataPath ./dd "DEVELOPMENT_TEAM=$TEAM" \
  USE_IP=127.0.0.1 test-without-building   # runs until killed; keep the phone unlocked
iproxy -s 127.0.0.1 -u "$UDID" 8100:8100   # -s: this Mac only, not the LAN
curl -X POST http://127.0.0.1:8100/session \
  -H 'Content-Type: application/json' \
  -d '{"capabilities":{"alwaysMatch":{"platformName":"iOS"}}}'
```

Then point Mobster at the relay with the returned session — the factory
applies the measured snapshot settings itself:

```sh
python3 -m mobile_agent run 'Open Settings' --wda-url http://127.0.0.1:8100 \
  --session <session-id> --execute
```

Or keep the runner and relay alive together (restarted if either exits):

```sh
TEAM=<team id> UDID=<device udid> WDA_DIR=/path/to/wda scripts/usb-wda.sh
```

## Evaluating on the phone

```sh
set -a; source mobile_agent/.env; set +a      # TYPESAFE_API_KEY; serve does not auto-load it
python3 -m mobile_agent serve --wda-url http://127.0.0.1:8100 --session <id> --enable-live
python3 -m mobile_agent.evals.harness --wda-url http://127.0.0.1:8100 --device iphone15pro --repeats 5
```

Ground truth per device lives in `mobile_agent/evals/tasks.py` (`DEVICES`).
Turn on a Focus mode first: a notification can take the foreground mid-run.

## Measured behavior (iPhone 15 Pro, iOS 26)

- `snapshotMaxDepth` is **60**. It was 25 when full-depth widget home-screen
  snapshots took 40.5 s; with `visible`/`accessible`/`index`/`traits`
  excluded from the source, the home screen reads in ~270 ms even at depth
  80, and depth 25 hid most of Spotify's content (text recall 0.23 vs 0.77).
- `waitForIdleTimeout`/`animationCoolOffTimeout` tuning showed **no
  measurable benefit** (3×3.5 s identical fingerprints either way): not
  shipped.
- `/wda/homescreen` is a **sessionless** route — session-scoped clients get
  HTTP 404. HOME goes through `/wda/pressButton {"name": "home"}`.
- Post-transition snapshots (right after HOME) are slow: allow a ~3 s settle
  (the agent already settles after actions) or a ≥25 s budget.
- One open-folder AX snapshot stalled indefinitely; pressing home unstuck
  it. AX is unavailable while the phone is locked (`kAXErrorServerNotFound`).
- In-app trees observe fast (~seconds); SpringBoard-with-widgets is the
  pathological case and is what depth-25 fixes.
