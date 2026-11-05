"""The Slack connector's tools, answered by Mobster with Slack's Web API and your sign-in (a user token), in place of
Slack's MCP server, which takes only apps listed in the Slack Marketplace and a workspace's own internal apps."""

import json
import re
import time
from urllib.parse import urlencode

from ..outlets import net, webhook
from . import client as mcp
from .api_session import ApiSession, Refusal, count_schema, required_text, short, tool_spec

API = "https://slack.com/api"
NAME = "Slack"
SEARCH_LIMIT, HISTORY_LIMIT, THREAD_LIMIT = 20, 50, 100
CHANNEL_PAGES = 5                   # users.conversations pages of 200
TEXT_CHARS = 2000                   # what is kept of one message's text
READ_LIMIT = 4 * 1024 * 1024       # a page of history with its blocks can pass net.READ_LIMIT
CHANNEL_ID = re.compile(r"[CGD][A-Z0-9]{2,30}")
CHANNEL_NAME = re.compile(r"#?([a-z0-9][a-z0-9._-]{0,79})")
TS = re.compile(r"\d{9,11}\.\d{1,6}")
SIGNED_OUT = frozenset({"invalid_auth", "token_expired", "not_authed", "token_revoked", "account_inactive",
                        "user_removed_from_team", "team_disabled"})
REFUSED = {
    "channel_not_found": "Slack has no channel with that id, or you aren't in it. Use list_channels for the ids",
    "not_in_channel": "You aren't in that channel. Join it in Slack first",
    "is_archived": "That channel was archived",
    "thread_not_found": "Slack has no thread at that ts in that channel. Use the ts of the thread's first message",
    "restricted_action": "A workspace admin turned posting there off",
    "cant_post": "You can't post in that channel",
    "msg_too_long": "The message is too long for Slack. Shorten it",
    "no_text": "The message has no text",
    "feature_not_enabled": "Slack's search API isn't available on this workspace. Read a channel instead",
    "assistant_search_context_disabled": "Slack's search is having trouble. Try again in a minute",
    "query_too_long": "The search words are too long. Use fewer",
}


CHANNEL = {"type": "string", "description": "A channel's or DM's id (C…, G… or D…), or a channel's name with #."}
TOOLS = (
    tool_spec("search_messages", "Search Slack messages",
              "Search your Slack messages in public and private channels, group DMs and DMs, newest first. Slack's "
              "search words work: from:@name, in:#channel, before:2026-10-01, after:2026-10-01, is:thread. Each "
              "message has its author, channel, channel id, ts and link.",
              {"query": {"type": "string", "description": "What to search for, with Slack's search words."},
               "limit": count_schema(SEARCH_LIMIT, 10),
               "sort": {"type": "string", "enum": ["timestamp", "score"],
                        "description": "timestamp: newest first (the default); score: best match first."}},
              required=("query",)),
    tool_spec("list_channels", "List my Slack channels",
              "The public and private channels you're in: each one's id and #name.", {}),
    tool_spec("read_channel", "Read a Slack channel",
              "A channel's or DM's latest messages, newest first: each one's author's user id, ts, text and how many "
              "replies its thread has.",
              {"channel": CHANNEL,
               "limit": count_schema(HISTORY_LIMIT, 20)},
              required=("channel",)),
    tool_spec("read_thread", "Read a Slack thread",
              "A thread's messages, oldest first, from the ts of its first message (a search "
              "result's or read_channel's).",
              {"channel": CHANNEL, "ts": {"type": "string", "description": "The ts of the thread's first message."},
               "limit": count_schema(THREAD_LIMIT, 50)},
              required=("channel", "ts")),
    tool_spec("send_message", "Send a Slack message",
              "Post a message as you in a channel or DM, or as a reply in a thread. Plain text: Mobster escapes &, < "
              "and >, so it can't mention @channel or people, or make links.",
              {"channel": CHANNEL, "text": {"type": "string", "description": "The message."},
               "thread_ts": {"type": "string", "description": "Reply in this thread: the ts of its first message."}},
              required=("channel", "text"), read_only=False),
)


def connect(*, token=None, timeout=mcp.CONNECT_TIMEOUT, name=NAME, call=None):
    """A ready SlackSession for ``token``. AuthNeeded without one, or when Slack refuses it."""
    session = SlackSession(token, name=name, call=call)
    session.initialize(timeout=timeout)
    return session


class SlackSession(ApiSession):
    """The MCP session's surface (client.Session) over Slack's Web API."""

    NAME, TOOLS = NAME, TOOLS

    def _api(self, method, params, deadline, write=False):
        """Slack's answer to ``method`` when it's ``ok``, or a ``Refusal``. Raises AuthNeeded, NotSent and, for a
        write, OutcomeUnknown (a 5xx or no answer: it may have posted, and it's never sent again)."""
        if not self.token:
            raise mcp.AuthNeeded(f"{self.name} isn't signed in.")
        timeout = max(1.0, deadline - time.monotonic())
        body = urlencode({k: v for k, v in params.items() if v is not None}).encode()
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/x-www-form-urlencoded",
                   "User-Agent": webhook.USER_AGENT}
        try:
            reply = self.call("POST", f"{API}/{method}", body, headers, timeout=timeout, read_limit=READ_LIMIT)
        except net.NetError as error:
            if error.unsent:
                raise mcp.NotSent(error.message) from None
            if write:
                raise mcp.OutcomeUnknown(f"{error.message} The message may have posted: check Slack before you send it "
                                         "again.") from None
            raise mcp.OutcomeUnknown(error.message) from None
        if reply.status == 429:
            wait = reply.retry_after if str(reply.retry_after or "").isdigit() else "a few"
            return Refusal(f"Slack asked Mobster to wait {wait} seconds. Try again then.")
        if reply.status >= 500:
            if write:
                raise mcp.OutcomeUnknown(f"Slack answered {reply.status}. The message may have posted: check Slack "
                                         "before you send it again.")
            return Refusal(f"Slack answered {reply.status}. Try again in a minute.")
        try:
            answer = json.loads(reply.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            answer = None
        if not isinstance(answer, dict):
            if write and reply.status == 200:
                raise mcp.OutcomeUnknown("Slack's answer couldn't be read. The message may have posted: check Slack "
                                         "before you send it again.")
            return Refusal(f"Slack answered {reply.status} without a reply Mobster could read.")
        if answer.get("ok") is True:
            return answer
        error = str(answer.get("error") or "unknown")[:60]
        if error in SIGNED_OUT:
            raise mcp.AuthNeeded(f"Slack signed Mobster out ({error}). Sign in to Slack again in Settings › "
                                 "Connectors.")
        if error == "missing_scope":
            needed = str(answer.get("needed") or "a scope it wasn't given")[:120]
            return Refusal(f"Your Slack sign-in can't do this: it needs {needed}. Sign in to Slack again in Settings › "
                           "Connectors, and allow what Slack asks.")
        if error in ("ratelimited", "rate_limited"):
            return Refusal("Slack asked Mobster to wait. Try again in a minute.")
        return Refusal(f"Slack refused ({error}). {REFUSED[error]}." if error in REFUSED
                       else f"Slack refused ({error}).")

    def initialize(self, timeout=mcp.CONNECT_TIMEOUT):
        """auth.test: the token works (it needs no scope). AuthNeeded or NotSent when it doesn't."""
        answer = self._api("auth.test", {}, time.monotonic() + timeout)
        if isinstance(answer, Refusal):
            raise mcp.NotSent(answer.message)
        self.server_info = {"name": NAME, "team": str(answer.get("team") or "")[:100]}
        self.ready = True
        return {"serverInfo": self.server_info}

    def ping(self, timeout=mcp.PING_TIMEOUT):
        if not self.ready:
            raise mcp.NotSent(f"{self.name} isn't connected.")
        answer = self._api("auth.test", {}, time.monotonic() + timeout)
        if isinstance(answer, Refusal):
            raise mcp.NotSent(answer.message)
        return True

    def _search_messages(self, args, deadline):
        query = required_text(args.get("query"), "query", 500)
        answer = self._api("assistant.search.context", {
            "query": query, "channel_types": "public_channel,private_channel,mpim,im", "content_types": "messages",
            "limit": _limit(args.get("limit"), 10, SEARCH_LIMIT),
            "sort": _choice(args.get("sort"), ("timestamp", "score")),
            "sort_dir": "desc"}, deadline)
        if isinstance(answer, Refusal):
            return answer
        results = answer.get("results") if isinstance(answer.get("results"), dict) else {}
        messages = [{"author": short(m.get("author_name") or m.get("author_user_id"), 100),
                     "channel": short(m.get("channel_name"), 100), "channelId": short(m.get("channel_id"), 40),
                     "ts": short(m.get("message_ts"), 40), "text": short(m.get("content"), TEXT_CHARS),
                     "link": short(m.get("permalink"), 300)}
                    for m in results.get("messages") or () if isinstance(m, dict)]
        more = bool((answer.get("response_metadata") or {}).get("next_cursor"))
        return {"messages": messages, "more": more}

    def _channels(self, deadline):
        found, cursor = [], None
        for _ in range(CHANNEL_PAGES):
            answer = self._api("users.conversations", {"types": "public_channel,private_channel",
                                                       "exclude_archived": "true", "limit": "200", "cursor": cursor},
                               deadline)
            if isinstance(answer, Refusal):
                return answer
            for item in answer.get("channels") or ():
                if isinstance(item, dict) and CHANNEL_ID.fullmatch(str(item.get("id"))) \
                        and isinstance(item.get("name"), str):
                    found.append({"id": item["id"], "name": "#" + item["name"][:80],
                                  "private": bool(item.get("is_private"))})
            cursor = (answer.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return sorted(found, key=lambda item: item["name"])

    def _list_channels(self, args, deadline):
        found = self._channels(deadline)
        return found if isinstance(found, Refusal) else {"channels": found}

    def _channel_id(self, value, deadline):
        """A channel id as given, or the id of the channel you're in by that #name."""
        text = required_text(value, "channel", 90)
        if CHANNEL_ID.fullmatch(text):
            return text
        named = CHANNEL_NAME.fullmatch(text.lower())
        if not named:
            raise ValueError("channel is a channel's id (C…, G… or D…) or its #name")
        found = self._channels(deadline)
        if isinstance(found, Refusal):
            return found
        match = next((c["id"] for c in found if c["name"] == "#" + named.group(1)), None)
        if match is None:
            raise ValueError(f"You aren't in a channel called #{named.group(1)}. Use list_channels for the ones "
                             "you're in")
        return match

    def _read_channel(self, args, deadline):
        channel = self._channel_id(args.get("channel"), deadline)
        if isinstance(channel, Refusal):
            return channel
        answer = self._api("conversations.history", {"channel": channel,
                                                     "limit": _limit(args.get("limit"), 20, HISTORY_LIMIT)}, deadline)
        return answer if isinstance(answer, Refusal) else {"channelId": channel, "messages": _messages(answer),
                                                           "more": bool(answer.get("has_more"))}

    def _read_thread(self, args, deadline):
        channel = self._channel_id(args.get("channel"), deadline)
        if isinstance(channel, Refusal):
            return channel
        ts = required_text(args.get("ts"), "ts", 20)
        if not TS.fullmatch(ts):
            raise ValueError("ts is a message's ts, such as 1760000000.123456")
        answer = self._api("conversations.replies", {"channel": channel, "ts": ts,
                                                     "limit": _limit(args.get("limit"), 50, THREAD_LIMIT)}, deadline)
        return answer if isinstance(answer, Refusal) else {"channelId": channel, "messages": _messages(answer),
                                                           "more": bool(answer.get("has_more"))}

    def _send_message(self, args, deadline):
        channel = self._channel_id(args.get("channel"), deadline)
        if isinstance(channel, Refusal):
            return channel
        text = required_text(args.get("text"), "text", 40_000)
        thread = args.get("thread_ts")
        if thread is not None and not TS.fullmatch(str(thread)):
            raise ValueError("thread_ts is the ts of the thread's first message, such as 1760000000.123456")
        # &, < and > are escaped and link_names is off, so text from a screen can't mention @channel or a person, or
        # make a link.
        answer = self._api("chat.postMessage", {"channel": channel, "text": webhook.slack_text(text),
                                                "thread_ts": thread,
                                                "link_names": "false", "unfurl_links": "false",
                                                "unfurl_media": "false"}, deadline, write=True)
        if isinstance(answer, Refusal):
            return answer
        return {"posted": True, "channelId": short(answer.get("channel"), 40), "ts": short(answer.get("ts"), 40)}


def _messages(answer):
    return [{"user": short(m.get("user") or m.get("bot_id"), 40), "ts": short(m.get("ts"), 40),
             "text": short(m.get("text"), TEXT_CHARS),
             "replies": m.get("reply_count") if isinstance(m.get("reply_count"), int) else 0}
            for m in answer.get("messages") or () if isinstance(m, dict)]


def _limit(value, default, top):
    if value is None:
        return str(default)
    if type(value) is not int or not 1 <= value <= top:
        raise ValueError(f"limit is a whole number from 1 to {top}")
    return str(value)


def _choice(value, choices):
    if value is None:
        return choices[0]
    if value not in choices:
        raise ValueError(f"sort is {' or '.join(choices)}")
    return value
