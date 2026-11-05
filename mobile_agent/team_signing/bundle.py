"""Path C: a signed helper and its profile, exported from one Mac and installed from another.

One Mac with a team's App Store Connect key (or with Xcode) signs the helper once; the other Macs, which need neither,
import it and only install and launch it. A bundle is a gzip tar of ``manifest.json`` and ``Runner.app``. The signature
inside is Apple's team signature, so what the bundle can do on a phone is fixed by its profile: it runs only on the
phones the profile lists, until the profile ends.

Importing trusts nothing in the file. It extracts into a private folder with tar's ``data`` filter (no absolute paths,
no links out, no devices), then refuses unless: the person named the team the bundle says it is from, the digest of
every file matches the manifest, the signature verifies, the profile inside says what the manifest says, it was built
with this Mobster's patches, and it has more than a day left. The team check is the person's: a bundle signed by
another team would verify too.
"""

import datetime
import io
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

from ..devkit import DevError
from ..wda import patches as wda_patches
from . import resign

KIND = "mobster-signed-helper"
VERSION = 1
MIN_LEFT = 86400


def export(runner, destination, *, run=None, decode=None, now=None):
    """Write the signed ``runner`` (…/Runner.app) as a bundle at ``destination`` (a file path). Returns the manifest.
    DevError when the runner's signature doesn't verify, or it is a runner this Mobster didn't sign with its patches."""
    runner, destination = Path(runner), Path(destination)
    facts = resign.check(runner, run=run, decode=decode)
    now = datetime.datetime.now(datetime.timezone.utc).timestamp() if now is None else now
    manifest = {
        "kind": KIND,
        "version": VERSION,
        "team": facts["team"],
        "bundle_id": facts["bundle_id"],
        "udids": facts["udids"],
        "expires": facts["expires"],
        "profile": facts["uuid"],
        "profile_name": facts["name"],
        "patches": wda_patches.patch_digest(),
        "tree": resign.tree_digest(runner),
        "created": now,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    try:
        with tarfile.open(partial, "w:gz") as archive:
            data = json.dumps(manifest, indent=1, sort_keys=True).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mtime, info.mode = len(data), now, 0o600
            archive.addfile(info, io.BytesIO(data))
            archive.add(runner, arcname="Runner.app")
        partial.replace(destination)
        destination.chmod(0o600)
    finally:
        partial.unlink(missing_ok=True)
    return manifest


def read_manifest(path):
    """The manifest of a bundle, without extracting the runner (so a person can be shown the team before importing)."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            member = archive.getmember("manifest.json")
            if member.size > 1 << 20:
                raise DevError("That isn't a Mobster helper bundle.")
            manifest = json.loads(archive.extractfile(member).read())
    except (OSError, KeyError, ValueError, tarfile.TarError):
        raise DevError("That isn't a Mobster helper bundle.", "Export one with `mobster signing export`.") from None
    if not isinstance(manifest, dict) or manifest.get("kind") != KIND or manifest.get("version") != VERSION:
        raise DevError("That isn't a Mobster helper bundle this Mobster can read.")
    return manifest


def import_bundle(path, destination, *, team, udid=None, run=None, decode=None, now=None):
    """Check a bundle and put its runner at ``destination``/Runner.app. ``team`` is the team id the person says it is
    from. Returns {path, manifest-facts}. DevError, with the fix, on any refusal; nothing is left behind then."""
    manifest = read_manifest(path)
    if manifest.get("team") != team:
        raise DevError(
            f"That bundle is from team {manifest.get('team')}, not {team}, so Mobster didn't open it.",
            "Use the bundle from the team you mean.",
        )
    now = datetime.datetime.now(datetime.timezone.utc).timestamp() if now is None else now
    if manifest.get("patches") != wda_patches.patch_digest():
        raise DevError(
            "That helper was built with other patches than this Mobster has.",
            "Export a new bundle from a Mobster of the same version.",
        )
    if not isinstance(manifest.get("expires"), (int, float)) or manifest["expires"] < now + MIN_LEFT:
        raise DevError("That helper's profile has ended or ends within a day.", "Export a new bundle.")
    if udid is not None and udid not in (manifest.get("udids") or ()):
        raise DevError(
            "That bundle's profile doesn't list this iPhone.",
            "Register the iPhone on the signing Mac, sign again and export a new bundle.",
        )
    destination = Path(destination)
    scratch = Path(tempfile.mkdtemp(prefix="mobster-import-"))
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = [m for m in archive.getmembers() if m.name == "Runner.app" or m.name.startswith("Runner.app/")]
            if not members:
                raise DevError("That bundle has no helper in it.")
            archive.extractall(scratch, members=members, filter="data")
        runner = scratch / "Runner.app"
        if resign.tree_digest(runner) != manifest.get("tree"):
            raise DevError("That bundle changed after it was exported, so Mobster didn't use it.")
        facts = resign.check(runner, run=run, decode=decode)
        if (facts["team"], facts["bundle_id"], facts["udids"]) != (
            manifest["team"],
            manifest["bundle_id"],
            manifest["udids"],
        ):
            raise DevError("That bundle's profile doesn't say what its label says, so Mobster didn't use it.")
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / "Runner.app"
        shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(runner), str(target))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {
        "path": str(target),
        "team": manifest["team"],
        "bundle_id": manifest["bundle_id"],
        "udids": manifest["udids"],
        "expires": manifest["expires"],
        "profile": manifest.get("profile"),
        "certificate": None,
    }
