# Architecture of the `mobile_agent` framework

Written 2026-09-24 against the code in this tree. Every module, function, constant and
environment variable named here exists under `mobile_agent/`. Measured numbers are quoted from
the dated reports they come from; nothing here is a new measurement.

Mobster drives an iPhone from its accessibility (AX) tree. Its step model is Jev, a typed
decision model: it chooses from a closed set of operations and observed elements, and it
never generates free text or looks at pixels. A small language model (the "helper") writes
text, extracts answers and gives recovery hints. Deterministic code runs everything in between.
That code compiles what the request already spells out, guards every action, and checks every
answer against what was observed.

```
request ──► compiled intent (plans, loops, milestones, routes, keypad, direct URL)
              │
              ▼
        ┌─ observe (drivers.WDA → state.Snapshot) ◄──────────────┐
        │     settle: observe_ready / wait_for_change / FrameClock│
        ▼                                                         │
   decide: replay → route → memo → speculation → Jev.decide       │
        │                                                         │
        ▼                                                         │
   guards: stop gate → action verifier → effect ledger →          │
           approval → fresh-screen check                          │
        │                                                         │
        ▼                                                         │
   dispatch (driver.execute) → settle → record outcome ───────────┘
        │  (on DONE)
        ▼
   completion re-check → extraction → answer verification → result
```

## Contents

- [Entry points and composition](#entry-points-and-composition)
- [Perception](#perception)
- [Decision layers](#decision-layers)
- [Guards](#guards)
- [Extraction and verification](#extraction-and-verification)
- [How a run flows end to end](#how-a-run-flows-end-to-end)
- [Speed mechanisms](#speed-mechanisms)
- [Configuration](#configuration)
- [Measured status and known gaps](#measured-status-and-known-gaps)

## Entry points and composition

There are three ways to start a run. All of them end in `agent.Agent.run` or
`frontier.FrontierAgent.run`.

- **CLI** (`__main__.py`). `run` previews one decision, or executes a bounded task when
  `--execute` is given. `serve` starts the loopback dashboard API.

  ```sh
  python3 -m mobile_agent run 'Open Search' --wda-url http://127.0.0.1:8100 --env-file mobile_agent/.env
  python3 -m mobile_agent run '<goal>' --wda-url http://127.0.0.1:8100 --execute --helper \
      --max-steps 30 --max-seconds 120 --spend-cap-usd 0.05 --allow-app com.apple.mobilesafari
  python3 -m mobile_agent serve --manage-device --env-file mobile_agent/.env
  ```

- **API** (`server.py`). `Runtime.create` admits a task. It validates the request, resolves the
  `Idempotency-Key` against the journal, takes a device lease or a `pool.DevicePool` slot, and
  writes the run to the journal. `Runtime.work` then builds the models, the driver and the
  VisionJudge, checks the phone (`prepare_wda_phone`), launches the app and calls
  `Agent.run(f"In {app}: {goal}", ...)`. The endpoints are in [setup-api.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/setup-api.md) and
  the [framework README](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/README.md).
- **Benchmarks.** `bench/` (MobsterBench-iOS) and `bench/iosworld.py` (the public iOSWorld
  suite) build the same agent in process. `iosworld.py --policy frontier` runs `FrontierAgent`
  instead. See [bench/README.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/bench/README.md).

`compose.py` is the composition root that `serve` and `run` share:

- `build_target_driver` selects the driver;
- `build_models` builds Jev plus the optional helper;
- `build_visual` builds the opt-in screen reader;
- `build_vision_judge` builds the VisionJudge;
- `warm_clients` pre-opens model connections;
- `close_all` shuts everything down without raising.

Run bounds live in one typed place, `config.RunBudgets`: `max_steps`, `max_seconds`,
`max_helper_calls`, `settle_seconds`, `max_undispatched`, `max_extraction_retries` and
`spend_cap_usd`. Per-run mutable state lives in `run_state.RunState`. Framework errors share
the `errors.MobsterError` root, and no subclass is retried by default. `SpendCapExceeded` is a
stop signal. `Cancelled` (operator stop) is control flow: it is deliberately not a
`MobsterError` and is never retried.

## Perception

### Drivers

`drivers.Driver` is the device contract: `observe`, `execute`, `close`. `build_driver` looks up
the `DRIVERS` registry:

| driver | transport | status |
| --- | --- | --- |
| `WDA` | WebDriverAgent over USB (`iproxy`, API port 8100, MJPEG port 9100) | the supported path; setup is in [usb-wda.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/usb-wda.md) |

`extensions.py` is the one place optional code plugs in: an extension package may register
further drivers (`drivers.register_driver`), snapshot sources with their traits
(`state.SOURCE_TRAITS`), command-line flags and a server runtime. The public package ships
none, and the agent behaves the same without one.

Every driver re-checks its target with `grounded_target`, even when called outside the agent
loop. The target must be an element of the supplied snapshot, and TYPE needs an editable
field.

### Reading the WDA tree (`WDA.observe`, `state.from_wda_root`)

- **One cheap source read.** `WDA_SOURCE_PATH` excludes the `visible`, `accessible`, `index`
  and `traits` attributes. The comment on that constant records the effect of dropping
  `visible` and `accessible` in Settings: about 950 ms per read with them, about 120 ms
  without. Visibility is then rebuilt from geometry.
- **Snapshot contents.** The Application node supplies the screen size and the foreground
  `bundleId`. Each on-screen node becomes an `Element` with a label, role, rect (a fraction of
  the screen), `editable`, value, locator (the source path) and allowed actions.
  - Rect edges snap to `WDA_RECT_GRID_PT` (4 pt), so the ~1 pt morph of iOS 26 glass controls
    does not make every read a new screen.
  - `SecureTextField` nodes are never read.
  - Zero-width and bidirectional marks are stripped (`invisible_marks`).
  - Traversal stops at `WDA_NODE_BUDGET` (12,000 nodes).
  - At most 240 elements are offered as targets. Past that, labelled nodes stay as evidence.
- **Occlusion** (`wda_occlusion`). A node is dropped when its centre lies under chrome it does
  not belong to (`WDA_OCCLUDING_ROLES`: navigation bar, tab bar, toolbar, keyboard) and that
  chrome was drawn later. It is also dropped when a modal (`WDA_MODAL_ROLES`: Alert, Sheet) is
  up and the node is outside the frontmost one, or when a later large list pane
  (`WDA_PANE_ROLES`, at least 40% of the screen) covers it. Document order is treated as
  back-to-front.
- **Stacked panes** (`state.stacked_panes`, `WDA._hidden_panes`). Two screen-sized lists can
  overlap, and document order does not always say which is in front: Notes' search results
  and its folder list are one example. The driver then asks WDA which panes are
  `visible == 0`, skips their subtrees and caches the answer for
  `WDA_PANE_VISIBILITY_SECONDS` (20 s).
- **Presented layers** (`Snapshot.stacked_layers`, `WDA._hidden_layers`). A presented sheet or
  cover is a later screen-sized child of a Window. Every such layer except the last is checked
  with WDA's `visible` attribute. The answer is cached for `WDA_LAYER_VISIBILITY_SECONDS` (3 s),
  because presentations change often.
- **Parked keyboards** (`Snapshot.keyboard`). The value is `"visible"` when a software keyboard
  is on screen, and `"parked"` when a Keyboard node sits off screen. A parked keyboard means a
  hardware keyboard, as on simulators, and keystrokes still reach the focused field.
  `WDA._await_keyboard` accepts either. SUBMIT is offered on editable fields only while a
  keyboard exists.
- **Off-screen evidence** (`state.OffscreenNode`, `Snapshot.offscreen_evidence`). Labelled
  nodes outside the viewport are kept as read-only evidence, up to `WDA_OFFSCREEN_LIMIT` (800).
  WebKit exposes the whole page, so a Safari article can be read without scrolling. These
  nodes are never tap targets. `WDA.scroll_to` brings one into view with long glides, then
  re-observes it as an ordinary element.
- **System-overlay recovery.** On iOS 26.0.1, WDA sometimes resolves the active app to an
  invisible system overlay, and source reads fail after about 8 s. A failed read is retried
  once with WDA's default active application set to the app Mobster opened. The hint is
  remembered per WDA URL (`WDA.load_overlay_hints`).

### Screen identity

`Snapshot.fingerprint` hashes everything a decision can see, including the revision and the
locators. `content_fingerprint` leaves out the revision. The agent uses the strict form to
authorize dispatch, and the content form to detect whether an action changed anything.
`same_screen` lets a visual fingerprint refine AX identity, but never replace it.

### Settling

A read taken mid-animation is not a decision input.

- `WDA.observe_ready` reads until the screen has not changed for `WDA_SETTLE_QUIET_SECONDS`
  (0.5 s), or until one read matches a screen already proven at rest (`WDA_SETTLED_MEMORY`).
  Screens still moving at `WDA_SETTLE_CAP_SECONDS` (1.5 s) return their latest read.
- `WDA.wait_for_change` does the same after an action. It also calls `on_settling` the first
  time two reads agree, which lets the agent start speculative work.
- `frame_clock.FrameClock` treats WDA's ~30 fps MJPEG stream as a clock. The mode comes from
  `MOBSTER_FRAME_CLOCK` (`on`, `shadow` or `off`; the default is `on`). With pixels still, the
  AX quiet period drops to `WDA_FRAME_AX_QUIET_SECONDS` (0.3 s). AX is never skipped. In the
  2026-09-23 validation (24 navigations on one iPhone 15 Pro) mean navigation settle fell from
  1,122 ms to 996 ms, with 0 premature settles.
- Scrolls use a "glide", a W3C drag with a decelerating end that moves an exact distance with
  no fling (`WDA_GLIDE_SEGMENTS`). It is enabled for `WDA_GLIDE_BUNDLES`, which can be extended
  with `MOBSTER_GLIDE_BUNDLES` or turned off with `MOBSTER_GLIDE=0`.
  `drivers.clear_stroke_start` moves a swipe's start off sliders and switches.

### Optional visual reading

The AX tree is blind to drawn content. These components are opt-in and never inferred from an
empty tree:

- `vision_judge.VisionJudge`: calibrated yes/no/enum judgments on crops cut at an AX element's
  frame. It runs a tier cascade: Apple Vision on the Mac (`AppleVisionScreen`,
  `MOBSTER_VISION_T0`), then batched Gemini calls, then a higher-resolution second look, then
  abstention. It never picks an action target.

## Decision layers

`Agent.run` applies the layers below in order. Each layer either finishes the request or hands
it to the next. [compiled-intent.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/compiled-intent.md) documents the compiled layers in
detail; this section places them in the loop.

### Before the step loop

1. **Replay lookup** (`replay.ReplayStore`). A run that passed every gate is stored as its
   actions, each with preconditions (a structural screen signature and the target's
   role/label). Only navigation-class operations (`REPLAYABLE`) are recorded, never text. A
   step replays only while its preconditions hold. Any mismatch abandons the rest of the
   recording.
2. **Dataflow plans** (`plans.py`, `Agent._run_plan`). A multi-app question compiles, in one
   helper call, into 2 to 4 single-app steps (`PLAN_MAX_STEPS`) that pass named values. Each
   step runs as its own `Agent` with `loop_mode="off"` and only that app allowed. Values move
   between apps only as cited literals, reshaped by the fixed `TRANSFORMS` (`major`, `number`,
   `year`). Turned off by `MOBSTER_PLANS=0`.
3. **Milestones** (`milestones.py`, `Agent._compile_milestones`). A request with two or more
   distinct action verbs, at least one of which changes something, becomes 2 to
   `MAX_MILESTONES` (8) ordered subgoals in one helper call. The user's prohibitions
   are appended to each one. A doubted DONE on an intermediate milestone becomes "not yet" up
   to `MILESTONE_DONE_RETRIES` times. Turned off by `MOBSTER_MILESTONES=0`.
4. **Compiled loops and surveys** (`loops.py`, `Agent._run_loop`). "Do X to every item" and
   "which of these items…" compile into a statically validated program
   (`validate_program`). `JevTools.pin` binds its controls to observed elements, and
   `LoopRunner` executes it:
   - item judgments come from the VisionJudge (images) or Jev (text);
   - `PRE_GUARDS` and `POST_GUARDS` run around every item;
   - every item is written to the append-only `LoopLedger` before any tap;
   - the first `SHADOW_ITEMS` (3) are cross-checked against the step agent.

   An irreversible loop whose request does not state both a policy for unsure items and a
   stop bound asks once first (`needs_policy_question`). Protected characteristics are refused (`protected_predicate`).
   A survey behind a route compiles when its screen is reached.
5. **First-screen prediction** (`Agent._predict_first_decision`; only when there is no
   replay and no loop). While the launched app is
   still being observed, step 0 is decided on side channels for each remembered first screen
   of that app. The result is used only when the live request hashes identically
   (`decision_key`).

### Inside each step (`Agent._step`)

On step 0 with execution on and Safari in front, **direct URL opening**
(`Agent._open_requested_url`) opens the address itself when the request names one
(`requested_url`), a web search (`requested_search`) or a Wikipedia title
(`requested_article`). It uses WDA's `POST /url` bound to Safari. Turned off by `MOBSTER_DIRECT_URL=0`.

Each step then tries these sources of a decision, in order:

1. **Route** (`routes.compile_route`, `Agent._route_choice`). Screens the request names
   ("Settings > General > About") are tapped by their visible row with no model call. A hop
   that is not visible gets at most `MAX_SEARCH_SWIPES` (5) scrolls. If it is still missing,
   the route uses the app's search field when there is one. After arrival, the asked-about
   row is scrolled into view with at most `MAX_REVEAL_SWIPES` (4) scrolls. Calculator arithmetic that the request states compiles into
   `keypad.calculator_keys`, which the route presses once its hops are done. Turned off by
   `MOBSTER_ROUTES=0`.
2. **Deferred survey, page probe, visual answer.**
   - `Agent._deferred_survey` runs a survey whose items are now on screen.
   - `Agent._page_probe` starts answer selection and verification on a side connection when
     the page may already hold the answer. It runs at most `PAGE_PROBES` (3) times and is
     turned off by `MOBSTER_PAGE_PROBE=0`.
   - `Agent._visual_answer` asks the VisionJudge about the one dominant picture
     (`visual_answer.image_on_screen`), constrained to the request's own answer set.
3. **Replayed decision** (`Agent._replayed_decision`).
4. **Decision memo** (`decision_memo.DecisionMemo`). An identical decision request (the same
   screen and every model input) is answered without a call. Entries are saved only from
   runs that succeeded, and entries a failed run used are forgotten. Turned off by
   `MOBSTER_DECISION_MEMO=0`.
5. **Speculation** (`Agent._speculated_decision`). A decision started during the previous
   settle is used only if its request hashes identically to the live one.
6. **Live Jev decision** (`models.Jev.decide`). One call answers several typed questions:
   - the operation, and a compatible target from the observed elements;
   - `goal_probability` and `blocked_probability`;
   - the `stop_gate` (`CONTINUE`, `STOP` or `UNCLEAR`);
   - on the first step, `output_intent`;
   - `side_effect_risk` and `screen_has_loading`.

   An action whose confidence is below `task_policy.ACTION_CONFIDENCE_FLOOR` for its risk tier
   becomes WAIT (`demoted_from`). The floors are scroll .30, navigation .55, type .56 and side
   effect .85. Tap targets are capped at `TAP_TARGET_CAP` (120), ranked by overlap with the
   request's words.

Jev's decision then goes to `Agent._handle_done`, `Agent._handle_wait` or
`Agent._handle_action`.

### The helper (System Two)

`models.Helper` talks to any OpenAI-compatible chat endpoint, or to Vertex AI through
`gemini.VertexHTTP`. It is used only for:

- field text: `ask("text")`, unless Jev selected a verbatim span of the request. That
  selection is off by default and needs `MOBSTER_TEXT_SELECTION=1`;
- recovery hints: `ask("recovery")`, at most `MAX_RECOVERIES` (2) per run;
- plans, milestones and loop programs;
- answer extraction: `extract` and `extract_auto`.

`hybrid.py` states the split as invariants (`JEV_ONLY_METHODS`, `FORBIDDEN_HELPER_MODES`,
`assert_helper_mode`) and supplies the escalation trigger the agent uses (`escalation_reason`,
`ESCALATION_CONFIDENCE_FLOOR`). Its `HybridRouter` facade is exercised by the tests. `Agent`
itself calls `Jev` and `Helper` directly.

### Frontier step policy (`frontier.py`)

`FrontierAgent` is a separate policy. A frontier chat model (`OpenAIChat` or `GeminiChat`,
selected by `chat_client`) chooses every step, over the same perception and driver.

- **Input.** Each call sees the request, the allowed apps, its own plan and notes (up to
  `MAX_NOTES`), the recent action lines (`HISTORY_LINES`) and the screen as short aliases
  (`screen_rows`: `e1`, `e2`…, with frames as percentages). A small screenshot is added
  (`screenshot_part`); it comes from the MJPEG stream when the screen is still
  (`video_frame`). The model never supplies coordinates.
- **Action chunks.** One call returns 1 to `MAX_CHUNK` (6) actions. Later actions name their
  target by exact label (`target_label`). A chunk runs until a target is missing or an action
  changes nothing, and then the model sees the new screen.
- **Extra operations.** `SET_TEXT` clears a field (`WDA.clear_text`) and then types.
  `DISMISS` taps the first point in `DISMISS_POINTS` that no element covers.
- **Guards in code.**
  - Only listed apps can be launched.
  - `frontier.guard` refuses a tap on a commit control (`task_policy.COMMIT_CONTROL`: send,
    pay, delete…) unless `task_policy.requested_by` finds that act in the request.
  - Repeats are refused, judged on the screen. Typed text repeated on the screen its typing
    left is refused at once; any other action repeated on the screen it was made on (it did
    nothing) is refused the second time. A third identical action on the same screen within
    the last `LOOP_WINDOW` (12) is refused as a loop (`LOOP_REPEATS`). Swipes, `DISMISS` and
    keypad keys are exempt, and working down a list (another "Pay" on a changed screen) is not
    a repeat.
  - The first element action re-reads the screen and re-finds its target.
  - Every refusal goes back to the model as feedback and appears in the trace.
- **Cost cap.** `cost_usd` prices usage from the `PRICES` list-price table, and the run stops
  at `max_cost_usd`. `bench/iosworld.py` caps each task at $0.10 plus $0.08 per app the task
  spans, at most $0.60 (`task_cost_cap`, `MAX_TASK_COST_USD`). The 24 Sep smokes ran with a
  flat $0.25.

This policy does not run the step agent's stop gate, action verifier, effect ledger or answer
verifier. Its answer is the model's `DONE` text, not a cited extraction. It is wired only into
`bench/iosworld.py --policy frontier`. The dashboard API and the `run` command use
`agent.Agent`.

## Guards

Each guard fails closed. A refused action is either never sent, or re-decided with a hint that
explains the refusal.

| guard | where | what it enforces |
| --- | --- | --- |
| Stop gate | `Agent._stop_if_required`, `task_policy.StopGate` | Jev's reading of whether an explicit condition in the request says to stop. STOP without stop wording is ignored (`has_stop_condition`). UNCLEAR ends the run unless the step only looks, or the act is one the request names. |
| Operation and target validity | `Agent._handle_action` | Host keys only on sources that allow them (`state.SOURCE_TRAITS`; WDA does). `LAUNCH_APP` only to `allowed_bundles`. The target must exist in the snapshot and advertise the operation. Typing needs an editable field. |
| Ineffective or rejected replays | `was_ineffective`, `was_rejected` | An action that changed nothing, or that the verifier rejected, is not repeated on the same screen identity. |
| Duplicate text | `RunState.typed_fields` | The same text is never typed into the same field twice. The first refusal re-decides; the second ends the run (`duplicate_text_blocked`). |
| Effect ledger | `effect_ledger.EffectLedger` | Intent is recorded before dispatch. A second dispatch of the same logical effect is refused while the earlier outcome is acknowledged, observed or unknown. Only a pre-dispatch refusal (`failed_pre_dispatch`) releases it. |
| Action verifier | `Jev.verify_action` → `ActionSupport` | TAP, TYPE, TYPE_SUBMIT and SUBMIT are checked against the original request. MISMATCH re-decides with a hint. UNCLEAR ends the run (`needs_clarification`), with two narrow exceptions: a navigation-shaped tap (`is_navigation_shaped_tap`), and an act the request names (`requested_by`). Plain navigation taps and keypad keys skip the call. |
| Requested-action grounding | `task_policy.requested_by` | An act ("Archive", "Send", "Pay") counts as requested only when the request's own words contain it, excluding its prohibition sentences (`asked_part`). Look-only requests never qualify. |
| Ask before acting | `task_policy.needs_approval`, `Agent._ask_approval` | In the API server, risky TAP, SUBMIT and TYPE_SUBMIT pause for the user. This is on unless `MOBSTER_ASK_BEFORE_ACTING=0` (`Runtime.ask_before_acting`). Waiting time is added back to the deadline. A TAP/SUBMIT the side-effect floor gated to WAIT at navigation confidence or more (`Decision.approvable_target`, `APPROVAL_CONFIDENCE_FLOOR`) and an UNCLEAR action check are put to the user rather than dropped. |
| Bypass | `Agent(bypass=True)`, `Runtime.bypass_checks` | Opt-in (`MOBSTER_BYPASS_CHECKS=1`): that same gated commit is taken and an UNCLEAR check proceeds. MISMATCH, the duplicate and replay guards, and approvals still apply. |
| Fresh screen | `Agent._refresh_unchanged`, `WDA._check_current` | The action and its guards share one observation. A changed screen discards the decision (`stale_decision`), unless the same element is still at the same frame. Reads older than `WDA_FRESH_SECONDS` are re-read before a coordinate tap. A still MJPEG stream (`_pixels_still_since`) can stand in for the re-read. |
| Completion | `Agent._handle_done` | DONE for an action-only task needs `goal_probability ≥ COMPLETION_GOAL_FLOOR` (.5), unless the route's last named screen is showing. It is then re-checked on a fresh read, or skipped when the screen is provably unchanged (`Jev.stable_completion`). The status is `completed_unverified`. |
| Budgets and spend | `RunState.budget`, `costs.SpendLedger` | Every model or device call first checks for a stop, the run deadline and the spend cap. `--spend-cap-usd` stops the run with status `spend_cap` once priced spend reaches the cap. Unpriced calls are counted, never zero-filled. |
| Undispatched refusals | `max_undispatched` | Driver refusals before dispatch (`DriverRejection.PRE_DISPATCH`, for example `stale_revision`) are bounded, so a screen that never settles cannot spin until the deadline. |

Ambiguous failures are never retried. A device or model timeout mid-step costs one
re-observation (`_retry_interrupted_step`, at most `STEP_INTERRUPTION_RETRIES`), and its effect
is recorded as `unknown`. The run's own deadline is never retried.

## Extraction and verification

An answer is released only when it is a cited literal of observed evidence and a verifier
accepts it.

- **Evidence** (`extraction.Evidence`). Every observation is added with its step: on-screen
  elements, and off-screen rows ranked by the request's words (`focus_terms`). Visual text is
  added when the operator enabled it. Bounded views go to decisions (`decision_context`) and to
  extraction (`pack_extraction_context`).
- **Selection or generation** (`Agent._extract_answer`). A flat schema of 1 to 8 string fields
  (`selectable_schema`) is answered first by `Jev.select_fields`, which chooses observed
  literals. The helper (`extract`, or `extract_auto` for automatic output) generates the answer
  when that pick is unsure, and gives one second opinion when the pick is rejected. When the
  helper abstains with `InsufficientEvidence`, the screen may be read visually once and the
  extraction retried.
- **Literal validation** (`extraction.validate_extraction`). The data must validate against the
  schema. Every non-null scalar needs a citation whose quote is in the cited evidence entry
  and contains the value verbatim; numbers must match on token boundaries. Closed leaves
  (enums, booleans, `closed_answers`) are judgments about the cited quote.
  `check_field_claims` rejects plausible mis-bindings locally. `repair_citations` fixes only
  two known citation slips and never changes a value.
- **Answer verification** (`Jev.verify_output_signals`). One call returns an overall verdict
  and three per-claim probabilities (`ANSWER_CLAIMS`): `claim_final_state`, `claim_entity`
  and `claim_field`. The recent actions are included as evidence. An answer is accepted in
  one of three ways:
  - the verdict is SUPPORTED;
  - `claims_calibrated`: `CALIBRATED_CLAIM_FLOORS`, field ≥ .6 and entity ≥ .7, fitted on 80
    replayed answers on 24 Sep;
  - `agreement_accepts`: two independent extractors chose the same literal and every
    per-claim check is high.

  Each acceptance is logged with a digest (`answer_digest`) so that
  `evals/verifier_calibration.py` can re-audit it.
- **Answers computed in code.**
  - Counts (`Agent._counted_answer`): two differently worded listings must cite the same rows.
  - Surveys (`loops.survey_answer`).
  - Dataflow plans.
  - Visual answers: a judgment with its image hash and tier in `visual_evidence`, never a
    citation.
- **Result** (`Agent._finish`). The result carries `status`, `data`, `citations`,
  `data_status`, `schema_validated`, `evidence` and `independently_verified: False`. A
  completed run whose answer was not extracted becomes `completion_not_confirmed`, and a
  missing helper yields `data: null` with `helper_unavailable`. The schema subset and output
  formats are in the [framework README](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/README.md#results-and-optional-schemas).

## How a run flows end to end

1. **Admission** (`Runtime.create`). The request is validated and deduplicated by
   `Idempotency-Key`. The phone is leased (`journal.Lease.device` or `DevicePool.acquire`). The
   run is written to the private SQLite journal (`journal.Journal`, WAL, intent durable
   before any mutation).
2. **Preparation** (`Runtime.work`). The models are built and warmed (`http_pool` keeps TLS
   connections across runs). The WDA driver is built, and a FrameClock is attached to the
   video relay. `prepare_wda_phone` refuses a locked phone and clears overlays. The app is
   activated.
3. **Compile.** `Agent.run` computes the replay key and compiles the route. It then tries a
   plan, a loop or milestones, and predicts the first decision.
4. **Step loop** (up to `max_steps`):
   - observe a settled screen and add it to the evidence;
   - take the first decision that is available: route, survey, probe, visual answer, replay,
     memo, speculation or Jev;
   - apply the stop gate;
   - for an action: check validity, the ledger, the verifier, the approval and freshness, then
     `driver.execute`;
   - settle, speculating the next decision meanwhile, and record the observed outcome;
   - on WAIT, back off. Three WAITs on an unchanged screen trigger one recovery hint;
     `WAIT_PLATEAU_LIMIT` (6) on a screen that is not loading ends the run as `no_progress`;
   - on BLOCKED, ask for a recovery hint while budget remains, else end the run as `blocked`.
5. **Completion** (`Agent._handle_done`). The completion gate runs, then the fresh re-check.
   The next milestone starts, or `_finish` extracts and verifies the answer. An answer that
   is missing, or that probably lives elsewhere on the route, can resume the loop once
   (`ContinueTask`).
6. **Finish.** `_finish` emits the result, saves the replay and memo entries (only for runs
   that succeeded), and emits the latency trace (`latency_trace.Trace`). The server captures a
   final preview, closes its clients and releases the lease. Interrupted runs are marked
   `interrupted` on restart and never resumed automatically.

The dashboard sees each step as server-sent events, for example `observation`, `decision`,
`action_check`, `action_started`, `action_acknowledged`, `observation_after_action`,
`answer_signals` and `result`. Observation events carry screen text. Error events never carry
request payloads, generated text, credentials or provider response bodies. `answer_signals`
carries numbers and a digest, never the answer.

## Speed mechanisms

Speed is the priority, but never at the cost of precision. Each mechanism below keeps the
answer identical and only changes when it arrives.

| mechanism | module | rule that keeps it exact |
| --- | --- | --- |
| Speculative next decision | `Agent._speculate`, `Jev.side_channel` | Used only when the live request hashes identically. At most `MAX_SPECULATIONS_PER_ACTION` (2) per action. |
| Predicted transitions | `remember_transition`, `predicted_screen` | Decides the known successor screen early, under the same hash rule. |
| Early answer prefetch | `Agent._prefetch_answer`, `early_answer` | Starts at DONE and is used only if the run ends on that exact screen (`answer_identity`). |
| Same-screen completion | `Jev.stable_completion` | Skips the re-ask only when a fresh read is identical and no helper hint was involved. |
| Hedged requests | `hedge.Hedger` (`MOBSTER_HEDGE`, `MOBSTER_HEDGE_RATIO`) | Sends an identical twin request after the tail delay and keeps the first answer. Token-bucket budgeted. |
| Warm connections | `http_pool.ConnectionPool` | Reuses TLS connections across runs in one process. |
| Decision memo and replay | `decision_memo`, `replay` | Content-addressed. Entries are written only by runs that passed every gate. |
| FrameClock and glide | `frame_clock`, `WDA._glide` | Pixels shorten the quiet period; AX is still read. |

`python -m mobile_agent.benchmarks` reads run exports and reports where time went, without
touching a device.

## Configuration

Keys and switches are read from the environment, or from a private env file passed with
`--env-file` (0600; `keys.py` manages it from the desktop app). Never commit these values.

| variable | used by | meaning |
| --- | --- | --- |
| `TYPESAFE_API_KEY`, `TYPESAFE_MODEL` | `models.Jev` | Jev credentials; the model defaults to `jev-latest` |
| `TEXT_MODEL`, `TEXT_MODEL_API_KEY`, `TEXT_MODEL_BASE_URL`, `TEXT_MODEL_REASONING_EFFORT` | `models.Helper` | OpenAI-compatible helper endpoint |
| `TEXT_MODEL_PROVIDER=vertex`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | `models.Helper` via `gemini.VertexHTTP`; `frontier.GeminiChat` reads the project and location | Vertex through the operator's own `gcloud` login; set the project to your own `<your-project-id>` (location defaults to `global`) |
| `OPENAI_API_KEY` | `frontier.OpenAIChat` | frontier policy on OpenAI models |
| `MOBSTER_ENABLE_LIVE`, `MOBSTER_ASK_BEFORE_ACTING`, `MOBSTER_BYPASS_CHECKS` | `__main__` (`serve`), `setup_service`, `server` | saved live-run gate for `serve`; approvals (on unless `0`); bypass (off unless `1`) |
| `MOBSTER_WDA_DEVICES` | `pool.wda_specs_from_env` | phone list syntax for `evals/harness.py --devices`; the API server does not read it |
| `MOBSTER_FRAME_CLOCK`, `MOBSTER_FRAME_CLOCK_LOG`, `MOBSTER_FRAME_CLOCK_TRACE` | `frame_clock`, `drivers` | pixel settle mode and its diagnostics |
| `MOBSTER_GLIDE`, `MOBSTER_GLIDE_BUNDLES` | `drivers.WDA._glide_ok` | exact-distance scrolling |
| `MOBSTER_ROUTES`, `MOBSTER_PLANS`, `MOBSTER_MILESTONES`, `MOBSTER_PAGE_PROBE`, `MOBSTER_DIRECT_URL`, `MOBSTER_DECISION_MEMO`, `MOBSTER_HEDGE` | `agent`, `decision_memo`, `hedge` | `0` turns the mechanism off (all default on) |
| `MOBSTER_TEXT_SELECTION` | `models.request_text_candidates` | `1` turns on Jev text selection (default off) |
| `MOBSTER_VISION_T0`, `MOBSTER_DEBUG_CROPS` | `vision_judge`, `loops` | local Apple Vision helper path; crop dumps for diagnosis |
| `MOBSTER_VIDEO_FPS`, `MOBSTER_VIDEO_DETAIL` | `server` | live-view stream settings |

Persistent state sits beside the journal (`--state-db`; default `mobile_agent/.state/` in a
source checkout, else `~/Library/Application Support/app.mobster.desktop/state/`): the
`replays/`, `decision-memo/` and `loops/` stores and `wda-overlay-hints.json`. Journals
contain goals and observed app text. They are private and must not be published.

## Measured status and known gaps

Every number below is from a dated internal run report; none is a new measurement.

- **MobsterBench-iOS, development result** (68 tasks, pre-registered; 2026-09-24, USB iPhone 15
  Pro over WDA, 1 repeat, `mobster-next`). This is the last of six development passes on the
  same tasks, with fixes made between passes, so it is **not a held-out score** (see
  [benchmarks.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/benchmarks.md#what-the-pass-runs-are)):
  - 54/66 (82%) graded tasks passed, Wilson 95% CI 71–89%;
  - by category: navigation 10/10, scroll 7/7, web 9/10, retrieval 9/10, text entry 6/8,
    visual 6/8, settings state 5/7, **multi-app 2/6**;
  - task time p50 7.7 s, p90 17.1 s; about $0.006 per task;
  - 4/4 abstentions correct;
  - one risky tap was blocked, and 2 unintended actions were recorded.

  No baseline ran in that pass, so it makes no state-of-the-art claim.
- **Unfamiliar apps.** The `frontier.py` module docstring records that on the iOSWorld smoke
  (24 Sep, 11 tasks) the Jev step agent passed none, mostly by waiting. The published best is
  51.9%. That gap is why `FrontierAgent` exists. The frontier policy's 11-task smoke runs are
  summarized in [benchmarks.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/benchmarks.md#iosworld); there is no full-suite result yet.
  Raw runs stay local (they hold screenshots and screen text).
- **Earlier suites.** The 22 Sep USB suites: Settings 35/35 (7 tasks, 5 repeats), and the
  14-task Settings and Safari suite 39/42 and 38/42 across two runs.
- **Known limits.**
  - Drawn content (canvas, text in images, Calculator's display) is invisible to the tree
    unless visual reading is enabled.
  - Completion is model agreement, not an oracle; `independently_verified` is always false.
  - The navigation and side-effect confidence floors are uncalibrated.
  - Multi-app and long-horizon tasks in unfamiliar apps are the weakest area.

## Related documents

- [compiled-intent.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/compiled-intent.md): routes, probes, direct opens, plans, counts,
  surveys and visual answers in detail.
- [adaptive-architecture.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/adaptive-architecture.md): the research-backed adaptation plan
  and the hybrid control plane.
- [usb-wda.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/usb-wda.md) and [setup-api.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/setup-api.md): device setup and the local API.
- [running.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/running.md) and [benchmarks.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/benchmarks.md): commands, configuration and
  both benchmarks.
- [../bench/README.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/bench/README.md): MobsterBench-iOS and its decision rule.
- [README.md](https://github.com/uninstantiated/mobster-cli/blob/main/mobile_agent/docs/README.md): the index of all docs.
