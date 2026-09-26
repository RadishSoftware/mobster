# App adaptation: evidence, architecture, and release gates

Research review: 2026-09-19. This is a design and evaluation plan, not a claim
that Mobster currently learns across tasks or is state of the art.

## Research that changes the design

| Primary source | Relevant finding | Mobster decision |
| --- | --- | --- |
| [AndroidWorld-Generalization, March 2026](https://arxiv.org/abs/2603.07432) | Unseen instances, templates, and apps are different tests; the reported RL improvements are much smaller on unseen apps. | Hold out entire apps and workflow templates, not just prompt wording. Android scores are not evidence of iOS performance. |
| [CoAdapt-GUI, August 2026](https://arxiv.org/abs/2608.11588) | Separating transferable procedures, failures, and verification rules from app-bound state improves unfamiliar-app adaptation; its full method also updates policy adapters. | Store verified procedures separately from screen IDs and coordinates. Hosted Jev API use does not imply access to weight updates or implement that paper's training method. |
| [MobileUse, July 2025](https://arxiv.org/abs/2507.16853) | Reflection on demand and task-guided exploration address recovery and unfamiliar environments. | Keep fast decisions as the common path; invoke the helper for a demonstrated ambiguity or lack of progress. Exploration remains inside user authority. |
| [MobileExplorer, May 2026](https://arxiv.org/abs/2605.26546) | The paper reports lower latency through parallel exploration with explicit rollback mechanisms. | Do not copy concurrent speculative taps onto one real user device. Parallelize read-only reasoning; device mutations have one owner. App side effects are not generally reversible. |

These are heterogeneous research results, not a comparable leaderboard or a
guarantee that combining their ideas improves this implementation.

## Current implementation and gaps

Mobster has an AX-first normalized snapshot, bounded execution, persistent run
history, a Jev-first decision path, and on-demand Gemini text/extraction/recovery. Journaling is
not learning: each task currently starts a new agent and only sees its own
recent actions. Framework-named mock nodes do not prove React Native, Expo, or
SwiftUI coverage. A catalog entry does not prove an app can be controlled.

A strict whole-snapshot revision check can reject a valid target when
unrelated screen content changes. Pre-dispatch rejection and unknown
execution outcome also share a terminal error path. The safe remedy
is explicit outcome and target contracts, not weakened freshness checks or
automatic replay.

## Intended control architecture

1. **Observe:** authenticated app identity, bounded semantic AX nodes,
   capability declarations, modal ownership, and observation revision. Use
   event-driven invalidation plus a bounded recovery snapshot. AX gaps remain
   explicit; selectively request vision only for otherwise unreadable content.
2. **Decide:** generate legal choices from adapter capabilities. Jev chooses
   the next operation; Gemini supplies text, constrained extraction, or a
   recovery hint. Share authority rules, not contradictory output/role prompts.
3. **Execute once:** one device owner, atomic target precondition check, and
   one outcome: rejected before dispatch, acknowledged, or unknown. Only a
   proven rejection can lead to re-observation and a newly grounded decision.
4. **Verify:** inspect the resulting state and test explicit task
   postconditions. A second model agreement or literal citation is not an
   independent success oracle. Keep current-screen evidence available even
   when historical evidence reaches its capacity.
5. **Remember:** after verified outcomes, retain a small procedure with its
   app/version/locale scope, preconditions, postconditions, failure evidence,
   and provenance. Retrieve it as an untrusted hint, never an executable macro.
   Re-ground every control on the current screen. No cross-user private data,
   credentials, or raw account content belongs in shared memory.

Memory, target-level native preconditions, and the broader capability contract
above are not implemented yet. Introduce them only with falsifiable tests and
an ablation against the simpler baseline. Avoid a vector database, extra agent
layers, or policy training until measured retrieval or model limits justify it.

## Evaluation and release gates

Use independent read-only tasks first across actual UIKit, SwiftUI, React
Native/Expo, and embedded web/custom-rendered apps. Separate cold unseen-app
performance from warm remembered performance. Pin app/OS/model versions and
restore known test state between attempts without resetting a user's phone.

Measure independently verified success, false-success rate, prohibited side
effects, recovery rate, p50/p95 time to first action and task completion,
provider latency, AX/transport time, and reported USD/token usage. Compare
AX-only, selective vision, no-memory, and verified-memory variants under the
same task and budget conditions. Include modal/locked-screen, stale target,
network loss, cancellation, app update, and lost-acknowledgment cases.

A demo release needs a real dashboard task with observed correct output,
stable live video, safe cancellation, durable history, and truthful costs.
Passing unit tests alone does not satisfy this gate. Universal app automation,
autonomous weight learning, and SOTA performance must not be advertised until
the relevant experiments substantiate them.

## Hybrid Jev + LLM control plane (2026-09-21)

The intended Decide step is now an explicit two-path policy in
`mobile_agent/hybrid.py`.

1. **System One only for action authority.** Jev (`/systemone`, `jev-latest`)
   is the sole path for operation/target selection, WAIT/DONE/BLOCKED,
   stop_gate, action_verification, and output verification. A helper call on a
   decision-class mode is a hard error (`assert_helper_mode`).
2. **System Two for language and retrieval work only.** The small helper
   generates TYPE text, schema-constrained extraction, recovery hints, and an
   optional once-per-task plan. It never chooses taps or targets.
3. **One-shot escalation, then Jev.** Low-confidence demotion, blocked Noul,
   WAIT plateau, or unclear output intent may spend one helper recovery/plan
   call. After that hint, Jev decides again. Device mutations still have one
   owner; parallelize read-only reasoning only.
4. **Speculative fan-out is free capacity, not a second brain.** Typed Noul/Score
   questions ride in the same Jev request as the operation choice (measured
   8 questions / 1 round-trip on a typical TAP screen). Malformed speculative
   answers stay None and never block a decision.
5. **Budget isolation.** Helper navigation budget may reserve one extraction
   slot; Jev call volume is not capped by the helper budget. Path cost totals
   (`costs.path_cost_totals`) keep System One and System Two USD separate.

Constraints from this document remain in force: no vector database, no extra
agent layers, and no policy training until measured retrieval or model limits
justify them. Multi-app recovery may propose "open another app" as a milestone
hint when the user goal requires a fact there; that hint is never authority and
never replaces re-grounding controls on the current screen.
