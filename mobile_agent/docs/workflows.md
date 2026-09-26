# Saved tasks and schedules

Saved workflows share the private run journal and its process lease. Creating a workflow never starts an agent, and every workflow starts with `enabled: false`, no cron expression, and `outputFormat: "text"`. Tasks always use `Runtime.create`, including its installed-app, live readiness, device lease, idempotency, and durable run checks. This is the local single-operator service, not a multi-tenant scheduler.

## API contract

`GET /api/workflows` returns `{ "workflows": [...], "schedulerError": null }`. A scheduler persistence or unexpected runtime failure stops scheduling and supplies an explicit error; it does not silently keep retrying.

Each workflow contains `id`, `name`, `appId`, `goal`, `outputSchema`, `outputFormat`, `cron`, `timezone`, `enabled`, `revision`, `createdAt`, `updatedAt`, `nextAt`, `lastRunId`, and `lastOutcome`. Dates are UTC epoch milliseconds. A null `nextAt` means paused/manual; a null last outcome means never dispatched. `revision` versions configuration edits, not background task outcomes.

`POST /api/workflows` accepts `name` (1–120 characters), `appId`, `goal` (1–4000 characters), optional `outputSchema`, and optional `outputFormat`; it returns `{ "workflow": ... }` with HTTP 201. Task definition fields remain immutable. Saving an edited task creates a distinct workflow.

`POST /api/workflows/{id}` accepts the current integer `revision` and optional `name`, `cron`, `timezone`, and `enabled`. It returns the updated workflow; an outdated revision returns HTTP 409 with `revision_conflict`. Scheduling requires an explicit update with `enabled: true`. Pausing cancels future occurrences, not an already-started task.

`POST /api/workflows/preview` accepts `{ "cron": "0 9 * * mon-fri", "timezone": "America/Los_Angeles" }` and returns `{ "next": [three UTC epoch millisecond timestamps] }`. Preview never persists or runs anything.

`POST /api/workflows/{id}/run` accepts an empty JSON object and requires exactly one `Idempotency-Key` header. It returns the canonical `{ "run": ..., "replayed": false }` with HTTP 201, or the same prior run with HTTP 200 and `replayed: true`. A failed/uncertain dispatch with that key is never repeated; the operator must inspect status before deliberately issuing a new key. Retired run IDs return HTTP 410 rather than restarting.

## Timing and safety

Only five-field standard cron is accepted. Month and weekday names are supported. Seconds, year fields, aliases, random/hash syntax, nth weekdays, and last-day extensions are rejected. Day-of-month and day-of-week use standard OR semantics.

The schedule uses an explicit IANA timezone and timezone-aware cron iteration. During a fall-back hour, repeated wall-clock times are distinct UTC occurrences. The preview exposes those exact instants. Missing spring-forward times follow croniter's timezone rules; no separate hidden timezone conversion is applied. The calculation is bounded to eight years between matches.

Validation conservatively limits a matching calendar day to 60 slots and enforces at least five minutes between minute/hour combinations, including the midnight boundary. Runtime adds a shared limit of 60 scheduled dispatch attempts per rolling 24 hours and a per-workflow five-minute interval. Skipped busy/offline occurrences do not consume that execution budget; uncertain dispatches do. Manual runs remain explicit operator actions, not scheduled executions.

Claims and the next occurrence are committed atomically before calling the runtime. No SQLite transaction is held during device readiness or run creation. If a crash occurs after the canonical run commit but before the claim outcome commit, recovery links the existing journal request to the claim without calling the runtime again. If no canonical request exists, the claim is marked interrupted, not retried.

Startup skips all missed slots, including recently missed ones. While running, a tick more than 60 seconds late skips that occurrence and advances directly to the next future occurrence. Busy/offline slots are recorded and skipped. There are no catch-up bursts. Configuration changes and dispatch initiation serialize so a completed pause cannot be overtaken by an unstarted scheduled dispatch.

The scheduler starts only in `serve`, not when constructing a runtime for tests. Shutdown stops and joins scheduling before closing the run journal. If a dispatch cannot stop within the deadline, the journal and leases remain open until process exit. Saved definitions are limited to 200, durable dispatch tombstones to 10,000; reaching retention capacity fails closed and requires operator maintenance rather than deleting replay protection.

## Verification and sources

Tests use temporary databases and fake runtimes only. They never enable a schedule or dispatch work on the user's database/device.

Scheduling uses pinned croniter 6.2.4. Current API and timezone behavior were checked against the maintainer's [documentation](https://github.com/pallets-eco/croniter) through Context7 and the [published package](https://pypi.org/project/croniter/).
