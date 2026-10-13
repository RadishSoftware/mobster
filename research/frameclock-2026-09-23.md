# FrameClock: the MJPEG stream as a settle clock, plus faster scrolling (2026-09-23)

This memo covers FrameClock, which implements the settle-detection and scroll-odometry proposals of an earlier visual-loops research memo (not published). It uses WDA's 30 fps MJPEG stream as a clock that answers two questions: did the action take effect, and is the screen at rest? Measuring scrolls with it also produced a faster exact-distance scroll primitive, the "glide". Two follow-ups are covered at the end: off-screen evidence with SCROLL_TO, and launch readiness.

**Status:** measured live on the USB iPhone 15 Pro (iOS 26.0.1, WDA 16.12.10), in Settings and Safari, with read-only actions only. The default is now **`MOBSTER_FRAME_CLOCK=on`**. The gates passed on the surfaces that were measured, with the caveats below.

The measurement conditions were not clean:
- The Mac was under heavy load from other work, with a load average of about 56.
- WDA dropped its connection 4 times in about 70 minutes, twice as a connection reset and twice as connection refused. The runner restarted once, with a new session ID, while the app on 8765 was also unreachable. Every step that was cut short was redone. The runs only ever read, tap navigation rows and scroll.
- The phone was left on the home screen.

## Results at a glance

| what | before | after | n | precision |
| --- | --- | --- | --- | --- |
| TAP settle, Settings → General | 1,351 ms p50 | **1,028 ms** | 6 + 6 | 0 premature |
| TAP settle, General → About | 860 | 864 | 6 + 6 | 0 |
| TAP settle, back to General | 986 | **824** | 6 + 6 | 0 |
| TAP settle, back to the root | 1,314 | 1,266 | 6 + 6 | 0 |
| **All 48 navigation settles** | 1,122 ms mean | **996 ms mean (−125 ms)** | 24 + 24 | 24/24 held 1 s later in both modes |
| SWIPE in Settings: dispatch + settle | 1,676 + 599 = 2.28 s | **1,238 + 326 = 1.56 s (−0.71 s)** | 4 + 4 | distance 0.4836 vs 0.4883 screens; 8/8 held |
| SWIPE in Safari: dispatch + settle | 3,050 + 679 = 3.73 s | **1,919 + 1,338 = 3.26 s (−0.47 s)** | 4 + 4 | 0.48 screens, 4/4 held (clock unhealthy, AX settle) |
| First read in a new process, with the Siri overlay up | 9,007 ms | **160 ms** | 1 + 1 | the same read |
| Scroll to a target 4.3 screens down (Safari) | ~9 swipe cycles × ~4 s | **12.9 s, 6 glides, target at y = 0.404** | 1 | the target is re-observed as an on-screen element |
| MJPEG capture lag vs `/screenshot` | — | **≤ ~55 ms** (clear matches in [−57, +75] ms windows) | 7 clear | — |
| AX read p50 with and without MJPEG streaming | 150–190 ms | same | 4 blocks × 8 | — |

The "before" column is today's AX-only settle (FrameClock "shadow" mode, which behaves the same as off) and XCTest's drag. The "after" column is FrameClock "on" plus the glide.

## What the live data changed

The version that was built and tested offline accepted **one AX read** once the pixels had been still for 0.18 s. The phone showed that this is unsafe, and that several assumptions were wrong:

1. **AX changes without visible change (measured twice).**
   - After a push to General, AX briefly re-exposes the root's floating Search field. It appears for about 150 ms, while no 16-px tile moves.
   - After a pop to the root, iOS 26's glass search bar keeps morphing by about 1 pt for 0.5–0.8 s. Its AX rect crosses the 4 pt grid (`Image 'Search'` jitters between (0.112, 0.925) and (0.122, 0.93)), and AX passes through 3–5 states. Some of those states last two consecutive reads (about 270 ms).
   - With "one read" or "two agreeing reads" as the rule, "on" mode had **1 premature settle in 20**: the next read, 1.2 s later, differed.
   - **Final rule:** changed reads that agree with each other for **0.3 s**, instead of the AX-only 0.5 s, **and** no tile moved from 0.18 s before the last read until it returned, **and** a pixel change was seen after dispatch. AX is never skipped. Across 32 traced AX-only settles replayed offline, and 24 live "on" settles, there were 0 premature settles.
2. **Byte identity is an exact stillness signal, but too strict to settle on.**
   - A static screen re-encodes to identical JPEG bytes (29/29 frames). The glass morph shows up as changing bytes that stop roughly when AX settles.
   - After pushes, however, the bytes keep changing for about 1.0–1.2 s while AX has been stable since about 0.3–0.5 s. Settling on bytes therefore never beat AX quiet.
   - It is kept as `still_for()`'s **default** (exact) for callers that *skip* an AX read, such as the agent's pixel-guarded refresh. Tile stillness misses changes that alter AX.
3. **iOS 26 push animations are long.**
   - The last tile motion comes 0.35–0.65 s after the tap request returns. The tap's effect is visible 60–200 ms *before* the request returns, because WDA answers about 200 ms after the touch.
   - About's AX is quiet from about 0.3 s, so AX quiet already ends at the same time as the pixels: no gain there.
4. **Frame rate drops while WDA works.**
   - Settings streams at about 20–28 fps (p50 frame gap 36 ms, p99 127 ms, max 495 ms). During AX reads frames arrive every 60–100 ms.
   - In Safari the rate drops to about 9 fps under the heavy AX load (source reads take about 630–700 ms there). The clock then reports **unhealthy** and the driver keeps the AX settle, as designed. Pixel settles in Safari are therefore **not** measured.
5. **The coordinator's first run was on a Settings search-results page**, not the root. The "not_stable" and changed=false swipes were rubber-band bounces on a short list. The fixed script resets to the root: it closes search, goes back, and taps the status bar to scroll to the top.
   - Separately, **the AX tree on that page still listed the root's rows (General, Wi-Fi, …) under the search overlay**, with on-screen rects. `state.wda_occlusion` did not hide them. That is a precision risk for taps, outside FrameClock, and is worth a fix in `wda_occlusion` (the search overlay is not a NavigationBar, Alert or Sheet).
6. **The earlier glass-lag number (−635 ms) came from a method confounded by the phone/Mac clock offset.** The new method needs no clock:
   - Fling a list, then match each WDA `/screenshot` taken during the momentum to the MJPEG frame with the smallest difference.
   - The capture happens inside the request, so lag ∈ [arrival − request end, arrival − request start].
   - The clear matches gave windows of about [−57, +56] ms, so the **MJPEG lag is at most about 55 ms**.

## The drag investigation

With FrameClock odometry, plus AX ground truth (the median element shift between reads before and after), in Settings and Safari:

| primitive | first motion after request | request time | distance (screens) | motion after release |
| --- | --- | --- | --- | --- |
| XCTest `dragfromtoforduration` (today) | 0.63–0.77 s (Safari 2.3 s) | 1.60–1.76 s (Safari 3.0–3.3 s) | **0.4875**, always | none |
| W3C drag, 400 ms move + 250 ms `pause` + up | 0.37–0.48 s | 1.16–1.27 s | 0.5 and then **flings 0.5–1.5 extra** | ~2 s |
| W3C drag, 50–200 ms move, no hold | 0.33–0.38 s | 0.61–0.86 s | 0.28–0.55, inconsistent, flings | 1–2 s |
| W3C drag + stationary `pointerMove` tail | — | 0.65–0.95 s | flings (the tail makes no touch samples) | ~1.5 s |
| Fast-start glide (90 % in 150 ms, decelerating end) | 0.45 s | 1.03–1.13 s | 0.356–0.409, **varied between runs** | none |
| **Glide: 4 % in 80 ms, 92 % in 200 ms, 99 % in 120 ms, 100 % in 80 ms** | 0.52–0.64 s | **1.11–1.28 s** (Safari 1.90–2.14 s) | **0.4742 (8/8), 0.4836 via the driver; Safari 0.481** | none |
| Glide over a longer span (85 % → 15 %) | — | 1.27–1.43 s | 0.6808 (4/4) | none |
| Several glides in one W3C request | — | linear in the strokes | only the first stroke scrolled | — |

Why the W3C drag flung before:
- A W3C `pause` produces no touch samples, so at lift UIKit still sees the last move's velocity.
- A `pause` while the finger is up costs about **5 s** in WDA (a pause-only pointer action took 5.2 s).
- A fast start loses 0.05–0.15 screens to touch slop, and the loss varies. A slow first 4 % makes the pan begin at a precise point.
- The decelerating end leaves almost no velocity at lift.

**Adopted:** the glide is used for SWIPE_UP/DOWN in the two measured apps (Settings and Safari, `WDA_GLIDE_BUNDLES`). Other apps keep XCTest's drag unless listed in `MOBSTER_GLIDE_BUNDLES` (`*` means all). A paging feed could snap back from a glide with no velocity. When content keeps moving more than 0.12 screens after release (measured from frames), the FrameClock demotes that app to XCTest's drag. Horizontal swipes are unchanged.

Scroll settle after a glide is 0.33 s in Settings, with 2 reads. In Safari it is 1.34 s, compared with 0.68 s after the legacy drag. That is because Safari's toolbar animates after a fast scroll, and the slow legacy drag hides that animation inside its 3 s request. The net Safari swipe is still 0.47 s faster.

WDA's own per-request cost dominates what is left: 0.35 s (Settings) to 1.3 s (Safari) before the first touch, and about 0.2 s after it. `/window/size` takes 138 ms in Settings and 671 ms in Safari, which suggests that the active-app resolution and snapshot inside WDA scale with the size of the AX tree. The client cannot remove this cost.

## Off-screen evidence and SCROLL_TO (follow-up item 1)

- **`state.from_wda`** now keeps labelled nodes that lie entirely outside the viewport as `Snapshot.offscreen`: `OffscreenNode(label, role, rect, value, locator, web)`. The `rect` is unclipped and in screen fractions, so y = 2.6 means 2.6 screens down. The list is capped at 800 and deduplicated by text and position.
  - These nodes are **never** in `elements`, `text`, `public()` or either fingerprint, so screen identity and settling are unchanged. They can never be a tap target.
  - `Snapshot.offscreen_evidence(limit, web_only)` returns them nearest first, with `direction` and `screens_away`, for extraction and scroll-to decisions.
  - Live, a Google results page in Safari exposed **310 off-screen web nodes, up to 6.07 screens down**, with 39 elements on screen.
  - Settings' UIKit list exposes none, because cells are recycled.
- **`WDA.scroll_to(snapshot, node)`** reads the distance from the node's AX frame. It covers the distance with long glides (0.68 screens each, the last one cut to the remainder) with no read in between. One settled read then finds the node again, the AX being the exact odometer, and up to 3 correction rounds follow. A glide that moves no pixels ends the pass early.
  - It returns `(snapshot, element, info)`. `element` is a fresh on-screen `Element` that still needs the usual verification.
  - Live in Safari: "Videos", 4.3 screens down, took **6 glides and 12.9 s** and landed at y = 0.404 (target 0.40). "Privacy", 5.7 screens down, took **8 glides and 17.2 s** and landed at y = 0.817 (end of page). The decide-and-swipe loop needs about 9–12 cycles of about 4 s each for those distances.
- Hooks for the agent (the latency engineer owns `agent.py`): use `snapshot.offscreen_evidence()` as read-only evidence with literal citations, and call `driver.scroll_to(snapshot, snapshot.offscreen[i])` for a SCROLL_TO decision.
- Risks: off-screen web nodes can belong to collapsed or hidden sections, so keep them evidence-only with verification. A label that repeats is resolved to the match nearest the predicted position, and `info["ambiguous"]` flags it.

## Launch (follow-up item 2)

- **The 8–9 s first read is the invisible Siri overlay** (`com.apple.siri.IntelligentLight`, which WDA resolves as the active app). The driver already recovered from it once per process with `defaultActiveApplication`. The apps that needed that hint are now kept across processes:
  - `WDA.load_overlay_hints(path)` loads them.
  - The server wires `<state dir>/wda-overlay-hints.json` in `Runtime.work`.
  - Measured in a fresh process: first read **9,007 ms → 160 ms**.
  - The hint has a known cost, carried over from before: while it is set, a SpringBoard alert over that app can be hidden from reads.
- **Pixel-gated launch was measured and rejected.** Waiting for "the first still frame" before reading made warm launches slower: Settings 0.70 → 1.49 s, Safari 1.5–1.8 → 3.0 s. iOS's zoom-in animation outlasts the app's AX tree.
  - `WDA.launch(bundle)` is activate (hint applied) plus `observe_ready`, where the "on" rule applies, and it returns the phase timings. Cold launches of other apps were not measured, because no app was terminated.

## What was built (files)

| piece | where |
| --- | --- |
| `FrameClock`: draft-mode decode (1/4, grey, status bar and right-edge scroll indicator cropped), 16-px tile diffs, masks for activity that is still going on, `mark`, `wait_effect`, `wait_stable`, `quiet_between(exact=)`, `still_for(exact=True)`, release frames, `displacement` (exhaustive profile search; a coarse pass aliased on repeating lists), health, JSONL records | `mobile_agent/frame_clock.py` |
| Settle rule, shadow comparison (`fc_ms`, `premature`, `next_read`), trace mode, glide, `scroll_to`, `launch`, persisted overlay hints | `mobile_agent/drivers.py` |
| `Snapshot.offscreen`, `OffscreenNode`, `offscreen_evidence` | `mobile_agent/state.py` |
| Frame source in the relay tuple; `screenshot_fallback=False` for private consumers | `mobile_agent/wda_video.py` |
| Wiring: the relay subscription and overlay hints in `server.py Runtime.work`; its own connection in `__main__ run` and `evals/harness.py`; the harness `--baseline` turns FrameClock and the glide off | |
| `Pillow==12.3.0` in `requirements.txt` and `pyproject.toml` | |
| Live script (`--only latency,lag,nav,swipe,safari`, `--create-session`) and offline bench | `research/frameclock_live.py`, `research/frameclock_bench.py` (not published) |

### Flags

| variable | values | default |
| --- | --- | --- |
| `MOBSTER_FRAME_CLOCK` | `off` \| `shadow` \| `on` | **`on`** |
| `MOBSTER_FRAME_CLOCK_LOG` | a JSONL path | server: `<state dir>/frameclock.jsonl` (rotated at 5 MB) |
| `MOBSTER_FRAME_CLOCK_TRACE` | any value: per-frame motion and per-read timelines in each settle record | off |
| `MOBSTER_GLIDE` | `0` uses XCTest's drag everywhere | on for Settings and Safari |
| `MOBSTER_GLIDE_BUNDLES` | comma-separated list, or `*` | none |

### Offline cost (Apple M5 Max)

- Frame processing: 0.28–0.30 ms p50 while a list scrolls, 0.74 ms for a photo feed, 0.001 ms for byte-identical frames.
- Odometry: about 10 ms per swipe, on a background thread.
- Live p50 was 0.02–0.3 ms. p99 reached 26 ms only under the Mac load average of about 56.

## Gates and the default

| gate | result |
| --- | --- |
| Premature-settle rate no higher than AX-only | **0/32 "on" vs 0/32 AX-only** (24 navigation + 8 swipes, 1 s hold check). The shadow replay of the final rule gave 0/32 premature. The earlier one-read and two-read rules failed (1/20) and were replaced. |
| AX read latency unchanged with the stream up | 150–190 ms p50 with and without (coordinator's run) |
| Scroll distance within 2 % of the legacy drag, no momentum | glide 0.4836 vs 0.4875–0.4883 (−1 %), consistent, no motion after release |

The default is **on**. Caveats:
- The samples are small: 0/32 has a 95 % upper bound of about 9 %.
- Only Settings and Safari were measured. In Safari the clock reports unhealthy and the AX rule applies unchanged.
- The research gate of "14-task suite × 3" was not run. It needs model calls and text entry in Safari, which fall outside this brief's read-only limits.

`MOBSTER_FRAME_CLOCK=shadow` keeps logging `fc_ms` and `premature` against the AX settle for every run, so the rate can be audited in the field.

## Open issues

- WDA connection drops (reset or refused, about 4 per hour under this load) end live runs with a TransportError. Reads are safe to repeat, but the transport never retries.
- AX exposes rows hidden under Settings' search overlay (item 5 above).
- The glide in Safari leaves a toolbar animation that costs about 0.65 s of settle. A slower final segment might hide it.
- Safari runs at about 9 fps under AX load, so pixel settles and the glide's post-release check are off there. Lowering `MIN_FPS` would need its own precision data.
