"""App Store Connect with your own API key: TestFlight builds, TestFlight feedback and crashes, the TestFlight build
installed on your iPhone by Mobster driving the TestFlight app, and, for a paid team, the three writes that sign
Mobster's helper without Xcode.

    key = load_key()                          # the Keychain, else MOBSTER_ASC_* in the environment
    asc = Client(key)
    app = asc.app("com.example.yourapp")      # {"id", "name", "bundleId"}
    asc.builds(app["id"], limit=10)           # newest first: version, build, uploaded, state, TestFlight state
    asc.feedback(app["id"])                   # screenshot and crash feedback from your testers

**Reads, and three named writes.** ``request`` sends GET requests and refuses any other verb. ``download`` GETs a
link in Apple's answer (a tester's screenshot) without the token. The only writes are ``WRITES``, by name: register an
iPhone's UDID, create a development certificate from a certificate signing request, and create a development
provisioning profile. ``write`` sends one only with an ``Approval`` that names exactly that body, and only after its
ledger has made the attempt durable (team_signing/ledger.py); a write whose outcome is unknown is never sent again.
Mobster submits nothing else to Apple: no build, metadata, screenshot, review or tester change. Uploading screenshots
stays your action (`fastlane deliver`, or App Store Connect itself).

The key: an App Store Connect API key (Users and Access › Integrations), its Key ID, and the Issuer ID for a team key
(an individual key has none). `mobster asc key add` keeps it in your login Keychain, readable only by Mobster's own
programs (keychain_acl). A CI job that has no Keychain passes MOBSTER_ASC_KEY_ID, MOBSTER_ASC_ISSUER_ID and
MOBSTER_ASC_KEY (the .p8's text) or MOBSTER_ASC_KEY_PATH. Each request carries a JWT signed here (es256.py) that
lasts 20 minutes, the most Apple allows. The key never leaves this Mac and is never printed.
"""

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import DevError, es256

API = "https://api.appstoreconnect.apple.com"
SERVICE = "Mobster App Store Connect"
ACCOUNT = "default"
# the longest a token may last for Apple
TOKEN_SECONDS = 20 * 60
KEY_ID = re.compile(r"[A-Z0-9]{8,12}")
ISSUER = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_PAGES = 20
TESTFLIGHT = "com.apple.TestFlight"
# The only writes Mobster makes to Apple, by name: the path, and the JSON:API type the body must carry. Signing the
# helper without Xcode needs these three. Nothing else is writable: `Client.request` refuses every verb but GET.
WRITES = {"register_device": ("/v1/devices", "devices"),
          "create_certificate": ("/v1/certificates", "certificates"),
          "create_profile": ("/v1/profiles", "profiles")}
UNKNOWN_FIX = (
    "Mobster won't send it again. Check developer.apple.com › Certificates, Identifiers & Profiles, then tell "
    "Mobster what you found."
)
# In an approved body, {"$ref": "<name>"} stands for an id Apple hands back earlier in the same run.
REF = "$ref"


class Key:
    def __init__(self, key_id, issuer_id, p8, source):
        self.key_id, self.issuer_id, self.source = key_id, issuer_id or None, source
        self._d = es256.private_key_from_p8(p8)

    def public(self):
        return {"keyId": self.key_id, "issuerId": self.issuer_id, "kind": "team" if self.issuer_id else "individual",
                "source": self.source}

    def token(self, now=None):
        now = int(time.time() if now is None else now)
        claims = {"iat": now, "exp": now + TOKEN_SECONDS, "aud": "appstoreconnect-v1"}
        claims.update({"iss": self.issuer_id} if self.issuer_id else {"sub": "user"})
        return es256.jwt({"alg": "ES256", "kid": self.key_id, "typ": "JWT"}, claims, self._d)


def check_ids(key_id, issuer_id):
    if not KEY_ID.fullmatch(key_id or ""):
        raise DevError(f"{key_id!r} isn't a Key ID: it's 10 letters and digits, such as 2X9R4HXF34.",
                       "Copy it from App Store Connect › Users and Access › Integrations.")
    if issuer_id and not ISSUER.fullmatch(issuer_id.lower()):
        raise DevError("That isn't an Issuer ID: it's a UUID, such as 57246542-96fe-1a63-e053-0824d011072a.",
                       "Copy it from the top of App Store Connect › Users and Access › Integrations.")


def _refused(error, verb, kind=DevError):
    """The plain-language error for an HTTP error from Apple. ``verb`` is "read" or "write"."""
    detail = ""
    try:
        errors = json.loads(error.read().decode("utf-8")).get("errors") or []
        detail = (errors[0].get("detail") or errors[0].get("title") or "") if errors else ""
    except Exception:
        pass
    if error.code == 401:
        return kind("App Store Connect refused the key (401).",
                    "Check the Key ID and Issuer ID, and that the key isn't revoked.")
    if error.code == 403:
        return kind(f"The key can't {'make that change' if verb == 'write' else 'read that'} (403). {detail}".strip(),
                    "Give the key the Admin or Developer role in App Store Connect." if verb == "write" else
                    "Give the key the App Manager or Developer role in App Store Connect.")
    return kind(f"App Store Connect answered {error.code}. {detail}".strip())


def _store(store=None):
    if store is not None:
        return store
    from ..keychain_acl import Store
    return Store(SERVICE)


def account_for(project=None):
    """The Keychain account of a project's key: one per project folder. None is the key every project shares."""
    if project is None:
        return ACCOUNT
    return "project-" + hashlib.sha256(str(Path(project).resolve()).encode()).hexdigest()[:16]


def save_key(key_id, issuer_id, p8_text, *, store=None, project=None):
    check_ids(key_id, issuer_id)
    try:
        key = Key(key_id, issuer_id, p8_text, "Keychain" if project is None else "Keychain (this project)")
        key.token()
    except es256.P8Error as error:
        raise DevError(f"Mobster couldn't read the key: {error}", "Pass the AuthKey_<Key ID>.p8 file Apple gave you.")
    _store(store).set(account_for(project),
                      json.dumps({"keyId": key_id, "issuerId": issuer_id or None, "p8": p8_text.strip()}))
    return key


def remove_key(*, store=None, project=None):
    return _store(store).delete(account_for(project))


def load_key(*, store=None, environ=None, project=None):
    environ = os.environ if environ is None else environ
    if environ.get("MOBSTER_ASC_KEY_ID"):
        text = environ.get("MOBSTER_ASC_KEY", "")
        if not text and environ.get("MOBSTER_ASC_KEY_PATH"):
            try:
                text = Path(environ["MOBSTER_ASC_KEY_PATH"]).expanduser().read_text()
            except OSError:
                raise DevError("MOBSTER_ASC_KEY_PATH names a file Mobster can't read.") from None
        check_ids(environ["MOBSTER_ASC_KEY_ID"], environ.get("MOBSTER_ASC_ISSUER_ID"))
        try:
            return Key(environ["MOBSTER_ASC_KEY_ID"], environ.get("MOBSTER_ASC_ISSUER_ID"), text, "environment")
        except es256.P8Error as error:
            raise DevError(f"MOBSTER_ASC_KEY isn't a usable key: {error}") from None
    try:
        source = "Keychain (this project)"
        stored = _store(store).get(account_for(project)) if project is not None else None
        if not stored:
            source, stored = "Keychain", _store(store).get(ACCOUNT)
    except Exception as error:
        raise DevError(f"The Keychain didn't give Mobster the App Store Connect key ({error}).",
                       "Unlock the Mac's login Keychain, then try again.") from None
    if not stored:
        raise DevError("Mobster has no App Store Connect API key.",
                       "Add one: mobster asc key add --key-id KEY_ID --issuer-id ISSUER_ID --p8 AuthKey_KEY_ID.p8")
    data = json.loads(stored)
    return Key(data["keyId"], data.get("issuerId"), data["p8"], source)


class WriteRejected(DevError):
    """Apple answered a write with an error: nothing was made."""


class WriteUnknown(DevError):
    """A write was sent and Mobster never saw Apple's answer: it may have gone through. It is not sent again."""


def _matches(template, body):
    """Whether ``body`` is ``template``, with an id (a non-empty string) wherever the template has a {"$ref": ...}."""
    if isinstance(template, dict):
        if set(template) == {REF}:
            return isinstance(body, str) and bool(body)
        return isinstance(body, dict) and template.keys() == body.keys() and all(
            _matches(template[key], body[key]) for key in template)
    if isinstance(template, list):
        return isinstance(body, list) and len(template) == len(body) and all(map(_matches, template, body))
    return type(template) is type(body) and template == body


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(name, body):
    """What names one write in the ledger: its name and exact body."""
    return hashlib.sha256(canonical([name, body]).encode()).hexdigest()


class Approval:
    """The writes a person approved on a card: each a write's name and its body, where an id Apple returns earlier in
    the same run stands as {"$ref": name}. ``digest`` is what the card carries; ``Client.write`` sends a body only if it
    is one of these steps, and each step once."""

    def __init__(self, steps):
        self.steps = [(name, body) for name, body in steps]
        for name, body in self.steps:
            if name not in WRITES:
                raise DevError(f"{name} isn't a change Mobster makes to App Store Connect.")
        self.spent = set()

    @property
    def digest(self):
        return hashlib.sha256(canonical(self.steps).encode()).hexdigest()

    def spend(self, name, body):
        """Mark the step this body is as sent. DevError when it isn't an approved step, or was already sent."""
        for index, (step, template) in enumerate(self.steps):
            if step == name and index not in self.spent and _matches(template, body):
                self.spent.add(index)
                return index
        raise DevError("Mobster didn't send that: it isn't a change you approved, or it was already sent.")


class Client:
    """GET requests to the App Store Connect API and the https links in its answers (``download``), and the three
    ``WRITES`` (``write``)."""

    def __init__(self, key, *, opener=None, api=API, clock=time.time):
        self.key, self.opener, self.api, self.clock = key, opener or urllib.request.urlopen, api.rstrip("/"), clock
        self._token, self._expires = None, 0

    def _bearer(self):
        now = self.clock()
        if self._token is None or now > self._expires - 60:
            self._token, self._expires = self.key.token(now), now + TOKEN_SECONDS
        return self._token

    def request(self, method, path, params=None):
        if method != "GET":
            raise DevError(
                "Mobster reads from App Store Connect, and `write` makes its three named changes. It sent nothing."
            )
        if not re.match(r"^/v[12]/[A-Za-z0-9/_\-]+$", path) and not path.startswith(self.api + "/v"):
            raise DevError(f"{path} isn't an App Store Connect API path.")
        url = path if path.startswith("http") else self.api + path
        if params:
            url += "?" + urllib.parse.urlencode(params, safe="[],")
        req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {self._bearer()}",
                                                                  "Accept": "application/json",
                                                                  "User-Agent": "mobster-asc"})
        try:
            with self.opener(req, timeout=30) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as error:
            raise _refused(error, "read") from None
        except (urllib.error.URLError, OSError) as error:
            raise DevError(f"Mobster couldn't reach App Store Connect ({type(error).__name__}).",
                           "Check the Mac's internet connection.") from None

    def write(self, name, body, *, approval, ledger, summary=None):
        """Send one of ``WRITES`` and return Apple's ``data`` ({id, type, attributes}). It sends only a body the
        ``approval`` names, once; the ``ledger`` makes the attempt durable first (begin) and records how it ended (end).
        WriteRejected when Apple answers with an error. WriteUnknown when the answer never arrived: that write is
        never sent again by Mobster, and the ledger refuses it until the person says what happened."""
        if name not in WRITES:
            raise DevError("Mobster doesn't make that change to App Store Connect, so it sent nothing.")
        path, kind = WRITES[name]
        if not isinstance(body, dict) or (body.get("data") or {}).get("type") != kind:
            raise DevError(f"{name} sends a {kind} resource, so Mobster sent nothing.")
        approval.spend(name, body)
        ident = digest(name, body)
        ledger.begin(name, ident, summary or {})
        req = urllib.request.Request(self.api + path, method="POST", data=canonical(body).encode(),
                                     headers={"Authorization": f"Bearer {self._bearer()}",
                                              "Accept": "application/json", "Content-Type": "application/json",
                                              "User-Agent": "mobster-asc"})
        try:
            with self.opener(req, timeout=30) as response:
                landed = getattr(response, "geturl", lambda: req.full_url)()
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            # A 5xx may come after Apple made the change: only a 4xx says nothing was made.
            outcome = "rejected" if error.code < 500 else "unknown"
            refusal = _refused(error, "write", WriteRejected if outcome == "rejected" else WriteUnknown)
            ledger.end(ident, outcome, {"status": error.code})
            raise refusal from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            ledger.end(ident, "unknown", {"error": type(error).__name__})
            raise WriteUnknown(f"Mobster sent {name.replace('_', ' ')} to App Store Connect and never saw its answer "
                               f"({type(error).__name__}), so it may have gone through.", UNKNOWN_FIX) from None
        try:
            data = json.loads(raw)["data"]
            if landed != req.full_url or not isinstance(data, dict) or not data.get("id"):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            ledger.end(ident, "unknown", {"error": "unreadable answer"})
            raise WriteUnknown(f"App Store Connect answered {name.replace('_', ' ')} with something Mobster couldn't "
                               "read, so it may have gone through.", UNKNOWN_FIX) from None
        ledger.end(ident, "done", {"id": data["id"]})
        return data

    def get(self, path, params=None):
        return self.request("GET", path, params)

    def download(self, url, limit=20_000_000):
        # no Authorization header: the key's token never goes to the CDN
        if not url.startswith("https://"):
            raise DevError(f"{url} isn't an https link, so Mobster didn't fetch it.")
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "mobster-asc"})
        try:
            with self.opener(req, timeout=30) as response:
                return response.read(limit)
        except (urllib.error.URLError, OSError) as error:
            raise DevError(f"Mobster couldn't download {url} ({type(error).__name__}).") from None

    def pages(self, path, params=None, limit=None):
        """Every item of a list, following links.next, with ``included`` merged into one lookup."""
        items, included = [], {}
        answer = self.get(path, params)
        for _ in range(MAX_PAGES):
            items += answer.get("data") or []
            for item in answer.get("included") or ():
                included[(item.get("type"), item.get("id"))] = item
            following = (answer.get("links") or {}).get("next")
            if not following or (limit and len(items) >= limit):
                break
            answer = self.get(following)
        return (items[:limit] if limit else items), included

    # -- what Mobster reads ------------------------------------------------------------------------------------

    def app(self, bundle):
        answer = self.get("/v1/apps", {"filter[bundleId]": bundle, "fields[apps]": "name,bundleId,sku"})
        found = [item for item in answer.get("data") or () if (item.get("attributes") or {}).get("bundleId") == bundle]
        if not found:
            raise DevError(f"App Store Connect has no app {bundle} this key can see.",
                           "Check the bundle ID, and that the key's team owns the app.")
        item = found[0]
        return {"id": item["id"], "name": item["attributes"].get("name"), "bundleId": bundle}

    def apps(self):
        items, _ = self.pages("/v1/apps", {"fields[apps]": "name,bundleId", "limit": 200})
        return [{"id": item["id"], "name": (item.get("attributes") or {}).get("name"),
                 "bundleId": (item.get("attributes") or {}).get("bundleId")} for item in items]

    def builds(self, app_id, limit=10):
        items, included = self.pages("/v1/builds", {
            "filter[app]": app_id, "sort": "-uploadedDate", "limit": min(limit, 200),
            "include": "preReleaseVersion,buildBetaDetail",
            "fields[builds]": "version,uploadedDate,processingState,expired,minOsVersion,preReleaseVersion,"
                              "buildBetaDetail",
            "fields[preReleaseVersions]": "version",
            "fields[buildBetaDetails]": "internalBuildState,externalBuildState"}, limit=limit)
        return [build_row(item, included) for item in items]

    def feedback(self, app_id, *, kinds=("screenshots", "crashes"), limit=50):
        out = []
        for kind, path in (("screenshots", "betaFeedbackScreenshotSubmissions"),
                           ("crashes", "betaFeedbackCrashSubmissions")):
            if kind not in kinds:
                continue
            items, included = self.pages(f"/v1/apps/{app_id}/{path}", {"limit": min(limit, 200),
                                                                       "sort": "-createdDate", "include": "build"},
                                         limit=limit)
            out += [feedback_row(item, kind, included) for item in items]
        out.sort(key=lambda row: row.get("created") or "", reverse=True)
        return out[:limit]

    # -- what Mobster reads to sign the helper without Xcode (team_signing) -------------------------------------

    def devices(self):
        """The team's iPhones: id, name, UDID, status."""
        items, _ = self.pages("/v1/devices", {"filter[platform]": "IOS", "limit": 200,
                                              "fields[devices]": "name,udid,status,deviceClass"})
        return [{"id": item["id"], "name": (item.get("attributes") or {}).get("name"),
                 "udid": (item.get("attributes") or {}).get("udid"),
                 "status": (item.get("attributes") or {}).get("status"),
                 "class": (item.get("attributes") or {}).get("deviceClass")} for item in items]

    def bundle_id(self, identifier):
        """The team's registered App ID for ``identifier`` ({id, identifier, name, team}), or None. ``"*"`` is the
        wildcard App ID Xcode keeps for a team; it is found by listing, since a filter can't name it."""
        wildcard = identifier == "*"
        params = {"limit": 200, "fields[bundleIds]": "identifier,name,platform,seedId"}
        if not wildcard:
            params["filter[identifier]"] = identifier
        items, _ = self.pages("/v1/bundleIds", params)
        for item in items:
            attributes = item.get("attributes") or {}
            if attributes.get("identifier") == identifier:
                return {"id": item["id"], "identifier": identifier, "name": attributes.get("name"),
                        "team": attributes.get("seedId")}
        return None

    def team_id(self):
        """The team's 10-character id, from the App ID prefix of any App ID it has; None when it has none."""
        answer = self.get("/v1/bundleIds", {"limit": 1, "fields[bundleIds]": "seedId"})
        for item in answer.get("data") or ():
            seed = (item.get("attributes") or {}).get("seedId")
            if isinstance(seed, str) and re.fullmatch(r"[A-Z0-9]{10}", seed):
                return seed
        return None

    def certificate_content(self, certificate_id):
        """A certificate's DER bytes (base64) from Apple, to recover one Mobster made and did not get to keep."""
        data = self.get(
            f"/v1/certificates/{certificate_id}", {"fields[certificates]": "certificateContent,serialNumber"}
        )
        attributes = (data.get("data") or {}).get("attributes") or {}
        return attributes.get("certificateContent") or "", attributes.get("serialNumber")

    def development_certificates(self):
        """The team's development certificates: id, serial number, expiry."""
        items, _ = self.pages("/v1/certificates", {
            "filter[certificateType]": "DEVELOPMENT,IOS_DEVELOPMENT", "limit": 200,
            "fields[certificates]": "serialNumber,certificateType,displayName,expirationDate,activated"})
        return [{"id": item["id"], "serial": (item.get("attributes") or {}).get("serialNumber"),
                 "name": (item.get("attributes") or {}).get("displayName"),
                 "expires": (item.get("attributes") or {}).get("expirationDate")} for item in items]

    def development_profiles(self, prefix):
        """The team's active iOS development profiles whose name starts with ``prefix``, with the ids of the devices
        and certificates each lists and its App ID."""
        items, _ = self.pages("/v1/profiles", {"filter[profileType]": "IOS_APP_DEVELOPMENT",
                                               "filter[profileState]": "ACTIVE", "limit": 200,
                                               "fields[profiles]": "name,uuid,expirationDate"})
        rows = []
        for item in items:
            attributes = item.get("attributes") or {}
            if not str(attributes.get("name") or "").startswith(prefix):
                continue
            related = {}
            for name in ("devices", "certificates"):
                data, _ = self.pages(f"/v1/profiles/{item['id']}/relationships/{name}", {"limit": 200})
                related[name] = sorted(entry["id"] for entry in data)
            bundle = (self.get(f"/v1/profiles/{item['id']}/relationships/bundleId").get("data") or {}).get("id")
            rows.append({"id": item["id"], "name": attributes.get("name"), "uuid": attributes.get("uuid"),
                         "expires": attributes.get("expirationDate"), "devices": related["devices"],
                         "certificates": related["certificates"], "bundle": bundle})
        return rows

    def profile_content(self, profile_id):
        """A profile's .mobileprovision bytes, from Apple."""
        data = self.get(f"/v1/profiles/{profile_id}", {"fields[profiles]": "profileContent"}).get("data") or {}
        return base64.b64decode(((data.get("attributes") or {}).get("profileContent")) or "")

    def crash_log(self, submission_id):
        answer = self.get(f"/v1/betaFeedbackCrashSubmissions/{submission_id}/crashLog")
        return ((answer.get("data") or {}).get("attributes") or {}).get("logText") or ""


def _related(item, name, included):
    data = ((item.get("relationships") or {}).get(name) or {}).get("data")
    if isinstance(data, dict):
        return included.get((data.get("type"), data.get("id")))
    return None


def build_row(item, included):
    attributes = item.get("attributes") or {}
    version = _related(item, "preReleaseVersion", included)
    beta = _related(item, "buildBetaDetail", included)
    beta_attributes = (beta or {}).get("attributes") or {}
    return {"id": item.get("id"), "version": ((version or {}).get("attributes") or {}).get("version"),
            "build": attributes.get("version"), "uploaded": attributes.get("uploadedDate"),
            "processing": attributes.get("processingState"), "expired": bool(attributes.get("expired")),
            "internal": beta_attributes.get("internalBuildState"),
            "external": beta_attributes.get("externalBuildState")}


def feedback_row(item, kind, included):
    attributes = item.get("attributes") or {}
    build = _related(item, "build", included)
    return {"id": item.get("id"), "kind": "crash" if kind == "crashes" else "screenshot",
            "created": attributes.get("createdDate"), "comment": attributes.get("comment"),
            "device": attributes.get("deviceModel"), "os": attributes.get("osVersion"),
            "build": ((build or {}).get("attributes") or {}).get("version"),
            "screenshots": [shot.get("url") for shot in attributes.get("screenshots") or () if isinstance(shot, dict)
                            and shot.get("url")]}


# -- TestFlight on the phone ---------------------------------------------------------------------------------------

INSTALL_WORDS = ("Install", "Update")
DONE_WORDS = ("Open",)
TERMS_WORDS = ("Accept", "Start Testing")


def testflight_link(apple_id):
    return f"itms-beta://beta.itunes.apple.com/v1/app/{int(apple_id)}"


def _buttons(tree):
    return {node.label: node for node in tree.shown() if node.role == "Button" and node.label}


class UnknownOutcome(DevError):
    """Mobster tapped Install or Update and never saw TestFlight say Open: the install may still finish."""


class NeedsTap(DevError):
    """TestFlight shows Install or Update and Mobster was told not to tap (an earlier tap's outcome is unknown)."""


def testflight_install(record, wda, apple_id, *, timeout=600, clock=time.monotonic, sleep=time.sleep,
                       progress=None, tap=True, expected=None):
    from ..deeplinks import open_link
    from ..phone_io import Calls
    from ..verify.tree import read_tree
    progress = progress or (lambda line: None)
    if record.get("kind") == "simulator":
        raise DevError("TestFlight runs on an iPhone, not a simulator.", "Pass --device with your iPhone's name.")
    with wda(record) as request:
        driver = Calls(request)
        open_link(driver, testflight_link(apple_id), bundle=TESTFLIGHT)
        tapped = False
        deadline = clock() + timeout
        while clock() < deadline:
            try:
                tree = read_tree(driver)
                buttons = _buttons(tree)
            except Exception:
                buttons = {}
            if any(word in buttons for word in TERMS_WORDS) and not tapped:
                raise DevError("TestFlight asks you to accept its terms for this app first, and Mobster doesn't "
                               "accept terms for you.", "Tap Accept on the iPhone once, then run it again.")
            if expected and buttons.keys() & (set(INSTALL_WORDS) | {"Open"}):
                version, build = expected
                label = re.compile(r"(?<![\w.])" + re.escape(str(version)) + r"\s*\(\s*" +
                                   re.escape(str(build)) + r"\s*\)")
                if not any(label.search(node.label or "") for node in tree.shown()):
                    error = UnknownOutcome if tapped else DevError
                    raise error(f"TestFlight hasn't confirmed version {version} ({build}) on this iPhone.",
                                "Open the app's TestFlight page and check its version and build before trying again.")
            if not tapped:
                word = next((word for word in INSTALL_WORDS if word in buttons), None)
                if word and not tap:
                    raise NeedsTap(f"TestFlight shows {word} and Mobster won't tap it: an earlier tap's outcome is "
                                   "unknown, and a second tap could start a second install.",
                                   f"Look at the iPhone. If TestFlight is installing, wait for Open. Otherwise tap "
                                   f"{word} yourself.")
                if word:
                    progress(f"Tapping {word} in TestFlight")
                    tapped = True  # from here a failure may have sent the tap
                    try:
                        _tap(request, word)
                    except DevError:
                        tapped = False  # it never found the button, so nothing was sent
                        raise
                elif "Open" in buttons:
                    return "already"
            elif any(word in buttons for word in DONE_WORDS):
                return "installed"
            sleep(2)
    if tapped:
        raise UnknownOutcome("Mobster tapped Install in TestFlight and didn't see it finish, so the outcome is "
                             "unknown.", "Look at the iPhone before you try again. Mobster won't tap again until "
                             "TestFlight shows Open.")
    raise DevError("TestFlight didn't show Install, Update or Open in time.",
                   "Check the iPhone's network and that you're a tester of the app, then run it again.")


def _tap(request, label):
    found = request("POST", "/element", {"using": "predicate string",
                                         "value": f"type == 'XCUIElementTypeButton' AND label == '{label}'"}, 10)
    element = (found or {}).get("ELEMENT") or (found or {}).get("element-6066-11e4-a52e-4f735466cecf")
    if not element:
        raise DevError(f"Mobster couldn't find {label} in TestFlight.", "Tap it on the iPhone yourself.")
    request("POST", f"/element/{element}/click", {}, 10)
