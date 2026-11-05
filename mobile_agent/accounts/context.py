"""A profile's notes for the agent, and its values in a task's placeholders (the feature is Profiles; see screen.py)."""

import re

from .. import harness_api
from .store import display

ORDER = 15
MAX_CHARS = 4000
PLACEHOLDER = re.compile(r"(?<!\{)\{\s*([A-Za-z][A-Za-z0-9_ -]{0,39}?)\s*\}(?!\})")


def values(account):
    """{lowercase name: value} a placeholder may name."""
    out = {"handle": "@" + account["handle"]}
    if account.get("signature"):
        out["signature"] = account["signature"]
    links = account.get("links") or []
    if links:
        out["link"] = links[0]["url"]
        for link in links:
            out.setdefault(re.sub(r"\s+", "_", link["label"].strip().casefold()), link["url"])
            out.setdefault(link["label"].strip().casefold(), link["url"])
    for name, value in (account.get("variables") or {}).items():
        if value:
            out.setdefault(name, value)
    return out


def fill(goal, account):
    """``goal`` with the profile's values in its placeholders: {handle}, {signature}, {link}, link labels, variables."""
    known = values(account)

    def put(match):
        name = " ".join(match.group(1).split()).casefold()
        value = known.get(name, known.get(name.replace(" ", "_")))
        return value if value is not None else match.group(0)

    return PLACEHOLDER.sub(put, goal or "")


def text(account):
    lines = [f"You act as {display(account)} in this task. Everything you post, reply, comment or send there goes out "
             f"as @{account['handle']}.",
             f"Before the first post, reply, comment or message in {account['app']['name']}, open its own profile "
             "screen (the one with Edit profile) and use CHECK_PROFILE. If another profile is signed in, stop: "
             "Mobster never switches profiles."]
    if account.get("notes"):
        lines.append("How this profile sounds (the person's notes): " + account["notes"])
    if account.get("signature"):
        lines.append("Signature: " + account["signature"])
    for link in account.get("links") or ():
        lines.append(f"Link, {link['label']}: {link['url']}")
    for name, value in (account.get("variables") or {}).items():
        if value:
            lines.append(f"{name}: {value}")
    return "\n".join(lines)[:MAX_CHARS]


class AccountProvider:
    key = "account"
    max_chars = MAX_CHARS

    def __init__(self, account):
        self.account = account

    def blocks(self, ctx):
        return (harness_api.ContextBlock(key=self.key, title="The profile", text=text(self.account), stable=True),)

    def turn_blocks(self, ctx, turn):
        return ()


def provider(ctx):
    """The profile context captured when this run was admitted."""
    account = (ctx.extras or {}).get("account")
    if not isinstance(account, dict) or not account.get("id"):
        return None
    snapshot = getattr(ctx, "account_context", None)
    return AccountProvider(snapshot) if snapshot is not None else None
