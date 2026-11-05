# Saved tasks and schedules

Saved workflows share the private run journal and its process lease. Creating a workflow never starts an agent. Unless the request says otherwise, a workflow starts with `enabled: false`, no cron expression, `outputFormat: "auto"`, no engine, and the Mac's own time zone. Tasks always use `Runtime.create`, including its installed-app, live readiness, device lease, idempotency, and durable run checks. This is the local single-operator service, not a multi-tenant scheduler.

## API contract

`GET /api/workflows` returns `{ "workflows": [...], "schedulerError": null }`. A scheduler persistence or unexpected runtime failure stops scheduling and supplies an explicit error; it does not silently keep retrying.

Each workflow contains `id`, `name`, `appId` (null for a Smart task that starts on the Home screen), `appName` (the app's display name, saved with the workflow so an app found on the phone keeps its name while the phone is away; null without an app, and absent on workflows saved before it existed), `goal`, `outputSchema`, `outputFormat`, `engine` (`"smart"` or null; older workflows may omit it), `cron`, `timezone`, `enabled`, `revision`, `createdAt`, `updatedAt`, `nextAt`, `lastRunId`, and `lastOutcome`. Dates are UTC epoch milliseconds. A null `nextAt` means paused/manual; a null last outcome means never dispatched. `startsItself` (`service._starts_itself`) is true when turning the workflow on makes it start by itself: it is published, and its published graph has a schedule (`cron`, which the tick fires at `nextAt`), a `trigger.notification` or a `trigger.app` (which `WorkflowTriggers` reads from the published graph only). It doesn't read `enabled`, so a paused workflow says true and the Mac app offers Resume; nor the draft, which nothing fires until it is published. False means `enabled` changes nothing that starts it. `listensFor` (`service._listens_for`) names the trigger in the published graph that starts it from outside: `"notification"`, else `"app"`, else null; the row's **On a notification** and **On something new in an app** read it, never the draft's triggers. `enabled` gates only those three: Run, webhooks, shortcuts, MCP, Remote, `POST /v1/workflows/<slug>`, a `workflow.call` step and Settings › Triggers rules (`triggers.fire`) start a workflow whatever it says. `revision` versions configuration edits, not background task outcomes. `lastSuccess` (`{at, runId, line}`) is the newest verified run, kept with the record because dispatch records go after a week. `GET /api/workflows` adds `health` (`workflows/health.py`): `lastSuccess`, and `attention` (`failed`, `phone`, `input`, `limit` or `unknown`, for a run whose record went before its outcome was read: never a failure and never a Needs you repair) read from the newest attempt that ended, so a later verified run supersedes an earlier failure; a missed slot neither raises nor hides attention.

`POST /api/workflows` accepts `name` (1–120 characters), `appId`, optional `appName` (1–120 characters; without it the service fills it from the catalog, then from the apps found on the phone), `goal` (1–4000 characters), optional `outputSchema`, `outputFormat` and `engine`, and an optional schedule: `cron`, `timezone` and `enabled`, validated exactly as an update validates them. It returns `{ "workflow": ... }` with HTTP 201. `appId` may be left out only when `engine` is `"smart"`: the run then starts on the Home screen and Smart picks the apps. Without `timezone`, the workflow uses the Mac's IANA zone (`TZ`, else the `/etc/localtime` link, else UTC), so "9:00 AM" means 9:00 AM where the user is; existing workflows keep the zone they were saved with. The output settings stay as saved; the task and its app can be edited (below).

`POST /api/workflows/{id}` accepts the current integer `revision` and optional `name`, `goal`, `appId`, `appName`, `cron`, `timezone`, `enabled` and `engine`. `goal` is validated as on create (1–4000 characters, stored trimmed). A changed `appId` or `engine` is checked the way create checks them: only Smart may drop the app, and the app must be in the catalog or found on the phone. A new `appId` without `appName` takes the app's own name; the same app keeps its saved one. It returns the updated workflow; an outdated revision returns HTTP 409 with `revision_conflict`. Scheduling requires an explicit update with `enabled: true`. Pausing cancels future occurrences, not an already-started task. An edited task applies to later runs only; tasks already started keep the instruction they started with.

`DELETE /api/workflows/{id}` removes the workflow and its dispatch records in one transaction and returns `{ "deleted": id }`. Its schedule stops; the tasks it started stay in History, since they are separate runs in the journal. An unknown id returns HTTP 404 with `not_found`. Deletion serializes with dispatch, so it never lands between a claim and its outcome.

`POST /api/workflows/preview` accepts `{ "cron": "0 9 * * mon-fri", "timezone": "America/Los_Angeles" }` and returns `{ "next": [three UTC epoch millisecond timestamps] }`. Preview never persists or runs anything.

`POST /api/workflows/{id}/run` accepts an empty JSON object and requires exactly one `Idempotency-Key` header. It returns the canonical `{ "run": ..., "replayed": false }` with HTTP 201, or the same prior run with HTTP 200 and `replayed: true`. A failed/uncertain dispatch with that key is not repeated while its record is kept (up to seven days; an uncertain one's run keeps refusing it after that); the operator must inspect status before deliberately issuing a new key. Retired run IDs return HTTP 410 rather than restarting. The dashboard keeps a manual start's key while its answer is unknown: Retry sends that key again, and so does a Run click within two minutes of its last send; dismissing the error, or a Run click after that, sends a new key. An answer with `replayed: true` opens the earlier task and says it is that one. A 503 `workflow_dispatch_uncertain` that names an `activeRunId` comes only from the request that just started that task (its start record failed), so the dashboard opens it as this click's task, with no earlier-task notice.

A workflow saved before 13 Oct 2026 with `fast` or `quick` as its engine reads as `smart` (`engines.RETIRED`), and its `helperModel` is dropped. A saved `engine` is passed to `Runtime.create(engine=...)` only when it is set; a workflow without one runs on the app's default engine. A runtime that has no engine parameter refuses to save one rather than silently dropping it. A Smart run that started on the Home screen reports `appId: "any"`; creating a workflow with that `appId` and `engine: "smart"` saves it as null, so Save as workflow works for every run.

## Timing and safety

Only five-field standard cron is accepted. Month and weekday names are supported. Seconds, year fields, aliases, random/hash syntax, nth weekdays, and last-day extensions are rejected. Day-of-month and day-of-week use standard OR semantics.

The schedule uses an explicit IANA timezone and timezone-aware cron iteration. When clocks go back, a wall-clock time that repeats runs again only if the hour field is `*` or a step (`30 * * * *`, `0 */2 * * *`): each repeat is then a real, later hour, and a distinct UTC occurrence. A schedule at fixed hours (`30 1 * * *`, `0 1-3 * * *`) runs once, at the first of the two, as cron runs a fixed-time job. The preview exposes those exact instants. Missing spring-forward times follow croniter's timezone rules; no separate hidden timezone conversion is applied. The calculation is bounded to eight years between matches.

Validation conservatively limits a matching calendar day to 60 slots and enforces at least five minutes between minute/hour combinations, including the midnight boundary. Runtime adds a shared limit of 60 scheduled dispatch attempts per rolling 24 hours and a per-workflow five-minute interval. Skipped busy/offline occurrences do not consume that execution budget; uncertain dispatches do. Manual runs remain explicit operator actions, not scheduled executions.

Claims and the next occurrence are committed atomically before calling the runtime. No SQLite transaction is held during device readiness or run creation. If a crash occurs after the canonical run commit but before the claim outcome commit, recovery links the existing journal request to the claim without calling the runtime again. If no canonical request exists, the claim is marked interrupted, not retried.

Startup skips all missed slots, including recently missed ones. While running, a tick more than 60 seconds late skips that occurrence and advances directly to the next future occurrence. Busy/offline slots are recorded and skipped. There are no catch-up bursts. Configuration changes and dispatch initiation serialize so a completed pause cannot be overtaken by an unstarted scheduled dispatch.

The scheduler starts only in `serve`, not when constructing a runtime for tests. Shutdown stops and joins scheduling before closing the run journal. If a dispatch cannot stop within the deadline, the journal and leases remain open until process exit. Saved definitions are limited to 200. Finished dispatch records are kept seven days, beyond any retry, since a slot never recurs once its claim has committed the next occurrence. At 10,000 records the oldest finished records go first: skipped ones of any age, since the daily limit and interval never count them, and others only from before the last 24 hours, which the daily limit and interval read. Schedules cannot fill the table: apart from skipped occurrences, the last 24 hours hold at most 60 scheduled attempts. The run journal likewise keeps 10,000 request keys: once full it forgets the oldest keys whose run has already left history, never one whose run is kept. If startup recovery cannot be saved, scheduling stops with `schedulerError` and the rest of Mobster still starts.

## Verification and sources

Tests use temporary databases and fake runtimes only. They never enable a schedule or dispatch work on the user's database/device.

Scheduling uses pinned croniter 6.2.4. Current API and timezone behavior were checked against the maintainer's [documentation](https://github.com/pallets-eco/croniter) through Context7 and the [published package](https://pypi.org/project/croniter/).

## Step cards and loops (13 Oct 2026)

A workflow record (version 3) also has `steps`, `repeat` and `limits` (`mobile_agent/workflows/records.py` holds the
format and its checks; `docs/workflows.mdx` is the user page). Version 2 records are upgraded on open: their `goal` and
`appId` become one task card, `repeat` is `null` and `limits` the defaults. `goal` is derived from the cards (each
task's words, then an until check's), so blanks, search, ⌘K and learned replay read it as before.

A workflow whose published graph is one task (`graph/chain.is_plain`) dispatches exactly as before. Any other is a
graph run (`graph/run.py`, `GraphRun`): `_dispatch_batch` claims the dispatch as usual, picks the entry trigger
(`trigger.schedule` for a scheduled key, `trigger.webhook` for origin `hook`, else `trigger.manual`), writes a
`workflow_batches` row (`graph: 2`, `trigger`, `revision`), records the dispatch `started` with run id `batch:<id>`, and
starts the run's coordinator thread. Each node instance is a row in `workflow_nodes` (its state as journaled, and its
input and output, masked and cut at 256 KB). A phone step is written `running` with its idempotency key (the dispatch's
key, then `:g<run>:<seq>`, plus `r<n>` for a retry) before `Runtime.create`, then with its task's id, then how it ended;
`recover()` ends a run left going as `interrupted`, giving a running step its task's real outcome or `unknown`.
The run's status is the first unhandled failure's own status (`approval_denied`, `spend_cap`, `loop_limit`,
`condition_unknown`, `delivery_failed`, `monthly_limit`, `busy`, `offline`, `check`, `error`...), `stopped`,
`time_limit`, `interrupted` or `completed`; it becomes the dispatch's outcome and the workflow's `lastOutcome`, and
`lastRunId` is the newest task. The workflow owner commits the run's terminal row, the dispatch outcome and the last
result in one journal transaction before publishing `batch_finished`, and shutdown keeps the worker until those writes
and the publication finish. One run per workflow; a second start answers `409 run_active` and a schedule records
`skipped_busy`. `BatchStore.public` makes the step cards' `log` and `rounds` from the node rows, so the run view and its
CSV read graph runs unchanged.

`outlets/hooks.py` keeps legacy signed JSON bodies and their 600-second signature replay guard. An explicit
`_mobsterEvent` version 1 envelope with `id`, `occurredAt` (Unix milliseconds) and `values` instead claims a permanent
`(hook_id, event_id)` digest before `Workflows.run`; the claim remains for busy, failed and uncertain starts. A duplicate
ID or changed content never dispatches, even with a fresh HMAC timestamp. The envelope expires after five minutes,
its values must name declared graph inputs or blanks, and 100,000 saved IDs fail closed until a URL is revoked.
Event dispatch rechecks expiry and active dispatches or child batches under `Workflows.lock` before making its own
claim. A paused schedule does not disable an existing webhook URL; revocation removes its authority and event identities.

`GET /api/workflows/{id}/runs` returns `{batches}` (newest 20, each with `nodes`, `log` and `rounds`);
`GET /api/workflows/{id}/runs/{runId}/nodes/{instance}` returns one node's history; `POST /api/workflows/{id}/stop` stops
every task of the running one or answers `409 not_running`. Events on `workflows`: `batch_started`, `batch_node` (each
state change), `batch_step` (the log's entry, for step kinds), `batch_finished` and `batch_notification`. `GET /api/workflows` adds `running` ({workflow id: batch id}), `stepKinds` and
`deliveryKinds`. Tracks add card kinds with `records.register_step(kind, validate, run)` and send places with
`records.register_delivery(kind, handler)`; the Mac app offers a kind once `stepKinds` lists it.

## Graphs (16 Oct 2026)

`mobile_agent/workflows/` holds the service. `Workflows` (`service.py`: the saved records, their checks, edits and
drafts) inherits `WorkflowRuns` (`runs.py`: dispatch claims and outcomes, the schedule tick, recovery, alerts, graph
runs, Stop, approvals) and holds `WorkflowHistory` (`history.py`: versions, publish, rollback). The graph itself is
`mobile_agent/graph/`: `expr`, `values`, `functions`, `infer` (expressions), `catalog` and `schema` (types and
validation), `chain` (cards), `edit` and `dispatch`, `run`, `state`, `nodes` and `rerun` (running).

A workflow record (version 4) has `graph`: `{version: 2, inputs, vars, nodes, edges, limits}` (`mobile_agent/graph/`;
the contract is WORKFLOW-GRAPH-SPEC, the user page `docs/workflows.mdx › The graph`). The graph is the one source of truth.

- **Draft and published.** `graph` is the draft the canvas edits; `published` is `{revision, graph}`, the graph that
  runs. `derive()` writes the run-facing fields from the published graph on every save: `cron` and `timezone` (from its
  `trigger.schedule`), `goal` (`graph/chain.words`), `limits`, and `steps`/`repeat` while it is chain-shaped
  (`graph/chain.as_chain`), else null. A `graph` body is a draft unless `publish: true`; a card body (`steps`, `goal`,
  `appId`, `repeat`, `limits`, `cron`, `timezone`) changes the published graph and publishes, so the card builder
  behaves as before. A created workflow is published unless `publish: false`.
- **Upgrade.** Version 3 records get `graph = from_chain(steps, repeat, limits, cron, timezone)` and `published` at
  their revision on open. `from_chain` and `as_chain` are exact inverses (`test_graph_schema.ChainTests`).
- **Versions.** Every create, update and rollback writes the whole record to `workflow_versions (workflow_id,
  revision, saved_at, data, published_at)` in the save's transaction; the newest 100 per workflow are kept and go with
  the workflow. `publish` marks `published_at`. GET .../versions`, `GET .../versions/{n}`,
  `GET .../versions/{a}/diff/{b}`, `POST .../publish {revision}`, `POST .../rollback {revision, current}`.
- **Validation.** `graph/schema.validate(graph, Context)` returns the graph with config defaults filled, or
  `GraphError` with every problem; the server answers `400 {error, code: invalid_graph, errors}`.
  `Workflows._graph_context` supplies apps, phones, profiles, destinations, the connector step's own check, packs, cron
  and zone checks. `POST /api/workflows/validate`, `POST /api/workflows/evaluate`, `GET /api/workflows/node-types`.
- **Running.** A plain graph dispatches as one task; any other runs as a graph run (above). A graph with a step type
  that has no runner yet (`graph/run.unsupported`) is refused before anything is claimed (`409 graph_unsupported`),
  and a workflow with nothing published answers `409 not_published`.

### The debug loop (graph/rerun.py)

`POST /api/workflows/{id}/run` takes `test` (run the draft, with pins) and one of `from {batchId, node, iteration?,
input?}`, `only {node, batchId?, input?}` and `resume <batchId>`; all need the app session (`403 test_from_editor`).
`rerun.plan` builds a `run.Debug`: `reuse` maps an instance id (`<node>@<round>`) to its recorded result from
`workflow_nodes`, applied when the instance is made, so a loop's rounds reuse per round; `skip` names nodes that don't
run; `inputs` and `items` replace one node's input or one loop's items. Reused results never count toward the new run's
outcome, except that a run blocked behind a step kept as it ended takes that step's status. A failed step records
`acted` (False for a refused admission or a look; the permission gate's evidence for a task; True when it can't
tell), and Resume runs again only a failed step that didn't act, or one of `rerun.SAFE_KINDS`. The request key's digest
includes the test and re-run fields, so the same Idempotency-Key with other options is another dispatch.

### Approvals and sub-workflows

`approval` (graph/nodes.py `_approval`) waits on an event in `GraphRun.approvals`; `Workflows.answer_approval` (POST
`/api/workflows/{id}/runs/{run}/approvals/{instance}`, app session only) sets it through `GraphRun.answer`, reaching a
sub-workflow's run through `children`. `GET /api/workflows/approvals` lists what waits. Events: `batch_approval` (asked,
then answered or `timed_out`) and a `batch_notification` with `approval`. `workflow.call` (`_call`) runs the child's
published graph (or a previously published `revision` from `workflows/history.py`) as its own graph run: a
`workflow_batches` row with `parent`
and trigger `trigger.manual`, key `workflow:<child>:call:c<run>x<seq>` (`:scheduled:` when the parent was scheduled, so
its tasks read as scheduled), the parent's `create` (conversation, origin), the parent's `Slots` (one step per phone
across both), limits capped at the call's reserved share and the parent's time left. Its run answers to its parent,
never to a dispatch; `stop_all` reaches it.

`output` validates a rendered Result against its saved `schema` before marking the node done. A graph with a declared
Result shape needs its Result completed on the run's path; handling its error alone cannot complete the run.
`GraphRun._outcome` checks that Result's final value against its saved shape before reporting completion.

### App reads and replay modes

`app.read` runs a task with run field `packRead` `{pack, reader, n, arg}` (packs/register.py `pack_read`, a pre-run hook
at order 3 beside `packStats`): the pack's reader by code, read only, ending with `data: {rows, columns, title, stopped}`
and a table. A phone step's `mode` rides as run field `replay`: `off` makes the learned workflow hook and the saved
program hook step aside; `only` makes the program hook step aside, and `graph/nodes.replay_only` (order 30, after
learned 20 and programs 24) ends the run with no model call when nothing replayed to the end.

### Words to a graph

`POST /api/workflows/draft {"words", "graph"?}` → `graph/draft.py`: the drafter (`starters.draft_client`, Claude Haiku
5.5, one attempt) fills `PLAN_SCHEMA` (steps with `after` lists, `inside` for loop bodies, `<id>:<port>` for branch
cases and approvals); `compile_plan` builds the graph by code (a read step before a loop over items feeds its `rows`
into `items`; body steps with no `after` start from the loop's `item`) and lays it out in rows by depth;
`schema.validate` checks it and its problems go back to the drafter at most `REPAIRS` (2) times. With `graph`, steps
that stay keep their places and the answer adds `patch` from `graph/edit.diff`. No key: `draft.local`, a chain of the
sentences. The words are the person's; nothing from a screen reaches the drafter.

### Ask the model

`ai.ask` (graph/nodes.py `_ai_ask`) makes one call through `Runtime.model_result`: a task with no phone, made as
`compose_result` makes one, checked against the monthly limit before the call, whose call is metered (`costs.meter`)
into its own events, so `usage_calls`, `journal.spend_since` and the monthly limit count it. Idempotent by key: a
repeat never calls again. The prompt is the person's instruction, then the input fenced by `harness_api.fence`; the
system prompt says the input is data. No key: the step fails "needs an Anthropic key" and nothing is called.

### Schedule rules

`schedule_rule(workflow, name)` reads the published `trigger.schedule`'s `missed` and `overlap`. `tick` claims a slot
more than `LATE_TOLERANCE_MS` late with `missed: run_once` instead of `skipped_missed` (the claim moves `nextAt` past now,
so one run however many slots passed), and `recover` leaves such a slot for the first tick. With `overlap: queue_one`, a
scheduled graph dispatch that finds the workflow running records outcome `queued` and the record's `queuedDispatch`
(one at most; another is `skipped_busy`); `tick` starts it (`_start_queued`) once the workflow isn't running.

### Pacing, approval screens and cost

- **Pacing.** `accounts/screen.AccountCheck.paced` emits `pacing_stop {account, code, until}` for every stop. A phone
  step whose task ends `blocked` with a `pacing_gap` or `pacing_daily` stop and no outward act (`nodes.outward`) raises
  `nodes.Paced`; `GraphRun._paced` frees its slot and queues it (`queuedReason: "pace"`, `until`), and `_start_ready`
  skips it until then. The next attempt has its own key (`r<n>`, `inst.attempt`, shared with retries). A wait past
  `maxMinutes` fails `time_limit`, more than `MAX_PACES` (20) fails `blocked`; both record `acted: false`.
- **Approval screens.** `_approval`'s request carries `screen` from `_screen_before`: the newest frame event of the
  newest finished task among the approval's upstream nodes (through `run.incoming`) in its own scope chain. It reads
  kept frames and never touches a phone.
- **Model calls and estimates.** `nodes.metered` counts `inference_finished` events per task into `inst.model_calls`,
  `inst.replayed` and `run.model_calls` (the record's `modelCalls`). `graph/estimate.estimate` is pure: node history
  (the last `HISTORY_RUNS` done, unreused, unpinned rows per node), `costs.smart_estimate` through
  `Runtime.engine_estimates` for steps that haven't run, rounds for `times`, `rows` and `phones` loops, and the called
  workflow's own total (`MAX_CALL_DEPTH`). `WorkflowRuns.estimate` gathers them for `GET /api/workflows/{id}/estimate`.

### Ways in

`workflow_tools.tools` is the one definition of a workflow made a tool (`asTool`, published): MCP name, slug, REST path,
`inputSchema` (`tool_schema`), `outputSchema` from a Result step's shape. Slugs follow save order; a duplicate, a
reserved route name (`RESERVED_SLUGS`) or a 12-hex slug takes 4 characters of the id. `GET /api/workflows/tools`
serves it; `threads/mcp.WorkflowsProvider` reads it (no naming of its own). `POST /v1/workflows/<slug>`
(`Workflows.run_tool`) checks the body against `inputSchema`, starts the published workflow with one Idempotency-Key and
waits through `WorkflowRuns.follow` (the graph run's thread, or the task's condition) up to `wait_seconds`. A run with
`trigger: "shortcut"` and `input` (`Dispatch.trigger`, `Dispatch.input`, app session only) enters at
`trigger.shortcut` (`graph/dispatch.entry`), else `trigger.manual`; both carry `input` as data. `mobster workflow show`
(threads/workflow_cli.py) prints `graph/chain.ordered` with each step's incoming edges.

### Run controls

`Workflows.prove` saves the draft (`update`) and starts a test run with `prove` (Dispatch, record `prove: true`, in
the request key's digest). `batch_finished` calls `_proved` when it completed: pins (`PROVE_PINS`, top-level done
unreused instances, by port without `error`, within `MAX_PIN_BYTES` and `MAX_PINS_BYTES`) go into the draft it ran as a
new revision, and `proven {revision, batchId, at, pinned}` is written beside it; a revision that moved meanwhile
gets neither. `graph/rerun.resume_all` (behind `WorkflowRuns.resume_all`) checks each run (kept, not running or completed, `resume_preview` has something
to run), then one thread (`_resume_each`, one per workflow) starts each as `rerun={"resume"}` with key
`<Idempotency-Key>:<batchId>` once the workflow isn't running, follows it a second at a time, and publishes
`resume_queue`. `close()` sets `_closing` and joins those threads before stopping batches, so none writes to a closed
journal. `timeline` merges the node rows (with `readyAt`, set when an instance is queued) and each task's frame events.

### Triggers from outside (workflows/triggers.py)

`Workflows.triggers` (`WorkflowTriggers`) owns `workflow_seen (workflow_id, node, key, seen_at, last_seen, read_at, row,
run_key)`. `notify(notification)` is the sink the notification watcher calls for workflows whose graph subscribes; `fire(workflow_id, notification)` starts one workflow a rule outside the graph matched (Settings › Triggers), with the same limits; `listening()` says whether any graph subscribes, so the watcher reads the stores. A notification or a Shortcut's input for a graph without its trigger enters at `trigger.manual` as `input`, and a one-task workflow's task gets it as `workflowInput` (data). `notification_of` checks and masks it,
`matches` applies the node's `app` and `contains`, the dispatch key is `workflow:<id>:notification:<digest>`, and
`NOTIFY_PER_HOUR` counts claimed dispatches. One that arrives mid-run takes the workflow's one waiting slot (`queuedDispatch` with `queuedEvent {trigger, event}`, shared with a schedule's `queue_one`; `public()` drops `queuedEvent`), and `_start_queued` dispatches it after the run; another while one waits is `skipped_busy`. `tick` (from `WorkflowRuns.tick`, under the lock) starts an app trigger's
read (`workflow:<id>:poll:<node>:<ms>`, `packRead`, `queue=False`), takes a finished read into the seen-set (the first
only primes it; rows read newest first are stored oldest first; `last_seen` keeps keys the app still lists), and starts
the oldest pending row when the workflow isn't running: the row's `run_key` is written, then the dispatch is claimed,
then `_dispatch` runs `Dispatch(trigger="app", event={row, rows})`. `graph/dispatch.unattended` names the keys nobody
watches start (scheduled, notification, app, poll): their runs take origin `schedule` and the lock guard's schedule
mode (server.is_scheduled). `enabled` may be true without a cron when the graph has one of these triggers (`_listens`).
