# Third-party notices

Mobster CLI's own code is MIT-licensed, copyright Radish Retail, LLC (see [LICENSE](https://github.com/uninstantiated/mobster-cli/blob/main/LICENSE)). This file lists the third-party software and content that Mobster CLI installs, runs or includes, and under which licence. It was gathered on 2026-09-25 from `mobile_agent/requirements.txt` and `pyproject.toml`.

It is an inventory, not the full licence texts: see [Keeping this file current](#keeping-this-file-current).

## The framework (`mobile_agent`, the `mobster-cli` Python package)

### Python dependencies

Installed by pip from PyPI; none is copied into this repository. Versions are the tested pins in `mobile_agent/requirements.txt`.

| package | version | licence | why |
| --- | --- | --- | --- |
| jsonschema | 4.25.1 | MIT | output-schema validation |
| referencing | 0.37.0 | MIT | schema reference handling (via jsonschema, and imported directly) |
| jsonschema-specifications | 2025.9.1 | MIT | jsonschema dependency |
| attrs | 26.1.0 | MIT | jsonschema dependency |
| rpds-py | 2026.6.3 | MIT | jsonschema dependency |
| typing_extensions | 4.16.0 | PSF-2.0 | referencing dependency |
| croniter | 6.2.4 | MIT | saved-task schedules |
| python-dateutil | 2.9.0.post0 | Apache-2.0 or BSD-3-Clause (dual) | croniter dependency |
| six | 1.17.0 | MIT | python-dateutil dependency |
| Pillow | 12.3.0 | MIT-CMU (HPND) | FrameClock JPEG decode, image crops. Its macOS wheels bundle libjpeg-turbo, libtiff, libwebp, libavif, lcms2, OpenJPEG, libpng, FreeType, HarfBuzz, Brotli, xz, libxcb, libXau and zlib-ng, each under its own licence, all listed in Pillow's `LICENSE` file. |

The Python interpreter (3.12+) is yours; the framework does not ship one.

### External tools the framework runs

These are separate programs that you install. Mobster starts them as child processes; it does not bundle, link to, or modify them, so their licences place no conditions on Mobster's code.

| tool | licence | how Mobster uses it |
| --- | --- | --- |
| [WebDriverAgent](https://github.com/appium/WebDriverAgent) (Appium; originally Facebook) | BSD-3-Clause | The on-phone runner. `serve --manage-device` clones it from GitHub into your data folder at setup time and builds it with your own Xcode and Apple team; `scripts/usb-wda.sh` runs a copy you built. Mobster talks to it over HTTP. |
| [libimobiledevice](https://libimobiledevice.org/) and libusbmuxd | LGPL-2.1-or-later (libraries; see each project for its command-line tools) | `idevice_id`, `ideviceinfo` and `iproxy`, installed with `brew install libimobiledevice`, are invoked as external commands to find the phone and relay ports 8100 and 9100 over USB. |
| Xcode (`xcodebuild`, `swiftc`, `xcrun`) | Apple's Xcode licence | builds WebDriverAgent and the optional Swift helpers (`ocr.swift`, `vision_t0.swift`, `video/`) |
| `git` | GPL-2.0 | fetches WebDriverAgent |

### Benchmark harness and data

| item | licence | notes |
| --- | --- | --- |
| [iOSWorld](https://github.com/ljang0/iOSWorld) (arXiv:2606.09764) | CC BY 4.0 | `bench/iosworld.py` runs iOSWorld's tasks, reset and judge from your own checkout of iOSWorld (`--repo`); the task goals are read from it at run time. This repository quotes a few task goals and app names as test fixtures and code comments (for example `tests/test_milestones.py`, `tests/test_requested_actions.py`), and the documentation cites task ids and published results. Credit: iOSWorld by its authors, licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/); the quoted goals are unchanged. |
| VisionJudge evaluation images (`mobile_agent/evals/vision/images/`) | CC0 and public domain | 512-px re-encodes of Wikimedia Commons images. Each file's source page, author and licence are recorded in `evals/vision/manifest.json`. |
| MobsterBench-iOS (`mobile_agent/bench/`) | MIT (this project) | Categories mirror published suites (AndroidLab, AndroidWorld, SPA-Bench, MobileAgentBench, WebArena and others) by design; no task text or data is copied from them. |

Model providers (TypeSafe Jev, and whichever helper provider you configure) are online services used under their own terms, not software distributed with Mobster.

## Keeping this file current

Regenerate the inventory with `pip-licenses --with-license-file --from=mixed` in the venv built from `mobile_agent/requirements.txt`.
