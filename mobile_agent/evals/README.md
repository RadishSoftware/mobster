# Report offline Automate and Test evidence

Run this source-checkout module with Python 3.12 against existing artifacts. It reads files only; it does not run an
agent, model, app, simulator or phone.

```sh
python -m mobile_agent.evals.offline_report \
  --bench .mobster/runs/research/bench-runs/<run> \
  --development <paired-development-run> \
  --development-manifest <manifest.json> \
  --out evaluation.md
```

You may repeat `--bench` and `--development`. The report keeps each run separate. It never pools the benchmark's
automation tasks with paired development bugs, even when a development case has the `automation` family label. You
get no aggregate quality or world-best score.

For a bench run, provide `plan.json` and `records.jsonl` from `mobile_agent.bench run`. The report checks every record
against the planned repeat, task, agent and order. Infrastructure records may precede one terminal record for the
same planned slot; they stay visible as retries but do not increase its denominator. A second terminal record or an
unplanned record stops reporting. Missing, skipped, infrastructure and ungraded slots stay in the planned denominator.
The conditional graded rate uses only pass and fail slots and is printed separately. The first scheduled repeat and
later independent repeats are shown separately. A later bench repeat is not an adaptive recovery attempt.

The plan's `suite_hash` is checked against the corresponding checked-in manifest for `v1`, `heldout`, `h2` and `h3`.
Only a matching manifest that the plan also declared frozen receives a registered-corpus label. A training, hard,
combo or unknown suite is marked unverified. The registered label does not certify that the current model never saw
or tuned on those tasks. The report includes the plan, records, suite, selected task list, configuration, device ID
and truth digests where present. It separates planned model/configuration from observed agent-run model/configuration,
including the coverage and any variation across observed runs. A device ID digest avoids copying a raw device
identifier into the report. Plan metadata is a provenance claim, not proof of an actual build or device observation.

For a paired development run, provide `claims.json`, `oracle.json` and the referenced evidence files. The report
calls the canonical `mobile_agent.evals.development.score` validator against the manifest you pass with
`--development-manifest <manifest.json>`. First-attempt detection, later recovery,
unproved and disproved success claims, reproduction and verified-fix counts come from that scorer. These files can
record build and configuration digests but cannot authenticate who produced the oracle evidence.

Agent time is the bench runner's recorded `agent_ms`; reset and grading are outside that clock. Priced dollars are a
subtotal of recorded telemetry, with coverage and unpriced call counts beside it. A missing value is reported as
not recorded; it is not zero. Blocked interventions come from recorded `unsafe` events, not human approval counts.
The paired development scorer has no time, cost or intervention fields, so the report says not recorded for them.
