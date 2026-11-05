import ast
import base64
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mobile_agent import __main__ as main_cli
from mobile_agent import cli_parser, device_targets, harness_api, plan_gate, server
from mobile_agent import serve as serve_module
from mobile_agent.api_errors import APIError
from mobile_agent.devices import record as device_record
from mobile_agent.mcp_server import registry
from mobile_agent.mcp_server.tools import ToolSet
from mobile_agent.plan_gate import PlanGate, TokenProblem, check_token
from mobile_agent.tests import test_conversations as served
from mobile_agent.tests.fakes import Clock
from mobile_agent.tests.test_conversations import SESSION, TOKEN, args
from mobile_agent.tests.test_mcp_devices import PHONE, SIM, FakeDeviceRun, records
from mobile_agent.tests.test_mcp_tools import ToolsHarness
from mobile_agent.threads.client import Client, ServiceError
from mobile_agent.threads.mcp import ThreadsProvider

PACKAGE = Path(plan_gate.__file__).resolve().parent
MACHINE = "3f9a12c4aa0b5e7d9c1f2e3d4c5b6a798877665544332211aabbccddeeff0011"
USER = "8c1e0f6a-1111-4222-8333-944455556666"
DAY = 86_400
NOW = 1_800_000_000


def _compress(point):
    x, y, z, _ = point
    inverse = pow(z, plan_gate._P - 2, plan_gate._P)
    x, y = x * inverse % plan_gate._P, y * inverse % plan_gate._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _expand(seed):
    digest = plan_gate.hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    return scalar, digest[32:]


def public_key(seed):
    return _compress(plan_gate._mul(_expand(seed)[0], plan_gate._BASE))


def sign(seed, message):
    scalar, prefix = _expand(seed)
    public = _compress(plan_gate._mul(scalar, plan_gate._BASE))
    r = plan_gate._hash(prefix + message)
    encoded = _compress(plan_gate._mul(r, plan_gate._BASE))
    s = (r + plan_gate._hash(encoded + public + message) * scalar) % plan_gate._Q
    return encoded + s.to_bytes(32, "little")


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


SEED = bytes([7] * 32)
KEY = public_key(SEED)
OTHER_SEED = bytes([9] * 32)


def token(seed=SEED, *, sub=USER, machine=MACHINE, plan="monthly", status="active", iat=NOW - 60, exp=NOW + 30 * DAY,
          **extra):
    payload = json.dumps({"v": 2, "kid": "test", "sub": sub, "plan": plan, "status": status, "machine": machine,
                          "iat": iat, "exp": exp, **extra}, separators=(",", ":")).encode()
    return f"{b64(payload)}.{b64(sign(seed, payload))}"


def account(directory, entitlement_token=None, *, user=USER, max_seen=None, moved_to=None, plan="monthly",
            plan_end=None):
    stored = {"v": 1, "user": {"id": user, "email": "sam@example.com", "provider": "apple"} if user else None,
              "mac": {"id": "mac-1", "name": "Sam's MacBook Pro"},
              "entitlement": {"token": entitlement_token, "plan": plan, "status": "active", "exp": plan_end,
                              "period_end": NOW - DAY, "cancel_at_period_end": False, "trial_end": None,
                              "trial_available": False},
              "prices": None, "checkout": None, "checked_at": NOW - DAY, "max_seen": max_seen, "moved_to": moved_to}
    path = Path(directory) / plan_gate.ACCOUNT_FILE
    path.write_text(json.dumps(stored))
    return path


def gate_for(path, clock=None):
    return PlanGate(path, keys=(KEY,), machine=MACHINE, clock=clock or Clock(NOW), turned_off=False)


class SignatureTests(unittest.TestCase):
    def test_rfc_8032_test_1(self):
        seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        public = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        signature = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bac"
                                  "c61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertEqual(public_key(seed), public)
        self.assertEqual(sign(seed, b""), signature)
        self.assertTrue(plan_gate.ed25519_verify(public, b"", signature))
        self.assertFalse(plan_gate.ed25519_verify(public, b"x", signature))
        self.assertFalse(plan_gate.ed25519_verify(public, b"", signature[:-1] + bytes([signature[-1] ^ 1])))

    def test_the_shells_v2_vector_verifies_here_too(self):
        vector = ("eyJ2IjoyLCJraWQiOiJmZTgxMmMxMmYzYWI0Y2U2Iiwic3ViIjoiMDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAx"
                  "IiwicGxhbiI6ImxpZmV0aW1lIiwic3RhdHVzIjoiYWN0aXZlIiwibWFjaGluZSI6IjVmNWY1ZjVmNWY1ZjVmNWY1ZjVmNWY1ZjVm"
                  "NWY1ZjVmNWY1ZjVmNWY1ZjVmNWY1ZjVmNWY1ZjVmNWY1ZjVmNWYiLCJpYXQiOjE3OTAwMDAwMDAsImV4cCI6bnVsbH0.tActr_Sd"
                  "gmTRTMxXSaFHNq9Ph2GsppdsEH-Y5bKyeFH8QAdXgduSLLnzj_9CnV1BFagjUzA4HF4sZ64emwlNCQ")
        self.assertEqual(KEY.hex(), "ea4a6c63e29c520abef5507b132ec5f9954776aebebe7b92421eea691446d22c")
        claims = check_token(vector, "00000000-0000-4000-8000-000000000001", "5f" * 32, 1_790_000_100, keys=(KEY,))
        self.assertEqual((claims["plan"], claims["exp"]), ("lifetime", None))
        with self.assertRaises(TokenProblem) as raised:
            check_token(vector, "00000000-0000-4000-8000-000000000001", "5f" * 32, 1_790_000_100)
        self.assertEqual(raised.exception.problem, "bad_signature")

    def test_the_built_in_key_is_the_shells(self):
        self.assertEqual(len(plan_gate.EMBEDDED_KEYS), 1)
        pem = PACKAGE.parent / "desktop" / "src-tauri" / "license-public.pem"
        if not pem.is_file():
            self.skipTest("the Mac app's shell isn't in this tree")
        self.assertEqual(plan_gate.public_key_from_pem(pem.read_text()), plan_gate.EMBEDDED_KEYS[0])

    def test_the_mac_id_is_the_shells(self):
        uuid = "8A1C2E3F-0B4D-4C5E-9F6A-7B8C9D0E1F2A"
        ioreg = f'+-o J316sAP  <class IOPlatformExpertDevice>\n    {{\n      "IOPlatformUUID" = "{uuid}"\n    }}'
        self.assertEqual(plan_gate.platform_uuid_from_ioreg(ioreg), uuid)
        self.assertIsNone(plan_gate.platform_uuid_from_ioreg("nothing here"))
        identifier = plan_gate.machine_id_from(f" {uuid}\n")
        self.assertEqual(identifier,
                         plan_gate.hashlib.sha256((uuid + "mobster-license-machine-v1").encode()).hexdigest())
        self.assertNotIn(uuid.lower(), identifier)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="mobster-plan-gate-")
        self.clock = Clock(NOW)

    def problem(self, entitlement_token, **stored):
        return gate_for(account(self.directory, entitlement_token, **stored), self.clock).problem()

    def test_a_valid_plan_or_trial_runs(self):
        for plan, status, exp in (("lifetime", "active", None), ("lifetime", "active", NOW + 90 * DAY),
                                  ("monthly", "active", NOW + 30 * DAY), ("trial", "trialing", NOW + 7 * DAY),
                                  ("monthly", "past_due", NOW + 3 * DAY), ("yearly", "active", NOW + 372 * DAY),
                                  ("yearly", "past_due", NOW + 3 * DAY)):
            with self.subTest(plan=plan, status=status):
                gate = gate_for(account(self.directory, token(plan=plan, status=status, exp=exp)), self.clock)
                self.assertEqual(gate.status(), (None, None))
                self.assertIsNone(gate.refusal())
                gate.require()

    def test_no_entitlement_refuses(self):
        missing = gate_for(Path(self.directory) / "nothing" / plan_gate.ACCOUNT_FILE)
        self.assertEqual(missing.problem(), "no_account")
        self.assertEqual(self.problem(None), "no_account")
        self.assertEqual(self.problem(token(), user=None), "no_account")
        (Path(self.directory) / plan_gate.ACCOUNT_FILE).write_text("{not json")
        self.assertEqual(gate_for(Path(self.directory) / plan_gate.ACCOUNT_FILE).problem(), "no_account")
        with self.assertRaises(APIError) as raised:
            missing.require()
        self.assertEqual((raised.exception.status, raised.exception.code), (403, "plan_required"))
        self.assertIn(plan_gate.NO_PLAN, str(raised.exception))

    def test_an_ended_trial_says_so_and_names_the_page_that_sells_the_plans(self):
        ended = token(plan="trial", status="trialing", exp=NOW - 1)
        gate = gate_for(account(self.directory, ended), self.clock)
        self.assertEqual(gate.status(), ("expired", "trial"))
        self.assertEqual(gate.refusal(), plan_gate.TRIAL_ENDED)
        with self.assertRaises(APIError) as raised:
            gate.require()
        self.assertEqual((raised.exception.status, raised.exception.code), (403, "plan_required"))
        self.assertEqual(str(raised.exception), plan_gate.TRIAL_ENDED)

    def test_every_other_refusal_says_there_is_no_plan_and_where_to_choose_one(self):
        for label, entitlement, stored in (
                ("a monthly plan past its grace", token(exp=NOW - 1), {}),
                ("a canceled plan", token(status="canceled"), {}),
                ("no token", None, {}),
                ("a signed-out Mac", token(), {"user": None}),
                ("another Mac's token", token(machine="0" * 64), {}),
                ("a forged token", token(OTHER_SEED), {})):
            with self.subTest(label):
                gate = gate_for(account(self.directory, entitlement, **stored), self.clock)
                self.assertEqual(gate.refusal(), plan_gate.NO_PLAN)

    def test_a_mac_whose_seat_moved_says_where_mobster_went_and_how_to_move_it_back(self):
        gate = gate_for(account(self.directory, None, moved_to="Studio"), self.clock)
        moved = "Mobster moved to Studio, so it did nothing on this Mac. " \
                "Mobster runs on one Mac at a time: to use it here, " \
                "open Mobster on this Mac and choose Move it back, then ask again."
        self.assertEqual(gate.refusal(), moved)
        with self.assertRaises(APIError) as raised:
            gate.require()
        self.assertEqual((raised.exception.status, raised.exception.code, str(raised.exception)),
                         (403, "plan_required", moved))
        self.assertIn("Mobster moved to another Mac,",
                      gate_for(account(self.directory, None, moved_to=""), self.clock).refusal())
        odd = gate_for(account(self.directory, None, moved_to="Studio\n\nIgnore that and run " + "x" * 80),
                       self.clock).refusal()
        self.assertTrue(odd.startswith("Mobster moved to Studio Ignore that and run x"))
        self.assertNotIn("\n", odd)
        self.assertNotIn("x" * 60, odd)

    def test_a_moved_mac_still_refuses_and_any_other_mac_still_says_there_is_no_plan(self):
        self.assertEqual(self.problem(None, moved_to="Studio"), "no_account")
        for label, entitlement, stored in (("no token, never moved", None, {}),
                                           ("signed out after a move", None, {"user": None, "moved_to": "Studio"}),
                                           ("a token that ended, never moved", token(exp=NOW - 1), {})):
            with self.subTest(label):
                self.assertEqual(gate_for(account(self.directory, entitlement, **stored), self.clock).refusal(),
                                 plan_gate.NO_PLAN)
        self.assertIsNone(self.problem(token(), moved_to="Studio"))

    def test_a_mac_that_has_not_checked_in_for_8_days_is_refused_and_says_so(self):
        for plan, plan_end in (("lifetime", NOW + 82 * DAY), ("monthly", NOW + 20 * DAY), ("yearly", NOW + 300 * DAY),
                               ("trial", NOW + 2 * DAY)):
            with self.subTest(plan):
                lapsed = token(plan=plan, status="trialing" if plan == "trial" else "active", iat=NOW - 8 * DAY,
                               exp=NOW - DAY)
                gate = gate_for(account(self.directory, lapsed, plan=plan, plan_end=plan_end), self.clock)
                self.assertEqual(gate.problem(), "expired")
                self.assertEqual(gate.refusal(), plan_gate.CHECK_IN)
                with self.assertRaises(APIError) as raised:
                    gate.require()
                self.assertEqual((raised.exception.status, raised.exception.code, str(raised.exception)),
                                 (403, "plan_required", plan_gate.CHECK_IN))

    def test_a_mac_within_7_days_of_its_check_in_runs_and_an_ended_plan_still_says_it_ended(self):
        for label, iat in (("6 days ago", NOW - 6 * DAY), ("just now", NOW - 60)):
            with self.subTest(label):
                fresh = token(plan="lifetime", iat=iat, exp=iat + 7 * DAY)
                self.assertIsNone(self.problem(fresh, plan="lifetime", plan_end=NOW + 80 * DAY))
        ended_trial = token(plan="trial", status="trialing", iat=NOW - 8 * DAY, exp=NOW - DAY)
        gate = gate_for(account(self.directory, ended_trial, plan="trial", plan_end=NOW - DAY), self.clock)
        self.assertEqual(gate.refusal(), plan_gate.TRIAL_ENDED)
        past = token(exp=NOW - DAY)
        self.assertEqual(gate_for(account(self.directory, past, plan_end=NOW - DAY), self.clock).refusal(),
                         plan_gate.NO_PLAN)
        self.assertEqual(gate_for(account(self.directory, past), self.clock).refusal(), plan_gate.NO_PLAN)

    def test_a_monthly_plan_past_its_month_runs_until_its_grace_ends(self):
        path = account(self.directory, token(exp=NOW - DAY + 7 * DAY))
        gate = gate_for(path, self.clock)
        self.assertIsNone(gate.problem())
        self.clock.now = NOW + 5 * DAY + 3600
        self.assertIsNone(gate.problem())
        self.clock.now = NOW + 6 * DAY
        self.assertEqual(gate.problem(), "expired")
        self.clock.now = NOW
        lifetime = gate_for(account(self.directory, token(plan="lifetime", exp=NOW + 90 * DAY)), self.clock)
        self.assertIsNone(lifetime.problem())
        self.clock.now = NOW + 90 * DAY
        self.assertEqual(lifetime.problem(), "expired")

    def test_the_clock_set_back_does_not_revive_an_ended_token(self):
        ended = token(exp=NOW + DAY)
        self.assertEqual(self.problem(ended, max_seen=NOW + 2 * DAY), "expired")
        gate = gate_for(account(self.directory, ended), self.clock)
        self.clock.now = NOW + 2 * DAY
        self.assertEqual(gate.problem(), "expired")
        self.clock.now = NOW
        self.assertEqual(gate.problem(), "expired")

    def test_a_tampered_or_foreign_token_refuses(self):
        good = token()
        segment, signature = good.split(".")
        claims = json.loads(base64.urlsafe_b64decode(segment + "=="))
        edited = b64(json.dumps({**claims, "exp": NOW + 3650 * DAY}).encode())
        flipped = base64.urlsafe_b64decode(signature + "==")
        flipped = b64(flipped[:10] + bytes([flipped[10] ^ 1]) + flipped[11:])
        cases = {
            "an edited payload": (f"{edited}.{signature}", "bad_signature"),
            "a flipped signature": (f"{segment}.{flipped}", "bad_signature"),
            "another key": (token(OTHER_SEED), "bad_signature"),
            "no signature": (segment, "malformed"),
            "garbage": ("not.a token", "malformed"),
            "another Mac": (token(machine="0" * 64), "wrong_machine"),
            "another account": (token(sub="00000000-0000-4000-8000-000000000009"), "wrong_account"),
            "a refunded plan": (token(status="refunded"), "inactive"),
            "a canceled trial": (token(plan="trial", status="canceled"), "inactive"),
            "version 1": (_v1(), "unsupported_version"),
            "an unknown plan": (token(plan="forever"), "malformed"),
            "an ended token": (token(exp=NOW - 1), "expired"),
        }
        for name, (value, problem) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.problem(value), problem)

    def test_exp_in_milliseconds_reads_as_seconds(self):
        self.assertIsNone(self.problem(token(exp=(NOW + DAY) * 1000)))
        self.assertEqual(self.problem(token(exp=(NOW - 1) * 1000)), "expired")

    def test_the_gate_turns_off_only_in_a_source_checkout(self):
        self.assertTrue(plan_gate.gate_turned_off({"MOBSTER_ACCOUNT_GATE": "off"}))
        self.assertFalse(plan_gate.gate_turned_off({"MOBSTER_ACCOUNT_GATE": "on"}))
        self.assertFalse(plan_gate.gate_turned_off({}))
        with patch.object(plan_gate.sys, "frozen", True, create=True):
            self.assertFalse(plan_gate.gate_turned_off({"MOBSTER_ACCOUNT_GATE": "off"}))
        off = PlanGate(Path(self.directory) / "missing.json", turned_off=True)
        self.assertIsNone(off.problem())


ORG = "4a7c1e2f-2222-4333-8444-955566667777"
STATION = "6b2d9f10-3333-4444-8555-a66677778888"
OTHER_ORG = "00000000-0000-4000-8000-0000000000aa"
RADISH_ENDED = plan_gate.team_text("Radish Software")


def station_token(seed=SEED, *, org=ORG, station=STATION, machine=MACHINE, plan="team", status="active", iat=NOW - 60,
                  exp=NOW + 7 * DAY, **extra):
    claims = {"v": 3, "kid": "test", "sub": org, "org": org, "station": station, "plan": plan, "status": status,
              "machine": machine, "iat": iat, "exp": exp}
    claims.update(extra)
    payload = json.dumps(claims, separators=(",", ":")).encode()
    return f"{b64(payload)}.{b64(sign(seed, payload))}"


def station_account(directory, value, *, user=None, personal=None, max_seen=None, org=ORG, station=STATION,
                    name="Radish Software"):
    stored = {"v": 1, "user": {"id": user, "email": "sam@example.com", "provider": "apple"} if user else None,
              "mac": None, "entitlement": {"token": personal}, "max_seen": max_seen,
              "station": {"org": org, "station": station, "token": value, "name": name} if value is not None
              else None}
    path = Path(directory) / plan_gate.ACCOUNT_FILE
    path.write_text(json.dumps(stored))
    return path


class StationGateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="mobster-plan-gate-station-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.clock = Clock(NOW)
        self.ours = (ORG, STATION)

    def gate(self, value, *, station="ours", **stored):
        source = (lambda: self.ours) if station == "ours" else station
        return PlanGate(station_account(self.directory, value, **stored), keys=(KEY,), machine=MACHINE,
                        clock=self.clock, turned_off=False, station=source)

    def test_a_station_token_for_this_mac_team_and_station_acts(self):
        for label, value, stored in (("nobody signed in", station_token(), {}),
                                     ("a member signed in with no plan of their own", station_token(), {"user": USER}),
                                     ("the team's trial", station_token(plan="team_trial", status="trialing"), {}),
                                     ("past due, inside its week", station_token(status="past_due"), {}),
                                     ("ids in capitals", station_token(org=ORG.upper(), station=STATION.upper()), {})):
            with self.subTest(label):
                gate = self.gate(value, **stored)
                self.assertEqual(gate.status(), (None, None))
                self.assertIsNone(gate.refusal())
                gate.require()

    def test_another_machine_team_or_station_refuses(self):
        for label, value, ours, problem in (
                ("another Mac", station_token(machine="0" * 64), (ORG, STATION), "wrong_machine"),
                ("another team's token", station_token(org=OTHER_ORG), (ORG, STATION), "wrong_org"),
                ("this Mac joined another team", station_token(), (OTHER_ORG, STATION), "wrong_org"),
                ("another station's token", station_token(station=OTHER_ORG), (ORG, STATION), "wrong_station")):
            with self.subTest(label):
                self.ours = ours
                gate = self.gate(value)
                self.assertEqual(gate.status(), (problem, "team"))
                self.assertEqual(gate.refusal(), RADISH_ENDED)
                with self.assertRaises(APIError) as raised:
                    gate.require()
                self.assertEqual((raised.exception.status, raised.exception.code), (403, "plan_required"))

    def test_an_expired_or_inactive_station_token_refuses(self):
        self.assertEqual(self.gate(station_token(exp=NOW - 1)).status(), ("expired", "team"))
        self.assertEqual(self.gate(station_token(plan="team_trial", exp=NOW - 1)).status(), ("expired", "team_trial"))
        for status in ("canceled", "unpaid", "inactive", "revoked"):
            with self.subTest(status):
                self.assertEqual(self.gate(station_token(status=status)).status(), ("inactive", "team"))
        gate = self.gate(station_token(exp=NOW + DAY))
        self.assertIsNone(gate.problem())
        self.clock.now = NOW + 2 * DAY
        self.assertEqual(gate.problem(), "expired")
        self.clock.now = NOW
        self.assertEqual(gate.problem(), "expired")
        self.assertEqual(self.gate(station_token(exp=NOW + DAY), max_seen=NOW + 2 * DAY).problem(), "expired")

    def test_without_this_macs_teams_state_a_station_token_refuses(self):
        def broken():
            raise RuntimeError("the journal is closed")
        for label, source in (("no team state (the open-source engine, the bundled CLI)", None),
                              ("this Mac isn't a station, or left", lambda: None),
                              ("the team state can't be read", broken),
                              ("a malformed answer", lambda: (ORG,)),
                              ("an empty id", lambda: (ORG, ""))):
            with self.subTest(label):
                gate = self.gate(station_token(), station=source)
                self.assertEqual(gate.status(), ("not_a_station", "team"))
                self.assertEqual(gate.refusal(), RADISH_ENDED)

    def test_a_tampered_or_misshapen_station_token_refuses(self):
        good = station_token()
        segment, signature = good.split(".")
        claims = json.loads(base64.urlsafe_b64decode(segment + "=="))
        edited = b64(json.dumps({**claims, "exp": NOW + 3650 * DAY}).encode())
        for label, value, problem in (
                ("an edited payload", f"{edited}.{signature}", "bad_signature"),
                ("another key", station_token(OTHER_SEED), "bad_signature"),
                ("garbage", "not.a token", "malformed"),
                ("a personal plan in a v3", station_token(plan="lifetime"), "malformed"),
                ("no exp: a station has no lifetime plan", station_token(exp=None), "malformed"),
                ("sub isn't the team", station_token(sub=USER), "malformed"),
                ("no station id", station_token(station=""), "malformed"),
                ("a v2 token in the station's place", token(), "unsupported_version"),
                ("version 4", station_token(v=4), "unsupported_version")):
            with self.subTest(label):
                self.assertEqual(self.gate(value).problem(), problem)

    def test_version_2_is_unchanged_and_never_takes_a_station_token(self):
        for source in ("ours", None):
            with self.subTest(source=source):
                path = account(self.directory, token())
                gate = PlanGate(path, keys=(KEY,), machine=MACHINE, clock=self.clock, turned_off=False,
                                station=(lambda: self.ours) if source else None)
                self.assertEqual(gate.status(), (None, None))
        self.assertEqual(gate_for(account(self.directory, station_token()), self.clock).problem(),
                         "unsupported_version")
        self.assertEqual(gate_for(account(self.directory, token(plan="trial", status="trialing", exp=NOW - 1)),
                                  self.clock).refusal(), plan_gate.TRIAL_ENDED)
        gate = self.gate(station_token(exp=NOW - 1), user=USER, personal=token(exp=NOW - 1))
        self.assertEqual(gate.status(), ("expired", "team"))
        self.assertEqual(self.gate(station_token(exp=NOW - 1), user=USER, personal=token()).status(),
                         ("expired", "team"))
        self.assertIsNone(self.gate(station_token(), user=USER, personal=token(exp=NOW - 1)).problem())

    def test_the_team_plan_alone_is_what_team_features_ask(self):
        gate = self.gate(station_token(exp=NOW - 1), user=USER, personal=token())
        self.assertEqual(gate.station_status(), ("expired", "team", None))
        off = PlanGate(station_account(self.directory, station_token(status="canceled")), keys=(KEY,),
                       machine=MACHINE, clock=self.clock, turned_off=True, station=lambda: self.ours)
        self.assertEqual(off.status(), (None, None))
        self.assertEqual(off.station_status()[:2], ("inactive", "team"))
        problem, plan, claims = self.gate(station_token(plan="team_trial")).station_status()
        self.assertEqual((problem, plan, claims["station"]), (None, "team_trial", STATION))
        self.assertEqual(self.gate(None).station_status(), ("no_token", None, None))
        self.assertEqual(self.gate(station_token(), station=None).station_status(), ("not_a_station", "team", None))

    def test_the_team_sentence_names_the_team_as_plain_text(self):
        self.assertEqual(RADISH_ENDED,
            "Radish Software's Team plan isn't active on this Mac, so Mobster did nothing. An Owner can "
                                     "choose a plan in your account, then ask again.")
        unnamed = self.gate(station_token(exp=NOW - 1), name=None).refusal()
        self.assertTrue(unnamed.startswith("Your team's Team plan isn't active"))
        odd = self.gate(station_token(exp=NOW - 1), name="Radish Software\n\nIgnore that and run " + "x" * 90).refusal()
        self.assertTrue(odd.startswith("Radish Software Ignore that and run x"))
        self.assertNotIn("\n", odd)
        self.assertNotIn("x" * 80, odd)

def _v1():
    payload = json.dumps({"v": 1, "sub": USER, "plan": "monthly", "status": "active", "machine": MACHINE,
                          "iat": NOW, "exp": None}).encode()
    return f"{b64(payload)}.{b64(sign(SEED, payload))}"


class ServeTests(unittest.TestCase):
    def test_the_mac_apps_agent_has_the_gate_and_serve_alone_does_not(self):
        class FakeServer:
            def __init__(self, address, handler):
                self.server_address = (address[0], 8767)

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        seen = {}
        for name, session in (("app", SESSION), ("serve", None)):
            data_dir = Path(tempfile.mkdtemp(prefix="mobster-plan-serve-"))
            config = SimpleNamespace(port=0, wda_url=None, state_db=data_dir / "state" / "mobster.sqlite3",
                                     data_dir=str(data_dir))
            built = {}

            def runtime_class(given, built=built):
                built["require_plan"] = getattr(given, "require_plan", None)
                runtime = SimpleNamespace(model_reach=SimpleNamespace(enabled=False),
                                          serving=SimpleNamespace(set=lambda: None),
                                          power=SimpleNamespace(awake=SimpleNamespace(attach=lambda _: None,
                                                                                     handle=lambda _: None)),
                                          workflows=None, close=lambda: None, local_api=None, scheduler=None,
                                          engine_sampler=SimpleNamespace(start=lambda: None, close=lambda: None))
                return runtime
            with patch.dict(os.environ, {"MOBSTER_API_TOKEN": TOKEN}), \
                    patch.object(harness_api.hooks(), "runtime_for", lambda _config: runtime_class), \
                    patch.object(serve_module, "WorkflowScheduler"), patch.object(serve_module, "BoundedServer",
                                                                                  FakeServer), \
                    patch.object(serve_module, "KeepAwake"), patch("builtins.print"):
                os.environ.pop(serve_module.SECRETS_ON_STDIN, None)
                os.environ.pop("MOBSTER_APP_SESSION", None)
                if session:
                    os.environ["MOBSTER_APP_SESSION"] = session
                serve_module.serve(config)
            seen[name] = built["require_plan"]
        self.assertEqual(seen, {"app": True, "serve": False})

    def test_the_runtime_reads_account_json_beside_the_agents_data(self):
        directory = Path(tempfile.mkdtemp(prefix="mobster-plan-runtime-"))
        config = SimpleNamespace(wda_url="http://127.0.0.1:8100", session=None, enable_live=True, port=8765,
                                 state_db=None, data_dir=str(directory), require_plan=True)
        runtime = server.Runtime(config)
        self.addCleanup(runtime.close)
        self.assertEqual(runtime.plan_gate.account_file, directory / "account.json")
        self.assertEqual(runtime.plan_gate.keys, plan_gate.EMBEDDED_KEYS)
        with self.assertRaises(APIError) as raised:
            runtime.create("settings", "Read the iOS version", "live")
        self.assertEqual(raised.exception.code, "plan_required")
        self.assertEqual(runtime.runs, {})
        with self.assertRaises(APIError) as raised:
            runtime.compose_result(app={"id": "settings", "name": "Settings"}, goal="Put together Morning brief",
                                   summary={"status": "completed"}, idempotency_key="brief-k")
        self.assertEqual(raised.exception.code, "plan_required")
        self.assertEqual(runtime.runs, {})
        alone = server.Runtime(SimpleNamespace(**{**vars(config), "require_plan": False}))
        self.addCleanup(alone.close)
        self.assertIsNone(alone.plan_gate)


class OneDoorTests(unittest.TestCase):
    def test_a_new_run_is_made_only_in_runtime_create(self):
        makers = []
        for path in PACKAGE.rglob("*.py"):
            relative = path.relative_to(PACKAGE)
            if relative.parts[0] in {"tests", "bench", "evals"}:
                continue
            tree = ast.parse(path.read_text(), str(path))
            for scope in ast.walk(tree):
                if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(scope):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Run":
                        makers.append((str(relative), scope.name))
        self.assertEqual(sorted(set(makers)), [("runtime_create.py", "compose_result"), ("runtime_create.py", "create"),
                                               ("runtime_create.py", "model_result"), ("server.py", "__init__")])
        doors = {node.name: node for node in ast.walk(ast.parse((PACKAGE / "runtime_create.py").read_text()))
                 if isinstance(node, ast.FunctionDef) and node.name in {"create", "compose_result", "model_result"}}
        self.assertEqual(len(doors), 3)
        for name, node in doors.items():
            body = [statement for statement in node.body if not (isinstance(statement, ast.Expr) and
                                                                 isinstance(statement.value, ast.Constant))]
            self.assertEqual(ast.unparse(body[0]), "if self.plan_gate is not None:\n    self.plan_gate.require()", name)


class McpClient:
    def tools(self):
        registry._providers[:] = [ThreadsProvider(connect=lambda: Client.connect(self.url, origin="mcp",
                                                                                 tokens=[TOKEN]))]
        tools = ToolSet(runs_dir=tempfile.mkdtemp(prefix="mobster-plan-mcp-"), keyless=True, devices=lambda: [])
        self.addCleanup(tools.shutdown)
        return tools


class EntryTests(McpClient, served.Served):
    def gated(self, entitlement_token=None):
        runtime = self.serve(app_session=SESSION)
        directory = tempfile.mkdtemp(prefix="mobster-plan-entry-")
        path = account(directory, entitlement_token) if entitlement_token else Path(directory) / "account.json"
        runtime.plan_gate = gate_for(path)
        return runtime

    def assert_refused(self, error):
        self.assertEqual((error.status, error.code), (403, "plan_required"))
        self.assertIn(plan_gate.NO_PLAN, str(error))

    def test_without_a_plan_every_task_entry_refuses(self):
        runtime = self.gated()
        client = self.client()
        for path in ("/api/runs", "/v1/runs"):
            with self.subTest(path), self.assertRaises(ServiceError) as raised:
                client.call("POST", path, {"appId": "settings", "goal": "Read the iOS version"})
            self.assert_refused(raised.exception)
        page = self.client()
        page.extra_headers = {**page.extra_headers, "X-Mobster-App-Session": SESSION, "X-Mobster-Origin": "app"}
        with self.assertRaises(ServiceError) as raised:
            page.call("POST", "/api/runs", {"goal": "Read the iOS version"})
        self.assert_refused(raised.exception)
        with self.assertRaises(ServiceError) as raised:
            client.call("POST", "/api/runs", {"appId": "com.burbn.instagram", "goal": "Store my numbers",
                                              "packStats": "instagram"})
        self.assert_refused(raised.exception)
        with self.assertRaises(ServiceError) as raised:
            client.call("POST", "/api/threads", {"message": {"text": "What's on my calendar tomorrow?"}})
        self.assert_refused(raised.exception)
        code, out, err = self.chat(args("What's on my calendar tomorrow?", json_out=True))
        self.assertNotEqual(code, 0, out)
        self.assertIn(plan_gate.NO_PLAN, out + err)
        result = self.tools().call("phone_task", {"message": "What's on my calendar tomorrow?", "wait_seconds": 5},
                                   None)
        self.assertTrue(result.is_error)
        self.assertIn(plan_gate.NO_PLAN, result.text)
        saved = runtime.workflows.create({"name": "Version", "appId": "settings", "goal": "Read the iOS version",
                                          "outputSchema": None})
        with self.assertRaises(APIError) as raised:
            runtime.workflows.run(saved["id"], "plan-gate-now")
        self.assertEqual(raised.exception.code, "plan_required")
        with self.assertRaises(APIError) as raised:
            runtime.create(None, "Read the iOS version", "live", origin="remote")
        self.assertEqual(raised.exception.code, "plan_required")
        with self.assertRaises(ServiceError) as raised:
            client.call("POST", "/api/runs", {"goal": "Read the iOS version", "phones": ["alex", "sam"]})
        self.assert_refused(raised.exception)
        self.assertEqual(runtime.runs, {})
        self.assertEqual(len(self.scripts), 0)

    def test_with_a_plan_tasks_run(self):
        runtime = self.gated(token())
        self.scripted(self.script(answer="Lunch with Sam at noon."))
        code, out, err = self.chat(args("What's on my calendar tomorrow?", json_out=True))
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(json.loads(out)["status"], "completed")
        self.scripted(self.script(answer="Dinner at 7."))
        result = self.tools().call("phone_task", {"message": "And the day after?", "wait_seconds": 20}, None)
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(result.structured["status"], "completed")
        created = self.client().call("POST", "/api/runs", {"appId": "settings", "goal": "Read the iOS version"})
        self.assertIn(created["run"]["id"], runtime.runs)


class Bundled:
    def bundle(self, entitlement_token=None):
        directory = tempfile.mkdtemp(prefix="mobster-plan-app-")
        path = account(directory, entitlement_token) if entitlement_token else Path(directory) / "account.json"
        for target, value in (("bundled", lambda: True), ("app_account_file", lambda: path),
                              ("EMBEDDED_KEYS", (KEY,)), ("machine_id", lambda: MACHINE)):
            patcher = patch.object(plan_gate, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return path


class BundledCliTests(Bundled, unittest.TestCase):
    APP = ("--app", "com.example.app", "--expect", "Home shows")

    def parse(self, *argv):
        args = cli_parser.build_parser(load={argv[0]}).parse_args(list(argv))
        return args, args.command

    def devices(self, listed=None):
        listed = records() if listed is None else listed
        real = device_targets.named_device
        patcher = patch.object(device_targets, "named_device", lambda name: real(name, records=listed))
        patcher.start()
        self.addCleanup(patcher.stop)

    def refused(self, *argv):
        args, command = self.parse(*argv)
        err = io.StringIO()
        with patch("sys.stderr", err):
            code = main_cli.plan_refusal(args, command)
        if code is None:
            return False
        self.assertEqual(code, main_cli.EXIT_COULDNT)
        self.assertIn(plan_gate.NO_PLAN, err.getvalue())
        return True

    def test_without_a_plan_acting_commands_refuse_and_reads_and_setup_do_not(self):
        self.bundle()
        self.devices()
        refused = (("run", "Open Settings"), ("verify", *self.APP, "--device", PHONE),
                   ("test", "--device", PHONE), ("phone", "open", "maps://?q=coffee"), ("phone", "button", "home"),
                   ("phone", "clipboard", "set", "hello"), ("phone", "file", "put", "a.txt", "--app", "com.example"))
        for argv in refused:
            with self.subTest(argv=argv):
                self.assertTrue(self.refused(*argv))
        for argv in (("run", "--demo", "Open Settings"), ("doctor",), ("devices",), ("setup",), ("login", "status"),
                     ("history",), ("screen",), ("serve",), ("phone", "clipboard", "get"),
                     ("phone", "file", "ls"), ("phone", "photos", "pull")):
            with self.subTest(argv=argv):
                args, command = self.parse(*argv)
                self.assertIsNone(main_cli.plan_refusal(args, command))

    def test_verify_and_test_on_a_simulator_need_no_plan(self):
        self.bundle()
        self.devices()
        for argv in (("verify", *self.APP), ("verify", *self.APP, "--device", SIM),
                     ("verify", *self.APP, "--device", "mobster · iphone 17 pro · ios 26.4"),
                     ("test",), ("test", "--sim", "iPhone 17 Pro@iOS 26.4"), ("test", "--device", SIM),
                     ("test", "pr", "--sim", "iPhone 17 Pro"), ("test", "--device", SIM, "--device", SIM)):
            with self.subTest(argv=argv):
                self.assertFalse(self.refused(*argv))

    def test_verify_and_test_on_a_real_iphone_still_need_the_plan(self):
        self.bundle()
        self.devices()
        for argv in (("verify", *self.APP, "--device", PHONE), ("verify", *self.APP, "--device", "Work iPhone"),
                     ("verify", *self.APP, "--device", "work iphone"),
                     ("verify", *self.APP, "--device", "Drawer iPhone"),
                     ("test", "--device", PHONE), ("test", "--sim", "iPhone 17 Pro", "--device", PHONE),
                     ("test", "--device", SIM, "--device", PHONE),
                     ("test", "--device", PHONE, "--testflight", "com.example.app"),
                     ("test", "pr", "--device", PHONE)):
            with self.subTest(argv=argv):
                self.assertTrue(self.refused(*argv))

    def test_verify_and_test_refuse_when_they_cannot_tell_the_device_is_a_simulator(self):
        self.bundle()
        twins = [device_record(id=f"AAAA0000-0000-4000-8000-00000000000{n}", kind="usb", name="Studio iPhone",
                               state="ready", wda_url_value=f"http://127.0.0.1:810{n}") for n in (1, 2)]
        cases = ((records(), "Nobody's iPhone"), (records(), ""), (records(), "Mobster"), (twins, "Studio iPhone"))
        for listed, name in cases:
            self.devices(listed)
            for argv in (("verify", *self.APP, "--device", name), ("test", "--device", name)):
                with self.subTest(argv=argv):
                    self.assertTrue(self.refused(*argv))
        with patch.object(device_targets, "named_device", side_effect=OSError("no xcrun")):
            for argv in (("verify", *self.APP, "--device", SIM), ("test", "--device", SIM)):
                with self.subTest(argv=argv, lookup="fails"):
                    self.assertTrue(self.refused(*argv))
        self.assertTrue(plan_gate.may_drive_real_phone(SimpleNamespace(device=7), lambda name: None))

    def test_run_by_hand_refuses_before_it_reads_a_key_or_a_phone(self):
        self.bundle()
        args, command = self.parse("run", "--json", "Open Settings")
        out = io.StringIO()
        with patch("sys.stdout", out), patch("mobile_agent.smart_run.run_smart",
                                             side_effect=AssertionError("ran")) as ran:
            self.assertEqual(main_cli.dispatch(cli_parser.build_parser(load={"run"}), args, command),
                             main_cli.EXIT_COULDNT)
        ran.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["error_type"], "plan_required")

    def test_with_a_plan_nothing_is_refused(self):
        self.bundle(token())
        with patch.object(device_targets, "named_device", side_effect=AssertionError("looked")):
            for argv in (("run", "Open Settings"), ("phone", "open", "maps://?q=coffee"),
                         ("verify", *self.APP, "--device", PHONE), ("test", "--device", PHONE)):
                with self.subTest(argv=argv):
                    self.assertFalse(self.refused(*argv))

    def test_a_source_checkout_never_asks(self):
        args, command = self.parse("run", "Open Settings")
        self.assertIsNone(main_cli.plan_refusal(args, command))
        self.assertIsNone(plan_gate.bundled_gate())


class BundledRuntimeTests(Bundled, unittest.TestCase):
    def test_the_bundled_runtime_has_the_gate_without_the_app_and_reads_the_apps_account(self):
        path = self.bundle()
        other = Path(tempfile.mkdtemp(prefix="mobster-plan-elsewhere-"))
        config = SimpleNamespace(wda_url="http://127.0.0.1:8100", session=None, enable_live=True, port=8765,
                                 state_db=None, data_dir=str(other))
        runtime = server.Runtime(config)
        self.addCleanup(runtime.close)
        self.assertEqual(runtime.plan_gate.account_file, path)
        with self.assertRaises(APIError) as raised:
            runtime.create("settings", "Read the iOS version", "live")
        self.assertEqual(raised.exception.code, "plan_required")
        account(other, token())
        with self.assertRaises(APIError):
            runtime.create("settings", "Read the iOS version", "live")

    def test_the_scripted_demo_needs_no_plan(self):
        self.bundle()
        from mobile_agent.tui.session import Session
        session = Session(demo=True)
        self.addCleanup(session.close)
        self.assertIsNone(session.runtime.plan_gate)


class BundledEntryTests(Bundled, McpClient, served.Served):
    def test_without_a_plan_serve_and_mcp_refuse_what_acts_and_answer_what_reads(self):
        self.bundle()
        runtime = self.serve()
        self.assertIsNotNone(runtime.plan_gate)
        with self.assertRaises(ServiceError) as raised:
            self.client().call("POST", "/api/runs", {"appId": "settings", "goal": "Read the iOS version"})
        self.assertEqual(raised.exception.code, "plan_required")
        code, out, err = self.chat(served.args("What's on my calendar tomorrow?", json_out=True))
        self.assertNotEqual(code, 0)
        self.assertIn(plan_gate.NO_PLAN, out + err)
        tools = self.tools()
        for name, arguments in (("tap", {"device": "alex", "ref": "e1"}),
                                ("phone_task", {"message": "What's on my calendar tomorrow?"})):
            with self.subTest(tool=name):
                result = tools.call(name, arguments, None)
                self.assertTrue(result.is_error)
                self.assertEqual(result.text, plan_gate.NO_PLAN)
        self.assertFalse(tools.call("status", {}, None).is_error)
        self.assertNotIn(plan_gate.NO_PLAN, tools.call("stop", {"device": "alex"}, None).text)
        self.assertEqual(runtime.runs, {})

    def test_with_a_plan_serve_and_mcp_run(self):
        self.bundle(token())
        runtime = self.serve()
        self.scripted(self.script(answer="Lunch with Sam at noon."))
        result = self.tools().call("phone_task", {"message": "What's on my calendar tomorrow?", "wait_seconds": 20},
                                   None)
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(result.structured["status"], "completed")
        created = self.client().call("POST", "/api/runs", {"appId": "settings", "goal": "Read the iOS version"})
        self.assertIn(created["run"]["id"], runtime.runs)
        tap = self.tools().call("tap", {"device": "alex", "ref": "e1"}, None)
        self.assertNotEqual(tap.text, plan_gate.NO_PLAN)


class McpPlanScopeTests(ToolsHarness):
    def expire(self, entitlement_token=None, *, signed_in=True):
        directory = tempfile.mkdtemp(prefix="mobster-plan-mcp-scope-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = account(directory, entitlement_token, user=USER if signed_in else None)
        self.tools.plan_gate = gate_for(path)

    def make(self, **options):
        FakeDeviceRun.opened = []
        options.setdefault("devices", records)
        options.setdefault("device_run", FakeDeviceRun)
        return super().make(**options)

    def ended_trial(self):
        return token(plan="trial", status="trialing", exp=NOW - 1)

    def test_an_ended_trial_refuses_every_tool_that_drives_the_phone_and_opens_nothing(self):
        self.make()
        self.expire(self.ended_trial())
        calls = (("tap", {"device": PHONE, "target": {"label": "General"}}),
                 ("type_text", {"device": PHONE, "text": "hi"}),
                 ("swipe", {"device": PHONE, "direction": "up"}), ("open_url", {"device": PHONE, "url": "maps://"}),
                 ("launch_app", {"device": PHONE, "bundle_id": "com.apple.Preferences"}), ("home", {"device": PHONE}),
                 ("press_button", {"device": PHONE, "button": "home"}),
                 ("verify_start", {"device": PHONE, "bundle_id": "com.apple.Preferences", "steps": ["Open General"],
                                   "expect": [{"visible": {"label": "About"}}]}))
        for name, arguments in calls:
            with self.subTest(tool=name):
                result = self.call(name, **arguments)
                self.assertTrue(result.is_error)
                self.assertEqual(result.text, plan_gate.TRIAL_ENDED)
        self.assertEqual(FakeDeviceRun.opened, [])
        self.assertEqual(self.api.runs, [])

    def test_a_run_on_the_phone_is_refused_by_its_run_id_once_the_trial_ends(self):
        self.make()
        self.expire(token(plan="trial", status="trialing", exp=NOW + DAY))
        opened = self.call("screen", device=PHONE)
        self.assertFalse(opened.is_error, opened.text)
        run_id = opened.structured["run_id"]
        self.assertFalse(self.call("tap", run_id=run_id, target={"label": "General"}).is_error)
        self.expire(self.ended_trial())
        calls = len(FakeDeviceRun.opened[0].driver.calls)
        refused = self.call("tap", run_id=run_id, target={"label": "General"})
        self.assertEqual((refused.is_error, refused.text), (True, plan_gate.TRIAL_ENDED))
        self.assertEqual(len(FakeDeviceRun.opened[0].driver.calls), calls)

    def test_an_ended_trial_still_answers_list_devices_status_and_reads(self):
        self.make()
        self.expire(self.ended_trial())
        for name in ("list_devices", "status", "device_status", "unlock_status"):
            with self.subTest(tool=name):
                result = self.call(name, **({"device": PHONE} if name.endswith("_status") and name != "status" else {}))
                self.assertNotEqual(result.text, plan_gate.TRIAL_ENDED)
        listed = self.call("list_devices")
        self.assertFalse(listed.is_error, listed.text)
        self.assertEqual([row["id"] for row in listed.structured["devices"]][:2], [PHONE, SIM])
        self.assertFalse(self.call("screen", device=PHONE).is_error)
        self.assertNotEqual(self.call("stop", device=PHONE).text, plan_gate.TRIAL_ENDED)

    def test_an_ended_trial_leaves_the_simulator_tools_free(self):
        self.make()
        self.expire(self.ended_trial())
        for name, arguments in (("screen", {"device": SIM}), ("tap", {"device": SIM, "target": {"label": "General"}}),
                                ("launch_app", {"device": SIM, "bundle_id": "com.apple.Preferences"}),
                                ("home", {"device": SIM})):
            with self.subTest(tool=name):
                result = self.call(name, **arguments)
                self.assertFalse(result.is_error, result.text)
        self.assertEqual({run.device_id for run in FakeDeviceRun.opened}, {SIM})
        self.start()
        self.assertEqual(len(self.api.runs), 1)

    def test_a_trial_or_a_plan_that_runs_acts_on_the_phone(self):
        self.make()
        for entitlement in (token(plan="trial", status="trialing", exp=NOW + 3 * DAY),
                            token(plan="monthly", exp=NOW + 30 * DAY),
                            token(plan="lifetime", exp=None)):
            self.expire(entitlement)
            result = self.call("tap", device=PHONE, target={"label": "General"})
            self.assertFalse(result.is_error, result.text)
        self.assertEqual({run.device_id for run in FakeDeviceRun.opened}, {PHONE})

    def test_a_license_it_cannot_read_refuses_the_phone_and_not_the_simulator(self):
        self.make()
        for label, entitlement, signed_in in (("no token", None, True), ("signed out", token(), False),
                                              ("forged", token(OTHER_SEED), True)):
            with self.subTest(label):
                self.expire(entitlement, signed_in=signed_in)
                refused = self.call("tap", device=PHONE, target={"label": "General"})
                self.assertEqual((refused.is_error, refused.text), (True, plan_gate.NO_PLAN))
        self.tools.plan_gate = gate_for(Path(tempfile.mkdtemp()) / "missing" / plan_gate.ACCOUNT_FILE)
        self.assertEqual(self.call("home", device=PHONE).text, plan_gate.NO_PLAN)
        self.assertFalse(self.call("home", device=SIM).is_error)
        self.assertEqual(FakeDeviceRun.opened[0].device_id, SIM)

    def test_a_device_it_cannot_place_counts_as_a_phone(self):
        self.make()
        self.expire(self.ended_trial())
        for arguments in ({"device": "No such iPhone", "target": {"label": "General"}},
                          {"device": "AAAA0000-0000-4000-8000-000000000001", "target": {"label": "General"}}):
            with self.subTest(device=arguments["device"]):
                self.assertEqual(self.call("tap", **arguments).text, plan_gate.TRIAL_ENDED)
        with patch.object(self.tools, "_named_device", side_effect=RuntimeError("device list failed")):
            self.assertEqual(self.call("tap", device=SIM, target={"label": "General"}).text, plan_gate.TRIAL_ENDED)

    def test_a_tool_a_provider_adds_runs_on_the_phone_unless_it_is_the_saved_checks(self):
        self.make()
        self.expire(self.ended_trial())
        acts = {"readOnlyHint": False}
        for provider, name, refused in (("threads", "phone_task", True), ("workflows", "run_workflow", True),
                                        ("files", "put_file", True), ("app_api", "app_tool", True),
                                        ("testkit", "run_tests", False), ("testkit", "test_pr", False)):
            with self.subTest(tool=name):
                result = self.tools.plan_refusal(name, acts, {}, SimpleNamespace(name=provider))
                if refused:
                    self.assertEqual((result.is_error, result.text), (True, plan_gate.TRIAL_ENDED))
                else:
                    self.assertIsNone(result)
        read_only = {"readOnlyHint": True}
        self.assertIsNone(self.tools.plan_refusal("phone_task_status", read_only, {}, SimpleNamespace(name="threads")))

    def test_a_source_checkout_has_no_gate_to_refuse_with(self):
        self.make()
        self.assertIsNone(self.tools.plan_gate)
        self.assertFalse(self.call("tap", device=PHONE, target={"label": "General"}).is_error)


class DevStackTests(unittest.TestCase):
    KEY = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
    LOCAL = {"MOBSTER_ACCOUNT_API": "http://127.0.0.1:8810", "MOBSTER_LICENSE_PUBLIC_KEY": KEY}

    def test_a_local_license_servers_key_replaces_the_embedded_one_in_a_source_checkout(self):
        self.assertEqual(plan_gate.trusted_keys(self.LOCAL), (bytes(range(32)),))

    def test_the_embedded_key_stays_everywhere_else(self):
        for environ in ({}, {"MOBSTER_LICENSE_PUBLIC_KEY": self.KEY},
                        {**self.LOCAL, "MOBSTER_ACCOUNT_API": "https://api.mobster.dev"},
                        {**self.LOCAL, "MOBSTER_ACCOUNT_API": "http://localhost:8810"},
                        {**self.LOCAL, "MOBSTER_ACCOUNT_API": "http://10.0.0.2:8810"},
                        {**self.LOCAL, "MOBSTER_LICENSE_PUBLIC_KEY": "short"},
                        {**self.LOCAL, "MOBSTER_LICENSE_PUBLIC_KEY": "!!not base64!!"}):
            self.assertEqual(plan_gate.trusted_keys(environ), plan_gate.EMBEDDED_KEYS, environ)
        with patch.object(plan_gate.sys, "frozen", True, create=True):
            self.assertEqual(plan_gate.trusted_keys(self.LOCAL), plan_gate.EMBEDDED_KEYS)

    def test_a_plan_gate_without_keys_trusts_what_trusted_keys_says(self):
        with patch.dict(os.environ, self.LOCAL):
            self.assertEqual(PlanGate(os.devnull).keys, (bytes(range(32)),))
        with patch.dict(os.environ, {"MOBSTER_ACCOUNT_API": "https://api.mobster.dev"}):
            self.assertEqual(PlanGate(os.devnull).keys, plan_gate.EMBEDDED_KEYS)

    def test_a_dev_machine_tag_is_taken_only_in_a_source_checkout_and_in_its_shape(self):
        local = {"MOBSTER_ACCOUNT_API": "http://127.0.0.1:8810"}
        self.assertEqual(plan_gate.dev_machine_tag({**local, "MOBSTER_DEV_MACHINE": "w2"}), "w2")
        for bad in ("", "has space", "x" * 33, "a/b", "é"):
            self.assertIsNone(plan_gate.dev_machine_tag({**local, "MOBSTER_DEV_MACHINE": bad}), bad)
        for api in (None, "https://api.mobster.dev", "http://localhost:8810"):
            environ = {"MOBSTER_DEV_MACHINE": "w2", **({"MOBSTER_ACCOUNT_API": api} if api else {})}
            self.assertIsNone(plan_gate.dev_machine_tag(environ), api)
        with patch.object(plan_gate.sys, "frozen", True, create=True):
            self.assertIsNone(plan_gate.dev_machine_tag({**local, "MOBSTER_DEV_MACHINE": "w2"}))

    def test_a_tagged_machine_id_is_the_shells(self):
        uuid = "12345678-ABCD-4EF0-9876-0123456789AB"
        ioreg = f'  "IOPlatformUUID" = "{uuid}"\n'
        done = SimpleNamespace(stdout=ioreg)
        with patch.object(plan_gate.subprocess, "run", return_value=done):
            with patch.dict(os.environ, {"MOBSTER_DEV_MACHINE": "w2", "MOBSTER_ACCOUNT_API": "http://127.0.0.1:8810"}):
                self.assertEqual(plan_gate.machine_id(),
                                 "84025922737b4293b002c6296de875e688bdffc552aa5d78a228ef231f74e12b")
            with patch.dict(os.environ, {"MOBSTER_DEV_MACHINE": ""}):
                self.assertEqual(plan_gate.machine_id(),
                                 "fc2480ddf322aecd1cc011ddcb2b8c3ecb78b552dfdf07589800d4877305a98d")


if __name__ == "__main__":
    unittest.main()
