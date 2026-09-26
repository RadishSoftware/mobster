"""Signing facts Setup reads from this Mac: the Apple development teams it can build with, and
when a built runner's provisioning profile expires (a free Apple account signs for 7 days).

Everything here only reads: Xcode's own preferences, the login keychain's certificates, and
the profile embedded in the built runner.
"""

from datetime import timezone
from pathlib import Path
import plistlib
import re
import subprocess

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


def account_teams(defaults=None):
    """Teams of the Apple IDs signed in under Xcode › Settings › Accounts, personal teams included.

    Signing in is enough: a free Apple ID's Personal Team is listed here before it has a
    signing certificate, and the build creates the certificate (-allowProvisioningUpdates).
    """
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


def subject_team(subject):
    """(team id, team name) from `openssl x509 -subject -nameopt multiline` output.

    In an Apple Development certificate OU is the team id and O the team's name (for a
    personal team, the person's name). The id in parentheses in CN is not the team id.
    """
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
