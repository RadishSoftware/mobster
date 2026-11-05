"""Signing facts Setup reads, and only reads, from this Mac: the Apple teams it can build with, and profile expiry."""

import hashlib
import os
import plistlib
import re
import subprocess
from datetime import timezone
from pathlib import Path

TEAM_ID = re.compile(r"[A-Z0-9]{10}")
XCODE_DEFAULTS = "com.apple.dt.Xcode"
# Xcode 14 and later keep signed-in accounts' teams under the first key; older versions the second.
TEAM_KEYS = ("IDEProvisioningTeamByIdentifier", "IDEProvisioningTeams")
PROFILE_GLOB = "Build/Products/*-iphoneos/WebDriverAgentRunner-Runner.app/embedded.mobileprovision"


def _read(command, timeout=10, text=True, input=None):
    try:
        completed = subprocess.run(command, capture_output=True, text=text, timeout=timeout, input=input)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def xcode_defaults():
    """Xcode's preferences as a dict ({} when Xcode has never been opened)."""
    data = _read(["/usr/bin/defaults", "export", XCODE_DEFAULTS, "-"], text=False)
    if not data:
        try:
            data = (Path.home() / "Library" / "Preferences" / f"{XCODE_DEFAULTS}.plist").read_bytes()
        except OSError:
            return {}
    try:
        value = plistlib.loads(data)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


# Signing in is enough: a free Apple ID's Personal Team is listed before it has a certificate, which the build creates.
def account_teams(defaults=None):
    defaults = xcode_defaults() if defaults is None else defaults
    teams = []
    for key in TEAM_KEYS:
        accounts = defaults.get(key)
        for entries in accounts.values() if isinstance(accounts, dict) else ():
            for entry in entries if isinstance(entries, list) else ():
                if not isinstance(entry, dict):
                    continue
                team, name = entry.get("teamID"), entry.get("teamName")
                if not isinstance(team, str) or not TEAM_ID.fullmatch(team) or any(t["id"] == team for t in teams):
                    continue
                personal = entry.get("isFreeProvisioningTeam") is True or entry.get("teamType") == "Personal Team"
                teams.append({"id": team, "name": name.strip()[:80] if isinstance(name, str) and name.strip() else None,
                              "personal": personal})
    return teams


# In an Apple Development certificate OU is the team id and O its name; the id in parentheses in CN is not the team id.
def subject_team(subject):
    fields = dict(re.findall(r"^\s*(\w+)\s*=\s*(.*?)\s*$", subject or "", re.M))
    team = fields.get("organizationalUnitName")
    if team is None:  # the one-line format: "subject=UID=…, CN=…, OU=ABCDE12345, O=…"
        match = re.search(r"\bOU\s*=\s*([A-Z0-9]{10})\b", subject or "")
        team = match[1] if match else None
    if not team or not TEAM_ID.fullmatch(team):
        return None, None
    name = fields.get("organizationName")
    return team, name[:80] if name else None


def certificate_teams():
    """Teams with an Apple Development certificate in the login keychain."""
    pem = _read(["security", "find-certificate", "-a", "-c", "Apple Development", "-p"]) or ""
    teams = []
    for block in re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", pem, re.S):
        subject = _read(["openssl", "x509", "-noout", "-subject", "-nameopt", "multiline,-esc_msb,utf8"],
                        timeout=5, input=block)
        team, name = subject_team(subject)
        if team and not any(t["id"] == team for t in teams):
            teams.append({"id": team, "name": name, "personal": False})
    return teams


def merge_teams(*sources):
    """One entry per team id; the first source to name a team wins, later ones fill a missing name."""
    merged = []
    for source in sources:
        for team in source:
            known = next((t for t in merged if t["id"] == team["id"]), None)
            if known is None:
                merged.append(dict(team))
            else:
                known["name"] = known["name"] or team.get("name")
                known["personal"] = known["personal"] or team.get("personal", False)
    return merged


def profile_expiry(derived):
    """When the built runner's provisioning profile expires (epoch seconds), or None if unreadable."""
    for profile in sorted(Path(derived).glob(PROFILE_GLOB)):
        text = _read(["security", "cms", "-D", "-i", str(profile)])
        try:
            expires = plistlib.loads(text.encode()).get("ExpirationDate") if text else None
        except Exception:
            expires = None
        if expires is not None and hasattr(expires, "timestamp"):
            # plistlib returns naive datetimes in UTC.
            return (expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)).timestamp()
    return None


# A runner whose signature ends within this long is renewed (runner_renewal.py), and a build in this
# window first drops Xcode's cached profile so the new signature starts a fresh 7 days.
RENEW_WINDOW = 48 * 3600
# To prove a renewal on a real phone in one sitting, MOBSTER_RENEW_WINDOW_HOURS=170 (in the agent's
# env file, e.g. the Mac app's agent.env) widens the window past a fresh 7-day signature, so the
# runner renews within seconds of Mobster opening. Remove it afterwards: a signature is never older
# than 168 hours, so with it set the runner is always due (retried at most every 6 hours).
MAX_RENEW_WINDOW_HOURS = 170


def renew_window(env=None):
    """RENEW_WINDOW, or MOBSTER_RENEW_WINDOW_HOURS (1 to 170) when set for a verification run."""
    value = (os.environ if env is None else env).get("MOBSTER_RENEW_WINDOW_HOURS")
    try:
        hours = float(value) if value else None
    except ValueError:
        hours = None
    if hours is None or not 1 <= hours <= MAX_RENEW_WINDOW_HOURS:
        return RENEW_WINDOW
    return int(hours * 3600)


# Where Xcode keeps downloaded provisioning profiles: Xcode 16 and later, then older versions.
PROFILE_DIRS = (Path.home() / "Library" / "Developer" / "Xcode" / "UserData" / "Provisioning Profiles",
                Path.home() / "Library" / "MobileDevice" / "Provisioning Profiles")
RUNNER_BUNDLE = "app.mobster.wda.runner"


# App IDs are unique across Apple's teams, so a team digest makes the id its own without spelling out the team id.
def runner_bundle_id(team):
    if not team or not TEAM_ID.fullmatch(team):
        return RUNNER_BUNDLE
    return f"{RUNNER_BUNDLE}.{hashlib.sha256(team.encode()).hexdigest()[:10]}"


def team_label(team):
    """ "Jane Appleseed (Personal Team)", "Example, LLC", or the bare id when Xcode gave no name."""
    name = team.get("name")
    if not name:
        return team["id"]
    return f"{name} (Personal Team)" if team.get("personal") else name


def stale_runner_profiles(team, before, directories=PROFILE_DIRS):
    # wildcard profiles and other apps' profiles are never touched
    if not team or not TEAM_ID.fullmatch(team):
        return []
    prefix = f"{team}.{RUNNER_BUNDLE}"
    stale = []
    for directory in directories:
        try:
            files = (sorted(Path(directory).glob("*.mobileprovision"))
                     + sorted(Path(directory).glob("*.provisionprofile")))
        except OSError:
            continue
        for path in files:
            text = _read(["security", "cms", "-D", "-i", str(path)])
            try:
                profile = plistlib.loads(text.encode()) if text else {}
            except Exception:
                continue
            identifier = (profile.get("Entitlements") or {}).get("application-identifier")
            expires = profile.get("ExpirationDate")
            if not isinstance(identifier, str) or not (identifier == prefix or identifier.startswith(prefix + ".")):
                continue
            if not hasattr(expires, "timestamp"):
                continue
            expires = (expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)).timestamp()
            if expires < before:
                stale.append(path)
    return stale
