# Third-party notices

Mobster CLI's own code is MIT-licensed, copyright Radish Retail, LLC (see [LICENSE](https://github.com/RadishSoftware/mobster/blob/main/LICENSE)). This file lists the third-party software and content that Mobster CLI installs, runs or includes, and under which licence. It was gathered on 2026-09-25 from `mobile_agent/requirements.txt` and `pyproject.toml`.

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
| textual | 8.2.8 | MIT | the terminal UI (`tui/`), imported only when it opens |
| rich | 15.0.0 | MIT | Textual dependency; the UI's text rendering |
| markdown-it-py | 4.2.0 | MIT | Textual dependency |
| mdit-py-plugins | 0.6.1 | MIT | Textual dependency |
| mdurl | 0.1.2 | MIT | markdown-it-py dependency |
| platformdirs | 4.12.0 | MIT | Textual dependency |
| Pygments | 2.21.0 | BSD-2-Clause | Rich dependency |
| textual-image | 0.14.1 | LGPL-3.0-or-later | draws the phone's screenshot inside the terminal UI (kitty graphics protocol or sixel); installed from PyPI and imported unchanged, only when the UI opens in a terminal that shows images |
| Pillow | 12.3.0 | MIT-CMU (HPND) | FrameClock JPEG decode, image crops. Its macOS wheels bundle libjpeg-turbo, libtiff, libwebp, libavif, lcms2, OpenJPEG, libpng, FreeType, HarfBuzz, Brotli, xz, libxcb, libXau and zlib-ng, each under its own licence, all listed in Pillow's `LICENSE` file. |
| PyYAML | 6.0.3 | MIT | check files for `mobster verify --check` (`verify/checks.py`). Its macOS wheels build LibYAML 0.2.5 (MIT) into the `_yaml` extension. |

The Python interpreter (3.12+) is yours; the framework does not ship one.

### External tools the framework runs

These are separate programs that you install. Mobster starts them as child processes; it does not bundle, link to, or modify them, so their licences place no conditions on Mobster's code. The one exception, WebDriverAgent, which Mobster patches, has its own section below.

| tool | licence | how Mobster uses it |
| --- | --- | --- |
| [libimobiledevice](https://libimobiledevice.org/) and libusbmuxd | LGPL-2.1-or-later (libraries; see each project for its command-line tools) | `idevice_id`, `ideviceinfo` and `iproxy`, installed with `brew install libimobiledevice`, are invoked as external commands to find the phone and relay ports 8100 and 9100 over USB. |
| [ideviceinstaller](https://github.com/libimobiledevice/ideviceinstaller) | GPL-2.0-or-later | `brew install ideviceinstaller`; invoked as `ideviceinstaller list` to read which apps are installed (never to install or remove one) |
| Xcode (`xcodebuild`, `swiftc`, `xcrun`) | Apple's Xcode licence | builds WebDriverAgent and the optional Swift helpers (`ocr.swift`, `vision_t0.swift`, `video/`) |
| `git` | GPL-2.0 | fetches WebDriverAgent |

### WebDriverAgent (modified by Mobster)

[WebDriverAgent](https://github.com/appium/WebDriverAgent) is the runner Mobster puts on the iPhone (and on its simulators) to read the screen and to tap, type and swipe. The upstream project is `appium/WebDriverAgent`, maintained by the Appium project and created by Facebook, Inc. It is licensed under the BSD-3-Clause licence, copyright (c) 2015-present, Facebook, Inc.; the full text is below. Mobster isn't affiliated with or endorsed by Facebook, Meta or the Appium project.

Mobster does not ship WebDriverAgent: this repository, the release tarball and the Mac app hold none of its source or binaries. At setup, `serve --manage-device` and the Mac app clone the pinned release, `appium/WebDriverAgent` tag `v16.12.10` (commit `00c38220c3e84906c965b996ffc4c12d09fef62f`, set in [`mobile_agent/wda_source.py`](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/wda_source.py)), into your data folder and build it there with your own Xcode and Apple team. `mobster sim` builds the same pinned source, unsigned, for the simulators it creates.

**Mobster modifies the copy it builds.** Both changes are in [`mobile_agent/device_manager.py`](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/device_manager.py) and are applied to the clone before every build:

- **Source patches** (`WDA_PATCHES`, applied by `patch_wda()`) edit three upstream files. `WebDriverAgentLib/Routing/FBWebServer.m` makes the MJPEG video socket listen on the address the API uses (`USE_IP`) instead of every interface. `WebDriverAgentLib/Routing/FBHTTPServer.m` and `WebDriverAgentLib/Utilities/FBMjpegServer.m` refuse a request that has an `Origin` header or a `Host` other than `127.0.0.1`, `localhost` or `[::1]`, so a web page can't reach the phone through the USB relay. If upstream's source doesn't fit the patches, the build fails instead of running unpatched.
- **Bundle identifiers** (`BUNDLE_REBRAND`, applied by `_rebrand()`) in `WebDriverAgent.xcodeproj/project.pbxproj`: `com.facebook.WebDriverAgentRunner` becomes `app.mobster.wda.runner` (with your team's suffix) and `com.facebook.WebDriverAgentLib` becomes `app.mobster.wda.lib`.

Mobster also builds it with `USE_IP=127.0.0.1`, the setting WebDriverAgent reads to choose the address it listens on, and `pin_loopback()` writes the same value into test plans built earlier. `scripts/usb-wda.sh` runs a copy you built yourself, and the manual recipe in [`mobile_agent/docs/usb-wda.md`](https://github.com/RadishSoftware/mobster/blob/main/mobile_agent/docs/usb-wda.md) doesn't apply the source patches.

WebDriverAgent's licence, copied unchanged from its `LICENSE` file at `v16.12.10`:

```text
BSD License

For WebDriverAgent software

Copyright (c) 2015-present, Facebook, Inc. All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

 * Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

 * Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

 * Neither the name Facebook nor the names of its contributors may be used to
   endorse or promote products derived from this software without specific
   prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### Benchmark harness and data

| item | licence | notes |
| --- | --- | --- |
| [iOSWorld](https://github.com/ljang0/iOSWorld) (arXiv:2606.09764) | CC BY 4.0 | `bench/iosworld.py` runs iOSWorld's tasks, reset and judge from your own checkout of iOSWorld (`--repo`); the task goals are read from it at run time. This repository quotes a few task goals and app names as test fixtures and code comments (for example `tests/test_milestones.py`, `tests/test_requested_actions.py`), and the documentation cites task ids and published results. Credit: iOSWorld by its authors, licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/); the quoted goals are unchanged. |
| VisionJudge evaluation images (`mobile_agent/evals/vision/images/`) | CC0 and public domain | 512-px re-encodes of Wikimedia Commons images. Each file's source page, author and licence are recorded in `evals/vision/manifest.json`. |
| MobsterBench-iOS (`mobile_agent/bench/`) | MIT (this project) | Categories mirror published suites (AndroidLab, AndroidWorld, SPA-Bench, MobileAgentBench, WebArena and others) by design; no task text or data is copied from them. |

Model providers (TypeSafe Jev, and whichever helper provider you configure) are online services used under their own terms, not software distributed with Mobster.

## Keeping this file current

Regenerate the inventory with `pip-licenses --with-license-file --from=mixed` in the venv built from `mobile_agent/requirements.txt`.
