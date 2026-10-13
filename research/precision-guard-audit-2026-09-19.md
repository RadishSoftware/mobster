# Prospective-action precision guard — 2026-09-19

## Saved-run diagnosis

Read-only inspection of run `339f38171e05` (`Set an alarm for tomorrow at 12pm`) found:

- Native adjustable actions did change the intended hour: 5 → 6 → … → 12. Crossing 11 → 12 also changed PM → AM; the following decision changed that component back to PM.
- The minute remained **37** throughout. The first commit occurred at **12:37 PM**, then another Add flow committed **9:37 PM**. These are not approximate successes.
- Target-choice descriptions used `label or role`; all three unlabeled wheels therefore had identical `UIAccessibilityPickerComponent` descriptions, despite distinct values in the full snapshot. Action history also omitted those values.
- The existing final completion-consistency gate rejected DONE with goal probability .29, but that was after two incorrect persistent effects. Final verification cannot undo an earlier wrong commit.

No user alarm was changed, deleted, disabled or replayed during this investigation.

## Changes

1. Target criteria retain label, role, **current value** and supported actions. The original stable IDs remain local and compact aliases are restored only after response validation.
2. Bounded history retains target before/after values and up to eight coupled field changes. Identity matching requires source, bundle, ID, locator, role and label agreement; OCR IDs without stable locators cannot establish value transitions. Missing matches remain unknown, not fabricated changes. Truncation is explicit.
3. Every proposed TAP/TYPE has a separate request-bound `verify_action` call using Jev, not Gemini. It receives the exact selected target, exact generated append text, current screen, original request, last eight actions and their truncation marker. Typed verdicts are `allowed`, `mismatch`, `unclear`.
4. Malformed or tied permissions fail closed. A mismatch is latched for the same unchanged state/action/text and gets one bounded re-decision opportunity; it cannot be reapproved stochastically without state changing. Unclear stops without dispatching that action. Prior actions are not represented as rolled back.
5. Screen identity/revision/value is refreshed again after the verifier. Cancellation, elapsed budgets, observation errors and stale decisions cannot dispatch the pending action. Existing stop conditions, output intent, answer-verification gates and completion thresholds are unchanged.
6. Request time is captured once with an explicit host-clock source and timezone offset. It is diagnostic, **not** authority for the user's/device's relative date. Unknown date/time relationships must remain unclear.
7. Provider telemetry uses `action_verification`, independently counted as `jev_action_verification`; Activity presents the new `action_check` boundary in plain language.

## Verification and limitations

- **436 Python tests passed** at the implementation checkpoint, including focused precision-boundary tests. Independent review reproduced and then verified fixes for stochastic reapproval of a rejected action and falsely joining recycled IDs.
- Mocked tests prove dispatch behavior, freshness, cancellation, bounded history, target description and exact-text binding. They do **not** prove Jev's semantic judgment.
- `research.action_guard_eval`: 17 deidentified positive/negative cases × 2 repeats, maximum 34 provider calls, zero device actions. Root agent owns the paid invocation and result assessment.
- `research.picker_decision_eval`: nine synthetic decisions checking minute selection, AM/PM selection and exact-noon commit readiness. Root agent owns invocation.
- For model evaluations, distinguish unsafe acceptance (a negative case returns `allowed`) from conservative disagreement (`mismatch` vs `unclear`) and liveness failure (a valid case is rejected).
- This new check covers **TAP/TYPE only**. Native INCREMENT/DECREMENT and swipes retain the original one-decision path. Immediate side effects of those operations are not protected by this new prospective gate.
- Last-eight history plus an omission marker is not a durable effect ledger. This is not a guarantee against all duplicates, and the model is not an independent oracle.
- No production-readiness or SOTA claim follows from this patch. The actual provider evaluation and coordinated end-to-end device evaluation remain necessary before claiming reliable task fulfillment.

## Actual Jev evaluation — root-run checkpoint

The root agent ran `research.action_guard_eval` against Jev: **34 attempts, 26 exact verdict matches, estimated USD $0.001745856, zero device actions**.

| Case group | Observed result |
| --- | --- |
| Correct exact-noon, preparatory Add, edit-existing, quantity, units, recipient and append | All allowed (14/14 positive cases). |
| Wrong minute/hour/AM, quantity, unit and recipient | All blocked (12/12 negative cases). |
| Unknown relative date relationship | `mismatch` instead of expected `unclear` (2/2): conservative classification disagreement, no dispatch permission. |
| Add again after prior commit | **Unsafe `allowed` 2/2.** The model did not reliably enforce duplicate-effect policy. |
| Unknown prior action outcome | **Unsafe `allowed` 2/2.** Agent already stops after uncertain native dispatch; direct verifier callers now also abstain deterministically before the provider. |
| Append text already in the field | **Unsafe `allowed` 2/2.** Agent already rejects this locally; the verifier now also returns mismatch deterministically before the provider. |

These results prove that the model guard must not be treated as an oracle. The two new local verifier checks have offline regressions; the provider's original wrong judgments are not rewritten as passes. The eval script now distinguishes provider decisions from deterministic decisions, reports unsafe acceptances separately, and does not assign a previous call's cost/timing to a local short-circuit.

The **repeat-Add semantic failure remains open**. Do not claim this patch guarantees no duplicate objects. A durable, request-bound effect ledger or stronger validated action contract is needed; guessing from app-specific button text or blindly banning all repeated navigation would not be a correct generalized fix. No already-created user alarm was changed to conceal this failure.

## Actual picker-decision evaluation

The root agent ran `research.picker_decision_eval`: **9/9 correct selections**, estimated **USD $0.001473444**, zero device actions. Across three repeats each, Jev chose the minute wheel for 12:37 PM, the AM/PM wheel for 12:00 AM, and the commit control for 12:00 PM. This supports the value-rich target-description change on those synthetic states; it is not a full device-task success rate.

## Calculator run `7e254520b574`

The saved trace contains the correct four input actions: 5 → Multiply → 5 → Equals. It then contains **26 consecutive WAIT decisions** on the same snapshot, zero helper calls, and `max_steps`. The AX tree exposes `Last Expression` and `Edit field` as SwiftUI nodes with empty values; neither the expression nor result 25 appears in observed evidence. Returning a computed 25 as if it had been read from the app would be dishonest.

The agent-side defect was that WAIT bypassed the existing blocked-action recovery path. After three unchanged waits, the loop now rechecks the snapshot/guard boundary and spends at most one available recovery call per unchanged screen (preserving the reserved answer budget). Without a helper budget it supplies a bounded diagnostic hint instead. Repeated waits back off rather than hammering inference; real asynchronous loading remains bounded by the existing run duration/step limits, not a new arbitrary short timeout. No completion gate is bypassed and no answer is fabricated.

The native SwiftUI value omission is a separate unresolved observation issue, handed to the root/native owner. The WAIT change alone must not be described as making the Calculator task succeed.

## Implemented: durable request-bound effect ledger (offline)

`mobile_agent/effect_ledger.py` exposes `EffectLedger.record_intent(op, target_id, text)`,
`.would_duplicate(...)`, `.record_outcome(...)` for the agent loop to call around every
persistent side-effect dispatch (TAP that committed a form, TYPE append, INCREMENT/DECREMENT).

Policy: the same logical effect on the same run is never dispatched twice when the prior
outcome is acknowledged/unknown (in-flight `intent` counts as unknown). Ambiguous outcomes
never auto-retry; only a pre-dispatch refusal (`not_dispatched` / `failed_pre_dispatch`)
releases the slot. Swipes/WAIT/DONE/BLOCKED are not persistent and are never blocked here.
Not global: one ledger per run.

Offline tests in `test_evidence_context.py` pin: duplicate Add refused; unknown prior
outcome blocks retry; distinct fields allowed; TYPE same-text refused; per-run not global.
`agent.py` still uses its local `blocked_pairs` / `typed_fields`; wiring the loop to this
ledger is left to the agent owner. The model is still not an independent oracle.
