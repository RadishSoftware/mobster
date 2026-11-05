# Traces: a run's tape, its bundle and its replay

18 Oct 2026. Experimental: recording needs `MOBSTER_EXPERIMENTAL_TRACES=1`, and a task records nothing without it.
Exporting a bundle and replaying one work on any run that has the data. The code is `mobile_agent/traces.py` and
`mobile_agent/bench/tape.py`.

A trace turns a failed run into something you can replay on a changed engine with no phone, no network and no model.

## What a run records

With traces on, a task in the Mac app, `mobster serve` or the terminal UI records a **tape** (the format is in
`bench/tape.py`'s docstring, and `bench/iosworld.py` has always written it for simulator runs): the request, every
screen the agent read as data (labels, values, frames, the tree's structure), every model reply with its usage, and
every action it sent, in order. A picture is kept as its size only. The tape is `tape.jsonl.gz` in the run's folder
beside its screens (`frames/<run id>/`, 0700; the file is 0600). The header names the hooks the live run had that a
replay doesn't (`hooks`: approvals, the phone guard, steering, tools).

- **Masked as it is written.** Each entry goes through `secret_filter.redact` before it reaches the disk: keys, tokens,
  card numbers, one-time codes, passwords and the other values the filter catches. The filter takes tens of milliseconds
  on a screen, so a tape masks and writes on its own thread; the agent never waits for it.
- **A cost bound.** A tape stops at 32 MB and is then removed whole: a tape with a hole replays nothing.
- **No tape without a place for it.** A run with no durable history (no journal on disk) records nothing.
- **Masking changes what replays.** A label the filter masks (a six-digit number reads as a code) is masked the same way
  in the recorded action, so the replay still matches it. The prompt text a replay builds differs from the live one,
  so `prompts same` is lower than on a simulator tape. The actions are the check.

## The bundle

```sh
mobster export latest --trace                     # ~/Downloads/mobster-trace-<id>.zip
mobster export 3f9c --trace -o ~/Desktop/bug.zip  # a file name, or a folder
mobster export 3f9c --trace --screens             # with the screenshots
```

| member | what it holds |
|---|---|
| `manifest.json` | the run's facts (task, app, status, outcome, cost, model), the event count, and whether the tape and the screens are in |
| `events.jsonl` | the run's journal, one event a line: each decision with its latency, each action with its exec and settle times, refusals, cost |
| `tape.jsonl.gz` | the tape, masked again on the way in; left out, with the reason in the manifest, when the run recorded none |
| `screens/*.jpg` | the run's screenshots, only with `--screens` |

Every string in the bundle goes through the secret filter, and so does every key. The run's answer data and result
cards stay out: the bundle keeps the facts. The task text and the text on its screens are not secrets to the filter, and
the screenshots can't be masked, so read a bundle before you share it. Nothing uploads it. The options are hidden from
`mobster export --help` until `MOBSTER_EXPERIMENTAL_TRACES` is set, and they work without it.

The test that plants values (`tests/test_traces.py`) puts an API key, a token, a card number, a code, a bearer token and
a password into the screens, the model's thought, the journal and the answer, then unpacks every member of the bundle
and fails on any of them.

## Evals

A failed task becomes a regression task you replay on every engine change:

```sh
mobster history --to-eval 3f9c                  # the task's tape and what it expects, into your eval set
python -m mobile_agent.bench evals              # replay every task offline: no phone, no network, no model
python -m mobile_agent.bench evals --dir DIR    # another set, for the tuning loop
```

The Mac app does the same from `POST /api/runs/{id}/eval` (a finished task, no body). The eval set is the `evals/`
folder beside the journal (`state/evals/` in the app's data folder), 0700, and each task is a folder (`bench/trace_evals.py`
has the format): `task.json` with the goal, the recorded outcome and failure line, and what it expects, and the task's
tape, masked. A task with no tape is refused with the reason: run it again with `MOBSTER_EXPERIMENTAL_TRACES=1`. Adding a
task twice changes nothing.

A replay can't say a new action is right, only that it is different, so each task expects one of two things of it:

| The task | Expects | Today's code acts the same | Today's code acts differently |
|---|---|---|---|
| failed | `changes` | `open`: the failure is still there | `changed` at step N: a candidate fix, for a paired live run to confirm |
| finished | `holds` | `holds` | `broken` at step N |

`expected.status` records the outcome a fix should reach (`completed` for a failure). Only a live run shows it; the paired
A/B in `~/mobster-work/tuning/PLAN.md` is where a `changed` task is promoted or dropped. `bench evals` exits 1 when a
task is `broken` or can't be read, and 2 when the set is empty.

Measured 19 Oct 2026: the 24 failed runs of the 18 Oct iOSWorld run, each added as an eval (their tapes, masked again on the
way in), replay in 28 s for all 24 in one process on a Mac at a load average near 35: 21 `open`, 3 `changed` (tasterank-001,
multi-092 and mem-042, the runs the guard fixes target). The masked tapes replay as the unmasked ones did.

## Replay

See [benchmarks.md](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/benchmarks.md) for the replay command.
