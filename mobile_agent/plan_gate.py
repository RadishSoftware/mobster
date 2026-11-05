"""The Mac app's plan check, in the agent's own run path: a signed token checked offline,
and anything unreadable refuses."""

import base64
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import build_identity, genuine, premium_model
from .api_errors import APIError

ACCOUNT_FILE = "account.json"
# The sentence names no amount: the site owns the prices, and a coding agent passes it to the person as it is.
BUY_URL = "mobster.dev/pricing"
TRIAL_ENDED = ("Your 7-day free trial on this Mac has ended, so Mobster did nothing: choose a plan at "
               f"{BUY_URL}, then ask again.")
NO_PLAN = ("This Mac has no active Mobster plan or free trial, so Mobster did nothing: open Mobster to start your free "
           f"trial or sign in, or choose a plan at {BUY_URL}, then ask again.")
# A team's station whose Team plan doesn't license it now; `{team}` is a name, never an instruction.
NO_TEAM_PLAN = ("{team}'s Team plan isn't active on this Mac, so Mobster did nothing. An Owner can choose a plan in "
                "your account, then ask again.")
# One Mac at a time: another Mac took this seat; `{mac}` is a name, never an instruction.
MOVED = ("Mobster moved to {mac}, so it did nothing on this Mac. Mobster runs on one Mac at a time: to use it here, "
         "open Mobster on this Mac and choose Move it back, then ask again.")
# Every plan's token ends 7 days after the check that signed it (services/license-api CHECK_IN_S);
# the plan hasn't ended.
CHECK_IN = "Mobster couldn't check your plan for 7 days, so it did nothing. Connect to the internet, then try again."
# desktop/src-tauri/license-public.pem; MACHINE_SALT and INACTIVE_STATUSES match entitlement.rs.
PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAPykA81VTckj/R8OOQ2CFhAGhlQvkFSZYr6+t5ipU4KY=
-----END PUBLIC KEY-----
"""
MACHINE_SALT = "mobster-license-machine-v1"
INACTIVE_STATUSES = frozenset({"canceled", "cancelled", "revoked", "refunded", "disputed", "unpaid",
                               "incomplete_expired", "inactive"})
PLANS = frozenset({"lifetime", "monthly", "yearly", "trial"})
TEAM_PLANS = frozenset({"team", "team_trial"})
GATE_OFF_ENV = "MOBSTER_ACCOUNT_GATE"


# Ed25519 verification (RFC 8032 §5.1.7) in plain Python: the agent ships no crypto library.
_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    first, second = (y1 - x1) * (y2 - x2) % _P, (y1 + x1) * (y2 + x2) % _P
    third, fourth = 2 * t1 * t2 * _D % _P, 2 * z1 * z2 % _P
    e, f, g, h = second - first, fourth - third, fourth + third, second + first
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _mul(scalar, point):
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _same(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _x_for(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    return _P - x if (x & 1) != sign else x


def _point(data):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _x_for(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_BASE = (_x_for(4 * pow(5, _P - 2, _P) % _P, 0), 4 * pow(5, _P - 2, _P) % _P, 1, 0)
_BASE = (_BASE[0], _BASE[1], 1, _BASE[0] * _BASE[1] % _P)


def _hash(data):
    return int.from_bytes(hashlib.sha512(data).digest(), "little") % _Q


def key_id(key):
    """The license signing key's ID: the first 16 hex characters of its raw bytes' SHA-256."""
    return hashlib.sha256(key).hexdigest()[:16]


def ed25519_verify(public, message, signature):
    """Whether ``signature`` (64 bytes) is ``public``'s (32 bytes) over ``message``."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a, r = _point(public), _point(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _Q:
        return False
    return _same(_mul(s, _BASE), _add(r, _mul(_hash(signature[:32] + public + message), a)))


def public_key_from_pem(pem):
    """The 32 key bytes of an Ed25519 SPKI PEM, or None."""
    body = "".join(line.strip() for line in pem.splitlines() if not line.startswith("-----"))
    try:
        der = base64.b64decode(body, validate=True)
    except ValueError:
        return None
    prefix = bytes.fromhex("302a300506032b6570032100")
    return der[12:] if len(der) == 44 and der[:12] == prefix else None


EMBEDDED_KEYS = tuple(key for key in (public_key_from_pem(PUBLIC_KEY_PEM),) if key)
DEV_KEY_ENV = "MOBSTER_LICENSE_PUBLIC_KEY"
DEV_MACHINE_ENV = "MOBSTER_DEV_MACHINE"


def local_account_api(environ):
    """Whether MOBSTER_ACCOUNT_API is a license server on this Mac's loopback (http://127.0.0.1)."""
    from urllib.parse import urlsplit
    parts = urlsplit((environ.get("MOBSTER_ACCOUNT_API") or "").strip())
    return parts.scheme == "http" and parts.hostname == "127.0.0.1"


def trusted_keys(environ=None):
    """The license keys this Mac trusts."""
    environ = os.environ if environ is None else environ
    raw = (environ.get(DEV_KEY_ENV) or "").strip()
    if not raw or getattr(sys, "frozen", False) or not local_account_api(environ):
        return EMBEDDED_KEYS
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except ValueError:
        return EMBEDDED_KEYS
    return (key,) if len(key) == 32 else EMBEDDED_KEYS


def _b64url(text):
    text = text.strip()
    if not text or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=" for c in text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text.rstrip("=") + "=" * (-len(text.rstrip("=")) % 4))


def seconds(value):
    """Unix seconds; a value in milliseconds is scaled down (entitlement.rs › seconds)."""
    return value // 1000 if value > 100_000_000_000 else value


class TokenProblem(Exception):
    """Why a token doesn't license this Mac."""

    def __init__(self, problem, plan=None):
        super().__init__(problem)
        self.problem = problem
        self.plan = plan


def verified_payload(token, keys=EMBEDDED_KEYS):
    """The payload a token's signature covers, once it verifies with one of ``keys``."""
    if not isinstance(token, str) or token.count(".") != 1:
        raise TokenProblem("malformed")
    segment, signature = token.strip().split(".")
    try:
        payload, signature = _b64url(segment), _b64url(signature)
    except (ValueError, TypeError):
        raise TokenProblem("malformed") from None
    if len(signature) != 64:
        raise TokenProblem("malformed")
    if not any(ed25519_verify(key, payload, signature) or ed25519_verify(key, segment.encode(), signature)
               for key in keys):
        raise TokenProblem("bad_signature")
    return payload


def check_token(token, sub, machine, now, keys=EMBEDDED_KEYS, payload=None):
    """The claims of a version 2 token for account ``sub`` on ``machine`` at ``now`` (Unix seconds)."""
    if payload is None:
        payload = verified_payload(token, keys)
    try:
        claims = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise TokenProblem("malformed") from None
    if not isinstance(claims, dict) or type(claims.get("v")) is not int or claims["v"] != 2:
        raise TokenProblem("unsupported_version")
    exp = claims.get("exp")
    if (not all(isinstance(claims.get(name), str) for name in ("sub", "status", "machine"))
            or claims.get("plan") not in PLANS or type(claims.get("iat")) is not int
            or (exp is not None and type(exp) is not int)):
        raise TokenProblem("malformed")
    if not isinstance(machine, str) or not machine or claims["machine"].lower() != machine.lower():
        raise TokenProblem("wrong_machine")
    if not isinstance(sub, str) or claims["sub"].lower() != sub.lower():
        raise TokenProblem("wrong_account")
    if claims["status"].lower() in INACTIVE_STATUSES:
        raise TokenProblem("inactive", claims["plan"])
    if exp is not None and now >= seconds(exp):
        raise TokenProblem("expired", claims["plan"])
    return claims


def check_station_token(token, org, station, machine, now, keys=EMBEDDED_KEYS, payload=None):
    """The claims of a version 3 (team station) token for station ``station`` of team ``org`` on ``machine`` at ``now``
    (Unix seconds); TokenProblem otherwise (entitlement.rs › check_station_token)."""
    if payload is None:
        payload = verified_payload(token, keys)
    try:
        claims = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise TokenProblem("malformed") from None
    if not isinstance(claims, dict) or type(claims.get("v")) is not int or claims["v"] != 3:
        raise TokenProblem("unsupported_version")
    if (not all(isinstance(claims.get(name), str) and claims[name]
                for name in ("sub", "org", "station", "status", "machine"))
            or claims.get("plan") not in TEAM_PLANS or type(claims.get("iat")) is not int
            or type(claims.get("exp")) is not int or claims["sub"].lower() != claims["org"].lower()):
        raise TokenProblem("malformed")
    if not isinstance(machine, str) or not machine or claims["machine"].lower() != machine.lower():
        raise TokenProblem("wrong_machine")
    if not isinstance(org, str) or claims["org"].lower() != org.lower():
        raise TokenProblem("wrong_org", claims["plan"])
    if not isinstance(station, str) or claims["station"].lower() != station.lower():
        raise TokenProblem("wrong_station", claims["plan"])
    if claims["status"].lower() in INACTIVE_STATUSES:
        raise TokenProblem("inactive", claims["plan"])
    if now >= seconds(claims["exp"]):
        raise TokenProblem("expired", claims["plan"])
    return claims


# A CI token (version 4) licenses simulator runs of `mobster ci run` and `mobster test` on a machine with no account,
# such as a hosted CI runner. It names no machine, always expires, and never licenses a phone.
CI_TOKEN_ENV = "MOBSTER_CI_TOKEN"
CI_TOKENS_ENV = "MOBSTER_CI_TOKENS"
# Dark until the owner turns CI tokens on: they join the flat plan only once he confirms it.
CI_TOKENS_ENABLED = False
CI_COMMANDS = frozenset({"ci", "test"})


def ci_tokens_enabled(environ=None):
    environ = os.environ if environ is None else environ
    return CI_TOKENS_ENABLED or environ.get(CI_TOKENS_ENV) == "on"


def check_ci_token(token, now, keys=EMBEDDED_KEYS, payload=None):
    """The claims of a version 4 CI token at ``now`` (Unix seconds); TokenProblem otherwise."""
    if payload is None:
        payload = verified_payload(token, keys)
    try:
        claims = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise TokenProblem("malformed") from None
    if not isinstance(claims, dict) or type(claims.get("v")) is not int or claims["v"] != 4:
        raise TokenProblem("unsupported_version")
    if (claims.get("kind") != "ci" or claims.get("scope") != "simulator"
            or not all(isinstance(claims.get(name), str) and claims[name] for name in ("sub", "status"))
            or claims.get("plan") not in PLANS | TEAM_PLANS or type(claims.get("iat")) is not int
            or type(claims.get("exp")) is not int):
        raise TokenProblem("malformed")
    if claims["status"].lower() in INACTIVE_STATUSES:
        raise TokenProblem("inactive", claims["plan"])
    if now >= seconds(claims["exp"]):
        raise TokenProblem("expired", claims["plan"])
    return claims


CI_CHECK_PATH = "/api/ci-tokens/check"
CI_CHECK_SECONDS = 5.0
CI_REFUSALS = frozenset({"revoked", "expired", "plan_required"})


def ci_token_answer(token):
    """What the license API says of ``token``: "ok", why it refuses (any 401 or 403 it doesn't name reads as revoked),
    or None when it can't be asked (no network, a pin mismatch, a busy or failing server)."""
    from . import share
    try:
        status, value = share.call("POST", CI_CHECK_PATH, token, None, timeout=CI_CHECK_SECONDS, limit=4096)
    except share.ShareError:
        return None
    if status == 200:
        return "ok"
    if status in (401, 403):
        error = value.get("error")
        return error if error in CI_REFUSALS else "revoked"
    return None


def ci_token_online(token):
    """None when the license API vouches for ``token``, else why it refuses. None too when it can't be asked: with
    ``premiumModel`` off the signature and expiry, checked offline, stand then (ci_token_problem)."""
    answer = ci_token_answer(token)
    return None if answer == "ok" else answer


def ci_token_problem(environ=None, now=None, keys=None, online=None):
    """None when ``MOBSTER_CI_TOKEN`` is a live CI token and CI tokens are on, else why not.

    The signature and expiry are checked here, offline. Then the license API is asked whether the token was revoked
    (``online``, `ci_token_online` by default) and its answer stands when there is one."""
    environ = os.environ if environ is None else environ
    if not ci_tokens_enabled(environ):
        return "off"
    token = (environ.get(CI_TOKEN_ENV) or "").strip()
    if not token:
        return "no_token"
    try:
        check_ci_token(token, int(time.time()) if now is None else now,
                       trusted_keys(environ) if keys is None else keys)
    except TokenProblem as problem:
        return problem.problem
    return (online or ci_token_online)(token)


# With premiumModel on no run needs a CI token, but everything under one that's there refuses unless the license API
# vouched for it: each verdict's sentence (ci_token_verdict).
CI_TOKEN_REFUSALS = {
    "revoked": f"This CI token was revoked, so Mobster did nothing. Ask whoever made it, or take {CI_TOKEN_ENV} out of "
               "this job.",
    "expired": f"This CI token has ended, so Mobster did nothing. Create a new one and replace the secret, or take "
               f"{CI_TOKEN_ENV} out of this job.",
    "plan_required": f"The plan behind this CI token isn't active, so Mobster did nothing. Check the plan, or take "
                     f"{CI_TOKEN_ENV} out of this job.",
    "unsendable": f"This CI token has characters no request can carry, so Mobster couldn't check it and did nothing. "
                  f"Copy the token into the secret again, or take {CI_TOKEN_ENV} out of this job.",
    "unchecked": f"Mobster couldn't check this CI token with its license service, so it did nothing. Check this "
                 f"runner's network, or take {CI_TOKEN_ENV} out of this job.",
}
# How long a verdict stands, in seconds: one the license API vouched for 5 minutes, and an unanswered check 30 s, so a
# long-lived MCP server doesn't wait on the API at every tool call. A refusal stands for the life of the process.
CI_TOKEN_STANDS = {"ok": 300.0, "unchecked": 30.0}
# What still runs under a token that refuses, beside the plain doctor: `ci doctor` says what's wrong (none of its flags
# acts: --hosted picks the rows, --json the format), and `ci init` writes a workflow file. Neither touches a phone.
CI_TOKEN_EXEMPT_CI = frozenset({"doctor", "init"})
_ci_token_verdicts = {}  # token: (when it was asked, its verdict), for a long-lived process's many tool calls
_ci_token_lock = threading.Lock()


def ci_token_verdict(environ=None, ask=None, clock=time.monotonic):
    """With ``premiumModel`` on, what this job's CI token lets run: None with no token, so a free path asks nothing and
    being offline costs it nothing; "ok" once the license API vouched for it; else a CI_TOKEN_REFUSALS key, and
    everything under it refuses. None with the flag off, where ci_token_problem decides."""
    environ = os.environ if environ is None else environ
    if not premium_model.enabled(environ):
        return None
    token = (environ.get(CI_TOKEN_ENV) or "").strip()
    if not token:
        return None
    with _ci_token_lock:
        asked, verdict = _ci_token_verdicts.get(token, (None, None))
    if verdict is not None and clock() - asked < CI_TOKEN_STANDS.get(verdict, math.inf):
        return verdict
    try:
        verdict = (ask or ci_token_answer)(token) or "unchecked"
    except ValueError:  # http.client refuses a header with a line break in it: such a token can't be verified
        verdict = "unsendable"
    with _ci_token_lock:
        kept = _ci_token_verdicts.get(token, (None, None))[1]
        if kept is not None and kept not in CI_TOKEN_STANDS:  # a refusal another call heard first stands
            return kept
        _ci_token_verdicts[token] = (clock(), verdict)
    return verdict


def ci_token_stop(command, args=None, environ=None, ask=None):
    """The sentence that stops ``command`` under this job's CI token, else None: every command but the exempt ones."""
    if plain_doctor(command, args) or (command == "ci" and getattr(args, "ci_command", None) in CI_TOKEN_EXEMPT_CI):
        return None
    return ci_token_refusal(ci_token_verdict(environ, ask))


def ci_token_refusal(verdict):
    """None for no token or one the license API vouched for ("ok"); else the sentence that refuses, and a verdict no
    sentence names reads as unchecked."""
    if verdict is None or verdict == "ok":
        return None
    return CI_TOKEN_REFUSALS.get(verdict, CI_TOKEN_REFUSALS["unchecked"])


def plain_doctor(command, args=None):
    """Whether ``command`` is `mobster doctor` with neither --fix, which starts (and for an expired helper rebuilds and
    installs) the helper on the phone and stops processes holding Mobster's ports, nor --apps, which opens every app on
    the phone and presses Home. It only says what's wrong, so it runs in a copy that fails its seal and under a CI token
    that refuses. A flag it can't read counts as given."""
    return command == "doctor" and getattr(args, "fix", True) is False and getattr(args, "apps", True) is False


MODIFIED = ("This copy of Mobster was changed after it was signed, so Mobster did nothing. Download Mobster again from "
            "mobster.dev/download and replace the one in Applications, then ask again.")
UNCHECKED_COPY = ("Mobster couldn't check that this copy is the one it signed, so it did nothing. Download Mobster "
                  "again from mobster.dev/download and replace the one in Applications, then ask again.")
# The sentence for each way a copy fails its seal (genuine.failure); any other reads as unchecked.
SEAL_REFUSALS = {genuine.CHANGED: MODIFIED, genuine.UNCHECKED: UNCHECKED_COPY}


def seal_refusal():
    """None when this copy passed its seal check or none runs (genuine.failure), else the sentence that says why not."""
    failed = genuine.failure()
    return None if failed is None else SEAL_REFUSALS.get(failed, UNCHECKED_COPY)


# `mobster mcp` starts, so an MCP client gets the sentence on each tool it calls (``TOOL``) rather than a server that
# never answers; every tool but `stop` refuses. Equally strict, and the person sees why.
SERVER_COMMANDS = frozenset({"mcp"})
TOOL = "tool"


def stopped(command, args=None):
    """The sentence that stops ``command`` whatever it does, else None: a copy that fails its seal check (genuine.py)
    or a CI token that refuses, both with ``premiumModel`` on. The MCP server asks with ``TOOL`` for every tool but
    ``stop``."""
    if command in SERVER_COMMANDS:
        return None
    # A failed seal stops all but the plain doctor and the version (help is answered before this).
    sealed = None if command == "version" or plain_doctor(command, args) else seal_refusal()
    return sealed if sealed is not None else ci_token_stop(command, args)


_ci_scope = []  # the token the CLI let in for this process (grant_ci_token)


def grant_ci_token(command, args, environ=None, named_device=None, online=None):
    """Whether a live CI token licenses ``command``: a simulator run of `ci` or `test`, never a phone.

    On success this process's gates accept the token. Anything else refuses."""
    _ci_scope.clear()
    if command not in CI_COMMANDS or ci_token_problem(environ, online=online) is not None:
        return False
    if may_drive_real_phone(args, named_device):
        return False
    environ = os.environ if environ is None else environ
    _ci_scope.append(environ[CI_TOKEN_ENV].strip())
    return True


def machine_id_from(platform_uuid):
    return hashlib.sha256(f"{platform_uuid.strip()}{MACHINE_SALT}".encode()).hexdigest()


def platform_uuid_from_ioreg(text):
    for line in text.splitlines():
        if '"IOPlatformUUID"' in line and "=" in line:
            value = line.split("=", 1)[1].strip().strip('"').strip()
            return value or None
    return None


def dev_machine_tag(environ=None):
    """MOBSTER_DEV_MACHINE in a source checkout beside a loopback license server, else None."""
    environ = os.environ if environ is None else environ
    tag = (environ.get(DEV_MACHINE_ENV) or "").strip()
    if not tag or getattr(sys, "frozen", False) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", tag) \
            or not local_account_api(environ):
        return None
    return tag


def machine_id():
    """This Mac's id, or None where the platform UUID can't be read."""
    try:
        text = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True,
                              text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    uuid = platform_uuid_from_ioreg(text)
    if not uuid:
        return None
    tag = dev_machine_tag()
    return machine_id_from(f"{uuid}:{tag}" if tag else uuid)


def bundled():
    """Whether this frozen process enforces Mobster.app's checks (never the standalone CLI)."""
    return build_identity.app_bundle()


def app_account_file():
    """The Mac app's account.json (its data folder, paths.user_data_dir)."""
    from .paths import user_data_dir
    return user_data_dir() / ACCOUNT_FILE


def for_runtime(config, data_dir):
    """The gate a Runtime keeps, or None: the bundled agent always; a source checkout's only for the Mac app."""
    if bundled():
        return PlanGate(app_account_file())
    if getattr(config, "require_plan", False):
        return PlanGate(Path(data_dir) / ACCOUNT_FILE)
    return None


def bundled_gate():
    """The gate for the bundled agent's commands and MCP tools; None in a source checkout."""
    return PlanGate(app_account_file()) if bundled() else None


PHONE_READS = frozenset({("clipboard", "get"), ("file", "get"), ("file", "ls"), ("photos", "pull")})


def cli_needs_plan(command, args, named_device=None):
    """Whether the bundled agent's ``command`` acts on a phone or runs a task."""
    if command == "run":
        return getattr(args, "demo", False) is not True
    if command in ("verify", "test"):
        return may_drive_real_phone(args, named_device)
    if command == "ci":
        return getattr(args, "ci_command", None) == "run"
    if command == "expo":
        return getattr(args, "expo_command", None) in ("preview", "dev", "reload", "build")
    if command == "asc":
        return getattr(args, "asc_command", None) not in (None, "key")
    if command == "testflight":
        return getattr(args, "testflight_command", None) not in (None, "key")
    if command == "xcode":
        return getattr(args, "xcode_command", None) == "after-build"
    if command == "links":
        return getattr(args, "links_command", None) == "check" and may_drive_real_phone(args, named_device)
    if command == "fixtures":
        return getattr(args, "fixtures_command", None) == "apply"
    if command == "xray":
        # Mapping an app or recording a phone needs the plan; `diff` and `network --from` read files only.
        verb = getattr(args, "xray_command", None)
        return verb == "app" or (verb == "network" and not getattr(args, "from_file", None))
    if command == "phone":
        verb = getattr(args, "phone_command", None)
        return verb is not None and (verb, getattr(args, f"{verb}_command", None)) not in PHONE_READS
    return False


def may_drive_real_phone(args, named_device=None):
    """Whether `mobster verify` or `mobster test` may run on a real iPhone."""
    if getattr(args, "testflight", None):
        return True
    names = getattr(args, "device", None)
    if names is None:
        return False
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple)):
        return True
    if not names:
        return False
    try:
        if named_device is None:
            from .device_targets import named_device
        records = [named_device(name) for name in names]
    except Exception:
        return True
    return any(not isinstance(record, dict) or record.get("kind") != "simulator" for record in records)


def mcp_needs_plan(name, annotations):
    """Whether an MCP tool can need the plan: everything but the read-only tools and ``stop``."""
    return name != "stop" and not (annotations or {}).get("readOnlyHint")


def gate_turned_off(environ=None):
    """``premiumModel`` is on (nothing is walled but the model), or ``MOBSTER_ACCOUNT_GATE=off`` in a source checkout,
    as the shell's development builds read it."""
    environ = os.environ if environ is None else environ
    return premium_model.enabled(environ) or (environ.get(GATE_OFF_ENV) == "off" and not getattr(sys, "frozen", False))


def refusal_text(problem, plan=None, team=None):
    """The sentence a refused task or tool says for ``problem`` (``PlanGate.status``)."""
    if plan in TEAM_PLANS:
        return team_text(team)
    return TRIAL_ENDED if plan == "trial" and problem in ("expired", "inactive") else NO_PLAN


def plain_name(value, limit=64):
    """A name to put in a sentence: control characters out, spaces collapsed, at most ``limit`` characters."""
    text = value if isinstance(value, str) else ""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())[:limit]


def team_text(team):
    """The refusal on a team's station whose Team plan doesn't license it: ``team`` is the team's name, or None."""
    return NO_TEAM_PLAN.format(team=plain_name(team, 80) or "Your team")


def plan_outlives_its_token(account_file, plan, now):
    """Whether the plan itself still runs at ``now`` although its token ended."""
    stored = stored_account(account_file)
    if stored is None:
        return False
    entitlement = stored["entitlement"]
    if str(entitlement.get("status") or "").lower() in INACTIVE_STATUSES:
        return False
    if plan == "lifetime":
        return True
    end = entitlement.get("exp")
    return type(end) is int and seconds(end) > now


def moved_text(mac):
    """The refusal on a Mac whose seat moved to ``mac``: what happened, where Mobster went, and how to move it back."""
    return MOVED.format(mac=plain_name(mac) or "another Mac")


def moved_to(account_file):
    """The Mac that took this Mac's seat, by name ("" when not known yet), when account.json says the seat moved."""
    try:
        stored = json.loads(Path(account_file).read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict):
        return None
    user = stored.get("user") if isinstance(stored.get("user"), dict) else {}
    entitlement = stored.get("entitlement") if isinstance(stored.get("entitlement"), dict) else {}
    mac = stored.get("moved_to")
    if not isinstance(user.get("id"), str) or not user["id"] or entitlement.get("token") or not isinstance(mac, str):
        return None
    return mac


def stored_station(account_file):
    """account.json's ``station`` as the shell saved it (account.rs › Stored.station)."""
    try:
        stored = json.loads(Path(account_file).read_bytes())
    except (OSError, ValueError):
        return None
    station = stored.get("station") if isinstance(stored, dict) else None
    if not isinstance(station, dict) or not all(isinstance(station.get(name), str) and station[name]
                                                for name in ("org", "station", "token")):
        return None
    return {"org": station["org"], "station": station["station"], "token": station["token"],
            "name": station.get("name") if isinstance(station.get("name"), str) else None,
            "max_seen": stored.get("max_seen")}


def stored_account(account_file):
    """account.json as the shell saved it, when it holds a signed-in account and its entitlement token (``user.id`` and
    ``entitlement.token``, non-empty strings); else None."""
    try:
        stored = json.loads(Path(account_file).read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict):
        return None
    user = stored.get("user") if isinstance(stored.get("user"), dict) else {}
    entitlement = stored.get("entitlement") if isinstance(stored.get("entitlement"), dict) else {}
    token, sub = entitlement.get("token"), user.get("id")
    if not isinstance(token, str) or not token or not isinstance(sub, str) or not sub:
        return None
    return stored


class PlanGate:
    """Whether this Mac's account may run a task now, from ``account_file`` (the shell's account.json)."""

    def __init__(self, account_file, *, keys=None, machine=None, clock=time.time, turned_off=None, station=None):
        self.account_file = Path(account_file)
        self.keys = trusted_keys() if keys is None else keys
        self.clock = clock
        self.turned_off = gate_turned_off() if turned_off is None else turned_off
        self._machine = machine
        # Answers (team id, station id) while this Mac is a team's station, else None; without it a station token never
        # licenses this Mac.
        self.station = station
        self._lock = threading.Lock()
        self._seen = 0
        self._verified = {}

    def machine(self):
        with self._lock:
            if self._machine is None:
                self._machine = machine_id() or ""
            return self._machine

    def status(self):
        """(None, None) when a task may start now, else (why not, the plan the token carried or None)."""
        if genuine.failure() is not None:  # premiumModel on, before the gate it turns off: serve and the app's tasks
            return "modified", None
        if self.turned_off or self._ci_licensed():
            return None, None
        stored, station = stored_account(self.account_file), stored_station(self.account_file)
        if stored is None and station is None:
            return "no_account", None
        now = self._now((stored or station).get("max_seen"))
        if station is not None:
            # A machine is a person's Mac or a station, never both,
            # so a personal token beside a station token licenses nothing here.
            problem, plan, _ = self._station_check(station, now)
            return (None, None) if problem is None else (problem, plan)
        token, sub = stored["entitlement"]["token"], stored["user"]["id"]
        try:
            check_token(token, sub, self.machine(), now, payload=self._payload(token))
        except TokenProblem as problem:
            return problem.problem, problem.plan
        return None, None

    def _ci_licensed(self):
        """Whether the CLI let a live CI token in for this simulator-only run."""
        if not _ci_scope:
            return False
        try:
            check_ci_token(_ci_scope[0], int(self.clock()), self.keys)
        except TokenProblem:
            return False
        return True

    def station_status(self):
        """The team's entitlement on this Mac, alone."""
        station = stored_station(self.account_file)
        if station is None:
            return "no_token", None, None
        return self._station_check(station, self._now(station.get("max_seen")))

    def _now(self, seen):
        """The latest time this Mac has shown: its clock, this process's, and account.json's ``max_seen``."""
        with self._lock:
            now = max(int(self.clock()), self._seen, seen if type(seen) is int else 0)
            self._seen = now
        return now

    def _station_check(self, station, now):
        try:
            payload = self._payload(station["token"])
            ours = self._station_identity()
            if ours is None:
                plan = json.loads(payload).get("plan") if payload else None
                raise TokenProblem("not_a_station", plan if plan in TEAM_PLANS else "team")
            claims = check_station_token(station["token"], ours[0], ours[1], self.machine(), now, payload=payload)
        except TokenProblem as problem:
            return problem.problem, problem.plan or "team", None
        except (ValueError, AttributeError):
            return "malformed", "team", None
        return None, claims["plan"], claims

    def _payload(self, token):
        """``token``'s verified payload, from the cache or checked now; TokenProblem when its signature fails."""
        with self._lock:
            payload = self._verified.get(token)
        if payload is None:
            payload = verified_payload(token, self.keys)
            with self._lock:
                if len(self._verified) >= 8:
                    self._verified.clear()
                self._verified[token] = payload
        return payload

    def _station_identity(self):
        """(team id, station id) from this Mac's team state, or None: no state, none set, or it can't be read."""
        source = self.station
        if source is None:
            return None
        try:
            identity = source()
        except Exception:  # noqa: BLE001 -- unreadable team state refuses (fail closed)
            return None
        if (not isinstance(identity, tuple) or len(identity) != 2
                or not all(isinstance(part, str) and part for part in identity)):
            return None
        return identity

    def problem(self):
        """None when a task may start now, else why not (a TokenProblem's name, or no_account)."""
        return self.status()[0]

    def refusal(self):
        """None when a task may start now, else the sentence that says why not and what to do."""
        problem, plan = self.status()
        if problem is None:
            return None
        if problem == "modified":
            return seal_refusal() or UNCHECKED_COPY
        if plan in TEAM_PLANS:
            station = stored_station(self.account_file)
            return team_text(station["name"] if station else None)
        with self._lock:
            now = self._seen
        if problem == "expired" and plan_outlives_its_token(self.account_file, plan, now):
            return CHECK_IN
        mac = moved_to(self.account_file) if problem == "no_account" else None
        return moved_text(mac) if mac is not None else refusal_text(problem, plan)

    def require(self):
        """APIError 403 plan_required unless a task may start now."""
        message = self.refusal()
        if message is not None:
            raise APIError(message, 403, "plan_required")

    def allows(self):
        return self.problem() is None
