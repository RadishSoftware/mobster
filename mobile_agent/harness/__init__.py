"""Track `harness`: the agent loop as a real harness (SPEC §3.1, v1).

- ``control``: pause and continue, and "stop after this step", for a running Smart task (the steering queue's
  controls, beside its messages).
- ``checkpoints``: the agent's masked state at the end of each turn (journal table ``run_checkpoints``), and picking
  up an interrupted or stopped task from it as a new run.
- ``tools``: the policy over the tools other tracks register (order, availability, interactive-only, a budget of 8),
  and ``ASK_USER``, the clarifying question.
- ``prewarm``: the phone's WebDriverAgent session made ready, and one screen read kept for 3 s, while the person
  types, so the first action comes sooner.
- ``settings``: "Mobster may ask me questions" (Settings › Advanced).
- ``routes``: the HTTP API (docs/harness.md).

Only the harness track edits this package. Nothing here weakens an approval: a message, an answer to a question, a
pause or a prewarm never approves a send, buy, post or delete.
"""
