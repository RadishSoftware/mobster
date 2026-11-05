"""A Slack destination: each result is a message in one channel you picked, posted as you."""

import json
import re
from urllib.parse import urlencode

from ..api_errors import APIError
from . import webhook
from .kinds import Kind, again, careful, connection, target
from .webhook import USER_AGENT, Outcome

API = "https://slack.com/api"
LABEL = "Slack"
CHANNEL = re.compile(r"[CG][A-Z0-9]{2,30}")
PAGES = 5                     # users.conversations pages of 200 read at Connect
SAFE_LINK = re.compile(r"https://[A-Za-z0-9._~:/?#\[\]@!$'()*+,;=%&-]{1,500}")
SECTION = 3000                # Block Kit's limit for a section's text
STATUS = {"run.started": "Started", "needs_you": "Needs you in Mobster", "test": "Test"}
RENEW = ("invalid_auth", "token_expired", "not_authed")
SIGNED_OUT = ("token_revoked", "account_inactive", "user_removed_from_team", "team_disabled")
NOT_HERE = {"channel_not_found": "the channel is gone", "is_archived": "the channel was archived",
            "not_in_channel": "you're no longer in the channel", "restricted_action": "a workspace admin turned "
            "posting there off", "cant_post": "you can't post in that channel"}
TEST = "Mobster: this is a test. Results from your tasks will arrive here."


def headers(token, body=False):
    out = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
    if body:
        out["Content-Type"] = "application/json; charset=utf-8"
    return out


def refusal(error):
    """Slack's ``error`` as a sentence: what happened, then what to do."""
    if error in RENEW or error in SIGNED_OUT:
        return "Slack signed Mobster out. Sign in to Slack again under Use in tasks in Settings › Connectors."
    if error == "missing_scope":
        return ("Your Slack sign-in can't do this: it needs chat:write, channels:read and groups:read. Sign in to "
                "Slack again under Use in tasks.")
    if error in NOT_HERE:
        return f"Slack refused: {NOT_HERE[error]}. Pick another channel: " + again(LABEL)
    return f"Slack refused ({str(error)[:60]})."


def read(call, method, token, query=None):
    """One Web API read: Slack's answer when ``ok``. NetError, or ValueError with Slack's refusal."""
    reply = call("GET", f"{API}/{method}" + (f"?{urlencode(query)}" if query else ""), None, headers(token))
    answer = reply.json_object()
    if reply.status != 200 or answer is None:
        raise ValueError(f"Slack answered {reply.status}. Try again.")
    if answer.get("ok") is not True:
        raise ValueError(refusal(answer.get("error")))
    return answer


def channels(call, token):
    """The channels you're in, by name: [{"id", "name"}]."""
    found, cursor = [], None
    for _ in range(PAGES):
        query = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": "200"}
        if cursor:
            query["cursor"] = cursor
        answer = read(call, "users.conversations", token, query)
        for item in answer.get("channels") or ():
            if isinstance(item, dict) and isinstance(item.get("id"), str) and CHANNEL.fullmatch(item["id"]) \
                    and isinstance(item.get("name"), str):
                found.append({"id": item["id"], "name": "#" + item["name"][:80]})
        cursor = (answer.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return sorted(found, key=lambda item: item["name"])


class Slack(Kind):
    id, label = "slack", LABEL
    example = "Team channel"
    fields = ("connect", "target")
    replay_safe = False
    keychain = False

    def connected(self, call, tokens):
        token = tokens["access_token"]
        team = read(call, "auth.test", token).get("team")
        return {"credentials": {"connector": tokens["connector"]},
                "account": team[:100] if isinstance(team, str) and team.strip() else None,
                "targets": channels(call, token)}

    def setup(self, body, current=None):
        if current is not None and not ({"connect", "target"} & set(body)):
            return {}
        found = connection(body, self)
        channel = target(found, body.get("target"), "a channel")
        config = {"connector": found["credentials"]["connector"], "channel": channel["id"],
                  "channelName": channel["name"]}
        if found.get("account"):
            config["workspace"] = found["account"]
        return {"hint": f"Slack › {channel['name']}", "config": config, "last_test": None}

    @staticmethod
    def suite_message(body):
        """A suite's message (testkit/notify.py): the title bold, one section of its lines, and a context line."""
        report = body["report"]
        title = webhook.slack_text(" ".join(str(report["title"]).split()))[:SECTION]
        link = report.get("link")
        safe = isinstance(link, str) and SAFE_LINK.fullmatch(link)
        lines = [f"<{link.replace('&', '&amp;')}|Report>" if safe and line == f"Report: {link}"
                 else webhook.slack_text(" ".join(str(line).split())) for line in report["lines"]]
        text = "\n".join(lines)
        budget = SECTION - len(title) - 3  # bold title and newline share this section
        text = text if len(text) <= budget else text[:budget - 1] + "…"
        return {"text": title, "unfurl_links": False, "unfurl_media": False, "link_names": False,
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n{text}"}},
                           {"type": "context", "elements": [{"type": "mrkdwn", "text": "Checks run by Mobster"}]}]}

    @staticmethod
    def message(body):
        """Block Kit: the line as one section, and how the task ended as a quiet context line. &, < and > are escaped:
        text from a screen can't mention @channel or a person, or make a link."""
        text = webhook.slack_text(" ".join(str(body.get("text") or "Mobster").split()))
        text = text if len(text) <= SECTION else text[:SECTION - 1] + "…"
        kind = body.get("type")
        if kind == "tests.finished":
            return Slack.suite_message(body)
        status = STATUS.get(kind) or ("Done" if body.get("status") in webhook.DONE else "Couldn't finish")
        flow = (body.get("workflow") or {}).get("name")
        context = webhook.slack_text(f"{status} in Mobster" + (f", from {flow}" if flow else ""))
        return {"text": text, "unfurl_links": False, "unfurl_media": False, "link_names": False,
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}},
                           {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]}]}

    def _post(self, ctx, message):
        config = ctx.destination["config"]
        if not config.get("channel") or not config.get("connector"):
            # A Slack destination saved before 15 Oct 2026 posted through an incoming webhook.
            return Outcome(False, False, None, "Mobster now posts as you, in a channel you pick. " + again(LABEL),
                           unsent=True)
        connectors = ctx.connectors()
        if connectors is None:
            return Outcome(False, True, None, "Mobster's connectors aren't running. Restart Mobster, then resend.",
                           unsent=True)
        from ..connectors import client as mcp
        data = json.dumps({**message, "channel": config["channel"]}, ensure_ascii=False).encode()
        for renew in (False, True):
            try:
                token = connectors.bearer(config["connector"], refresh=renew)
            except APIError:
                return Outcome(False, False, None, "Slack was disconnected from Mobster. Connect Slack under Use in "
                               "tasks, then connect this destination again.", unsent=True)
            except mcp.AuthNeeded:
                return Outcome(False, False, None, refusal("token_revoked"), unsent=True)
            except mcp.NotSent as error:
                return Outcome(False, True, None, error.message, unsent=True)
            reply, failed = careful(lambda: ctx.call("POST", f"{API}/chat.postMessage", data,
                                                     headers(token, body=True)), "the channel")
            if failed:
                return failed
            if reply.status == 429:
                return Outcome(False, True, 429, "Slack asked Mobster to wait.", unsent=True)
            if reply.status >= 500:
                return Outcome(False, False, reply.status, f"Slack answered {reply.status}. Check the channel, then "
                                                           "resend if the message is missing.")
            answer = reply.json_object() or {}
            if reply.status == 200 and answer.get("ok") is True:
                return Outcome(True, False, 200)
            error = answer.get("error")
            if error in RENEW and not renew:
                continue
            # Slack answered ok: false, so it posted nothing.
            return Outcome(False, error == "ratelimited", reply.status, refusal(error), unsent=True)
        return Outcome(False, False, 401, refusal("invalid_auth"), unsent=True)

    def send(self, ctx, body):
        return self._post(ctx, self.message(body))

    def test(self, ctx):
        return self._post(ctx, self.message({"type": "test", "text": TEST}))
