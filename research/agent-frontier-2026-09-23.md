# Agent frontier, 23 Sep 2026: fewer model calls, faster steps, new eval tooling

USB iPhone 15 Pro, iOS 26.0.1, WebDriverAgent. This pass followed on from
`frontier-2026-09-22.md`, `usb-speed-precision-2026-09-22.md`,
`web-suite-2026-09-22.md` and `app-coverage-2026-09-22.md`. Each change is a
general mechanism. None is written for a particular task or app.

## Where the time went (measured before changing anything)

Source: 1,040 Jev decisions and 498 actions in the 22 Sep eval journals
(scratch `state*/mobster.sqlite3`).

| phase | p50 | notes |
| --- | --- | --- |
| Jev decision | 312 ms (p90 1,097) | Latency barely tracks prompt size (R² = 0.04 for input and output tokens), so trimming prompts is not the lever. |
| Jev action verification / answer verification | 304 / 298 ms | |
| Helper (Gemini) text generation | 997 ms (p90 2,060) | Every TYPE / TYPE_SUBMIT waits for this. |
| Helper recovery / extraction | 1,196 / 1,565 ms | |
| SWIPE_UP: WDA call + settle | 1,894 + 640 ms | n = 249, the most frequent action |
| TAP: WDA call + settle | 483 + 1,109 ms | Settling needs 0.5 s of quiet. |
| Completion re-check | one full decision | In 219 of 219 checks, the re-asked DONE agreed whenever the first DONE had passed the gate. |


## What changed

| mechanism | files | what it does | precision guard |
| --- | --- | --- | --- |
| **Text entry as selection** | `models.py` (`request_text_candidates`, `selected_text`, the `type_text` question), `agent.py` | Whenever a text field is on screen, the same Jev decision call also chooses the text to type: an exact span of the user's request (quoted text, addresses, the phrase after "search for / go to / titled / saying", or a clause). This removes the helper round trip and helper-budget dependence for most text entry. | The text is a verbatim substring of the request. The pick needs confidence ≥ .7 and a strict top choice, otherwise it abstains. The action verifier still checks the text. A field that already holds part of the text goes to the helper. |
| **Same-screen completion** | `agent.py` (`_handle_done`), `Jev.stable_completion` | When a DONE (made without a helper hint) is followed by a fresh read with the identical fingerprint, the decision is not asked again. | It's a model capability, not a general rule: generic or fake models are still re-asked. Any change on screen re-asks. Justified by 219/219 agreement in the journals. |
| **Speculative next decision** | `agent.py` (`_speculate`, `_speculated_decision`, `decision_key`), `drivers.py` (`wait_for_change(on_settling=)`), `Jev.side_channel(name)`, thread-safe Jev call ids | When the settle loop sees two agreeing reads, the next step's decision starts on a separate connection, during the 0.5 s quiet period. | The request (screen fingerprint plus every model input: history, hint, evidence context) is hashed. The live step uses the speculative result only when its own request hashes identically; anything else decides live. At most 2 speculations per action, and none while replaying or before output classification. |
| **WDA configure: settings first** | `drivers.py` | Session settings (with a safe (160,320) detection point) are applied before anything that resolves the active app. `/window/size` gets 3 s; if it fails, the first observation's Application frame sets the exact centre. | Previously a fresh session could hang 10 s and fail. Measured now: configure completes in 3.0 s under the same conditions. |
| **System-overlay recovery** | `drivers.py` (`WDA.observe`, `_overlay_apps`), `evals/oracles.py` (`SYSTEM_OVERLAYS`, probe foreground/observe via the session) | On iOS 26.0.1, WDA resolved the active app to the invisible `com.apple.siri.IntelligentLight` with Safari in front. Every source read failed after ~8 s and `activeAppInfo` took 8–14 s. A failed read is retried once with WDA's `defaultActiveApplication` set to the app we activated (0.58 s instead of an 8 s failure). The app is remembered per WDA URL so later drivers hint it immediately. | A read never mutates, so one retry is safe. Cost: while the hint is set, WDA prefers that app when several are active, so a SpringBoard alert over it could be hidden. See "Left open". |
| **Eval tooling** | `evals/harness.py`, `evals/tasks.py`, `evals/calibrate.py` | `--in-process` runs the agent directly (with the same device lease and app activation as `serve`). `--baseline` gives a same-day A/B with the three speed paths off. `--yield-to URL` waits until another server has no queued or running task (GET only). `--suite` and a comma `--only` select tasks. Records carry model calls by purpose, act/settle timings and (numbers-only) answer signals, which `calibrate.py` now reads inline. New **near-miss suite** (5 tasks, ground truth read on the phone): Capacity 128 GB vs Available, Model Name vs Model Number, construction start 1887 vs completion 1889, architectural 300 m vs tip 330 m, death year 1923 vs birth 1832. These supply the negatives calibration lacked. | — |

## Measured results

**Text selection, offline against the real Jev API** (8 request types × 3, synthetic text-field screens):
24/24 correct after one cue fix (21/24 before it; the misses fell back to the helper). **0 wrong texts.**
Both "must compose" cases (translate a message, research a population) abstained 6/6. The added question
costs nothing: decision p50 was 141 ms with it.

**Live A/B, same day, same phone, in-process, n = 1 per task per arm** (small; the runner was unstable, see below):

| task | baseline ms / decisions / calls | new ms / decisions / calls |
| --- | --- | --- |
| settings.about.version | 7,487 / 5 / 7 | 7,195 / 4 / 6 |
| settings.nav.general.keyboard | 10,201 / 6 / 6 | 9,075 / 5 / 5 |
| web.follow_link | 6,641 / 3 / 7 | 5,170 / 2 / 6 |
| web.type_url | 6,635 / 4 / 9 (helper text 688 ms) | 6,234 / 2 / 8 (text selected; no helper text call) |
| **sum, pairs where both passed** | **30,964 ms, 18 decisions, 29 calls** | **27,674 ms (−10.6%), 13 decisions (−28%), 25 calls** |

Pass rate was unchanged on these pairs (4/4 in both arms). web.search failed in both arms the same way: the
agent stopped on Google's results page, where the snippet shows the year, without opening the article. The
verifier's final-state check refused the answer, so nothing false was returned. A smoke run of
settings.about.version showed 3 decisions speculated, 2 used, and 1 completion re-ask skipped.

**Scrolling (negative result, reverted).** A W3C pointer drag held still before release returned in
0.87–0.94 s against 1.75–2.14 s for `/wda/dragfromtoforduration`, and moved exactly in Settings (UIKit).
In Safari, though, WebKit still applied momentum: settling hit its 1.5 s cap, content moved for ~2 s and the
distances were inconsistent, even with 600 ms moves and 400 ms holds. `pressAndDragWithVelocity` with a hold
was no faster (~2 s). The legacy drag moved exactly 0.488 screens every time. Scrolls stay on the legacy
drag, with the numbers recorded in `drivers.py`.

**Off-screen web content is in the tree.** On the Eiffel Tower article, WDA reported 310 labelled nodes
below the viewport and 97 on screen. The infobox's "Height / Tip / 330" sat at y≈2230–2302 pt (2.6 screens
down), and `from_wda` drops all of it. The scroll task spent 4 decide+swipe cycles (~16 s) reaching it.

## Environment problems found (and handled)

* The system overlay above made Safari unreadable. It crashed or restarted the runner several times, and
  caused every web reset to fail in the first A/B attempt (6/42 then, all infrastructure). Recovery is now in
  the driver and the probe.
* The runner restarted repeatedly during the session and was down at the end (phone locked or app quit), so
  the planned 3-repeat core A/B and the near-miss suite could not be completed live. The near-miss tasks are
  defined but **not yet run**.

## Left open, highest leverage first

1. **Scroll-to-target from the off-screen tree** (web views and non-lazy SwiftUI stacks). Offer off-screen
   labelled nodes as SCROLL_TO targets, and have the driver issue ⌈distance / 0.49⌉ exact legacy drags in one
   action. That drops the per-swipe decide-and-settle cycles; the estimate is ~5 s off web.read.scroll.
   A cheaper first step is a "below the fold" outline in the decision state, so Jev knows the target exists
   and which way to scroll.
2. **Action-requirement tracking for multi-step requests** (web.search). Jev says DONE when a snippet already
   shows the fact, even though the request says to open the article. The verifier refuses correctly, but the
   run should continue rather than end. Candidate: when the final-state claim fails with a high entity/field
   score, turn DONE back into "continue" once, with a hint naming the unmet action.
3. **Replace, don't append, when typing into a focused field with text.** The journals show
   `https://www.google.comAda Lovelace Wikipedia` in Safari's address bar. Clear with delete keys before
   TYPE_SUBMIT. Needs a live check that `\b` via `/wda/keys` works on iOS 26.
4. **Alerts while the foreground hint is set**: probe `/alert/text` (or SpringBoard) when an action changes
   nothing, so a permission prompt is never hidden by the hint.
5. **Run the near-miss suite ≥ 3× and refit** `AGREEMENT_*` and the verifier floors with `calibrate.py`
   (records now carry their signals inline).
6. **Picker wheels and sliders on WDA** (INCREMENT/DECREMENT are not implemented for WDA). Candidates:
   tapping the adjacent row of a wheel, or WDA's element value set (needs an element lookup). Validate on
   Clock → Timer without starting it.
7. **Speculation hit rate**: measure used/speculated across a full suite. The one smoke run was 2/3; a
   discarded speculation costs one Jev call (~$0.0007).
