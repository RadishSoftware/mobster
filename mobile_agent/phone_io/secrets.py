"""One explicit local password entry, separate from model-driven typing and its guards."""

import getpass
import ipaddress
import os
import stat
import unicodedata
import urllib.request
import uuid
import warnings
from pathlib import Path
from urllib.parse import urlsplit

from . import PhoneIOError, choose_device, wda

LIMIT = 256
ROUTE = "/mobster/secret"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def read_secret(path=None):
    """Never accept a password in argv, from an echoed prompt, or from a public file."""
    try:
        if path is None:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass("Password (hidden): ")
        else:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077):
                    raise PhoneIOError("The password file must be a private regular file owned by you.", "usage",
                                       "Set its permissions to 600.")
                value = handle.read(LIMIT * 4 + 1).decode("utf-8")
                # Allow a single editor-added line ending; preserve every password space.
                value = value.removesuffix("\n").removesuffix("\r")
    except (OSError, UnicodeError, EOFError, getpass.GetPassWarning):
        raise PhoneIOError("Mobster couldn't read the password privately.", "usage",
                           "Use a terminal with hidden input, or --file with a private UTF-8 file.") from None
    if (not value or any(unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp") for char in value)
            or len(value.encode("utf-16-le")) // 2 > LIMIT):
        raise PhoneIOError("Use a nonempty password of at most 256 UTF-16 units without control characters "
                           "or line breaks.",
                           "usage")
    return value


def _local_transport(record):
    """Only Mobster's loopback relay: a bare LAN HTTP address exposes the password."""
    try:
        url = urlsplit(record.get("wdaUrl") or "")
        valid = (url.scheme == "http" and ipaddress.ip_address(url.hostname).is_loopback
                 and (url.port is None or url.port > 0)
                 and url.username is None and url.password is None and not url.query and not url.fragment
                 and url.path in ("", "/"))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise PhoneIOError("Password entry requires Mobster's local device relay.", "usage",
                           "Connect through Mobster's USB or paired Wi-Fi setup.")


def _check_answer(answer, *, sent=False):
    if isinstance(answer, dict) and answer.get("ok") is True:
        if (sent and answer.get("code") == "sent") or (not sent and type(answer.get("version")) is int
                                                      and answer["version"] == 1):
            return
    code = answer.get("code") if isinstance(answer, dict) else None
    if code in ("target", "foreground", "field"):
        raise PhoneIOError("The requested empty, focused secure password field is not ready.", "target",
                           "Open the named app, clear its password field, and tap that field yourself.")
    if sent:
        raise PhoneIOError("Password entry has an unknown outcome; Mobster did not retry.", "unknown_outcome",
                           "Inspect the phone before attempting another entry.")
    raise PhoneIOError("This runner doesn't support private password entry.", "unsupported",
                       "Rebuild the Mobster runner.")


def enter(call, *, app, field, read, journal):
    """The caller holds the device lease; read happens only after a no-secret preflight."""
    if not app or app == "com.apple.springboard" or not field:
        raise PhoneIOError("Name an app and its secure password field; phone passcodes aren't supported.", "usage")
    target = {"bundleId": app, "identifier": field}
    try:
        answer = call("POST", ROUTE + "/check", target, 10)
    except PhoneIOError:
        raise PhoneIOError("This runner couldn't verify private password entry.", "unsupported",
                           "Open the app's password field and rebuild the Mobster runner if needed.") from None
    _check_answer(answer)
    secret = read()
    attempt = uuid.uuid4().hex
    journal({"event": "password_entry_intent", "attempt": attempt})
    try:
        # No fallback to WDA /keys or /element/value: those log unredacted typing events.
        answer = call("POST", ROUTE, {**target, "secret": secret}, 30)
    except (Exception, KeyboardInterrupt):
        raise PhoneIOError("Password entry has an unknown outcome; Mobster did not retry.", "unknown_outcome",
                           "Inspect the phone before attempting another entry.") from None
    finally:
        secret = None
    _check_answer(answer, sent=True)
    try:
        journal({"event": "password_entry_sent", "attempt": attempt})
    except (Exception, KeyboardInterrupt):
        raise PhoneIOError("The password was sent, but its completion record could not be saved.", "unknown_outcome",
                           "Inspect the phone; do not repeat the command automatically.") from None
    return {"ok": True, "sent": True, "submitted": False}


def add_arguments(subs, helpers):
    parser = subs.add_parser("password", help="enter a password privately into a focused secure app field",
                             description="Enter one password from a hidden prompt or a private UTF-8 file. "
                                         "Requires a rebuilt Mobster runner and an empty, focused secure field. "
                                         "Does not submit, retry, or enter a phone passcode.")
    parser.add_argument("--app", required=True, metavar="BUNDLE", help="the foreground app's bundle ID")
    parser.add_argument("--field", required=True, metavar="NAME",
                        help="the secure field's accessibility identifier or label")
    parser.add_argument("--file", type=helpers.get("path_type"), metavar="FILE",
                        help="read the password from a private UTF-8 file")
    parser.add_argument("--device", metavar="NAME", help="the device's id, UDID or name")
    parser.add_argument("--json", action="store_true", help="print one JSON object without the password")


def run(args):
    from ..env_file import private_dir
    from ..journal import append_jsonl
    from ..paths import user_data_dir
    from .cli import _print

    record = choose_device(args.device)
    _local_transport(record)
    folder = Path(user_data_dir()) / "private-input"
    private_dir(folder)
    # A loopback URL must stay local even when the shell configures an HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with wda(record, opener=opener.open) as call:
        result = enter(call, app=args.app, field=args.field, read=lambda: read_secret(args.file),
                       journal=lambda event: append_jsonl(folder / "attempts.jsonl", event))
    _print(result, args.json, "Sent the password to the secure field. Mobster did not submit the form.")
    return 0
