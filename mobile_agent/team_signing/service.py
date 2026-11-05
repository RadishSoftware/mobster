"""Without Xcode, for a paid team: read the team, show the card, and after approval write, sign, install.

``TeamSigning`` is what Setup's row and ``mobster signing`` drive. One run at a time:

1. ``card`` reads App Store Connect (the phones, the App ID, the certificates, Mobster's profiles), makes the
   certificate signing request on this Mac if a certificate is needed, and returns the card: exactly the requests
   Mobster would send (writes.py). It sends nothing.
2. ``approve`` takes the digest the person approved. It reads the team again, refuses if the card changed, then makes
   the writes (each journaled first, none retried when the outcome is unknown), keeps the certificate beside its key,
   signs the prebuilt runner with ``codesign`` and the new profile, mounts the developer image if the phone has none,
   installs the signed runner, and records it for the supervisor.

Everything is off unless ``MOBSTER_EXPERIMENTAL_NO_XCODE=1`` (team_signing.enabled).
"""

import base64
import datetime
import shutil
import tempfile
import threading
import time
from pathlib import Path

from .. import signing
from ..devkit import DevError, asc
from ..wda import patches as wda_patches
from . import FLAG, bundle, enabled, prebuilt, resign, tooling, writes
from . import identity as identity_module
from . import ledger as ledger_module

STAGES = ("idle", "reading", "approval", "writing", "signing", "installing", "ready", "failed")
BUSY = ("reading", "writing", "signing", "installing")
KEY_TTL = 30.0
NO_KEY = (
    "Mobster has no App Store Connect key for your team.",
    "Make one in App Store Connect › Users and Access › Integrations with the Admin or Developer role, then "
    "run: mobster asc key add --key-id KEY_ID --issuer-id ISSUER_ID --p8 AuthKey_KEY_ID.p8",
)


class TeamSigning:
    def __init__(
        self,
        manager=None,
        *,
        folder=None,
        environ=None,
        make_client=None,
        identity=None,
        ledger=None,
        sign=resign.sign,
        run=None,
        clock=time.time,
        pmd3=tooling.pmd3,
        go_ios=tooling.go_ios,
        runner=None,
        load_key=asc.load_key,
    ):
        self.manager, self.environ, self.clock = manager, environ, clock
        self.folder = Path(folder) if folder is not None else self._folder()
        self.load_key = load_key
        self.make_client = make_client or (lambda: asc.Client(self.load_key()))
        self.identity = identity or identity_module.Identity(self.folder)
        self.ledger = ledger or ledger_module.Ledger(self.folder / "ledger.jsonl")
        self.sign, self.run, self.pmd3, self.go_ios, self.runner = sign, run, pmd3, go_ios, runner
        self.lock = threading.Lock()
        self.job = None
        self.current = {"stage": "idle", "error": None, "fix": None, "card": None, "result": None}
        self._card = None
        self._key = (0.0, None)

    def refresh(self):
        """Read the key again at the next status ("Check again")."""
        self._key = (0.0, None)

    def _folder(self):
        from . import state_dir

        return state_dir(getattr(self.manager, "data_dir", None))

    # -- what Setup reads ------------------------------------------------------------------------------------

    def status(self):
        with self.lock:
            current = dict(self.current)
        # A finished run belongs to the phone it was for: another phone starts again from the card.
        result = current.get("result") or {}
        if (
            current["stage"] == "ready"
            and result.get("udid")
            and self.manager is not None
            and self.manager.settings().get("udid") not in (None, result["udid"])
        ):
            current = {**current, "stage": "idle", "card": None, "result": None}
        key = self._public_key()
        driver = None
        try:
            driver = tooling.choose_driver(flag=enabled(self.environ), pmd3_path=self.pmd3(), go_ios_path=self.go_ios())
        except DevError:
            pass
        return {
            "enabled": enabled(self.environ),
            "flag": FLAG,
            "driver": driver,
            "tools": {"pymobiledevice3": bool(self.pmd3()), "go-ios": bool(self.go_ios())},
            "key": key,
            "unresolved": self.ledger.unresolved(),
            **current,
        }

    def _public_key(self):
        """The key's public part, read from the Keychain at most every 30 s (Setup asks every couple of seconds)."""
        at, key = self._key
        if self.clock() - at < KEY_TTL and at:
            return key
        try:
            key = self.load_key().public()
        except DevError:
            key = None
        self._key = (self.clock(), key)
        return key

    def _set(self, **changes):
        with self.lock:
            self.current = {**self.current, **changes}

    def _fail(self, error):
        fix = getattr(error, "fix", "") or None
        message = getattr(error, "message", None) or str(error) or type(error).__name__
        self._set(stage="failed", error=message, fix=fix)

    # -- the card --------------------------------------------------------------------------------------------

    def _phones(self, phones=None):
        if phones:
            return [dict(phone) for phone in phones]
        manager = self.manager
        found = [d for d in (manager.devices() if manager else ()) if d.get("trusted")]
        chosen = manager.device(found) if manager else None
        ordered = ([chosen] if chosen else []) + [d for d in found if not chosen or d["udid"] != chosen["udid"]]
        return [{"name": d["name"], "udid": d["udid"]} for d in ordered]

    def _plan(self, client, phones, bundle_identifier=None, team=None):
        phones = self._phones(phones)
        if not phones:
            raise DevError("Mobster has no iPhone to set up.", "Plug in an iPhone that trusts this Mac.")
        if bundle_identifier is None:
            team = team or (self.manager.settings().get("team") if self.manager else None) or client.team_id()
            if not team:
                raise DevError(
                    "Mobster couldn't tell which team the key belongs to.",
                    "Pass the team id with --team, or register an App ID on the team first.",
                )
            bundle_identifier = signing.runner_bundle_id(team) + ".xctrunner"
        state = self.identity.state()
        card = writes.plan(
            client,
            bundle_identifier=bundle_identifier,
            phones=phones,
            csr_pem=self.identity.request,
            certificate_serial=state.get("certificate_serial"),
            stamp=datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc).strftime("%Y%m%d"),
        )
        return card, bundle_identifier

    def card(self, phones=None, bundle_identifier=None, team=None):
        """Read the team and return the card (JSON-ready). Sends nothing to Apple but reads."""
        with self.lock:
            if self.current["stage"] in BUSY:
                raise DevError("Mobster is still working on the last card.")
            self.current = {**self.current, "stage": "reading", "error": None, "fix": None}
        try:
            self._require_enabled()
            client = self.make_client()
            card, bundle_identifier = self._plan(client, phones, bundle_identifier, team)
            shown = card.public(key=client.key.public())
            self._card = (card, bundle_identifier, phones, team)
            self._set(stage="approval", card=shown)
            return shown
        except Exception as error:  # noqa: BLE001 -- the card shows why, in words
            self._fail(error)
            raise

    def apply(self, digest, phones=None, bundle_identifier=None, team=None, writes_only=False):
        """``card`` and ``approve`` in one process, for the terminal: reads the team again, refuses unless the card
        is the one ``digest`` names, then runs it and waits."""
        self.card(phones, bundle_identifier, team)
        return self.approve(digest, writes_only=writes_only, background=False)

    def _require_enabled(self):
        if not enabled(self.environ):
            raise DevError("Without Xcode is off.", f"Turn it on with {FLAG}=1.")

    # -- approval and the run --------------------------------------------------------------------------------

    def approve(self, digest, *, writes_only=False, background=True):
        """Run what the card shows. ``digest`` is the card's, as the person approved it."""
        with self.lock:
            if self.current["stage"] != "approval" or self._card is None:
                raise DevError("There is no card waiting for approval.", "Ask for the card again.")
            card = self._card[0]
            if digest != card.digest:
                raise DevError("That isn't the card on screen, so Mobster sent nothing.", "Look at it again.")
            self.current = {**self.current, "stage": "writing", "error": None, "fix": None}
        if background:
            self.job = threading.Thread(
                target=self._job, args=(digest, writes_only), name="mobster-team-signing", daemon=True
            )
            self.job.start()
        else:
            self._job(digest, writes_only)
        return self.status()

    def _job(self, digest, writes_only):
        try:
            client = self.make_client()
            _, bundle_identifier, phones, team = self._card
            self._recover(client)
            card, _ = self._plan(client, phones, bundle_identifier, team)
            if card.digest != digest:
                raise DevError(
                    "Your team changed after you approved the card, so Mobster sent nothing.", "Look at the new card."
                )
            answers = writes.execute(client, card, digest, self.ledger, on_answer=self._keep)
            if writes_only:
                self._set(
                    stage="ready", result={"writes": {name: answer.get("id") for name, answer in answers.items()}}
                )
                return
            self._set(stage="signing")
            profile = self._profile(client, card, answers)
            identity = self.identity.signing_identity()
            if identity is None:
                raise DevError(
                    "The signing certificate isn't in Mobster's keychain.", "Ask for the card again to make one."
                )
            derived = getattr(self.manager, "derived", None)
            built = self.manager.settings().get("built_patches") if self.manager else None
            source, where = prebuilt.locate(derived, built)
            phone = self._phones(phones)[0]
            signed = self.sign(
                source,
                profile,
                identity["sha1"],
                self.identity.keychain,
                self.folder / "runners" / phone["udid"],
                bundle_id=bundle_identifier,
            )
            self._set(stage="installing")
            self._install(phone["udid"], signed)
            self._record(phone, signed, where)
            self._set(
                stage="ready",
                result={
                    "runner": signed["bundle_id"],
                    "expires": signed["expires"],
                    "phones": signed["udids"],
                    "udid": phone["udid"],
                },
            )
        except Exception as error:  # noqa: BLE001 -- the stage shows why; the ledger holds what was sent
            self._fail(error)

    def _keep(self, step, answer):
        if step.produces == "certificate":
            attributes = answer.get("attributes") or {}
            self.identity.add_certificate(attributes.get("certificateContent") or "", attributes.get("serialNumber"))

    def _recover(self, client):
        """A certificate Apple made whose answer Mobster didn't get to keep (it stopped between the write and the
        keychain): fetch it by the id the ledger recorded, so the work isn't lost and the write isn't repeated."""
        if self.identity.state().get("pending_key") != "imported":
            return
        writes_by_digest, made = {}, None
        for event in self.ledger.events():
            if event["event"] == "intent":
                writes_by_digest[event["digest"]] = event["write"]
            elif (
                event["event"] in ("done", "resolved_done")
                and writes_by_digest.get(event["digest"]) == "create_certificate"
            ):
                made = event
        if made is None or not (made.get("detail") or {}).get("id"):
            return
        content, serial = client.certificate_content(made["detail"]["id"])
        if content:
            self.identity.add_certificate(content, serial)

    def _profile(self, client, card, answers):
        folder = self.folder / "profiles"
        from .. import env_file

        env_file.private_dir(folder)
        answer = answers.get("profile")
        profile_id = (answer or card.profile or {}).get("id")
        content = ((answer or {}).get("attributes") or {}).get("profileContent")
        data = base64.b64decode(content) if content else client.profile_content(profile_id)
        if not data:
            raise DevError("Apple's profile came back empty.", "Try again.")
        path = folder / f"{profile_id}.mobileprovision"
        with env_file.open_private(path, "wb") as stream:
            stream.write(data)
        return path

    def _install(self, udid, signed):
        program = self.pmd3()
        driver = tooling.choose_driver(flag=True, pmd3_path=program, go_ios_path=self.go_ios())
        if driver == "pymobiledevice3":
            tooling.ensure_mounted(program, udid, run=self.runner)
            tooling.run_short(tooling.install_command(program, udid, signed["path"]), run=self.runner)
            return
        scratch = tempfile.mkdtemp(prefix="mobster-install-")
        try:
            ipa = tooling.ipa_of(signed["path"], scratch)
            tooling.run_short(tooling.go_ios_install_command(self.go_ios(), udid, ipa), run=self.runner)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _record(self, phone, signed, where):
        driver = tooling.choose_driver(flag=True, pmd3_path=self.pmd3(), go_ios_path=self.go_ios())
        if self.manager is None:
            return
        self.manager.save_settings(
            udid=phone["udid"],
            device_name=phone["name"],
            team=signed["team"],
            built_for=phone["udid"],
            built_patches=wda_patches.patch_digest(),
            expires_at=signed["expires"],
            autostart=True,
            driver=driver,
            signed_runner={
                "path": signed["path"],
                "bundle_id": signed["bundle_id"],
                "profile": signed["profile"],
                "prebuilt": where,
                "installed_at": self.clock(),
            },
        )

    # -- after an unknown outcome ----------------------------------------------------------------------------

    def resolve(self, digest, made):
        """The person looked at developer.apple.com and says whether the change was made."""
        self.ledger.resolve(digest, bool(made))
        return self.status()

    # -- Path C: a signed helper from another Mac ---------------------------------------------------------------

    def export_bundle(self, destination, udid=None):
        """Write the helper this Mac signed for ``udid`` (default: the chosen phone) as a bundle other Macs can use."""
        self._require_enabled()
        udid = udid or self._phones()[0]["udid"]
        runner = self.folder / "runners" / udid / "Runner.app"
        if not runner.exists():
            raise DevError("This Mac hasn't signed a helper for that iPhone.", "Set the iPhone up first.")
        return bundle.export(runner, destination)

    def import_bundle(self, path, team, phones=None):
        """Install a bundle's helper on the phone. ``team`` is what the person says the bundle is from."""
        self._require_enabled()
        phone = self._phones(phones)[0]
        self._set(stage="installing", error=None, fix=None)
        try:
            imported = bundle.import_bundle(
                path, self.folder / "runners" / phone["udid"], team=team, udid=phone["udid"]
            )
            self._install(phone["udid"], imported)
            self._record(phone, imported, "an imported bundle")
        except Exception as error:  # noqa: BLE001
            self._fail(error)
            raise
        self._set(
            stage="ready",
            result={
                "runner": imported["bundle_id"],
                "expires": imported["expires"],
                "phones": imported["udids"],
                "udid": phone["udid"],
            },
        )
        return imported
