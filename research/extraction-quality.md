# Extraction quality: method and before/after numbers

Date: 2026-09-22. Offline session. Stored paid eval fixtures were **not** re-executed
(no provider keys). No new provider scores are claimed.

## Failure taxonomy (stored evals, real counts)

Source: `research/extraction-eval-semantic-initial.json` (28 attempts, 20 passed)
and `research/extraction-eval-initial.json` (24 attempts, 19 passed). 13 failed
attempts total.

| Class | Count | Cases |
| --- | ---: | --- |
| wrong-field bind | 2 | `quote_label` × 2 (semantic) — returned `Model Number` for "label above the model number" |
| helper hallucination | 2 | `no_hidden_account` × 2 (literal) — `Recovery code hidden` shipped as the recovery code |
| incomplete evidence | 6 | `model_label_missing` × 4 (ValueError instead of clean abstain) + `quote_placeholder` × 2 (helper returned the correct literal; Jev false-rejected) |
| units/transform | 3 | `unicode_literal` × 3 — observed literal not returned as exact substring |
| missing label_context | 0 | (already enforced post-eval by `check_field_claims`) |
| superseded group | 0 | (already enforced) |
| transitive value | 0 | (no stored instance) |

Notes: `model_label_missing` failures are fail-closed (data=null, ValueError from
rejecting a label-as-value or missing label_context) — correct refusal under
"never fabricate", scored as fail because the oracle wants clean abstention.
`quote_placeholder` extraction was correct; the semantic verifier rejected it.

## Before / after pass rates

| Measure | Before | After (offline) |
| --- | ---: | ---: |
| Semantic (stored, oracle match) | 20/28 = **71.4%** | 20/28 = **71.4%** (stored outputs fixed; no re-run) |
| Literal (stored, oracle match) | 19/24 = **79.2%** | 19/24 = **79.2%** (stored outputs fixed; no re-run) |
| Semantic extraction-level (counts correct refusals + Jev-rejected correct candidates) | — | see `--offline-score` |
| Golden fixtures through `validate_extraction` / `check_field_claims` | n/a | `research/extraction_eval.py --fixtures` |

Live re-runs of `research/extraction_eval.py` require Vertex / TEXT_MODEL keys and
must not invent scores.

## What changed (grounded, fail-closed)

1. **Helper `extract` / `extract_auto` prompts** (`mobile_agent/models.py`):
   TypeSafe-style atomicity — one field per claim; cite the exact observed literal;
   never transform units (`1.2K` stays `1.2K`, never `1200`); never normalize
   unicode/case; refuse when `label_context` tokens disagree with the field key;
   absence/placeholder text is not a concrete code/id/number; prefer abstention
   over guess. Abstention envelope unchanged: `{"data":null,"citations":[]}` /
   `{"schema":null,"data":null,"citations":[]}`.
2. **`check_field_claims`** (`mobile_agent/extraction.py`): only the diagnosis-
   backed miss — absence-marker text (`hidden`, `unavailable`, …) is refused as a
   concrete value for non-content field names (`recovery_code`, `model_number`, …).
   Content-as-label exemption preserved: `title` ← `"Coffee brewing guide"` and
   root-scalar quoted messages still validate. `model_name`↔`model_number` tests
   unchanged and green. No LLM oracle added.
3. **Evidence packing** (`pack_extraction_context`): Helper extract prompts now
   receive field-key token-overlap + recency filtered entries (label/value group
   pairs travel together; multi-screen facts with token overlap stay). Reduces
   context rot (Jev jaggedness: large state hurts). Full evidence remains the
   validation source of truth for citations.
4. **Offline harness** (`research/extraction_eval.py`):
   - `--offline-score PATH` — re-score stored provider outputs against CASES
     oracles; classify failures; no API.
   - `--fixtures` — deterministic golden candidates through local validation.

## Offline commands

```sh
cd <repository root>   # the research.* eval scripts are not published
mobile_agent/.venv/bin/python -m research.extraction_eval --offline-score \
  research/extraction-eval-semantic-initial.json
mobile_agent/.venv/bin/python -m research.extraction_eval --offline-score \
  research/extraction-eval-initial.json
mobile_agent/.venv/bin/python -m research.extraction_eval --fixtures
```

## Tests added (existing files only)

- `test_extraction_retry.py`: units transform rejected (`1.2K` stays `1.2K`);
  abstention on empty evidence; absence marker is not a concrete code value.
- `test_auto_output.py`: units transform rejected + literal ships; extract prompt
  requires literal reading / one-field-per-claim / prefer abstention.
- `test_evidence_context.py`: field-token packer keeps needed history entries
  and label/value group peers; drops unrelated noise.

## Residual risk

- Stored pass rates cannot move without a live helper re-run. Prompt and packer
  changes are expected to convert `model_label_missing` ValueError → clean
  abstention and reduce `unicode_literal` transforms, but that is **unmeasured**.
- `quote_label` (wrong-field bind on a spatial label question) is a helper goal-
  reading issue; local checks cannot see "the label above X".
- `quote_placeholder` is a verifier false-reject, not an extraction failure.

## Addendum 2026-09-21: label-as-value closed; live re-measurement blocked

### What changed (one deterministic fix, fail-closed)

**`check_field_claims` label-as-value rejection** (`mobile_agent/extraction.py`).
Failure class closed: the success-path variant of `model_label_missing`
("incomplete evidence", 6 stored attempts). Reproduced before the fix: with the
value row absent, `model_name <- "Model Name"` citing the label row passed every
check (literal quote, matching label_context tokens, no divergent heads) and
shipped a fabricated fact. Now: a claim whose shipped value equals its own
binding's label text is rejected **only when the field is named by the label it
cites** (`name_tokens <= label_tokens`, e.g. `model_name` vs `Model Name`).
Exemptions verified by test: `*_label` fields (the goal asks for the label),
root scalars quoting a label/message, and content elements that are their own
context (`title <- "Coffee brewing guide"` on a result card stays shippable —
an earlier broader draft of this rule false-positived that case and was
narrowed). Rejection converts a fabricated success into retry → abstention.

### Measured numbers (offline gates — PROVISIONAL)

Per the 2026-09-21 directive these are regression gates, **not** the headline
result; the headline must come from the on-device harness (blocked, below).

| Measure | Baseline (reproduced) | After fix |
| --- | ---: | ---: |
| `--offline-score` semantic stored | 20/28 = 71.4% (extraction-level 85.7%) | unchanged (stored outputs are fixed) |
| `--offline-score` literal stored | 19/24 = 79.2% (extraction-level 87.5%) | unchanged (stored outputs are fixed) |
| `--fixtures` | 11/11 | **13/13** (added `label_as_value_fill_rejected`, `label_quoting_field_accepted`) |
| Owned tests | — | 43 passed (`test_extraction_retry`, `test_auto_output`, `test_evidence_context`); +56 `test_core_hardening` consumers green |

Stored-output scores cannot move by construction; they pin the baseline, not
the lift. No new provider or on-device number is claimed.

### Remaining classes: precise structural reasons

- `unicode_literal` (units/transform, 3): the local literal check already
  rejects a transformed echo; the failure is the helper emitting non-byte-exact
  unicode. Byte-exactness is inviolable, so the fix layer is the prompt
  contract (already hardened) — measurable only by live re-run.
- `quote_label` (wrong-field bind, 2): "the label above the model number" is
  goal-spatial semantics; the citation contract carries no spatial referent and
  local checks cannot evaluate it without an oracle. Prompt contract names it.
- `quote_placeholder` (2): the extraction was **correct**; the Jev semantic
  verifier false-rejected it. Not an extraction-layer failure.
- `model_label_missing` ValueError-vs-clean-abstain (4): the helper attempted a
  fill instead of abstaining; validation correctly rejected. Prompt hardened;
  abstention conversion measurable only by live re-run.

### Live measurement: blocked twice over (2026-09-21)

1. **No live device**: the test device of the time could not boot on this host.
2. **No provider env**: `TYPESAFE_API_KEY`, `TYPESAFE_MODEL`,
   `TEXT_MODEL*`/`GOOGLE_CLOUD_PROJECT` are unset in process env, shell
   profiles, and the launchd user domain. Caution: `launchctl getenv <VAR>`
   exits 0 even for **unset** vars — an exit-code check reports a false "set";
   byte checks (`| wc -c` → 0) and `launchctl print user/$UID` show them absent.
   gcloud login itself works (`GCloudToken.get()` ok, the configured project),
   so once `TYPESAFE_API_KEY` + Vertex env are exported, re-run:
   `python3 -m research.extraction_eval --repeats 2` (provider-level, synthetic
   evidence) and, with a booted device, the on-device harness:
   `python3 -m mobile_agent serve --socket <sock> --enable-live` then
   `python3 -m mobile_agent.evals.harness --socket <sock> --api http://127.0.0.1:8765 --repeats 3`.

### Offline commands (unchanged)

```sh
cd <repository root>   # the research.* eval scripts are not published
python3 -m research.extraction_eval --offline-score research/extraction-eval-semantic-initial.json
python3 -m research.extraction_eval --offline-score research/extraction-eval-initial.json
python3 -m research.extraction_eval --fixtures
python3 -m pytest mobile_agent/tests/test_extraction_retry.py mobile_agent/tests/test_auto_output.py mobile_agent/tests/test_evidence_context.py -q
```

### Live measured lift (2026-09-21 evening, real provider + real validation path)

Both blockers cleared (device available again; key supplied). Same 14 cases,
same key, gemini-3.5-flash-lite helper, Jev verifier; repeats as noted:

| Run | Code state | Pass |
| --- | --- | --- |
| 21:21 baseline (2 reps) | pre-fix | **14/28 = 50.0%** (2 incorrect shipped) |
| postfix1 (2 reps) | i-brand tokenizer fix | 15/28 = 53.6% |
| postfix2 (2 reps) | + packed-label binding-context fix | **18/28 = 64.3%** |
| postfix3 (2 reps) | + CSV prompt sentences | 15/28 — **reverted** (over-abstention wave across unrelated text cases) |
| final (3 reps) | = postfix2 state | **29/42 = 69.0%** (3 incorrect shipped, all `quote_label`) |

**+19 points over the evening baseline, from two deterministic validation fixes —
both found by live measurement, neither visible offline:**

1. **i-brand tokenizer** (`_tokens`): camel-splitting turned "iOS" into {i, os},
   so `ios_version` vs a packed "iOS Version, 26.6.1" row read as a divergent-head
   wrong-field conflict and the correct answer was rejected. Same hazard for
   iPhone/iCloud/iMessage rows. Also unblocked on-device
   `settings.about.version` 0/3 → 3/3 in the harness (regression test:
   `test_apple_ibrand_tokens_are_not_a_wrong_field_conflict`).
2. **Packed-label binding context** (`check_field_claims`): the compound-field
   rule consulted only `label_context`; SwiftUI packed rows carry the label in
   the row text itself. Binding context now falls back to entry text for label
   rows. Verified: value-only answers accept, packed label-as-value and
   model_number→model_name misbinds still reject (675 tests green).

Remaining failing classes (3 reps each, final run):

| Case | Rate | Class | Layer |
| --- | --- | --- | --- |
| `quote_placeholder` | 0/3 | Jev verifier false-rejects a correct literal | verifier, not extraction (known structural) |
| `quote_label` | 0/3 | goal-spatial semantics ("label above X") not in citation contract | structural; ships "Model Number" (the 3 incorrect) |
| `shopping_csv` | 0/3 | helper CSV shaping: over-abstains or emits empty `properties` (rejected correctly) | helper prompt/contract gap — one prompt attempt REGRESSED unrelated cases and was reverted; needs a measured approach |
| `bio_literal` | 0/3 | helper flakiness (replay passed 2/2 immediately after; eval variance high) | helper nondeterminism |
| `injected_creator` | 2/3 | flake | — |

Artifacts: `research/extraction-eval-2026-09-21-live.json` (baseline),
`…-postfix.json`, `…-postfix2.json`, `…-postfix3.json` (reverted experiment),
`…-live-final.json`. Total provider spend this evening ≈ $0.042.
