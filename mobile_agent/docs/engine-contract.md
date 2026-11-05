# Engine contract v1

What the agent's API and run events say about Mobster's engine, for the dashboard and any
other client. Every field here is new and optional: a client that ignores them keeps
working, and an agent that predates them simply omits them.

- **Smart** (`smart`, Mobster's agent, the one engine): the frontier loop (`frontier.FrontierAgent`) on
  `claude-sonnet-5-5` at reasoning `low` (`gpt-5.6-sol` until 12 Oct 2026), with the task contract
  and DONE gate (Bet 1) and exact actuation (Bet 2). It is `engines.SMART_CONFIG`, the same config
  `bench.iosworld --policy frontier` runs. It may open any app on the phone. Key
  (`engines.smart_model`): `ANTHROPIC_API_KEY` (or an Anthropic helper's key) through
  `model_clients.AnthropicChat`, also when an OpenAI key is set beside it. **On OpenAI**: a user whose
  only key is `OPENAI_API_KEY` (or the helper's key when the helper provider is OpenAI), or who
  chose OpenAI (`MOBSTER_SMART_PROVIDER=openai`), runs the same loop on `gpt-5.6-sol`
  (`engines.OPENAI_SMART_MODEL`). `MOBSTER_SMART_MODEL` picks any OpenAI or Claude model outright
  (`claude-opus-5-5`, `gpt-5.6-terra`...), and the key it needs follows the model. Everything else
  (reasoning `low`, limits, switches) stays `SMART_CONFIG`'s.
- `"fast"` and `"quick"`, the names of an engine retired on 13 Oct 2026, read as `smart`
  (`engines.read_engine`) wherever they arrive: a saved workflow, a thread message, a run request,
  an estimate.
- The coding agents (`codex`, `claude_code`, `gemini`; `ai_plans`) are engines a task may name too.

Mobster never runs a task on an engine the user did not choose: an unavailable engine
refuses the task with its reason.

## API

### `GET /api/engine`

The engine-health state (`engine_health.py`; the user's view is in docs/scripting.mdx § Engine health). Every state's words
and its fix are one row of `states.ROWS`, the table `mobster doctor` reads too; `fix` rides in each issue. `Runtime`
owns one `EngineHealth`; `Sampler` reads the detectors into it every second once `serve` starts it (without it, each
`engine()` call runs one pass). The detectors, and nothing else, decide:

- `DeviceManager.engine_reading()` gives plain facts about a phone's helper and link, `classify_phone` turns them into
  a state and cause. The helper log's causes come from `runner_hints.RUNNER_HINTS`.
- `engines_status()` gives the model (`classify_model`).
- A run's `handoff_waiting` events give a task's wait (`run.phone_wait`, `task_condition`).

`/api/status` carries the same snapshot as `engine`. `limitations` stays for older readers and is no longer the place to
add a reason. The UI has no health logic of its own. `mobster mcp`'s `status` returns `engine` too, read from the app.
The sampler only reads; it never sends an action, and a wait that begins mid-action sets `action_unknown` and nothing
else (the effect ledger and `FrontierAgent._recheck_unknown` already hold the action back).

### `GET /api/status`

Adds:

```json
"engines": {
  "smart": {"available": true, "reason": null, "model": "claude-sonnet-5-5", "provider": "Anthropic", "billing": "key"}
  // on OpenAI: {"available": true, "reason": null, "model": "gpt-5.6-sol", "provider": "OpenAI", "billing": "key"}
}
```

The reply also has `app_session`: `true` when this service was started with the Mac app's per-launch session secret
(the Mac app's own agent), `false` for a service started on its own.

- `reason` when unavailable: `Connect your AI account in Setup` (with Setup; `Add OPENAI_API_KEY or ANTHROPIC_API_KEY to the agent's env file` without it;
  `Add ANTHROPIC_API_KEY to the agent's env file to run Smart on <model>` when
  `MOBSTER_SMART_MODEL` names a Claude model and no Anthropic key is set), `Your OpenAI key
  can't use gpt-5.6-sol.`, `OpenAI didn't accept your key. Check it in Settings › Models.` or
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
  <model>.`, `Claude didn't accept your key. Check it in Settings › Models.` and
  `Your Claude account is out of credit. Add credit at platform.claude.com, then try again.`
  (`engines.NO_ANTHROPIC_CREDIT`: a 402, or a 400 that says the credit balance is too low). The
  background check is `GET https://api.anthropic.com/v1/models/<model>`; verdicts are kept per key
  and model. Setup's AI account step takes a Claude key: `POST /api/keys {"anthropic": {"key": ...},
  "smartProvider": "anthropic"}` saves it as the key Smart uses and `POST /api/keys/test {"target":
  "anthropic"}` tests it (`GET /api/keys` adds `anthropic: {configured, keyHint, fromHelper, model}`
  and `smart: {provider, preference}`); Setup's key step counts it as done (`keys.anthropic` in the
  Setup state).
- `live_enabled` and `limitations[0]` follow Mobster's agent.

### `POST /api/runs`

Takes `engine: "smart"` or a coding agent (absent: Mobster's agent; `"fast"` reads as `"smart"`). `appId` is
optional and only says where the task starts: absent, it starts on the Home Screen and
the run's app is `{"id": "any", "name": "Any app"}`. A present `engine` is part of the
request's identity for `Idempotency-Key`.

Refusals, with `code`. Nothing started for any of them, so a client keeps the draft and shows `error`:

| code | status | when |
| --- | --- | --- |
| `engine_unavailable` | 409 | the chosen (or default) engine can't run; `error` is its reason, `engine` names it |
| `monthly_limit_reached` | 409 | this month's spend reached the monthly limit, or the tasks running now may spend the rest of it; carries `monthUsd`, `monthlyLimitUsd` |
| `usage_unavailable` | 503 | a monthly limit is set and this month's spend can't be read: the start fails closed |
| `device_unavailable` | 503 | the iPhone isn't ready; `stop: "phone_locked"` when it is locked |

Under a monthly limit a task stops at the lower of its per-task limit and what is left of the month, counting
what the tasks running now may still spend, so two phones at once never pass the limit together.

`dryRun` is refused (400, `Leave dryRun out: Mobster's agent acts from its first step and asks before it sends, buys,
posts or deletes`): a request that asked for a preview must never act. `helperModel`, like any field not listed, is
refused as `Unsupported task field` (400).

### Run JSON (`GET /api/runs`, `/api/runs/{id}`)

The list's rows have no `events` (only `eventCount`) and leave out the summary's
`timing`; `GET /api/runs/{id}` has both. Every `GET` answer carries an `ETag`, and
`If-None-Match` with it gets `304` and no body while nothing changed.

| field | meaning |
| --- | --- |
| `engine` | `smart`, a coding agent, `fast` (a run from before 13 Oct 2026), or null (a demo replay, a run from before engines) |
| `costUsd` | published-rate spend so far, live; null until a priced call finishes |
| `estimateUsd` | the single estimate shown before the task ran (the geometric mean of its range) |
| `reviewedOk` | the user marked the result OK |
| `approval` | the pending approval, now with `kind`, `title`, `act`, `target`, `text`, `app` |

### Approvals

`POST /api/runs/{id}/approval` takes `{id, approve}` as before, plus
`{id, approve: false, instruction}` for **Decline and say what to do instead**: the
decision is `redirected`, the agent gets the instruction (1–500 characters) and the task
goes on. The instruction is never written to the run's events.

A second answer to a request already answered (another window, a double click) gets `409
approval_not_pending` with `decision` (`approved`, `denied`, `redirected`, `choice`, `answered`,
`timeout` or `stopped`) and an `error` that says what the first answer did. After Stop no answer
is taken, and an approval that raced a Stop is never acted on. A request ends at its `expiresAt`
by the wall clock too, so after the Mac sleeps past it the task ends within a second of waking.

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
- `POST /api/runs/{id}/eval {}` (experimental, `MOBSTER_EXPERIMENTAL_TRACES=1`) keeps a finished run as a
  regression task in the eval set beside the journal (`traces.add_to_evals`, `docs/traces.md`) and returns
  `{id, path, added, expected, recorded}`: `added` is false when the run was already there, `expected.replay` is
  `changes` for a run that failed and `holds` for one that didn't. 503 `traces_off`, 409 `running`/`run_active`
  while it runs, 422 `no_tape` or `unreadable` when the run can't be replayed, and 400 for a body.

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

- `POST /api/usage/estimate {goal, outputSchema?, outputFormat?, engine?}` returns `engine` (`smart`) and
  `engines: {smart}`, an `EngineEstimate`
  (`{available, reason, model, provider, lowUsd, highUsd}`). `GET` returns the same for an
  empty draft. Smart's range is its estimate halved to doubled (`costs.smart_estimate`: fitted
  on 184 recorded runs of the loop, within 2x of the actual cost on 82% of held-out runs), plus
  `SMART_STRUCTURE_USD` when Smart puts its answer into a structure (an `outputSchema`, or
  `outputFormat` `csv`, `json` or `yaml` without one), so the composer shows one figure from the range: `costPhrase(estimate)` in
  `components/composer-estimate.tsx` gives `about 4¢`, `under 1¢` or `about $1.20`.
- `GET /api/usage` adds `monthUsd` (this calendar month, local time, published rates) and
  `monthlyLimitUsd`. Smart's calls are provider `openai`, or `anthropic` on Claude.
  A run on the person's own agent adds rows with provider `claude_code`, `codex` or `gemini`.
  Each `providers[]` row is per provider, model and `billing` (`plan` or `api_key`) and gains
  `cache_read_tokens`, `cache_write_tokens` and `api_equivalent_usd`; `totals` gains the same
  and `plan_tokens`; the top level gains `limits` (per agent id: `status`, `type`, `resetsAt`,
  `windows`, `at`). Plan rows cost 0 and never count toward `monthlyLimitUsd`.
  `GET /api/home`'s `week` gains `planTokens` and `apiEquivalentUsd`.
- `GET/POST /api/settings` add `taskCostLimitUsd` (default `1.00`; null
  turns the stop off; 0.05–100) and `monthlyLimitUsd` (default null; 1–10,000). A task
  that reaches its limit ends with status `spend_cap`; Smart spends its last turn
  reporting what it found.
  With an app session, changing either limit needs it (`403 app_only`), as a task's
  `capUsd` does.
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
| `step` | `n`, `step`, `text`, `frameId`, `redact`, `focus`, `commit`, `changed`; `sheet`, `media` for WATCH | after each action, in plain words; a WATCH step's frame is its contact sheet (`sheet`) and `media` is `{seconds, ended, heard, rows: [{t, end?, kind: scene, sound or said, n?, text, confidence?}], throughout}`, the timeline the model read, transcript included; `step` is the loop's turn (several actions share one). `redact` (with a frame): the normalized `[x, y, w, h]` frames of the text fields and message text on that screen, at most 24 and never their text, which a saved GIF blurs with `--redact` (`run_gif.py`). `focus` (with a frame, only for a tap or typing step whose element is still on that screen): its normalized `[x, y, w, h]`; left out when unknown, and for a coordinate tap. Data only. An approval's `screen` carries it as `focus`, `{x, y, w, h}` in the frame's pixels. `commit` (a tap or a hold on a named control): `true` when it may have committed something by the loop's own fail-closed test (`frontier.safety.consequential`, which `frontier_action` carries as `consequential`: a commit word, a price, a delete or discard, an App Store tap, a tap while a form holds typed text) or, for a coding agent's tap, by the permission gate's reading; `false` when neither says so. `changed`: whether the screen changed after the action. Each is left out when unknown, which means "may commit" and "may not have changed": a typing, a swipe, a point, a link, a skill's or a tool's own step, a dialog, a WATCH and an older run carry no `commit`. The app folds a step into a run of visits only when it says `commit: false` and `changed: true` |
| `thought` | `step`, `text`, `ms` | after each model turn (not a saved program's), before that turn's steps: the model's own short sentence for the turn (`text`, at most 300 characters, null when it wrote none; the user's secrets masked by the loop and anything that reads as a key, a card or a code by `secret_filter.redact`) and how long the call took (`ms`). The run view shows it as "Thought for 2s", opening to the sentence. Never the prompt |
| `cost` | `usd`, `calls` | after each model call; `usd` is the task's spend so far |
| `agent_usage` | `agent`, `call_id`, `model` (or null), `usage` (`input_tokens` with cache included, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`) | one per model call of an agent engine (Claude Code, Codex, Gemini) |
| `agent_limits` | `agent`, `limits` (`status`, `type`, `resetsAt`, `windows`) | whenever the agent prints its plan limits; only numbers and known keys are kept |
| `inference_started` / `inference_finished` | `provider` (`"anthropic"`, or `"openai"` on OpenAI), `call_id`, `model`, `purpose`; finished adds `success`, `latency_ms`, `usage`, `estimated_usd`, `cost_nanodollars`, `cost_basis` | every model call, with its list-price cost; on Claude a hedged call's losing twin, which is billed too, is a pair of its own with `purpose: "hedge"` |

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
- A row's swipe action that deletes is a `delete` commit like a Delete button (`contract.deletes`): the trash glyph
  an unlabelled action reads as (`trash`, `trash.fill`, `xmark.bin`), `Move to Trash`, or a narrow button named
  Trash or Bin on a row. A cell, a text or a full-width row named Trash is the folder. The swipe that reveals the
  actions, and actions that don't delete (Flag, Pin, More), are unchanged.
- A commit's confirmation row is a new row with the confirmation words of its act, or of an act
  the tapped control's own verb names: `Request CityRideX · $12.80` books a ride, so
  `Driver arriving` confirms it whatever act the contract compiled. `Confirm`, `Submit` and
  `Send` name no act of their own and add nothing.
- Who a message goes to is picked by name (`Contract._recipient_pick`, `_made_address`), while a message commit to a
  named person (`recipient`) is open, by names and roles only. Return in a To field holding a typed name (not a phone
  number or an email) is refused: it makes the name an address of its own. Among suggestions or results for a typed
  name, a row that isn't the request's name exactly is refused when another row is (`Jordan Lee` over
  `jordanlee@example.com` or `Jordan Leeds`); with no exact row and two or more close ones the agent is told to ask
  (`ASK_USER`) or stop, not guess; one close row is picked. At Send, a To token that is the name squashed into one
  word (`jordanlee`) is refused.
- A like or a follow (`react`, `follow`) is confirmed only by the control the agent tapped,
  where it was, turning on afterwards: selected when it wasn't, its label or value saying so
  (`Liked`, `Unlike`, `Following`, `Requested`), a value turned on (`0` to `1`), or the count on
  or just beside it up by one (`7` to `8`). A change elsewhere (a banner closing, a `Liked by …`
  row, a `Following` tab) confirms nothing, and neither does the post still being on screen. A
  tap on a point (`TAP_XY`) or an ungated tap never confirms one, so a run that claims a like it
  never tapped through the gate ends with it not confirmed. The gate refuses a tap on a like's
  control that already shows on (selected, or its value says `Liked`): it would undo the like.
  Unfollows keep their confirmation row.
- A write the agent marks as already so (`already: …`) is proven by the row it quotes, or by its
  value as a whole row of a list in its app (`New York` among saved locations) seen before
  Mobster typed anything in that app. A page titled with the value is not a list row.
- A value the agent records in a turn (a report item found, a write's value) from a row the screenshot doesn't show in
  full is scrolled into view before that turn leaves the screen (`LAUNCH_APP`, `HOME`, `OPEN_URL`) or its DONE is
  accepted: one swipe up by code (XCTest's half-screen drag), no model call, once a turn (`Contract.hidden_row`). A row
  is hidden when the screen's foot cuts it or a bar in the bottom 20% of the screen lies over it: a tab bar or
  toolbar, a control 60% of the screen wide, or a line of three controls as wide. Texts and pictures are never bars,
  and a row's own button is not one. A tab bar or toolbar lies over any row not inside it; another bar only over rows
  listed before it (the tree lists back to front, so its own texts come after it). A row under a top bar is not
  scrolled: no stroke reliably starts inside the content under a header (TeamChat's pinned chips took both the drag's
  and the glide's start, live, 18–19 Oct). It is journaled before it is sent as `frontier_cleanup` with `reason:
  "reveal_value"`. iOSWorld dev runs (88, recorded 18 Oct, tape replay): it fires in 6.
- A write that adds something (`log`, `add`, `create`, `new`) with nothing typed for it (a search
  is not the write) counts once each object it names has a new row in its app: more rows naming
  it than on any screen before Mobster first wrote there. A row from before proves nothing: an
  oatmeal breakfast already in the log is not one logged now. A change of state (a reminder
  marked done) or a picker keeps the agent's word, as before.
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
before the first model call.

Messages, pause and picking up (track harness, [harness.md](harness.md)) add these Smart events:

| event | fields | when |
| --- | --- | --- |
| `user_message` | `id`, `text`, `source` (`app`, `tui`, `cli`, `mcp`, `voice`) | the person wrote to the running task (journaled, as the goal is) |
| `steer_applied` | `ids[]`, `step` | at the top of the next turn, the messages the model reads from now on, in order; a pending message also stops a chain before its next action (`frontier_chunk_stop` with `reason: "steer"`) |
| `steer_unread` | `ids[]` | the task ended before the model read them |
| `stop_after_step_requested` | | "Stop after this step" was asked: the action under way finishes, nothing new starts, and the result is `stopped` ("Stopped after the step in progress, as you asked.") |
| `paused` / `continued` | `step`; `reason` and `level` when Mobster paused it | the loop held at a step boundary, touching nothing, then went on; a pause between chained actions stops the chain (`frontier_chunk_stop` with `reason: "pause"`) and the screen is read again after Continue. Ten minutes paused ends the task `stopped`. `reason: "phone_battery"`: a long or scheduled task paused while its iPhone's battery (`level`, a percent) was low and not charging, and went on once it charged (power_watch.py); thirty minutes of it ends the task `stopped` |
| `mac_battery_low` | `percent`, `threshold` | the task runs on a Mac on battery at `threshold` percent or less; once per task until the Mac is plugged in |
| `phone_low_power` | `message` | a scheduled task starts on an iPhone that says Low Power Mode is on, so it locks after 30 seconds; once per task, only with `MOBSTER_QUEUE_PREFLIGHT=1` (phone_ready.py) |
| `picked_up` | `from`, `step` | a task that picks up `from` starts from its checkpoint, `step` turns in |
| `watch_started` | `seconds`, `untilEnd`, `hearing` | the agent's WATCH starts watching (and hearing, on a USB iPhone unless `MOBSTER_LISTEN=off`) what the iPhone plays |
| `watched` | `ok`; `seconds`, `ended`, `frames`, `samples`, `words`, `heard`, `ms`, `text`, `frameId` (the contact sheet) when it watched, and `reason` (a code such as `permission` or `no_device`) and `hearing` (the sentence) when it tried to hear and couldn't; `code` when it couldn't watch | WATCH ended; its timeline goes in the `step` that follows (`media`), not here |
| `prewarm_used` | `age_ms` | the first screen came from prewarm's read, that many ms old, with nothing on screen changed since |

Long tasks (track longform, [longform.md](longform.md)) add the plan route, `GET /api/runs/{id}/longform`, the run
field `longform`, and the events `longform_plan`, `longform_progress`, `longform_item`, `longform_finding`,
`longform_step`, `phone_released` and `longform_report`; their result carries `report` and `markdown`, and their
`cost` events report the run's whole spend.

A clarifying question (`ASK_USER`, interactive tasks only) is an `approval_requested` with
`kind: "clarify"`, `operation: "ASK_USER"`, `label` (the question's first 80 characters) and
`choices` (ids); its step reads `Asked you a question`. `approval_resolved.decision` is
`answered` for a typed answer (never its text), `choice:<id>` for a choice, `denied` for Skip.
Nobody answering in 10 minutes ends the task `approval_timeout`.

`approval_requested` adds `kind` (`commit`, `unsure`, `loop`, `question`, `action`),
`title` (`"Send this message to Sam?"`), `act` (the contract act, e.g. `send_message`) and
`target` (`{x, y, w, h}`, screen fractions of the control about to be tapped). The pending
approval object carries the same fields plus `text`, the exact text that would be sent.
A message's `title` names who the screen shows it going to (`contract.visible_recipient`): the To
field's tokens, else the thread's title, else the one name atop a thread whose bar has no title
(never in a mail app, whose top line is the subject). A party that isn't the request's is named beside it
(`"Send this message to Sam Rivera (Kate Bell in your request)?"`). With no one on screen the title
keeps the request's words as the request's (`"Send this message to the person you named (Kate Bell)?"`),
or names no one (`"Send this message?"`). A developer's name for a view (`contract.internal_name`: a
class name such as `CKComposeChat`, an accessibility identifier, a bundle id) is never shown as a party.
`approval_resolved.decision` may be `redirected`.
A tap at a point (`TAP_XY`) is bound to no listed element, and the name the model gives it unlocks nothing
(`contract_asks.point_ask`). It asks as an `approval_requested` with `operation: "TAP_XY"`, `role: "Point"`, an empty
`label`, a `target` box around the point and `title` `"Tap at this spot — it may <act>?"` naming the open commit's
act (`"… it may like first post in feed?"`, `"… it may send a message to Sam?"`), when the task has an open COMMIT
that asks, or when the point is inside no listed control (`"… it may do something that needs your OK?"`, `act:
"other"`). A point inside a plainly navigational control goes ahead: a tab, the navigation bar's leading (Back)
button, or a list row with no commit verb (its trailing quarter, where a row's own heart or Follow sits, left out).
So does a point inside no listed control in the top bar band (`contract_asks.top_bar_navigation`: y at most 0.13) of a
task with no open commit, with no keyboard, alert, sheet or popover up and no commit word on, near or anywhere in
that band (real iPhone, 13 Oct: Instagram's profile menu is drawn outside the tree).
A point on a listed control whose words commit is refused, so it is tapped by its id and gated as a TAP. The ask is
forced (no "Don't ask again" rule meets it or is offered). Full access doesn't ask, and neither does a run with
no permission mode and no approver (a benchmark, a check on a simulator); a run with no mode asks the approver it
has (`mobster verify`'s, which denies every commit on a real iPhone). A long run asks. In a mode that asks, with
nobody to answer, the tap is refused (`frontier_refused` with `label: "needs_approval"`). Every product path carries
a mode, Ask before acting unless the person chose otherwise (`tests/test_permissions.py`): the
Runtime's runs (the Mac app, `mobster serve`, schedules, Mobster Remote, MCP tasks, the terminal UI) and AI plans.

Orchestration (mobile_agent/orchestrate.py; `Run` methods, never hand-built events) adds these:

| event | fields | when |
| --- | --- | --- |
| `interpretation` | `text` (at most 160 characters), `apps[{name, bundleId}]`, `readOnly` | `run.interpret(...)`: how the agent read a vague, read-only goal, before its first action; a later one replaces it. The person corrects it with a message (a steer) |
| `approval_requested` with `kind: "plan"` | `operation: "PLAN"`, `plan{summary, apps[{name, bundleId, does}], steps[{id, title, app}], policy[], readOnly, long?}` | `run.propose_plan(plan)`: the plan card. A long task's plan carries `long` (longform/plan.py's card: budget, estimate, alternatives, cap) and the approval `choices` (`default`, then the alternatives). `approval_resolved.decision` is `approved` (Start), `choice:<id>` (Start with that budget), `denied` (Skip), `redirected` (what to change) or `timeout` |
| `approval_requested` with `kind: "proposals"` | `operation: "PROPOSE"`, `count`, `items[{id, app, bundleId, act, to, title}]` | `run.propose(items)`: drafts to review together. The pending approval's items also carry `text` (the draft), `context` (what it answers), `decision` and `edited`; events never do |
| `proposal_decided` | `approval_id`, `item_id`, `decision` (`send`, `skip`), `edited` | each draft as the person decides it (`POST /api/runs/{id}/approval` with `items`). A decision is final |
| `approval_resolved` for proposals | `decision` (`decided`, `timeout`, `stopped`), `sent`, `skipped` | every draft decided, or time ran out (undecided drafts are skipped), or Stop (every draft is skipped) |
| `proposal_result` | `approval_id`, `item_id`, `status` (`sent`, `failed`, `skipped`), `line` | `run.proposal_result(...)`: what happened to a decided draft |
| `subruns_planned` | `subruns[{subrun, app, bundleId, title}]` | the rows of a multi-app task, waiting until each starts |
| `subrun_started` / `subrun_progress` / `subrun_finished` | `subrun`, `app`, `bundleId`, `title`, `runId` / `line`, `counts` / `outcome` (`done`, `check`, `couldnt_finish`, `stopped`, `skipped`), `line`, `counts` | one app's part of the task. While one is open every other event carries `subrun` too; one at a time; at most 2 progress events a second |

One plan for everything: `Runtime.confirm_plan` shows a run's plan before the phone is touched, for a task from the Mac
app or the terminal UI. A long task (`run.longform`) shows its own plan with its budget; a goal that pins or names two or
more apps shows `orchestrate.apps_plan` (`longform/plan.py › detect` leaves those to it). Start sets `run.plan`; the
engine proposes a plan of its own only while `run.plan` is None.

`POST /api/runs` and thread messages take `pinnedApps` (1 to 5 app ids, @App in the composer): with no `appId` the
task starts in the first, and Smart is asked `In Messages, WhatsApp and Instagram: <goal>`.

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
| `stop_code` | the phone guard stopped it (`phone_locked`, `unplugged`, `face_id`… `agent_hooks.STOP_CODES`): `reason` is the phone's own sentence. Smart (status `blocked`) and a refused preflight (status `error`) both carry it |
| `model_error` | status `error` because of the AI provider: `unreachable`, `rejected_key`, `model_unavailable`, `no_credit`, `rate_limited`, `provider_error` or `declined`; `reason` is its sentence (`engines.plain_error`) |
| `spend_limit` | `monthly` when what was left of the month's limit, not the per-task limit, stopped it (status `spend_cap`) |
| `engine`, `costUsd` | the run's engine and final spend |
| `failure` | experimental (`MOBSTER_EXPERIMENTAL_TRACES=1`): `{class, step, line}` on a run that didn't finish, from its trace (`failure_line.py`). `class` is `out_of_steps`, `gave_up`, `premature_done`, `blocked_by_prompt`, `app_crashed`, `wrong_app`, `stuck` or `error`; `step` counts from 1 (null before the first); `line` is one sentence, secrets masked. A run that finished, was stopped or declined has none |

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
- CSV, JSON or YAML with no schema: one small call (`smart_result.structure_auto`, on Smart's own
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
and replaces it with `frameId`.

## TypeScript

`dashboard/src/lib/engine-contract.ts` holds the types above (`Engine`, `Rect`,
`EngineEstimate`, `EngineStatus`, `Outcome`, `Proof`, `ApprovalChoice`,
`ResultFields`, `ApprovalFields`), `frameUrl` and `parseEngineEstimate`. `ServiceStatus` in
`lib/api.ts` has the optional `engines`. `StartPayload` in `lib/run-state.ts` takes an optional `appId` and
`engine`.
