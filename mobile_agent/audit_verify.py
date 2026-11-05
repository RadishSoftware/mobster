"""`mobster audit verify <file>`: check a Team plan audit export offline, trusting nothing from the relay."""

import base64
import datetime
import hashlib
import json
import sys

from .cli_constants import EXIT_FAILED, EXIT_OK, EXIT_USAGE
from .plan_gate import EMBEDDED_KEYS, ed25519_verify, key_id, public_key_from_pem

GENESIS = "0" * 64
SIGNED_PREFIX = b"mobster-audit-v1\x00"
EXPORT_PREFIX = b"mobster-audit-export-v2\x00"


class Unreadable(Exception):
    """The export or key file can't be read as one; the message says what to do."""


def _utf8(text):
    """The bytes a JS string encodes to (TextEncoder): a lone surrogate becomes U+FFFD."""
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace").encode("utf-8")


def _units(text):
    """A JS string's length: UTF-16 code units."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def entry_hash(row):
    """org-audit.ts › entryHash: SHA-256 hex over the row's place in the chain and every byte it holds."""
    sig = row["sig"] or ""
    entry = row["entry"]
    text = (f"{row['prev_hash']}|{row['seq']}|{row['at']}|{row['src']}|{row['mac_id'] or ''}|"
            f"{_units(entry)}|{entry}|{_units(sig)}|{sig}")
    return hashlib.sha256(_utf8(text)).hexdigest()


def _b64url(text):
    if (not isinstance(text, str) or not text
            or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in text)):
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        return None


def read_keys(text):
    """{mac id: 32 key bytes} from the keys file's text."""
    try:
        raw = json.loads(text)
    except ValueError:
        raise Unreadable("The keys file isn't JSON. It should look like {\"<mac id>\": \"<base64url Ed25519 key>\"}.")
    if not isinstance(raw, dict):
        raise Unreadable("The keys file should be a JSON object: {\"<mac id>\": \"<base64url Ed25519 key>\"}.")
    keys = {}
    for mac, value in raw.items():
        key = _b64url(value)
        if key is None or len(key) != 32:
            raise Unreadable(f"The key for Mac {mac} isn't a base64url Ed25519 public key (32 "
                             "bytes). Fix it in the keys file.")
        keys[mac] = key
    return keys


def _row(value, index):
    """One entry, checked for shape (the file is data from outside), or an Unreadable."""
    ok = (isinstance(value, dict) and all(isinstance(value.get(k), int) and not isinstance(value.get(k), bool)
                                          for k in ("seq", "at"))
          and all(isinstance(value.get(k), str) for k in ("src", "entry", "prev_hash", "hash"))
          and all(value.get(k) is None or isinstance(value.get(k), str) for k in ("mac_id", "sig")))
    if not ok:
        raise Unreadable(f"Entry {index + 1} in the file doesn't have the fields of an audit entry (seq, at, src, "
                         "mac_id, entry, sig, prev_hash, hash).")
    return value


def _link(value, name):
    if not (isinstance(value, dict) and isinstance(value.get("seq"), int) and isinstance(value.get("hash"), str)):
        raise Unreadable(f"The file has no valid {name} ({{seq, hash}}). It isn't an audit export.")
    return value


def verify(export, keys=None):
    """Check a parsed export (version 1), stopping at the first problem: nothing after it can be trusted."""
    if not isinstance(export, dict) or export.get("v") != 1 or not isinstance(export.get("entries"), list):
        raise Unreadable("This file isn't an audit export (version 1). Export the log as JSON from the console.")
    checkpoint, head = _link(export.get("checkpoint"), "checkpoint"), _link(export.get("head"), "head")
    rows = [_row(value, i) for i, value in enumerate(export["entries"])]
    result = {"ok": False, "entries": len(rows), "from": rows[0]["at"] if rows else None,
              "to": rows[-1]["at"] if rows else None,
              "partial": bool(export.get("partial")), "signatures_checked": keys is not None, "problem": None}

    def fail(entry, code, message):
        result["problem"] = {"entry": entry, "code": code, "message": message}
        return result

    prev, seen = {"seq": checkpoint["seq"], "hash": checkpoint["hash"]}, {}
    for r in rows:
        n = r["seq"]
        problem = _link_problem(prev, r)
        if problem:
            return fail(prev["seq"] + 1 if problem[0] == "gap" else n, *problem)
        if entry_hash(r) != r["hash"]:
            return fail(n, "hash", _hash_message(n))
        if keys is not None and r["src"] == "mac":
            problem = _signature_problem(r, keys, seen)
            if problem:
                return fail(n, *problem)
        prev = {"seq": n, "hash": r["hash"]}
    if prev["seq"] != head["seq"] or prev["hash"] != head["hash"]:
        return fail(prev["seq"] + 1, "head", f"The export ends at entry {prev['seq']}, but its head says entry "
                    f"{head['seq']}. Entries may have been cut off the end. Export the log again.")
    result["ok"] = True
    return result


def read_license_key(text):
    """The 32 key bytes of a license public key PEM, from `--key`."""
    key = public_key_from_pem(text)
    if key is None:
        raise Unreadable("The --key file isn't an Ed25519 public key in PEM form (-----BEGIN PUBLIC KEY-----). "
                         "Ask for the license public key, or leave --key out to use the one built into Mobster.")
    return key


def _lines(text):
    """The non-final lines of a JSON Lines file, parsed: [(line number, value or None when it isn't JSON)]. Blank lines
    are an edit."""
    out = []
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    for number, line in enumerate(parts, 1):
        try:
            out.append((number, json.loads(line)))
        except ValueError:
            out.append((number, None))
    return out


def _payload(head, license_keys):
    """(payload dict, kid) when the head is signed by one of `license_keys`, else (None, problem tuple)."""
    kid, payload, sig = head.get("kid"), _b64url(head.get("payload")), _b64url(head.get("sig"))
    if not isinstance(kid, str) or payload is None or sig is None:
        raise Unreadable("The first line isn't a signed head (kid, payload, sig). This isn't an audit export. Export "
                         "the log from the console or the API.")
    key = next((k for k in license_keys if key_id(k) == kid), None)
    if key is None:
        known = ", ".join(sorted(key_id(k) for k in license_keys)) or "none"
        return None, ("unknown_key", f"The head is signed with key {kid}, and the key this check uses is {known}. Pass "
                      "the right license public key with --key <file>, or check the file "
                      "came from your Mobster team.")
    if not ed25519_verify(key, EXPORT_PREFIX + payload, sig):
        return None, ("signature", "The head's signature doesn't match the license key: the head was changed, or the "
                      "file isn't from Mobster. Nothing in it can be trusted. Export the log again, and "
                      "keep this file as evidence.")
    try:
        body = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        body = None
    ints = ("count", "matched")
    ok = (isinstance(body, dict) and body.get("v") == 2 and isinstance(body.get("org"), str)
          and isinstance(body.get("exported_at"), int)
          and all(isinstance(body.get(k), int) and not isinstance(body.get(k), bool) for k in ints)
          and isinstance(body.get("filters"), dict) and isinstance(body.get("log_head"), dict)
          and all(body.get(k) is None or (isinstance(body.get(k), int) and not isinstance(body.get(k), bool))
                  for k in ("first_seq", "last_seq", "from", "to"))
          and all(body.get(k) is None or isinstance(body.get(k), str) for k in ("first_prev_hash", "last_hash")))
    if not ok:
        raise Unreadable("The signed head doesn't have the fields of an audit export (version 2). Export the log "
                         "again with a current Mobster.")
    return body, kid


def verify_lines(text, license_keys=None, keys=None):
    """Check a JSON Lines export (version 2), stopping at the first problem."""
    license_keys = list(license_keys if license_keys is not None else EMBEDDED_KEYS)
    lines = _lines(text)
    if not lines or not isinstance(lines[0][1], dict) or lines[0][1].get("t") != "head" or lines[0][1].get("v") != 2:
        raise Unreadable("This file isn't an audit export (JSON Lines, version 2): its first line should be the signed "
                         "head. Export the log from the console or the API.")
    result = {"ok": False, "format": 2, "entries": 0, "from": None, "to": None, "partial": False,
              "signatures_checked": keys is not None,
              "signed_by": None, "org": None, "exported_at": None, "matched": 0, "elided": 0, "filters": {},
              "log_head": None, "problem": None}

    def fail(entry, code, message):
        result["problem"] = {"entry": entry, "code": code, "message": message}
        return result

    body, kid = _payload(lines[0][1], license_keys)
    if body is None:
        return fail(None, *kid)
    result.update(signed_by=kid, org=body["org"], exported_at=body["exported_at"], filters=body["filters"],
                  log_head=body["log_head"],
                  entries=body["count"], matched=body["matched"], elided=body["count"] - body["matched"])
    result["partial"] = bool(body["filters"] or body["from"] is not None or body["to"] is not None
                             or (body["last_seq"] is not None and body["log_head"].get("seq") != body["last_seq"]))
    rows = lines[1:]
    prev, seen, shown, first_at, last_at, signed = None, 0, 0, None, None, {}
    for number, value in rows:
        t = value.get("t") if isinstance(value, dict) else None
        if t not in ("entry", "elided") or not _is_link(value, t):
            return fail(None, "line", f"Line {number} isn't an audit entry: it was changed or damaged. "
                        "This export can't be trusted. Export the log again, and "
                        "keep this file as evidence.")
        n = value["seq"]
        if prev is None:
            if body["first_seq"] is None or n != body["first_seq"] or value["prev_hash"] != body["first_prev_hash"]:
                return fail(
                    n, "head",
                    f"The first entry (entry {n}) isn't where the signed head says the export starts"
                    f"{'' if body['first_seq'] is None else
                       f' (entry {body['first_seq']}, linked to the entry before it)'}. "
                    "Entries may have been cut off the start, or the file edited. Export the log again.")
        else:
            problem = _link_problem(prev, value)
            if problem:
                return fail(prev["seq"] + 1 if problem[0] == "gap" else n, *problem)
        if t == "entry":
            if entry_hash(value) != value["hash"]:
                return fail(n, "hash", _hash_message(n))
            shown += 1
            first_at = value["at"] if first_at is None else first_at
            last_at = value["at"]
            if keys is not None and value["src"] == "mac":
                problem = _signature_problem(value, keys, signed)
                if problem:
                    return fail(n, *problem)
        prev = {"seq": n, "hash": value["hash"]}
        seen += 1
    result.update({"from": first_at, "to": last_at})
    if (seen != body["count"]
            or (prev is not None and (prev["seq"] != body["last_seq"] or prev["hash"] != body["last_hash"]))
            or (prev is None and body["count"] != 0)):
        last = prev["seq"] if prev else 0
        return fail(last + 1, "head", f"The file has {seen} entries ending at entry {last}, but its signed head says "
                    f"{body['count']} ending at entry {body['last_seq']}. Entries may have been cut "
                    "off the end or added. Export the log again.")
    if shown != body["matched"]:
        return fail(None, "matched", f"The file shows {shown} entries in full, but its signed head says "
                    f"{body['matched']} matched the filters. An entry was hidden or "
                    "restored. Export the log again.")
    result["ok"] = True
    return result


def _is_link(value, t):
    """Whether a line has the fields its type needs (the file is data from outside)."""
    ints = all(isinstance(value.get(k), int) and not isinstance(value.get(k), bool) for k in ("seq",)
               + (("at",) if t == "entry" else ()))
    strs = all(isinstance(value.get(k), str) for k in ("prev_hash", "hash")
               + (("src", "entry") if t == "entry" else ()))
    opt = t == "elided" or all(value.get(k) is None or isinstance(value.get(k), str) for k in ("mac_id", "sig"))
    return ints and strs and opt


def _link_problem(prev, r):
    """(code, message) when `r` doesn't follow `prev` in the chain, else None."""
    n = r["seq"]
    if n != prev["seq"] + 1:
        if n > prev["seq"] + 1:
            last = n - 1
            span = f"Entry {prev['seq'] + 1}" if last == prev["seq"] + 1 else f"Entries {prev['seq'] + 1} to {last}"
            return ("gap", f"{span} {'is' if last == prev['seq'] + 1 else 'are'} missing: the export jumps from entry "
                    f"{prev['seq']} to entry {n}. This export can't be trusted past entry "
                    f"{prev['seq']}. Export the log again.")
        return ("order", f"Entry {n} is out of order: it comes after entry {prev['seq']}. This export can't be trusted "
                f"from entry {n} on. Export the log again.")
    if r["prev_hash"] != prev["hash"]:
        return ("prev_hash", f"Entry {n} doesn't follow entry {n - 1}: its link to the entry before doesn't match. "
                             f"This export can't be trusted from entry {n} on. Export the log again.")
    return None


def _hash_message(n):
    return (f"Entry {n} was changed after it was written: its hash doesn't match. "
            f"This export can't be trusted from entry {n} on. Export the log again, and keep this file as evidence.")


def _signature_problem(r, keys, seen=None):
    """(code, message) when a Mac entry's signature fails against its Mac's key, else None. A signature binds the
    entry's words only, so the words must name the Mac whose key signed them and the same team as every other Mac
    entry, and no entry may come twice (the relay keeps one of each):
    ``seen`` holds the team and the entries so far."""
    n, mac = r["seq"], r["mac_id"]
    key = keys.get(mac) if mac else None
    if key is None:
        return ("no_key", f"Entry {n} is from Mac {mac}, and there is no key for Mac {mac} in the keys file. "
                "Add its key to the keys file, or ask the owner for it. Nothing "
                f"after entry {n} was checked.")
    sig = _b64url(r["sig"]) if r["sig"] else None
    try:
        entry = r["entry"].encode("utf-8")
    except UnicodeEncodeError:
        entry = None
    if sig is None or entry is None or not ed25519_verify(key, SIGNED_PREFIX + entry, sig):
        return (
            "signature",
            f"Entry {n} says it is from Mac {mac}, but its signature doesn't match that Mac's key. "
            f"This export can't be trusted from entry {n} on. "
            "Check you have the right keys file, then export the log again.",
        )
    try:
        body = json.loads(r["entry"])
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("mac") != mac:
        return (
            "mismatch",
            f"Entry {n} is signed with Mac {mac}'s key, but its words name another Mac. An entry was moved "
            f"between Macs. This export can't be trusted from entry {n} on. "
            "Export the log again, and keep this file as evidence.",
        )
    seen = {} if seen is None else seen
    if seen.setdefault("org", body.get("org")) != body.get("org"):
        return (
            "mismatch",
            f"Entry {n} names another team than the Mac entries before it. An entry was copied in from another "
            f"team's log. This export can't be trusted from entry {n} on. "
            "Export the log again, and keep this file as evidence.",
        )
    copies = seen.setdefault("entries", set())
    digest = hashlib.sha256(f"{mac}|{r['entry']}".encode()).digest()
    if digest in copies:
        return (
            "duplicate",
            f"Entry {n} repeats an earlier entry from Mac {mac} word for word, and a Mac never logs the same "
            f"entry twice. It was copied in. This export can't be trusted from entry {n} on. "
            "Export the log again, and keep this file as evidence.",
        )
    copies.add(digest)
    return None


def _day(ms, years):
    d = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
    return f"{d.day} {d:%b}" + (f" {d.year}" if years else "")


def summary(result):
    """The one-line answer, in words."""
    n = result["entries"]
    if n == 0:
        text = ("The export has no entries, and its signed head says so." if result.get("format") == 2
                else "The export has no entries, and its head matches its checkpoint.")
    elif result["from"] is None:
        text = f"All {n:,} entries chain, but the filters left every one out, so there is nothing to date."
    else:
        years = datetime.datetime.fromtimestamp(result["from"] / 1000, datetime.timezone.utc).year != \
            datetime.datetime.fromtimestamp(result["to"] / 1000, datetime.timezone.utc).year
        start, end = _day(result["from"], years), _day(result["to"], years)
        text = (f"All {n:,} entries check out, from {start} to {end}." if n > 1 else "The 1 entry "
                f"checks out, from {start}.")
    if result.get("format") == 2:
        text += f" The head is signed by Mobster's license key {result['signed_by']}."
        if result["elided"]:
            text += (f" {result['matched']:,} of the {n:,} entries are shown in full; the filters left out "
                     f"{result['elided']:,}, which still chain, so nothing between them was removed.")
    if result["partial"]:
        text += " The export is partial: it covers part of the log."
    if not result["signatures_checked"]:
        text += (" The Macs' signatures weren't checked. To check them, run this again with --keys <file>, "
                 "a JSON file of {\"<mac id>\": \"<base64url Ed25519 key>\"}.")
    return text


def add_arguments(parser, helpers):
    subs = parser.add_subparsers(dest="audit_command", metavar="<subcommand>")
    subs.required = True
    verify_parser = subs.add_parser(
        "verify", help="check an audit export's hash chain and signatures",
        description=("Check an audit export, offline. It follows the hash chain entry by entry and says which entry "
                     "first fails: a gap, an entry out of order, or one that was changed. For the JSON Lines export "
                     "from the API it also checks the signed head against Mobster's license key, which pins where the "
                     "export starts, where it ends and how many entries it holds, so a cut-off start or end is caught "
                     "too. With --keys it checks each Mac entry's signature against that Mac's key. The relay's own "
                     "entries are unsigned.\n\n"
                     "Get the Macs' keys from the Macs themselves, not from the export, so the relay can't vouch for "
                     "itself. Nothing leaves this Mac."),
        epilog=("examples:\n  mobster audit verify audit.jsonl\n  mobster audit verify audit.jsonl --keys keys.json\n"
                "  mobster audit verify export.json\n\n"
                "exit codes: 0 every entry checked out, 1 something didn't check out, 2 a file couldn't be read"))
    verify_parser.add_argument("file", metavar="FILE", help="the audit export: JSON Lines from the "
                               "API, or JSON from the console")
    verify_parser.add_argument("--key", metavar="FILE", help="the license public key, as a PEM, for a JSON Lines "
                               "export's signed head (default: "
                               "the key built into Mobster)")
    verify_parser.add_argument("--keys", metavar="FILE", help="a JSON file of {\"<mac id>\": \"<base64url Ed25519 "
                               "public key>\"}; checks every Mac entry's signature")
    verify_parser.add_argument("--json", action="store_true", help="print the result as one JSON object")


def _read(path, what):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError) as error:
        raise Unreadable(f"Couldn't read {what} {path}: "
                         f"{getattr(error, 'strerror', None) or 'not UTF-8 text'}. Check the path.")


def run(args):
    as_json = bool(getattr(args, "json", False))
    try:
        text = _read(args.file, "the export")
        keys = read_keys(_read(args.keys, "the keys file")) if args.keys else None
        license_keys = [read_license_key(_read(args.key, "the license key"))] if getattr(args, "key", None) else None
        if text.lstrip().startswith('{"t":"head"'):
            result = verify_lines(text, license_keys, keys)
        else:
            try:
                export = json.loads(text)
            except ValueError:
                raise Unreadable(f"{args.file} isn't JSON or JSON Lines, so it isn't an audit export. Export the log "
                                 "from the console or the API.")
            if getattr(args, "key", None):
                raise Unreadable("--key is for the JSON Lines export from the API, which has a signed head. This file "
                                 "is the console's JSON export, which has none.")
            result = verify(export, keys)
    except Unreadable as error:
        if as_json:
            print(json.dumps({"ok": False, "problem": {"entry": None, "code": "unreadable", "message": str(error)}}))
        else:
            print(f"mobster audit: {error}", file=sys.stderr)
        return EXIT_USAGE
    if as_json:
        print(json.dumps(result))
    elif result["ok"]:
        print(summary(result))
    else:
        print(f"mobster audit: {result['problem']['message']}", file=sys.stderr)
    return EXIT_OK if result["ok"] else EXIT_FAILED
