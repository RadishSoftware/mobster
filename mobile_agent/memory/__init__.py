"""Track `memory`: what Mobster's agent remembers for the person who uses it (SPEC §3.3).

- ``store``: facts, suggestions and settings in one SQLite file on this Mac (user_data_dir()/memory).
- ``extract``: suggestions read out of what the person types ("remember that…", "my X is Y", "I always…").
- ``retrieval`` and ``provider``: the ≤ 12 facts that go with a task, as one stable block.
- ``listener``: posts suggestions to the conversation and the bus; ``routes``: /api/memory; ``cli``: `mobster memory`.

Only the memory track edits this package. Routines (procedural memory) are v2.
"""
