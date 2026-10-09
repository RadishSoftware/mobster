"""The setup failures people actually hit, as one line to act on and one button.

xcodebuild, devicectl and libimobiledevice explain a failure in a paragraph written
for Xcode engineers. Setup shows ``fix`` (what to do, in the user's words) and a
button for ``action`` instead, and keeps the tool's own text as ``raw`` under
"Details". Text nothing here recognizes is shown as it came (``translate`` returns
None and the caller falls back to the raw text).

Actions the dashboard knows how to render as a button:

- ``choose_team``: focus the Apple team picker on the build step.
- ``check_again``: re-read the phone, tools and teams (POST /api/setup/refresh).
- ``retry``: run the step that failed again (build, or start the runner).
- ``copy_command``: copy ``command`` to paste into Terminal.
"""

import re

# The failures, most specific first: a destination error can mention Developer Mode, so
# Developer Mode is tested before the generic "not available" wordings, and an expired
# Xcode sign-in is told apart from a missing team before "Pick your Apple team".
RULES = (
    # iOS caps apps signed by a free Apple ID at 3 on the phone. Waiting never frees a slot; deleting one does.
    ("free_app_limit", "Delete an app you installed with Xcode from your iPhone. A free Apple Account allows 3",
     "retry",
     (r"maximum number of apps for free development", r"maximum number of installed apps using a free developer",
      r"installed apps using a free (developer|development) profile")),
    ("app_id_limit", "Your free Apple Account can make 10 app IDs a week. Wait a few days, or use a paid developer team",
     "choose_team",
     (r"maximum (number of )?app ?ids?", r"app id limit", r"may create up to \d+ app ids")),
    ("bundle_conflict", "The helper's app ID is taken by another team. Install again to give it one of your own",
     "retry",
     (r"cannot be registered to your development team.*not available", r"failed registering bundle identifier")),
    # The team is chosen, but Xcode's sign-in for it lapsed: the likeliest way a renewal fails days later.
    ("xcode_signin", "Sign in again in Xcode › Settings › Accounts", "check_again",
     (r"no account for team", r"no accounts? (with|for)\b", r"no accounts?: add a new account",
      r"unable to log in with account", r"login details for account .* were rejected",
      r"your session has expired")),
    ("no_team", "Choose your Apple Account", "choose_team",
     (r"requires a development team", r"no signing certificate", r"signing certificate .* not found",
      r"no team (was )?selected")),
    ("developer_mode", "Turn on Developer Mode: on your iPhone open Settings › Privacy & Security › Developer Mode",
     "check_again",
     (r"developer mode (is )?(disabled|off|not enabled)", r"enable developer mode",
      r"developermodestatus.*false")),
    ("untrusted_developer", "Trust Mobster's helper on your iPhone: open Settings › General › VPN & Device "
                            "Management", "retry",
     (r"not been explicitly trusted", r"developer app certificate is not trusted", r"untrusted developer",
      r"invalid code signature", r"verify the developer app")),
    ("locked", "Unlock your iPhone and keep it unlocked, then try again", "retry",
     (r"device (is|was) locked", r"because the device is locked", r"could not,? be,? unlocked",
      r"password protected \(-17\)", r"passcode (protected|is required)", r"unlock (your|the) (device|iphone)")),
    ("not_trusted", "Unlock your iPhone and tap Trust, then enter your passcode", "check_again",
     (r"pairing dialog response pending", r"user denied pairing", r"trust this computer",
      r"has not trusted this (computer|mac)", r"not trusted by (the )?(device|iphone)")),
    ("not_paired", "Unplug your iPhone, plug it back in and tap Trust", "check_again",
     (r"invalid hostid", r"not paired", r"no pairing record", r"pairing (record )?(is )?(missing|invalid)",
      r"could not (connect to lockdownd|pair)", r"lockdownd.*(-21|-3)\b")),
    ("xcode_components", "Let Xcode finish installing its components", "copy_command",
     (r"-runfirstlaunch", r"is not installed\. to use with xcode", r"download and install the platform",
      r"required plugin failed to load", r"coresimulator is out of date", r"requires a newer version of xcode",
      r"no ios (platform|sdk)", r"platform .* (is )?not installed")),
    ("disk_full", "Free up space on your Mac, then try again", "retry",
     (r"no space left on device", r"disk (is )?full", r"not enough (free )?(disk )?space", r"errno 28",
      r"out of disk space")),
)
COMMANDS = {"xcode_components": "sudo xcodebuild -runFirstLaunch && xcodebuild -downloadPlatform iOS"}
PATTERNS = tuple((kind, fix, action, re.compile("|".join(patterns), re.I)) for kind, fix, action, patterns in RULES)
RAW_LIMIT = 4000


def translate(text):
    """{"kind", "fix", "action", "raw"[, "command"]} for a failure people can act on, else None."""
    if not isinstance(text, str) or not text.strip():
        return None
    for kind, fix, action, pattern in PATTERNS:
        if pattern.search(text):
            problem = {"kind": kind, "fix": fix, "action": action, "raw": raw_text(text)}
            if kind in COMMANDS:
                problem["command"] = COMMANDS[kind]
            return problem
    return None


def raw_text(text):
    """The tool's own words, trimmed to the lines that say what failed (for "Details")."""
    lines = [line.rstrip() for line in str(text).splitlines() if line.strip()]
    errors = [line for line in lines if re.search(r"\berror\b|failed|ERROR", line)]
    kept = errors[-12:] if errors else lines[-12:]
    return "\n".join(kept)[-RAW_LIMIT:]


def problem(text, fallback=None):
    """translate(text), or the raw text with no fix (``fix`` None) when nothing matches."""
    return translate(text) or ({"kind": "unknown", "fix": None, "action": None, "raw": raw_text(text)}
                               if isinstance(text, str) and text.strip() else fallback)
