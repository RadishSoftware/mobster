# Long tasks (track longform)

Dated design note, 8 Oct 2026. This page is what the code does and
the API the Mac app and scripts use. The track's package is `mobile_agent/longform/`. Core owns budgets, the plan,
the notebook, the report and the routes; the feed track owns `WATCH_FEED` and `READ_EACH` (`feed.py`, `items.py`,
`extract.py`). Nothing in the package depends on which app a task runs in: a list of matches in a dating app is read
and ranked the way a list of results in Yelp is, under the rules every run follows.

A long task is a Smart run with a `LongSession`. It covers a budget ("for 10 minutes", "30 reels", "every match") in
as little phone time as it can, keeps what it sees in a notebook, closes its driver and releases its device lease
before `phone_released`, and then writes a report from the notebook. Another queued task may take the phone during
report writing. The first task still reserves its remaining model spend until its report finishes. Every other run
is unchanged: without a session the frontier sends byte for byte
the prompts it sent before (`test_longform.ByteIdentityTests` pins three recorded turns).

## When a task is long

`plan.detect(goal, apps)` decides, by code, with no model call:

| kind | when | example |
| --- | --- | --- |
| `feed` | a feed noun or app (Reels, TikTok, Shorts, Instagram) with a watch verb or a budget | "Scroll Instagram Reels about basketball for 10 minutes and tell me what you learned" |
| `survey` | a reading verb over a collection (matches, DMs, chats, emails, listings, results) | "Go through my Hinge matches and find the most promising ones" |
| `chain` | two or more parts split on then, and then, after that, `;` or a numbered list, naming different verbs | "Find the best ramen near me across Maps and Yelp, then book Friday at 8" |
| `browse` | a time or item budget with a browsing verb and nothing else | "Browse Zillow for 20 minutes" |

It is conservative: "Text Sam I'm 10 minutes late", "Set a timer for 10 minutes" and "Delete every message from Kate
Bell" are not long. `--long` on the CLI and `longform: {}` on the API force a long task. `MOBSTER_LONGFORM=off` turns
long tasks off: every task then runs as it did before them.

## Budgets

`budget.parse_budget(goal)` reads a time (`for 10 minutes`, `half an hour`, `the whole 10 minutes`), a count (`30
reels`) or everything (`every match`, `all my DMs`: up to 40, raised to 100 on the card). A time becomes a
human-equivalent item target: a person sees a Reel in about 12 s, so "10 minutes of Reels" is 50 Reels, which Mobster
covers in about 2 minutes of phone time. "Use the full 10 minutes" (`alternative: "full"`) drops the target and runs
the whole time. Kept and off-topic items count toward the target; ads and repeats don't.

The run ends at whichever comes first: the item target (after at most 2 more turns to wrap up), the phone-time cap,
the spend cap with the report's reserve, the turn cap (`60 + 12 per step + 1 per 10 items`, at most 600) or the time
cap (phone time plus 300 s, at most 7,200 s). A picked-up run gets the turns it already used on top.

Spend is the run's whole spend: every metered call (navigation, extraction, digests, the report, hedges), read from
the run's ledger. The report's reserve (`costs.synth_estimate`) is kept back: wrap-up starts at 80% of the cap with
the reserve counted, so a run never ends at its cap with nothing to show.

## The plan card

`POST /api/longform/plan` with `{goal, appId?, threadId?, device?, force?}` answers at once (no model call):

- `{"longform": false}`: not a long task (or long tasks are off);
- `{"longform": false, "refused": {"code", "message"}}`: with `force`, a request that only changes things
  (`bulk_change`: "delete every email"), which runs as a normal task instead;
- `{"longform": true, "plan": PlanCard}`.

```json
{"id": "p_7c1e0a9b2d", "kind": "feed", "title": "Scroll Instagram Reels about basketball for 10 minutes and tell me what you learned",
 "focus": "basketball", "summary": "Watches about 50 basketball Reels, the number you'd scroll in 10 minutes, then writes up what it learned.",
 "apps": [{"bundleId": "com.burbn.instagram", "name": "Instagram"}],
 "steps": [{"id": 1, "title": "Open Instagram and find basketball Reels", "app": "Instagram"},
           {"id": 2, "title": "Watch about 50 Reels", "app": "Instagram"},
           {"id": 3, "title": "Write up what it learned", "app": null}],
 "budget": {"kind": "time", "minutes": 10, "items": 50, "strict": false, "phoneSecondsCap": 600, "itemNoun": "Reels",
            "label": "About 50 Reels", "clamped": false},
 "alternatives": [{"id": "full", "label": "Use the full 10 minutes", "items": null, "phoneSeconds": 600,
                   "lowUsd": 0.14, "highUsd": 0.43, "exceeds": false, "proposedUsd": null}],
 "estimate": {"phoneSeconds": 135, "reportSeconds": 11, "usd": 0.12, "lowUsd": 0.07, "highUsd": 0.22},
 "cap": {"limitUsd": 1.0, "exceeds": false, "proposedUsd": null},
 "policy": {"readOnly": true, "lines": []},
 "commits": [],
 "models": {"navigate": "claude-sonnet-5-5", "extract": "claude-haiku-5-5", "report": "claude-sonnet-5-5"},
 "expiresAt": 1791523200000}
```

A survey has the alternative `{"id": "more", "label": "Up to 100 matches"}`. **Commits.** Every part of a chain, and
the whole request of a feed, survey or browse, is checked for an act that asks (`contract_asks.ASK_ACTS`, "like" among
them) used as an instruction ("and like the best ones", "then book Friday at 8", "reply to each one"; not "I'd like
to know" or "send me a summary"). Each one found goes in `commits` (`[{"act": "book", "app": "Yelp"}]`), the card is
not `readOnly`, and it says what it asks before: "Asks before it books." in a chain, "Asks before each reply." in a
feed, survey or browse. A long run asks before each of them whatever Ask before acting says, so the lines always
show. Plans are kept in memory for 15 minutes (at most 64); an expired one is made again from its goal, with the
same result.

**The raise.** When the plan's high estimate passes the per-task limit, `cap.exceeds` is true and
`cap.proposedUsd` is `ceil_to_0.50(1.2 x highUsd)`; each alternative carries its own `exceeds` and `proposedUsd`.
`longform.capUsd` is honoured only for a run started in the Mac app, the terminal UI or the CLI, at least the
current limit, at most the proposed amount of the plan as started (or the CLI's `--spend-cap-usd`). Over HTTP it
needs the Mac app's session (`X-Mobster-App-Session`, as `askBeforeActing` does): without it `POST /api/runs`
answers 403 `app_only`, since `X-Mobster-Origin` grants nothing. The CLI and the terminal UI call `Runtime.create`
in-process. The operator's cap (`config.spend_cap_usd`) is never raised.

## Starting one

`POST /api/runs` takes the run field `longform: {planId?, alternative? ("full" | "more"), budget?: {items?,
minutes?, strict?}, capUsd?}`; `POST /api/threads/{id}/messages` passes `longform` through. An invalid field is a
400 with its sentence ("longform.capUsd can be at most $2.00, the amount this task's plan proposed"). A task the
server detects as long without the field (MCP, the plain API) starts at once with its plan, and no raise. Long tasks
run on Mobster's agent only: `longform` with a coding agent's `engine` is a 400 ("Long tasks run on Mobster's agent"). A forced long task (`longform` given, `mobster run
--long`, the plan route's `force`) whose request only changes things ("delete every email": a change verb and no
report intent, `plan.bulk_change`) is refused: "A long task reads and reports, and this one only changes things. Run
it as a normal task." (the plan route answers `refused.code: "bulk_change"`).

## Events

All journaled; new and optional (engine contract v1).

| event | fields | when |
| --- | --- | --- |
| `longform_plan` | `plan`, `revision` | at the start (revision 0) |
| `longform_progress` | `phase` (`navigating`, `watching`, `reading`, `writing`, `done`), `stepId`, `items: {seen, kept, ads, repeats, offTopic, watched, target}`, `phoneSeconds`, `phoneSecondsLeft`, `costUsd`, `label` | at most every 5 s while nothing changes, at once on a new phase and on every 10th item |
| `longform_item` | `itemId`, `n`, `kind`, `status` (`captured`, `extracted`, `skipped`), `skip`, `source: {creator, name, title}`, `summary`, `tags`, `relevant`, `frameId`, `step` | per item, then again when its extraction lands |
| `longform_finding` | `findingId`, `text`, `itemIds`, `kind` (`theme`, `insight`, `fact`) | the digest's new themes and the model's notes |
| `longform_step` | `stepId`, `state` (`now`, `done`, `skipped`, `failed`), `result` | step changes; the write-up step is `done` when the report lands |
| `longform_budget_end` | `reason` (`items`, `phone_time`, `list_end`, `spend`, `stuck`, `max_steps`, `timeout`), `detail` (why it is stuck) | the loop ends at the budget, or because it stopped making progress |
| `phone_released` | `phoneSeconds` | the loop is done with the phone; the report is written after |
| `longform_synthesis_failed` | `model`, `error` | a report call failed (then Sonnet once more, then the deterministic report) |
| `longform_report` | `report`, `markdown` | the report is written |
| `longform_picked_up` | `from`, `items`, `kept` | a picked-up long run kept its notebook |

`cost` events report the run's whole spend (the ledger), not only the navigation client's. Every model call's
`inference_started` and `inference_finished` carry a `purpose`: `decision`, `planning`, `extract`, `digest`,
`synthesis`, `followup` or `<purpose>_hedge`.

## The result

A long run's `result` (and the run's `summary`) adds `report` and `markdown`. `answer` is the TL;DR's first sentence,
`data` is the Markdown with `resolved_output_format: "markdown"`, the outcome is `done` (`check` when the report is
partial or a commit went unproven), and `proof` holds up to three of the report's first sources as `{quote, app,
screen, frameId}` (`screen` is "Reel 12"). A partial report's `reason` is "Stopped after 31 of 50 Reels. This covers
those 31."

```json
{"title": "What I learned from 44 basketball Reels",
 "tldr": "The feed was mostly practical coaching: ...",
 "kind": "feed",
 "sections": [{"heading": "Footwork drills come with specific reps and cues",
               "bullets": [{"text": "A 3-cone first step drill is given as 4 sets x 30 s ...", "sources": ["i1", "i9"]}]}],
 "ranked": [],
 "gaps": ["Mobster read captions, on-screen text and a few frames of each video. It doesn't hear the audio."],
 "partial": false, "stopReason": null,
 "stats": {"items": 44, "seen": 58, "ads": 5, "repeats": 3, "offTopic": 6, "seenBefore": 0, "phoneSeconds": 79,
           "reportSeconds": 11, "costUsd": 0.09},
 "sources": {"i1": {"creator": "@hoopslab", "name": null, "title": null, "frameId": "f0a1b2c3d4", "n": 1,
                    "kind": "Reel 1", "caption": "My go-to first step drill before every practice #footwork",
                    "summary": "...", "app": "com.burbn.instagram"}}}
```

The model writes `title`, `tldr`, `sections`, `ranked` and `gaps`. Code drops sources that aren't kept items or
notes, drops bullets left with no source, caps sections and bullets at 6, fills `stats`, `sources`, `partial` and
`stopReason`. A shortlist ranks against what the person asked for: the request and the conversation's standing
preferences, a field a profile states (a religion, a neighbourhood) included. A note (`f4`, what the agent noted or did) can be a source, which a chain needs; its source
entry has `note: true` and the Markdown names no one for it. A report that couldn't be written by a model has
`fallback: true` (and `synthesisFailed: true`): its sections list the items by tag, so the notebook is never lost.

`GET /api/runs/{id}/longform` (`?items=0` leaves the items out) answers `{plan, notebook: {counters, seenBefore,
steps, items, facts, digest, phone, focus, kind}, report, markdown}`, live while the run goes and from the journal
after; 404 `not_longform` for any other run. `GET /api/runs/{id}/longform/report.md` is the Markdown as a download
(`Content-Disposition: attachment; filename="Mobster – {title}.md"`); 404 `no_report` before it is written.

## The notebook and what the model sees

The notebook (`notebook.py`) is append-only: items (a Reel, a profile, a chat), facts (the model's `notes_add`, the
digest's themes, step results, the final answer) and compacted history lines. An item whose identity was seen before
is stored as a repeat and never extracted; so is one an earlier long task in the same conversation already showed
(`seen_before`, and the report says "12 were already in an earlier task in this conversation"). Strings are capped and
masked; the journal copy (schema `longform` v1, tables `longform_notebooks` and `longform_reports`, deleted with their
run) is redacted on a writer thread at every step change, every 10 items, at phone release and at the end.

A long run's prompt: `[system + LONGFORM_SYSTEM]` (cache breakpoint 1), `[request, apps, plan steps]` (2), the
notebook's epoch (3: counts, digest, themes, the newest 15 facts and 6 items, steps; re-rendered only when the digest
or a step changes or 10 new items or facts arrive), then the turn: the budget line ("Budget: 41 of about 50 Reels. 1
min 12 s of phone time used (cap 10 min). $0.08 spent of $1.00 (report reserve $0.03). Wrap up when the items are
covered."), the notebook's delta since the epoch, the newest 6 compacted log lines, the last 16 history lines, the
screen and the screenshot. Every 8 history lines that leave the window become one log line made by code. The reply
gains `step` (the plan step the model works on): moving past a step marks it done, emits `longform_step` and saves
the notebook. Every 15 newly extracted items or 10 new facts, one Haiku call (no thinking, never hedged, its own
thread) rewrites a 600-character digest with themes.

## Models

| call | Claude (default) | OpenAI key only | thinking |
| --- | --- | --- | --- |
| navigation turns | `claude-sonnet-5-5` | `gpt-5.6-sol` | low |
| per-item extraction, digest, map-reduce chunks | `claude-haiku-5-5` | `gpt-5.6-luna` | disabled, never hedged |
| the report | `claude-sonnet-5-5` | `gpt-5.6-sol` | low; a failed call goes once more to a fresh Sonnet client |
| follow-up answers | `claude-sonnet-5-5` | `gpt-5.6-sol` | low |

Haiku 5.5 is priced at $0.10 / $0.50 / $0.01 / $0.125 per MTok (input, output, cache read, cache write; read 6 Oct
2026) and `anthropic_thinking` maps it like the other 5.5 models: `none` disables thinking.

**The report's model, measured.** Synthesis A/B, 8 Oct 2026, on two notebooks recorded from the scripted world (the
feed track's `longform_world`: 58 Reels seen, 44 kept; 15 Hinge profiles), scored with the feed track's rubric
(`longform_rubric.score`) and a Sonnet judge for the insight part. Real calls, $0.58 in all, logged to the shared
spend log.

| notebook | model | runs | rubric (mean) | $ per report | seconds (mean) |
| --- | --- | --- | --- | --- | --- |
| feed (6,747 input tokens) | Sonnet 5.5, low | 2 | 0.844 | 0.031 | 11.0 |
| feed | Opus 5.5, medium | 2 | 0.849 | 0.076 | 20.8 |
| survey (5,184 input tokens) | Sonnet 5.5, low | 4 | 0.885 | 0.031 | 10.8 |
| survey | Opus 5.5, medium | 4 | 0.902 | 0.080 | 24.6 |

The cost is each arm's first report (the later runs read the first one's prompt cache, which a real report never
does; the report call now sends no cache breakpoint at all).

Opus stays a kind's default only when it beats Sonnet by at least 0.05. It didn't (by 0.005 and 0.017), and Sonnet
writes the report in under half the time for about 40% of the cost, so Sonnet writes every report
(`models.SYNTH_DEFAULT`). Every shortlist put Riley and Noor first; the top 3 matched the ground truth exactly in 3
of 4 Sonnet reports and 1 of 4 Opus reports. The key frames a feed report gets in production were not in these
prompts.

Over 120 kept items the report is map-reduced: Haiku groups each 60 into at most eight themes. The report receives
those themes, the 40 highest-quality items, and a raw line for every kept item no valid displayed theme covers.
A failed, empty or malformed chunk summary leaves its items as raw lines. Item lines include bounded extracted
details such as names, numbers, dates, prices and ratings, or screen details when extraction fails.
If the raw selection exceeds 500 items, Mobster skips the report model and produces a deterministic report with an
explicit coverage-limit gap. The notebook still holds the observations. Theme IDs represent source coverage; they
do not prove a theme's conclusion. The spending reserve covers up to 500 raw items plus all map chunks even when a
successful map normally leaves only 40 raw items.

## Long conversations

After each run in a conversation the thread keeps up to 12 numbered things it produced (`_context.referents`: a
report's shortlist, else an answer's numbered or bulleted lines, else its proof) and a long run's report in short
(`_context.report`, at most 1,200 characters: title, TL;DR, headings, shortlist). The next run's thread block has a
third part, "Lists from earlier tasks in this conversation" (fenced as data, newest first, at most 1,500
characters), and the block is capped at 4,500 characters, so turn 20 of a thread costs what turn 4 does
(`test_longform`: turn 20 resolves turn 3's list, and its prompt is at most 1.1 times turn 4's).

`RECALL text` (offered in long runs and in runs in a conversation with earlier tasks) searches the conversation's
earlier asks, answers, notes, lists and reports, their notebooks while their runs are retained, and this run's
notebook, and returns at most 12 matching lines.

A question in a conversation about an earlier long task's report ("Which creator had the best footwork drill?", not
"open it again") is answered by one Sonnet call from the report and notebook before the phone is touched: the run
ends `done` with the note "From the report of your earlier task.", in 2 to 5 s for at most $0.03. A question the
report can't answer, or a request to act, runs as usual.

## Reliability

- A navigation call that fails in transit (429, 5xx, 529, a dropped connection, a timeout) is sent again after 2, 5
  and 10 s; then the run ends `error` with a partial report. Stop is honoured during the waits.
- A long run a person started waits for them on a lock in the middle of the run, as at the start ("Look at your
  iPhone", `handoff_waiting` with `midrun: true`). Not unlocked in time: it stops `phone_locked` with a partial
  report and can be picked up.
- Stop, the budget, an error or a lost phone all end with a report (`partial: true` and the reason).
- **No progress ends the run.** `LongSession` keeps a progress signature each turn (the counters, the number of
  notes, the step states, and the screen's content fingerprint and app). The run ends `blocked` with a partial report
  (`longform_budget_end` reason `stuck`) after 6 turns with no new item, note, step change or screen not seen since
  the last of those; after 3 refused actions in a row (a loop, a repeat, a gate); or after 2 macro returns in a row
  that saved nothing and say the feed or list is done (`stuck`, `looping`, `not_feed`, `end`, `refused`). A LAUNCH_APP
  of the app already in front is a no-op: it doesn't end a run of refusals. When the two returns were READ_EACH's
  `end` and items were saved, the list is read: the run ends `completed` (`list_end`).
- **Approvals in a long run.** Every act that asks (a declared commit whose act is in `contract_asks.ASK_ACTS`; without a
  contract, a control `COMMIT_CONTROL` or "like" names) goes through the run's approver even with Ask before acting
  off (`FrontierAgent._long_asks`, through `forced_approve`); with nobody to ask it is refused.
- **Feeds and swipes.** WATCH_FEED runs only on a full-screen item: an element as big as the screen that the tree
  names as an item ("Reel by @hoopslab") or a `Video`, and a first read that shows at least 2 of a creator, a caption
  or audio line and an item label; otherwise it returns `not_feed` before its first swipe (a home feed, DMs, Stories
  or a card stack, where a swipe may be an action).
- **READ_EACH only reads.** It taps a row, the `open:` control and Back, never a control whose label names an act (a
  like, a message, a commit, Report or Unmatch), in any app. Each page becomes an item of the kind the plan's
  collection names (`profile` for profiles or matches, `chat` for chats, DMs or messages, else `row`), with one frame
  for the extractor.
- A picked-up long run (`resumeFrom`) keeps the old run's notebook, counters, step states, cumulative phone time and
  spend, and gets the remaining budget. Checkpoint phone time can restore an interrupted segment without a release;
  pauses and approval waits are excluded once. Damaged elapsed-time state exhausts the phone-time budget rather than
  granting more actions.

## Result blocks

`blocks.attach` gives a long task's report `blocks`, the result-block envelope `{"v": 1, "blocks": [...]}`: an inbox
sweep's `review_queue`; a feed of videos' `media_list` (each kept item's key frame, creator, topic and format, and its
audio line as its sound) and a `chart` of the formats two or more items share; a feed of posts' `table` (creator,
topic, what it says, each row with its frame). They are data only, with screenshots by frame id.

## Inbox sweep

"Go through my messages and draft replies to anything that needs one" is a survey over conversations, and its plan
card says "drafts replies ... for you to review. Nothing is sent." Its last step is "Sort them and draft replies, sending
nothing". Only a request that asks for replies
(`inbox.wants_drafts`: draft, write or suggest ... replies, answers or responses) gets drafts; any other long task over
the same lists reads them as rows, as before.

- **Opening.** READ_EACH opens each conversation, unread ones included (the owner's call, 10 Oct: they show as read
  afterwards).
- **Drafts.** With replies asked for, the extractor reads each conversation (messages, chats, DMs or emails) with
  `extract.CHAT_SCHEMA`: who it is with as the screen names them, who wrote last, the last message, its `bucket`
  (`reply`, `wait` or `spam`), whether it needs a reply, a short draft when it does, and `decision`: what only the
  person can decide before replying ("Whether Friday at 8 works"), with that part of the draft left as a blank in
  square brackets. It never promises, buys, books, accepts or declines in a draft. Code checks the bucket: a message
  the person sent last can wait, and spam gets no draft.
- **The review queue.** `inbox.attach` (called by `synthesis.synthesize`) puts `drafts` in the report it saves and
  emits, titles it "Review queue: 31 DMs", and adds "Review queue (nothing sent)" to the Markdown. The queue itself
  is a `review_queue` result block (`longform/blocks.py`, schema v1): every conversation, replies first, each with
  `to` as the screen named them, `context`, `lastMessage`, `draft`, `decision` and `frameId`. The run view draws it;
  Approve starts a separate task that sends exactly that draft to exactly that recipient, which asks before it sends.
- **Review only, nothing sent.** The run proposes nothing for sending and sends nothing.
- **Triage** (`MOBSTER_INBOX_TRIAGE=on`; off by default, unmeasured). Before READ_EACH opens a list's conversations,
  the run's own model reads the list's previews in one call and names those that need no reply; they aren't opened.
  "Needs a reply?" is a judgment, so it goes to the LLM, not a decision model. The previews go fenced as data, with
  secrets masked; a failed call opens every row.
- **One row per app.** In a run whose plan names two or more apps, the session opens a sub-run when it reaches each
  app and closes it at the next app or when the phone is released (`run.subrun_start`, `run.subrun_finish`).

## Openers

A survey over posts or results whose request asks to "draft a DM" or "draft an opener" to each person
(`inbox.wants_openers`, and not replies) gets the same review queue. Its card says "drafts a DM to each person who
fits, for you to review. Nothing is sent." and its last step is "Draft a DM to each person who fits, sending nothing".

- **Reading.** The extractor reads each opened post with `extract.OPENER_SCHEMA`: the person as the screen names
  them, their handle, the post, the hook quoted from it, `fit` (yes, no or unsure), `no_dms`, and a first message.
  Its prompt says the message goes to someone who hasn't written to the person, and carries the text of the plugin
  skills the task matched (`session.guidance`, set by the Runtime from the "plugins" context blocks).
- **Code checks the draft.** Only `fit` "yes" without `no_dms` keeps a draft (bucket `reply`); the rest are `wait`
  with no draft. Screen text that asks for no DMs or no pitches (`extract.NO_DMS`) drops the draft too.
- **The queue.** Each item has `to`, `handle`, `context` (the hook, quoted), `lastMessage` (the post) and `draft`;
  the buckets read "Fits", "Doesn't fit" and "Ads and bots". `result_blocks.approval_item` refuses an item with no
  stored draft, and a draft with a part in brackets, as for a reply.

## Negated acts on the card

`plan.commits_in` leaves out the verbs a negation lists ("don't like, follow, reply to or message anyone", "never
post", "without liking"): `plan.negated_spans` takes the verbs right after "don't", "never", "without" and the like,
and each one after them in the same list. "Don't forget to text Sam" still texts. The card is all it changes: in a long
run every act that asks still asks before it runs (`frontier._long_asks`).

**Where the text goes.** The extractor sends each opened conversation's screen text and picture to the run's model
(Claude or OpenAI, whichever key the run plans on); triage, when on, sends the list's previews there too. The drafts
stay in the run's report on this Mac. Nothing goes anywhere else.

Not measured on the phone yet.

## The same in every app

The engine has no app-specific branch. What a long run may do follows the rules every run follows, plus one for long
runs: every act that asks (a reply, a like, a booking) asks first, even with Ask before acting off
(`FrontierAgent._long_asks`, through `forced_approve`, which `Runtime.run_smart` mounts for long runs only). The plan
card says so ("Asks before each message.") from the request's own words (`plan.commit_lines`). The frontier gets the
session and that asker, nothing else; a short task gets neither and runs byte for byte as before.

## Measured

On the owner's iPhone 15 Pro (iOS 26.0.1), 10 Oct 2026, Instagram, "Watch 40 Instagram Reels about AI and tell me
what you learned" (`bench/vague_run.py`: the bench safety monitor in front of every action, every approval declined;
Haiku 5.5 navigating and extracting, Sonnet 5.5 writing the report):

| | one run |
| --- | --- |
| items seen (kept, off-topic, ads) | 41 (32, 8, 1) |
| WATCH_FEED alone | 41 items in 71.6 s: 34 a minute, 1.75 s each |
| items a minute of phone time, all-in | 9.6 |
| cost, all-in; for 10 minutes of phone time | $0.045; $0.108 |
| navigation calls | 14 |

- Feeding itself beats the target (34 a minute against 25). Getting there doesn't: the planner spent 175 s retyping
  "AI" into Instagram's search field, whose typing never took, before a hashtag link worked.
- Before two fixes, an earlier run watched 35 items in 308 s (6.2 a minute): the viewer Instagram opens from search was
  "not a feed" (its full-screen element is labelled as a video cell, not "Reel by @name"), and each photo post or ad
  in that viewer sent WATCH_FEED back to the planner (14 times). `ads.VIDEO_SURFACE` and `BY_HANDLE` read such a
  screen by signs that name no app; `feed.MIXED_ITEMS` (4) reads a post with a creator or caption as the next item.

## Requested outcome receipts

A long task's report includes a bounded snapshot of its requested contract outcomes. It shows the current task
separately from earlier tasks picked up from the same notebook, labels requested payloads and recorded values, and
keeps screen receipt excerpts with their source task and turn. The desktop report ("What it was asked to do") and its
Markdown show each outcome as Confirmed, Partly confirmed or Not confirmed with the screen each excerpt came from, and
leave the task ids and turns out.
Plan steps marked done do not establish an outcome. A generic screen change, a forbidden-action constraint, and a
model-recorded value do not count as independent confirmation. A counted commit is complete only when its existing
proof gate passes and enough explicit action receipts were saved. Reading and writing outcomes without such receipts
remain unverified in this version. Interrupted checkpoint snapshots are labelled as such and cannot make a run
complete. The saved history retains at most 12 task snapshots, 24 contract items per task, and eight evidence
excerpts per item; the report shows omitted counts.

## The CLI

`mobster run TASK` prints a long task's plan and asks `Start? [Y/n, f for the full 10 minutes]` on a terminal, then
`This may cost more than your $1.00 limit per task. Allow up to $2.00? [y/N]` when the plan could pass the limit.
`--long` forces a long task, `--budget 10m|90s|1h|30|all` sets its budget, `--yes` starts without asking. While it
runs it prints a line every 10 items ("Watched 20 Reels, 2 ads skipped") and "Your iPhone is free to use. Writing the
report…", and at the end the report as Markdown. With `--json` the plan is a `longform_plan` event and the result
carries `report` and `markdown`.

```text
$ mobster run "Scroll Instagram Reels about basketball for 10 minutes and tell me what you learned" --execute

  Watches about 50 basketball Reels, the number you'd scroll in 10 minutes, then writes up what it learned.
    1. Open Instagram and find basketball Reels
    2. Watch about 50 Reels
    3. Write up what it learned
  About 2 min 15 s on your iPhone, about $0.12, report about 11 s later.
  Or use the full 10 minutes: about $0.14 to $0.43.
  Start? [Y/n, f for the full 10 minutes]
```
