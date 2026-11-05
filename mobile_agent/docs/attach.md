# Attach: WebView DOM, Flutter semantics and the app score (26 Oct 2026, dark)

Dark behind `MOBSTER_EXPERIMENTAL_ATTACH=1`. Nothing in the agent loop calls this package yet; the one hook is the hidden `mobster doctor --apps`. Real-phone proofs are pending. No Web Inspector bridge exists (3 Nov 2026, below), so `web.read` always returns the AX tree; the Flutter and WebView fixtures are still synthetic.

## What WebDriverAgent gives

WebDriverAgent reports a WKWebView as one `WebView` element with whatever accessibility children WebKit publishes. It has no web context and no script execution in a page. The DOM comes from the WebKit remote inspector (the route Appium's XCUITest driver takes through a debug proxy). `attach/web.py` defines `WebBridge` (`contexts()`, `evaluate()`) as the only seam to that inspector. No real bridge implements it: with none, `web.read` returns the AX tree with the reason `no web inspector bridge`.

## Modules

| Module | What it does |
|---|---|
| `attach/web.py` | `read(tree, bridge)` returns the DOM as `AXNode`s (path `web:<selector>`, role from tag/ARIA, frame scaled from CSS pixels into the WebView's frame) merged with the native nodes outside the WebView. With no bridge, no page for the app, or a dropped connection it returns the AX tree and a `reason`. `click` and `type_text` target a selector. |
| `attach/flutter.py` | `read(tree)` recognises Flutter's shape (engine element names, or mostly labelled `Other` nodes with few typed controls), maps `Semantics(identifier:)` to `identifiers` and turns a labelled, enabled leaf `Other` into a `Button`. |
| `attach/score.py` | `score_tree` rates a screen good, partial or poor with reasons; `score_apps` launches and scores a list of bundles; `save` keeps the latest score per bundle in `<data dir>/attach/app-scores.json`. |
| `attach/cli.py` | `mobster doctor --apps` (hidden flag): lists the USB phone's apps, activates each one, reads its first screen, stores and prints the scores. It taps, types and scrolls nothing. |

## Rules

- Page text is data. DOM labels fill `label` and are never read as instructions.
- An action is one attempt. A script that errors, a vanished element or a dropped page raises `WebActionError`; nothing is re-sent, and the caller reads the screen. Selectors and typed text go into the script as JSON strings, never as code.
- Anything that can't be proved falls back to the AX tree with a reason; it never guesses a selector.

## The removed inspector bridge

From 26 Oct to 3 Nov 2026, `attach/inspector.py` was a bridge that imported pymobiledevice3's `WebinspectorService` into Mobster's process. For a simulator it subclassed that service and replaced its private connection, so it could connect to `webinspectord_sim`'s unix socket. Mobster never imports pymobiledevice3, so the bridge was removed. Its command-line tool can't stand in: it runs a script in a page only from an interactive shell (`webinspector js-shell`) or a long-running DevTools server (`webinspector cdp`), and it has no way to reach a simulator's socket. No command or setting reached the bridge: it had no caller outside its own tests.

What the bridge measured on 26 Oct 2026 (iPhone 17 Pro simulator, iOS 26.4, pymobiledevice3 11.17.0, Safari showing a local page) still describes the inspector:

- The simulator's `webinspectord_sim` listens on the unix socket that `xcrun simctl spawn <udid> launchctl getenv RWI_LISTEN_SOCKET` names. It speaks the device service's framing: a 4-byte big-endian length, then a binary plist. `_rpc_reportIdentifier:` was answered by `_rpc_reportCurrentState:`, `_rpc_reportConnectedApplicationList:` and `_rpc_reportConnectedDriverList:`.
- Listing Safari's three tabs took 1.45 s. `web.read` on the page returned `source: web` with the heading, the button (`#go`), the field (`#name`) and both outputs. `click` fired the handler once, and `type_text` set the value and fired `input`.
- With Safari stopped, a script timed out after 5 s and was not sent again, and `web.read` fell back to `ax` with a reason.
- The listing doesn't say which tab is on screen, and lists tabs in the order they opened. An app with more than one inspectable page falls back to `ax` (`pick_context`).
- Not fixed: DOM frames are placed from the WebView's top edge. Safari's WebView spans the whole screen, and the page starts below the status bar, so frames are about 71 pt too high. `click` and `type_text` don't use frames.

A new bridge implements `WebBridge` with code Mobster owns, or runs a separate program, and keeps the rules above.

## Rating rules

Poor: three or fewer elements, or fewer than 60% of controls labelled. Partial: fewer than 85% labelled, fewer than eight elements, no controls, a WebView, or a Flutter tree. Good: none of those. The thresholds match `evals/app_survey.py`'s recall classes in spirit but are not calibrated; calibration needs a survey run on real apps.

## Simulator proof plan

Step 3 and the fallback ran on 26 Oct 2026 with the removed bridge (above); step 3 waits for a new bridge. Steps 1, 2 and 4 wait for a WKWebView and a Flutter sample build.

Take `SIM-LOCK`, boot one simulator, then:
1. Install a sample app with a WKWebView (a local HTML page) and a Flutter sample app; run WDA on the simulator.
2. Record each app's `/source` with `read_source` and replace the synthetic fixtures in `tests/test_attach.py` with the recordings, checking Flutter's real element names and roles against `flutter.MARKS`.
3. Wire a `WebBridge` to the simulator's inspectable pages and check that `web.read` returns the page's controls and that `click` fires the page's handler.
4. Run `MOBSTER_EXPERIMENTAL_ATTACH=1 mobster doctor --apps` against the simulator and compare ratings with `evals.app_survey` text recall.
5. Shut the simulator down and remove `SIM-LOCK`.
