"""Cheap evals: learn what a full A/B would tell us, for cents and minutes, from runs we already have.

The levels (the design is in the tuning loop's notes, not here):

- L0 (``l0``): trace analytics over recorded runs, $0. Wasted actions, step inefficiency against the shortest
  passing trajectory of the same task, time sinks by step type and app group, and where failing runs first
  leave the best passing run. One ranked table of the mechanisms that cost the most seconds and steps.
- ``traces``: the loader every level shares. It reads the iOSWorld adapter's task folders (task.json and
  prompts.jsonl) and the phone and simulator benchmark's artifacts (records.jsonl, events.jsonl, prompts.jsonl)
  into one shape: a Run of Turns of Actions.

Nothing here touches a simulator or a phone, and nothing here names a benchmark task: task text lives only in
the recorded runs and the frozen sets, which stay outside the repository.
"""
