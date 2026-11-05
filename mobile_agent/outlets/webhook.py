"""A webhook destination's address, the JSON a run sends it, the signature, and one POST."""

import base64
import datetime
import hashlib
import hmac
import io
import ipaddress
import json
import time
from urllib.parse import urlsplit

from .. import secret_filter
from . import net

TIMEOUT = 10.0
URL_MAX = 2000
SECRET_MAX = 200
NAME_MAX = 60
ANSWER_MAX = 20_000
TEXT_MAX = 3000
SCREENSHOT_SIDE = 1280
USER_AGENT = "Mobster-Webhooks/1"
# The run statuses that finished the task: no reason goes with them.
DONE = frozenset({"completed", "completed_unverified", "user_condition_met"})
LOCAL_SUFFIXES = (".local", ".localhost", ".lan", ".home.arpa", ".internal")


class AddressError(ValueError):
    """Why an address can't take results, in one sentence that never repeats the address."""


def _literal(host):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _private(host):
    """This Mac or a private network: localhost, a name with no dot or a .local one, or a loopback, private or
    link-local address."""
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES) or ("." not in host and ":" not in host):
        return True
    address = _literal(host)
    if address is None:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_private or address.is_link_local


def validate_url(value):
    """``value`` trimmed, when results may go there; AddressError otherwise."""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise AddressError("Paste the webhook's address.")
    if len(text) > URL_MAX or any(ord(c) < 33 or ord(c) == 127 for c in text):
        raise AddressError("That address isn't a web address.")
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise AddressError("Use an address that starts with https://.")
    if parts.username or parts.password:
        raise AddressError("Put the secret in Secret, not in the address.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise AddressError("That address has no host.")
    try:
        parts.port
    except ValueError:
        raise AddressError("That address has a port Mobster can't use.") from None
    if scheme == "http" and not _private(host):
        raise AddressError("Use https:// for an address on the internet. http:// works for this Mac and your own "
                           "network.")
    return text


def hint(url):
    """What the app shows of an address: its scheme, host and first path segment ("https://hooks.zapier.com/hooks/…").
    The rest often carries the address's own key."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host += f":{parts.port}"
    segments = [s for s in parts.path.split("/") if s]
    shown = f"{parts.scheme.lower()}://{host}"
    if segments:
        first = segments[0] if len(segments[0]) <= 24 else segments[0][:24] + "…"
        shown += "/" + first + ("/…" if len(segments) > 1 or parts.query else "")
    elif parts.query:
        shown += "/…"
    return shown


host_of = net.host_of


def validate_secret(value):
    if value is None or value == "":
        return None
    if (not isinstance(value, str) or not 8 <= len(value) <= SECRET_MAX
            or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        raise ValueError("A secret is 8 to 200 characters with no spaces.")
    return value


def validate_name(value, example="My server"):
    """The destination's name, spaces folded. ``example``: a name for its kind, for the error (kinds.Kind.example)."""
    text = " ".join(value.split()) if isinstance(value, str) else ""
    if not 1 <= len(text) <= NAME_MAX:
        raise ValueError(f"Name the destination in 1 to {NAME_MAX} characters, like {example}.")
    return text


def iso(seconds):
    if not isinstance(seconds, (int, float)):
        return None
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def masked(value, depth=0):
    """``value`` with secret_filter.redact over every string inside it (keys too). Depth past 20 becomes None."""
    if depth > 20:
        return None
    if isinstance(value, str):
        return secret_filter.redact(value)
    if isinstance(value, dict):
        return {secret_filter.redact(str(k)): masked(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [masked(v, depth + 1) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return secret_filter.redact(str(value))


def answer_of(summary):
    """The run's whole answer as text: the answer it wrote in full, else its text data, else its headline."""
    for key in ("full_answer", "data", "answer"):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:ANSWER_MAX]
    return None


def data_of(summary):
    """The structured data: an output schema's (or a format's) data once it was validated; None for a text answer."""
    data = summary.get("data")
    if summary.get("data_status") == "validated" and data is not None and not isinstance(data, str):
        return data
    return None


def line_of(title, status, answer, reason):
    """``text``: the answer; without one, Done or why it couldn't finish."""
    if answer:
        said = answer
    elif status in DONE:
        said = "Done."
    else:
        said = f"Couldn't finish. {reason}" if reason else "Couldn't finish."
    text = f"{title}: {' '.join(said.split())}"
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 1] + "…"


def payload(run, workflow=None, screenshot=None):
    """The body for ``run``: an object as the module docstring shows."""
    summary = run.summary if isinstance(getattr(run, "summary", None), dict) else {}
    status = getattr(run, "status", None) or summary.get("status") or "error"
    answer = masked(answer_of(summary))
    reason = summary.get("reason") if status not in DONE else None
    reason = masked(" ".join(reason.split())) if isinstance(reason, str) and reason.strip() else None
    flow = None
    if isinstance(workflow, dict) and workflow.get("name"):
        blanks = workflow.get("blanks") if isinstance(workflow.get("blanks"), dict) else {}
        flow = {"id": workflow.get("id"), "name": masked(workflow["name"]), "blanks": masked(blanks)}
    body = {"type": "run.finished", "runId": run.id, "status": status, "answer": answer,
            "data": masked(data_of(summary)), "reason": reason, "startedAt": iso(getattr(run, "created_at", None)),
            "finishedAt": iso(getattr(run, "finished_at", None)), "workflow": flow,
            "text": line_of(f"Mobster › {flow['name']}" if flow else "Mobster", status, answer, reason)}
    if screenshot:
        body["screenshot"] = {"contentType": "image/jpeg", "base64": base64.b64encode(screenshot).decode()}
    return body


def started_payload(run, workflow=None):
    """run.started: which run, when, and its workflow when it came from one. No task words."""
    flow = _flow(workflow)
    title = f"Mobster › {flow['name']}" if flow else "Mobster"
    return {"type": "run.started", "runId": run.id, "startedAt": iso(getattr(run, "created_at", None)),
            "workflow": flow, "text": f"{title}: a task started."}


# approval_requested kinds (server.request_approval) and what a needs_you event calls them.
NEED_KINDS = {"action": "approval", "commit": "approval", "proposals": "approval", "clarify": "question",
              "loop": "question", "question": "question", "plan": "plan"}
QUESTION_MAX = 300


def needs_payload(run, event, workflow=None, now=None):
    """needs_you: the run is waiting for an answer in the app. ``event`` is its approval_requested event."""
    now = time.time() if now is None else now
    flow = _flow(workflow)
    # What the card asks, masked: never the message, a typed answer or anything the card doesn't say.
    asked = " ".join(str(event.get("title") or event.get("label") or "").split())[:QUESTION_MAX]
    question = secret_filter.redact(asked) or "Mobster needs your answer in the app."
    title = f"Mobster › {flow['name']}" if flow else "Mobster"
    return {"type": "needs_you", "runId": run.id, "kind": NEED_KINDS.get(event.get("kind") or "action", "approval"),
            "question": question, "askedAt": iso(now), "workflow": flow,
            "text": f"{title} needs you: {question}"[:TEXT_MAX]}


def _flow(workflow):
    if isinstance(workflow, dict) and workflow.get("name"):
        blanks = workflow.get("blanks") if isinstance(workflow.get("blanks"), dict) else {}
        return {"id": workflow.get("id"), "name": masked(workflow["name"]), "blanks": masked(blanks)}
    return None


def test_payload(now=None):
    now = time.time() if now is None else now
    return {"type": "test", "runId": None, "status": "completed",
            "answer": "This is a test from Mobster. A finished task's answer goes here.", "data": None, "reason": None,
            "startedAt": iso(now), "finishedAt": iso(now), "workflow": None,
            "text": "Mobster: this is a test. Results from your tasks will arrive here."}


def slack_text(text):
    """Slack's escaping for its text field: &, < and > only."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def encode_body(url, body):
    """The bytes to send. Slack's incoming webhooks read ``text`` as their own markup, so it's escaped for them."""
    host = host_of(url)
    if host == "hooks.slack.com" or host.endswith(".slack.com"):
        body = {**body, "text": slack_text(body.get("text") or "")}
    return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def signature(secret, timestamp, data):
    """v1: HMAC-SHA256 with ``secret`` over ``<timestamp>.<body>``, in hex."""
    message = str(int(timestamp)).encode() + b"." + data
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def headers(event, delivery_id, data, secret=None, timestamp=None):
    timestamp = int(time.time() if timestamp is None else timestamp)
    out = {"Content-Type": "application/json", "User-Agent": USER_AGENT, "Mobster-Event": event,
           "Mobster-Delivery": delivery_id, "Mobster-Timestamp": str(timestamp)}
    if secret:
        out["Mobster-Signature"] = f"t={timestamp},v1={signature(secret, timestamp, data)}"
    return out


def screenshot_of(run, frames=None):
    """The run's last screen as JPEG bytes (at most SCREENSHOT_SIDE px), or None: the final full screenshot while the
    app still has it, else the last step's saved frame."""
    image = getattr(run, "image", None)
    if isinstance(image, str) and image.startswith("data:image/") and "," in image:
        try:
            raw = base64.b64decode(image.split(",", 1)[1])
            from PIL import Image
            picture = Image.open(io.BytesIO(raw)).convert("RGB")
            picture.thumbnail((SCREENSHOT_SIDE, SCREENSHOT_SIDE))
            out = io.BytesIO()
            picture.save(out, "JPEG", quality=80)
            return out.getvalue()
        except Exception:  # noqa: BLE001 -- the step frame below, or none
            pass
    if frames is None:
        return None
    for event in reversed(list(getattr(run, "events", None) or ())):
        frame = event.get("frameId") if isinstance(event, dict) else None
        if isinstance(frame, str):
            try:
                data = frames.get(run.id, frame)
            except Exception:  # noqa: BLE001
                data = None
            if data:
                return data
    return None


class Outcome:
    """What one try came to: ``ok``, ``retry`` (worth another try), the HTTP ``status`` and a ``message``. ``unsent``:
    the try surely never reached the destination, so another can't make a duplicate."""

    def __init__(self, ok, retry, status=None, message=None, unsent=False):
        self.ok, self.retry, self.status, self.message, self.unsent = ok, retry, status, message, unsent

    def __repr__(self):
        return f"Outcome(ok={self.ok}, retry={self.retry}, status={self.status}, message={self.message!r})"


def post(url, data, request_headers, timeout=TIMEOUT, connect=net.connection):
    """POST ``data`` to ``url`` once. Never raises; never names the address beyond its host."""
    try:
        reply = net.call("POST", url, data, request_headers, timeout, connect, read_limit=4096)
    except net.NetError as error:
        return Outcome(False, error.retry, None, error.message, unsent=error.unsent)
    return judge(reply.status)


def judge(status):
    if 200 <= status < 300:
        return Outcome(True, False, status, None)
    if 300 <= status < 400:
        return Outcome(False, False, status, f"The address answered {status}, a redirect. Use the address it points "
                                             "to.")
    if status in (408, 425, 429) or status >= 500:
        return Outcome(False, True, status, f"The address answered {status}.")
    return Outcome(False, False, status, f"The address answered {status}. Check it in Settings › Connectors.")
