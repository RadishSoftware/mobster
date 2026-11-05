# Standalone CLI packaging

The CLI build owns its entry point, freezer pins and signing entitlements in `packaging/cli/`. It requires no desktop checkout.

```sh
make venv
packaging/cli/build.sh
```

The build freezes the command, performs smoke checks and writes the macOS arm64 archive under `dist/`. Signing and notarization require the release owner's configured Apple credentials. Do not commit those credentials.

This repository split has not published a new binary or registry release. The standalone Homebrew formula and Claude Desktop bundle remain release inputs until the integrated gates pass. App casks and the deprecated CLI cask stay in the app repository.

Release artifacts are checked by `scripts/scan_release_secrets.py`, using the shared detection rules in `scripts/secret_patterns.py`.
