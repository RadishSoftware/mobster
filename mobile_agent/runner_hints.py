"""What the USB helper's failures say, in plain words: xcodebuild errors, runner log causes and automation prompts."""

import datetime
import re

# xcodebuild failures users can act on, in their words.
BUILD_HINTS = (
    ("isn't registered in your developer account",
     "This iPhone isn't registered with your Apple team. Open Xcode › Settings › Accounts, select the team, "
     "and let Xcode register the device (or register it in the Apple Developer portal)."),
    ("No Accounts", "Sign in to Xcode with your Apple ID: Xcode › Settings › Accounts."),
    ("No signing certificate", "Xcode has no signing certificate for this team yet. Open Xcode › Settings › Accounts "
                               "and choose Manage Certificates › Apple Development."),
    ("Developer Mode",
     "Turn on Developer Mode on the iPhone: Settings › Privacy & Security › Developer Mode, then restart it."),
    ("is not available", "The iPhone isn't available to Xcode. Unlock it, keep it connected, and trust this Mac."),
    ("requires a development team", "Choose your Apple development team and build again."),
    ("requires Xcode, but active developer directory",
     "Mobster couldn't find Xcode. Install it from the Mac App Store and open it once, then build again."),
    ("agreed to the Xcode", "Accept the Xcode license: open Xcode and agree, then build again."),
    ("invalid active developer path", "Install Xcode from the Mac App Store and open it once, then build again."),
    ("Could not resolve host",
     "Mobster couldn't download WebDriverAgent. Check your internet connection and build again."),
    ("Unable to find a destination", "Xcode can't use your iPhone yet. Unlock it, keep it connected and turn on "
                                     "Developer Mode. If iOS is newer than your Xcode, update Xcode."),
)
EXPOSED_HINT = ("Mobster stopped its helper on your iPhone because it answered on the phone's network address, "
                "where anyone on that network could reach it. Build it again, then start it.")
# A free Apple account's signature lasts 7 days; after that iOS refuses to launch the runner.
EXPIRED_HINT = "Mobster's helper needs a refresh: its signature expired. Refresh it in Setup."
# A runner held back because the phone is locked (DeviceManager.runner_waiting): it launches when the phone is unlocked.
LOCKED_START_HINT = "Unlock your iPhone. Mobster's helper starts as soon as it is unlocked."
# Why a started runner is not answering yet, read from the tail of its log.
RUNNER_HINTS = (
    ("because the device is locked", "locked", LOCKED_START_HINT),
    # iOS asks for the passcode to let UI automation start unless Settings › Developer › Enable UI Automation is on;
    # nobody answering it fails the start after 60 s.
    ("Timed out while enabling automation mode", "ui_automation_off",
     "On your iPhone, turn on Settings › Developer › Enable UI Automation, and keep it unlocked while Mobster's helper "
     "starts."),
    ("Developer Mode", "developer_mode_off",
     "Turn on Developer Mode on the iPhone: Settings › Privacy & Security › Developer Mode."),
    ("has not been explicitly trusted", "trust_needed",
     "On the iPhone, open Settings › General › VPN & Device Management and trust your developer app."),
    ("invalid code signature", "trust_needed",
     "On the iPhone, open Settings › General › VPN & Device Management and trust your developer app."),
    ("is not available", None,
     "The iPhone isn't available to Xcode. Keep it connected, unlocked and trusting this Mac."),
)

# The runner says "Running tests..." and, once iOS has turned on automation mode, "Test Suite 'All tests' started"
# about 2 s later (Sam's iPhone 16, iOS 18.3.1, 17 Oct 2026: 1.9 s). With Settings › Developer › Enable UI
# Automation off, iOS waits for the passcode instead and the start fails after 60 s. Waiting this long already names
# the cause, so the person can act during the first launch instead of after two 60 s timeouts.
AUTOMATION_WAIT_S = 8.0
AUTOMATION_WAITING = "Mobster: iOS hasn't enabled automation mode for the helper yet"
AUTOMATION_WAIT_HINT = ("On your iPhone, turn on Settings › Developer › Enable UI Automation. If it asks for its "
                        "passcode, enter it. Mobster's helper starts by itself after that.")
# The pre-arm (L13): iOS 17 and later wait for the passcode before a first UI-test session unless Settings › Developer
# › Enable UI Automation is on, and no API on the Mac reads that switch (CoreDevice's details, DDI services and lockdown
# say nothing of it; checked 17 Oct 2026, iOS 18.3.1). So for a phone whose helper has never answered, Mobster asks
# for the switch while the helper installs, in the build's ~50 s, so the first launch passes instead of timing out.
PREARM_MIN_IOS = 17
PREARM_ASK = ("On your iPhone, open Settings › Developer and turn on Enable UI Automation, so Mobster's helper "
              "starts on the first try.")
RUNNER_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+[+-]\d{4}) WebDriverAgentRunner-Runner\[")


def runner_stamp(line):
    """The epoch seconds a runner log line was written (its own "2026-10-17 19:47:46.812939-0700" prefix), or None."""
    match = RUNNER_STAMP.match(line)
    if match is None:
        return None
    try:
        return datetime.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S.%f%z").timestamp()
    except ValueError:
        return None


def build_hint(lines):
    text = "\n".join(lines)
    for needle, hint in BUILD_HINTS:
        if needle in text:
            return hint
    errors = [line.strip() for line in lines if "error:" in line]
    return errors[-1][:300] if errors else "The build failed. See the build log for details."
