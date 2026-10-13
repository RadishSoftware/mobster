# Apps whose state is drawn, not published

Measured on device, 21 Sep 2026, with `--read-screen` enabled.

## What happens

Calculator exposes 24 accessibility nodes: every key, plus `Last Expression` and
`Edit field`. Both of the latter report an **empty value**. No keypress changes
the tree in any way.

A real run of "compute 5 times 5":

| step | action | verifier | result |
| --- | --- | --- | --- |
| 1 | TAP `5` | allowed | acknowledged |
| 2 | TAP `Multiply` | allowed | acknowledged |
| 3 | TAP `5` | **mismatch** | refused |
| 4 | TAP `5` again | — | run ends `blocked` after 6.3 s |

## Why this is not a guard bug

The guard refuses an action whose prospective effect does not match the request,
and it is judging identical evidence: the observation before step 3 is the same
as the one before step 2. On that evidence, "tap 5 again" genuinely is
indistinguishable from replaying an action that did nothing. Relaxing the guard
would trade a real safety property for one app's convenience.

The read-screen fallback cannot help here either, because it runs at extraction
time and the run never gets that far.

## The actual fix

Screen-state identity needs a visual component when screen reading is enabled,
so two different calculator states stop hashing the same. The constraint that
makes this safe is the existing one: recognized text may contribute to *whether
the screen changed*, while still never becoming an action target and never
entering decision context as a prior fact. That separation is what keeps OCR
from silently steering actions.

Cost matters: a capture per observation is roughly a second, so this belongs
behind the same explicit `--read-screen` gate and probably only after a
suspected no-op, not on every step.

## Scope

Expect this on any app whose state is drawn rather than published: calculators,
canvases, games, drawing tools, media scrubbers. Apps that publish state through
accessibility -- Settings, Notes, Clock, Contacts, Mail -- are unaffected, which
is why the Settings suite runs 20/21.

Not yet established: whether the visual fingerprint alone is enough to unblock
Calculator end to end. That needs the change built and re-measured.

## Implemented (offline)

Screen-state identity now carries an optional visual component, used only when
`--read-screen` / `Agent(visual=...)` is configured and a suspected no-op is
detected (unchanged AX `content_fingerprint` after an action).

- `visual_observation.visual_fingerprint` hashes the frame's image hash with the
  already confidence-gated recognized texts. Change detection only: never an
  action target, never a prior fact in `decision_context`, never content
  authority for a private field.
- `state.same_screen` refines identity only when both sides have a visual
  fingerprint. A missing component falls back to AX-only comparison, so an
  unavailable capture cannot claim two AX-identical screens differ (which would
  relax the no-op replay guard).
- `agent.py` captures once after a suspected no-op (never per frame, never
  before the first no-op). A proven visual change marks the action as progress
  and does not record a blocked pair. A proven visual no-op, or a
  missing/refused/stale/blank capture, falls back to today's AX-only block.
  The first attempt of a run has no before identity, so it cannot prove
  ineffectiveness and is not recorded -- recording it would also refuse a later
  digit append ("55") that shares the after identity.
- The action guard's "same screen + same action = no-op replay" rule still holds
  when the drawn screen also did not change.

Offline tests pin the contract (14 new). `calculator_task` stays `known_gap`:
live end-to-end Calculator with `--read-screen` has not been re-measured on a
booted phone in this change.
