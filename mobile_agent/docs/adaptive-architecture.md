# App adaptation: evidence, architecture, and release gates

Research review: 2026-09-19. This is a design and evaluation plan, not a claim
that Mobster currently learns across tasks or is state of the art.

## Research that changes the design

| Primary source | Relevant finding | Mobster decision |
| --- | --- | --- |
| [AndroidWorld-Generalization, March 2026](https://arxiv.org/abs/2603.07432) | Unseen instances, templates, and apps are different tests; the reported RL improvements are much smaller on unseen apps. | Hold out entire apps and workflow templates, not just prompt wording. Android scores are not evidence of iOS performance. |
| [CoAdapt-GUI, August 2026](https://arxiv.org/abs/2608.11588) | Separating transferable procedures, failures, and verification rules from app-bound state improves unfamiliar-app adaptation; its full method also updates policy adapters. | Store verified procedures separately from screen IDs and coordinates. A hosted model API gives no access to weight updates, and Mobster does not implement that paper's training method. |
| [MobileUse, July 2025](https://arxiv.org/abs/2507.16853) | Reflection on demand and task-guided exploration address recovery and unfamiliar environments. | Exploration remains inside user authority. |
| [MobileExplorer, May 2026](https://arxiv.org/abs/2605.26546) | The paper reports lower latency through parallel exploration with explicit rollback mechanisms. | Do not copy concurrent speculative taps onto one real user device. Parallelize read-only reasoning; device mutations have one owner. App side effects are not generally reversible. |

These are heterogeneous research results, not a comparable leaderboard or a
guarantee that combining their ideas improves this implementation.

## Current implementation and gaps

Mobster has an AX-first normalized snapshot, bounded execution, persistent run
history, and one decision path: Mobster's agent (`frontier.FrontierAgent`), a frontier model
choosing each step from the elements it was shown. Journaling is not learning: each task currently starts a new agent and only sees its own
recent actions. Framework-named mock nodes do not prove React Native, Expo, or
SwiftUI coverage. A catalog entry does not prove an app can be controlled.

## Intended control architecture

1. **Observe:** authenticated app identity, bounded semantic AX nodes,
   capability declarations, modal ownership, and observation revision. Use
   event-driven invalidation plus a bounded recovery snapshot. AX gaps remain
   explicit; selectively request vision only for otherwise unreadable content.
2. **Decide:** generate legal choices from adapter capabilities. The model
   chooses the next operation from elements it sees by short ids, never
   coordinates it could invent. Share authority rules, not contradictory
   output/role prompts.
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

