# Where the iPhone agent spends its time (offline), 23 Sep 2026

This analysis is offline only. I did not touch the phone, WDA, the MJPEG port or the running app, and I did not change any code.
Every number comes from journals and eval records that already existed. I opened the journals read-only (`?mode=ro`).

## Data

| source | what | n |
| --- | --- | --- |
| scratch `eval.sqlite3`, `state2…state13/mobster.sqlite3` (22 Sep, USB iPhone 15 Pro, iOS 26.0.1, WDA) | full event journals: every inference call, action, settle and observation, with timestamps | **346 runs**, 13.8k events, 1,040 Jev decision calls, 593 actions |
| `research/*.jsonl`, scratch `usb-r*/web-r*/full-r*.jsonl` | eval records with oracle verdicts, joined to the journals by `run_id` | 346/346 runs mapped to a task |
| scratch `mobster-frontier/*.jsonl` (23 Sep, in-process harness) | per-run metrics after the speculation, same-screen and text-selection changes (`decision_speculated/used`, `act_ms`, `settle_ms`) | 18 records (small) |
| the desktop app's state journal, `mobster.sqlite3` (23 Sep) | field runs from the real app | 4 runs |

I left out `mobile_agent/.state/mobster.sqlite3` (21 Sep). Its runs used an earlier virtual-device driver, not the USB iPhone.

**Headline set.** `state12` + `state13` (full suite r3/r4, 14 tasks × ~6 attempts, **83 runs**) is the most recent code with journals. For per-operation statistics I also give "post-speedup" (`state6`+, 237 runs) and "all" (346 runs). The 22 Sep journals predate the 23 Sep speculation, same-screen completion and text-selection changes. I apply those changes from the A/B data where it matters.

**Method.** For each run I sorted the events and assigned every millisecond between two consecutive events to one category. An interval where an inference call was in flight counts as that model call. When calls overlapped, I used this priority: decision > action verification > helper > extraction > verification. Only 26 s of 721 s overlapped. Any other interval is labelled by its pair of events. For example, `action_started→action_acknowledged` is dispatch, `action_acknowledged→observation_after_action` is settle, `decision→action_started` is the pre-dispatch refresh read, and `result→run_finished` is finalisation. Wall-clock runs from `run_started` to `run_finished`. Nothing is left over: CPU and unexplained gaps total 1.4 s across 346 runs. Scripts: scratch `lat_attr.py`, `lat_q2…q9.py`.

## 1. Breakdown

### Share of wall-clock (latest 83 runs: mean 8.7 s, p50 8.8 s, p90 17.6 s)

| component | share | per task mean | per task p50 / p90 |
| --- | --- | --- | --- |
| **Jev decision** (includes completion re-asks) | **19.5 %** | 1,692 ms | 1,167 / 3,426 |
| **SWIPE_UP dispatch** | **17.1 %** | 1,481 ms | 0 / 3,897 |
| **Settle after actions** (TAP 8.3, SWIPE 6.1, TYPE_SUBMIT 1.7, other 0.5) | **16.6 %** | 1,440 ms | — |
| Extraction + answer verification (Jev select, helper second opinion, verifier) | 10.6 % | 922 ms | 504 / 2,681 |
| First observation (`observe_ready`) | 7.3 % | 635 ms | 563 / 819 |
| Guard reads (refresh before dispatch, refresh after verification, stale-decision re-reads) | 6.8 % | 588 ms | 505 / 1,571 |
| Helper (Gemini text and recovery) | 4.6 % | 396 ms | 0 / 1,846 |
| Re-observe after settle / completion re-read / after helper | 3.5 % | 306 ms | 153 / 913 |
| TAP dispatch | 3.4 % | 295 ms | — |
| Setup + launch (driver, session, lock check, Home, activate) | 3.4 % | 294 ms | 256 / 499 |
| TYPE_SUBMIT / TYPE / SUBMIT dispatch | 2.9 % | 249 ms | — |
| Overhead (mostly `result→run_finished`: final preview screenshot + journal write) | 2.8 % | 243 ms | 182 / 348 |
| Action verification (Jev) | 1.3 % | 113 ms | 0 / 408 |

The "all" set (346 runs) has nearly the same shares: decision 18.8 %, swipe dispatch 17.2 %, settle 16.6 %, extraction 10.9 %, first observation 7.2 %, guard reads 7.0 %. It also has 3.2 % WAIT backoff, which comes from early runs that were later fixed.

### Per event (post-speedup set unless noted)

| step | n | p50 | p90 | notes |
| --- | --- | --- | --- | --- |
| Setup: `run_started→app_launch_started` | 237 | 174 ms | 461 ms | configure, session resolve, `/wda/locked`, Home press |
| Launch: activate call returns | 237 | 30 ms | 40 ms | The eval harness had already brought the app forward. See field data below. |
| First observation | 237 | 571 ms | 874 ms | ≈ 1 read + the **0.5 s quiet period**. Settings 569 ms, Safari 577 ms. |
| Single AX read (proxy: refresh before SWIPE) | 165 | 286 ms | 505 ms | latest 234 / 502 ms |
| Jev decision (warm connection) | 614 | 249 ms | 784 ms | |
| Jev decision, **first call of a run** (new TLS connection) | 346 | 419 ms | 1,537 ms | Even after replayed steps (no classification question) it is 411 / 1,179 ms, so the difference is connection setup, not prompt size. Input tokens are lower on the first call (8.7k vs 11.7k), and R² of latency on tokens is 0.00. |
| Completion re-ask (decision) | 221 | 267 ms | 779 ms | The re-ask agreed 219/221 times. Now skipped on the same screen. |
| Action verification (Jev) | 60 | 208 ms | 788 ms | |
| Helper text / recovery / extraction (Gemini) | 38 / 49 / — | 997 / 1,181 / 1,238 ms | 1,923 / 2,451 / 2,120 ms | The first helper call of a run is 1,505 ms p50 vs 1,050 ms later. |
| Jev `select_fields` / answer verification | 49 / 60 (latest) | 519 / 396 ms | 1,240 / 1,052 ms | Prefetched in parallel with the completion check (45 of 49 were prefetched). |
| **Dispatch TAP** | 145 | 478 ms | 517 ms | W3C tap. The 40 ms hold is included. |
| **Dispatch SWIPE_UP** | 165 | 2,073 ms | 2,442 ms | **Settings 1,840 ms, Safari 2,419 ms** (legacy `dragfromtoforduration`) |
| Dispatch TYPE_SUBMIT / TYPE / SUBMIT | 17 / 15 / 7 | 1,306 / 1,417 / 308 ms | 1,576 / 1,604 / 315 ms | TYPE includes the focus tap, keyboard polling and keystrokes |
| Dispatch BACK / HOME / LAUNCH_APP | 0 | — | — | Never chosen in these journals |
| **Settle TAP** | 143 | 1,247 ms | 1,459 ms | Bimodal: ~1.0 s, and ~1.4 s (bounded, see below). Settings 1,263 ms, Safari 608 ms. |
| Settle SWIPE_UP | 165 | 659 ms | 872 ms | Settings 586 ms, Safari 856 ms. Unchanged 7/165. |
| Settle TYPE_SUBMIT / TYPE / SUBMIT | 17 / 15 / 7 | 1,082 / 618 / 1,205 ms | 1,336 / 653 / 1,312 ms | |
| Extra `observe_ready` after a settle that returned unproven | 29 | 613 ms | 694 ms | 20 % of TAPs |
| Refresh read after verification (`action_check→action_started`) | 177 | 220 ms | 319 ms | Paid by plain-navigation TAPs too |
| `result→run_finished` | 236 | 170 ms | 228 ms | A final WDA screenshot (`server.py` `preview(final=True)`), `close_all`, then the journal write |

### Per task (latest set, per-task means in seconds)

| task | wall p50 / p90 | actions | decisions live / replayed | model calls | decide | dispatch | settle + re-observe | first obs | guard reads | extraction | helper |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| web.read.scroll | 20.3 / 23.3 | 4.0 | 3.0 / 2.2 | 6.0 | 2.80 | **9.73** | 4.00 | 0.92 | 1.98 | 0.70 | 0 |
| web.search | 15.8 / 19.5 | 2.2 | 5.0 / 0 | 13.0 | 3.17 | 2.12 | 2.91 | 0.55 | 1.64 | 0.90 | 2.54 |
| settings.nav.general.keyboard | 12.2 / 13.7 | 4.0 | 2.3 / 2.7 | 3.3 | 1.55 | 4.84 | 3.83 | 0.56 | 0.90 | 0.25 | 0 |
| settings.about.version | 10.6 / 13.0 | 3.0 | 2.0 / 2.0 | 5.0 | 2.17 | 2.69 | **3.42** | 0.59 | 0.60 | 0.87 | 0 |
| settings.about.model | 10.4 / 11.5 | 3.0 | 2.0 / 2.0 | 5.0 | 1.67 | 2.75 | 3.25 | 0.57 | 0.62 | 1.03 | 0 |
| web.type_url | 10.1 / 15.1 | 1.7 | 2.7 / 0 | 9.5 | 1.70 | 2.07 | 1.46 | 0.56 | 0.80 | 2.60 | 1.02 |
| settings.nav.accessibility | 8.6 / 9.6 | 2.0 | 2.4 / 0.6 | 3.4 | 1.78 | 2.35 | 2.86 | 0.56 | 0.45 | 0.40 | 0 |
| web.absent.phone | 8.4 / 9.5 | 0.8 | 3.7 / 0 | 5.7 | 1.47 | 1.44 | 1.32 | 0.56 | 0.46 | 0 | **1.92** |
| web.follow_link | 7.6 / 9.3 | 0.8 | 1.5 / 0.5 | 5.7 | 1.92 | 0.43 | 0.93 | 0.82 | 0.61 | 1.73 | 0 |
| web.read.fact_in_prose | 5.2 / 6.1 | 0 | 1.3 / 0 | 5.3 | 1.93 | 0 | 0.61 | 0.92 | 0.15 | 0.90 | 0 |
| settings.root.readonly | 4.9 / 8.5 | 0 | 1.0 / 0 | 4.2 | 1.68 | 0 | 0.10 | 0.57 | 0 | **2.96** | 0 |
| web.read.heading | 1.9 / 3.0 | 0 | 1.0 / 0 | 4.0 | 0.76 | 0 | 0.06 | 0.55 | 0 | 0.50 | 0 |
| settings.absent.* (2 tasks) | 1.5–1.6 | 0 | 1.0 / 0 | 1.0 | 0.55 | 0 | 0 | 0.57 | 0 | 0 | 0 |

### Critical path

The loop is strictly sequential. In the 22 Sep code the only concurrent work was the answer prefetch during the completion check, which overlapped 26 s of 721 s. One step on the critical path is:

`decide (0.25–0.4 s) → [verify (0.2 s), if not plain navigation] → guard read (0.22–0.29 s) → dispatch → settle`

The same step, costed per action type:

- TAP in Settings: 0.4 + 0.22 + 0.48 + **1.26** ≈ 2.4 s. Settle is the largest part.
- SWIPE in Settings: 0.4 + 0.29 + **1.84** + 0.59 ≈ 3.1 s.
- SWIPE in Safari: 0.4 + 0.29 + **2.42** + 0.86 ≈ 4.0 s.

Across a whole task, the **device side (setup, dispatch, settle, observe and guard reads) is ~61 % of wall-clock, and the model side is ~36 %**.

### Field runs (desktop app, 23 Sep, n = 4: small, but different in kind)

- `run_started→app_launch_started` took 4.85, 3.01, 3.01 and 3.01 s. The eval p50 is 174 ms. The exact 3.0 s matches `WDA_WINDOW_SIZE_SECONDS = 3` (`/window/size` in `configure()`), which times out when a system overlay holds the "active app".
- Activate acknowledgements took 1.2 s (Settings), 5.3 s and 6.7 s (TikTok). The eval takes 30 ms.
- The first observation took **9.5 s** and saw Safari, not Settings (the overlay-recovery path). Two TikTok runs failed their first read at 20 s.
- In the field, **~13 s passed before the first decision**. In evals it is 0.8 s.

## 2. Specific questions

**Waiting for quiet vs reading.** A single AX read costs ~230–290 ms. Settle plus observation (post-action settle, first observation, re-proofs) is **26.6 % of wall-clock** in the latest set. Of that, **≥17 % of wall-clock is pure quiet-period waiting**: at least 0.5 s × 245 proofs (126 settled actions, of which only 4 matched a screen already known at rest, plus 83 first observations and 40 re-proofs). Specific cases:

- A legacy drag leaves the list at rest when the call returns, so the post-swipe settle in Settings (586 ms) is almost entirely proof.
- The first observation (569 ms) is one read plus the 0.5 s quiet period.
- **The TAP settle is capped at 1.25 s, not 1.5 s.** `agent.py:738` passes `timeout = settle_seconds + 1 = 1.6 s`, and `wait_for_change` returns once fewer than 0.35 s remain (`drivers.py:760`). 73 of 143 TAPs ended at ≥1.2 s. 29 of 143 (20 %) returned unproven, and the next step then paid another full `observe_ready` (613 ms p50) to prove the same screen.

**Did the settled screen match the prediction?**

- Speculation (23 Sep, n = 13): 11 used, 1 discarded, 1 never consumed (the completion skip made it moot). The request hashes matched in 11 of the 12 speculations that were consumed.
- Replay: 146 replayed steps matched their recorded screen, and 9 abandoned the recording (94 %).
- An offline transition cache keyed on (exact screen, operation, target), across the 22 Sep runs in time order: repeated transitions produced exactly the recorded next screen in **357/452 (79 %)** of cases. By operation: SWIPE 85 %, TAP 82 %, TYPE/SUBMIT 24 %.
- 97 % of first screens and 77 % of post-action screens had been seen in an earlier run.

These rates are **inflated by the eval's repeated tasks**. Treat them as upper bounds for real use.

**Time saved by speculation.** A speculation starts after the second agreeing read. It can hide at most the rest of the quiet window (≈0.5 − one read ≈ 0.25–0.3 s). Its side channel also opens a new TLS connection on first use, which costs ~160 ms of that window. Estimated saving ≈ **0.2–0.25 s per used speculation**, or ~0.3 s per suite task. The 23 Sep A/B saw −3.3 s over 4 task pairs (−10.6 %). That figure combines speculation, same-screen completion (saves one re-ask per DONE: 267 ms p50, 0.81 re-asks per task) and text selection (saves ~1 s of helper per text task). Each is n = 1 per arm.

**Replay usage.** 52 of 237 post-speedup runs (22 %) replayed ≥1 step, and 21 % of all decisions were replayed. For the same task, replayed runs were 0.5–3.9 s faster at p50. For example, settings.nav.general.keyboard took 15.0 s live and 11.2 s replayed. Almost all of this comes from repeating eval tasks.

**Model calls per task (latest): 5.17.** They are:

- Jev decision 2.94, including 0.81 completion re-asks
- answer verification 0.72
- Jev select 0.59
- action verification 0.30
- Gemini extraction 0.24
- Gemini text 0.19
- Gemini recovery 0.18

Avoidable or movable calls:

- **Completion re-ask (0.81/task).** Already removed when the screen is unchanged (23 Sep).
- **Helper text (0.19).** Mostly replaced by text selection (23 Sep).
- **Recovery on absent-information pages.** web.absent.phone spends 1.9 s per task on recovery hints (23 calls), and the run then ends `blocked` anyway.
- **Helper second opinion after a rejected pick (0.11/task, ~1.2 s each).**
- **Action verification.** Already skipped for 196 of 240 TAPs. The remaining verified actions are the text operations, which should keep it.

## 3. Top 10 speedups

The estimates are for a suite-average task (8.7 s: 0.71 swipes, 0.61 taps, 0.2 text entries, ~0.7 answers) after the 23 Sep changes. The "where it bites" column gives the gain on the tasks it targets.

**Before the ranking: fix field setup.** Cache the window size and detection point per device (for example alongside `WDA._overlay_apps`), and skip `/window/size` when the size is already known (`drivers.py:599–628`, `compose.py:20–23`). This saves **~3 s on each of 4/4 field runs** and nothing in evals. The risk is none, because the window size of a given phone does not change. It is a bug fix rather than tuning, so it is not ranked.

| # | speedup | mechanism and code | estimate (data) | suite avg | where it bites | precision risk |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **Read below-the-fold web content instead of scrolling, or scroll-to-target in one action** | The web tree already holds off-screen nodes (310 below the viewport on the Eiffel page, and the infobox 2.6 screens down). `from_wda` drops them. Admit them as citable evidence for extraction, or offer them as SCROLL_TO targets with ⌈d/0.488⌉ legacy drags in one action (`state.py from_wda`, `agent.py _handle_action`, `drivers.py:827`). | A Safari swipe cycle is 2.42 dispatch + 0.86 settle + 0.29 guard read + ~0.4 decide ≈ 4.0 s. web.read.scroll does 4 of them (20.3 s p50). The same fact-in-infobox task without scrolling (web.read.fact_in_prose) takes 5.2 s. | **~1.4 s** | web.read.scroll −12 to −15 s | Medium. Off-screen text can belong to hidden or collapsed sections. Restrict it to extraction evidence, keep literal citations and the answer verifier, and never use it as a tap target. |
| 2 | **Settle from the 30 fps MJPEG stream (frame differencing) instead of a fixed 0.5 s AX quiet period** | Declare the screen settled when frames have been unchanged for ~150–200 ms **and** two AX reads agree. Keep the 1.5 s cap. The relay already exists (`wda_video.py WdaVideo`, one upstream connection). Feed it to `WDA.wait_for_change` / `observe_ready` (`drivers.py:680–761`, `:80`). | The 0.5 s quiet period accounts for ≥17 % of wall-clock (245 proofs). Expected saving ≈0.3 s per settled action and ≈0.3 s per first observation. | **~0.8 s** | Settings 3-action tasks ~1.3 s | Medium. AX-only changes (lazy rows off screen, like General's 24→29) still need AX agreement, which is kept. Spinners and video never go quiet and fall back to the cap. Measure first whether 30 fps capture slows AX reads on the device. |
| 3 | **Warm and pool model connections across runs** | Clients are created per run (`compose.py:34–37`, `models.py:380`, and `side_channel` `:391–400` opens a new TLS connection on first use). Keep a process-wide pool, or send a warm-up request during setup and launch for Jev (main, speculation, prefetch) and Gemini. | First Jev call p50 419 vs 251 ms warm, mean +365 ms. First helper call +455 ms p50 (35 % of runs use the helper). TLS on the speculation channel eats most of its ~0.3 s window. | **~0.5 s** | Every task | None |
| 4 | **Drop guard re-reads that protect nothing** | SWIPE and BACK have no target and a scroll is reversible, so skip the pre-dispatch `_refresh_unchanged` for swipes (`agent.py:491–492`). For TAP and TYPE, replace the post-verification AX re-read (`agent.py:617`) with "no pixel change since the snapshot's capture" from the MJPEG stream, and fall back to the read if frames are missing. | Guard reads cost 588 ms per task. A refresh before a swipe is 286 ms p50. The refresh after a check is 220 ms p50. | **~0.4 s** | web.read.scroll −1.2 s | Low for swipes. Low–medium for taps: a pixel check is stricter than AX equality for the visible target, but blind to AX-only changes. |
| 5 | **Fast held drag on UIKit screens** | A W3C pointer drag with a hold before release, used only when the snapshot has no WebView. Keep `dragfromtoforduration` for WebKit (`drivers.py:26–35, 827–833`). Also shorten the post-swipe settle to two agreeing reads, since a legacy drag leaves no momentum. | Measured 23 Sep: 0.87–0.94 s vs 1.75–2.14 s, and exact in Settings. Settings swipe dispatch is 1,840 ms p50 and its settle is 586 ms, almost all proof. | **~0.4 s** | Settings nav tasks −0.7 to −1.4 s | Low–medium. Needs WebView detection and a fresh distance calibration. It failed only in WebKit. |
| 6 | **Answer earlier** | Start `select_fields` + `verify_output` on the decided screen as soon as the DONE decision returns, not after the completion re-read (`agent.py:374–388`, `842–869`). Better still, add the field-choice questions to the DONE decision's fan-out so that only verification remains. | Select 519 ms p50 and verification 396 ms p50 run in sequence. The join after the check is 270 ms p50. Extraction and verification take 922 ms per task. | **~0.3 s** | Retrieval tasks −0.4 to −0.6 s | Low. Keep the use-only-if-identical-fingerprint guard and the verifier. |
| 7 | **Predict the next screen and speculate at dispatch time (chunking-lite)** | Keep a transition cache of (screen fingerprint, op, target) → next fingerprint. When it predicts, start the speculative decision at dispatch, 0.8–1.3 s earlier than `on_settling`. The existing request-hash guard decides whether it is used (`agent.py:291–337`). The same cache can drive a two-action chunk (e.g. General → About) when both steps are replay-grade. | Repeated transitions predicted the exact next screen 79 % of the time (eval-inflated). The full decision latency (~0.4 s) is hidden instead of the ~0.25 s window. | **~0.25 s** | Navigation chains | None with the hash guard (a miss costs ~$0.0007). Chunking without a new decision carries replay-level risk. |
| 8 | **Hedge slow Jev decisions** | If no answer arrives by ~600 ms, send the identical request on a second connection and take the first answer (`models.py decide`). | Latency does not depend on tokens (R² 0.00). Batch p50 drifted 257–539 ms, so this is server variance. Simulation on 1,040 calls: mean 514 → 432 ms, p90 1,097 → 844 ms, +26 % calls. That is optimistic if the latency is correlated in time. | **~0.2 s** | Tail tasks | None (the same model and request; either answer is valid) |
| 9 | **Open URLs directly** | When the request contains a literal URL for Safari, open it with WDA's `/url` (XCUIDevice open) instead of tap-focus, keyboard wait, type, submit and verify (`agent.py`, text path; `drivers.py:810–826`). | web.type_url is 10.1 s p50. The TYPE_SUBMIT path costs 1.3 s dispatch + 1.1 s settle + a decision + verification + text, about 3 s. | **~0.2 s** | URL tasks −3 s | Low. The URL is verbatim from the request and the page is still observed and verified. Validate `/url` on iOS 26. |
| 10 | **Publish the result before the final screenshot, and type faster** | Call `run.finish()` before `preview(final=True)` and `close_all` (`server.py:767–772`), which saves 170 ms p50 per task. Separately, cut TYPE dispatch (1.3–1.4 s vs a 0.48 s tap): raise WDA `maxTypingFrequency` in `WDA_SESSION_SETTINGS`, and replace keyboard polling (`drivers.py:848–855`) with a single read after ~300 ms. | 170 ms, plus ~0.4 s per text entry | **~0.25 s** | All tasks and text tasks | None for the result. Low–medium for typing (dropped keys), because the typed value is already read back. |

**Also considered.**

- **Fix the 1.25 s TAP settle bound.** Pass `timeout ≥ cap + margin + 1 read`. It saves ~0.4 s on the 20 % of TAPs that re-prove, about 0.1 s per task. Include it with #2.
- **Persist the settled-screen memory across runs** (`drivers.py:570`). A first screen seen before would then take one read instead of 0.57 s. Eval data shows 97 % of first screens were seen before (inflated). It saves ~0.3 s per task and is superseded by #2.
- **Cheaper recovery on absent information.** web.absent.phone spends 1.9 s of Gemini time on it, but skipping it risks recall.
- **Skip the Home press when the app is already in front** (`server.py:89–106`). There is no eval data, because the harness resets the app.
- **Reduce Motion on the phone.** Untested.

## Caveats and missing instrumentation

- Only one phone and two apps (Settings and Safari), with repeated eval tasks. Replay and prediction rates are upper bounds. The field sample is 4 runs.
- The settle loop does not log individual reads. That is why the quiet-proof share is a lower bound derived from the code (0.5 s per proof), not a measurement. Adding `settle_first_final_ms` and `settle_proved_ms` to `observation_after_action` would measure it. So would logging the `on_settling` time and the `waited_ms` for used speculations in `run_metrics`.
- Guard reads emit no events. They are inferred from gaps (`decision→action_started`, `action_check→action_started`).
- Model latency drifts between batches (decision p50 257–539 ms), so single A/B pairs are noisy.
