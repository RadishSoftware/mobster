# Contributing

You need Python 3.12 or later. Device operation requires macOS and Xcode; the tests need neither a device nor a model key.

```sh
make venv
make test-backend
mobile_agent/.venv/bin/python -m unittest mobile_agent.tests.test_build_identity
```

The CLI and MCP entry points are in `mobile_agent/__main__.py` and `mobile_agent/mcp_server/`. Simulator management lives in `mobile_agent/sim/`, verification in `mobile_agent/verify/`, and standalone packaging in `packaging/cli/`.

Keep changes reviewable. Include the command or behavior changed, the checks run and any remaining limitations in a pull request. Update documentation in the same change. Never commit real credentials, phone data, model weights or experiment output.

This migration candidate still needs the full backend and frozen-build gates before release. App-only Remote and Teams tests remain with those extensions. The shared test modules retain their core coverage and skip only tests that need an absent app extension.
