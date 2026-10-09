# Contributing to Mobster CLI

Thanks for helping. Bug reports, device reports, fixes, docs and tests are all welcome. This page covers the setup, how the code is laid out, and what a pull request needs.

## Set up

You need macOS and Python 3.12 or later. No phone, simulator or key is needed for the tests.

```sh
git clone https://github.com/RadishSoftware/mobster
cd mobster
make venv                                   # mobile_agent/.venv with the pinned requirements
mobile_agent/.venv/bin/python -m mobile_agent --demo
```

If `python3.12` is not on your `PATH`: `make venv PYTHON_BOOTSTRAP=$(brew --prefix python@3.12)/bin/python3.12`, or with uv: `uv venv --python 3.12 mobile_agent/.venv && uv pip install --python mobile_agent/.venv/bin/python -r mobile_agent/requirements.txt`.

## Run the tests

```sh
make test-backend                           # the whole suite, as CI runs it
mobile_agent/.venv/bin/python -m unittest mobile_agent.tests.test_tui          # one module
```

The suite takes about two minutes. It uses fake drivers, recorded screens and the scripted demo phone, never a device or a network. On a slow machine, `MOBSTER_TEST_TIME_SCALE=2.5` relaxes the timing bounds, as CI does.

The terminal UI's tests drive the real app with Textual's pilot (`mobile_agent/tests/test_tui.py`). `mobster --help` and every command's help are pinned in `mobile_agent/tests/snapshots/help/`. After changing help text on purpose, rewrite the snapshots and read the diff:

```sh
MOBSTER_UPDATE_SNAPSHOTS=1 mobile_agent/.venv/bin/python -m unittest mobile_agent.tests.test_cli_surface
```

To look at the UI after a change, render it headlessly:

```sh
mobile_agent/.venv/bin/python scripts/tui_screenshots.py /tmp/mobster-shots --size 120x36 --png
```

It writes an SVG per state (idle, running, approval, finished, details, history, help, no phone), and a PNG with `--png` when Google Chrome is installed. The images in `docs/images/` come from this script.

## Find your way around

[Architecture](https://docs.mobster.dev/architecture) maps the code. The short version:

- `mobile_agent/agent.py` is the observe, decide, verify, act loop, and `drivers.py` talks to WebDriverAgent.
- `mobile_agent/server.py` has `Runtime`, which runs tasks for the terminal UI, `mobster serve` and the Mac app.
- `mobile_agent/tui/` is the terminal UI. `session.py` is its engine and imports no UI library. `app.py` draws.
- `mobile_agent/__main__.py` is the `mobster` command.
- `docs/` holds user docs, and `mobile_agent/docs/` holds dated design notes.

The terminal UI must not grow its own copy of run logic. If it needs something a run does not offer, add it to `Runtime` or `Agent`, where `serve` and the Mac app get it too.

## Make a change

- **Keep behavior and docs together.** A change to a command, a flag, an event or a setting updates the docs in the same pull request. A test checks that `docs/cli.md` names every command and flag. Write in the second person and the present tense, name commands and flags exactly as the code does, show what a command prints, and use placeholders such as `<UDID>` for anything personal.
- **Safety code stays conservative.** Guards, approvals, the journal and the device lock fail closed on purpose. A change that makes Mobster act in more situations needs a test that shows where it now acts, and one that shows where it still refuses.
- **Measure speed claims.** A change that claims to make Mobster faster says what was measured, on what, and before and after.
- **No private data.** Do not commit keys, env files, journals, screenshots of personal screens, device UDIDs or team IDs. Use placeholders such as `<UDID>` and `<your team id>`.

## Open a pull request

1. Branch from `main`.
2. Run `make test-backend`.
3. Fill in the pull request template: what changed, why, and how you tested it. For UI changes, attach a screenshot from `scripts/tui_screenshots.py`.

Each commit message says what changed and why, in plain sentences. Small, focused pull requests are reviewed fastest.

## How a pull request lands

This repository is exported from Mobster's source repository, which also holds Mobster for Mac. The `SOURCE_REV` file at the root names the source commit the files here were cut from. A maintainer merges your pull request here, applies the same commit in the source repository with your authorship kept, and the next export carries it. Nothing you merged is overwritten: the export refuses to publish while a change merged here is missing from the source. Contributions come in under the [MIT license](https://github.com/RadishSoftware/mobster/blob/main/LICENSE), the same license they go out under, so there is no CLA.

## Report a device or app

Mobster is measured on one iPhone model. Reports from other phones and iOS versions help a lot. Use the "Device problem or report" issue form, which asks for the output of `mobster doctor --json` and what worked.

## Code of conduct

Everyone who takes part follows the [Code of Conduct](https://github.com/RadishSoftware/mobster/blob/main/CODE_OF_CONDUCT.md).
