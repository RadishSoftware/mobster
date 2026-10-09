# The harness: steering, questions, picking up, prewarm

Mobster's agent (Smart, `frontier.py`) takes direction while it works, can ask when a request truly reads two ways,
can be picked up after an interruption, and gets the phone ready while you type. Quick mode (Fast) runs as before.
None of it weakens an approval: a message, an answer, a pause or a prewarm never approves a send, buy, post or delete,
and every one of those still asks with its exact text.

The code is `mobile_agent/harness/` (track `harness`, SPEC §3.1), the loop's side is in `frontier.py`. The Mac app's
pieces are in `dashboard/src/features/harness/`. The events are listed in [engine-contract.md](engine-contract.md).

## Writing to a running task

`POST /api/runs/{id}/messages` with `{"text": "...", "source"?: "voice"}` answers `202 {"message": {id, text, at,
source}}`. The model reads every message at the top of its next turn, in order, and keeps the newest in view (10
messages, 2,000 characters: only messages it has already read ever drop out). A pending message stops a chain of
actions before its next one.

- `source` is the request's `X-Mobster-Origin` (`app`, `tui`, `cli`, `mcp`; a request without one counts as `cli`).
  The body may only say `voice`, and only from the Mac app. A task started over MCP takes messages only from MCP or the
  Mac app; any other task never from MCP (`403 steer_not_allowed`).
- `409 approval_pending` while an approval waits: nothing is queued. "Yes, send it" typed then is not an approval;
  the approval sheet is the only way to approve.
- `409 run_not_active` once the task has finished, `409 steer_unsupported` for Quick mode, `429 steering_full` when
  10 messages wait unread.
- Messages the task never read come back as `steer_unread` when it ends.

`POST /api/runs/{id}/messages` with `{"control": "stop_after_step"}` answers `202`: the action under way finishes,
nothing new starts, and the task ends `stopped` ("Stopped after the step in progress, as you asked."). It is a control,
never parsed from text.

## Pause and continue

`POST /api/runs/{id}/pause` with `{"paused": true}` or `{"paused": false}` answers `{run, paused}` (`409
run_not_active`, `409 pause_unsupported` for Quick mode). The loop holds at its next step boundary (before the next
model call, or between chained actions, which stops the chain) and touches nothing: no read, no action. After
Continue it reads the screen again, since you may have used the phone, and tells the model that actions it chose
before the pause did not run. After a pause of 20 seconds or more the phone check runs first, as after a long
approval: a phone that locked meanwhile ends the task `blocked` (`phone_locked`), and it can be picked up once
unlocked. Waiting never counts against the task's time. Stop works while paused; 10 minutes paused ends the task
`stopped`.

## Clarifying questions (`ASK_USER`)

Offered only in a task a person watches (started from the Mac app, the terminal UI or `mobster chat`) whose surface
can show a question and says so with the run field `askUser: true` (until it does, a question would reach the person
as an approval sheet). A conversation is such a surface: a task started in a thread from the Mac app, the terminal UI
or `mobster chat` gets `askUser: true` from the conversations service, and the question shows inline as a clarify
item. Never in workflows, schedules, MCP or the plain API; at most twice a task. Its prompt line:

> ASK_USER text: only when the request can be read two ways the screen can't settle and a wrong guess would change
> what is sent, bought, posted, deleted or answered. Never to confirm a send (that asks by itself). Put up to 4 short
> answers after the question, each after " | " ("Which Sam? | Sam Lee | Sam Ortiz").

It asks through the run's approval channel with `kind: "clarify"`: the question, up to 6 choices and a typed
answer. Answer with `POST /api/runs/{id}/approval` `{"id", "approve": true, "choice"}` or `{"id", "approve": true,
"answer": "..."}` (token only: an answer approves nothing), or `{"id", "approve": false}` to skip. The answer goes
to the model as "The user answered your question: ..."; it is never in `approval_resolved`. Nobody answering in 10
minutes ends the task `approval_timeout`, and the wait never counts against the task's time.

Settings: Settings › Advanced › **Mobster may ask me questions** (on by default) is `GET /api/harness` → `{askUser,
askUserLimit, pauseLimitSeconds, ledger, earlyLaunch, longRun}` and `POST /api/harness {"askUser": bool}`, kept in
the run journal. The setting off, or `MOBSTER_ASK_USER=off` for the process (the owner's A/B), wins over a task's
`askUser: true`. Without the tool, the prompts are exactly as before.

## Checkpoints and picking up

At the end of every Smart turn the agent's state (plan, notes, the last 60 history lines, turns used, spend, the app
in front) is masked, passed through the secret filter and kept as the task's checkpoint in the journal (table
`run_checkpoints`, schema `harness` v1, at most 64 KB a task), off the agent's thread. It never holds an approval's
text or a code. A task that ends done, declined or at a spending limit drops its checkpoint; the rest go with the task
when history drops it.

`POST /api/runs/{id}/pickup` with `{}` and an `Idempotency-Key` starts a **new** task with the same goal, app, phone
and conversation, and `extras.resumeFrom` naming the old one: `201 {run, replayed}`. It works for a Smart task that
ended `interrupted` (Mobster restarted), `error`, `stopped`, `approval_timeout`, or `blocked` because the phone was
unplugged or locked, once it has a checkpoint. Refusals: `409 no_checkpoint` ("There's nothing to pick up yet...")
`409 not_resumable`, `409 run_active`, and `409 picked_up` with the `runId` of the task that already picks it up (the
same key replays that one instead).

The new task starts from the checkpoint (`picked_up {from, step}`): its plan, notes, history, turns and spend, 50 more
turns, what you wrote to the old task as a note, and the feedback "You're picking up a task after it was interrupted.
The last action may or may not have happened: look at the screen before repeating anything.", naming any send or
other commit you approved before the interruption. It reads the screen before its first decision, so nothing is done
again without a new decision, and a send still asks. Picking up is never automatic.

## Prewarm

`POST /api/prewarm` with `{}` or `{"device": "..."}` answers `202 {warmed: [...], pending}` within 1.5 s and never
5xx for a cold phone. On a worker thread it resolves WebDriverAgent's session (creating it if the helper has none),
applies the session settings, learns the screen size and the loop's tune (`"wda"`), then keeps one settled screen read
with a frame clock for 3 s (`"screen"`). A task's first read reuses it only when the live stream shows not one changed
byte since that read began, on the same session, with the task's start app (or the Home Screen) in front; anything
else reads afresh. Prewarm never acts, never touches a phone with a task running (in this Mobster, or another one
holding the phone's lease, which it checks for an instant and lets go), and calls within 2 s share one warm-up.

The Mac app's composer calls it when it opens and 1 s after you stop typing, at most once every 20 s.

On the fake-latency harness (`tests/test_harness_prewarm.py`, assumed latencies: 1.5 s for WDA to create a session,
0.6 s for a new process's configure, 0.8 s for a settled read, 2.6 s for the task contract and 2.4 s for a decision),
time to the first action fell from 5.8 s to 3.2 s (45%) for the first task after the helper starts, and from 3.8 s to
3.2 s (18%) for a later task whose screen didn't change. These are not phone measurements; the owner's test plan
times it on a real iPhone.

## Tools other tracks register

`harness_api.register_tool` tools pass the policy in `harness/tools/` before the model sees them: a new op of capital
letters, `prepare` and `perform`, a one-line prompt that starts with the op, `interactive_only` tools only where a
person watches, `available(SkillContext)` true, and at most 8, in their order. A tool's prompt line is in the prompt
only when it is offered. A skill may report `SkillResult.waited` (seconds spent waiting for the person, which don't
count against the task) and `SkillResult.status` (`approval_timeout` or `stopped`, which end the task with its
feedback as the reason).

## Not in this build (v2)

The long-run ledger and its condenser (`MOBSTER_LEDGER`), long-run mode (`budget`, `mobster run --long`), subtasks,
launching the named app early (`MOBSTER_EARLY_LAUNCH`) and warming the model connection.
