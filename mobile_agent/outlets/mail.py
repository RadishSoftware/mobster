"""An email destination: each result is an email from the Mail app on this Mac to the one address saved with the
destination."""

import json
import re
import subprocess

from .kinds import Kind, title_of
from .webhook import Outcome

OSASCRIPT = "/usr/bin/osascript"
TIMEOUT = 60
LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,62}[A-Za-z0-9])?"
ADDRESS = re.compile(r"[^@\s<>\"',;]{1,64}@" + LABEL + r"(?:\." + LABEL + ")+")
BODY_MAX = 50_000

# Reads {"to", "subject", "content"} from stdin and sends it from Mail's default account. Prints "sent", or
# "no_account" when Mail has none.
SCRIPT = """
ObjC.import('Foundation');
function run() {
  const input = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
  const message = JSON.parse($.NSString.alloc.initWithDataEncoding(input, $.NSUTF8StringEncoding).js);
  const Mail = Application('Mail');
  if (Mail.accounts.length === 0) return 'no_account';
  const outgoing = Mail.OutgoingMessage({ subject: message.subject, content: message.content, visible: false });
  Mail.outgoingMessages.push(outgoing);
  outgoing.toRecipients.push(Mail.Recipient({ address: message.to }));
  outgoing.send();
  return 'sent';
}
"""


def run(argv, data):
    """One osascript (tests replace it): (returncode, stdout, stderr)."""
    done = subprocess.run(argv, input=data, capture_output=True, timeout=TIMEOUT)
    return done.returncode, done.stdout.decode(errors="replace"), done.stderr.decode(errors="replace")


def address(value):
    text = value.strip() if isinstance(value, str) else ""
    if len(text) > 254 or not ADDRESS.fullmatch(text):
        raise ValueError("Type your email address, like you@example.com.")
    return text


def content(body):
    """The email's text: the answer, why it stopped when it did, the workflow's blanks and the data."""
    parts = [body.get("answer") or ("Done." if not body.get("reason") else "The task couldn't finish.")]
    if body.get("reason"):
        parts.append(f"Why it stopped: {body['reason']}")
    flow = body.get("workflow") or {}
    if flow.get("blanks"):
        parts.append("\n".join(f"{name}: {value}" for name, value in flow["blanks"].items()))
    if body.get("data") is not None:
        parts.append(json.dumps(body["data"], ensure_ascii=False, indent=2))
    facts = [f"Status: {body.get('status')}"]
    if body.get("finishedAt"):
        facts.append(f"Finished: {body['finishedAt']}")
    if body.get("runId"):
        facts.append(f"Task: {body['runId']}")
    parts.append("\n".join(facts))
    parts.append("Sent by Mobster from this Mac. Change where results go in Settings › Connectors.")
    text = "\n\n".join(parts)
    return text if len(text) <= BODY_MAX else text[:BODY_MAX - 1] + "…"


class Email(Kind):
    id, label = "email", "Email"
    example = "My email"
    fields = ("to",)
    replay_safe = False

    def setup(self, body, current=None):
        if current is not None and "to" not in body:
            return {}
        to = address(body.get("to"))
        return {"hint": to, "vault": {"url": None, "secret": None, "to": to}, "last_test": None}

    def _send(self, to, subject, text):
        # The address, subject and body go to osascript on stdin as JSON, never in argv.
        data = json.dumps({"to": to, "subject": subject, "content": text}, ensure_ascii=False).encode("utf-8")
        try:
            code, out, err = run([OSASCRIPT, "-l", "JavaScript", "-e", SCRIPT], data)
        except subprocess.TimeoutExpired:
            return Outcome(False, False, None, "Mail didn't answer within a minute. Check Mail's Outbox, then resend "
                                               "if it isn't there.")
        except OSError:
            return Outcome(False, False, None, "Mobster couldn't start osascript to reach Mail.")
        said = out.strip()
        if code == 0 and said == "sent":
            return Outcome(True, False, None)
        if code == 0 and said == "no_account":
            return Outcome(False, False, None, "Mail has no account to send from. Add one in Mail, then resend.",
                           unsent=True)
        if "-1743" in err or "Not authorized" in err or "not allowed" in err.lower():
            return Outcome(False, False, None, "macOS didn't let Mobster use Mail. Allow it in System Settings › "
                                               "Privacy & Security › Automation › Mobster, then resend.", unsent=True)
        if "-600" in err or "-609" in err or "isn't running" in err:
            return Outcome(False, False, None, "Mail couldn't be opened. Open Mail once, then resend.", unsent=True)
        return Outcome(False, False, None, "Mail didn't send it. Check Mail's Outbox, then resend if it isn't there.")

    def send(self, ctx, body):
        return self._send(ctx.saved["to"], "Mobster: " + title_of(body, 120), content(body))

    def test(self, ctx):
        return self._send(ctx.saved["to"], "Mobster: a test",
                          "This is a test from Mobster. When a task that sends here ends, its result arrives like "
                          "this.\n\nSent by Mobster from this Mac. Change where results go in Settings › Connectors.")
