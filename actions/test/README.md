# Mobster test action

Tests the screens a pull request changed, on simulators or your own iPhone, and keeps one comment on the pull request with the verdict, the frames and GIFs. It runs [`mobster test pr`](https://docs.mobster.dev/testing#test-a-pull-request) on a macOS runner with [Mobster for Mac](https://mobster.dev/download) installed, through the agent inside the app.

```yaml
name: mobster
on: pull_request
permissions:
  contents: read
  pull-requests: write
jobs:
  test:
    runs-on: [self-hosted, macOS]   # a Mac with Mobster for Mac in Applications
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: RadishSoftware/test-action@v1
        with:
          scheme: YourApp
```

Every input, the comment it posts, and how to run it on your iPhone: [docs.mobster.dev/ci](https://docs.mobster.dev/ci#the-mobster-test-action).

A phone (`device`, one per line) or a TestFlight app (`testflight`) named in the workflow runs only when the runner's `.env` lists it in `MOBSTER_RUNNER_DEVICES` or `MOBSTER_RUNNER_TESTFLIGHT`, because a pull request can edit the workflow file but not the runner. If the `.env` doesn't list one of the phones, the job stops before a check runs.

Use the `device` and `testflight` inputs for phones. The action refuses `--device` and `--testflight` in `args`.

On the Team plan, set `MOBSTER_TEAM_KEY` (an upload key from the console, as a secret) in the step's `env`: the action adds `--share`, and the comment links your team's hosted report instead of the artifact ([docs.mobster.dev/ci](https://docs.mobster.dev/ci#share-the-report-with-your-team)).

The comment ends without a credit line. Set `tested-with: true` to end it with "Tested with Mobster", linked to mobster.dev.

## Replay a prebuilt app with no model and no `xcodebuild`

A check with steps runs Smart once on your Mac, keeps the steps it took, and replays them with no model call from then on ([Replay and heals](https://docs.mobster.dev/testing#replay-and-heals)). A job can test a build that an earlier job made, so the job runs no `xcodebuild`, needs no Xcode project and calls no model:

```yaml
jobs:
  replay:
    runs-on: [self-hosted, macOS]
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/download-artifact@v4       # the .app your build job made
        with:
          name: app
          path: .mobster/build
      # Then the test step from above, with these two inputs added:
      #   with:
      #     build: "false"                       # test the builds the checks name
      #     args: "--no-heal"                    # fail a changed screen instead of asking Smart
```

Each check names its build in `app.path` (for example `.mobster/build/YourApp.app`). Replays keep their recorded steps in `.mobster/cache/replay` on the runner, so a Mac records each check once and replays it after that. The runner needs the simulator tools that come with Xcode, which the job never calls. With `--no-heal` the job needs no API key. A check with no recording yet runs Smart, which needs one; `--keyless` reports it as couldn't run instead.

## Publish it to the Marketplace

The Marketplace lists a public repository with `action.yml` at its root, so this folder is published as `RadishSoftware/test-action`. `action.yml` already has the `name`, `description`, `author` and `branding` (the `smartphone` icon, purple) the listing needs. To publish a release:

1. Copy this folder's files to the root of `RadishSoftware/test-action` and commit them.
2. Tag the release `v1.x.y` and move the `v1` tag to it.
3. On the release page, choose **Publish this Action to the GitHub Marketplace**, accept the Marketplace agreement, and choose the categories Continuous integration and Testing.

The owner's account does steps 2 and 3.

A README badge says the app is tested with Mobster:

```md
[![Tested with Mobster](https://mobster.dev/badge/tested-with-mobster.svg)](https://mobster.dev/s/badge)
```

Both badges are at [mobster.dev/brand](https://mobster.dev/brand#badge).

This folder is the action's source. It is published as `RadishSoftware/test-action`. `action.py` uses the standard library only and runs on the runner's own `python3` (3.9 or later); its tests are `mobile_agent/tests/test_test_action.py`.
