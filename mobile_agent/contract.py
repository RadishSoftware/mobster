"""Proof-gated completion (frontier Bet 1): the task contract, the declared-commit policy, the source
ledger and the DONE gate in code.

One text-only call (reasoning none, run while the first screen is read) compiles the request into typed
items: READ a source, WRITE a value, COMMIT an act other people see or that spends, REPORT a detail,
FORBID an act the request rules out. Code then does three jobs the prompt used to do:

- Commits pass only when declared. A tap or long press on a control whose first words name a commit
  ("Send", "Pay $42.00", "Place Order"), or Return in a message composer, passes only when every commit
  verb in it belongs to the act family of an open COMMIT item of the current app, or is the first commit
  word of that item's verbatim quote ("Request a CityRideX" allows "Request" whatever act it compiled
  to); its amount must meet
  the item's bounds ("under $50"); a message needs text the agent typed in a field on screen (a feed's
  share "Send" has none); an object commit (Follow) needs its object's name on screen. A COMMIT whose
  quote is not verbatim in the request is dropped. F1 replay (26 Sep, 88 runs): the regex guard refused
  63 required commits; this policy allows all 63 and refuses every unrequested one.
- Receipts close items: a sent message shows in a non-editable row and has left the composer; a booking,
  order, payment or follow shows a new confirmation row; a write reads back in its app on a screen recorded
  after this run first wrote or committed there (typing, a picker, a tap on a control that names an act), with
  numbers as whole tokens, or the model cites the row that showed it already so ("already: <row>"); a write that adds
  with nothing typed for it needs a new row per object it names; a reported value
  was on a screen at or before the turn it was recorded (the ledger); a named source was seen in its app.
  A closed commit refuses repeats (a second Follow tap unfollows); "each" items stay open.
- DONE is accepted when every item has its receipt, or with at most LOW_TURNS turns left, when the
  answer names what is unproven. A missing receipt reopens its item with a one-line reason (at most
  MAX_REOPENS times an item, then it is marked unproven so the gate cannot eat the budget).

Elements are duck-typed (label, role, rect as screen fractions, editable, value): state.Element in the
agent, recorded rows in the offline replay.
"""

import re
from types import SimpleNamespace

from .contract_screen import (ACTS, ACT_LABEL, ADDRESS, ADDS, ALREADY, APP_STORE, ATOM, COMMIT_WORDS, COMPUTED_ITEM,
                              CONFIRM_ITEM, CONFIRM_WORDS, DELIVERY, EMPTY_STATE, FAMILIES, FOLDERS, GENERIC, Gate,
                              Item, KINDS, LOW_TURNS, MAX_ITEMS, MAX_REOPENS, MESSAGE, MESSAGE_ACTS, MONEY,
                              MONEY_ACTS, MORE_THAN_ONE, NONE_VALUE, NOTEBOOK_KINDS, NO_RESULTS, NUMBER, OBJECT_ACTS,
                              OBJECT_STOP, OFF_VALUES, ON_VALUES, OTHERS, Pending, ROW_ROLES, STATE_ACTS, STATE_ON,
                              SUMMARY_ITEM, SWITCH_ROLES, TAINT_CHARS, TO_FIELD, WRITE_OPS, center_y, as_float,
                              overlaps, shows, squash, addressed_to, bounds, commit_amount, composer_field,
                              control_state, counterparty, deletes, element_text, is_on, is_search_field, label_head,
                              label_verbs, list_rows, message_key, name_match, plain, point_in, quoted,
                              receipt_patterns, recipient, row_name, row_of, same_control, screen_title, shown_count,
                              store_verbs, typed_query)


# ---------------------------------------------------------------- compile

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


def compile_contract(client, request, apps=(), timeout=60):
    """The request's Contract (one text-only call at reasoning "none"), or None if the call fails or
    yields no items: the agent then runs without one, under the regex guard (frontier.guard)."""
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": [
        {"type": "text", "text": f"Request: {request}\n\nApps: " + ", ".join(apps)}]}]
    # A client that takes reasoning per call is not touched: the first decision may be in flight on it.
    per_call = getattr(client, "per_call_reasoning", False) is True
    saved = None if per_call else getattr(client, "reasoning", None)
    if saved is not None:
        client.reasoning = "none"
    try:
        out, _ = (client.complete(messages, SCHEMA, timeout=timeout, reasoning="none") if per_call
                  else client.complete(messages, SCHEMA, timeout=timeout))
    except Exception:
        return None
    finally:
        if saved is not None:
            client.reasoning = saved
    contract = Contract.from_items((out or {}).get("items") or (), request)
    return contract if contract.items else None


# ---------------------------------------------------------------- the contract

def excludes_others(item):
    """A FORBID whose words rule out only the acts the request doesn't name (OTHERS): "Delete nothing else."."""
    return item.kind == "FORBID" and bool(OTHERS.search(f"{item.quote} {item.what}"))


class Contract:
    def __init__(self, items, request, dropped=()):
        self.items, self.request, self.dropped = list(items), request, list(dropped)
        self.screens = []           # (step, app, [(text, editable, role)], keyboard)
        self.typed = []             # {step, app, text, field, search}
        self.empty_searches = []    # (step, app, query)
        self.visited = set()
        self.pending = []
        self.money = []             # receipted money commits: (amount, names, step)
        self.events = []            # receipts and refusals, for the trace
        # Parallel to ``screens``: (step, app, title, {row: the row as shown}). The rows above are folded for
        # matching; the Mac app quotes proof as the screen showed it, in the place it showed it.
        self.shown = []
        # A long task's notebook (longform/notebook.py): its REPORT items are proven by what the notebook holds,
        # since the report is written from it. None for every other run.
        self.notebook = None
        # app -> the number of screens recorded when this run first wrote or committed there: a WRITE is proven only
        # by a screen recorded after that (a value already on screen before the run touched the app proves nothing).
        self.first_writes = {}
        # (step, app, rows): the rows of each screen that sit in a list (a Cell, or inside a Cell's frame). A saved
        # location, contact or alarm is a row of its app's list; a page's own title is not.
        self.listed = []

    @classmethod
    def from_items(cls, raw, request):
        """Validated items: kinds and acts known; a COMMIT whose quote is not verbatim in the request is
        dropped (the model cannot declare an act the user never asked for)."""
        items, dropped, asked = [], [], plain(request)
        for entry in list(raw)[:MAX_ITEMS]:
            if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
                continue
            text = {k: " ".join(str(entry.get(k) or "").split()) for k in
                    ("app", "what", "payload", "act", "count", "condition", "quote")}
            if not text["what"]:
                continue
            item = Item(id=len(items) + 1, kind=entry["kind"], **text)
            if item.kind in ("COMMIT", "FORBID") and item.act not in FAMILIES:
                dropped.append(item)
                continue
            if item.kind == "COMMIT" and not (plain(item.quote) and plain(item.quote) in asked):
                dropped.append(item)
                continue
            if item.kind != "COMMIT":
                item.count = ""
            items.append(item)
        for index, item in enumerate(items):
            item.id = index + 1
        return cls(items, request, dropped)

    # ------------------------------------------------ prompt

    def lines(self):
        """The contract as the model sees it: ``6. [open] COMMIT pay (SplitPay): pending requests [each]``."""
        out = []
        for item in self.items:
            if item.kind == "FORBID":
                mark = "[never]"
            elif item.kind == "REPORT":
                mark = {"found": f"[found: {item.value}]", "missing": "[missing]"}.get(item.status, "[ ]")
            elif item.status == "closed":
                mark = "[done: shown on screen]"
            elif item.receipts:
                mark = f"[{len(item.receipts)} done, open]"
            elif item.kind in ("READ", "WRITE") and self.proof(item)[0]:
                # Proven from the screens already seen: shown as such, or the model reopens the source "to verify"
                # (two turns, ~9.5 s, in a 9 Oct run: a READ the search row had already proven stayed "[ ]").
                mark = "[seen on screen]" if item.kind == "READ" else "[done: shown on screen]"
            else:
                mark = "[ ]"
            out.append(f"{item.id}. {mark} {item.label()}" + (f" -- {item.reason}" if item.reason and
                                                                 item.status not in ("closed",)
                                                                 and not mark.startswith(("[seen", "[done")) else ""))
        return out

    HEADER = ("Task contract (code enforces it: a commit it does not list is refused, and DONE is accepted only "
              "when the screens you saw show every item. In checklist_updates mark REPORT items found with the "
              "exact value as shown, or missing; mark other items done when you finish them. Sent messages, "
              "confirmations and saved text are checked on screen. A WRITE that was already so before you acted: mark "
              "it done with value \"already: \" and the exact row that shows it):")

    # ------------------------------------------------ the model's marks

    def update(self, updates, step):
        """Apply a turn's checklist_updates; returns the items that changed."""
        by_id, changed = {item.id: item for item in self.items}, []
        for update in updates or ():
            item = by_id.get(update.get("id")) if isinstance(update, dict) else None
            status = update.get("status") if isinstance(update, dict) else None
            if item is None or status not in ("done", "found", "missing"):
                continue
            value = " ".join(str(update.get("value") or "").split())[:300] or None
            if item.kind == "REPORT":
                if status == "found" and not value:
                    continue
                item.status = "found" if status in ("found", "done") and value else "missing"
                item.value, item.value_step = value, step
            else:
                item.claim = status
                if value and item.kind != "COMMIT":
                    item.value, item.value_step = value, step
                cited = ALREADY.match(value or "") if status == "done" and item.kind == "WRITE" else None
                if cited:
                    item.already = (step, plain(cited.group(1)), False)
                elif status == "done" and item.kind == "WRITE" and value:
                    item.already = (step, plain(value), True)  # a bare value counts only as a whole row
            changed.append(item)
        return changed

    # ------------------------------------------------ the ledger

    def _wrote(self, app):
        self.first_writes.setdefault(plain(app), len(self.screens))

    def record_action(self, app, operation, label):
        """A dispatched action: typing, a picker or a tap on a control that names an act is a write in ``app``."""
        if operation in WRITE_OPS or operation in ("TAP", "LONG_PRESS", "TAP_XY") and ACT_LABEL.search(label or ""):
            self._wrote(app)

    def record_typed(self, app, element, text, step):
        if text and str(text).strip() and not (element is not None and is_search_field(element)):
            self._wrote(app)
        if text and str(text).strip():
            self.typed.append({"step": step, "app": app, "text": str(text),
                               "field": getattr(element, "label", "") or "",
                               "search": bool(element is not None and is_search_field(element)),
                               "picker": getattr(element, "role", "") in ("PickerWheel", "Picker", "Slider")})

    def typed_texts(self, app=None):
        return [t["text"] for t in self.typed if app is None or t["app"] == app]

    def saw_lines(self, app, step, lines):
        """Rows read by a macro (READ_LIST) are seen too."""
        self.screens.append((step, app, [(plain(line), False, "StaticText") for line in lines], False))
        self.shown.append((step, app, None, {plain(line): " ".join(str(line).split()) for line in lines}))
        self.visited.add(app)

    def observe(self, snapshot, app, step):
        """Record a settled screen: the ledger, pending receipts, empty-search proofs."""
        elements = list(getattr(snapshot, "elements", ()) or ())
        rows = [(plain(element_text(e)), bool(getattr(e, "editable", False)), e.role) for e in elements]
        keyboard = getattr(snapshot, "keyboard", "") == "visible"
        originals = {}
        for element, (text, _, _) in zip(elements, rows):
            originals.setdefault(text, " ".join(element_text(element).split()))
        title = screen_title(snapshot)
        if not self.screens or self.screens[-1][1:] != (app, rows, keyboard):
            self.screens.append((step, app, rows, keyboard))
            self.shown.append((step, app, title, originals))
            listed = list_rows(elements, rows)
            if listed:
                self.listed.append((step, app, listed))
        self.visited.add(app)
        texts = frozenset(t for t, _, _ in rows)
        for pending in list(self.pending):
            receipt = self._receipt(pending, app, rows, texts, title, elements)
            if receipt:
                self._close(pending, receipt, step, originals, title)
        self._empty_search(elements, app, step)

    def _receipt(self, pending, app, rows, texts, title=None, elements=()):
        if app != pending.app:
            return None
        act = pending.item.act
        if act in STATE_ACTS:
            return self._state_receipt(pending, elements)
        if act in MESSAGE_ACTS and pending.payload:
            key = message_key(pending.payload)

            def composing(text, editable):
                # The composer still holding the text: unsent. Known by its label; else any field (as before).
                return editable and (not pending.composer or text.startswith(pending.composer))
            held = any(key and key in t for t, editable, _ in rows if composing(t, editable))
            if held:
                return None
            shown = [t for t, editable, _ in rows if key and key in t and not editable]
            if shown:
                return f"sent: {shown[-1][:120]}"
            # The bubble itself (a text view in iOS 26 Messages) with its delivery mark, in the thread where the
            # send was approved: "Delivered" under the exact text (O2, Kate Bell: sent, then "not confirmed").
            bubbles = [t for t, editable, _ in rows if key and key in t and not composing(t, editable)]
            same_thread = not pending.title or not title or plain(title) == plain(pending.title)
            if len(bubbles) > pending.hits and same_thread and any(DELIVERY.match(t) for t, _, _ in rows):
                return f"sent: {bubbles[-1][:120]}"
            return None
        new = [t for t in texts if t not in pending.before]
        patterns = receipt_patterns(act, pending.verbs)
        if patterns:
            hit = next((t for t in new if any(pattern.search(t) for pattern in patterns)), None)
            return f"shown: {hit[:120]}" if hit else None
        if act in MESSAGE_ACTS:
            return None  # a message without its typed text has no receipt to look for
        return "screen changed" if texts != pending.before else None

    @staticmethod
    def _state_receipt(pending, elements):
        """A like's or a follow's receipt: the control the agent tapped, where it was, now on (selected, its words for
        on, a value turned on) when it was not before, or the count on or beside it up by one. A change anywhere else
        (a banner closing, a page opening) is none."""
        before = pending.control
        if before is None:
            return None
        on = STATE_ON.get(pending.item.act)
        was_on = bool(on and (on.search(before.label) or on.search(before.value)))
        for e in same_control(before, elements):
            label, value = " ".join((e.label or "").split()), " ".join((getattr(e, "value", "") or "").split())
            if getattr(e, "selected", False) and not before.selected:
                return f"turned on: {label[:100] or e.role}"
            if on and not was_on and (on.search(label) or on.search(value)):
                return f"shown: {label[:100]}"
            if before.value.casefold() in OFF_VALUES and value.casefold() in ON_VALUES:
                return f"turned on: {label[:100] or e.role}"
        if before.count is not None:
            controls = same_control(before, elements)
            after = shown_count(controls[0] if controls else None, elements, before.rect)
            if after == before.count + 1:
                return f"count: {before.count} -> {after}"
        return None

    def _close(self, pending, receipt, step, originals=None, title=None):
        item = pending.item
        self.pending.remove(pending)
        item.receipts.append({"step": step, "label": pending.label, "receipt": receipt, "amount": pending.amount,
                              "payload": pending.payload})
        item.reason = ""
        if pending.amount is not None and item.act in ("pay", "transfer") and pending.names:
            self.money.append((pending.amount, pending.names, step))
        needed = item.needed()
        if needed is not None and len(item.receipts) >= needed:
            item.status = "closed"
        # For the Mac app's proof: the receipt's row as shown (the receipt itself is folded and cut), the
        # commit's exact text and the screen's title.
        row = receipt.split(": ", 1)[1] if receipt.startswith(("sent: ", "shown: ")) else None
        shown = next((original for text, original in (originals or {}).items() if row and text.startswith(row)), None)
        self.events.append({"event": "receipt", "item": item.id, "step": step, "receipt": receipt, "shown": shown,
                            "payload": pending.payload, "label": pending.label, "app": pending.app, "screen": title})

    def _empty_search(self, elements, app, step):
        typed = self.typed_texts(app)
        for field_ in elements:
            if not getattr(field_, "editable", False) or not is_search_field(field_) and not any(
                    t["search"] for t in self.typed if t["app"] == app):
                continue
            query = getattr(field_, "value", "") or ""
            if not query or not any(overlaps(query, t) for t in typed):
                continue
            below = [e for e in elements if e is not field_ and e.rect[1] > field_.rect[1] + field_.rect[3]
                     and e.rect[1] < .85 and e.role not in ("Key", "Keyboard") and not getattr(e, "editable", False)]
            if any(NO_RESULTS.search(element_text(e)) for e in elements) or not below:
                self.empty_searches.append((step, app, query))
                return

    # ------------------------------------------------ the commit gate

    def check(self, operation, element, snapshot, app, *, text=None, chain_typed=(), bind=True):
        """Gate one action. Not a commit: Gate(True). A commit: allowed only for an open declared item.
        ``chain_typed``: text typed earlier in this model call's chain. ``bind=False`` skips the message
        and object bindings (label-level measurement only)."""
        elements = list(getattr(snapshot, "elements", ()) or ())
        if element is None:
            return Gate(True)
        payload = None
        store = getattr(snapshot, "bundle_id", "") == APP_STORE
        picked = self._recipient_pick(operation, element, elements, app, text)
        if picked is not None:
            return picked
        if operation in ("TAP", "LONG_PRESS"):
            verbs = label_verbs(element.label, element.role)
            if not verbs and deletes(element.label, element.role, getattr(element, "rect", None)):
                verbs = {"delete"}  # a row's swipe action named Trash
            if store:
                verbs = (verbs - {"install", "buy", "purchase"}) | store_verbs(element.label) if store_verbs(
                    element.label) else verbs
            if not verbs and element.role in SWITCH_ROLES and COMMIT_WORDS.search(element.label or ""):
                return self._toggle(element, app)
        elif operation in ("SUBMIT", "TYPE_SUBMIT") and composer_field(element, elements, self.typed_texts()):
            verbs = {"send"}
            value = getattr(element, "value", "") or ""
            if operation == "TYPE_SUBMIT" and text:
                payload = text
            elif value and any(overlaps(value, t) for t in self.typed_texts()):
                payload = value
            else:  # the read shows the placeholder: the text typed just before is what Return sends
                payload = chain_typed[-1] if chain_typed else next(
                    (t["text"] for t in reversed(self.typed) if t["app"] == app and not t["search"]), None)
        else:
            return Gate(True)
        if not verbs:
            return Gate(True)
        verbs = frozenset(verbs)
        what = f"{'Return in' if operation in ('SUBMIT', 'TYPE_SUBMIT') else 'tapping'} {label_head(element.label) or element.label!r}"
        declared = [i for i in self.items if i.kind == "COMMIT" and self._app_ok(i, app) and verbs <= i.family()
                    and (i.act != "install" or store)]  # an app is installed from the App Store only
        for item in self.items:
            if item.kind == "FORBID" and self._app_ok(item, app) and verbs & item.family():
                if excludes_others(item) and any(i.status != "closed" for i in declared):
                    # "Delete nothing else": the deletes the request names go on, bound to their object and asked
                    # for; once those are done, or for any act the request doesn't name, this still refuses.
                    continue
                return self._refuse(verbs, f"Refused: {what} would {item.act} -- the request rules it out "
                                           f"(\"{item.quote}\").")
        # "save" also completes a requested write (an alarm's or a form's Save) in the write's app.
        if verbs == {"save"} and not [i for i in declared if i.status != "closed"] and any(
                i.kind == "WRITE" and self._app_ok(i, app) for i in self.items):
            return Gate(True, verbs=verbs)
        if not declared:
            return self._refuse(verbs, f"Refused: {what} performs an act the request does not ask for "
                                       f"({', '.join(sorted(verbs))}{' in ' + app if app else ''}). Choose an "
                                       "action that does what the user asked.")
        open_ = [i for i in declared if i.status != "closed"]
        if not open_:
            done = declared[0]
            return self._refuse(verbs, f"Refused: item {done.id} ({done.what}) is already done and shown on "
                                       "screen; doing it again would repeat or undo it.")
        amount = commit_amount(element, elements) if any(i.act in MONEY_ACTS for i in open_) else None
        if amount is None and any(i.act == "install" for i in open_):
            # An app's price is on its own button ("$4.99"); a Get button is free whatever else the page shows.
            price = MONEY.search(element.label or "")
            amount = as_float(price.group(1)) if price else None
        names = frozenset(counterparty(element, elements)) if amount is not None else frozenset()
        reasons = []
        for item in open_:
            if item.act in STATE_ACTS and is_on(item.act, element):
                # A heart already on: a tap would take the like back, which the request never asked for.
                reasons.append(f"{label_head(element.label) or element.role!r} already shows it on (selected, or its "
                               "words say so); tapping it would undo it")
                continue
            low, high = bounds(item)
            if amount is not None and (low is not None and amount < low or high is not None and amount > high):
                reasons.append(f"the amount ${amount:,.2f} is outside the request's bound (\"{item.quote}\")")
                continue
            if item.act in ("pay", "transfer") and amount is not None and names and any(
                    abs(a - amount) < .005 and names & n for a, n, _ in self.money):
                step = next(s for a, n, s in self.money if abs(a - amount) < .005 and names & n)
                reasons.append(f"${amount:,.2f} to {' '.join(sorted(names)).title()} was already paid (turn {step}); "
                               "a request can still show pending after a direct payment")
                continue
            if bind and item.act in MESSAGE_ACTS and verbs <= MESSAGE and self._made_address(item, elements):
                token, who = self._made_address(item, elements)
                reasons.append(f"the To field holds {token!r}, an address made of the name, not the contact {who!r}: "
                               f"remove it and pick {who!r} from the suggestions")
                continue
            if bind and item.act in MESSAGE_ACTS and verbs <= MESSAGE:
                bound = payload if operation == "TYPE_SUBMIT" and payload else \
                    self._composer_text(elements, chain_typed, payload)
                if bound is None:
                    reasons.append("no text you typed is in a field on this screen, so this would send or share "
                                   "something else (type the message in its composer first)")
                    continue
                payload = payload or bound
            if bind and item.act in OBJECT_ACTS and item.act != "install":
                row = row_of(element, elements)
                tokens = self.distinctive(item)
                if row is not None and tokens and not any(token in plain(element_text(row)) for token in tokens):
                    # A control in a row acts on that row: the row must name the object (a Follow or a Delete on
                    # the row above the named one would act on someone else while the card named the right one).
                    reasons.append(f"this {', '.join(sorted(verbs))} belongs to the row "
                                   f"{' '.join(element_text(row).split())[:60]!r}, not to {item.what!r}")
                    continue
            if (bind and item.act in OBJECT_ACTS and item.needed() == 1 and MORE_THAN_ONE.search(element.label or "")
                    and not MORE_THAN_ONE.search(f"{item.quote} {item.what}")):
                reasons.append(f"{label_head(element.label)!r} acts on more than the one thing the request names")
                continue
            if bind and item.act in OBJECT_ACTS and not self._object_on_screen(
                    item, elements, list(chain_typed) + self.typed_texts(app)):
                reasons.append(f"{item.what!r} is not on this screen, so this {', '.join(sorted(verbs))} is for "
                               "something else")
                continue
            if bind and item.act == "install" and not self._beside_object(item, element, elements):
                # A results list puts a Get button on every row (and an ad above them): it must be the named app's.
                reasons.append(f"this button is not beside {item.what!r}, so it would install something else")
                continue
            return Gate(True, item=item, verbs=verbs, amount=amount, names=names, payload=payload)
        return self._refuse(verbs, f"Refused: {what}: " + "; ".join(dict.fromkeys(reasons)) + ".")

    def screen_text_in(self, text, request=""):
        """A run of TAIL_CHARS or more of ``text`` that a screen of this run showed and the request doesn't hold, or
        None: text copied from the screen into a message (the taint check)."""
        wanted = plain(text)
        if len(wanted) < TAINT_CHARS:
            return None
        asked = plain(request or self.request)
        for _, _, _, originals in self.shown:
            for line in originals:
                if len(line) < TAINT_CHARS:
                    continue
                for start in range(0, max(1, len(wanted) - TAINT_CHARS + 1), 4):
                    piece = wanted[start:start + TAINT_CHARS]
                    if len(piece) == TAINT_CHARS and piece in line and piece not in asked:
                        return piece
        return None

    def _toggle(self, element, app):
        """A switch whose label names a thing, not an act ("9:30, Client call, Weekdays" is an alarm): it
        changes that thing, so it passes only when a WRITE or COMMIT of this app names it."""
        tokens = {t for t in re.findall(r"\d{1,2}:\d{2}|[a-z]{3,}", plain(element.label))} - {"the", "and", "for"}
        for item in self.items:
            if item.kind in ("WRITE", "COMMIT") and self._app_ok(item, app):
                named = set(re.findall(r"\d{1,2}:\d{2}|[a-z]{3,}", plain(" ".join((item.what, item.payload, item.quote)))))
                if len(tokens & named) >= 2 or any(":" in t for t in tokens & named):
                    return Gate(True, verbs=frozenset({"toggle"}))
        return self._refuse({"toggle"}, f"Refused: switching {element.label[:40]!r} changes something the request "
                                        "does not name. Choose an action that does what the user asked.")

    def _recipient_names(self, app):
        """The names the open message commits in ``app`` go to, as the request gave them ("Jordan Lee")."""
        return [who for who in (recipient(i) for i in self.items if i.kind == "COMMIT" and i.act in MESSAGE_ACTS
                                and i.status != "closed" and self._app_ok(i, app)) if who]

    def _recipient_pick(self, operation, element, elements, app, text=None):
        """A refusal while choosing who a message goes to, else None. MobsterBench Real, 12 Oct: the contact search
        showed nothing yet, Return in the To field made the typed name an address of its own ("benchtester"), and
        the message was about to go there. By names and roles only:
        - Return in a To field holding a typed name (not a phone number or an email) is refused: pick the row.
        - Among suggestions or results for a typed name, a row that isn't the request's name exactly is refused
          when another row is; with no exact row and two or more close ones, the agent asks instead of guessing."""
        names = self._recipient_names(app)
        if not names:
            return None
        if operation in ("SUBMIT", "TYPE_SUBMIT") and getattr(element, "editable", False) and TO_FIELD.match(
                element.label or ""):
            typed = " ".join(str(text).split()) if operation == "TYPE_SUBMIT" and text else typed_query(element)
            if typed and not ADDRESS.match(typed):
                who = max(names, key=lambda n: name_match(typed, n) is not None)
                return self._refuse({"address"}, f"Refused: Return in {element.label.strip()!r} would make {typed!r} "
                                                 f"an address of its own, not the contact {who!r}. TAP {who!r} in the "
                                                 "suggestions, or type the phone number or email the request gives.")
            return None
        if operation != "TAP" or getattr(element, "editable", False):
            return None
        queries = [typed_query(e) for e in elements if getattr(e, "editable", False)]
        for who in names:
            if not any(q and name_match(q, who) for q in queries):
                continue  # no field holds this name typed: not a pick among suggestions for it
            mine = name_match(row_name(element.label), who)
            if mine is None or mine == "exact":
                continue
            fields = [e for e in elements if getattr(e, "editable", False)]
            rows = [e for e in elements if e.role in ROW_ROLES and not getattr(e, "editable", False) and not any(
                point_in(f.rect, e.rect[0] + e.rect[2] / 2, e.rect[1] + e.rect[3] / 2) for f in fields)]
            exact = next((e for e in rows if name_match(row_name(e.label), who) == "exact"), None)
            if exact is not None:
                return self._refuse({"recipient"}, f"Refused: {row_name(element.label)!r} is not {who!r}; "
                                                   f"{row_name(exact.label)!r} is. TAP that row.")
            close = list(dict.fromkeys(row_name(e.label) for e in rows if name_match(row_name(e.label), who)))
            if len(close) >= 2:
                return self._refuse({"recipient"}, f"Refused: no row is exactly {who!r}, and "
                                                   f"{' and '.join(repr(c) for c in close[:3])} are all close. Ask the "
                                                   "person which one they mean (ASK_USER), or finish with BLOCKED; "
                                                   "don't guess.")
        return None

    def _made_address(self, item, elements):
        """(token, name) when the To field holds the request's name squashed into an address ("jordanlee" for "Jordan
        Lee"): Return made the typed name a recipient of its own. Else None."""
        who = recipient(item)
        to = addressed_to(SimpleNamespace(elements=elements)) if who else None
        for token in (to or "").split(", "):
            if token and plain(token) != plain(who) and " " not in token.strip() and " " in who.strip() \
                    and squash(token) == squash(who):
                return token, who
        return None

    def _refuse(self, verbs, reason):
        self.events.append({"event": "refused", "reason": reason[:200]})
        return Gate(False, reason=reason, verbs=frozenset(verbs))

    @staticmethod
    def _app_ok(item, app):
        return not item.app or not app or plain(item.app) == plain(app)

    def _composer_text(self, elements, chain_typed, payload):
        """Text the agent typed this task (or earlier in this chain) that an editable field on screen holds."""
        if chain_typed:
            return chain_typed[-1]
        if payload and any(overlaps(payload, t) for t in self.typed_texts()):
            return payload
        for element in elements:
            value = getattr(element, "value", "") or ""
            if getattr(element, "editable", False) and len(value.strip()) >= 2:
                for typed in reversed(self.typed_texts()):
                    if overlaps(value, typed):
                        return value
        return None

    @staticmethod
    def distinctive(item):
        """Tokens that name the commit's object: quoted names, else capitalized words of ``what``."""
        names = quoted(item.what)
        if names:
            return [plain(n) for n in names]
        words = re.findall(r"(?<![\w'])([A-Z][\w&.-]+)", item.what)
        skip = {plain(item.app)} | GENERIC
        return [plain(w) for w in words if plain(w) not in skip]

    def _object_on_screen(self, item, elements, typed=()):
        """The commit's object is named on screen, or is one the agent is making (a new alarm labeled
        'Gym' it typed the label of)."""
        tokens = self.distinctive(item)
        if not tokens:
            return True
        texts = [plain(element_text(e)) for e in elements] + [plain(t) for t in typed]
        return any(token in text for token in tokens for text in texts)

    def _beside_object(self, item, element, elements):
        """The control names its object itself, or is the App Store button nearest a row that names it (within
        an app row's height). Near is not enough: a compact list puts a Get on every row, .09 of the screen
        apart, and the row above's Get is as near the name as its own; the approval would name one app while
        the tap installed another. A tie is refused."""
        tokens = self.distinctive(item)
        if not tokens or any(token in plain(element.label) for token in tokens):
            return True
        buttons = [e for e in elements if e is not element and store_verbs(e.label)] + [element]

        def gap(button, name):
            return (round(abs(center_y(button) - center_y(name)), 3),
                    round(abs(button.rect[0] + button.rect[2] / 2 - name.rect[0] - name.rect[2] / 2), 3))
        for other in elements:
            if (other is element or other.rect[3] > .25 or abs(center_y(other) - center_y(element)) > .12
                    or not any(token in plain(element_text(other)) for token in tokens)):
                continue
            if min(buttons, key=lambda button: gap(button, other)) is element:
                return True
        return False

    def committed(self, gate, element, before, app, step):
        self._wrote(app)
        return self._committed(gate, element, before, app, step)

    def _committed(self, gate, element, before, app, step):
        """A declared commit ran: wait for its receipt on the screens that follow."""
        if gate.item is None:
            return
        gate.item.executions += 1
        elements = list(getattr(before, "elements", ()) or ())
        rows = frozenset(plain(element_text(e)) for e in elements)
        composer, hits = "", 0
        if gate.item.act in MESSAGE_ACTS and gate.payload:
            key = message_key(gate.payload)
            field_ = element if getattr(element, "editable", False) else next(
                (e for e in elements if getattr(e, "editable", False) and overlaps(getattr(e, "value", "") or "",
                                                                                   gate.payload)), None)
            composer = plain(field_.label) if field_ is not None and field_.label else ""
            hits = sum(1 for e in elements if key and key in plain(element_text(e))
                       and not (getattr(e, "editable", False) and (not composer
                                                                    or plain(element_text(e)).startswith(composer))))
        self.pending = [p for p in self.pending if p.item is not gate.item or gate.item.needed() is None
                        and p.payload != gate.payload]
        control = control_state(element, elements) if gate.item.act in STATE_ACTS else None
        self.pending.append(Pending(gate.item, (element.label or "")[:80], app, step, rows, gate.payload,
                                    gate.amount, gate.names, screen_title(before), composer, hits, gate.verbs,
                                    control))

    # ------------------------------------------------ receipts per item

    def _seen(self, predicate, app=None, until=None):
        for step, screen_app, rows, keyboard in self.screens:
            if until is not None and step > until:
                break
            if app and plain(screen_app) != plain(app):
                continue
            for text, editable, role in rows:
                if predicate(text, editable, keyboard):
                    return step
        return None

    def proof(self, item):
        """(proven, reason when not). FORBID is always proven."""
        if item.kind == "FORBID":
            return True, ""
        if item.kind == "COMMIT":
            if item.receipts:
                return True, ""
            state = item.act in STATE_ACTS
            if item.claim in ("missing", "done") and (item.condition or item.needed() is None) and (
                    not state or item.claim == "missing"):
                return True, ""  # a condition not met, or no object left for an "each"
            if item.executions and state:
                return False, ("tapped, but the control never showed it on afterwards (selected, \"Liked\" or "
                               "\"Following\", or its count up by one): look at that control again. Tapping it again "
                               "would undo it if it is on")
            if item.executions:
                return False, "done but not shown: open the screen that shows it (the sent message, the confirmation)"
            if state:
                # A like or a follow is proven only by its own control turning on after the agent tapped it: a tap on
                # a point, or a change elsewhere (a banner closing), leaves it open (MobsterBench Real, 11 Oct).
                return False, ("not shown done: a like or a follow counts once the control you TAP turns on (selected, "
                               "\"Liked\" or \"Following\", or its count up by one). If it already shows on, don't tap "
                               "it: that would undo it")
            if item.claim == "done" and item.act not in MESSAGE_ACTS and (
                    not item.app or any(plain(item.app) == plain(a) for a in self.visited)):
                # Its control named no commit word ("Add to Wishlist"), so no receipt was awaited: the
                # object must still be on a screen of its app. Not a message: its Send is always a commit
                # word, and a chat that already holds the same words from before proves nothing (27 Sep:
                # a declined draft's twin in the history was taken for the message sent, and nothing was).
                tokens = self.distinctive(item)
                if not tokens or self._seen(lambda t, e, k: any(x in t for x in tokens), item.app) is not None:
                    return True, ""
            return False, "not done yet"
        if item.kind == "WRITE":
            return self._write_proof(item)
        if item.kind == "READ":
            return self._read_proof(item)
        return self._report_proof(item)

    def _write_proof(self, item):
        typed = [t for t in self.typed if not t["search"] and not t.get("picker") and self._app_ok(item, t["app"])]
        # A message body is proven by the message's own receipt: the sent bubble may be a text view, which never
        # counts as shown while the keyboard is up (O2).
        texts = [item.payload] if item.payload else [t["text"] for t in typed]
        if any(other.kind == "COMMIT" and other.act in MESSAGE_ACTS and self._app_ok(item, other.app)
               and any(receipt.get("payload") and overlaps(receipt["payload"], text)
                       for receipt in other.receipts for text in texts if text) for other in self.items):
            return True, ""
        if item.already is not None:
            # The model quoted the row that shows it already so ("already: <row>", or a value that is a whole row):
            # proven when that row was on a screen of its app by then.
            step, row, whole = item.already
            if row and self._seen(lambda text, e, k: row == text or not whole and row in text, item.app,
                                  until=step) is not None:
                return True, ""
            if not whole and self._listed_already(item, step, row):
                return True, ""  # "already: ..." only: a bare value is what the model set, not a citation
        # Only screens recorded after this run first wrote or committed in the item's app prove it.
        apps = [plain(item.app)] if item.app else list(self.first_writes)
        firsts = [self.first_writes[a] for a in apps if a in self.first_writes]
        if not firsts:
            return False, ("nothing entered or set for it yet" + (f" in {item.app}" if item.app else "")
                           + (f": enter \"{item.payload}\"" if item.payload else ""))
        after = min(firsts)
        keys = []
        if item.payload:
            keys = [(plain(item.payload), min((t["step"] for t in typed if overlaps(item.payload, t["text"])),
                                              default=0))]
        else:
            keys = [(plain(next((line for line in t["text"].split("\n") if line.strip()), ""))[:40], t["step"])
                    for t in typed if len(t["text"].strip()) >= 3]
        if not keys:
            if item.claim != "done":
                return False, "nothing typed for it yet" if not item.payload else f"\"{item.payload}\" not entered yet"
            # Nothing typed for it: a write that adds (log, add, create) counts only once each object it names has a
            # new row in its app. A row from before proves nothing (iOSWorld dev, 12 Oct: runs logged a shake, took
            # the oatmeal row already in the log for the oatmeal, and ended done). A change of state or a picker keeps
            # the model's "done".
            missing = [] if not ADDS.search(item.what + " " + item.quote) or any(
                t.get("picker") and self._app_ok(item, t["app"]) for t in self.typed) else [
                words for words in self._objects(item) if not self._new_row(item, words, after)]
            return (False, f"no new row naming {' '.join(missing[0])} in {item.app or 'its app'} since you started "
                           "there (a row from before proves nothing): add it, then show it") if missing else (True, "")
        for key, since in keys:
            for index, (step, app, rows, keyboard) in enumerate(self.screens):
                if index < after or step < since or not self._app_ok(item, app):
                    continue
                if any(key and shows(key, text) and (not editable or not keyboard) for text, editable, _ in rows):
                    return True, ""
        return False, "not read back on screen after it was entered: save it and show the saved text"

    def _objects(self, item):
        """The objects a WRITE names, in the request's words, one list per part split at "and", "&" and commas:
        "protein shake and oatmeal breakfast log" -> [["post-run", "protein", "shake"], ["oatmeal", "breakfast"]]."""
        asked, app = plain(self.request), set(plain(item.app).split())
        parts = ([w for w in re.findall(r"[a-z][a-z'-]{3,}", part) if w in asked and w not in OBJECT_STOP | app]
                 for part in re.split(r"\s*(?:,|&|\band\b)\s*", plain(item.what)))
        return [words for words in parts if words]

    def _new_row(self, item, words, after):
        """More rows name one of ``words`` on a screen of the item's app from ``after`` on than on any before it. A
        screen holding a search the run typed shows what was looked for, not what exists."""
        searches = [plain(t["text"])[:20] for t in self.typed if t["search"]]
        counts = {False: 0, True: 0}
        for index, (_, app, rows, _) in enumerate(self.screens):
            if self._app_ok(item, app) and not any(e and any(q in text for q in searches) for text, e, _ in rows):
                count = max(sum(not e and w in text for text, e, _ in rows) for w in words)
                counts[index >= after] = max(counts[index >= after], count)
        return counts[True] > counts[False]

    def _listed_already(self, item, until, cited):
        """A WRITE the model marked as already so, citing its value but not a row: proven when that value is a whole
        row of a list in the item's app ("New York" in its saved locations), on a screen seen by then and before
        this run typed anything in that app (a list of search results shows what was looked for, not what exists).
        ab1, 11 Oct: "already: New York row in My Locations (59° Mostly Cloudy)" matched no row, though the list
        showed New York; a forecast page titled "New York" is no list row, and proves nothing."""
        value = plain(item.payload)
        if not value or value not in (cited or ""):
            return False
        for step, app, listed in self.listed:
            if step > until:
                break
            if not self._app_ok(item, app) or item.app and plain(app) != plain(item.app):
                continue
            if any(t["step"] <= step and plain(t["app"]) == plain(app) for t in self.typed):
                continue
            if any(text == value or text.startswith(value + ",") for text in listed):
                return True
        return False

    def _read_proof(self, item):
        if item.app and not any(plain(item.app) == plain(app) for app in self.visited):
            return False, f"{item.app} not opened yet"
        names = [plain(n) for n in quoted(item.what)]
        folder = next((f for f in FOLDERS if re.search(rf"\b{f}\b", plain(item.what))), None)
        if folder:
            names = [folder]
        for name in names:
            if self._seen(lambda text, e, k: (text == name or text.startswith(name + " ") or name in text
                                              and folder is None), item.app) is None:
                return False, f"'{name}' not seen in {item.app or 'any app'}"
        return True, ""

    def _report_proof(self, item):
        what = item.what + " " + item.quote
        if CONFIRM_ITEM.search(item.what):
            related = [i for i in self.items if i.kind in ("COMMIT", "WRITE") and (
                not item.app or not i.app or plain(i.app) == plain(item.app))]
            # "confirm the job was saved" is about the save, not the reply in the same app
            words = set(re.findall(r"[a-z]{4,}", plain(item.what))) - CONFIRM_WORDS
            named = [i for i in related if words & set(re.findall(r"[a-z]{4,}", plain(i.what + " " + i.act + " " + i.quote)))]
            related = named or related
            unproven = [i for i in related if not self.proof(i)[0] and not i.unproven]
            return (not unproven), ("" if not unproven else "its action is not shown yet: " +
                                    ", ".join(f"item {i.id}" for i in unproven))
        book = getattr(self, "notebook", None)
        if book is not None and item.status in ("open", "found") and getattr(book, "kind", None) in NOTEBOOK_KINDS:
            # A long read (feed, survey, browse): its summary item ("what you learned", "the most promising") is the
            # report, written from the kept items (DESIGN §11). Any other value is proven when an item's own text
            # holds it (never the model's notes), else on the screens as for any run. A chain keeps screen proof.
            if SUMMARY_ITEM.search(item.what + " " + item.quote) and book.counters.get("kept"):
                return True, ""
            if item.status == "found" and self._in_notebook(item.value):
                return True, ""
        if item.status == "open":
            return False, "not reported yet: read it on screen and mark it found with its value (or missing)"
        if item.status == "missing":
            if item.app and not any(plain(item.app) == plain(app) for app in self.visited):
                return False, f"marked missing but {item.app} was never opened"
            # An absence is a finding too: proven like a "none" (dev runs, 10-12 Oct: "Terminal not shown; gate E13"
            # after the boarding pass alone, while the trip's own page showed "Terminal 2, Gate E13").
            if self._absence_shown(item):
                return True, ""
            return False, ("marked missing, but no screen showed it absent: look where it would be (open the row "
                           "for its own page, another tab, the app's search) and mark it found, or leave on screen "
                           "the empty search or the app's \"No ...\" that proves it")
        value = item.value or ""
        if NONE_VALUE.match(plain(value)):
            proof = self._absence_shown(item)
            return (proof, "" if proof else "\"none\" needs proof: open the screen that shows it is empty "
                                            "(or search for it so the empty result is on screen)")
        if COMPUTED_ITEM.search(what):
            return True, ""
        until = item.value_step
        atoms = ATOM.findall(value)
        if atoms:
            missing = [a for a in atoms if self._seen_atom(a, until) is None]
            if not missing:
                return True, ""
            return False, (f"{', '.join(missing[:3])} not seen on any screen before you recorded it: read it where "
                           "it is shown")
        phrase = plain(value)
        words = re.findall(r"[a-z]{4,}", phrase)
        if len(phrase.split()) > 6 or not words:
            return True, ""  # prose the agent wrote from what it read: no single source to point at
        if self._seen(lambda t, e, k: phrase in t or all(w in t for w in words), None, until) is not None:
            return True, ""
        long_words = [w for w in words if len(w) >= 5] or words
        pool = " ".join(t for step, _, rows, _ in self.screens if until is None or step <= until for t, _, _ in rows)
        if sum(w in pool for w in long_words) >= max(1, len(long_words) / 2):
            return True, ""  # a name said in full ("Seattle Seahawks") where the screen shows part of it
        return False, f"\"{value[:60]}\" not seen on any screen before you recorded it: read it where it is shown"

    def _absence_shown(self, item):
        """Whether a screen of the item's app showed it absent by the time it was marked: a search the run typed that
        came back empty, or the app's own "No Results", "No Events", an empty list."""
        if any(self._app_ok(item, s[1]) for s in self.empty_searches):
            return True
        return self._seen(lambda t, e, k: bool(EMPTY_STATE.match(t) or NO_RESULTS.search(t)),
                          item.app or None, item.value_step) is not None

    def _in_notebook(self, value):
        """Whether the long task's kept items (what they showed: creator, summary, text) hold ``value``: its atoms,
        else its words. The model's own notes don't count: they are what it says, not what a screen showed."""
        book = getattr(self, "notebook", None)
        if book is None or not value:
            return False
        texts = []
        with book.lock:
            for entry in book.items:
                if entry.skip is not None:
                    continue
                texts += [entry.who(), entry.summary(), *(str(v) for v in (entry.text or {}).values()
                                                         if isinstance(v, str))]
        pool = plain(" ".join(t for t in texts if t))
        atoms = ATOM.findall(value)
        if atoms:
            return all(plain(atom) in pool or atom.lstrip("$") in pool for atom in atoms)
        words = re.findall(r"[a-z]{4,}", plain(value))
        return bool(words) and sum(w in pool for w in words) >= max(1, len(words) / 2)

    def _seen_atom(self, atom, until):
        if atom.startswith("$") or re.fullmatch(r"\d[\d,]*(?:\.\d+)?", atom):
            number = as_float(atom.lstrip("$").strip())
            if number is None:
                return None
            return self._seen(lambda t, e, k: any(abs((as_float(n) or -1e18) - number) < .005
                                                  for n in NUMBER.findall(t)), None, until)
        key = plain(atom)
        return self._seen(lambda t, e, k: key in t, None, until)

    # ------------------------------------------------ DONE

    def done_gate(self, answer, turns_left):
        """(accepted, feedback, answer). Every item needs its receipt; with LOW_TURNS or fewer turns
        left the answer is accepted and names what is unproven."""
        open_ = []
        for item in self.items:
            proven, reason = self.proof(item)
            if not proven and not item.unproven:
                open_.append((item, reason))
        if open_ and turns_left > LOW_TURNS:
            lines = []
            for item, reason in open_:
                item.reopens += 1
                item.reason = reason
                if item.reopens > MAX_REOPENS:
                    item.unproven = True
                lines.append(f"{item.id}. {item.label()} -- {reason}")
            if all(item.unproven for item, _ in open_):
                return True, "", self.final_answer(answer)
            return False, ("DONE refused: not shown on screen yet:\n" + "\n".join(lines) +
                           "\nDo these now (or mark a REPORT missing if it is shown nowhere), then DONE again."), None
        for item, reason in open_:
            item.unproven, item.reason = True, reason
        return True, "", self.final_answer(answer)

    def unproven(self):
        return [item for item in self.items if item.unproven and not self.proof(item)[0]]

    def final_answer(self, answer):
        """The answer, with found values it leaves out appended from the ledger, and unproven items named."""
        answer = (answer or "").strip()
        text = plain(answer)
        extra = []
        for item in self.items:
            if item.kind != "REPORT" or item.status != "found" or not item.value or CONFIRM_ITEM.search(item.what):
                continue
            atoms = ATOM.findall(item.value)
            present = all(plain(a) in text or a.lstrip("$") in text for a in atoms) if atoms else plain(item.value) in text
            if not present:
                extra.append(f"- {item.what}: {item.value}")
        # Actions the screen never confirmed are named (the user must know); a reported value stays as
        # given, since a doubt added to a right answer costs more than it tells.
        unproven = [item for item in self.unproven() if item.kind in ("COMMIT", "WRITE")]
        if unproven:
            extra.append("Not confirmed on screen: " + "; ".join(item.what for item in unproven) + ".")
        return (answer + ("\n\n" if answer and extra else "") + "\n".join(extra)).strip()

    def summary(self):
        return {"items": len(self.items), "dropped": len(self.dropped),
                "commits": sum(i.kind == "COMMIT" for i in self.items),
                "receipted": sum(bool(i.receipts) for i in self.items),
                "reports": sum(i.kind == "REPORT" for i in self.items),
                "unproven": [i.id for i in self.unproven()]}
