"""The prompt and output schema that compile a request into a task contract."""

from .contract_screen import ACTS, KINDS, MAX_ITEMS

SYSTEM = """Compile a phone agent's task contract from the user's request, before any work starts. Code enforces \
the contract: the agent may only perform the commits you declare, and the task counts as done only when every \
item has on-screen proof.

Item kinds:
- READ: every app, folder, list, record or tab the request names as a place to look, check or review (one item \
per source).
- WRITE: every text or value the agent must enter or change (a note, a document section, a comment body, a \
message body, a cell, an alarm, a form field). payload = the exact text only when the request dictates it word \
for word (a title in quotes, a time), else "".
- COMMIT: every act the request asks for that other people can see, that spends, moves or requests money, that \
orders, books or reserves, that follows or subscribes, that reacts to a post, that saves, archives, cancels or \
deletes, shares or calls, that gets, installs or buys an app from the App Store (act install, what = the app's \
name): anything a user would want to approve before it happens. One item per act and object. \
Declare only acts the request asks for; never infer one from a goal ("plan my trip" does not ask to book). A \
conditional act ("if rain, message X") is still declared, with its condition. A message, reply, comment or post \
the request asks the agent to compose, write or draft is also to be sent or posted, unless the request says not \
to: declare that COMMIT too, quoting the words that ask for the message.
- REPORT: every separate detail the user asks to be told or to have confirmed.
- FORBID: every act the request rules out ("do not delete anything", "don't send it yet"), with that act.

Fields:
- app: the app's name from the list, or "" if none.
- what: the source (READ), the target (WRITE), the object acted on (COMMIT, FORBID), or the detail (REPORT). \
Name the object as the request does (a person, a company, a document title).
- act: for a COMMIT or FORBID, its kind; "none" for other items.
- count: for a COMMIT, "1", a number, or "each" when the request says each, all or any; "" for other items.
- condition: the request's condition for the item, else "".
- quote: the request's own words that ask for this item, copied verbatim as one contiguous span. For a COMMIT \
the quote must include the words that ask for that act.
Keep the request's order; at most 24 items."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {
    "items": {"type": "array", "maxItems": MAX_ITEMS, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["kind", "app", "what", "payload", "act", "count", "condition", "quote"],
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "app": {"type": "string"}, "what": {"type": "string"}, "payload": {"type": "string"},
            "act": {"type": "string", "enum": list(ACTS)}, "count": {"type": "string"},
            "condition": {"type": "string"}, "quote": {"type": "string"}}}}}}
