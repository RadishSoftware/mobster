"""Alerts for scheduled tasks: a webhook post when one fails, stops or waits on an approval for a minute.

Opt-in: nothing is sent until MOBSTER_ALERT_WEBHOOK_URL holds an https address (Settings in the Mac app, or
the env file). The post is shaped for the service the address belongs to: Slack ({"text"}), Discord
({"content"}), ntfy (plain text with a Title header), or anything else as JSON {workflow, status, reason, runId,
at, text}. It never carries the task's answer, a screenshot, typed text or any secret: the reason is one of
Mobster's own fixed sentences, chosen by the run's status.

Delivery never blocks or fails a run: it happens on its own thread, tries twice with a 5-second limit each, and
gives up quietly. Addresses on this Mac's network (localhost, private, link-local and reserved addresses, .local
names) are refused, the name is resolved once and that address is the one connected to, and redirects are not
followed, so an alert can't be turned against something on the local network.

`mobster alerts test` sends a test post (``add_arguments`` and ``run``, registered by devtools.py).
"""

import datetime
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import threading
import time
from urllib.parse import urlsplit

ENV = "MOBSTER_ALERT_WEBHOOK_URL"
TIMEOUT = 5.0
ATTEMPTS = 2
RETRY_PAUSE = 1.0
APPROVAL_WAIT_S = 60
# Statuses that need no alert: it worked, or the user ended it themselves.
QUIET = frozenset({"completed", "stopped", "approval_denied", "demo_complete", "queued", "running"})
REASONS = {
    "blocked": "It couldn't finish the task.",
    "max_steps": "It ran out of steps.",
    "timeout": "It ran out of time.",
    "spend_cap": "It reached its spending limit.",
    "approval_timeout": "Nobody answered its approval in time.",
    "approval_waiting": "It is waiting for your approval.",
    "interrupted": "Mobster stopped while it ran.",
    "error": "It hit an error.",
    "failed": "It failed.",
    "test": "This is a test alert from Mobster.",
}
# Why a run stopped before it began, when the phone guard says (agent_hooks.GuardVerdict.code).
CODES = {
    "phone_locked": "Your iPhone was locked.",
    "passcode_failed": "The saved passcode didn't unlock your iPhone.",
    "face_id": "An app asked for Face ID.",
    "app_locked": "The app is locked on your iPhone.",
    "apple_confirmation": "The App Store wanted you to confirm on your iPhone.",
    "unplugged": "Your iPhone was unplugged.",
    "keychain_locked": "Your Mac's Keychain was locked.",
    "unlock_declined": "Unlocking the iPhone was declined.",
    "blocked_app": "The task needed an app you told Mobster never to open.",
}
LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")


class AlertError(ValueError):
    """Why an address can't take alerts, in one plain sentence."""


def validate_url(url):
    """``url`` trimmed when it can take alerts: https, a host name or a public address. AlertError otherwise.
    (Names are resolved and checked again at each send.)"""
    text = str(url or "").strip()
    if not text:
        raise AlertError("Give the webhook's address.")
    if len(text) > 2000 or any(ord(c) < 33 for c in text):
        raise AlertError("That webhook address isn't valid.")
    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        raise AlertError("Alerts go only to https addresses.")
    if parts.username or parts.password:
        raise AlertError("Put the webhook's secret in its path, not as user:password@.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise AlertError("That webhook address has no host.")
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES) or "." not in host and not _literal(host):
        raise AlertError("Alerts never go to this Mac or its local network.")
    address = _literal(host)
    if address is not None and not _public(address):
        raise AlertError("Alerts never go to this Mac or its local network.")
    try:
        parts.port
    except ValueError:
        raise AlertError("That webhook address has a bad port.") from None
    return text


def _literal(host):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _public(address):
    """Reachable on the internet: not private, loopback, link-local, reserved or shared (100.64.0.0/10, where
    Tailscale and carrier NAT live), and not multicast."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not (address.is_private or address.is_loopback or address.is_link_local
                                      or address.is_multicast or address.is_reserved or address.is_unspecified
                                      or getattr(address, "is_site_local", False))


def resolve_public(host, port, resolve=socket.getaddrinfo):
    """The address to connect to: every address ``host`` resolves to must be public; the first is used."""
    try:
        infos = resolve(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise AlertError(f"{host} couldn't be found.") from None
    addresses = [info[4][0] for info in infos]
    if not addresses or not all(_public(ipaddress.ip_address(a.split("%")[0])) for a in addresses):
        raise AlertError("Alerts never go to this Mac or its local network.")
    return addresses[0]


def configured_url(env=None, data_dir=None, read_file=False):
    """The alert address: $MOBSTER_ALERT_WEBHOOK_URL, else (``read_file``: `mobster alerts test`) the Mac app's
    agent.env; None when there is none. The agent itself reads only its environment, which its env file and
    Settings fill."""
    env = os.environ if env is None else env
    value = (env.get(ENV) or "").strip()
    if value:
        return value
    if not read_file:
        return None
    try:
        from .paths import user_data_dir
        path = Path(data_dir) if data_dir else user_data_dir()
        for line in (path / "agent.env").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.strip().removeprefix("export ").partition("=")
            if key.strip() == ENV:
                rest = rest.strip()
                if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "'\"":
                    rest = rest[1:-1]
                return rest or None
    except (OSError, UnicodeDecodeError, ImportError):
        return None
    return None


# -- what is sent ----------------------------------------------------------------------------------------------

def reason_for(status, code=None):
    if code in CODES:
        return CODES[code]
    return REASONS.get(status, "It didn't finish.")


def payload(workflow, status, run_id=None, code=None, at=None):
    """The alert: never the answer, a screenshot or anything the task typed."""
    at = time.time() if at is None else at
    name = " ".join(str(workflow or "Saved task").split())[:120]
    return {"workflow": name, "status": str(status)[:40], "reason": reason_for(status, code),
            "runId": str(run_id)[:40] if run_id else None,
            "at": datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat(timespec="seconds")}


def headline(data):
    if data["status"] == "approval_waiting":
        return f"Mobster: “{data['workflow']}” is waiting for your approval. Open the Mobster app to answer it."
    if data["status"] == "test":
        return "Mobster: alerts work. You'll hear here when a scheduled task fails or waits for you."
    return (f"Mobster: “{data['workflow']}” didn't finish ({data['status']}). {data['reason']} "
            "Open the Mobster app to see it.")


def shape(url, data):
    """(body bytes, headers) for the service ``url`` belongs to."""
    host = (urlsplit(url).hostname or "").lower()
    line = headline(data)
    if host == "hooks.slack.com" or host.endswith(".slack.com"):
        body = {"text": line}
    elif host in ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"):
        body = {"content": line}
    elif host == "ntfy.sh" or host.startswith("ntfy."):
        title = f"Mobster: {data['workflow']}"[:120].encode("ascii", "replace").decode()
        return line.encode("utf-8"), {"Content-Type": "text/plain; charset=utf-8", "Title": title,
                                      "Tags": "iphone,warning"}
    else:
        body = {**data, "text": line}
    return json.dumps(body, ensure_ascii=False).encode("utf-8"), {"Content-Type": "application/json"}


# -- sending ---------------------------------------------------------------------------------------------------

class _Pinned(http.client.HTTPSConnection):
    """HTTPS to an address checked beforehand, with the name kept for TLS (SNI and the certificate check)."""

    def __init__(self, host, address, port, timeout, context):
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _post(host, address, port, path, body, headers, timeout):
    connection = _Pinned(host, address, port, timeout, ssl.create_default_context())
    try:
        connection.request("POST", path, body=body, headers={**headers, "User-Agent": "Mobster-alerts"})
        response = connection.getresponse()
        response.read(2048)
        return response.status
    finally:
        connection.close()


def deliver(url, data, *, post=None, resolve=socket.getaddrinfo, sleep=time.sleep, attempts=ATTEMPTS):
    """Send ``data`` to ``url``; True when the service answered 2xx. Never raises."""
    try:
        url = validate_url(url)
        parts = urlsplit(url)
        host, port = parts.hostname.rstrip("."), parts.port or 443
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        body, headers = shape(url, data)
    except Exception:
        return False
    post = post or _post
    for attempt in range(attempts):
        try:
            address = resolve_public(host, port, resolve)
            status = post(host, address, port, path, body, headers, TIMEOUT)
            if 200 <= int(status) < 300:
                return True
            if 400 <= int(status) < 500 and status != 429:
                return False  # the address refuses it: trying again won't help
        except AlertError:
            return False
        except Exception:
            pass
        if attempt + 1 < attempts:
            sleep(RETRY_PAUSE)
    return False


class Alerts:
    """What workflows.py calls. Reads the address at each alert (the Mac app's Settings change it live), and
    sends on a daemon thread so no run or schedule ever waits on it."""

    def __init__(self, env=None, *, post=None, resolve=socket.getaddrinfo, background=True, clock=time.time):
        self.env, self.post, self.resolve, self.background, self.clock = env, post, resolve, background, clock
        self.sent = []  # (status, run id) of every alert started, for the log and tests

    def url(self):
        value = configured_url(self.env)
        if not value:
            return None
        try:
            return validate_url(value)
        except AlertError:
            return None

    def run_finished(self, workflow, status, run_id, code=None):
        """Alert when a scheduled run ended without finishing. True when an alert was started."""
        if status in QUIET:
            return False
        return self._send(payload(workflow, status, run_id, code, self.clock()))

    def approval_waiting(self, workflow, run_id):
        return self._send(payload(workflow, "approval_waiting", run_id, None, self.clock()))

    def _send(self, data):
        url = self.url()
        if not url:
            return False
        self.sent.append((data["status"], data["runId"]))

        def work():
            deliver(url, data, post=self.post, resolve=self.resolve)
        if self.background:
            threading.Thread(target=work, daemon=True, name="mobster-alert").start()
        else:
            work()
        return True


# -- `mobster alerts` ------------------------------------------------------------------------------------------

def add_arguments(parser, helpers):
    subs = parser.add_subparsers(dest="alerts_command", metavar="<subcommand>")
    subs.required = True
    test = subs.add_parser("test", help="send a test alert to your webhook",
                           description="Send a test alert to MOBSTER_ALERT_WEBHOOK_URL (or --url) and say whether "
                                       "the service took it.",
                           epilog="exit codes: 0 sent, 1 the service didn't take it, 2 no usable address")
    test.add_argument("--url", metavar="URL", help="send to this https address instead of the saved one")
    test.add_argument("--json", action="store_true", help="print one JSON object")


def run(args, *, post=None, resolve=socket.getaddrinfo):
    as_json = bool(getattr(args, "json", False))
    url = getattr(args, "url", None) or configured_url(read_file=True)

    def say(ok, message, code):
        try:
            if as_json:
                print(json.dumps({"ok": ok, "message": message}), flush=True)
            else:
                print(("" if ok else "mobster alerts: ") + message, file=sys.stdout if ok else sys.stderr, flush=True)
        except BrokenPipeError:
            pass
        return code
    if not url:
        return say(False, f"No alert address. Set {ENV} (Settings in the Mobster app), or pass --url.", 2)
    try:
        url = validate_url(url)
    except AlertError as error:
        return say(False, str(error), 2)
    if deliver(url, payload("Test", "test"), post=post, resolve=resolve):
        return say(True, f"Sent a test alert to {urlsplit(url).hostname}.", 0)
    return say(False, f"{urlsplit(url).hostname} didn't take the test alert. Check the address, then try again.", 1)
