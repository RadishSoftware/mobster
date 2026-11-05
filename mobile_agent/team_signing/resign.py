"""Sign the helper's prebuilt runner on this Mac with ``codesign`` and the team's profile, and check that it verifies.

Input: the unsigned or previously signed ``WebDriverAgentRunner-Runner.app`` (the prebuilt runner, or a copy Xcode
built once), a development profile Apple made (``.mobileprovision``) and the identity in Mobster's signing keychain.
Output: a signed copy, ready for pymobiledevice3 to install. The original is never changed.

Only ``/usr/bin/codesign`` and ``/usr/bin/security`` run, with absolute paths, with ``DEVELOPER_DIR`` pointing nowhere,
so ``xcodebuild`` and ``xcrun`` can't start: Xcode isn't needed and isn't invoked. Nothing changes how the phone looks
to an app: the runner's contents are the ones it was built with.
"""

import datetime
import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..devkit import DevError

CODESIGN = "/usr/bin/codesign"
SECURITY = "/usr/bin/security"
ALLOWED = frozenset({CODESIGN, SECURITY})
ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "DEVELOPER_DIR": "/var/empty", "HOME": str(Path.home())}
RUNNER_NAME = "WebDriverAgentRunner-Runner.app"
MIN_DAYS = 7  # a profile that ends sooner than this is refused: it would need replacing at once


def _run(argv, timeout=120):
    if argv[0] not in ALLOWED:
        raise DevError(f"Mobster didn't run {argv[0]}: signing uses codesign and security only.")
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=ENV, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        return 127, "", type(error).__name__
    return done.returncode, done.stdout, done.stderr


def decode_profile(path, run=None):
    """The plist inside a ``.mobileprovision`` (a CMS envelope), as a dict. DevError when it can't be read."""
    code, out, _ = (run or _run)([SECURITY, "cms", "-D", "-i", str(path)])
    try:
        profile = plistlib.loads(out.encode("utf-8")) if code == 0 and out else None
    except Exception:  # noqa: BLE001 -- unreadable is the error below
        profile = None
    if not isinstance(profile, dict):
        raise DevError("Mobster couldn't read the provisioning profile.", "Make the profile again.")
    return profile


def profile_facts(profile):
    """{team, app_identifier, udids, certificates (SHA-1s), expires (epoch), uuid, name} from a decoded profile."""
    entitlements = profile.get("Entitlements") or {}
    teams = profile.get("TeamIdentifier") or []
    expires = profile.get("ExpirationDate")
    if isinstance(expires, datetime.datetime):
        expires = (expires if expires.tzinfo else expires.replace(tzinfo=datetime.timezone.utc)).timestamp()
    return {
        "team": teams[0] if teams else None,
        "app_identifier": entitlements.get("application-identifier"),
        "udids": sorted(str(udid) for udid in profile.get("ProvisionedDevices") or ()),
        "certificates": sorted(
            hashlib.sha1(blob).hexdigest().upper()
            for blob in profile.get("DeveloperCertificates") or ()
            if isinstance(blob, bytes)
        ),
        "expires": expires,
        "uuid": profile.get("UUID"),
        "name": profile.get("Name"),
        "get_task_allow": entitlements.get("get-task-allow") is True,
    }


def covers(app_identifier, team, bundle_id):
    """Whether a profile's application-identifier (``TEAM.com.example.app`` or ``TEAM.*`` or ``TEAM.com.example.*``)
    covers ``bundle_id`` for ``team``."""
    if not isinstance(app_identifier, str) or not app_identifier.startswith(team + "."):
        return False
    pattern = app_identifier[len(team) + 1 :]
    if pattern.endswith("*"):
        return bundle_id.startswith(pattern[:-1])
    return pattern == bundle_id


def runner_bundle_id(app):
    try:
        info = plistlib.loads((Path(app) / "Info.plist").read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise DevError(f"{Path(app).name} isn't an app bundle Mobster can read.") from None
    identifier = info.get("CFBundleIdentifier")
    if not isinstance(identifier, str) or not identifier:
        raise DevError(f"{Path(app).name} has no bundle identifier.")
    return identifier


def entitlements_for(facts, bundle_id):
    """What the signed runner is allowed: the four entitlements the runner has when Xcode signs it, limited to what the
    profile grants."""
    application = f"{facts['team']}.{bundle_id}"
    out = {
        "application-identifier": application,
        "com.apple.developer.team-identifier": facts["team"],
        "keychain-access-groups": [application],
    }
    if facts["get_task_allow"]:
        out["get-task-allow"] = True
    return out


def code_objects(app):
    """Everything that carries its own signature, innermost first: the frameworks and libraries inside the test bundle,
    the test bundle, the app's own frameworks and libraries. The app itself is signed last, by the caller."""
    app = Path(app)
    objects = []
    for plugin in sorted((app / "PlugIns").glob("*.xctest")) if (app / "PlugIns").is_dir() else ():
        frameworks = plugin / "Frameworks"
        if frameworks.is_dir():
            objects += sorted(p for p in frameworks.iterdir() if p.suffix in (".framework", ".dylib"))
        objects.append(plugin)
    frameworks = app / "Frameworks"
    if frameworks.is_dir():
        objects += sorted(p for p in frameworks.iterdir() if p.suffix in (".framework", ".dylib"))
    return objects


def sign(source, profile_path, identity, keychain, destination, *, now=None, run=None, decode=None, bundle_id=None):
    """Re-sign a copy of ``source`` into ``destination``/Runner.app and verify it.

    ``identity`` is the signing certificate's SHA-1. ``bundle_id`` is the id the signed runner carries (default: the one
    it has). DevError, with the fix, when the profile isn't this runner's, doesn't
    list the certificate, is about to end, or the signature doesn't verify. Returns the signed runner's facts."""
    run = run or _run
    source, destination = Path(source), Path(destination)
    if not (source / "Info.plist").exists():
        raise DevError(f"There is no runner at {source}.", "Mobster needs the prebuilt helper runner to sign.")
    facts = profile_facts((decode or decode_profile)(profile_path))
    original = runner_bundle_id(source)
    bundle_id = bundle_id or original
    now = datetime.datetime.now(datetime.timezone.utc).timestamp() if now is None else now
    if not facts["team"] or not covers(facts["app_identifier"], facts["team"], bundle_id):
        raise DevError(f"The profile isn't for {bundle_id}.", "Make a profile for the helper's App ID and try again.")
    if identity.upper() not in facts["certificates"]:
        raise DevError(
            "The profile doesn't list the signing certificate.", "Make the profile again with the certificate."
        )
    if not facts["expires"] or facts["expires"] < now + MIN_DAYS * 86400:
        raise DevError("The profile ends in under a week.", "Make a new profile.")
    if not facts["udids"]:
        raise DevError("The profile lists no iPhone.", "Register the iPhone and make the profile again.")
    destination.mkdir(parents=True, exist_ok=True)
    signed = destination / "Runner.app"
    shutil.rmtree(signed, ignore_errors=True)
    shutil.copytree(source, signed, symlinks=True)
    try:
        (signed / "embedded.mobileprovision").unlink(missing_ok=True)
        for dsym in signed.rglob("*.dSYM"):
            shutil.rmtree(dsym, ignore_errors=True)
        shutil.copyfile(profile_path, signed / "embedded.mobileprovision")
        if bundle_id != original:
            # One prebuilt runner serves every team: an App ID is unique across Apple's teams, so each team's runner
            # carries its own id (signing.runner_bundle_id), set here before the signature seals the bundle.
            info = plistlib.loads((signed / "Info.plist").read_bytes())
            info["CFBundleIdentifier"] = bundle_id
            (signed / "Info.plist").write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_XML))
        workspace = Path(tempfile.mkdtemp(prefix="mobster-sign-"))
        try:
            plist = workspace / "entitlements.plist"
            plist.write_bytes(plistlib.dumps(entitlements_for(facts, bundle_id)))
            base = [CODESIGN, "-f", "-s", identity, "--keychain", str(keychain), "--timestamp=none"]
            for item in code_objects(signed):
                _checked(run, [*base, str(item)], f"sign {item.name}")
            _checked(
                run,
                [*base, "--entitlements", str(plist), "--generate-entitlement-der", str(signed)],
                f"sign {signed.name}",
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        verify(signed, facts, bundle_id, run)
    except BaseException:
        shutil.rmtree(signed, ignore_errors=True)
        raise
    return {
        "path": str(signed),
        "team": facts["team"],
        "bundle_id": bundle_id,
        "udids": facts["udids"],
        "expires": facts["expires"],
        "profile": facts["uuid"],
        "profile_name": facts["name"],
        "certificate": identity.upper(),
    }


def _checked(run, argv, what):
    code, _, err = run(argv)
    if code != 0:
        line = next((line for line in reversed(err.splitlines()) if line.strip()), "")
        raise DevError(
            f"codesign couldn't {what}. {line[:200]}".strip(), "Unlock your Mac's login Keychain and sign again."
        )


def verify(app, facts, bundle_id, run=None):
    """The signature holds all the way down, it names the team and the runner's bundle id, and the profile inside is
    the one the signature was made with. DevError otherwise."""
    run = run or _run
    code, _, err = run([CODESIGN, "--verify", "--deep", "--strict", str(app)])
    if code != 0:
        raise DevError("The signed runner doesn't verify.", (err.strip().splitlines() or [""])[-1][:200])
    code, _, err = run([CODESIGN, "-dvv", str(app)])
    fields = dict(re.findall(r"^(Identifier|TeamIdentifier)=(.+)$", err, re.M))
    if fields.get("TeamIdentifier") != facts["team"] or fields.get("Identifier") != bundle_id:
        raise DevError("The signed runner names a different team or bundle id than its profile.")
    code, out, _ = run([CODESIGN, "-d", "--entitlements", ":-", str(app)])
    try:
        granted = plistlib.loads(out.encode("utf-8"))
    except Exception:  # noqa: BLE001
        granted = {}
    if granted.get("application-identifier") != f"{facts['team']}.{bundle_id}":
        raise DevError("The signed runner's entitlements don't match its profile.")
    embedded = Path(app) / "embedded.mobileprovision"
    if not embedded.exists():
        raise DevError("The signed runner has no profile inside.")


def check(app, run=None, decode=None):
    """Read a signed runner back (Path C's import, a later install): its profile's facts after the signature verifies.
    DevError when the signature doesn't hold or the profile is gone."""
    app = Path(app)
    profile = app / "embedded.mobileprovision"
    if not profile.exists():
        raise DevError("That runner isn't signed with a profile.")
    facts = profile_facts(decode(profile) if decode else decode_profile(profile, run))
    verify(app, facts, runner_bundle_id(app), run)
    return {**facts, "bundle_id": runner_bundle_id(app)}


def tree_digest(app):
    """A digest of a runner bundle's files and their modes, to see that an exported bundle arrived unchanged."""
    digest = hashlib.sha256()
    base = Path(app)
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base).as_posix()
        if path.is_symlink():
            digest.update(f"L {relative} {os.readlink(path)}\n".encode())
        elif path.is_file():
            digest.update(f"F {relative} {path.stat().st_mode & 0o111 != 0} ".encode())
            with open(path, "rb") as stream:
                for block in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(block)
            digest.update(b"\n")
    return digest.hexdigest()
