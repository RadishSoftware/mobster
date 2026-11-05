"""The Microsoft 365 connector's tools, answered by Mobster with Microsoft Graph and your sign-in (a delegated token),
because Microsoft runs no MCP server for Outlook, Calendar, OneDrive, Contacts and Teams chats that a Mobster app can
sign in to."""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from ..outlets import net, webhook
from . import client as mcp
from .api_session import ApiSession, Refusal, count_schema, required_text, short, tool_spec

API = "https://graph.microsoft.com/v1.0"
NAME = "Microsoft 365"
LIMIT_TOP = 50
BODY_CHARS = 8000                   # what is kept of one message's text
TEXT_CHARS = 2000                   # what is kept of one chat message's text
SEND_CHARS = 28_000                 # Teams takes about 28 KB of message
READ_LIMIT = 4 * 1024 * 1024
ID = re.compile(r"[A-Za-z0-9_=:.@!%+\-]{1,512}")
EMAIL = re.compile(r"[^@\s<>,;\"']{1,64}@[A-Za-z0-9.-]{1,200}\.[A-Za-z]{2,24}")
WHEN = re.compile(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?(Z|[+-]\d{2}:\d{2})?")
ZONE = re.compile(r"[A-Za-z0-9_/ +.\-]{1,60}")
FOLDERS = ("inbox", "sentitems", "drafts", "archive", "deleteditems")
DENIED = frozenset({"ErrorAccessDenied", "Authorization_RequestDenied", "AccessDenied",
                    "ErrorInsufficientPermissionsToDelegate", "Forbidden"})
REFUSED = {
    "ErrorItemNotFound": "Microsoft has no item with that id. Use an id from a list or a search",
    "ResourceNotFound": "Microsoft has nothing at that id. Use an id from a list or a search",
    "ErrorInvalidIdMalformed": "That id isn't one Microsoft gave. Use an id from a list or a search",
    "ErrorQuotaExceeded": "Your mailbox is full",
    "ErrorMessageSizeExceeded": "The message is too big to send. Shorten it",
}


ADDRESSES = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20,
             "description": "Email addresses."}
WHEN_FIELD = {"type": "string", "description": "A date or a date and time, such as 2026-10-20 or 2026-10-20T14:00:00Z."}
TOOLS = (
    tool_spec("list_mail", "List Outlook mail",
              "Your Outlook messages, newest first: each one's id, subject, sender, time, read state and a preview. "
              "Give query to search instead (Outlook's search words work: from:sam, subject:invoice); a search is "
              "ordered by match.",
              {"folder": {"type": "string", "enum": list(FOLDERS), "description": "inbox if left out. Not used by a "
                          "search, which covers all folders."},
               "query": {"type": "string", "description": "Words to search for."},
               "limit": count_schema(LIMIT_TOP, 10)}),
    tool_spec("read_message", "Read an Outlook message",
              "One message's sender, recipients, subject, time and text, from an id that list_mail gave.",
              {"id": {"type": "string", "description": "The message's id."}}, required=("id",)),
    tool_spec("list_events", "List my calendar events",
              "Your calendar between two times, in time order: each event's id, subject, start, end (UTC), place, "
              "organizer and attendees.",
              {"start": WHEN_FIELD | {"description": "From when. Now if left out."},
               "end": WHEN_FIELD | {"description": "Until when. 7 days after start if left out."},
               "limit": count_schema(LIMIT_TOP, 25)}),
    tool_spec("create_event", "Create a calendar event",
              "Add an event to your calendar. With attendees, Outlook emails them an invitation as you.",
              {"subject": {"type": "string"}, "start": {"type": "string",
                                                        "description": "Local time, 2026-10-20T14:00:00."},
               "end": {"type": "string", "description": "Local time, 2026-10-20T15:00:00."},
               "timezone": {"type": "string", "description": "A time zone name such as "
                            "Europe/London; UTC if left out."},
               "attendees": ADDRESSES, "location": {"type": "string"}, "body": {"type": "string"}},
              required=("subject", "start", "end"), read_only=False),
    tool_spec("search_files", "Search OneDrive files",
              "Your OneDrive files and folders matching words in their name or content: each one's id, name, link, "
              "size and time changed.",
              {"query": {"type": "string", "description": "What to search for."}, "limit": count_schema(LIMIT_TOP, 10)},
              required=("query",)),
    tool_spec("list_contacts", "List my contacts",
              "Your Outlook contacts: each one's name, email addresses, company and phone. Give query to search them.",
              {"query": {"type": "string", "description": "A name or an address to search for."},
               "limit": count_schema(LIMIT_TOP, 25)}),
    tool_spec("list_chats", "List my Teams chats",
              "Your Teams chats, most recently active first: each one's id, topic, kind and last activity.",
              {"limit": count_schema(LIMIT_TOP, 20)}),
    tool_spec("read_chat", "Read a Teams chat",
              "A Teams chat's latest messages, newest first: each one's sender, time and text.",
              {"chat_id": {"type": "string", "description": "A chat's id from list_chats."},
               "limit": count_schema(LIMIT_TOP, 20)},
              required=("chat_id",)),
    tool_spec("send_mail", "Send an email",
              "Send an email as you from Outlook, in plain text. A copy goes in Sent Items.",
              {"to": ADDRESSES, "cc": ADDRESSES, "subject": {"type": "string"}, "body": {"type": "string"}},
              required=("to", "subject", "body"), read_only=False),
    tool_spec("send_chat_message", "Send a Teams chat message",
              "Post a message as you in a Teams chat, in plain text.",
              {"chat_id": {"type": "string", "description": "A chat's id from list_chats."},
               "text": {"type": "string"}},
              required=("chat_id", "text"), read_only=False),
)
WRITES = frozenset(t["name"] for t in TOOLS if not t["annotations"]["readOnlyHint"])


def connect(*, token=None, timeout=mcp.CONNECT_TIMEOUT, name=NAME, call=None):
    """A ready GraphSession for ``token``. AuthNeeded without one, or when Microsoft refuses it."""
    session = GraphSession(token, name=name, call=call)
    session.initialize(timeout=timeout)
    return session


class GraphSession(ApiSession):
    """The MCP session's surface (client.Session) over Microsoft Graph."""

    NAME, TOOLS = NAME, TOOLS

    def _api(self, method, path, deadline, query=None, body=None, headers=None):
        """Graph's JSON answer ({} for a 2xx with none) or a ``Refusal``. Raises AuthNeeded, NotSent and, for a write,
        OutcomeUnknown (a 5xx or no answer: it may have happened, and it's never sent again)."""
        if not self.token:
            raise mcp.AuthNeeded(f"{self.name} isn't signed in.")
        write = method != "GET"
        url = f"{API}{path}" + (f"?{urlencode(query, quote_via=quote)}" if query else "")
        send = {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "User-Agent": webhook.USER_AGENT,
                **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            send["Content-Type"] = "application/json"
        try:
            reply = self.call(method, url, data, send, timeout=max(1.0, deadline - time.monotonic()),
                              read_limit=READ_LIMIT)
        except net.NetError as error:
            if error.unsent:
                raise mcp.NotSent(error.message) from None
            if write:
                raise mcp.OutcomeUnknown(f"{error.message} It may have gone through: check Microsoft 365 before you do "
                                         "it again.") from None
            raise mcp.OutcomeUnknown(error.message) from None
        if reply.status == 429:
            wait = reply.retry_after if str(reply.retry_after or "").isdigit() else "a few"
            return Refusal(f"Microsoft asked Mobster to wait {wait} seconds. Try again then.")
        if reply.status >= 500:
            if write:
                raise mcp.OutcomeUnknown(f"Microsoft answered {reply.status}. It may have gone through: check "
                                         "Microsoft 365 before you do it again.")
            return Refusal(f"Microsoft answered {reply.status}. Try again in a minute.")
        try:
            answer = json.loads(reply.body.decode("utf-8")) if reply.body else {}
        except (ValueError, UnicodeDecodeError):
            answer = None
        if 200 <= reply.status < 300:
            if isinstance(answer, dict):
                return answer
            # A write Microsoft answered 2xx happened, whatever its body says; a read has nothing to show.
            return {} if write else Refusal("Microsoft's answer couldn't be read. Try again in a minute.")
        error = answer.get("error") if isinstance(answer, dict) else None
        code = str(error.get("code") if isinstance(error, dict) else "")[:60]
        if reply.status == 401:
            raise mcp.AuthNeeded(f"Microsoft signed Mobster out ({code or 401}). Sign in to {self.name} again in "
                                 "Settings › Connectors.")
        if reply.status == 403 or code in DENIED:
            return Refusal(f"Microsoft refused ({code or 403}). Your {self.name} sign-in can't do this: sign in again "
                           f"in Settings › Connectors and allow what Microsoft asks, or ask your admin.")
        if code in REFUSED:
            return Refusal(f"Microsoft refused ({code}). {REFUSED[code]}.")
        said = re.sub(r"\s+", " ", str(error.get("message") or "") if isinstance(error, dict) else "")[:200]
        return Refusal(f"Microsoft refused ({code or reply.status})." + (f" {said}" if said else ""))

    def initialize(self, timeout=mcp.CONNECT_TIMEOUT):
        """GET /me: the token works (it needs only User.Read). AuthNeeded or NotSent when it doesn't."""
        answer = self._api("GET", "/me", time.monotonic() + timeout, {"$select": "displayName,userPrincipalName"})
        if isinstance(answer, Refusal):
            raise mcp.NotSent(answer.message)
        self.server_info = {"name": NAME, "user": str(answer.get("userPrincipalName") or "")[:100]}
        self.ready = True
        return {"serverInfo": self.server_info}

    def ping(self, timeout=mcp.PING_TIMEOUT):
        if not self.ready:
            raise mcp.NotSent(f"{self.name} isn't connected.")
        answer = self._api("GET", "/me", time.monotonic() + timeout, {"$select": "id"})
        if isinstance(answer, Refusal):
            raise mcp.NotSent(answer.message)
        return True

    def _list_mail(self, args, deadline):
        top = _limit(args.get("limit"), 10)
        select = "id,subject,from,receivedDateTime,isRead,bodyPreview,webLink"
        query = args.get("query")
        if query is not None:
            words = " ".join(required_text(query, "query", 300).replace('"', " ").split())
            answer = self._api("GET", "/me/messages", deadline,
                               {"$search": f'"{words}"', "$top": top, "$select": select})
        else:
            folder = _choice(args.get("folder"), FOLDERS, "folder")
            answer = self._api("GET", f"/me/mailFolders/{folder}/messages", deadline,
                               {"$top": top, "$orderby": "receivedDateTime desc", "$select": select})
        if isinstance(answer, Refusal):
            return answer
        return {"messages": [{"id": short(m.get("id"), 512), "subject": short(m.get("subject"), 300),
                              "from": _address(m.get("from")), "received": short(m.get("receivedDateTime"), 40),
                              "read": bool(m.get("isRead")), "preview": short(m.get("bodyPreview"), 300),
                              "link": short(m.get("webLink"), 500)}
                             for m in _items(answer)],
                "more": "@odata.nextLink" in answer}

    def _read_message(self, args, deadline):
        ident = _id(args.get("id"), "id")
        answer = self._api("GET", f"/me/messages/{quote(ident, safe='')}", deadline,
                           {"$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,webLink"},
                           headers={"Prefer": 'outlook.body-content-type="text"'})
        if isinstance(answer, Refusal):
            return answer
        body = answer.get("body") if isinstance(answer.get("body"), dict) else {}
        return {"id": short(answer.get("id"), 512), "subject": short(answer.get("subject"), 300),
                "from": _address(answer.get("from")), "to": [_address(r) for r in answer.get("toRecipients") or ()],
                "cc": [_address(r) for r in answer.get("ccRecipients") or ()],
                "received": short(answer.get("receivedDateTime"), 40), "text": short(body.get("content"), BODY_CHARS),
                "link": short(answer.get("webLink"), 500)}

    def _list_events(self, args, deadline):
        start = _when(args.get("start"), "start") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = _when(args.get("end"), "end")
        if end is None:
            first = datetime.fromisoformat(_utc(start).replace("Z", "+00:00"))
            end = (first + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        answer = self._api("GET", "/me/calendarView", deadline,
                           {"startDateTime": _utc(start), "endDateTime": _utc(end),
                            "$top": _limit(args.get("limit"), 25), "$orderby": "start/dateTime",
                            "$select": "id,subject,start,end,location,organizer,attendees,webLink,isCancelled"},
                           headers={"Prefer": 'outlook.timezone="UTC"'})
        if isinstance(answer, Refusal):
            return answer
        return {"events": [{"id": short(e.get("id"), 512), "subject": short(e.get("subject"), 300),
                            "start": short((e.get("start") or {}).get("dateTime"), 40),
                            "end": short((e.get("end") or {}).get("dateTime"), 40),
                            "place": short((e.get("location") or {}).get("displayName"), 200),
                            "organizer": _address(e.get("organizer")),
                            "attendees": [_address(a) for a in (e.get("attendees") or ())[:20]],
                            "cancelled": bool(e.get("isCancelled")), "link": short(e.get("webLink"), 500)}
                           for e in _items(answer)],
                "more": "@odata.nextLink" in answer}

    def _search_files(self, args, deadline):
        words = required_text(args.get("query"), "query", 300).replace("'", "''")
        answer = self._api("GET", f"/me/drive/root/search(q='{quote(words, safe='')}')", deadline,
                           {"$top": _limit(args.get("limit"), 10),
                            "$select": "id,name,webUrl,size,lastModifiedDateTime,file,folder"})
        if isinstance(answer, Refusal):
            return answer
        return {"files": [{"id": short(f.get("id"), 512), "name": short(f.get("name"), 300),
                           "kind": "folder" if f.get("folder") else "file", "size": f.get("size")
                           if isinstance(f.get("size"), int) else None,
                           "modified": short(f.get("lastModifiedDateTime"), 40), "link": short(f.get("webUrl"), 500)}
                          for f in _items(answer)],
                "more": "@odata.nextLink" in answer}

    def _list_contacts(self, args, deadline):
        query = {"$top": _limit(args.get("limit"), 25),
                 "$select": "id,displayName,emailAddresses,companyName,mobilePhone"}
        if args.get("query") is not None:
            query["$search"] = '"' + " ".join(
                required_text(args["query"], "query", 200).replace('"', " ").split()) + '"'
        answer = self._api("GET", "/me/contacts", deadline, query)
        if isinstance(answer, Refusal):
            return answer
        return {"contacts": [{"id": short(c.get("id"), 512), "name": short(c.get("displayName"), 200),
                              "emails": [short(a.get("address"), 200) for a in (c.get("emailAddresses") or ())[:5]
                                         if isinstance(a, dict)],
                              "company": short(c.get("companyName"), 200), "phone": short(c.get("mobilePhone"), 40)}
                             for c in _items(answer)],
                "more": "@odata.nextLink" in answer}

    def _list_chats(self, args, deadline):
        answer = self._api("GET", "/me/chats", deadline, {"$top": _limit(args.get("limit"), 20),
                                                           "$orderby": "lastMessagePreview/createdDateTime desc",
                                                           "$select": "id,topic,chatType,lastUpdatedDateTime"})
        if isinstance(answer, Refusal):
            return answer
        return {"chats": [{"id": short(c.get("id"), 512), "topic": short(c.get("topic"), 200),
                           "kind": short(c.get("chatType"), 20), "updated": short(c.get("lastUpdatedDateTime"), 40)}
                          for c in _items(answer)],
                "more": "@odata.nextLink" in answer}

    def _read_chat(self, args, deadline):
        chat = _id(args.get("chat_id"), "chat_id")
        answer = self._api("GET", f"/me/chats/{quote(chat, safe='')}/messages", deadline,
                           {"$top": _limit(args.get("limit"), 20)})
        if isinstance(answer, Refusal):
            return answer
        found = []
        for m in _items(answer):
            sender = (((m.get("from") or {}).get("user") or {}).get("displayName") if isinstance(m.get("from"), dict)
                      else None)
            body = m.get("body") if isinstance(m.get("body"), dict) else {}
            found.append({"id": short(m.get("id"), 80), "from": short(sender, 200),
                          "time": short(m.get("createdDateTime"), 40),
                          "text": short(re.sub(r"<[^>]+>", "", str(body.get("content") or ""))
                                         if body.get("contentType") == "html"
                                         else body.get("content"), TEXT_CHARS)})
        return {"chatId": chat, "messages": found, "more": "@odata.nextLink" in answer}

    def _send_mail(self, args, deadline):
        message = {"subject": required_text(args.get("subject"), "subject", 300),
                   "body": {"contentType": "Text", "content": required_text(args.get("body"), "body", 100_000)},
                   "toRecipients": _recipients(args.get("to"), "to", required=True)}
        copied = _recipients(args.get("cc"), "cc")
        if copied:
            message["ccRecipients"] = copied
        answer = self._api("POST", "/me/sendMail", deadline, body={"message": message, "saveToSentItems": True})
        return answer if isinstance(answer, Refusal) else {"sent": True}

    def _create_event(self, args, deadline):
        zone = args.get("timezone", "UTC")
        if not isinstance(zone, str) or not ZONE.fullmatch(zone):
            raise ValueError("timezone is a time zone name such as Europe/London")
        event = {"subject": required_text(args.get("subject"), "subject", 300),
                 "start": {"dateTime": _local(args.get("start"), "start"), "timeZone": zone},
                 "end": {"dateTime": _local(args.get("end"), "end"), "timeZone": zone}}
        if args.get("location") is not None:
            event["location"] = {"displayName": required_text(args["location"], "location", 200)}
        if args.get("body") is not None:
            event["body"] = {"contentType": "Text", "content": required_text(args["body"], "body", 20_000)}
        guests = _recipients(args.get("attendees"), "attendees")
        if guests:
            event["attendees"] = [{**g, "type": "required"} for g in guests]
        answer = self._api("POST", "/me/events", deadline, body=event)
        return answer if isinstance(answer, Refusal) else {"created": True, "id": short(answer.get("id"), 512),
                                                           "link": short(answer.get("webLink"), 500)}

    def _send_chat_message(self, args, deadline):
        chat = _id(args.get("chat_id"), "chat_id")
        text = required_text(args.get("text"), "text", SEND_CHARS)
        answer = self._api("POST", f"/me/chats/{quote(chat, safe='')}/messages", deadline,
                           body={"body": {"contentType": "text", "content": text}})
        return (answer if isinstance(answer, Refusal)
                else {"posted": True, "chatId": chat, "id": short(answer.get("id"), 80)})


def _items(answer):
    return [item for item in answer.get("value") or () if isinstance(item, dict)]


def _address(value):
    box = value.get("emailAddress") if isinstance(value, dict) else None
    if not isinstance(box, dict):
        return None
    return {"name": short(box.get("name"), 200), "address": short(box.get("address"), 200)}


def _id(value, field):
    text = required_text(value, field, 512)
    if not ID.fullmatch(text):
        raise ValueError(f"{field} is an id that a list or a search gave")
    return text


def _limit(value, default):
    if value is None:
        return default
    if type(value) is not int or not 1 <= value <= LIMIT_TOP:
        raise ValueError(f"limit is a whole number from 1 to {LIMIT_TOP}")
    return value


def _choice(value, choices, field):
    if value is None:
        return choices[0]
    if value not in choices:
        raise ValueError(f"{field} is {' or '.join(choices)}")
    return value


def _recipients(value, field, required=False):
    if value is None and not required:
        return []
    if not isinstance(value, list) or not value or len(value) > 20 or not all(isinstance(a, str) for a in value):
        raise ValueError(f"{field} is a list of 1 to 20 email addresses")
    found = [a.strip() for a in value]
    if not all(EMAIL.fullmatch(a) for a in found):
        raise ValueError(f"{field} has an address that isn't an email address")
    return [{"emailAddress": {"address": a}} for a in found]


def _when(value, field):
    if value is None:
        return None
    if not isinstance(value, str) or not WHEN.fullmatch(value.strip()):
        raise ValueError(f"{field} is a date or a date and time, such as 2026-10-20T14:00:00Z")
    return value.strip()


def _utc(value):
    """A date or time as Graph's calendarView wants it: with a zone (UTC when it has none)."""
    text = value if len(value) > 10 else value + "T00:00:00"
    return text if re.search(r"(Z|[+-]\d{2}:\d{2})$", text) else text + "Z"


def _local(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?", value.strip()):
        raise ValueError(f"{field} is a local time such as 2026-10-20T14:00:00")
    return value.strip()
