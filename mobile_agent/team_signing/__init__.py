"""Pro without Xcode, for a paid Apple Developer team (PRO-WITHOUT-XCODE.md, 18 Oct 2026).

Mobster signs its helper (the WebDriverAgent runner) on this Mac and installs it with open-source device tooling,
so a phone-station Mac needs no Xcode:

- ``writes``: the three App Store Connect writes (register an iPhone, create a development certificate, create a
  development profile) as a card the person approves, with the ledger (``ledger``) that makes each attempt durable.
- ``identity``: the certificate signing request and the signing key, kept in a Keychain only Mobster opens.
- ``resign``: ``codesign`` of the prebuilt runner with the team's profile, and the check that it verifies.
- ``tooling``: install and launch the signed runner with pymobiledevice3 (go-ios as the fallback), never ``xcodebuild``.
- ``bundle``: Path C, a signed runner and its profile exported from one Mac and imported on another.

Off unless ``MOBSTER_EXPERIMENTAL_NO_XCODE=1``. Free Apple Accounts keep today's Xcode flow. Nothing here uses
Apple's private sign-in endpoints or changes how the phone looks to an app.
"""

import os
from pathlib import Path

FLAG = "MOBSTER_EXPERIMENTAL_NO_XCODE"


def enabled(environ=None):
    """Whether signing without Xcode is on (``MOBSTER_EXPERIMENTAL_NO_XCODE=1``)."""
    environ = os.environ if environ is None else environ
    return str(environ.get(FLAG, "")).strip().casefold() in ("1", "true", "yes", "on")


def state_dir(data_dir=None):
    """Where this feature keeps its files (the ledger, the certificate, the signed runners): a folder only you can
    open, in Mobster's data folder."""
    from .. import env_file, paths

    return env_file.private_dir(Path(data_dir) if data_dir is not None else paths.user_data_dir()) / "team-signing"
