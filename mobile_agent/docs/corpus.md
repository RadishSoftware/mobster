# Opt-in run corpus

Written 2026-10-26. Dark behind `MOBSTER_EXPERIMENTAL_CORPUS=1`; the flag off, nothing here runs and no command exists.

Every finished run is a list of (screen, action) pairs on real iOS. With your opt-in, Mobster keeps them in a private
file on your Mac. Nothing is uploaded or shared: sharing is a later decision, and it would need its own opt-in.

## The setting

Default off. It is on only after you were shown this text and said yes (`mobster corpus on` shows it and asks; Mobster
for Mac writes the same setting, `corpus/consent.json` in the data folder). A change to the text turns it off until
you answer again.

> **Help Mobster learn from your runs.** When this is on, Mobster keeps a private record of each task that finishes,
> one line per step, on this Mac only. Nothing is uploaded or shared. You can read everything it keeps with
> `mobster corpus export`, and turn it off at any time.

The full text is `TEXT` in `mobile_agent/corpus/consent.py`. `mobster corpus off` turns it off; what is stored stays
until you delete the `corpus` folder.

## What is stored

One JSON line per step, only for a run that ended done, was never redirected, and ran in an app other than Passwords,
Wallet and Health. A step on a screen with a password field is not kept.

| Field | Holds |
|---|---|
| `run` | 16 hex digits of a one-way hash of the run id |
| `app`, `version` | the app's bundle id and its installed version, or `null` |
| `screen.nodes` | the first 80 controls as `[role, token]`: the role (`Button`, `Cell`) and the label's token |
| `screen.fp` | 16 hex digits over the sorted nodes: the same screen shape gives the same value on any Mac |
| `action` | the operation (`TAP`, `SWIPE_UP`), whether it typed (`typed`, never the text) and the control it hit (`target`: index into `nodes`, role, token) |
| `outcome` | whether the screen changed, and that the run ended done |

A label's token is its lowercase text with digit runs folded to `#`, hashed to 10 hex digits (the digest
`compiled_read` uses for routes, so the two records line up). A label over 48 characters is `~long`; one that looks
like an email, phone number, handle or link, or that `secret_filter` would mask, is `~email`, `~number`, `~handle`,
`~link` or `~secret`. A short label such as a first name still hashes to a token that anyone can test a guess against,
which is why the file stays on your Mac.

A sample record (the label tokens are not text):

```json
{"v":1,"run":"9ea372114687a0a8","step":0,"app":"com.apple.Preferences","version":null,
 "screen":{"fp":"cafeba929dd9c0da","nodes":[["Cell","dfe2db7497"],["Cell","5780daf6db"],["Cell","~email"],["Cell","0676c665c4"]]},
 "action":{"op":"TAP","typed":false,"target":{"i":1,"role":"Cell","label":"5780daf6db"}},
 "outcome":{"changed":true,"run":"done"}}
```

## What is not stored

Screenshots. Text you typed. Any screen text, name, handle, number, message or link. The task's words. The model's
answers. Device ids. A run that did not finish, was redirected, or touched Passwords, Wallet or Health.

Every record passes `store.sanitize` before it is written and again before it is exported: each field must have the
shape above, every string must survive `secret_filter.redact_deep` unchanged, and a record that does not is dropped.

## Where it lives

`corpus/records.jsonl` in the data folder (0700 folder, 0600 files). At 4 MB it becomes `records.1.jsonl`, then
`records.2.jsonl`, and the oldest is deleted: 12 MB at most.

```sh
export MOBSTER_EXPERIMENTAL_CORPUS=1
mobster corpus on                       # shows the text, asks
mobster corpus stats                    # records, runs, bytes, steps per app
mobster corpus export --out corpus.jsonl   # a masked bundle to read; refuses to overwrite
mobster corpus off
```

## Code

`mobile_agent/corpus/`: `consent` (the setting), `skeleton` (a screen to nodes), `store` (JSONL, cap, rotation,
sanitize, export), `listener` (the run listener), `cli` (the command). Hooks outside it: `StepRecord.corpus` and one
line in `frontier/hooks.py` that fills it, a track in `tracks.py`, and `corpus.install` in `harness_api._load`.
