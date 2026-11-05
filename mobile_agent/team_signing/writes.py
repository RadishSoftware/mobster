"""The three App Store Connect writes that sign the helper, as a card the person approves.

``plan`` reads the team (its iPhones, the helper's App ID, its development certificates and Mobster's profiles) and
returns a ``Card``: only the writes still needed, each with the exact body that is sent. ``execute`` sends them, after
the person approved the card's digest, in order, through ``asc.Client.write`` (which sends nothing the approval
doesn't name, and journals first). Run again after a failure: what Apple already has is read, not written.

The writes, and nothing else:

1. ``register_device``: an iPhone's UDID and name, once per phone and team.
2. ``create_certificate``: a development certificate from a certificate signing request made on this Mac. The private
   key never leaves it.
3. ``create_profile``: one development profile for the helper's App ID, listing the certificate and every phone.
   Apple's API has no profile update, so adding a phone makes a new profile that lists all of them; Mobster doesn't
   delete the old one.

The App ID itself is not one of them. It must exist on the team (Xcode makes it on a first build; the portal's
Identifiers page does too), and ``plan`` says so when it doesn't.
"""

import hashlib
from dataclasses import dataclass, field

from ..devkit import DevError, asc

PROFILE_PREFIX = "Mobster helper"
CERTIFICATE_TYPE = "DEVELOPMENT"
PROFILE_TYPE = "IOS_APP_DEVELOPMENT"


def ref(name):
    return {asc.REF: name}


def device_body(name, udid):
    return {"data": {"type": "devices", "attributes": {"name": name, "platform": "IOS", "udid": udid}}}


def certificate_body(csr_pem):
    return {
        "data": {"type": "certificates", "attributes": {"certificateType": CERTIFICATE_TYPE, "csrContent": csr_pem}}
    }


def profile_body(name, bundle_id, certificates, devices):
    """``certificates`` and ``devices`` are ids, or ref(...) for ones the same run makes."""

    def many(kind, ids):
        return {"data": [{"type": kind, "id": item} for item in ids]}

    return {
        "data": {
            "type": "profiles",
            "attributes": {"name": name, "profileType": PROFILE_TYPE},
            "relationships": {
                "bundleId": {"data": {"type": "bundleIds", "id": bundle_id}},
                "certificates": many("certificates", certificates),
                "devices": many("devices", devices),
            },
        }
    }


@dataclass(frozen=True)
class Step:
    write: str  # an asc.WRITES name
    produces: str  # what later steps call its answer: "device:<udid>", "certificate", "profile"
    title: str  # the card's line for it
    template: dict  # the exact body, with {"$ref": produces-of-an-earlier-step} where Apple's id goes
    summary: dict  # what the ledger records about it (never the CSR)
    lines: tuple  # (label, value) pairs the card shows


@dataclass(frozen=True)
class Card:
    bundle: dict  # the helper's App ID on the team: {id, identifier, name}
    steps: tuple  # of Step, in the order they are sent
    certificate_id: str = ""  # the certificate the profile lists when no step makes one
    profile: dict = field(default_factory=dict)  # an existing profile that already does the job, if any
    devices: dict = field(default_factory=dict)  # UDID -> Apple's id, for the phones already registered

    def approval(self):
        return asc.Approval([(step.write, step.template) for step in self.steps])

    @property
    def digest(self):
        return self.approval().digest

    @property
    def needed(self):
        return bool(self.steps)

    def public(self, key=None):
        """What the approval card shows, as JSON: the exact request of each write. No token, key or secret."""

        def shown(value):
            if isinstance(value, dict):
                if set(value) == {asc.REF}:
                    return f"<the id Apple gives {value[asc.REF].replace(':', ' ')}>"
                return {k: shown(v) for k, v in value.items()}
            if isinstance(value, list):
                return [shown(item) for item in value]
            return value

        return {
            "digest": self.digest,
            "needed": self.needed,
            "key": key,
            "bundle": self.bundle,
            "steps": [
                {
                    "number": number,
                    "write": step.write,
                    "title": step.title,
                    "method": "POST",
                    "url": asc.API + asc.WRITES[step.write][0],
                    "lines": [{"label": label, "value": value} for label, value in step.lines],
                    "body": shown(step.template),
                }
                for number, step in enumerate(self.steps, 1)
            ],
        }


def canonical_ids(*parts):
    return asc.canonical(list(parts))


def profile_prefix(bundle_identifier, base=PROFILE_PREFIX):
    return f"{base} {bundle_identifier} "


def plan(client, *, bundle_identifier, phones, csr_pem=None, certificate_serial=None, stamp, base=PROFILE_PREFIX):
    """The card for these ``phones`` ([{name, udid}]) on this team.

    ``csr_pem`` is the certificate signing request made on this Mac (or a function that makes it), needed only when no
    certificate is. ``certificate_serial``
    is the serial of the certificate Mobster made here before, if any: it is reused while Apple still lists it.
    ``stamp`` ("20261018") starts a new profile's name, and ``base`` starts every name (a test uses its own,
    so it never meets the real helper's profiles). DevError when the App ID, a phone's status or the request is
    wrong."""
    bundle = client.bundle_id(bundle_identifier)
    if bundle is None:
        # Xcode keeps a wildcard App ID ("XC Wildcard") for a team that has built an app. A profile for it covers any
        # bundle id, the helper's included.
        bundle = client.bundle_id("*")
    if bundle is None:
        raise DevError(
            f"Your team has no App ID for {bundle_identifier}, and Mobster doesn't register App IDs.",
            "Open developer.apple.com › Certificates, Identifiers & Profiles › Identifiers, add an "
            f"explicit iOS App ID {bundle_identifier}, then try again.",
        )
    registered = {item["udid"]: item for item in client.devices()}
    steps, devices = [], {}
    for phone in phones:
        udid, name = phone["udid"], phone["name"]
        known = registered.get(udid)
        if known and known["status"] != "ENABLED":
            raise DevError(
                f"“{known['name']}” is registered on your team and disabled.",
                "Enable it in developer.apple.com › Certificates, Identifiers & Profiles › Devices.",
            )
        if known:
            devices[udid] = known["id"]
            continue
        steps.append(
            Step(
                "register_device",
                f"device:{udid}",
                f"Register “{name}” with your team",
                device_body(name, udid),
                {"name": name, "udid": udid},
                (("Name", name), ("UDID", udid), ("Platform", "iOS")),
            )
        )
    certificates = {item["serial"]: item for item in client.development_certificates()}
    have = certificates.get(certificate_serial) if certificate_serial else None
    certificate_id = have["id"] if have else ""
    if not have:
        csr_pem = csr_pem() if callable(csr_pem) else csr_pem
        if not csr_pem:
            raise DevError("Mobster needs a certificate signing request to ask for a certificate.")
        digest = hashlib.sha256(csr_pem.encode()).hexdigest()[:16]
        steps.append(
            Step(
                "create_certificate",
                "certificate",
                "Create a development certificate",
                certificate_body(csr_pem),
                {"csr_sha256": digest},
                (
                    ("Type", CERTIFICATE_TYPE),
                    ("Key", "made on this Mac, never leaves it"),
                    ("Request", f"sha256 {digest}"),
                ),
            )
        )
    existing = [
        row
        for row in client.development_profiles(profile_prefix(bundle_identifier, base))
        if row["bundle"] == bundle["id"]
    ]
    members = [devices.get(phone["udid"]) or ref(f"device:{phone['udid']}") for phone in phones]
    if not steps:
        known = set(members)
        reusable = next(
            (row for row in existing if certificate_id in row["certificates"] and known <= set(row["devices"])), None
        )
        if reusable:
            return Card(bundle, (), certificate_id, reusable, devices)
    # A new profile keeps every phone an earlier Mobster profile for this App ID listed (another Mac's, a phone
    # Mobster no longer manages), so adding a phone never drops one.
    kept = sorted(
        {device for row in existing for device in row["devices"]} - {i for i in members if isinstance(i, str)}
    )
    # The name is the same for the same card on the same day (a card and its approval run apart), and differs when the
    # phones or the certificate do.
    fingerprint = hashlib.sha256(
        canonical_ids(
            sorted(phone["udid"] for phone in phones),
            certificate_id or hashlib.sha256((csr_pem or "").encode()).hexdigest(),
        ).encode()
    ).hexdigest()[:6]
    name = f"{profile_prefix(bundle_identifier, base)}{stamp}-{fingerprint}"
    listed = [*members, *kept]
    steps.append(
        Step(
            "create_profile",
            "profile",
            "Create a development profile for Mobster's helper",
            profile_body(name, bundle["id"], [certificate_id or ref("certificate")], listed),
            {"name": name, "bundle": bundle_identifier, "phones": len(listed)},
            (
                ("Name", name),
                ("App ID", bundle_identifier),
                ("Type", "iOS App Development"),
                ("Lists", f"{len(listed)} iPhone{'s' if len(listed) != 1 else ''} and the certificate"),
            ),
        )
    )
    return Card(bundle, tuple(steps), certificate_id, {}, devices)


def _render(template, answers):
    if isinstance(template, dict):
        if set(template) == {asc.REF}:
            return answers[template[asc.REF]]["id"]
        return {key: _render(value, answers) for key, value in template.items()}
    if isinstance(template, list):
        return [_render(item, answers) for item in template]
    return template


def execute(client, card, approved, ledger, on_answer=None):
    """Send the card's writes, in order. ``approved`` is the digest the person approved: it must be this card's.
    Returns the answers by what each step produces ({"certificate": {id, attributes}, ...}). ``on_answer(step, answer)``
    runs right after each write, to keep what Apple handed back (the certificate) before anything else can fail.
    Stops at the first failure; the ones already made stay made, and a later ``plan`` sees them."""
    if approved != card.digest:
        raise DevError("The card changed after you approved it, so Mobster sent nothing.", "Look at it again.")
    approval, answers = card.approval(), {}
    for step in card.steps:
        answers[step.produces] = client.write(
            step.write, _render(step.template, answers), approval=approval, ledger=ledger, summary=step.summary
        )
        if on_answer is not None:
            on_answer(step, answers[step.produces])
    return answers
