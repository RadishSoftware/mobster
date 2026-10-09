# Releasing Mobster CLI

How a release of the `mobster` command is made, from version bump to Homebrew. The maintainers run it, because the official build is signed with Radish Retail's Developer ID and notarized by Apple. You can build an unsigned copy yourself; step 2 says how.

## What must be true first

- `make test-backend` passes on the commit being released.
- `CHANGELOG.md` has a `## X.Y.Z - YYYY-MM-DD` section (the Unreleased list, renamed) with credit for outside contributions ("Thanks @name (#123)"). It is the release's notes.
- `docs/changelog.mdx` has an update block for what users notice.
- The version is the same in all three places that carry it: `mobile_agent/__init__.py` (`__version__`; `pyproject.toml` reads it), `plugins/mobster/.claude-plugin/plugin.json` and `packaging/mcpb/manifest.json`.

Versions follow SemVer and are 0.x for now. Tags are `vX.Y.Z`. A prerelease is `vX.Y.Z-rc.N`, and the installer never picks one, because it downloads from `releases/latest`.

## 1. Bump the version

One pull request named "Release X.Y.Z" makes the changes above. Once it merges, its commit is the release.

## 2. Build

On an Apple silicon Mac:

```sh
uv venv --seed --python 3.12.13 mobile_agent/.venv
packaging/cli/build.sh
```

It freezes the command with PyInstaller and writes `dist/mobster-macos-arm64.tar.gz` and its `.sha256` (checked by `install.sh`). With `APPLE_SIGNING_IDENTITY` set to a "Developer ID Application" identity, every binary is signed with the hardened runtime, and with `APPLE_API_ISSUER`, `APPLE_API_KEY_PATH` and `APPLE_API_KEY_ID` set, `packaging/cli/notarize.sh` notarizes the build and fails it unless Apple accepts it. Without them, the build is signed ad hoc, which runs on the Mac that built it.

Today `build.sh` also reads two inputs kept with the Mac app's build (the entitlements file and the pinned PyInstaller requirements), so the official build runs in the source repository.

## 3. Export the tree

The maintainers export the release commit to this repository as one new commit on `main`, `Mobster X.Y.Z: the public tree cut from <sha>`, with `Cut-From:` and `Mobster-Version:` trailers. `SOURCE_REV` names the same source commit. The export is a plain push, never a force push, and it refuses to run while a pull request merged here is missing from the source (see [CONTRIBUTING.md](https://github.com/RadishSoftware/mobster/blob/main/CONTRIBUTING.md#how-a-pull-request-lands)).

## 4. Tag and publish the release

```sh
git tag -a vX.Y.Z -m "Mobster X.Y.Z" <the export commit> && git push origin vX.Y.Z
gh release create vX.Y.Z -R RadishSoftware/mobster --verify-tag --draft --title "Mobster X.Y.Z" \
  --notes-file notes.md dist/mobster-macos-arm64.tar.gz dist/mobster-macos-arm64.tar.gz.sha256
```

`notes.md` is the version's `CHANGELOG.md` section. Before publishing the draft, install from the local build the way a user would:

```sh
MOBSTER_INSTALL_BASE_URL=file://$PWD/dist MOBSTER_HOME=$(mktemp -d) MOBSTER_BIN_DIR=$(mktemp -d) sh packaging/install.sh
```

Then `gh release edit vX.Y.Z -R RadishSoftware/mobster --draft=false`. `install.sh` downloads from `releases/latest/download/`, so the new release is what it installs from then on.

## 5. Update the Homebrew tap

The tap is [RadishSoftware/homebrew-tap](https://github.com/RadishSoftware/homebrew-tap), so users run `brew install radishsoftware/tap/mobster`. Render the cask from the release's checksum, check it, and commit it to the tap as `Casks/mobster.rb`:

```sh
python3 packaging/homebrew/render.py --cask --version X.Y.Z --sha256 dist/mobster-macos-arm64.tar.gz.sha256 --out Casks/mobster.rb
brew style --cask Casks/mobster.rb
brew audit --cask --strict --online radishsoftware/tap/mobster
```

## 6. Check it as a new user

On a Mac account that has never had Mobster:

```sh
curl -fsSL https://mobster.dev/install.sh | sh
brew install radishsoftware/tap/mobster
mobster version
mobster mcp install --all
mobster mcp doctor
```

Then post the CHANGELOG section as an announcement in Discussions.
