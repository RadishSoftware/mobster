# The replay core: record once, replay with no model, heal as a diff

9 Oct 2026. The code is `mobile_agent/replay_core/` (the engine and the check front end). User docs: docs/testing.mdx
(Replay and heals) and docs/schedules.mdx (Learned workflows).

A check or a saved workflow that passed once is a known path. The replay core keeps that path with what each step needs
to find on the screen, and runs it again through WebDriverAgent with no model call. When the screen changed, the model
redoes that step only, and the change is written down for review. It never changes silently, and it never changes a
check's expectations.

## What a path holds

A path is the list of actions a passing Smart run executed, taken from the frontier's step observer
(`harness_api.StepRecord`) and the screens the frontier read (a recording wrapper around its driver). Each step:

| field | what it is |
| --- | --- |
| `op` | TAP, LONG_PRESS, TYPE, TYPE_SUBMIT, SET_TEXT, SUBMIT, the swipes, LAUNCH_APP, HOME, TAP_XY, DISMISS |
| `app` | the bundle in front before the action (for LAUNCH_APP, the bundle it opened) |
| `target` | the element acted on: label, role, its tree path, its frame in screen fractions |
| `screen` | the screen before: state.py's structural signature (role/label pairs, at most 200) and its hash; coordinate steps also keep the full content fingerprint, including geometry |
| `text` / `param` | typed text a replay may type: a literal from the check's own words or a search query, or a blank a workflow fills |
| `commit` | the recorded or effective approved act when the step sends, posts, deletes, buys or submits |
| `live` | the model must do this step each run: text it made up, a macro read (READ_LIST, SCROLL_TO) |

A stored path is validated whole (`path.validate`): an unknown op, a malformed target or text without a source drops
it, and the next run records afresh.

## Matching a step

`engine.match` decides whether a step can run on the screen as it is:

1. **The app.** The screen is in the step's app. An unknown foreground app refuses an app-bound step. LAUNCH_APP, HOME
   and WAIT need nothing.
2. **The screen.** The recorded hash, or a structural likeness of at least 0.8 (Jaccard over role/label pairs, the
   measure the first replay used, in September). Once two reads in a row agree (the screen stopped moving), 0.5 is
   enough when the target is there exactly once. A step learned from a step record has only its screen's hash; it is
   pinned by its target once the screen is still.
3. **The target.** Exactly one element with the recorded label and role (case and spacing ignored); the recorded tree
   path breaks a tie. Never a partial label.

`TAP_XY` and `DISMISS` also require the exact recorded screen content fingerprint, including dimensions and control
geometry. A legacy coordinate step without this proof drifts before dispatch. An ordinary labeled tap can still use
structural matching and healing.

Teaching freezes the screen before sending each gesture. Taught coordinate steps and practice corrections keep that
content fingerprint through storage. Teaching another layout adds a coordinate version for its exact geometry;
structurally identical labels do not merge different layouts. Teach a legacy coordinate step again to record its
missing proof; replay never invents one.

`Replayer.wait_for` polls every 0.15 s. A step whose target never shows has drifted once the screen stayed the same
for two reads after 1.0 s, or after 3.0 s at most: waiting longer changes nothing.

## Heals

A drifted step goes, in order, to:

1. **Keep failing.** A step the reviewer rejected (`strict`) fails the check, class `drift`.
2. **No healer.** `--no-heal`, or no key: the check fails, class `drift`.
3. **The circuit breaker.** When healing would make more than half the recorded steps healed, the replay stops and the
   check fails, class `drift`. It is checked before a heal starts, and the healer is stopped (through its `cancelled`)
   before the action that would cross it.
4. **Smart, for that step.** A FrontierAgent on the check's own request, told which recorded step changed and why, with
   the replayed steps as its history. After each of its actions the screen is matched against the drifted step and
   the next four; at the first match the healer is cancelled and the replay rejoins there. A healer that finishes the
   flow ends the replay. Its actions replace the drifted steps; one that rejoins at the drifted step itself added
   steps (a sheet dismissed) and replaced none.

Each heal becomes an entry: `step`, `from` and `to` in words, `why`, the old and new anchors, and frames before and
after. A check's run lists them in `healed` (result.json, and the suite's results.json, whose `diff` is relative to the
suite folder for the Test this PR reader). A passing run writes them to `<check>.heal.yaml` beside the check and stages
the complete candidate privately. The approved cache stays unchanged until `--accept` verifies the approved revision,
check, and evidence and promotes the candidate. `--reject` keeps the approved steps and makes the changed positions
strict. A run whose heal led to a failed assertion saves nothing. If the candidate cannot be staged, the result is
`couldnt_run`; it cannot report an unreviewable heal as passed.

An accessibility baseline refresh applies an empty baseline for that run and does not publish a new replay path or a
healed proposal. If the replay heals, the result is `couldnt_run` with `proposal_unavailable`; the approved path and
old baseline stay in place. Run `mobster test` without `--update-a11y-baseline` to record or review a changed path,
then refresh the baseline in a separate run. A baseline replacement still needs complete accessibility observations.

A `mobster test minimize` trial runs fresh from a draft file and does not publish a replay path. A trial with the
saved check's steps and app file name has the saved check's identity, so saving it would replace the approved path.

## The cache

`.mobster/cache/replay/<identity 16>-<env 8>.json`, 0700 and 0600, written atomically.

- **Identity**: the check's steps, bundle, app file name, launch arguments, environment, link and reset. Not its name
  and not its expectations.
- **Env**: the app's version and build, the iOS major version, device model, and simulator or iPhone. A new build,
  iOS major, or model records
  afresh, and the run says why ("the app changed (recorded on 1.0 (1), this is 1.1 (2))"). An identity keeps its
  four newest environments, so a matrix over several iOS versions keeps one path each.
- A failed heal keeps the approved path. An action that raises, an unconfirmed text receipt, a failed read after an action, a model
  callback that raises, or a heal that ends with an unknown action (`frontier_failed` with `outcome: unknown`) is
  `couldnt_run` class `unknown_outcome` (`Result.model_status == "unknown_outcome"`): no heal, no next action, no
  retry, and a learned workflow doesn't hand the task to the model. Inspect the app before you run it again.

On a real iPhone the app's version is not read (`WdaDevice.installed_app` has none), so only the iOS major and the
model keep the cache honest there. A missing physical model never selects an approved check path.

Repeated checks freeze the effective check, approved path revision, external evidence, device model and full iOS
runtime. The normal cache key groups iOS by major; a repeated attempt refuses a minor runtime change before app reset,
install or launch rather than silently measuring another environment. Simulator acquisition and WebDriverAgent startup
may already have occurred.

## Safety

- A replayed commit asks the approver the live run would ask: a check's `check_approver` (every commit refused on an
  iPhone, every classified commit approved on a simulator), a workflow's Ask before acting. With no approver a
  workflow hands that step to the model; a check refuses it. After approval, replay reads the screen again and
  dispatches only when its full fingerprint still matches and the app is still allowed. It uses the newly read
  target. The effective approved act stays with the executed step, so a later drift cannot hand a sent, posted or
  deleted action to the model.
- iOS's own dialogs in a check replay go to `Replayer(dialogs=...)`, a `verify.dialogs.Responder`: it reads the dialog
  before each step, when a step doesn't match, and once after the last step, and answers it from the check's `dialogs`.
  The answer is no step and no heal. A dialog the policy won't answer ends the replay with status `dialog`
  (`couldnt_run`, class `system_dialog`); a press that fails ends it too and is never repeated. The run's policy
  keeps the last dialog it answered (`Policy.pressed`), shared with Smart's heal steps, so the same dialog again, in
  this `clear()` or a later one, ends the replay too. Only iOS's own dialogs are pressed (`Policy.decide`): an alert
  needs SpringBoard or the exact Apple process for its kind in front and none of its lines in the app's tree, a sheet
  needs that process in front and its own tree read (`system_dialogs.SHEET_FRONT`). Anything else is left to the
  replayed step and its approver.
- Typed text comes from the check's words (`recorder.literal_text`) or a workflow's blanks. A query typed into a search
  field (`contract.is_search_field`) replays as typed: it is navigation (owner, 10 Oct). Masked text never replays.
  Anything else Smart wrote is a live step.
- Learned workflows (Mobster for Mac) never hand over to the model after a replayed send, post or delete: the run ends
  "Check the result", so nothing is done twice. They claim done only when the final screen shows the learned run's
  proof quotes (blanks filled), else "Check the result".

## Measurements

Measured 9 Oct 2026 with `scripts/bench_replay.py` (n = 10 runs of each), on the scripted world: Mobster's own code end
to end, with the phone's reads and actions timed as measured on a USB iPhone (a tree read 0.35 s, a tap 0.25 s, typing
0.6 s, verify's evidence read 0.76 s) and each model call timed at 2.5 s (the task contract 1.5 s). Cost is the prompts
the frontier really built, at 4 characters a token plus 150 output tokens a call, at claude-sonnet-5-5's list price
with no prompt caching. This world sends no screenshots, so the fresh costs are floors.

| Flow | Mode | Median time | Model calls | Model cost | Result |
|---|---|---|---|---|---|
| Check: Lumen sign-up (VerifyRun) | fresh (Smart) | 21.9 s | 6 | $0.0104 | passed |
| Check: Lumen sign-up (VerifyRun) | replay | 6.3 s | 0 | $0.0000 | passed |
| Check: Lumen sign-up, button renamed | replay + 1 heal | 11.7 s | 2 | $0.0034 | passed |
| Workflow: Notes: save a note | fresh (Smart) | 15.9 s | 5 | $0.0093 | completed |
| Workflow: Notes: save a note | replay | 2.5 s | 0 | $0.0000 | done |
| Workflow: Messages: text Alex (asks first) | fresh (Smart) | 15.7 s | 5 | $0.0096 | completed |
| Workflow: Messages: text Alex (asks first) | replay | 2.5 s | 0 | $0.0000 | done |

Check: replay is 3.5x faster than fresh (21.9 s → 6.3 s, the verdict read and report included), and costs $0.

The check replays 3.5x faster (21.9 s to 6.3 s, the verdict read and the report included: the replayed flow itself took
2.6 s of it) at $0, and its one heal costs one model turn plus the task contract. A real Smart run of a two-step check
cost $0.0246 on 28 Sep 2026 (the verify smoke run, with screenshots), so the real saving per run is larger than
this table's. Not measured yet: a replay on a real iPhone, and the cache-hit rate across app builds.

## Later

Cross-machine sharing of recorded paths, a hosted heal history, heals for visual checks, and replay of macro reads
(READ_LIST, SCROLL_TO), which stay live steps for now.
