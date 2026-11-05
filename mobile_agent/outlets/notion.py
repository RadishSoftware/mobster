"""A Notion destination: each result is a new page in one Notion database, through Mobster's public connection."""

import datetime
import json
import re

from . import connect, net
from .kinds import Kind, again, careful, connection, flat, target, title_of
from .webhook import USER_AGENT, Outcome

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
TEXT_MAX = 2000
BLOCKS_MAX = 40
LABEL = "Notion"
HEX32 = re.compile(r"[0-9a-f]{32}")


def headers(secret, body=False):
    out = {"Authorization": f"Bearer {secret}", "Notion-Version": VERSION, "User-Agent": USER_AGENT}
    if body:
        out["Content-Type"] = "application/json"
    return out


def refusal(status, answer):
    """Notion's refusal as a sentence: what happened, then what to do."""
    code = answer.get("code") if isinstance(answer, dict) else None
    if status == 401 or code == "unauthorized":
        return "Notion signed Mobster out. " + again(LABEL)
    if status == 404 or code == "object_not_found":
        return ("Notion no longer shares that database with Mobster. Connect Notion again, and pick it in Notion's "
                "page list.")
    if status == 403 or code == "restricted_resource":
        return "Mobster can't add pages to that database. Connect Notion again, and allow it to insert content."
    if status == 400 and isinstance(answer, dict) and isinstance(answer.get("message"), str):
        return f"Notion refused the page: {answer['message'][:200]}"
    return f"Notion answered {status}."


def plain(rich):
    return "".join(part.get("plain_text", "") for part in rich or () if isinstance(part, dict)).strip()


class Refused(ValueError):
    """Notion answered, and not with what was asked. ``status``: its HTTP status."""

    def __init__(self, status, answer):
        super().__init__(refusal(status, answer))
        self.status = status


def read_database(call, secret, database):
    """(title, properties) of the database: {name: type}. NetError, or Refused."""
    reply = call("GET", f"{API}/databases/{database}", None, headers(secret))
    answer = reply.json_object()
    if reply.status != 200 or not isinstance(answer, dict):
        raise Refused(reply.status, answer)
    properties = {name: spec.get("type") for name, spec in (answer.get("properties") or {}).items()
                  if isinstance(spec, dict)}
    return plain(answer.get("title")) or "Untitled", properties


def databases(call, secret):
    """The databases Mobster may see, the ones edited last first: [{"id", "name"}]. NetError, or Refused."""
    query = {"filter": {"property": "object", "value": "database"}, "page_size": 100,
             "sort": {"direction": "descending", "timestamp": "last_edited_time"}}
    reply = call("POST", f"{API}/search", json.dumps(query).encode(), headers(secret, body=True))
    answer = reply.json_object()
    if reply.status != 200 or not isinstance(answer, dict):
        raise Refused(reply.status, answer)
    found = []
    for item in answer.get("results") or ():
        if isinstance(item, dict) and item.get("object") == "database" and isinstance(item.get("id"), str) \
                and HEX32.fullmatch(item["id"].replace("-", "").lower()):
            found.append({"id": item["id"], "name": (plain(item.get("title")) or "Untitled")[:100]})
    return found


def rich(text):
    return [{"type": "text", "text": {"content": str(text)[:TEXT_MAX]}}]


def number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = re.sub(r"[\s,$€£%]", "", str(value))
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return None


def date(value):
    text = str(value).strip()
    try:
        datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return {"start": text}


def value_for(kind, value):
    """A property value of type ``kind`` holding ``value``, or None when that type can't hold it."""
    if value is None or value == "":
        return None
    if kind == "rich_text":
        return {"rich_text": rich(value)}
    if kind == "number":
        found = number(value)
        return {"number": found} if found is not None else None
    if kind == "checkbox":
        if isinstance(value, bool):
            return {"checkbox": value}
        text = str(value).strip().lower()
        return {"checkbox": text in ("true", "yes", "1")} if text in ("true", "yes", "1", "false", "no", "0") else None
    if kind == "select":
        return {"select": {"name": str(value).replace(",", " ")[:100]}}
    if kind == "multi_select":
        items = value if isinstance(value, list) else str(value).split(",")
        names = [str(item).replace(",", " ").strip()[:100] for item in items if str(item).strip()][:20]
        return {"multi_select": [{"name": name} for name in names]} if names else None
    if kind == "date":
        found = date(value)
        return {"date": found} if found else None
    if kind == "url":
        text = str(value).strip()
        return {"url": text[:2000]} if text.startswith(("https://", "http://")) else None
    if kind == "email":
        text = str(value).strip()
        return {"email": text[:200]} if "@" in text and " " not in text else None
    if kind == "phone_number":
        return {"phone_number": str(value)[:50]}
    return None


def page(database, properties, body):
    """The new page: its title, the columns that match a field of the result, and the result in its body."""
    title_name = next((name for name, kind in properties.items() if kind == "title"), "Name")
    values = {title_name: {"title": rich(title_of(body))}}
    row = {name.casefold(): value for name, value in flat(body).items()}
    for name, kind in properties.items():
        if kind == "title":
            continue
        found = value_for(kind, row.get(name.casefold()))
        if found is not None:
            values[name] = found
    blocks = []
    answer = body.get("answer") or ""
    for start in range(0, min(len(answer), TEXT_MAX * 20), TEXT_MAX):
        blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(answer[start:])}})
    if body.get("reason"):
        blocks.append({"object": "block", "type": "paragraph",
                       "paragraph": {"rich_text": rich(f"Why it stopped: {body['reason']}")}})
    if body.get("data") is not None:
        blocks.append({"object": "block", "type": "code", "code": {
            "language": "json", "rich_text": rich(json.dumps(body["data"], ensure_ascii=False, indent=2))}})
    return {"parent": {"database_id": database}, "properties": values, "children": blocks[:BLOCKS_MAX]}


class Notion(Kind):
    id, label = "notion", "Notion"
    example = "Notion"
    fields = ("connect", "target")
    replay_safe = False
    provider, keep = "notion", ("workspace_name",)

    def connected(self, call, tokens):
        workspace = (tokens.get("kept") or {}).get("workspace_name")
        try:
            found = databases(call, tokens["access_token"])
        except Refused as error:
            raise ValueError(str(error)) from None
        oauth = {key: value for key, value in tokens.items() if key != "kept"}
        return {"credentials": {"oauth": oauth},
                "account": workspace[:100] if isinstance(workspace, str) and workspace.strip() else None,
                "targets": found}

    def setup(self, body, current=None):
        if current is not None and not ({"connect", "target"} & set(body)):
            return {}
        found = connection(body, self)
        database = target(found, body.get("target"), "a database")
        config = {"database": database["id"], "title": database["name"]}
        if found.get("account"):
            config["workspace"] = found["account"]
        return {"hint": f"Notion › {database['name'][:48]}", "has_secret": True, "config": config,
                "vault": {"url": None, "secret": None, "oauth": found["credentials"]["oauth"]}, "last_test": None}

    @staticmethod
    def _read(ctx):
        """(secret, properties): the database read with a token that works, renewed once after a 401."""
        database = ctx.destination["config"].get("database")
        secret = None
        for refresh in (False, True):
            try:
                secret = connect.access(ctx, LABEL, refresh=refresh)
                return secret, read_database(ctx.call, secret, database)
            except connect.NoToken as error:
                return None, error.outcome
            except net.NetError as error:
                return None, Outcome(False, error.retry, None, error.message, unsent=True)
            except Refused as error:
                if error.status == 401 and not refresh:
                    continue
                return None, Outcome(False, error.status == 429 or (error.status or 0) >= 500, error.status,
                                     str(error), unsent=True)
        return None, Outcome(False, False, 401, refusal(401, None), unsent=True)

    def send(self, ctx, body):
        secret, read = self._read(ctx)
        if secret is None:
            return read
        _title, properties = read
        database = ctx.destination["config"].get("database")
        data = json.dumps(page(database, properties, body), ensure_ascii=False).encode("utf-8")
        reply, failed = careful(lambda: ctx.call("POST", f"{API}/pages", data, headers(secret, body=True)),
                                "the database")
        if failed:
            return failed
        if reply.status == 200:
            return Outcome(True, False, 200)
        if reply.status == 429:
            return Outcome(False, True, 429, "Notion asked Mobster to wait.", unsent=True)
        if reply.status >= 500:
            return Outcome(False, False, reply.status, f"Notion answered {reply.status}. Check the database, then "
                                                       "resend if the page is missing.")
        return Outcome(False, False, reply.status, refusal(reply.status, reply.json_object()))

    def test(self, ctx):
        """Reads the database: the sign-in and the connection work. No page is made."""
        secret, read = self._read(ctx)
        if secret is None:
            return Outcome(False, False, read.status, read.message)
        title, properties = read
        names = {name.casefold() for name in properties}
        matched = [name for name in ("Answer", "Status", "Finished", "Workflow", "Reason", "Run")
                   if name.casefold() in names]
        line = f"Connected to {title}."
        if matched:
            line += " Fills " + ", ".join(matched) + "."
        return Outcome(True, False, 200, line)
