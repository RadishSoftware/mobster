# USB iPhone: speed and precision pass

Physical iPhone 15 Pro, iOS 26.0.1, over USB through WebDriverAgent, 22 Sep 2026.
All task numbers are graded by `mobile_agent/evals/harness.py` with the
independent WDA oracle (`WDAProbe`), which never reads the agent's evidence.

## Result

| | before | after |
| --- | --- | --- |
| Suite (7 tasks) | 21/21 (`usb-task-eval-2026-09-22-baseline.jsonl`) | **35/35** (`usb-task-eval-2026-09-22.jsonl`, 5 repeats) |
| Navigation tasks reporting their own success | 0/6 (`inconsistent_completion`; the oracle did not check status) | **10/10**, now required by a `StatusIn` oracle |

Agent time per task, median of 3 (before) and of cold first attempt / warm replayed attempts (after):

| task | before | after, cold | after, warm |
| --- | --- | --- | --- |
| settings.about.version | 16.4 s | 8.5 s | 7.9 s |
| settings.about.model | 13.5 s | 9.2 s | 7.8 s |
| settings.nav.accessibility | 7.7 s | 6.6 s | 6.1 s |
| settings.nav.general.keyboard | 13.9 s | 12.0 s | 10.5 s |
| settings.root.readonly | 4.4 s | 2.8 s | — |
| abstention (two tasks) | 0.9–1.2 s | 0.8–0.9 s | — |

Cold medians are n=1 per task, and warm medians are n=4 per task.

## What moved the time

| change | measured effect |
| --- | --- |
| WDA source without `visible`/`accessible`/`index`/`traits`, with geometric occlusion instead | read ~950 ms → ~130–210 ms |
| `waitForIdleTimeout`/`animationCoolOffTimeout` = 0; tap by coordinates, not XPath | tap ~1.2 s → ~0.47 s; driver step ~10.6 s → ~2.0 s |
| Answer extraction by Jev choosing observed literals (helper as fallback) | 4–6 s generative extraction with retries → one ~0.3 s typed call |
| Answer selection + verification prefetched on a second connection during the completion check | tail of retrieval ~1.7 s → ~0.3 s |
| Separate action verification skipped for plain navigation taps (see policy below) | 0.2–1.0 s per tap |
| Redundant pre-verification re-read dropped for taps | ~0.27 s per tap |
| Compiled runs: replay a completed run's actions on byte-identical screens | ~0.3–0.4 s of model time per replayed step |

## Precision fixes found on the phone

- **Scroll demotion.** Jev picked SWIPE_UP at .43–.46; the .55 navigation floor
  demoted it to WAIT on every step (one run spent 30 steps, 84 s). Vertical
  scrolls now have their own floor (.30). Unchanged, non-loading WAIT plateaus
  now end as `no_progress` after recovery instead of burning the step budget.
- **Spurious STOP.** Jev returned `stop_gate=stop` on reaching About for a
  request with no condition in it. A STOP is ignored only when the request has
  no conditional or prohibitive wording at all.
- **Verifier token limit.** Full-form evidence (62 KB) exceeded the verifier's
  limit and failed every answer. Verification now receives compact evidence
  (~15 KB) with cited entries always included.
- **Completion floor calibrated.** Wrong screens scored goal-met 0.02–0.13 and
  correct screens 0.38–0.85 (0.72–0.83 in live runs), so the uncalibrated 0.9
  rejected every correct navigation. The floor is now 0.5 (n=15 probes + 12 runs; small).
- **Half-loaded screens.** General shows 24 elements for ~450 ms, identical
  across two reads, before loading to 29. A screen now needs a 0.5 s quiet
  period (or a previously proven identity) before it counts as settled.
- **Rejected: velocity swipe.** 5% faster, but momentum scrolled past the
  target rows and failed every navigation task. Scrolls keep a bounded distance.

## Plain-navigation tap policy

A TAP skips its separate verifier only if all hold: navigation risk tier (no
side-effect wording in goal or label), decision confidence ≥ .9, Jev's own
side-effect Score < .15, a role that cannot hold state (not Switch/Slider/
Picker/TextField…), and no state-changing verb in the label. Everything else
is verified as before.

## Known limits

- One phone, Settings only, 7 tasks. This is a regression suite, not a benchmark.
- Scrolls cost ~2.3–2.4 s; the floor is iOS deceleration, not WDA.
- Incoming notifications on a personal phone can take the foreground mid-run
  (observed once: Messages). Use a Focus mode during evals.
