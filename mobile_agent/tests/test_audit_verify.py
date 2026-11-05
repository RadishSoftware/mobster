import base64
import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mobile_agent import audit_verify
from mobile_agent.audit_verify import EXPORT_PREFIX, GENESIS, verify_lines

KEY_A = "iojj3XQJ8ZX9UtstPLpdcspnCb8dlBIb83SIAbQPb1w"
KEY_B = "gTl3Dqh9F19Wo1Rmw0x-zMuNipG07jeiXfYPW4_Js5Q"
MAC_A_ENTRY = ('{"v":1,"org":"org1","mac":"mac-a","at":1760000000000,"action":"run.start","actor":"m1",'
               '"origin":"console",'
               '"target":{"run":"r1","note":"caf\\u00e9 \\ud83d\\ude80"},"outcome":"ok"}')
MAC_A_SIG = "OFJJwx89fVpbaJc_r_iMZHJh-6Zd31sFDX6GviEeqag3n5WP3lcY7gLilyRxyKt7SdZDaWsIeG5DZNVY1uz9Dw"
MAC_B_ENTRY = ('{"v":1,"org":"org1","mac":"mac-b","at":1760000100000,"action":"approval.answer","actor":null,'
               '"origin":"mac_app","target":{},"outcome":"approved"}')
MAC_B_SIG = "WR_rDOZop-FM0AIe13dagBxZ7cNShq9zckzNSYEiFMETM1xAkEuUlasca2da-gakisVRmXABqnsOqlHVQUiZAQ"
RELAY_ENTRY = '{"note":"café 🚀"}'
RELAY_ENTRY_HASH = "374613f6323aa779cdea7e2e61e23838e58071de34f8a8495e5cff3696ed0bdd"


def chain_hash(prev, seq, at, src, mac, entry, sig):
    units = lambda s: len(s.encode("utf-16-le")) // 2
    sig = sig or ""
    return hashlib.sha256(
        f"{prev}|{seq}|{at}|{src}|{mac or ''}|{units(entry)}|{entry}|{units(sig)}|{sig}".encode()).hexdigest()


def build(rows, start=0, start_hash=GENESIS, partial=False):
    out, prev = [], start_hash
    for i, (at, src, mac, entry, sig) in enumerate(rows):
        seq = start + 1 + i
        h = chain_hash(prev, seq, at, src, mac, entry, sig)
        out.append({"seq": seq, "at": at, "src": src, "mac_id": mac, "entry": entry, "sig": sig,
                    "prev_hash": prev, "hash": h})
        prev = h
    return {"v": 1, "checkpoint": {"seq": start, "hash": start_hash}, "partial": partial, "entries": out,
            "head": {"seq": start + len(out), "hash": prev}}


ROWS = [
    (1759990000000, "relay", None, RELAY_ENTRY, None),
    (1760000000000, "mac", "mac-a", MAC_A_ENTRY, MAC_A_SIG),
    (1760000100000, "mac", "mac-b", MAC_B_ENTRY, MAC_B_SIG),
    (1760086400000, "relay", None, '{"action":"console.signin"}', None),
]
KEYS = {"mac-a": audit_verify._b64url(KEY_A), "mac-b": audit_verify._b64url(KEY_B)}


def reseal(export):
    prev = export["checkpoint"]["hash"]
    for r in export["entries"]:
        r["prev_hash"] = prev
        r["hash"] = prev = chain_hash(prev, r["seq"], r["at"], r["src"], r["mac_id"], r["entry"], r["sig"])
    export["head"] = {"seq": export["entries"][-1]["seq"], "hash": prev}
    return export


class VerifyTests(unittest.TestCase):
    def test_offline_audit_import_works_without_the_mac_only_team_package(self):
        code = """
import hashlib
import importlib.abc
import sys
class NoTeams(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'mobile_agent.teams' or fullname.startswith('mobile_agent.teams.'):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoTeams())
from mobile_agent import audit_verify
assert audit_verify.key_id(b'fixture') == hashlib.sha256(b'fixture').hexdigest()[:16]
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_hash_matches_the_relay_on_non_ascii_text(self):
        row = {"prev_hash": GENESIS, "seq": 1, "at": 1760000000000, "src": "relay", "mac_id": None,
               "entry": RELAY_ENTRY, "sig": None}
        self.assertEqual(audit_verify.entry_hash(row), RELAY_ENTRY_HASH)

    def test_a_good_export_passes_with_and_without_keys(self):
        export = build(ROWS)
        plain = audit_verify.verify(export)
        self.assertTrue(plain["ok"])
        self.assertEqual((plain["entries"], plain["signatures_checked"]), (4, False))
        signed = audit_verify.verify(export, KEYS)
        self.assertTrue(signed["ok"])
        self.assertTrue(signed["signatures_checked"])

    def test_a_partial_export_from_a_checkpoint_passes(self):
        export = build(ROWS[1:], start=811, start_hash="ab" * 32, partial=True)
        self.assertTrue(audit_verify.verify(export, KEYS)["ok"])
        self.assertEqual(export["entries"][0]["seq"], 812)

    def test_an_empty_export_passes_when_its_head_is_its_checkpoint(self):
        self.assertTrue(audit_verify.verify(build([]))["ok"])
        export = build([])
        export["head"]["seq"] = 3
        self.assertEqual(audit_verify.verify(export)["problem"]["code"], "head")

    def test_a_changed_entry_names_that_entry(self):
        export = build(ROWS)
        export["entries"][2]["entry"] = export["entries"][2]["entry"].replace("approved", "denied")
        problem = audit_verify.verify(export)["problem"]
        self.assertEqual((problem["entry"], problem["code"]), (3, "hash"))
        self.assertIn("Entry 3 was changed after it was written: its hash doesn't match.", problem["message"])
        self.assertIn("can't be trusted from entry 3 on", problem["message"])

    def test_a_changed_sig_fails_the_hash_and_with_resealed_hashes_the_signature(self):
        export = build(ROWS)
        export["entries"][1]["sig"] = MAC_B_SIG
        self.assertEqual(audit_verify.verify(export, KEYS)["problem"]["code"], "hash")
        resealed = audit_verify.verify(reseal(export), KEYS)
        self.assertEqual((resealed["problem"]["entry"], resealed["problem"]["code"]), (2, "signature"))
        self.assertTrue(audit_verify.verify(export)["ok"], "without --keys the chain is all that is checked")

    def test_a_removed_middle_entry_is_a_gap_at_its_number(self):
        export = build(ROWS)
        del export["entries"][1]
        problem = audit_verify.verify(export)["problem"]
        self.assertEqual((problem["entry"], problem["code"]), (2, "gap"))
        self.assertIn("Entry 2 is missing", problem["message"])

    def test_a_cut_end_does_not_match_the_head(self):
        export = build(ROWS)
        del export["entries"][-1]
        self.assertEqual(audit_verify.verify(export)["problem"]["code"], "head")
        export = build(ROWS)
        export["head"]["hash"] = "0" * 64
        self.assertEqual(audit_verify.verify(export)["problem"]["code"], "head")

    def test_a_mac_entry_signed_by_another_macs_key_fails(self):
        keys = {"mac-a": KEYS["mac-b"], "mac-b": KEYS["mac-a"]}
        problem = audit_verify.verify(build(ROWS), keys)["problem"]
        self.assertEqual((problem["entry"], problem["code"]), (2, "signature"))

    def test_a_mac_with_no_key_fails(self):
        problem = audit_verify.verify(build(ROWS), {"mac-a": KEYS["mac-a"]})["problem"]
        self.assertEqual((problem["entry"], problem["code"]), (3, "no_key"))
        self.assertIn("no key for Mac mac-b", problem["message"])

    def test_a_mac_entry_with_no_signature_fails_with_keys(self):
        export = copy.deepcopy(build(ROWS))
        export["entries"][1]["sig"] = None
        problem = audit_verify.verify(reseal(export), KEYS)["problem"]
        self.assertEqual((problem["entry"], problem["code"]), (2, "signature"))

    def test_the_summary_says_how_many_and_when(self):
        text = audit_verify.summary(audit_verify.verify(build(ROWS), KEYS))
        self.assertEqual(text, "All 4 entries check out, from 9 Oct to 10 Oct.")
        self.assertIn("--keys", audit_verify.summary(audit_verify.verify(build(ROWS))))

    def test_a_file_that_is_not_an_export_is_unreadable(self):
        for bad in ({}, {"v": 2, "entries": []},
                    {"v": 1, "entries": [1], "checkpoint": {"seq": 0, "hash": GENESIS},
                     "head": {"seq": 0, "hash": GENESIS}},
                    {"v": 1, "entries": [], "checkpoint": None, "head": None}):
            with self.assertRaises(audit_verify.Unreadable):
                audit_verify.verify(bad)


class BindingTests(unittest.TestCase):
    """A Mac's signature covers an entry's words only, so the words must name that Mac and the same team as the other
    Mac entries, and no entry may come twice: the relay or the license API can't move, copy in or repeat a real entry
    (security review of #1774)."""

    MAC_KEY = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)

    def signed(self, mac="mac-a", org="org1", at=1760000000000, run="0123456789ab"):
        entry = json.dumps({"v": 1, "org": org, "mac": mac, "at": at, "action": "run.stopped", "actor": None,
                            "origin": "console", "target": {"run": run}, "outcome": "ok"}, separators=(",", ":"))
        return entry, b64(self.MAC_KEY.sign(audit_verify.SIGNED_PREFIX + entry.encode()))

    def keys(self):
        return {"mac-a": self.MAC_KEY.public_key().public_bytes(serialization.Encoding.Raw,
                                                                serialization.PublicFormat.Raw)}

    def problem(self, *entries):
        rows = [(1760000000000 + i, "mac", "mac-a", entry, sig) for i, (entry, sig) in enumerate(entries)]
        result = audit_verify.verify(build(rows), self.keys())
        lines = verify_lines(make(rows, key=STRANGER), STRANGERS, self.keys())
        self.assertEqual(result["problem"] and (result["problem"]["entry"], result["problem"]["code"]),
                         lines["problem"] and (lines["problem"]["entry"], lines["problem"]["code"]))
        return result["problem"] and (result["problem"]["entry"], result["problem"]["code"])

    def test_real_entries_of_one_mac_and_team_pass(self):
        self.assertIsNone(self.problem(self.signed(), self.signed(at=1760000000001)))

    def test_an_entry_whose_words_name_another_mac_is_refused(self):
        self.assertEqual(self.problem(self.signed(), self.signed(mac="mac-b")), (2, "mismatch"))

    def test_an_entry_of_another_team_is_refused(self):
        self.assertEqual(self.problem(self.signed(), self.signed(org="org2")), (2, "mismatch"))

    def test_an_entry_repeated_word_for_word_is_refused(self):
        entry = self.signed()
        self.assertEqual(self.problem(entry, self.signed(run="ba9876543210"), entry), (3, "duplicate"))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def write(self, name, value):
        path = self.dir / name
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        return str(path)

    def run_command(self, file, keys=None, as_json=False):
        args = type("Args", (), {"file": file, "keys": keys, "json": as_json})()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = audit_verify.run(args)
        return code, out.getvalue(), err.getvalue()

    def test_exit_zero_only_when_everything_checks_out(self):
        export = self.write("e.json", build(ROWS))
        keys = self.write("k.json", {"mac-a": KEY_A, "mac-b": KEY_B})
        code, out, _ = self.run_command(export, keys)
        self.assertEqual((code, out.strip()), (0, "All 4 entries check out, from 9 Oct to 10 Oct."))
        code, out, _ = self.run_command(export)
        self.assertEqual(code, 0)
        self.assertIn("signatures weren't checked", out)
        bad = build(ROWS)
        bad["entries"][0]["entry"] = "{}"
        code, out, err = self.run_command(self.write("bad.json", bad))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Entry 1 was changed after it was written", err)

    def test_json_output_is_one_object(self):
        code, out, _ = self.run_command(self.write("e.json", build(ROWS)), as_json=True)
        result = json.loads(out)
        self.assertEqual((code, result["ok"], result["entries"], result["problem"]), (0, True, 4, None))

    def test_malformed_files_exit_two_with_a_clear_message(self):
        for file, key_file, message in (
            (self.write("m.json", "{not json"), None, "isn't JSON"),
            (str(self.dir / "missing.json"), None, "Couldn't read the export"),
            (self.write("o.json", {"v": 1}), None, "isn't an audit export"),
            (self.write("e.json", build(ROWS)), self.write("k2.json", "[]"), "should be a JSON object"),
            (self.write("e2.json", build(ROWS)), self.write("k3.json", {"mac-a": "AAAA"}),
             "isn't a base64url Ed25519 public key"),
            (self.write("e3.json", build(ROWS)), str(self.dir / "nokeys.json"), "Couldn't read the keys file"),
        ):
            code, out, err = self.run_command(file, key_file)
            self.assertEqual((code, out), (2, ""), message)
            self.assertIn(message, err)


FIXTURES = Path(__file__).parent / "fixtures" / "audit_export"
WHOLE = (FIXTURES / "whole.jsonl").read_text()
FILTERED = (FIXTURES / "filtered.jsonl").read_text()
LICENSE_PEM = (FIXTURES / "license_key.txt").read_text()
LICENSE_KEY = audit_verify.read_license_key(LICENSE_PEM)

STRANGER = Ed25519PrivateKey.from_private_bytes(bytes([9]) * 32)
STRANGER_PUBLIC = STRANGER.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
STRANGER_PEM = STRANGER.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def check(text, keys=None, license_keys=(LICENSE_KEY,)):
    return verify_lines(text, license_keys, keys)


def edit(text, line, change):
    parts = text.splitlines()
    value = json.loads(parts[line - 1])
    out = change(value)
    if out is None:
        del parts[line - 1]
    else:
        parts[line - 1] = json.dumps(out, separators=(",", ":"))
    return "\n".join(parts) + "\n"


def make(rows, start=0, prev=GENESIS, key=STRANGER, filters=None, shown=None, head_changes=None, log_head=None):
    out, lines = [], []
    for i, (at, src, mac, entry, sig) in enumerate(rows):
        seq = start + 1 + i
        h = chain_hash(prev, seq, at, src, mac, entry, sig)
        out.append((seq, prev, h))
        if shown is None or (i + 1) in shown:
            lines.append({"t": "entry", "seq": seq, "at": at, "src": src, "mac_id": mac, "entry": entry, "sig": sig,
                          "prev_hash": prev, "hash": h})
        else:
            lines.append({"t": "elided", "seq": seq, "prev_hash": prev, "hash": h})
        prev = h
    payload = {"v": 2, "org": "org1", "exported_at": 1800000000000, "from": None, "to": None, "filters": filters or {},
               "first_seq": out[0][0] if out else None, "first_prev_hash": out[0][1] if out else None,
               "last_seq": out[-1][0] if out else None, "last_hash": out[-1][2] if out else None,
               "count": len(out), "matched": sum(1 for line in lines if line["t"] == "entry"),
               "log_head": log_head or {"seq": out[-1][0] if out else 0, "hash": out[-1][2] if out else GENESIS},
               "kid": "x"}
    payload.update(head_changes or {})
    raw = json.dumps(payload, separators=(",", ":")).encode()
    kid = hashlib.sha256(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                       serialization.PublicFormat.Raw)).hexdigest()[:16]
    head = {"t": "head", "v": 2, "kid": kid, "payload": b64(raw), "sig": b64(key.sign(EXPORT_PREFIX + raw))}
    return "\n".join(json.dumps(x, separators=(",", ":")) for x in [head, *lines]) + "\n"


STRANGERS = (STRANGER_PUBLIC,)


class FixtureTests(unittest.TestCase):

    def test_the_whole_export_passes_with_and_without_the_macs_keys(self):
        r = check(WHOLE)
        self.assertTrue(r["ok"], r["problem"])
        self.assertEqual((r["entries"], r["matched"], r["elided"], r["partial"], r["signatures_checked"]),
                         (5, 5, 0, False, False))
        self.assertEqual(r["org"], "4a7c1e2f-2222-4333-8444-955566667777")
        signed = check(WHOLE, KEYS)
        self.assertTrue(signed["ok"], signed["problem"])
        self.assertTrue(signed["signatures_checked"])

    def test_the_filtered_export_passes_with_its_other_entries_left_out_but_chained(self):
        r = check(FILTERED)
        self.assertTrue(r["ok"], r["problem"])
        self.assertEqual((r["entries"], r["matched"], r["elided"], r["partial"]), (5, 2, 3, True))
        self.assertEqual(r["filters"], {"actions": ["sso.", "scim."]})
        self.assertIn("filters left out 3", audit_verify.summary(r))
        self.assertNotIn("approval", FILTERED)

    def test_the_built_in_key_is_not_this_files_key(self):
        r = verify_lines(WHOLE)
        self.assertFalse(r["ok"])
        self.assertEqual(r["problem"]["code"], "unknown_key")
        self.assertIn("--key", r["problem"]["message"])


class RefusalTests(unittest.TestCase):

    def refuses(self, text, code, entry=None, keys=None, license_keys=(LICENSE_KEY,)):
        r = check(text, keys, license_keys)
        self.assertFalse(r["ok"])
        self.assertEqual(r["problem"]["code"], code, r["problem"]["message"])
        if entry is not None:
            self.assertEqual(r["problem"]["entry"], entry)
        return r["problem"]["message"]

    def test_a_deleted_entry_is_a_gap_at_its_number(self):
        message = self.refuses(edit(WHOLE, 3, lambda v: None), "gap", 2)
        self.assertIn("Entry 2 is missing", message)
        self.assertIn("jumps from entry 1 to entry 3", message)

    def test_a_deleted_entry_that_a_filter_had_left_out_is_just_as_much_a_gap(self):
        self.refuses(edit(FILTERED, 3, lambda v: None), "gap", 2)

    def test_a_deleted_first_line_is_caught_by_the_signed_start(self):
        message = self.refuses(edit(WHOLE, 2, lambda v: None), "head", 2)
        self.assertIn("cut off the start", message)

    def test_a_deleted_last_line_is_caught_by_the_signed_end(self):
        text = "\n".join(WHOLE.splitlines()[:-1]) + "\n"
        message = self.refuses(text, "head")
        self.assertIn("signed head says 5", message)

    def test_a_file_cut_in_half_is_refused(self):
        self.refuses(WHOLE[: len(WHOLE) // 2], "line")

    def test_an_added_entry_is_refused(self):
        lines = WHOLE.splitlines()
        self.refuses("\n".join(lines + [lines[-1]]) + "\n", "order")

    def test_swapped_entries_are_out_of_order(self):
        lines = WHOLE.splitlines()
        lines[2], lines[3] = lines[3], lines[2]
        self.refuses("\n".join(lines) + "\n", "gap", 2)

    def test_a_repeated_line_is_out_of_order(self):
        lines = WHOLE.splitlines()
        lines.insert(3, lines[2])
        self.refuses("\n".join(lines) + "\n", "order", 2)

    def test_an_edited_entry_names_that_entry(self):
        def tamper(v):
            v["entry"] = v["entry"].replace("approved", "denied")
            return v
        message = self.refuses(edit(WHOLE, 4, tamper), "hash", 3)
        self.assertIn("Entry 3 was changed after it was written", message)

    def test_an_edited_time_actor_or_source_is_just_as_much_an_edit(self):
        for field, value in (("at", 1), ("src", "relay"), ("mac_id", "mac-z"), ("sig", None)):
            self.refuses(edit(WHOLE, 3, lambda v: {**v, field: value}), "hash", 2)

    def test_an_edited_hash_breaks_the_next_entrys_link(self):
        self.refuses(edit(WHOLE, 3, lambda v: {**v, "hash": "0" * 64}), "hash", 2)
        self.refuses(edit(FILTERED, 3, lambda v: {**v, "hash": "0" * 64}), "prev_hash", 3)

    def test_an_edited_link_is_caught(self):
        self.refuses(edit(WHOLE, 4, lambda v: {**v, "prev_hash": "f" * 64}), "prev_hash", 3)

    def test_an_edit_resealed_through_the_whole_chain_is_caught_by_the_signed_end(self):
        rows = [(r[0], r[1], r[2], r[3], r[4]) for r in ROWS]
        original = make(rows, key=STRANGER)
        lying = list(rows)
        lying[1] = (lying[1][0], lying[1][1], lying[1][2], lying[1][3].replace("run.start", "run.stop"), lying[1][4])
        forged = make(lying, key=STRANGER)
        head = original.splitlines()[0]
        text = "\n".join([head] + forged.splitlines()[1:]) + "\n"
        message = self.refuses(text, "head", license_keys=STRANGERS)
        self.assertIn("signed head", message)

    def test_an_edited_head_fails_its_signature(self):
        def tamper(v):
            raw = json.loads(base64.urlsafe_b64decode(v["payload"] + "=="))
            raw["count"] = 4
            v["payload"] = b64(json.dumps(raw, separators=(",", ":")).encode())
            return v
        message = self.refuses(edit(WHOLE, 1, tamper), "signature")
        self.assertIn("doesn't match the license key", message)

    def test_a_wrong_key_is_refused_and_says_which(self):
        for keys in ((STRANGER_PUBLIC,), (bytes(32),)):
            message = self.refuses(WHOLE, "unknown_key", license_keys=keys)
            self.assertIn("fe812c12f3ab4ce6", message)
        lie = make([(1, "relay", None, "{}", None)], key=STRANGER)
        forged = edit(lie, 1, lambda v: {**v, "kid": audit_verify.key_id(LICENSE_KEY)})
        self.refuses(forged, "signature")

    def test_a_head_that_lies_with_a_valid_signature_is_caught_by_the_entries(self):
        rows = list(ROWS)
        for change, code in (({"count": 3}, "head"), ({"matched": 3}, "matched"), ({"first_seq": 2}, "head"),
                             ({"last_hash": "a" * 64}, "head"), ({"first_prev_hash": "b" * 64}, "head")):
            text = make(rows, head_changes=change)
            self.refuses(text, code, license_keys=STRANGERS)

    def test_a_filter_that_hid_an_entry_in_a_file_that_says_it_hid_none_is_caught(self):
        text = make(list(ROWS), shown={1, 2, 4}, head_changes={"matched": 4})
        r = check(text, license_keys=STRANGERS)
        self.assertFalse(r["ok"])
        self.assertEqual(r["problem"]["code"], "matched")

    def test_a_macs_signature_is_checked_with_the_keys_file(self):
        self.refuses(WHOLE, "signature", 2,
                     keys={"mac-a": audit_verify._b64url(KEY_B), "mac-b": audit_verify._b64url(KEY_B)})
        self.refuses(WHOLE, "no_key", 2, keys={"mac-b": audit_verify._b64url(KEY_B)})

    def test_a_blank_line_or_one_that_is_not_json_is_an_edit(self):
        lines = WHOLE.splitlines()
        self.refuses("\n".join(lines[:2] + [""] + lines[2:]) + "\n", "line")
        self.refuses("\n".join(lines[:3] + ["{not json"] + lines[3:]) + "\n", "line")
        self.refuses("\n".join(lines[:3] + ['{"t":"entry"}'] + lines[3:]) + "\n", "line")
        self.refuses("\n".join(lines[:3] + ['{"t":"other","seq":3}'] + lines[3:]) + "\n", "line")


class SignedFileTests(unittest.TestCase):

    def test_a_range_from_the_middle_of_a_log_passes(self):
        text = make(list(ROWS[1:]), start=811, prev="ab" * 32, log_head={"seq": 900, "hash": "cd" * 32})
        r = check(text, KEYS, STRANGERS)
        self.assertTrue(r["ok"], r["problem"])
        self.assertTrue(r["partial"], "the log goes on past the export")

    def test_an_empty_export_passes_and_says_so(self):
        r = check(make([], log_head={"seq": 9, "hash": "ee" * 32}), license_keys=STRANGERS)
        self.assertTrue(r["ok"], r["problem"])
        self.assertIn("no entries", audit_verify.summary(r))

    def test_an_empty_export_whose_head_claims_entries_is_refused(self):
        text = make(list(ROWS)[:1])
        stripped = "\n".join(text.splitlines()[:1]) + "\n"
        r = check(stripped, license_keys=STRANGERS)
        self.assertEqual(r["problem"]["code"], "head")

    def test_a_file_that_filters_out_everything_still_chains(self):
        r = check(make(list(ROWS), shown=set()), license_keys=STRANGERS)
        self.assertTrue(r["ok"], r["problem"])
        self.assertIn("left every one out", audit_verify.summary(r))


class ExportCommandTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def run_command(self, file, key=None, keys=None, as_json=False):
        args = type("Args", (), {"file": file, "key": key, "keys": keys, "json": as_json})()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = audit_verify.run(args)
        return code, out.getvalue(), err.getvalue()

    def test_exit_zero_for_a_good_file_and_one_that_says_what_broke_for_a_bad_one(self):
        pem = self.write("license.pem", LICENSE_PEM)
        code, out, err = self.run_command(self.write("a.jsonl", WHOLE), key=pem)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("All 5 entries check out", out)
        self.assertIn("signed by Mobster's license key fe812c12f3ab4ce6", out)
        code, out, err = self.run_command(self.write("b.jsonl", edit(WHOLE, 3, lambda v: None)), key=pem)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Entry 2 is missing", err)
        code, out, err = self.run_command(self.write("c.jsonl", WHOLE), key=self.write("other.pem", STRANGER_PEM))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--key", err)
        code, out, err = self.run_command(self.write("d.jsonl", edit(WHOLE, 4, lambda v: {**v, "entry": "{}"})),
                                          key=pem)
        self.assertEqual(code, 1)
        self.assertIn("Entry 3 was changed after it was written", err)

    def test_json_output_names_the_problem_and_is_one_object(self):
        pem = self.write("license.pem", LICENSE_PEM)
        code, out, _ = self.run_command(self.write("a.jsonl", FILTERED), key=pem, as_json=True)
        result = json.loads(out)
        self.assertEqual((code, result["ok"], result["format"], result["matched"], result["problem"]),
                         (0, True, 2, 2, None))
        code, out, _ = self.run_command(self.write("b.jsonl", edit(WHOLE, 4, lambda v: None)), key=pem, as_json=True)
        problem = json.loads(out)["problem"]
        self.assertEqual((code, problem["code"], problem["entry"]), (1, "gap", 3))

    def test_files_that_are_not_exports_exit_two(self):
        pem = self.write("license.pem", LICENSE_PEM)
        v1 = self.write("v1.json",
                        json.dumps({"v": 1, "checkpoint": {"seq": 0, "hash": GENESIS}, "entries": [],
                                    "head": {"seq": 0, "hash": GENESIS}}))
        for file, key, message in (
            (self.write("g.jsonl", "hello\n"), pem, "isn't JSON or JSON Lines"),
            (self.write("h.jsonl", '{"t":"entry"}\n{"t":"head"}\n'), pem, "isn't JSON or JSON Lines"),
            (self.write("i.jsonl", '{"t":"head","v":2}\n'), pem, "isn't a signed head"),
            (self.write("j.jsonl", WHOLE), self.write("bad.pem", "not a key"), "isn't an Ed25519 public key"),
            (self.write("k.jsonl", WHOLE), str(self.dir / "missing.pem"), "Couldn't read the license key"),
            (v1, pem, "--key is for the JSON Lines export"),
        ):
            code, out, err = self.run_command(file, key=key)
            self.assertEqual((code, out), (2, ""), message)
            self.assertIn(message, err)

    def test_the_console_json_export_still_verifies_with_no_key(self):
        v1 = json.dumps({"v": 1, "checkpoint": {"seq": 0, "hash": GENESIS}, "entries": [],
                         "head": {"seq": 0, "hash": GENESIS}})
        self.assertEqual(self.run_command(self.write("v1.json", v1))[0], 0)


if __name__ == "__main__":
    unittest.main()
