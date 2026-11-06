# Standalone CLI packaging

The CLI build owns its entry point, freezer pins and signing entitlements in `packaging/cli/`. It requires no desktop checkout.

```sh
make venv
packaging/cli/build.sh
```

The build freezes the command, performs smoke checks and writes the macOS arm64 archive under `dist/`. Signing and notarization require the release owner's configured Apple credentials. Do not commit those credentials.

This repository split has not published a new binary or registry release. The standalone Homebrew formula and Claude Desktop bundle remain release inputs until the integrated gates pass. App casks and the deprecated CLI cask stay in the app repository.

Release artifacts are checked by `scripts/scan_release_secrets.py`, using the shared detection rules in `scripts/secret_patterns.py`.

## Publish the Python package

[Publish Python package](../.github/workflows/publish-pypi.yml) runs the backend tests, builds the wheel and source distribution, checks their metadata, scans both archives for secrets with Gitleaks and the release scanner, and installs the wheel in a fresh environment outside the checkout. The smoke check verifies the installed C, Objective-C and Swift source resources without compiling them, reads CLI help and performs a keyless MCP initialization and tool listing. It does not start a phone, simulator or model.

A pushed `cli-v*` tag starts validation and publishes after every check passes. The workflow first checks that the tag is exactly `cli-v` followed by `mobile_agent.__version__`. The tagged commit must contain this workflow; tag-triggered publication does not require merging it to the default branch. See [GitHub's tag-push trigger](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#push).

A successful validation uploads the `python-distributions` artifact containing `mobster_cli-<version>-py3-none-any.whl` and `mobster_cli-<version>.tar.gz`.

Before the first publication, sign in to PyPI and add a [pending trusted publisher](https://pypi.org/manage/account/publishing/) with these exact fields:

| Field | Value |
| --- | --- |
| PyPI project name | `mobster-cli` |
| Owner | `RadishSoftware` |
| Repository name | `mobster` |
| Workflow name | `publish-pypi.yml` |
| Environment name | `pypi` |

The GitHub repository must have an environment named `pypi`. No PyPI API token or repository secret is needed. PyPI creates the project when the configured publisher first uploads a release. See [PyPI's trusted-publishing instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

For the initial `0.4.3` release, check out the reviewed release commit, then create and push its release tag:

```sh
git tag -a cli-v0.4.3 -m "Release mobster-cli 0.4.3"
git push origin cli-v0.4.3
```

Pushing the tag starts publication. The workflow validates the tagged source and publishes only that run's artifact from a separate Ubuntu job with `id-token: write`. The publishing job does not check out or run repository code. An existing PyPI version cannot be overwritten.

Manual runs default to `publish=false`; set `publish=true` to publish after validation. Unlike a tag push, `workflow_dispatch` requires this workflow file on the repository's default branch. See [GitHub's manual-trigger requirements](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch).

The frozen macOS archive, Homebrew formula and Claude Desktop bundle use their own release checks; this workflow publishes only the Python wheel and source distribution.
