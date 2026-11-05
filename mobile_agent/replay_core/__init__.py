"""The replay core: run the model once, record the exact action path, replay it with no model, heal as a diff.

One engine, two front ends (FEATURE-BACKLOG v2, item 1):

- **Checks** (`mobster test`): a passing Smart run records its actions with their tree anchors; later runs replay
  them through WebDriverAgent with no model call. A step whose screen or target changed is healed by Smart for that
  step only, and the change is written as a reviewable diff (``<check>.heal.yaml`` and ``healed[]`` in
  results.json), never silently. A heal never changes an assertion: the verdict is still read from the tree.
- **Saved workflows** (the Mac app's Workflows): after the first good run a workflow is *learned* and runs without
  the model. Typed text is never replayed blind (it comes from the workflow's own words, or the model writes it),
  and sends, posts and deletes still stop for approval.

Modules:

- ``path.py``: the recorded path (steps with anchors) and its validation;
- ``recorder.py``: records a Smart run's executed actions (the frontier's step observer, plus the screens it read);
- ``engine.py``: replays a path on a driver: preconditions, actions, approvals, heals and the circuit breaker;
- ``store.py``: the per-machine cache, keyed by what the path replays (the check or workflow), the app version and
  the iOS major version;
- ``heal.py``: heal entries, ``.heal.yaml`` files, Accept and Keep failing;
- ``checks.py``: the check front end (verify/runner.py calls it).

The workflow front end is part of Mobster for Mac: a pre-run hook and a run listener (harness_api) on this engine.

Nothing here calls a model itself: a heal is a callback the front end supplies.
"""
