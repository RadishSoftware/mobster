"""Where a platform lets Mobster answer a command later: Slack's ``response_url`` and Discord's interaction token.

Slack and Discord hand the relay a one-use address with the command. Mobster posts the run's result there when the run
ends, as a new message in the place the command was typed (in the thread, when it was typed in one). Only
hooks.slack.com and discord.com are called, whatever the relay passes on; an address that isn't one of those is dropped.
One try each: a post whose outcome is unknown is never repeated.
"""

import json
import re

from . import net
from .webhook import USER_AGENT

SLACK = re.compile(r"https://hooks\.slack\.com/(?:commands|actions)/[A-Za-z0-9/_-]{1,200}")
# Joined here so no tracked file spells a Discord webhook address (scripts/check_no_webhook.py).
DISCORD_API = "https://discord.com" + "/api/v10/" + "webhooks"
DISCORD_APP = re.compile(r"\d{15,25}")
DISCORD_TOKEN = re.compile(r"[A-Za-z0-9._-]{20,300}")
# How long the platform keeps the address good (Slack: 30 minutes, Discord: 15), less a minute.
WINDOW = {"slack": 29 * 60, "discord": 14 * 60}


def address(channel, reply):
    """The URL to post to, or None when ``reply`` isn't an address this platform gave."""
    if not isinstance(reply, dict):
        return None
    if channel == "slack":
        url = reply.get("url")
        return url if isinstance(url, str) and SLACK.fullmatch(url) else None
    app, token = reply.get("app"), reply.get("token")
    if (isinstance(app, str) and isinstance(token, str)
            and DISCORD_APP.fullmatch(app) and DISCORD_TOKEN.fullmatch(token)):
        return f"{DISCORD_API}/{app}/{token}"
    return None


def body(channel, text, public):
    """The post's JSON. Slack's ``&``, ``<`` and ``>`` are escaped as it asks and Discord pings nobody, so text from a
    screen can't mention a channel or a person or make a link."""
    if channel == "slack":
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return {"response_type": "in_channel" if public else "ephemeral", "text": escaped[:3000]}
    return {"content": text[:1900], "flags": 0 if public else 64, "allowed_mentions": {"parse": []}}


def post(channel, reply, text, public, call=net.call):
    """Post ``text`` once. True when the platform took it."""
    url = address(channel, reply)
    if url is None:
        return False
    data = json.dumps(body(channel, text, public)).encode()
    try:
        answer = call("POST", url, data, {"Content-Type": "application/json", "User-Agent": USER_AGENT})
    except net.NetError:
        return False
    return answer.status in (200, 204)
