# Standalone CLI packaging

The CLI build owns its entry point, freezer pins and signing entitlements in `packaging/cli/`. It requires no desktop checkout.

```sh
make venv
packaging/cli/build.sh
```

The build freezes the command, performs smoke checks and writes the macOS arm64 archive under `dist/`. Signing and notarization require the release owner's configured Apple credentials. Do not commit those credentials.

The standalone Python package is published on [PyPI](https://pypi.org/project/mobster-cli/0.4.3/); no new frozen binary has been published. The standalone Homebrew formula and Claude Desktop bundle remain release inputs until the integrated gates pass. App casks and the deprecated CLI cask stay in the app repository.

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

Release `0.4.3` is published from the immutable `cli-v0.4.3` tag. For a future version, update the version and package pins, check out the reviewed release commit, then create and push the matching tag:

```sh
git tag -a cli-v<version> -m "Release mobster-cli <version>"
git push origin cli-v<version>
```

Pushing the tag starts publication. The workflow validates the tagged source and publishes only that run's artifact from a separate Ubuntu job with `id-token: write`. The publishing job does not check out or run repository code. An existing PyPI version cannot be overwritten.

Manual runs default to `publish=false`; set `publish=true` to publish after validation. Unlike a tag push, `workflow_dispatch` requires this workflow file on the repository's default branch. See [GitHub's manual-trigger requirements](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch).

The frozen macOS archive, Homebrew formula and Claude Desktop bundle use their own release checks; this workflow publishes only the Python wheel and source distribution.

## Publish the MCP Registry metadata

[`server.json`](../server.json) records the published `dev.mobster/mobster` entry. Its PyPI command uses the `mobster-cli` executable alias, which starts the same CLI as `mobster`. Verify the package installation and MCP handshake before publishing metadata with `mcp-publisher publish server.json`.

Authenticate as the domain owner using the official registry's HTTP or DNS proof. Keep signing keys and publisher tokens outside this repository. Published registry versions are immutable; adding another package requires a new registry version. Do not advertise an npm package until that exact version is published and checked.
