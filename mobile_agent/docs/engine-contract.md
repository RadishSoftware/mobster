# Engine contract v1

What the agent's API and run events say about the two engines, for the dashboard and any
other client. Every field here is new and optional: a client that ignores them keeps
working, and an agent that predates them simply omits them.

- **Smart** (`smart`, the default): the frontier loop (`frontier.FrontierAgent`) on
  `gpt-5.6-sol` at reasoning `low`, with the task contract and DONE gate (Bet 1) and exact
  actuation (Bet 2). It is `engines.SMART_CONFIG`, the same config
  `bench.iosworld --policy frontier` runs. It may open any app on the phone. Key:
  `OPENAI_API_KEY`, else the helper's key when the helper provider is OpenAI.
  **On Claude** (`engines.smart_model`): a user whose only key is `ANTHROPIC_API_KEY` (or an
  Anthropic helper's key) runs the same loop on `claude-sonnet-5-5` (`engines.ANTHROPIC_SMART_MODEL`)
  through `frontier.AnthropicChat`; `MOBSTER_SMART_MODEL` picks any OpenAI or Claude model outright
  (`claude-opus-5-5`, `gpt-5.6-terra`...), and the key it needs follows the model. Everything else
  (reasoning `low`, limits, switches) stays `SMART_CONFIG`'s. With both keys and no setting, Smart
  stays on `gpt-5.6-sol`.
- **Fast** (`fast`): the Jev pipeline (`agent.Agent` with the helper). Key:
  `TYPESAFE_API_KEY`.

Mobster never runs a task on an engine the user did not choose: an unavailable engine
refuses the task with its reason.

## API

### `GET /api/status`

Adds:

```json
"defaultEngine": "smart",
"engines": {
  "smart": {"available": true, "reason": null, "model": "gpt-5.6-sol", "provider": "OpenAI"},
  // on Claude: {"available": true, "reason": null, "model": "claude-sonnet-5-5", "provider": "Anthropic"}
  "fast":  {"available": false, "reason": "Add your Jev key in Setup", "model": "jev-latest", "provider": "TypeSafe"}
}
```

- `reason` when unavailable: `Add your OpenAI key in Setup`, `Add your Jev key in Setup`
  (with Setup; `Add OPENAI_API_KEY or ANTHROPIC_API_KEY to the agent's env file` without it;
  `Add ANTHROPIC_API_KEY to the agent's env file to run Smart on <model>` when
  `MOBSTER_SMART_MODEL` names a Claude model and no Anthropic key is set), `Your OpenAI key
  can't use gpt-5.6-sol.`, `OpenAI didn't accept your key. Check it in Settings › AI account.` or
  `Your OpenAI account is out of credit. Add credit at platform.openai.com, then try again.`
  (`engines.NO_CREDIT`). The key's own refusals come from a background check
  (`GET /v1/models/gpt-5.6-sol`, once per key, again after six hours) or from a run that hit
  the error. The empty account comes from a run whose model call answered HTTP 402 or a 429
  with `insufficient_quota` or `credit_balance_exhausted` (`engines.model_failure`; a plain
  429 rate limit says nothing about the key), or from a key test that found no credit. The
  model read is free and blind to credit, so it never clears that one, however old: a key
  test that passes (it generates), a Smart run whose model calls went through, or saving the
  key does. An unchecked key counts as available. Saving the key (`POST /api/keys`, the same
  key too) forgets every verdict and checks again, and a key that passes `POST /api/keys/test`
  is available at once. The dashboard says the empty account as `Your OpenAI account is out
  of credit.` with an Add credit link to OpenAI's Billing page (composer, engine chip, Settings
  › Models with Test key, Try again with Smart). Each of those also offers Check again (Test key
  in Settings): `POST /api/keys/test {"target": "openai"}`, then Smart's status again (`/status`,
  or a fresh estimate under Try again with Smart). A test that finds the account still empty
  leaves the line as it was. Any other failure (a rejected key, or a check that couldn't run)
  shows its own message in place of the reason. In Settings the test's result shows in the
  OpenAI row.
- On Claude the same rules hold, naming the account as people know it (MESSAGING § 11): `Your Claude key can't use
  <model>.`, `Claude didn't accept your key. Check it in Settings › AI account.` and
  `Your Claude account is out of credit. Add credit at platform.claude.com, then try again.`
  (`engines.NO_ANTHROPIC_CREDIT`: a 402, or a 400 that says the credit balance is too low). The
  background check is `GET https://api.anthropic.com/v1/models/<model>`; verdicts are kept per key
  and model. Setup's AI account step takes a Claude key: `POST /api/keys {"anthropic": {"key": ...},
  "smartProvider": "anthropic"}` saves it as the key Smart uses and `POST /api/keys/test {"target":
  "anthropic"}` tests it (`GET /api/keys` adds `anthropic: {configured, keyHint, fromHelper, model}`
  and `smart: {provider, preference}`); Setup's key step counts it as done (`keys.anthropic` in the
  Setup state).
- `live_enabled` and `limitations[0]` follow the default engine. `jev_configured` keeps
  its meaning (a Jev key is saved).

### `POST /api/runs`

Takes `engine: "smart" | "fast"` (absent: the default engine). With Smart, `appId` is
optional and only says where the task starts: absent, it starts on the Home Screen and
the run's app is `{"id": "any", "name": "Any app"}`. A present `engine` is part of the
request's identity for `Idempotency-Key`.

Refusals, all `409` with `code`:

| code | when |
| --- | --- |
| `engine_unavailable` | the chosen (or default) engine can't run; `error` is its reason, `engine` names it |
| `monthly_limit_reached` | this month's spend reached the monthly limit; carries `monthUsd`, `monthlyLimitUsd` |

A dry run (`dryRun: true`) is Fast only.

### Run JSON (`GET /api/runs`, `/api/runs/{id}`)

| field | meaning |
| --- | --- |
| `engine` | `smart`, `fast`, or null (a demo replay, a run from before engines) |
| `costUsd` | published-rate spend so far, live; null until a priced call finishes |
| `estimateUsd` | the single estimate shown before the task ran (the geometric mean of its range) |
| `reviewedOk` | the user marked the result OK |
| `approval` | the pending approval, now with `kind`, `title`, `act`, `target`, `text`, `app` |

### Approvals

`POST /api/runs/{id}/approval` takes `{id, approve}` as before, plus
`{id, approve: false, instruction}` for **Decline and say what to do instead**: the
decision is `redirected`, the agent gets the instruction (1–500 characters) and the task
goes on. The instruction is never written to the run's events.

### Review and delete

- `POST /api/runs/{id}/review {"ok": true}` sets `reviewedOk` on a finished run (409 while
  it runs).
- `DELETE /api/runs/{id}` removes a finished run, its events and its frames (409 while it
  runs). Spend already recorded stays in the usage ledger.
- `POST /api/runs/{id}/gif {}` saves a finished run as a captioned GIF in `~/Downloads`
  (`run_gif.py`), named `Mobster – {the task}.gif` and never overwriting, and returns
  `{name, path, frames, bytes, seconds, mp4}`. Optional fields: `theme` (`paper` or `night`),
  `redact` (blur text fields and messages, leave out the answer) and `mp4` (an MP4 beside it
  when ffmpeg is installed; `mp4` is null otherwise). 409 `run_active` while it runs, 404
  `no_frames` when no step kept a screen, 422 `gif_too_long`, 429 `export_busy` while another
  renders, 403 `downloads_denied`.

### Frames

`GET /api/runs/{id}/frames/{frameId}` returns a JPEG no more than 480 px on its long side
(`Cache-Control: no-store`). Frame ids match `f[0-9a-f]{10}`. Use
`frameUrl(runId, frameId)` and the usual `?token=` for an `<img>`.

Frames show the phone's screen, so no copy may outlive the task: a cacheable frame would
land in the webview's disk cache (`~/Library/Caches/app.mobster.desktop`), where deleting
the task or the app's data folder never reaches it. App icons (`GET /api/apps/icon`) are
sent the same way, as their URLs name apps on the phone. Both come from loopback and are
small. A client that wants to keep them keeps them in memory.

### Costs and limits

- `POST /api/usage/estimate {goal, outputSchema?, outputFormat?, helperModel?, engine?}` returns the Fast
  planning scenario as before, plus `engine` (the one asked for, else the default) and
  `engines: {smart, fast}`, each an `EngineEstimate`
  (`{available, reason, model, provider, lowUsd, highUsd}`). `GET` returns the same for an
  empty draft. Smart's range is its estimate halved to doubled (`engines.smart_estimate`: fitted
  on 184 recorded runs of the loop, within 2x of the actual cost on 82% of held-out runs), plus
  `SMART_STRUCTURE_USD` when Smart puts its answer into a structure (an `outputSchema`, or
  `outputFormat` `csv`, `json` or `yaml` without one), so a chip shows the range: `estimateLabel(estimate)` in `engine-contract.ts` gives `3–11¢`,
  `$0.26–1.04`, or one figure (`≈4¢`) for a narrow range.
- `GET /api/usage` adds `monthUsd` (this calendar month, local time, published rates) and
  `monthlyLimitUsd`. Smart's calls are provider `openai`, or `anthropic` on Claude.
- `GET/POST /api/settings` add `defaultEngine`, `taskCostLimitUsd` (default `1.00`; null
  turns the stop off; 0.05–100) and `monthlyLimitUsd` (default null; 1–10,000). A task
  that reaches its limit ends with status `spend_cap`; Smart spends its last turn
  reporting what it found.
- `POST /api/keys/test {"target": "openai"}` is passed to `Keys.test("openai")`, and
  `{"target": "anthropic"}` to `Keys.test("anthropic")` (a free model read, then 16 tokens). A key
  whose account has no credit answers `ok: false` with `problem: "no_credit"` and an Add credit
  `link`.
- On Claude the Smart estimate is sol's fit times `SMART_COST_RATIO[model]`, the measured cost of
  that model's tasks against sol's on the same iOSWorld tasks (28 Sep, 16 tasks: `claude-sonnet-5-5`
  0.68, `claude-opus-5-5` 1.02; sol counted at $4/$20).

## Events

Smart runs emit these alongside the loop's own `frontier_*` trace. The full prompt of
each turn (`frontier_prompt`) is not kept in the app's history.

| event | fields | when |
| --- | --- | --- |
| `plan` | `line`, `apps[]`, `commits[{act, app, count, target}]` | once, before the first action. `line` is null when nothing will need an OK, e.g. `"This task will send 1 message to Sam."` |
| `contract_item` | `id`, `kind` (`read`, `write`, `commit`, `report`, `forbid`), `label`, `text` (optional), `state` (`open`, `done`, `unproven`) | at the plan, then whenever an item's state changes |
| `receipt` | `itemId`, `kind`, `quote`, `app`, `screen`, `frameId`, `step` | when the screen proves an item: a sent message, a confirmation, a value read, a write read back. A later receipt for the same `itemId` replaces the earlier one |
| `step` | `n`, `step`, `text`, `frameId`, `redact` | after each action, in plain words; `step` is the loop's turn (several actions share one). `redact` (with a frame): the normalized `[x, y, w, h]` frames of the text fields and message text on that screen, at most 24 and never their text, which a saved GIF blurs with `--redact` (`run_gif.py`) |
| `cost` | `usd`, `calls` | after each model call; `usd` is the task's spend so far |
| `inference_started` / `inference_finished` | as for Fast, `provider: "openai"` (`"anthropic"` on Claude) | every model call, with its list-price cost; on Claude a hedged call's losing twin, which is billed too, is a pair of its own with `purpose: "hedge"` |

- `contract_item.label` is the contract's own wording (`COMMIT send_message (QuickChat):
  message to Zara Okonkwo`), for Developer details. `text` is the same item for a person
  (`Send the message to Zara Okonkwo in QuickChat`, `Find the iOS version in Settings`); an
  agent that predates it omits it. A commit shown finished without a receipt of its own (a
  save whose object is on screen) is `done`, as the DONE gate counts it.
- `receipt.quote` is what the screen showed, in its own case: the exact text of a sent
  message or reply (the text approved), a confirmation row, a found value with its row's label
  when they share a row (`iOS Version 26.4`), or text typed into a field once a later screen
  shows it outside that field, in more rows than before the typing (a saved reminder, a sent
  message; the same words sent last week are not this one). Text still in a compose box, a
  draft that was declined or typed over, proves nothing. `screen` is that screen's title
  (`About`), or null when it has none.
- `step.text` says what was done: `Opened Settings`, `Tapped General`, `Typed “buy oat milk” in
  Title`, `Wrote the message` (a compose box; `Rewrote the message` the second time),
  `Searched for “Zara”`, `Scrolled to Zara Okonkwo`, `Read the Reminders list`, and
  `Read iOS Version: 26.4` when a value is found. Typing the same field again right after is one
  step with the text it ends with, so a typing step shows when the next action (or model call)
  does; it always shows before an approval is asked.

Stop (`POST /api/runs/{id}/stop`, ⌘.) ends a Smart task before its next action: the loop
checks at the top of each turn, after each model call, before each action of a chain and
between a macro's swipes, so at most the action already under way finishes. The result is
`stopped`. Stop during setup (the phone check) ends the task before its app opens and
before the first model call, for Fast as well.

Messages, pause and picking up (track harness, [harness.md](harness.md)) add these Smart events:

| event | fields | when |
| --- | --- | --- |
| `user_message` | `id`, `text`, `source` (`app`, `tui`, `cli`, `mcp`, `voice`) | the person wrote to the running task (journaled, as the goal is) |
| `steer_applied` | `ids[]`, `step` | at the top of the next turn, the messages the model reads from now on, in order; a pending message also stops a chain before its next action (`frontier_chunk_stop` with `reason: "steer"`) |
| `steer_unread` | `ids[]` | the task ended before the model read them |
| `stop_after_step_requested` | | "Stop after this step" was asked: the action under way finishes, nothing new starts, and the result is `stopped` ("Stopped after the step in progress, as you asked.") |
| `paused` / `continued` | `step` | the loop held at a step boundary, touching nothing, then went on; a pause between chained actions stops the chain (`frontier_chunk_stop` with `reason: "pause"`) and the screen is read again after Continue. Ten minutes paused ends the task `stopped` |
| `picked_up` | `from`, `step` | a task that picks up `from` starts from its checkpoint, `step` turns in |
| `prewarm_used` | `age_ms` | the first screen came from prewarm's read, that many ms old, with nothing on screen changed since |

A clarifying question (`ASK_USER`, interactive tasks only) is an `approval_requested` with
`kind: "clarify"`, `operation: "ASK_USER"`, `label` (the question's first 80 characters) and
`choices` (ids); its step reads `Asked you a question`. `approval_resolved.decision` is
`answered` for a typed answer (never its text), `choice:<id>` for a choice, `denied` for Skip.
Nobody answering in 10 minutes ends the task `approval_timeout`.

`approval_requested` adds `kind` (`commit`, `unsure`, `loop`, `question`, `action`),
`title` (`"Send this message to Sam?"`), `act` (the contract act, e.g. `send_message`) and
`target` (`{x, y, w, h}`, screen fractions of the control about to be tapped). The pending
approval object carries the same fields plus `text`, the exact text that would be sent.
`approval_resolved.decision` may be `redirected`.

What asks, in Smart: a declared commit that passes the contract's check, when Ask before
acting is on, for every act except `save` and `archive` (a private write such as a
reminder or a note never asks). A repeat of the same commit asks again.

`result` (and the run's `summary`) adds:

| field | meaning |
| --- | --- |
| `outcome` | `done`, `check`, `couldnt_finish`, `stopped` or `declined` |
| `answer` | one sentence, a headline: the first sentence of the full answer (below), or its first line when that line has no sentence end and more lines follow ("Here are your reminders for today:" over a list; a list's "1." ends no sentence). It drops a leading "I" before a past-tense verb ("Added the reminder…") only when the task acted (a COMMIT or WRITE item) and no proof quote holds the sentence: words read on the phone ("I left the keys under the mat.") stay as they were |
| `proof` | `[{quote, app, screen, frameId}]`: the latest receipt per item, a write left out when a sent message or save in the same app quotes it, one entry per quote |
| `reason` | one sentence when the outcome is not `done`; a model call that failed without an OpenAI status (offline, a timeout) is `Mobster couldn't reach OpenAI. Check your internet connection, then try again.` (`…reach Anthropic…` on Claude), a phone or WDA failure `Your iPhone stopped answering…` |
| `engine`, `costUsd` | the run's engine and final spend |

A write the contract counts as done though nothing was typed or picked for it in its app (the
model judged the text already there) makes the outcome `check`, with the reason `Already done, so
Mobster changed nothing: …` naming up to three items. The app says it as its own sentence, never
as a missing proof.

Smart statuses: `completed` (every contract item proven: outcome `done`),
`completion_not_confirmed` (finished with an item unproven: `check`), `blocked`,
`max_steps`, `timeout`, `spend_cap`, `error`, `approval_denied`, `approval_timeout`,
`stopped`.

The full answer is the result; `answer` only heads it:

- Text, Markdown and Auto: `data` is the whole answer as the model wrote it (line breaks kept).
  The run view shows `answer`, then the rest of `data` under it; Copy answer copies all of it.
- An output schema: `data` is the validated object (`schema_validated: true`, `schema_source:
  "custom"`), the text is in `full_answer`, and the run view shows the data under `answer`.
- CSV, JSON or YAML with no schema: one small call (`engines.structure_auto`, on Smart's own
  key) names columns and fills rows from the answer; code builds the records (one object, or a
  list of them) and their schema and checks both (`output_contract.validate_automatic_schema`).
  `data` is the records, `output_schema` their schema, `schema_source: "automatic"`,
  `resolved_output_format` the format asked for. If that call fails or its table doesn't line
  up, `data` is the answer's text (`resolved_output_format: "text"`), `structure_error` says
  why and `output_note` says it for people, with curly apostrophes like every dashboard string:
  `Mobster couldn’t turn this answer into CSV, so it’s shown as text.` When that call found the OpenAI (or Anthropic) account empty, `structure_error` keeps
  the status and code (`RuntimeError: OpenAI HTTP 429: insufficient_quota`), so the run makes
  Smart unavailable like any other credit refusal, and `output_note` says the account ran out of
  credit (a schema's call too, whose `data_status` stays `schema_validation_failed`).

Frames: any event may carry `_frame` (JPEG bytes) into `Run.emit`; the server stores it
and replaces it with `frameId`. Fast frames come from `agent.capture_frames`.

## TypeScript

`dashboard/src/lib/engine-contract.ts` holds the types above (`Engine`, `Rect`,
`EngineEstimate`, `EngineEstimates`, `EngineStatus`, `Outcome`, `Proof`, `ContractItem`,
`ResultFields`, `ApprovalFields`), `frameUrl` and `estimateLabel`. `ServiceStatus` in
`lib/api.ts` has the optional `defaultEngine` and `engines`. `StartPayload` in `lib/run-state.ts` takes an optional `appId` and
`engine`.

## Measured (simulator 4, 27 Sep)

Through the API with `engine: "smart"` and Ask before acting on, at commit `dbb7c51` (the copy task at
`435082d`, which only adds the Check rule it shows). Estimates are the shipped `smart_estimate`'s
single figure:

| task | outcome | turns | seconds | cost | estimate | approvals | proof |
| --- | --- | --- | --- | --- | --- | --- | --- |
| iOS version (from the Home Screen) | done | 3 | 12.6 | $0.027 | $0.037 | 0 | iOS Version 26.4 (About) |
| add a reminder | done | 4 | 13.5 | $0.033 | $0.037 | 0 | buy oat milk (Reminders) |
| read a Safari heading | done | 3 | 20.2 | $0.023 | $0.058 | 0 | Example Domain |
| message a QuickChat contact | done | 4 | 13.5 | $0.033 | $0.048 | 1, at Send | I'm running late |
| copy an email from Contacts into a reminder | check | 3 | 13.3 | $0.029 | $0.076 | 0 | none: the reminder was there from an earlier run and nothing was typed |
| JSON-schema extraction from Settings › About | done | 2 | 7.7 | $0.022 | $0.071 | 0 | iOS Version 26.4; Model Name iPhone 17 Pro |
| decline "I'm running late", say ten minutes late | done | 4 | 14.4 | $0.037 | $0.048 | 2 | I'm running ten minutes late |
| Stop after the first step | stopped | 1 | 5.8 | $0.014 | $0.094 | 0 | none; the one action after Stop had started before it |

The Smart estimate (`engines.smart_estimate`) is fitted on 184 recorded runs of this loop
(`tests/smart_costs.json`): within 2x on 82% of held-out runs and 30 of the 32 ab7-b iOSWorld runs.
On short app tasks it errs high (median actual/estimate 0.67).
