"""The kinds of destination, and what each one takes and does with a result."""

import json
import re
from urllib.parse import urlsplit

from . import net, webhook
from .webhook import Outcome

FIELDS = ("name", "kind", "events", "scope")
# The kinds that take events besides a result (run.started, needs_you): the ones that post a message or JSON. A row in
# a sheet, a page, a file line or an email per result stays a result.
EVENT_KINDS = frozenset({"webhook", "slack", "discord"})
EVENTS = ("run.finished", "run.started", "needs_you")
SCOPES = ("named", "all")      # named: runs that choose it (sendTo); all: every run


def subscription(body, kind_id, current=None):
    """{"events", "scope"} from a body's ``events`` and ``scope`` (each optional), over ``current``'s."""
    current = current or {}
    events = body.get("events", current.get("events") or ["run.finished"])
    scope = body.get("scope", current.get("scope") or "named")
    if not isinstance(events, list) or not events or not all(isinstance(e, str) and e in EVENTS for e in events):
        raise ValueError("events is a list of run.finished, run.started and needs_you, at least one")
    events = [e for e in EVENTS if e in events]
    if kind_id not in EVENT_KINDS and events != ["run.finished"]:
        raise ValueError("This kind of destination takes results only. Use a webhook, Slack or Discord for the other "
                         "events")
    if scope not in SCOPES:
        raise ValueError("scope is named (tasks that choose it) or all (every task)")
    return {"events": events, "scope": scope}


class Kind:
    id = "webhook"
    label = "Webhook"
    fields = ()                 # what the add and update bodies may hold besides name and kind
    example = "My server"       # a name for one, in the empty-name error (the app's kindCopy names the same)
    replay_safe = True          # False: marked in flight before each try, so a crash mid-try never sends it twice
    keychain = True             # keeps an item in the Keychain (vault.py)
    provider = None             # the license API's sign-in Connect uses (connect.py), or None: it doesn't connect
    keep = ()                   # fields of that sign-in's token answer ``connected`` reads (Discord's ``webhook``)

    def setup(self, body, current=None):
        """{"hint", "config", "has_secret", "screenshot", "vault", "last_test"}, the parts that change."""
        raise NotImplementedError

    def send(self, ctx, body):
        raise NotImplementedError

    def test(self, ctx):
        raise NotImplementedError

    def connected(self, call, tokens):
        """What a finished sign-in found: {"credentials", "account", "targets"} (connect.py)."""
        raise NotImplementedError


def connection(body, kind):
    """What a Connect found (connect.py), as the service put it in ``body["connect"]``; ValueError without one."""
    found = body.get("connect")
    if not isinstance(found, dict):
        raise ValueError(f"Connect {kind.label} first.")
    return found


def target(found, value, what):
    """The one of a Connect's ``targets`` that ``value`` names: {"id", "name"}. ValueError when none does."""
    for item in found.get("targets") or ():
        if item["id"] == value:
            return item
    raise ValueError(f"Pick {what} from the list.")


def again(label):
    return f"Connect {label} again in Settings › Connectors."


def boolean(body, key, default=False):
    value = body.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"{key} is true or false")
    return value


COLUMNS = ("Finished", "Status", "Answer", "Reason", "Workflow", "Data", "Run")
EXTRA_COLUMNS = 30
RISKY = ("=", "+", "-", "@", "\t", "\r")
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def cell(value):
    """A spreadsheet-safe cell: text that a spreadsheet would read as a formula (=, +, -, @) starts with an apostrophe,
    so text from a screen can never run as one."""
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    if value.startswith(RISKY) and not NUMBER.fullmatch(value):
        return "'" + value
    return value


def flat(body):
    """The result as one row by column name: COLUMNS, then each blank of its workflow, then each top-level field of its
    data that is text, a number or true/false (at most EXTRA_COLUMNS)."""
    flow = body.get("workflow") or {}
    data = body.get("data")
    row = {"Finished": body.get("finishedAt") or "", "Status": body.get("status") or "",
           "Answer": body.get("answer") or "", "Reason": body.get("reason") or "", "Workflow": flow.get("name") or "",
           "Data": json.dumps(data, ensure_ascii=False) if data is not None else "", "Run": body.get("runId") or ""}
    extra = list((flow.get("blanks") or {}).items())
    if isinstance(data, dict):
        extra += [(k, v) for k, v in data.items() if isinstance(v, (str, int, float, bool)) or v is None]
    known = {name.casefold() for name in row}
    for key, value in extra[:EXTRA_COLUMNS]:
        name = " ".join(str(key).split())[:60]
        if name and name.casefold() not in known:
            known.add(name.casefold())
            row[name] = value if value is not None else ""
    return row


def title_of(body, limit=200):
    """One line for a title or a subject: the workflow's name, then the answer, or how it ended."""
    flow = (body.get("workflow") or {}).get("name")
    text = body.get("text") or ""
    line = text.split(": ", 1)[1] if ": " in text else text
    line = f"{flow}: {line}" if flow else line
    return line if len(line) <= limit else line[:limit - 1] + "…"


def careful(attempt, where):
    """One try at a destination that can't take the same result twice: ``attempt()`` -> Reply."""
    try:
        return attempt(), None
    except net.NetError as error:
        if error.unsent:
            return None, Outcome(False, error.retry, None, error.message, unsent=True)
        return None, Outcome(False, False, None, f"{error.message} It may have arrived: check {where}, then resend "
                                                 "if it's missing.")


PRESETS = {
    "zapier": (re.compile(r"hooks\.zapier\.com"),
               "Use the address of Zapier's Catch Hook trigger. It starts with https://hooks.zapier.com/hooks/catch/."),
    "make": (re.compile(r"hook\.(?:[a-z0-9-]+\.)?make\.com|hook\.(?:[a-z0-9-]+\.)?integromat\.com"),
             "Use the address of Make's Custom webhook module. It starts with https://hook. and ends in make.com/…."),
    "n8n": (None, None),          # any host: n8n runs in the cloud or on your own server
}


def preset_url(preset, url):
    """``url`` when it is where ``preset``'s webhooks live; AddressError otherwise."""
    pattern, words = PRESETS[preset]
    if pattern is not None:
        parts = urlsplit(url)
        if parts.scheme != "https" or not pattern.fullmatch((parts.hostname or "").lower()):
            raise webhook.AddressError(words)
    return url


class Webhook(Kind):
    id, label = "webhook", "Webhook"
    fields = ("url", "secret", "screenshot", "preset")

    def setup(self, body, current=None):
        if current is None:
            preset = body.get("preset")
            if preset is not None and preset not in PRESETS:
                raise ValueError("A webhook's preset is zapier, make or n8n")
            url = webhook.validate_url(body.get("url"))
            if preset:
                preset_url(preset, url)
            secret = webhook.validate_secret(body.get("secret"))
            return {"hint": webhook.hint(url), "has_secret": secret is not None,
                    "config": {"preset": preset} if preset else {},
                    "screenshot": boolean(body, "screenshot"), "vault": {"url": url, "secret": secret}}
        if "preset" in body and body["preset"] != current["row"].get("config", {}).get("preset"):
            raise ValueError("A webhook keeps its preset. Add a new one instead.")
        changes = {}
        if "screenshot" in body:
            changes["screenshot"] = boolean(body, "screenshot")
        if "url" in body or "secret" in body:
            url = webhook.validate_url(body["url"]) if "url" in body else None
            preset = current["row"].get("config", {}).get("preset")
            if url and preset in PRESETS:
                preset_url(preset, url)
            secret = webhook.validate_secret(body["secret"]) if "secret" in body else None
            saved = current["saved"]()
            if saved is None and url is None:
                raise ValueError("Paste the address again: Mobster no longer has it.")
            url = url or saved["url"]
            secret = secret if "secret" in body else (saved or {}).get("secret")
            changes.update(vault={"url": url, "secret": secret}, hint=webhook.hint(url), has_secret=secret is not None,
                           last_test=None)
        return changes

    def send(self, ctx, body):
        data = webhook.encode_body(ctx.saved["url"], body)
        return ctx.post(ctx.saved["url"], data, webhook.headers(ctx.event, ctx.delivery_id, data, ctx.saved["secret"],
                                                                 ctx.now))

    def test(self, ctx):
        return self.send(ctx, webhook.test_payload(ctx.now))


DISCORD_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")
DISCORD_MAX = 2000
SNOWFLAKE = re.compile(r"\d{15,25}")


def discord_url(value):
    """The webhook address Discord answered, checked: https, one of Discord's hosts, under /api/webhooks/."""
    refused = ValueError("Discord answered with an address Mobster doesn't post to. " + again("Discord"))
    try:
        text = webhook.validate_url(value)
    except webhook.AddressError:
        raise refused from None
    parts = urlsplit(text)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in DISCORD_HOSTS \
            or not parts.path.startswith("/api/webhooks/"):
        raise refused
    return text


class Discord(Kind):
    """Connect asks Discord for ``webhook.incoming``: its consent page picks the server and the channel, and its token
    answer holds the webhook it made there."""
    id, label = "discord", "Discord"
    example = "Team channel"
    fields = ("connect",)
    replay_safe = False
    provider, keep = "discord", ("webhook",)

    def connected(self, call, tokens):
        """Mobster keeps the webhook's address (Keychain) and the channel's ids, never the access token."""
        hook = (tokens.get("kept") or {}).get("webhook")
        if not isinstance(hook, dict):
            raise ValueError("Discord didn't say which channel to post in. " + again("Discord"))
        url = discord_url(hook.get("url"))
        server, channel = hook.get("guild_id"), hook.get("channel_id")
        if not all(isinstance(value, str) and SNOWFLAKE.fullmatch(value) for value in (server, channel)):
            raise ValueError("Discord didn't say which channel to post in. " + again("Discord"))
        return {"credentials": {"url": url, "server": server, "channel": channel}, "account": None, "targets": None}

    def setup(self, body, current=None):
        if current is not None and "connect" not in body:
            return {}
        found = connection(body, self)["credentials"]
        return {"hint": "A Discord channel",
                "config": {"server": found["server"], "channel": found["channel"]},
                "vault": {"url": found["url"], "secret": None}, "last_test": None}

    @staticmethod
    def message(text):
        """Discord's body: the line as plain content, with every mention turned off, so text from a screen can't ping
        @everyone."""
        text = text if len(text) <= DISCORD_MAX else text[:DISCORD_MAX - 1] + "…"
        return json.dumps({"content": text, "username": "Mobster", "allowed_mentions": {"parse": []}},
                          ensure_ascii=False).encode()

    def _post(self, ctx, text):
        url = ctx.saved["url"]
        reply, failed = careful(lambda: ctx.call("POST", url, self.message(text),
                                                 {"Content-Type": "application/json",
                                                  "User-Agent": webhook.USER_AGENT}), "the channel")
        if failed:
            return failed
        status = reply.status
        if 200 <= status < 300:
            return Outcome(True, False, status)
        if status == 429:
            return Outcome(False, True, status, "Discord asked Mobster to wait.", unsent=True)
        if status in (401, 403, 404):
            return Outcome(False, False, status, f"Discord answered {status}: the webhook was deleted. "
                           + again("Discord"))
        if status >= 500:
            return Outcome(False, False, status, f"Discord answered {status}. Check the channel, then resend if the "
                                                 "message is missing.")
        return Outcome(False, False, status, f"Discord answered {status}. " + again("Discord"))

    def send(self, ctx, body):
        return self._post(ctx, body.get("text") or "Mobster")

    def test(self, ctx):
        return self._post(ctx, "Mobster: this is a test. Results from your tasks will arrive here.")


def all_kinds():
    from . import folder, mail, notion, sheets, slack
    return {kind.id: kind for kind in (Webhook(), slack.Slack(), Discord(), sheets.Sheets(), notion.Notion(),
                                       folder.Folder(), mail.Email())}


_KINDS = None


def get(kind_id):
    global _KINDS
    if _KINDS is None:
        _KINDS = all_kinds()
    found = _KINDS.get(kind_id or "webhook")
    if found is None:
        raise ValueError("A destination's kind is " + ", ".join(_KINDS))
    return found
