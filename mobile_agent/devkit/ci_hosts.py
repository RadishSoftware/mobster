"""The files `mobster ci init --hosted` and `--host xcode-cloud|codemagic|bitrise|circleci` write.

Every file installs Mobster for Mac with its Homebrew cask (the `mobster` command is its agent), prepares a simulator
and runs `mobster ci run` with a CI token. None of them asks for a phone: a hosted runner has none."""

from . import APP_AGENT

DOCS = "https://docs.mobster.dev"
INSTALL = "brew install --cask radishsoftware/tap/mobster-app"

GITHUB_HOSTED = """# Mobster checks on every pull request, on a GitHub-hosted Mac (@DOCS@/ci/github-hosted).
# Add MOBSTER_CI_TOKEN as a repository secret, and ANTHROPIC_API_KEY for checks with steps.
name: mobster
on: pull_request
permissions:
  contents: read
  pull-requests: write   # the one comment Mobster keeps up to date
concurrency:
  group: mobster-${{ github.event.pull_request.number }}
  cancel-in-progress: true
env:
  MOBSTER: @AGENT@
jobs:
  checks:
    runs-on: macos-15
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # the diff needs the base branch
      - name: Install Mobster
        id: mobster
        run: |
          @INSTALL@
          echo "version=$(brew list --cask --versions mobster-app | awk '{print $2}')" >> "$GITHUB_OUTPUT"
          echo "xcode=$(xcodebuild -version | head -1 | tr -d ' ')" >> "$GITHUB_OUTPUT"
      - name: Cache WebDriverAgent
        uses: actions/cache@v4
        with:
          path: ~/Library/Application Support/app.mobster.desktop/wda
          key: mobster-wda-${{ runner.arch }}-${{ steps.mobster.outputs.xcode }}-${{ steps.mobster.outputs.version }}
          restore-keys: |
            mobster-wda-${{ runner.arch }}-${{ steps.mobster.outputs.xcode }}-
      - name: Prepare a simulator
        run: |
          "$MOBSTER" sim doctor --fix
      - name: Test what this PR changed
        run: |
          "$MOBSTER" ci run
        env:
          MOBSTER_CI_TOKEN: ${{ secrets.MOBSTER_CI_TOKEN }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}   # only checks with steps use it
      - name: Upload the report
        id: upload
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: mobster-report
          path: .mobster/ci/
          retention-days: 14
          if-no-files-found: ignore
      - name: Comment on the PR
        if: always()
        run: |
          "$MOBSTER" ci comment
        env:
          GITHUB_TOKEN: ${{ github.token }}
          MOBSTER_ARTIFACT_URL: ${{ steps.upload.outputs.artifact-url }}
"""

XCODE_CLOUD = """#!/bin/sh
# Mobster checks after Xcode Cloud builds a pull request (@DOCS@/ci/xcode-cloud).
# Add MOBSTER_CI_TOKEN as a secret environment variable of the workflow.
set -eu
if [ -z "${CI_PULL_REQUEST_NUMBER:-}" ]; then
  echo "This build isn't for a pull request, so Mobster tested nothing."
  exit 0
fi
MOBSTER=@AGENT@
@INSTALL@
"$MOBSTER" sim doctor --fix
status=0
"$MOBSTER" ci run || status=$?
"$MOBSTER" ci comment || true   # Xcode Cloud has no comment token; the verdict is in this log
exit "$status"
"""

CODEMAGIC = """# Mobster checks on every pull request, on a Codemagic Mac (@DOCS@/ci/codemagic).
# Put MOBSTER_CI_TOKEN in a variable group named mobster (Codemagic › Teams › Global variables and secrets).
workflows:
  mobster:
    name: Mobster checks
    instance_type: mac_mini_m2
    max_build_duration: 60
    triggering:
      events:
        - pull_request
    environment:
      groups:
        - mobster
      vars:
        MOBSTER: @AGENT@
        MOBSTER_BASE_BRANCH: main   # the branch your pull requests merge into
    scripts:
      - name: Install Mobster
        script: @INSTALL@
      - name: Prepare a simulator
        script: $MOBSTER sim doctor --fix
      - name: Test what this PR changed
        script: $MOBSTER ci run
    artifacts:
      - .mobster/ci/**
"""

BITRISE = """# Mobster checks on every pull request, on a Bitrise Mac (@DOCS@/ci/bitrise).
# Add MOBSTER_CI_TOKEN as a Secret in the app's Workflow Editor. Trigger the workflow on pull requests.
format_version: "13"
default_step_lib_source: https://github.com/bitrise-io/bitrise-steplib.git
workflows:
  mobster:
    envs:
      - MOBSTER: @AGENT@
    steps:
      - git-clone@8: {}
      - script@1:
          title: Install Mobster
          inputs:
            - content: |-
                #!/usr/bin/env bash
                set -euo pipefail
                @INSTALL@
                "$MOBSTER" sim doctor --fix
      - script@1:
          title: Test what this PR changed
          inputs:
            - content: |-
                #!/usr/bin/env bash
                set -uo pipefail
                "$MOBSTER" ci run
      - deploy-to-bitrise-io@2:
          is_always_run: true
          inputs:
            - deploy_path: .mobster/ci
"""

CIRCLECI = """# Mobster checks on every pull request, on a CircleCI Mac (@DOCS@/ci/circleci).
# Add MOBSTER_CI_TOKEN as a project environment variable. Pick the Xcode version from CircleCI's macOS list.
version: 2.1
jobs:
  mobster:
    macos:
      xcode: "16.4.0"
    resource_class: m4pro.medium
    environment:
      MOBSTER: @AGENT@
      MOBSTER_BASE_BRANCH: main   # the branch your pull requests merge into
    steps:
      - checkout
      - run:
          name: Install Mobster
          command: |
            @INSTALL@
            "$MOBSTER" sim doctor --fix
      - run:
          name: Test what this PR changed
          command: |
            if [ -z "${CIRCLE_PULL_REQUEST:-}" ]; then echo "This build isn't for a pull request."; exit 0; fi
            "$MOBSTER" ci run
      - store_artifacts:
          path: .mobster/ci
workflows:
  mobster:
    jobs:
      - mobster
"""

TEMPLATES = {"github": GITHUB_HOSTED, "xcode-cloud": XCODE_CLOUD, "codemagic": CODEMAGIC, "bitrise": BITRISE,
             "circleci": CIRCLECI}


def render(host):
    """The file for ``host`` ("github" means GitHub's hosted macOS runner)."""
    return TEMPLATES[host].replace("@DOCS@", DOCS).replace("@AGENT@", APP_AGENT).replace("@INSTALL@", INSTALL)


NEXT = {
    "github": ["Make a CI token (`mobster account ci-token create NAME`, or Settings › CI tokens in the console) and "
               "add it as the repository secret MOBSTER_CI_TOKEN.",
               "Add ANTHROPIC_API_KEY as a repository secret, for checks with steps.",
               "Commit the file and open a pull request."],
    "xcode-cloud": ["Add MOBSTER_CI_TOKEN as a secret environment variable of your Xcode Cloud workflow.",
                    "Run the script after the build: it is `ci_scripts/ci_post_xcodebuild.sh`, and Xcode Cloud finds "
                    "it by name.",
                    "Commit the file and open a pull request."],
    "codemagic": ["Put MOBSTER_CI_TOKEN in a Codemagic variable group named mobster.",
                  "Commit the file and open a pull request."],
    "bitrise": ["Add MOBSTER_CI_TOKEN as a Secret of the Bitrise app, and trigger the mobster workflow on pull "
                "requests.", "Commit the file and open a pull request."],
    "circleci": ["Add MOBSTER_CI_TOKEN as a CircleCI project environment variable.",
                 "Commit the file and open a pull request."],
}
