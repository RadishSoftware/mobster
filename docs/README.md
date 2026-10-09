# Mobster documentation

The pages in this folder are the docs site at [docs.mobster.dev](https://docs.mobster.dev). Read them there: the links below go to the site, which has search, the generated references and both themes. `docs.json` holds the site's navigation.

Mobster puts agents on a real iPhone. It tests the app you're building on your own phone and on simulators it manages, and it does your own tasks in the apps you're signed in to, from your Mac.

## Get started

- [Introduction](https://docs.mobster.dev): the two things Mobster does, and where to start.
- [Quickstart: Mobster for Mac](https://docs.mobster.dev/quickstart/mac), [Mobster CLI](https://docs.mobster.dev/quickstart/cli) and [test your app](https://docs.mobster.dev/quickstart/test).
- Concepts: [How it works](https://docs.mobster.dev/how-it-works), [Approvals and safety](https://docs.mobster.dev/safety), [Models and keys](https://docs.mobster.dev/models-and-keys), [Privacy and your data](https://docs.mobster.dev/privacy) and [Requirements](https://docs.mobster.dev/requirements).
- Your iPhone: [Device setup](https://docs.mobster.dev/device-setup), [Several phones](https://docs.mobster.dev/devices), [What it can and can't do](https://docs.mobster.dev/capabilities) and [Without the cable](https://docs.mobster.dev/wifi).
- [Pricing and license](https://docs.mobster.dev/pricing) and the [FAQ](https://docs.mobster.dev/faq).

## Test your app

- [Run every check](https://docs.mobster.dev/testing) with `mobster test`, and [add steps and save checks](https://docs.mobster.dev/test/steps).
- [Running checks in CI](https://docs.mobster.dev/ci).
- [Checks](https://docs.mobster.dev/checks): the check file, the assertion language, verdicts and the result JSON. [Simulators](https://docs.mobster.dev/simulators): the ones Mobster creates and `mobster sim`.

## Your tasks

- Mobster for Mac: [Tour of the app](https://docs.mobster.dev/mac/tour), [Conversations](https://docs.mobster.dev/conversations), [Voice](https://docs.mobster.dev/voice), [Memory](https://docs.mobster.dev/memory), [Files](https://docs.mobster.dev/files), [Schedules and workflows](https://docs.mobster.dev/schedules), [Settings](https://docs.mobster.dev/mac/settings) and [Keyboard shortcuts](https://docs.mobster.dev/mac/shortcuts).
- In the terminal: [Terminal UI](https://docs.mobster.dev/tui) and [Scripts and the API](https://docs.mobster.dev/scripting).
- [Troubleshooting](https://docs.mobster.dev/troubleshooting), by symptom.

## Agents and MCP

- [Add Mobster to your agent](https://docs.mobster.dev/agents) and [the plugin and skill](https://docs.mobster.dev/agents/plugin-and-skill).
- [MCP server](https://docs.mobster.dev/mcp-server): the key-less and Smart loops, driving a phone, `phone_task` and `run_tests`. [Instructions for your agent](https://docs.mobster.dev/agents/instructions).

## Reference

- [CLI reference](https://docs.mobster.dev/cli), [CLI options](https://docs.mobster.dev/reference/cli) and [MCP tools](https://docs.mobster.dev/reference/mcp-tools). The last two are generated from the code.
- [Configuration](https://docs.mobster.dev/configuration) and [Errors and exit codes](https://docs.mobster.dev/reference/errors).
- [Architecture](https://docs.mobster.dev/architecture), [Benchmarks](https://docs.mobster.dev/benchmarks) and the [Changelog](https://docs.mobster.dev/changelog).

## Contributing to the docs

The docs live beside the code and change in the same pull request as the behavior they describe. Write in the second person and the present tense, name commands and flags exactly as the code does, and show what a command prints. Pages link to each other with site paths such as `/testing#exit-codes`, and a test resolves every one. `python scripts/gen_docs_reference.py` writes the generated references, and a test checks that [CLI reference](https://docs.mobster.dev/cli) names every command and flag.
