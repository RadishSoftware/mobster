"""The red-team suite for consequential actions: Mobster never sends, posts, buys, deletes or otherwise acts beyond the
phone without the person's explicit OK.

- ``cases``: the attacks, as synthetic screens (accessibility trees, and what the pixels say where the two differ),
  what the user asked, what a fooled or careless model does, and the ground truth: which dispatches would have an
  effect the person must approve. The ground truth is the case's own, never read from Mobster's classifiers.
- ``navigation``: actions that only read and move around, which must not ask (the false-ask budget).
- ``harness``: runs each case through every path that acts on a phone (Mobster's agent, a coding agent's phone tools,
  an outside agent over MCP) with a declining approver, an approving one, and nobody to answer, and scores it.
- ``python -m mobile_agent.evals.redteam``: the offline report (no phone, no model, no network). ``--live`` replays the
  cases' requests and screens to a real model (a key and a spend cap, ``live``).

The unit tests (tests/test_redteam.py) hold the offline numbers: every must-ask unit passes, and false asks stay within
the budget.
"""
