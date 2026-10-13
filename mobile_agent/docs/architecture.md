# Architecture of the `mobile_agent` framework

Written 2026-09-24 and rewritten 2026-10-13 against the code in this tree. Every module, function,
constant and environment variable named here exists under `mobile_agent/`. Measured numbers are
quoted from the dated reports they come from; nothing here is a new measurement.

Mobster drives an iPhone from its accessibility (AX) tree. One engine runs every task: Mobster's
agent, `frontier.FrontierAgent`. A frontier model (Claude Sonnet 5.5 on a Claude key, gpt-5.6-sol on
an OpenAI key alone) plans every step over Mobster's perception and control layer. It sees the
screen as short element ids and a small screenshot, and it never supplies coordinates for a listed
element. Code around the model compiles the request into a task contract, guards every action,
asks before commits, and accepts DONE only when the contract's items have their receipts.

```
request ──► task contract (contract.compile_contract, while the first screen is read)
              │
              ▼
        ┌─ observe (drivers.WDA → state.Snapshot), settle on AX and pixels ◄─┐
        │                                                                     │
        ▼                                                                     │
   decide: a saved program's step, or one model call → 1-6 chained actions    │
        │                                                                     │
        ▼                                                                     │
   guards: allowed apps → requested act (task_policy.requested_by) →          │
           declared commit (Contract.check) → repeat and loop refusals →      │
           approval (permissions gate) → fresh screen                         │
        │                                                                     │
        ▼                                                                     │
   act (driver) → settle → record outcome, receipts and the trace ───────────┘
        │  (on DONE)
        ▼
   DONE gate (Contract.done_gate) → result (engines.summarize)
```

## Contents

- [Entry points and composition](#entry-points-and-composition)
- [Perception](#perception)
- [Mobster's agent](#mobsters-agent)
- [Guards](#guards)
- [The task contract and the DONE gate](#the-task-contract-and-the-done-gate)
- [How a run flows end to end](#how-a-run-flows-end-to-end)
- [Speed mechanisms](#speed-mechanisms)
- [Configuration](#configuration)
- [Measured status and known gaps](#measured-status-and-known-gaps)

## Entry points and composition

Every task ends in `frontier.FrontierAgent.run`, built by `engines.build_frontier`.

- **CLI** (`__main__.py`). `run` runs one task through the same `server.Runtime` the terminal UI
  uses (`smart_run.py`); `--demo` runs it over the scripted phone of `tui/demo_phone.py`. `serve`
  starts the loopback API.

  ```sh
  python3 -m mobile_agent run '<goal>' --execute --wda-url http://127.0.0.1:8100 --env-file mobile_agent/.env
  python3 -m mobile_agent serve --manage-device --env-file mobile_agent/.env
  ```

- **Terminal UI** (`tui/`). `mobster` with no command runs `server.Runtime` in process
  (`tui/session.py`) and draws its runs with Textual (`tui/app.py`). It adds nothing to a
  run: tasks, events, approvals, Stop and settings are the Runtime's own. `narrate.Narrator`
  turns the agent's `frontier_decision` and `frontier_action` events into readable steps.
- **API** (`server.py`). `Runtime.create` admits a task. It validates the request, resolves the
  `Idempotency-Key` against the journal, takes a device lease or a `pool.DevicePool` slot, and
  writes the run to the journal. `Runtime.work` then calls `Runtime.run_smart`, which takes the
  model client (`Runtime.smart_client`), builds the driver and checks the phone
  (`Runtime.open_driver`, `prepare_wda_phone`), builds the agent with `engines.build_frontier`
  and runs it. The endpoints are in [setup-api.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/setup-api.md) and the
  [framework README](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/README.md).
- **Benchmarks.** `bench/` (MobsterBench-iOS) and `bench/iosworld.py` (the public iOSWorld
  suite) build the same agent from `engines.SMART_CONFIG`. See
  [bench/README.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/bench/README.md).

`engines.py` is the one place the engine is configured. `SmartConfig` (frozen) holds the model,
reasoning, `max_steps` (50), `max_seconds` (900), screenshots and `settle_seconds`;
`engines.smart_model` reads `MOBSTER_SMART_MODEL` and `MOBSTER_SMART_PROVIDER`, the one thing a user
may change. `engines.SmartEvents` turns the loop's trace into the contract events the dashboard
renders ([engine-contract.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/engine-contract.md)), and `engines.meter` puts every model call in the
usage ledger.

`compose.py` holds what `serve` and `run` share: `build_target_driver` selects the driver and
`close_all` shuts everything down without raising. Framework errors share the
`errors.MobsterError` root, and no subclass is retried by default. `Cancelled` (operator stop) is control flow: it is deliberately not a `MobsterError` and is
never retried.

## Perception

### Drivers

`drivers.Driver` is the device contract: `observe`, `execute`, `close`. `build_driver` looks up
the `DRIVERS` registry:

| driver | transport | status |
| --- | --- | --- |
| `WDA` | WebDriverAgent over USB (`iproxy`, API port 8100, MJPEG port 9100) | the supported path; Mobster for Mac sets it up ([Device setup](https://docs.mobster.dev/device-setup)) |

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
  `WDA._await_keyboard` accepts either.
- **Off-screen evidence** (`state.OffscreenNode`, `Snapshot.offscreen_evidence`). Labelled
  nodes outside the viewport are kept as read-only evidence. WebKit exposes the whole page, so
  a Safari article can be read without scrolling. These nodes are never tap targets.
- **System-overlay recovery.** On iOS 26.0.1, WDA sometimes resolves the active app to an
  invisible system overlay, and source reads fail after about 8 s. A failed read is retried
  once with WDA's default active application set to the app Mobster opened. The hint is
  remembered per WDA URL (`WDA.load_overlay_hints`, `wda-overlay-hints.json` beside the journal).

### Screen identity

`Snapshot.fingerprint` hashes everything a decision can see, including the revision and the
locators. `content_fingerprint` leaves out the revision. The driver uses the strict form to refuse
a stale coordinate tap (`WDA._check_current`, `stale_revision`), and the agent uses the content
form to tell whether an action changed anything and whether the screen moved while the model
thought. `same_screen` lets a visual fingerprint refine AX identity, but never replace it.

### Settling

A read taken mid-animation is not a decision input.

- `WDA.observe_ready` reads until the screen has not changed for `WDA_SETTLE_QUIET_SECONDS`
  (0.5 s), or until one read matches a screen already proven at rest (`WDA_SETTLED_MEMORY`).
  Screens still moving at `WDA_SETTLE_CAP_SECONDS` (1.5 s) return their latest read.
- `WDA.wait_for_change` does the same after an action.
- `frame_clock.FrameClock` treats WDA's ~30 fps MJPEG stream as a clock. `Runtime.open_driver`
  attaches one (`frame_clock.attach_frame_clock`) whenever the phone's video relay is up, and the
  pixel settle is on while it is attached. With pixels still, the AX quiet period drops to
  `WDA_FRAME_AX_QUIET_SECONDS` (0.3 s). AX is never skipped. In the 2026-09-23 validation (24
  navigations on one iPhone 15 Pro) mean navigation settle fell from 1,122 ms to 996 ms, with 0
  premature settles. `MOBSTER_FRAME_CLOCK_LOG` and `MOBSTER_FRAME_CLOCK_TRACE` log its decisions.
- Scrolls use a "glide", a W3C drag with a decelerating end that moves an exact distance with
  no fling (`WDA_GLIDE_SEGMENTS`, `WDA._glide_ok`). `MOBSTER_GLIDE=0` turns it off.
  `drivers.clear_stroke_start` moves a swipe's start off sliders and switches.

## Mobster's agent

`frontier.FrontierAgent` (module docstring in `frontier.py`) runs the loop. Its model client comes
from `frontier.chat_client`: `AnthropicChat` for `claude-*`, `OpenAIChat` for OpenAI models,
`GeminiChat` for `gemini-*`, or a provider saved in Settings › Models.

- **Input.** Each call sees the request, the allowed apps, a cached log of what each earlier
  turn did (`_logged_prompt`: its actions and effects, notes, contract marks and plan changes),
  its plan and the screen as short aliases (`screen_rows`: `e1`, `e2`…, with frames as
  percentages). A long task sees its notebook and the recent action lines (`HISTORY_LINES`)
  instead. A 448-pixel screenshot is added (`screenshot_part`, `SCREENSHOT_SIDE`); it comes from
  the MJPEG stream when the screen is still (`video_frame`). Screen text is data in the prompt,
  never instructions.
- **Memory.** The model keeps a plan and notes across steps and apps, so a fact found in one app
  is there when another needs it.
- **Action chunks.** One call returns 1 to `MAX_CHUNK` (6) actions from `frontier.OPERATIONS`.
  Later actions name their target by exact label. A chunk runs until a target is missing or an
  action changes nothing, and then the model sees the new screen.
- **Extra operations.** `SET_TEXT` clears a field (`WDA.clear_text`) and then types.
  `DISMISS` taps the first point in `DISMISS_POINTS` that no element covers. `READ_LIST` and
  `SCROLL_TO` are macros: code loops over the screen with no model call per swipe. `TAP_XY`
  taps a control the screenshot shows and no listed element is, and asks like any tap.
- **Skills** (`agent_hooks.Skill`, `mobile_agent/skills/`). Operations code performs for the model:
  `USE_CODE` (a one-time code), `READ_NOTIFICATIONS` and `OPEN_URL`. A skill's secrets are masked
  in every later row, note, history line and answer, and blurred in screenshots.
- **Saved programs** (`learned/programs.py`, `MOBSTER_PROGRAMS`). A request with the same words as
  one that ended done, with other values in its blanks, repeats that run
  (`FrontierAgent(saved_program=...)`) through the same per-action path: guards, the contract's
  commit check, approvals and the journal. Parts of saved programs (`program_parts`) may run at a
  planning point. The record and replay engine is `replay_core/`.
- **Cost cap.** `frontier.cost_usd` prices usage from the `PRICES` list-price table, and the run
  stops at `max_cost_usd` (status `spend_cap`). `bench/iosworld.py` scales each task's cap with
  the apps it spans, at most `MAX_TASK_COST_USD` ($0.60; `task_cost_cap`).

## Guards

Each guard fails closed. A refused action is never sent; the refusal goes back to the model as
feedback and appears in the trace (`frontier_refused`).

| guard | where | what it enforces |
| --- | --- | --- |
| Allowed apps | `FrontierAgent._check_app`, `_blocked_app` | Only listed apps open. A blocked app is refused before launch. |
| Requested act | `frontier.guard`, `task_policy.requested_by` | A tap on a control whose first `GUARD_WORDS` (4) words name a commit (`task_policy.COMMIT_CONTROL`: send, pay, buy, delete, post, share, follow, call…) is refused unless the request asks for that act ("Delete" when the user asked to archive). Prohibition sentences don't count (`asked_part`). Opening a share sheet (`SHEET_OPENERS`) commits nothing. |
| Declared commit | `contract.Contract.check` | A commit passes only when it belongs to an open COMMIT item of the current app, within the item's amount bounds, with the typed text a message needs and the object a follow names on screen. A closed commit refuses repeats. |
| Repeats and loops | `REPEAT_ACT_FEEDBACK`, `LOOP_WINDOW`, `LOOP_REPEATS` | Typed text repeated on the screen its typing left is refused at once. Any other action repeated on the screen it was made on (it did nothing) is refused the second time. A third identical action on the same screen within the last `LOOP_WINDOW` (12) is refused as a loop. Swipes, `DISMISS` and keypad keys are exempt, and working down a list (another "Pay" on a changed screen) is not a repeat. |
| Approvals | `FrontierAgent._approval`, `permissions.PermissionGate`, `contract.asks` | The run's permission mode (`ask_all`, `ask_important`, `dont_ask`) and the person's "Don't ask again" rules decide what asks. A declared commit asks in `ask_important`. A one-time code the agent types asks in every mode (`floor_approve`). No answer is no action. iOS's own privacy prompts ask in every mode (`system_dialogs.py`). |
| Phone guard | `agent_hooks.PhoneGuard`, `lockscreen.guard_for` | Asked at preflight, when a launch fails, when a lock is suspected and after a long wait for the person. A locked or unplugged phone stops the run (`GuardStop`) with a code (`phone_locked`, `unplugged`…). |
| Fresh screen | `frontier._Verify`, `WDA._check_current` | One source read runs on a worker thread while the model thinks. When it differs from the screen the prompt saw, the chunk acts on the newer read and re-finds its targets by label. Reads older than `WDA_FRESH_SECONDS` are re-read before a coordinate tap. |
| Secrets | `FrontierAgent._mask`, `secret_filter.redact` | A typed code or a skill's secret never reaches the trace, the journal or the answer. |
| Budgets and spend | `SmartConfig`, `costs.SpendLedger` | `max_steps`, `max_seconds` and the spend cap end the run as `max_steps`, `timeout` or `spend_cap`. Unpriced calls are counted, never zero-filled. |

Ambiguous failures are never retried. An action whose outcome is unknown is recorded as such and
not sent again.

## The task contract and the DONE gate

`contract.py` (its module docstring has the details) is proof-gated completion.

- **Compile.** One text-only call (`contract.compile_contract`), run while the first screen is
  read, turns the request into typed items: READ a source, WRITE a value, COMMIT an act other
  people see or that spends, REPORT a detail, FORBID an act the request rules out. A COMMIT whose
  quote is not verbatim in the request is dropped.
- **Receipts.** `Contract.observe` records each screen. A sent message shows in a non-editable
  row and has left the composer; a booking, order, payment or follow shows a new confirmation
  row; a write reads back in its app on a screen recorded after this run first wrote there; a
  reported value was on a screen at or before the turn it was recorded.
- **DONE gate** (`Contract.done_gate`). DONE is accepted when every item has its receipt, or with
  at most `LOW_TURNS` (3) turns left, when the answer names what is unproven. A missing receipt
  reopens its item with a one-line reason, at most `MAX_REOPENS` times an item.
- **Result** (`engines.summarize`). The outcome is `done` when every COMMIT, WRITE and REPORT item
  is proven, `check` when one is unproven, else `couldnt_finish`, `stopped` or `declined`
  (`engines.outcome_of`). `independently_verified` is true only for `done`. A requested output
  format or JSON schema is filled from the answer (`engines.structure`, `structure_auto`).

## How a run flows end to end

1. **Admission** (`Runtime.create`). The request is validated and deduplicated by
   `Idempotency-Key`. The phone is leased (`journal.Lease.device` or `DevicePool.acquire`). The
   run is written to the private SQLite journal (`journal.Journal`, WAL, intent durable
   before any mutation).
2. **Preparation** (`Runtime.run_smart`, `Runtime.open_driver`). The model client is taken and
   metered. The WDA driver is built, and a FrameClock is attached to the video relay.
   `prepare_wda_phone` asks the phone guard, refuses a locked phone and clears overlays. The app
   is brought forward. Pre-run hooks (`harness_api.pre_runs`) may answer from an earlier report
   or hand the loop a saved program.
3. **Contract.** The agent compiles the task contract while it reads the first screen.
4. **Step loop** (up to `max_steps` turns):
   - observe a settled screen and record it in the contract;
   - take a saved program's step, or call the model for a chunk; the decision
     (`frontier_decision`) is emitted, and so journaled, before its actions run;
   - for each action: the guards, the contract's commit check, the approval and the fresh-screen
     check, then the driver;
   - settle and record the outcome (`frontier_action`), the receipts and the trace.
5. **Completion.** On DONE the gate runs; a missing receipt reopens its item and the loop goes on.
6. **Finish.** `engines.summarize` builds the result, and the latency trace is emitted
   (`latency_trace.Trace`). The server captures a final preview, closes its clients and releases
   the lease. Interrupted runs are marked `interrupted` on restart and never resumed
   automatically.

Every event reaches the journal through `Run._append` in `server.py`, and the dashboard sees each
as a server-sent event. Screens are stored beside the run and referenced by `frameId`, never
inlined. Error events never carry request payloads, generated text, credentials or provider
response bodies.

## Speed mechanisms

Speed is the priority, but never at the cost of precision. Each mechanism below keeps the
answer identical and only changes when it arrives.

| mechanism | module | rule that keeps it exact |
| --- | --- | --- |
| Action chunks | `MAX_CHUNK` | A chunk stops at the first missing target or the first action that changes nothing. |
| Verify read while the model thinks | `frontier._Verify` | The newer read wins; targets are re-found by label. |
| Macros | `READ_LIST`, `SCROLL_TO` | Code swipes and reads; the model sees the rows. |
| Saved programs | `learned/programs.py`, `replay_core/` | Each step's screen is checked; the contract and approvals apply to every step. |
| Hedged requests | `hedge.Hedger` (`MOBSTER_HEDGE`, `MOBSTER_HEDGE_RATIO`) | Sends an identical twin request after the tail delay and keeps the first answer. Token-bucket budgeted. |
| Warm connections | `http_pool.ConnectionPool` | Reuses TLS connections across runs in one process. |
| FrameClock and glide | `frame_clock`, `WDA._glide_ok` | Pixels shorten the quiet period; AX is still read. |

## Configuration

Keys and switches are read from the environment, or from a private env file passed with
`--env-file` (0600; `keys.py` manages it from the desktop app). Never commit these values.
[running.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/running.md) has the full list.

| variable | used by | meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | `engines.smart_key`, `frontier.AnthropicChat`, `frontier.OpenAIChat` | the key Mobster's agent runs on |
| `MOBSTER_SMART_MODEL`, `MOBSTER_SMART_PROVIDER` | `engines.smart_model` | the model, and which provider goes first when several are saved |
| `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | `frontier.GeminiChat` | Gemini on Vertex through your own `gcloud` login; set the project to `<your-project-id>` (location defaults to `global`) |
| `MOBSTER_ENABLE_LIVE`, `MOBSTER_PERMISSION_MODE`, `MOBSTER_ASK_BEFORE_ACTING` | `__main__` (`serve`), `setup_service`, `permissions.policy` | saved live-run gate for `serve`; the permission mode (`MOBSTER_ASK_BEFORE_ACTING=0` is the older name for `dont_ask`) |
| `MOBSTER_PROGRAMS` | `learned/programs.py` | saved programs: on for runs from the Mac app and the API, `on` or `off` for every origin |
| `MOBSTER_FRAME_CLOCK_LOG`, `MOBSTER_FRAME_CLOCK_TRACE` | `frame_clock`, `drivers` | pixel settle diagnostics |
| `MOBSTER_GLIDE` | `drivers.WDA._glide_ok` | `0` turns exact-distance scrolling off |
| `MOBSTER_HEDGE`, `MOBSTER_HEDGE_RATIO` | `hedge` | `0` turns hedged requests off; the ratio sets their budget |
| `MOBSTER_VIDEO_FPS`, `MOBSTER_VIDEO_DETAIL` | `server` | live-view stream settings |

Persistent state sits beside the journal (`--state-db`; default `mobile_agent/.state/` in a
source checkout, else `~/Library/Application Support/app.mobster.desktop/state/`), with
`wda-overlay-hints.json` and the run's screens. Journals contain goals and observed app text. They
are private and must not be published.

## Measured status and known gaps

Every number below is from a dated internal run report; none is a new measurement.

- **iOSWorld.** `engines.SmartConfig` records dev_core at 53.8% official pass on Claude Sonnet 5.5
  (the A arms of aa1, ab1, ac1 and ac2, 10-11 Oct 2026). The published best when the frontier
  policy was built was 51.9%. Results and their conditions are in
  [benchmarks.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/benchmarks.md#iosworld).
- **MobsterBench Real.** 38 of 46 graded tasks on the owner's iPhone (12 Oct 2026), the same
  record. The pre-registration is [realbench.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/realbench.md).
- **Earlier suites.** The 22 Sep USB suites: Settings 35/35 (7 tasks, 5 repeats), and the
  14-task Settings and Safari suite 39/42 and 38/42 across two runs. They ran on an earlier
  engine.
- **Known limits.**
  - Drawn content (canvas, text in images, Calculator's display) is invisible to the tree; the
    screenshot is the agent's only view of it.
  - `independently_verified` means the contract's receipts proved every item, not that an
    outside oracle checked the result.
  - Multi-app and long-horizon tasks in unfamiliar apps are the weakest area.

## Related documents

- [engine-contract.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/engine-contract.md): the events and result the engine emits.
- [adaptive-architecture.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/adaptive-architecture.md): the research-backed adaptation plan.
- [Device setup](https://docs.mobster.dev/device-setup) and [setup-api.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/setup-api.md): device setup and the local API.
- [running.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/running.md) and [benchmarks.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/benchmarks.md): commands, configuration and
  both benchmarks.
- [../bench/README.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/bench/README.md): MobsterBench-iOS and its decision rule.
- [README.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/README.md): the index of all docs.
