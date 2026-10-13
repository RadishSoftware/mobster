# Hybrid Jev + LLM architecture: eval harness and measured claims

Design date: 2026-09-21. This document records the architecture, the offline
invariants that must not regress, and **real numbers** from stored paid eval
fixtures and the offline unit suite. It is not a claim of universal SOTA.

## Architecture (hard split)

| Path | Owner | Allowed work | Forbidden |
| --- | --- | --- | --- |
| **System One** | Jev / TypeSafe `jev-latest` | `decide`, `verify_action`, `verify_output`; WAIT/DONE/BLOCKED; stop_gate; output_intent | helper text |
| **System Two** | small LLM helper (Gemini 3.5 flash-lite / OpenRouter) | `text`, `plan` (optional once-per-task), `recovery`, `extract` / `extract_auto` | operation/target selection, taps, stop_gate, action verification |

Implementation: `mobile_agent/hybrid.py` (`HybridRouter`, `escalation_reason`,
`fanout_stats`, `hybrid_totals`), `models.Jev` (batched typed questions),
`models.Helper` (mode-checked), `agent.Agent` (one recovery escalation on
low-confidence demotion, then Jev).

### Escalation (System Two, then System One)

Fires at most **once per run** (`ESCALATION_MAX_PER_RUN = 1`) for:

1. `low_confidence` — action demoted to WAIT below `ESCALATION_CONFIDENCE_FLOOR`
2. `blocked_signal` — BLOCKED / blocked Noul ≥ 0.9
3. `wait_plateau` — unchanged WAIT loops ≥ 3
4. `unclear_output_intent` — policy signal; the Agent still ends the auto-output
   contract with `needs_clarification` rather than inventing an answer
5. `multi_app_milestone` — optional plan to open another app for a fact the
   user requested; a suggestion, never authority

After the hint, Jev owns the next decision. **Never LLM-chosen taps.**

### Speculative fan-out

One Jev call batches operation + per-op targets + goal Noul + blocked Noul +
stop_gate + optional output_intent + speculative `screen_has_loading` Noul +
`side_effect_risk` Score. Prefer this single typed round-trip over multiple
calls. Optional Noul/Score never break `decide()` (malformed → None).

Measured offline on a typical TAP screen with `classify_output=True`:
**8 questions / 1 round-trip** (`operation`, `tap_target`, `goal`, `blocked`,
`stop_gate`, `output_intent`, `screen_has_loading`, `side_effect_risk`).
Dense multi-target screens add one target question per legal operation group.

### Hard constraints (mobile_agent/docs/adaptive-architecture.md)

- No vector DB, no extra agent layers, no policy training until measured limits
  justify it.
- Device mutations have one owner. Parallelize read-only reasoning only.
- Multi-screen `observation_history` keeps label/value/group/recency; historic
  facts are never action targets.

## Offline suite (this change)

```
mobile_agent/.venv/bin/python -m unittest discover -s mobile_agent -t . -q
```

| Cutoff | Tests | Result |
| --- | ---: | --- |
| Before hybrid work | 543 | OK |
| After hybrid work | **569** | OK |

New coverage (existing modules only): helper never asked to decide/verify;
router decide/verify reach System One only; low-confidence escalation recovers
once then returns to Jev; loading demotion does not spend escalation; helper vs
Jev budget isolation; speculative Score/Noul never break decide; fan-out stats;
path cost totals match inference events; `benchmarks.hybrid_report`.

## Accuracy / cost / latency from stored eval fixtures

These are **prior paid runs** kept in `research/*-initial.json` (and the
semantic extraction JSON). They were not re-executed for this document (no
`TYPESAFE_API_KEY` / Vertex key in this offline session). Live re-runs of
`research/task_policy_eval.py`, `research/action_guard_eval.py`,
`research/extraction_eval.py`, and `research/terminal_output_eval.py`
**require provider keys** and must not invent scores.

| Fixture | Cases | Calls / attempts | Pass or correct | Errors / unsafe | Median ms | p95 ms | Published-rate USD |
| --- | ---: | ---: | --- | --- | ---: | ---: | ---: |
| task_policy_eval (gate) | 50 × 3 | 150 | **147/150 gate (98.0%)** | 0 errors, **0 unsafe_continue** | 210.1 | 281.03 | 0.01354021 |
| task_policy_eval (intent) | 50 × 3 | 150 | **141/150 intent (94.0%)** | gate_confusion: 3 continue→unclear | — | — | (same run) |
| extraction_eval (literal) | 12 × 2 | 24 | **19/24 (79.2%)** | 2 fabricated/incorrect | 664.53 | — | 0.0061295 |
| extraction_eval (semantic) | 14 × 2 | 28 attempts / 48 provider calls | **20/28 (71.4%)** | 2 fabricated/incorrect | 833.62 | — | 0.007970302 |
| terminal_output_eval | 6 × 2 | 12 | **12/12 (100%)** | 0 | — | — | 0.000486276 |
| action_guard_eval | 17 × 2 | max 34 | **requires key** | — | — | — | — |

Gate confusion (task_policy, n=150): stop→stop 42, unclear→unclear 21,
continue→continue 84, continue→unclear 3. Unclear is fail-closed, not unsafe.

### Hybrid cost model (published rates, 2026-09-19)

From the task_policy fixture run: 150 Jev calls ≈ **$0.0135** total ≈
**$0.00009 per decision** (TypeSafe input-only pricing). Helper recovery/text
is optional and capped (`max_helper_calls`, extraction reserve). Path isolation
is enforced: helper spend never sits on the Jev ledger and Jev is not capped by
the helper budget (`HybridRouter.navigation_budget`).

## Architecture metrics harness

```sh
# Offline over a run export (no device, no model calls):
mobile_agent/.venv/bin/python -m mobile_agent.benchmarks --runs downloaded-run.json
# or in code:
from mobile_agent.benchmarks import hybrid_report
hybrid_report(unwrap_runs(export))
```

`hybrid_report` returns decisions/task, Jev calls/task, helper calls/task,
cost/task USD, System One vs System Two tokens/latency/USD, p50/p95 decision
latency, and fan-out questions/batch. It never invents accuracy: independently
verified runs stay separate from model-claimed completion.

Optional paid replay (keys required; the `research.*` eval scripts are not published):

```sh
# TYPESAFE_API_KEY required
mobile_agent/.venv/bin/python -m research.task_policy_eval --repeats 3
mobile_agent/.venv/bin/python -m research.action_guard_eval
# Vertex / TEXT_MODEL_* required
mobile_agent/.venv/bin/python -m research.extraction_eval
mobile_agent/.venv/bin/python -m research.terminal_output_eval
```

## iOSWorld-informed precision notes

Published iOSWorld-style SOTA remains weak on multi-app and memory tasks
(order of 52% overall / 37% multi-app in the literature cited by product).
Mobster's answer to that bottleneck is **not** extra agent layers:

- Keep multi-screen fact recall in `observation_history` (label/value/group/
  recency; superseded flags).
- Allow helper **plan/recovery** to propose one milestone to open another app
  when the user goal requires a fact there — as a hint only.
- Re-ground every control on the current screen; never treat history as a
  tappable target.

Unmeasured here: real multi-app iOSWorld tasks, physical device success rate,
and any claim that hybrid routing beats a pure-Jev baseline on those tasks.
Those need a device-backed eval with independent oracles (mobile_agent/docs/adaptive-architecture
release gates).

## Uncalibrated thresholds (disclosed)

| Constant | Value | Status |
| --- | ---: | --- |
| `ACTION_CONFIDENCE_FLOOR` (nav/type/side) | .55 / .65 / .85 | uncalibrated, fail closed |
| `LOADING_WAIT_THRESHOLD` | .65 | uncalibrated |
| `SIDE_EFFECT_RISK_FLOOR` | .5 | uncalibrated |
| `ESCALATION_CONFIDENCE_FLOOR` | .55 | aligned to nav floor; uncalibrated |
| `ESCALATION_WAIT_LOOPS` | 3 | matches Agent plateau trigger |
| `ESCALATION_BLOCKED_NOUL` | .9 | matches Agent blocked recovery |
| `ESCALATION_MAX_PER_RUN` | 1 | policy choice; not measured optimal |
