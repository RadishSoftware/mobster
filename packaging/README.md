# Standalone CLI packaging

The CLI build owns its entry point, freezer pins and signing entitlements in `packaging/cli/`. It requires no desktop checkout.

```sh
make venv
packaging/cli/build.sh
```

The build freezes the command, performs smoke checks and writes the macOS arm64 archive under `dist/`. Signing and notarization require the release owner's configured Apple credentials. Do not commit those credentials.

This repository split has not published a new binary or registry release. The Homebrew templates and Claude Desktop bundle remain release inputs until the integrated gates pass.

The release scanner currently shares secret-detection helpers with `scripts/check_public_tree.py`. That file retains the legacy engine-export policy; its export validation command is not the release path for this independent repository. The old cut and publish scripts are not included.
