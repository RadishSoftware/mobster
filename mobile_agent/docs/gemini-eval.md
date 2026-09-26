# Gemini helper evaluation

Measured September 19, 2026 against Google Vertex AI's global endpoint using the operator's existing gcloud credentials and explicitly selected existing project. No API keys, OAuth tokens, account identifiers, or real app data are included here.

## Selection

Use `gemini-3.5-flash-lite` for this workload. It was the fastest model among those passing every corrected fixture. This is a small local comparison, not a universal provider/model ranking or a production reliability guarantee.

| Model | Passed | Successful median | Successful p95 | Failed attempts |
| --- | ---: | ---: | ---: | --- |
| Gemini 2.5 Flash-Lite | 11/16 | 515.7 ms | 679.0 ms | 3 validation errors, 2 transport errors |
| Gemini 3.1 Flash-Lite | 16/16 | 719.9 ms | 1073.7 ms | None |
| Gemini 3.5 Flash-Lite | 16/16 | 592.0 ms | 747.2 ms | None |
| Gemini 3.5 Flash | 14/16 | 1026.7 ms | 7695.5 ms | 2 validation errors |

Fast failed requests are excluded from the successful latency columns. All-attempt medians were 505.2, 719.9, 592.0, and 1026.7 ms respectively. With at most 16 successful samples per model, the nearest-rank p95 is the maximum successful sample; it is not a stable tail-latency estimate.

## Method

The corrected run made exactly 64 sequential requests: four models, eight fixture cases, and two repeats. Model order rotated between repeats. Each model reused its HTTP connection; initial connection establishment is included in its first request, while initial gcloud authentication was performed before timing. Reported duration includes network and local contract validation, not just model generation. Gemini 2.5 used `thinkingBudget:0`; Gemini 3 models used `thinkingLevel:MINIMAL`. No failed request was retried.

The cases cover quoted and natural-language search queries, append-only text completion, Unicode input, a bounded navigation plan, literal-grounded structured extraction, explicit abstention when a required value is missing, and resisting an instruction embedded in observed text. These are synthetic fixtures, not successful TikTok tasks.

The harness used 300 output tokens for navigation/text and 2500 for extraction, JSON-object mode, and strict local validation. Provider-native JSON-schema enforcement is not claimed. Model output must match the requested data schema and cite exact observed literals; those checks do not establish semantic completeness or a real-world task outcome.

## Why the first comparison was discarded

An earlier 96-request evaluation used ambiguous citation instructions. Six additional synthetic diagnostic calls showed that Gemini 3.1/3.5 Flash-Lite could return the correct facts with paths such as `/data/title`, while Mobster expects pointers relative to the data value, such as `/title`. The prompt now explicitly defines this distinction with examples.

The old missing-data fixture also incorrectly expected nullable success from a non-nullable schema. Now only a parsed, exact `{"data":null,"citations":[]}` response is explicit abstention. Other schema/evidence failures still fail. Runtime abstention becomes `data_status:insufficient_evidence`, with `schema_validated:false`; it is never counted as extracted data. Generic validation failures were not retroactively relabeled as passes.

Known evaluation/probe calls total 170: four initial availability probes, 96 original fixtures, six diagnostic requests, and 64 corrected fixtures. The corrected 64 samples are preserved in [the JSONL record](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/evals/gemini-2026-09-19.jsonl). Their values were transcribed from tool-returned records and retain execution order, model, case, repeat, latency, and pass/error outcome. No further evaluation calls were made after selecting the winner.

## Operational safeguards and next evaluation

The adapter sends credentials only to fixed Google API hosts, retains tokens only in memory, bounds input/output and authentication time, requires a single naturally completed text candidate, ignores private thought text, rejects malformed metadata, and performs no automatic retry. The additional token cache is five minutes, not an assumed fresh one-hour expiry; a 401 invalidates it for the next request. `configured` indicates configuration presence, not a successful authentication or model-availability health check.

A broader follow-up should use held-out real AX traces with sensitive fields removed, more repeats across time windows, explicit latency/cost budgets, and separate navigation, recovery, extraction, and adversarial quality scores. Transport failures and billing usage must remain separate from semantic failures; the current accepted-response usage records are not an exact billing ledger.
