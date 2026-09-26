# Compiled intent: what runs before, instead of, and after the step model

Jev decides one step at a time from the accessibility tree. Much of a phone task is not a
judgment, though: it is written in the request ("In Settings > General > About"), or it is
arithmetic over what was observed ("how many"), or it is a question about pixels the tree does
not carry. Mobster compiles those parts into deterministic or verified mechanisms and leaves Jev
the steps that need judgment. Every mechanism is measured against MobsterBench-iOS
(`mobile_agent/bench/`); the numbers below are from the 24 Sep 2026 runs.

## Routes (`routes.py`)

The request's named screens become a route: hops parsed from breadcrumbs ("A > B > C"),
"open A, then B", "(under A)", or a plan step. Each step answers from the screen alone:

- the screen's title (its navigation bar, or the text inside a class-named bar) is the odometer;
- the next hop visible as a row: tap it, with no model call;
- not visible on a list: scroll, a bounded number of times; still missing and the app has a
  search field: search for the hop's name ("the note titled 'MobsterBench Note'");
- a selected tab is a hop already taken; a tap that changes nothing hands back to Jev;
- once arrived, the row the question asks about ("is Auto-Correction on") is scrolled into view.

Measured: nine Settings tasks that failed with Jev choosing WAIT at 0.2 confidence on the root
screen passed at 5–9 s each.

## Answer probes (`Agent._page_probe`)

Answer selection and verification start on a side connection when the screen may already answer:
a web page whose off-screen text (WebKit publishes the whole page) mentions the request, or the
asked-about row now visible. A SUPPORTED answer ends the run; anything else is dropped. A scroll
waits for a probe already in flight, because a Safari swipe costs ~2.5 s.

## Direct opens (`requested_url`, `requested_search`)

An address in the request opens with one WDA call; "search the web for X" opens the results page
for the request's own words (Wikipedia's search when the request names Wikipedia). Measured:
`web.type_url` and `text.safari_url` 2.2 s each.

A Wikipedia article the request names by title ("the Wikipedia article about iPhone 15 Pro")
opens as Wikipedia's go-search, which lands on that article; a described one ("the article
about the engineer the tower is named after") stays a link to follow.

## Evidence the screen already holds

- Off-screen page rows that share a word with the request are kept ahead of nearer rows
  (the Apollo 11 Moon-landing row sat past the nearest 300 nodes of its infobox).
- Invisible direction marks are stripped at the source (Calculator shows "\u200e579").
- A Settings Wi-Fi or Bluetooth row is "off" only when it reads "Off"; any other status is "on".
- A helper citation that names the neighbouring entry of the one holding its quote is
  re-pointed; the value is never changed, and anything else still fails validation.

## Dataflow plans (`plans.py`)

A multi-app question compiles once (one helper call) into 2–4 steps, one app each, that pass
named values: `{"app": Settings, "request": "Settings > General > About: report the iOS version",
"finds": {"ios_version": …}}`, then `"Open en.m.wikipedia.org/wiki/IOS_{ios_version:major}"`.
Each step is an ordinary verified run; values move between apps only as cited literals, reshaped
only by fixed transforms (`major`, `number`, `year`). The user's prohibitions are copied into
every step by code. Measured: `multi.ios_release` 10.4 s.

## Answers that are not literals

- **Answer sets** (`extraction.closed_answers`): "Answer 'on' or 'off'" becomes the field's enum;
  a value from the set may be a judgment about cited evidence, which the verifier must accept.
- **Switch states**: a switch's 1/0 is rendered as on/off in both the evidence and Jev's view.
- **Counts** (`Agent._counted_answer`): the helper lists the counted items as cited literals; a
  second, differently worded listing must cite exactly the same rows on the final screen; the
  count is computed in code. The verifier is not asked: replayed, it scored a right and a wrong
  count the same.
- **Surveys** (`loops.py`): "which of these 12 images…", "how many photos in this album…"
  compile as loops that only look; per-item judgments come from the tiered VisionJudge and the
  answer (names, count, labels) is assembled in code, or the run abstains. What the helper
  gets wrong is settled by code: the collection's shape (grid, list) and item selector come
  from the screen, stray fields are dropped, and a refusal the request's words do not support
  is asked once more. A survey behind a route compiles on arrival, not at the start screen.
  Items with identical labels (every imported photo is "Photo, September 24, 2:51 AM") are
  told apart by a 256-bit gradient hash of their thumbnail, so a scroll cannot re-count or
  skip one. Measured: visual 2/8 (pass 5) to 6/8; album surveys 51 s to 26 s.
- **Visual answers** (`visual_answer.py`): a question about one picture on screen is answered by
  the VisionJudge from the request's own answer set, recorded as `visual_evidence`, or abstained.

## Guards that stay

The action verifier, effect ledger, stop gate and answer verifier still run. Relaxations are
narrow and each is measured: a navigation-shaped tap under an UNCLEAR verdict proceeds (UNCLEAR
is not evidence of a side effect); a question's read-only step proceeds under an UNCLEAR stop
gate unless the request states a condition; keypad presses are input, not repeated effects;
two agreeing extractors with strong per-claim scores override a split overall verdict (logged as
`agreement_claims` for audit; see `evals/verifier_calibration.py`).
