"""Track `testing`: `mobster test` for app developers, on top of verify (SPEC §3.8).

- discover.py: which check files a run covers, the quarantine list, tags and shards;
- targets.py: where checks run (simulators, a matrix file, ``--device``) and the rules that keep a person's
  iPhone out of a repository's reach (developer builds only, never App Store apps);
- execute.py: one attempt, a verify run, with a stop, ``--parallel`` simulators and screen recordings;
- suite.py: the scheduler, retries, flakes, repeats and the exit code;
- reports.py: results.json, JUnit XML and the HTML report;
- record.py: a finished Mobster task as a check (`mobster test record`, Copy as check, MCP record_check);
- api.py: ``plan`` and ``execute`` for the CLI and the MCP tools; cli.py: `mobster test`; mcp.py: ``run_tests``
  and ``record_check``; register.py: the route and the MCP tools.

Only the testing track edits this package."""
