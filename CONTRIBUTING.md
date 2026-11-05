# Contributing

Contribute shared runtime, CLI and MCP changes directly to `RadishSoftware/mobster`. This repository owns that source independently of the Mac app.

You need Python 3.12 or later. Device operation requires macOS and Xcode; the tests need neither a device nor a model key.

```sh
make venv
make test-backend
mobile_agent/.venv/bin/python -m unittest mobile_agent.tests.test_build_identity
```

The CLI and MCP entry points are in `mobile_agent/__main__.py` and `mobile_agent/mcp_server/`. Simulator management lives in `mobile_agent/sim/`, verification in `mobile_agent/verify/`, and standalone packaging in `packaging/cli/`.

Keep changes reviewable. Include the command or behavior changed, the checks run and any remaining limitations in a pull request. Update documentation in the same change. Never commit real credentials, phone data, model weights or experiment output.

Release checks are maintained in [packaging/README.md](packaging/README.md). Verify each artifact through its own release path; publishing the Python package does not publish a frozen macOS build. App integration remains a separate cutover. App-only Remote and Teams tests stay with those extensions, while shared tests retain their core coverage.

`SOURCE_REV` identifies the app source commit used for the initial standalone import. It preserves that baseline and is not updated for subsequent CLI changes. Use this repository's Git history to identify the revision you are contributing to; contributions no longer pass through an engine export.
