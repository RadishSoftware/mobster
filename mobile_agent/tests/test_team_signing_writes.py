"""Signing the helper without Xcode, the App Store Connect side: the three writes by name and nothing else, the card
that names exactly what is sent, the ledger that makes each attempt durable and refuses a second try when the first's
outcome is unknown, and a planted key that appears in no card, ledger line, log or message. Recorded answers; no
network, Keychain or phone."""

import contextlib
import io
import json
import logging
import tempfile
import unittest
import urllib.error
from pathlib import Path

from mobile_agent.devkit import DevError, asc
from mobile_agent.journal import JournalError
from mobile_agent.team_signing import ledger as ledger_module
from mobile_agent.team_signing import writes
from mobile_agent.tests.test_devkit_asc import RFC_D, p8_for

TEAM_KEY = "ABCDEFGH12"
ISSUER = "57246542-96fe-1a63-e053-0824d011072a"
PHONE = {"name": "Studio iPhone", "udid": "00008130-000A1B2C3D4E5F60"}
OTHER = {"name": "Lab iPhone", "udid": "00000000-00000000ABCD0001"}
BUNDLE = "app.mobster.wda.runner.test"
CSR = (
    "-----BEGIN CERTIFICATE REQUEST-----\nMIICYjCCAUoCAQAwHTEbMBkGA1UEAwwSTW9ic3RlciBjc3IgZml4dHVyZTCC\n"
    "-----END CERTIFICATE REQUEST-----\n"
)
APP_ID = {"data": [{"id": "BID1", "type": "bundleIds", "attributes": {"identifier": BUNDLE, "name": "Runner"}}]}


def device(id_, item, status="ENABLED"):
    return {
        "id": id_,
        "type": "devices",
        "attributes": {"name": item["name"], "udid": item["udid"], "status": status, "deviceClass": "IPHONE"},
    }


class Apple:
    """A recorded App Store Connect: GET paths answer from ``routes``, POSTs from ``posts`` (path to a callable or an
    exception to raise). Everything it is sent is in ``seen``."""

    def __init__(self, routes=None, posts=None):
        self.routes, self.posts, self.seen = routes or {}, posts or {}, []

    def __call__(self, request, timeout=None):
        path = request.full_url.split("api.appstoreconnect.apple.com", 1)[1].split("?")[0]
        body = json.loads(request.data) if request.data else None
        self.seen.append((request.get_method(), path, body, request.headers.get("Authorization", "")))
        if request.get_method() == "POST":
            answer = self.posts[path]
            if isinstance(answer, BaseException):
                raise answer
            return contextlib.nullcontext(io.BytesIO(json.dumps(answer(body)).encode()))
        return contextlib.nullcontext(io.BytesIO(json.dumps(self.routes[path]).encode()))

    def sent(self):
        return [(path, body) for method, path, body, _ in self.seen if method == "POST"]


def http_error(code, detail="No."):
    return urllib.error.HTTPError(
        "https://api.appstoreconnect.apple.com/v1/x",
        code,
        "x",
        {},
        io.BytesIO(json.dumps({"errors": [{"detail": detail}]}).encode()),
    )


def created(kind, id_, **attributes):
    return lambda body: {"data": {"type": kind, "id": id_, "attributes": attributes}}


def routes(devices=(), certificates=(), profiles=(), bundle=APP_ID):
    out = {
        "/v1/devices": {"data": list(devices)},
        "/v1/bundleIds": bundle,
        "/v1/certificates": {
            "data": [
                {
                    "id": cid,
                    "type": "certificates",
                    "attributes": {"serialNumber": serial, "displayName": "Mobster", "expirationDate": "2027-10-18"},
                }
                for cid, serial in certificates
            ]
        },
        "/v1/profiles": {
            "data": [
                {
                    "id": pid,
                    "type": "profiles",
                    "attributes": {"name": name, "uuid": f"U-{pid}", "expirationDate": "2027-10-18"},
                }
                for pid, name, *_ in profiles
            ]
        },
    }
    for pid, _, bundle_id, certs, devs in profiles:
        out[f"/v1/profiles/{pid}/relationships/devices"] = {"data": [{"type": "devices", "id": d} for d in devs]}
        out[f"/v1/profiles/{pid}/relationships/certificates"] = {
            "data": [{"type": "certificates", "id": c} for c in certs]
        }
        out[f"/v1/profiles/{pid}/relationships/bundleId"] = {"data": {"type": "bundleIds", "id": bundle_id}}
    return out


def client(apple, d=RFC_D):
    return asc.Client(asc.Key(TEAM_KEY, ISSUER, p8_for(d), "test"), opener=apple)


def full_posts():
    return {
        "/v1/devices": created("devices", "D-NEW"),
        "/v1/certificates": created("certificates", "C-NEW", certificateContent="Q0VSVA==", serialNumber="S1"),
        "/v1/profiles": created(
            "profiles", "P-NEW", profileContent="UFJPRg==", uuid="U-NEW", expirationDate="2027-10-18T00:00:00Z"
        ),
    }


def plan(apple, phones=(PHONE,), csr=CSR, serial=None, stamp="20261018-1850"):
    return writes.plan(
        client(apple),
        bundle_identifier=BUNDLE,
        phones=list(phones),
        csr_pem=csr,
        certificate_serial=serial,
        stamp=stamp,
    )


class Directory(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.ledger = ledger_module.Ledger(Path(folder.name) / "team-signing" / "ledger.jsonl")


class AllowlistTests(Directory):
    def test_the_only_writes_are_the_three_by_name(self):
        self.assertEqual(set(asc.WRITES), {"register_device", "create_certificate", "create_profile"})
        self.assertEqual({path for path, _ in asc.WRITES.values()}, {"/v1/devices", "/v1/certificates", "/v1/profiles"})

    def test_every_other_write_is_refused_and_sends_nothing(self):
        apple = Apple()
        api = client(apple)
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            for path in (
                "/v1/devices",
                "/v1/certificates/C1",
                "/v1/profiles/P1",
                "/v1/bundleIds",
                "/v1/apps/1",
                "/v1/builds",
                "/v1/betaTesters",
                "/v1/appStoreVersionSubmissions",
            ):
                with self.assertRaises(DevError, msg=(method, path)):
                    api.request(method, path)
        approval = asc.Approval([("register_device", writes.device_body("x", "y"))])
        for name in (
            "revoke_certificate",
            "delete_certificate",
            "delete_profile",
            "create_bundle_id",
            "update_device",
            "submit_for_review",
            "create_beta_tester",
            "upload_build",
            "delete_device",
            "",
            "request",
        ):
            with self.assertRaises(DevError, msg=name):
                api.write(name, writes.device_body("x", "y"), approval=approval, ledger=self.ledger)
            with self.assertRaises(DevError, msg=name):
                asc.Approval([(name, writes.device_body("x", "y"))])
        self.assertEqual(apple.seen, [])
        self.assertEqual(self.ledger.events(), [])

    def test_a_write_needs_the_exact_approved_body_and_goes_once(self):
        apple = Apple(posts=full_posts())
        api = client(apple)
        body = writes.device_body(PHONE["name"], PHONE["udid"])
        approval = asc.Approval([("register_device", body)])
        for other in (
            writes.device_body(PHONE["name"], OTHER["udid"]),
            writes.device_body("Other", PHONE["udid"]),
            writes.certificate_body(CSR),
            {"data": {"type": "devices"}},
            {},
            None,
        ):
            with self.assertRaises(DevError):
                api.write("register_device", other, approval=approval, ledger=self.ledger)
        with self.assertRaises(DevError):  # the right body under a name the approval doesn't have
            api.write("create_certificate", writes.certificate_body(CSR), approval=approval, ledger=self.ledger)
        self.assertEqual(apple.seen, [])
        self.assertEqual(api.write("register_device", body, approval=approval, ledger=self.ledger)["id"], "D-NEW")
        with self.assertRaises(DevError):
            api.write("register_device", body, approval=approval, ledger=self.ledger)
        self.assertEqual(len(apple.sent()), 1)

    def test_the_resource_type_must_match_the_write(self):
        apple = Apple(posts=full_posts())
        body = writes.certificate_body(CSR)
        approval = asc.Approval([("register_device", body)])
        with self.assertRaises(DevError):
            client(apple).write("register_device", body, approval=approval, ledger=self.ledger)
        self.assertEqual(apple.seen, [])


class PlanTests(Directory):
    def test_a_new_team_needs_all_three_and_the_card_names_each_request(self):
        apple = Apple(routes())
        card = plan(apple)
        self.assertEqual(
            [step.write for step in card.steps], ["register_device", "create_certificate", "create_profile"]
        )
        shown = card.public(key={"keyId": TEAM_KEY, "kind": "team"})
        self.assertEqual(
            [step["url"] for step in shown["steps"]],
            [
                "https://api.appstoreconnect.apple.com/v1/devices",
                "https://api.appstoreconnect.apple.com/v1/certificates",
                "https://api.appstoreconnect.apple.com/v1/profiles",
            ],
        )
        self.assertEqual(
            shown["steps"][0]["body"]["data"]["attributes"],
            {"name": PHONE["name"], "platform": "IOS", "udid": PHONE["udid"]},
        )
        self.assertEqual(shown["steps"][1]["body"]["data"]["attributes"]["csrContent"], CSR)
        profile = shown["steps"][2]["body"]["data"]
        self.assertEqual(profile["attributes"]["profileType"], "IOS_APP_DEVELOPMENT")
        self.assertEqual(profile["relationships"]["bundleId"]["data"], {"type": "bundleIds", "id": "BID1"})
        self.assertIn("certificate", json.dumps(profile["relationships"]["certificates"]))
        self.assertTrue(all(method == "GET" for method, *_ in apple.seen))

    def test_execute_sends_the_card_in_order_with_apples_ids_filled_in(self):
        apple = Apple(routes(), full_posts())
        card = plan(apple)
        answers = writes.execute(client(apple), card, card.digest, self.ledger)
        sent = apple.sent()
        self.assertEqual([path for path, _ in sent], ["/v1/devices", "/v1/certificates", "/v1/profiles"])
        relationships = sent[2][1]["data"]["relationships"]
        self.assertEqual(relationships["certificates"]["data"], [{"type": "certificates", "id": "C-NEW"}])
        self.assertEqual(relationships["devices"]["data"], [{"type": "devices", "id": "D-NEW"}])
        self.assertEqual(answers["certificate"]["attributes"]["certificateContent"], "Q0VSVA==")
        self.assertEqual(
            [event["event"] for event in self.ledger.events()], ["intent", "done", "intent", "done", "intent", "done"]
        )
        self.assertEqual(self.ledger.unresolved(), [])

    def test_nothing_is_sent_unless_the_card_that_was_approved_is_the_card_sent(self):
        apple = Apple(routes(), full_posts())
        card = plan(apple)
        for approved in ("", "0" * 64, plan(apple, phones=(PHONE, OTHER)).digest):
            with self.assertRaises(DevError):
                writes.execute(client(apple), card, approved, self.ledger)
        self.assertEqual(apple.sent(), [])

    def test_what_apple_already_has_is_read_not_written(self):
        ready = routes(
            devices=[device("D1", PHONE)],
            certificates=[("C1", "S1")],
            profiles=[("P1", f"Mobster helper {BUNDLE} 20261001-0900", "BID1", ["C1"], ["D1"])],
        )
        card = plan(Apple(ready), serial="S1", csr=None)
        self.assertFalse(card.needed)
        self.assertEqual(card.profile["id"], "P1")
        self.assertEqual(card.devices, {PHONE["udid"]: "D1"})

    def test_a_second_phone_makes_a_new_profile_that_lists_both(self):
        state = routes(
            devices=[device("D1", PHONE)],
            certificates=[("C1", "S1")],
            profiles=[("P1", f"Mobster helper {BUNDLE} 20261001-0900", "BID1", ["C1"], ["D1"])],
        )
        apple = Apple(state, {"/v1/devices": created("devices", "D2"), "/v1/profiles": created("profiles", "P2")})
        card = plan(apple, phones=(PHONE, OTHER), serial="S1", csr=None)
        self.assertEqual([step.write for step in card.steps], ["register_device", "create_profile"])
        writes.execute(client(apple), card, card.digest, self.ledger)
        listed = apple.sent()[1][1]["data"]["relationships"]["devices"]["data"]
        self.assertEqual({item["id"] for item in listed}, {"D1", "D2"})
        self.assertEqual(apple.sent()[1][1]["data"]["relationships"]["certificates"]["data"][0]["id"], "C1")

    def test_a_phone_another_profile_lists_stays_when_a_new_profile_is_made(self):
        state = routes(
            devices=[device("D1", PHONE), device("D9", OTHER)],
            certificates=[("C1", "S1")],
            profiles=[("P1", f"Mobster helper {BUNDLE} 20261001-0900", "BID1", ["C1"], ["D1", "D9"])],
        )
        apple = Apple(state, {"/v1/devices": created("devices", "D3"), "/v1/profiles": created("profiles", "P2")})
        third = {"name": "Third", "udid": "00008030-000000000000000E"}
        card = plan(apple, phones=(PHONE, third), serial="S1", csr=None)
        writes.execute(client(apple), card, card.digest, self.ledger)
        listed = apple.sent()[-1][1]["data"]["relationships"]["devices"]["data"]
        self.assertEqual({item["id"] for item in listed}, {"D1", "D3", "D9"})

    def test_a_revoked_certificate_is_made_again(self):
        state = routes(devices=[device("D1", PHONE)], certificates=[("C7", "OTHER")])
        card = plan(Apple(state), serial="S1")
        self.assertEqual([step.write for step in card.steps], ["create_certificate", "create_profile"])

    def test_an_app_id_that_isnt_on_the_team_is_said_not_registered(self):
        with self.assertRaises(DevError) as caught:
            plan(Apple(routes(bundle={"data": []})))
        self.assertIn("Mobster doesn't register App IDs", caught.exception.message)
        self.assertIn("Identifiers", caught.exception.fix)

    def test_a_disabled_phone_is_not_re_enabled(self):
        with self.assertRaises(DevError) as caught:
            plan(Apple(routes(devices=[device("D1", PHONE, "DISABLED")])))
        self.assertIn("disabled", caught.exception.message)

    def test_a_certificate_needs_a_request(self):
        with self.assertRaises(DevError):
            plan(Apple(routes(devices=[device("D1", PHONE)])), csr=None)


class OutcomeTests(Directory):
    def card(self, posts, **state):
        apple = Apple(routes(devices=[device("D1", PHONE)], **state), posts)
        return apple, plan(apple)

    def test_an_answer_that_never_arrived_is_never_sent_again(self):
        apple, card = self.card(
            {"/v1/certificates": TimeoutError("timed out"), "/v1/profiles": created("profiles", "P")}
        )
        with self.assertRaises(asc.WriteUnknown) as caught:
            writes.execute(client(apple), card, card.digest, self.ledger)
        self.assertIn("won't send it again", caught.exception.fix)
        self.assertEqual(self.ledger.events()[-1]["event"], "unknown")
        self.assertEqual(len(apple.sent()), 1)
        # A new run of the same card is refused before anything goes out.
        again = plan(apple)
        with self.assertRaises(asc.WriteUnknown):
            writes.execute(client(apple), again, again.digest, self.ledger)
        self.assertEqual(len(apple.sent()), 1)
        # The person looked at Apple and it wasn't made: now it may go once more.
        [open_write] = self.ledger.unresolved()
        self.assertEqual(open_write["write"], "create_certificate")
        self.ledger.resolve(open_write["digest"], made=False)
        apple.posts["/v1/certificates"] = created("certificates", "C-OK", certificateContent="Q0VSVA==")
        writes.execute(client(apple), again, again.digest, self.ledger)
        self.assertEqual(len(apple.sent()), 3)

    def test_a_made_change_the_person_confirms_is_not_sent_again(self):
        apple, card = self.card({"/v1/certificates": ConnectionResetError()})
        with self.assertRaises(asc.WriteUnknown):
            writes.execute(client(apple), card, card.digest, self.ledger)
        self.ledger.resolve(self.ledger.unresolved()[0]["digest"], made=True)
        with self.assertRaises(DevError) as caught:
            writes.execute(client(apple), card, card.digest, self.ledger)
        self.assertIn("already made", caught.exception.message)
        self.assertEqual(len(apple.sent()), 1)

    def test_a_server_error_may_have_gone_through_and_a_client_error_did_not(self):
        apple, card = self.card({"/v1/certificates": http_error(503), "/v1/profiles": created("profiles", "P")})
        with self.assertRaises(asc.WriteUnknown):
            writes.execute(client(apple), card, card.digest, self.ledger)
        self.assertEqual(self.ledger.events()[-1]["event"], "unknown")
        apple2, card2 = self.card({"/v1/certificates": http_error(409, "A certificate already exists.")})
        ledger2 = ledger_module.Ledger(self.ledger.path.with_name("two.jsonl"))
        with self.assertRaises(asc.WriteRejected) as caught:
            writes.execute(client(apple2), card2, card2.digest, ledger2)
        self.assertIn("already exists", caught.exception.message)
        self.assertEqual(ledger2.events()[-1]["event"], "rejected")
        self.assertEqual(ledger2.unresolved(), [])
        apple2.posts["/v1/certificates"] = created("certificates", "C-OK")  # a rejected write may be tried again
        apple2.posts["/v1/profiles"] = created("profiles", "P")
        writes.execute(client(apple2), card2, card2.digest, ledger2)

    def test_a_key_without_the_role_is_told_which(self):
        apple, card = self.card({"/v1/certificates": http_error(403, "Forbidden request")})
        with self.assertRaises(asc.WriteRejected) as caught:
            writes.execute(client(apple), card, card.digest, self.ledger)
        self.assertIn("Admin or Developer", caught.exception.fix)

    def test_an_attempt_that_crashed_before_its_answer_blocks_a_second_one(self):
        apple, card = self.card(
            {"/v1/certificates": created("certificates", "C"), "/v1/profiles": created("profiles", "P")}
        )
        body = writes.certificate_body(CSR)
        self.ledger.begin("create_certificate", asc.digest("create_certificate", body), {})  # then the process died
        with self.assertRaises(asc.WriteUnknown):
            writes.execute(client(apple), card, card.digest, self.ledger)
        self.assertEqual(apple.sent(), [])

    def test_a_ledger_that_cannot_be_saved_sends_nothing(self):
        apple, card = self.card({"/v1/certificates": created("certificates", "C")})
        self.ledger.path.mkdir(parents=True)  # a folder where the file goes
        with self.assertRaises((JournalError, DevError)):
            writes.execute(client(apple), card, card.digest, self.ledger)
        self.assertEqual(apple.sent(), [])

    def test_a_damaged_ledger_sends_nothing(self):
        apple, card = self.card({"/v1/certificates": created("certificates", "C")})
        self.ledger.path.parent.mkdir(parents=True)
        self.ledger.path.write_text("{not json\n")
        with self.assertRaises(DevError):
            writes.execute(client(apple), card, card.digest, self.ledger)
        self.assertEqual(apple.sent(), [])


class SecretTests(Directory):
    """The key's text, its signing scalar and the token it makes appear in the Authorization header and nowhere else."""

    def test_a_planted_key_is_in_no_card_ledger_log_or_message(self):
        planted = 0xA11CE5ECBEEF5EC4E7C0FFEE5AFE17D0C0DEFACE00DDBA11D00D1E5EED15EA5
        text = p8_for(planted)
        body_text = "".join(text.splitlines()[1:-1])
        apple = Apple(routes(), full_posts())
        api = asc.Client(asc.Key(TEAM_KEY, ISSUER, text, "test"), opener=apple)
        records, handler = [], logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        before = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(before)))
        card = writes.plan(api, bundle_identifier=BUNDLE, phones=[PHONE], csr_pem=CSR, stamp="20261018-1850")
        writes.execute(api, card, card.digest, self.ledger)
        failing = Apple(routes(devices=[device("D1", PHONE)]), {"/v1/certificates": TimeoutError("late")})
        api2 = asc.Client(asc.Key(TEAM_KEY, None, text, "test"), opener=failing)
        card2 = writes.plan(
            api2, bundle_identifier=BUNDLE, phones=[PHONE], csr_pem=CSR.replace("MIIC", "MIID"), stamp="20261018-1851"
        )
        with self.assertRaises(asc.WriteUnknown) as caught:
            writes.execute(api2, card2, card2.digest, self.ledger)
        tokens = {auth.split(" ", 1)[1] for *_, auth in apple.seen + failing.seen if auth}
        self.assertTrue(tokens)
        bodies = json.dumps([body for _, _, body, _ in apple.seen + failing.seen])
        haystacks = [
            self.ledger.path.read_text(),
            json.dumps(card.public(key=api.key.public())),
            json.dumps(card2.public()),
            caught.exception.line(),
            "\n".join(record.getMessage() for record in records),
            bodies,
        ]
        for haystack in haystacks:
            for secret in (body_text, text.strip(), hex(planted)[2:], str(planted), *tokens, "PRIVATE KEY"):
                self.assertNotIn(secret, haystack)

    def test_the_ledger_records_no_csr_certificate_or_profile(self):
        apple = Apple(routes(), full_posts())
        card = plan(apple)
        writes.execute(client(apple), card, card.digest, self.ledger)
        text = self.ledger.path.read_text()
        for private in (CSR.splitlines()[1], "Q0VSVA==", "UFJPRg==", "BEGIN CERTIFICATE REQUEST"):
            self.assertNotIn(private, text)
        self.assertIn(PHONE["udid"], text)
        self.assertEqual(oct(self.ledger.path.stat().st_mode & 0o777), "0o600")


class RevokeScriptTests(Directory):
    """scripts/revoke_test_certificate.py: the one DELETE, kept out of Mobster's client."""

    def load(self):
        import importlib.util

        path = Path(__file__).resolve().parents[2] / "scripts" / "revoke_test_certificate.py"
        spec = importlib.util.spec_from_file_location("revoke_test_certificate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def made(self, certificate_id="C-MADE"):
        digest = asc.digest("create_certificate", writes.certificate_body(CSR))
        self.ledger.begin("create_certificate", digest, {"csr_sha256": "x"})
        self.ledger.end(digest, "done", {"id": certificate_id})

    def test_it_revokes_only_a_certificate_the_ledger_made_and_only_with_yes(self):
        script = self.load()
        self.made()
        seen = []

        class Deleted:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout=None):
            seen.append((request.get_method(), request.full_url))
            return Deleted()

        class Store:
            items = {}

            def get(self, account):
                return None

            def delete(self, account):
                return False

        key = lambda: asc.Key(TEAM_KEY, ISSUER, p8_for(RFC_D), "test")  # noqa: E731
        folder = self.ledger.path.parent.parent
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                script.main(
                    ["C-OTHER", "--data-dir", str(folder), "--yes"], load_key=key, opener=opener, store=Store()
                ),
                3,
            )
            self.assertEqual(
                script.main(["C-MADE", "--data-dir", str(folder)], load_key=key, opener=opener, store=Store()), 0
            )
        self.assertEqual(seen, [])
        self.assertIn("Add --yes", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(
                script.main(["C-MADE", "--data-dir", str(folder), "--yes"], load_key=key, opener=opener, store=Store()),
                0,
            )
        self.assertEqual(seen, [("DELETE", "https://api.appstoreconnect.apple.com/v1/certificates/C-MADE")])
        self.assertNotIn("BEGIN", out.getvalue())

    def test_mobsters_own_client_still_cannot_delete(self):
        apple = Apple()
        with self.assertRaises(DevError):
            client(apple).request("DELETE", "/v1/certificates/C-MADE")
        self.assertEqual(apple.seen, [])


if __name__ == "__main__":
    unittest.main()
