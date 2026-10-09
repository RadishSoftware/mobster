"""`mobster verify`: check an iOS app on a simulator Mobster manages and return passed, failed, needs review or
couldn't run, with the frames that prove it.

- checks.py: the check format (YAML, CLI flags or MCP arguments) and ``Check``;
- assertions.py: the assertion language, evaluated on the accessibility tree, never by a model;
- tree.py: the evidence read, ``AXTree`` and ``AXNode``;
- runner.py: ``VerifyRun`` and ``verify()``, the verdict precedence and the run folder;
- report.py: result.json, report.html and the accessibility overlay frame;
- cli.py: `mobster verify`'s flags and output.

Nothing here imports mobile_agent.sim at module level: the simulator manager is loaded when a run starts.
"""
