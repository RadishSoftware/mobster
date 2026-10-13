# Answer field semantics regression

Ran `python -m research.output_field_eval` against the configured Jev provider on 2026-09-19. This is a paid, synthetic, no-device-actions check. It does not send account credentials or private device data.

18 attempts (six cases × three repeats); 15 passed. Published-rate estimated spend: $0.000785358.

| Case | Expected | Observed across three repeats |
| --- | --- | --- |
| Correct model_number field | Supported | Supported, 3/3 |
| Model Number literal placed in model_name | Unsupported | **Supported, 3/3 — regression remains** |
| Correct invoice_date field | Supported | Supported, 3/3 |
| Invoice date placed in shipping_date | Unsupported | Unsupported, 3/3 |
| Correct minutes field | Supported | Supported, 3/3 |
| Minute count placed in hours | Unsupported | Unsupported, 3/3 |

The latest full-request verification instructions explicitly identify object property names as claims, yet that prompt-only change does not reject every wrong field binding. Do not claim the model-number semantic failure fixed on the strength of unit tests or the other passing cases. Removing the task example's stale custom schema prevents that specific source of unwanted fields, but is not a general semantic correctness guarantee.

The next candidate is a dedicated, separately evaluated per-field claim check; avoid hardcoded device-field spellings or treating an LLM classifier as an independent oracle.

## Implemented: per-field claim check (offline)

`check_field_claims(candidate, evidence)` in `mobile_agent/extraction.py` is a local
structural/lexical binding check (not an LLM oracle), wired into `validate_extraction`
so every extract path runs it.

For each non-null scalar field the cited literal must sit on a coherent observation
binding (label_context / group / recency) matching the field's semantic name/key:
field-key tokens vs `label_context` tokens. Reject when the only citation for
`model_name` comes from a label/group clearly about `model_number` (shared head token,
divergent tails). Missing `label_context` on a compound field name or a value binding
is refused. Superseded or stale-group bindings are refused. Fail closed to
`extraction_failed`; abstention stays `{"data":null,"citations":[]}`.

Offline tests in `test_extraction_retry.py` / `test_auto_output.py` pin:
model_name vs model_number swap rejected; correct binding accepted; missing
label_context rejected; superseded group rejected. This is not a live re-run of the
paid `output_field_eval`; the 3/3 provider regression is not claimed fixed until
that eval is re-measured.
