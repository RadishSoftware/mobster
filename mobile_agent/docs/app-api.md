# The App API: every app on your iPhone is an API (14 Oct 2026)

Code: `mobile_agent/app_api/`. Tests: `mobile_agent/tests/test_app_api.py`. Reference: `docs/reference/app-api.mdx`
(generated). Started as a spike the same day; the owner made it a product feature.

## What it is

Each endpoint is defined once (`endpoints.py`, from an app pack's reader) and served two ways with the same input and
output JSON schema:

- REST: `GET /v1/<app>/<thing>` on a local HTTP server (`service.py`, `mobster api serve`).
- MCP: the tool `<app>_<thing>` in Mobster's MCP server (`mcp.py`, track `app_api`), so `mobster mcp` and the Mac
  app's bundled agent list it, with `outputSchema`.

```sh
TOKEN=$(mobster api tokens create "my script")
mobster api serve --phone "<your iPhone>" --allow-device "<your iPhone>" &
curl -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8780/v1/instagram/dms?n=10'
```

| Endpoint | Answers |
|---|---|
| `GET /v1/openapi.json` | OpenAPI 3.1 for every endpoint, generated from the same schemas (`x-mcp-tool` names the tool) |
| `GET /v1/apps?phone=` | each app with endpoints, the version on that phone, the version its pack was checked on, health |
| `GET /v1/<app>/<thing>?n=&arg=&phone=&profile=` | `{app, reader, title, n, columns, rows, stopped, screen, read_at, phone, took_ms}` |
| same, `Accept: text/csv` or `?format=csv` | the rows as CSV (`result_blocks.table_csv`: column labels, formulas defused) |

A read is the pack's read (`Pack.read`, `reading.py`): it opens the screen by deep link or by the app's own controls and
reads the accessibility tree into rows by code, with no model call. A script, n8n or Zapier, or an LLM's tool caller
can use an app that has no public API. The consumer face is Takeout for any app: your saved posts, DMs or orders as a
CSV. `phone` names the iPhone; `profile` (Settings › Profiles, read from the running Mobster) reads on the iPhone that
profile is signed in on, and is refused when the profile is on another app.

REST checks its query with the MCP server's own validator (`mcp_server/schemas.validate`) against the same input schema,
and the tests hold REST and MCP to the same answers and the same refusals.

The row schemas are typed when a pack declares `read_columns` (`{reader: ((key, label, type), ...)}`); the Instagram
pack knows its columns only when it reads, so its rows are described as objects and each answer carries `columns`.

The REST server is its own process on its own port (8780), not a route on `mobster serve` (8765). `GET /api/apps` is a
core route there, so `/v1/apps` can't be a track route without shadowing it, and the App API needs a long-lived token
that opens reads and nothing else, where serve's token is per launch and opens everything.

## Safety

- Reads only. Every endpoint is a GET; any other method answers 405, and a request with a body is refused. There is no
  endpoint that taps, types, likes, follows, sends, posts, buys or deletes, and the OpenAPI description says so.
- A read goes through the same calls as the packs' MCP read (`packs/mcp.py`, `PacksProvider.call`), on the same
  `ToolSet` `mobster mcp` uses: the device allow-list (`--allow-device`) and the device's lease, the app's permission
  for outside agents (its first use asks in the Mac app, which names the caller "the App API (local HTTP)"), the
  user's permission rules on every tap and keystroke, the run lock, and the action journal (`<APP>_<THING>`, as MCP journals a tool, started
  before anything is done, `unknown` when it stops part way). `PackIO` refuses an act's control, so a reader that
  would tap Like answers 403 and nothing is tapped.
- One read at a time per phone, over REST and MCP alike: a second read on a busy phone answers 409 with `Retry-After`. A run holding the
  phone's lock (an MCP tap, say) answers 409 too.
- A read has the MCP read's deadline, 40 seconds, then 504.
- Local only: it binds 127.0.0.1 and refuses a caller that isn't this Mac, a Host that isn't 127.0.0.1 or localhost
  on its port (DNS rebinding), and any request with an Origin (a web page). It needs `Authorization: Bearer` with a
  token from `mobster api tokens create`, one per client; `revoke` ends one at once. Only hashes are kept. A token in
  the query string is refused.
- Text read from the phone is data: the OpenAPI says so on every reader, for the LLMs that will read it.
- Nothing is logged per request: a query can hold a person's name. The journal keeps the operation, never what was read.

## Mirroring compatibility (measured 14 Oct 2026)

iPhone Mirroring has no accessibility tree: `perception.snapshot_from` turns each line Apple Vision reads into a
StaticText element, with no other roles, no selected state, no identifiers and no off-screen rows. Method: each
Instagram pack fixture (Instagram 448.0.0, `tests/fixtures/packs/instagram`, 14 screens) was turned into an
OCR-shaped snapshot through the real `perception.snapshot_from`, three ways: upper (every label becomes a line of
text), likely (only text a person sees drawn: StaticText, placeholders, links, short text buttons; no icon names, no
identifiers like `sundial-viewer-video-cell`, no composite Cell labels), strict (StaticText and placeholders only).
Then `PACK.recognize` and the reader's parser ran on each. Script: `~/mobster-work/launch/wtf/app-api/mirroring_compat.py`.

| Fixture | Screen | Evidence | Tree | OCR upper | OCR likely | OCR strict | Rows on tree | Rows on OCR likely |
|---|---|---|---|---|---|---|---|---|
| feed | feed | recorded | yes (1.00) | no (0.50) | no (0.00) | no (0.00) | 1 | 0 |
| feed-2 | feed | recorded | yes (0.83) | no (0.50) | no (0.00) | no (0.00) | 1 | 0 |
| reel | reels | recorded | yes (0.83) | yes (0.67) | no (0.00) | no (0.00) | 5/6 fields | 3/6 fields |
| reel-audio | reels | recorded | yes (0.83) | yes (0.67) | no (0.00) | no (0.00) | 6/6 fields | 3/6 fields |
| reel-ad | reels | recorded | yes (0.83) | yes (0.67) | no (0.00) | no (0.00) | 5/6 fields | 3/6 fields |
| reel-collab | reels | recorded | yes (1.00) | no (0.50) | no (0.00) | no (0.00) | 4/6 fields | 2/6 fields |
| explore | explore | recorded | yes (1.00) | no (0.50) | no (0.33) | no (0.00) | n/a | n/a |
| search-results | search | recorded | yes (0.83) | yes (0.67) | no (0.17) | no (0.17) | n/a | n/a |
| comments | comments | recorded | yes (1.00) | yes (0.83) | yes (0.67) | no (0.50) | 3 | 0 |
| dm-inbox | dm_inbox | inferred | yes (1.00) | no (0.57) | no (0.29) | no (0.00) | 5 | 0 |
| dm-thread | dm_thread | inferred | yes (1.00) | yes (0.80) | no (0.40) | no (0.40) | 3 | 3 |
| profile | profile | inferred | yes (1.00) | yes (0.83) | yes (0.83) | no (0.00) | 3 | 3 |
| saved | saved | inferred | yes (0.83) | yes (0.83) | yes (0.83) | no (0.33) | 0 | 0 |
| saved-all | saved | inferred | yes (0.83) | yes (0.83) | yes (0.67) | yes (0.67) | 3 | 0 |

Recognized, of 14 screens: tree 14, OCR upper 9, OCR likely 4, OCR strict 1. 17 of the pack's 57 signals can never
match an OCR line (a role OCR has no word for, like Button or Cell, or `selected`), and the feed's and Explore's
`must` signal is one of them (the selected tab), so neither is ever recognized by pixels. The readers fare worse than
recognition: DMs, comments, feed posts and saved posts parse composite Cell labels ("Unread, Sam Lee, Delete, 4 new
messages, 3 hours ago") and Buttons' counts, which OCR never sees as one line; a profile's numbers and a thread's
bubbles survive.

So "every app is an API" works on the tree connection today and not over Mirroring. Mirroring needs a second signal
set per screen (`field="text"` lines with regions, never a role) and parsers over text lines; the Reel's and the
profile's are closest. Fixtures for it are OCR readings of the real app, which only a phone run can record.

## What it doesn't do

No acts: nothing taps, types, likes, follows, sends, posts, buys or deletes through it, and no write endpoint is
planned. One read at a time per phone: a second read answers 409 at once instead of queueing. It reads only apps that
have a pack (Instagram's and TikTok's come from the app-packs work), and only what a pack's readers read. It doesn't listen beyond this
Mac, and it has no rate limit yet.

## Path to product

1. Done (PR 2): `mobster api serve` and per-client tokens (`mobster api tokens create|list|revoke`, kept as SHA-256
   hashes in `app-api-tokens.json`, refused wholesale when someone else could write that file). Next in the Mac app:
   Settings › For developers starts it beside the agent, makes and revokes tokens, and lists each endpoint.
2. Takeout presets: a button per reader ("Export my saved posts") that calls the same endpoint with `format=csv` and
   saves the file; a schedule on top is Mobster's paid autopilot.
3. Remote: the Mobster Remote tunnel forwards `/v1` so cloud tools (Zapier, n8n, a hosted agent) can call it, with the
   app's permission and the user's rules asking in the Mac app as they do now.
4. Rate limits per token (reads per hour per app) and per-token app scopes.
5. Typed rows: each pack declares `read_columns`, so the OpenAPI is fully typed and client generators work.
6. Mirroring: the OCR signal set and text parsers above, measured on recorded OCR readings.
7. Apps without a pack: one general endpoint that reads the screen in front into a JSON schema the caller sends (a
   model call, priced per read), defined once like the others.
