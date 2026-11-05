"""The dashboard's event stream from a Smart run: SmartEvents turns the loop's trace into the contract events the
app renders (docs/engine-contract.md), with the readable step text. Nothing here changes a decision the loop makes."""

import re
import threading

from . import harness_api
from .task_policy import ROW_STATE

# FrontierAgent events kept out of the run's history: the full prompt and screenshot of every turn is
# for offline replay (bench/replay.py), 20-60 KB each; the loop's own result is replaced by the server's.
DROPPED = frozenset({"frontier_prompt", "result"})
TOOL_BLOCKS = 6
TYPING = frozenset({"TYPE", "TYPE_SUBMIT", "SET_TEXT"})
# A press on a named control: the steps whose `commit` the engine can vouch for (frontier.safety.consequential).
PRESSES = frozenset({"TAP", "LONG_PRESS"})


SYMBOLS = {"circle": "the selection circle", "ellipsis": "More", "ellipsis.circle": "More", "checkmark": "Done",
           "trash": "Delete", "trash.fill": "Delete", "square.and.pencil": "Compose"}
def readable(label):
    label = ROW_STATE.sub("", " ".join(str(label or "").split()))
    if label in SYMBOLS:
        return SYMBOLS[label]
    if label and " " not in label and re.fullmatch(r"[A-Za-z0-9_.-]+", label) and re.search(r"[_.]|[a-z][A-Z]", label):
        label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", label).replace("_", " ").replace(".", " ").replace("-", " ")
        label = " ".join(label.split()).lower()
    return label


def shorten(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def in_quotes(text, limit=40):
    return f"“{shorten(text, limit)}”"


def written_kind(label):
    """What a composer holds, by its label: the comment, the reply, the post or (else) the message."""
    folded = readable(label).casefold()
    for word, kind in (("comment", "comment"), ("reply", "reply"), ("caption", "post"), ("tweet", "post"),
                       ("post", "post"), ("on your mind", "post")):
        if word in folded:
            return kind
    return "message"


def step_text(op, label, apps=None, *, text=None, field=None, where=None, again=False):
    label = readable(label) if op not in ("LAUNCH_APP",) else " ".join(str(label or "").split())
    short = shorten(label, 60)
    if op == "LAUNCH_APP":
        return f"Opened {(apps or {}).get(label, label) or 'an app'}"
    if op in ("DRAG_XY", "HOLD_XY", "FLICK_XY"):
        verb = {"DRAG_XY": "Dragged", "HOLD_XY": "Held", "FLICK_XY": "Flicked"}[op]
        return f"{verb} {short or 'the screen'}"
    if op == "TAP_XY":
        return f"Tapped {re.sub(r' at [0-9]+,[0-9]+$', '', short) or 'the screen'}"
    if op in TYPING and text is not None:
        submit = " and pressed Return" if op == "TYPE_SUBMIT" else ""
        if op == "SET_TEXT" and not str(text).strip():
            return f"Cleared the {written_kind(label)}" if field == "composer" else f"Cleared {short}" if short \
                else "Cleared the field"
        if field == "search":
            return f"Searched for {in_quotes(text)}"
        if field == "composer":
            return f"{'Rewrote' if again else 'Wrote'} the {written_kind(label)}{submit}"
        place = (f" in {short}"
                 if short and len(short) <= 30 and short.casefold() != " ".join(str(text).split()).casefold() else "")
        return f"Typed {in_quotes(text)}{place}{submit}"
    if op == "SCROLL_TO" and text:
        return f"Scrolled to {shorten(text, 60)}"
    if op == "READ_LIST" and where:
        return f"Read the {shorten(where, 40)} list"
    return {
        "TAP": f"Tapped {short}" if short else "Tapped the screen",
        "LONG_PRESS": f"Pressed and held {short}",
        "TYPE": f"Typed in {short}" if short else "Typed text",
        "TYPE_SUBMIT": f"Typed in {short} and pressed Return" if short else "Typed text and pressed Return",
        "SET_TEXT": f"Entered text in {short}" if short else "Entered text",
        "SUBMIT": "Pressed Return",
        "SWIPE_UP": "Scrolled down", "SWIPE_DOWN": "Scrolled up",
        "SWIPE_LEFT": f"Swiped left on {short}" if short else "Swiped left",
        "SWIPE_RIGHT": f"Swiped right on {short}" if short else "Swiped right",
        "DISMISS": "Closed a pop-up", "HOME": "Went to the Home Screen",
        "READ_LIST": "Read the whole list", "SCROLL_TO": "Scrolled to the item",
        "ASK_USER": "Asked you a question", "OPEN_URL": "Opened a link", "WATCH_FEED": "Watched the feed",
        "WATCH": "Watched the screen",
        "READ_EACH": "Read each item", "RECALL": "Looked back through earlier results",
    }.get(op, op.replace("_", " ").capitalize())


def connector_step(call):
    """A CALL_TOOL step in plain words, from its last connector_call event: "Used Linear: create_issue"."""
    if not isinstance(call, dict) or not call.get("connector"):
        return "Didn't use a connected service"
    if call.get("status") == "searched":
        return f"Looked for a connected tool: {shorten(str(call.get('arguments') or ''), 60)}"
    name = " ".join(str(call["connector"]).split())
    name = shorten(name, 40) + (f": {shorten(str(call['tool']), 60)}" if call.get("tool") else "")
    return {"unknown": f"{name} didn't answer", "not_sent": f"Couldn't use {name}", "refused": f"Didn't use {name}",
            "error": f"{name} returned an error"}.get(call.get("status"), f"Used {name}")


def item_state(contract, item):
    """open | done | unproven, as the dashboard shows a contract item."""
    if item.kind == "FORBID":
        return "done"
    if item.unproven and not contract.proof(item)[0]:
        return "unproven"
    if item.kind == "REPORT":
        return "done" if item.status in ("found", "missing") else "open"
    if item.kind == "COMMIT":
        return "done" if item.status == "closed" or contract.proof(item)[0] else "open"
    return "done" if contract.proof(item)[0] else "open"


ITEM_ACTS = {
    "send_message": "Send the message", "reply": "Send the reply", "post": "Post it", "comment": "Post the comment",
    "react": "React", "pay": "Pay", "request_money": "Request money", "transfer": "Make the transfer",
    "order": "Place the order", "book": "Make the booking", "follow": "Follow", "unfollow": "Unfollow",
    "share": "Share", "call": "Call", "delete": "Delete", "cancel": "Cancel", "save": "Save", "archive": "Archive",
    "install": "Install"}


def item_text(item):
    from . import contract_screen as KS
    what = " ".join(str(item.what or "").split())
    where = f" in {item.app}" if item.app and not what.casefold().endswith(f" in {item.app}".casefold()) else ""
    if item.kind == "COMMIT":
        who = KS.recipient(item)
        verb = ITEM_ACTS.get(item.act)
        if verb is None:
            text = what[:1].upper() + what[1:]
        elif item.act in ("send_message", "reply", "transfer"):
            text = verb + (f" to {who}" if who else "")
        elif item.act in ("pay", "follow", "unfollow", "call"):
            text = f"{verb} {who or what}"
        elif item.act == "request_money":
            text = verb + (f" from {who}" if who else "")
        elif item.act in ("order", "book", "post", "comment", "react"):
            text = verb if item.act != "react" else f"React to {what}"
        else:
            text = f"{verb} {what}" if what else verb
        if item.condition:
            text += f" if {item.condition}"
        return text + where
    if item.kind == "WRITE":
        return (f"Enter {in_quotes(item.payload, 60)}" if item.payload else f"Write {what}") + where
    if item.kind == "READ":
        return f"Look at {what}" + where
    if item.kind == "REPORT":
        return f"Find {what}" + where
    verb = ITEM_ACTS.get(item.act)
    return f"Don't {verb.split()[0].lower()} {what}" if verb else f"Don't {what}"


class SmartEvents:

    def __init__(self, emit, driver=None, apps=None, frame=None, ledger=None):
        self.out, self.driver, self.apps = emit, driver, dict(apps or {})
        # A long task's SpendLedger: its cost events report the run's whole spend (every metered call), not only
        # the navigation client's.
        self.ledger = ledger
        self.agent = None
        self.frame = frame or self._video_frame
        self.steps = 0
        self.planned = False
        self.states = {}
        self.receipts_seen = 0
        self.reports = {}
        self.proof = {}
        self.write_keys = {}
        self.stale = set()       # WRITE items whose quoted text was typed over and not read back again
        self.typed_at = {}       # index in contract.typed -> the first contract.screens index after it
        self.held = None
        self.ops = []
        self.written = set()
        self.declined = set()    # texts the user declined at an approval (folded): never proof
        self.cost_usd = None
        self.watched = None
        # A tool's own words for its next step (``tool_step`` {op, text}: "Opened your Instagram DMs"), by op.
        self.tool_steps = {}
        # Result cards tools built from what they read (``result_blocks`` {op, blocks}), newest last.
        self.tool_blocks = []
        self.lock = threading.Lock()

    def _video_frame(self):
        from .frontier.screen import video_frame
        return video_frame(self.driver) if self.driver is not None else None

    def _grab(self):
        try:
            data = self.frame()
        except Exception:
            data = None
        return bytes(data) if isinstance(data, (bytes, bytearray)) and data else None

    def _with_frame(self, event, data=None):
        data = data if data is not None else self._grab()
        if data:
            event["_frame"] = data
        return event

    def _private(self):
        """Where the last action's screen shows fields and messages, as frames (never their text); None when unknown."""
        screen = getattr(self.agent, "last_screen", None)
        if screen is None:
            return None
        private_rects = harness_api.hooks().private_rects
        try:
            return private_rects(screen.elements, getattr(screen, "bundle_id", None))
        except Exception:
            return None

    @property
    def contract(self):
        return getattr(self.agent, "_contract", None)

    def __call__(self, event):
        kind = event.get("event")
        with self.lock:
            # WATCH's timeline is kept once, in its step (below), not in the event as well.
            media = event.pop("media", None) if kind == "watched" else None
            if kind not in DROPPED:
                self.out(event)
            if kind in ("frontier_contract", "frontier_checklist") and not self.planned:
                self._plan()
            elif kind == "frontier_decision":
                self.ops = list(event.get("ops") or ())
                held = self.held
                if held is not None and not (event.get("operation") in TYPING
                                             and event.get("target_label") == held["label"]):
                    self._flush()
                self._cost()
                self._thought(event)
            elif kind == "tool_step" and isinstance(event.get("op"), str) and isinstance(event.get("text"), str):
                self.tool_steps[event["op"]] = shorten(" ".join(event["text"].split()), 120)
            elif kind == "result_blocks" and isinstance(event.get("blocks"), list):
                self.tool_blocks = [*self.tool_blocks, *event["blocks"]][-TOOL_BLOCKS:]
            elif kind == "watched" and event.get("ok"):
                # After out(): the server put the contact sheet beside the run and left its id here.
                self.watched = {"text": event.get("text"), "frameId": event.get("frameId"), "media": media}
            elif kind == "connector_call":
                self.connector_call = event
                if event.get("status") == "done" and event.get("excerpt"):
                    # What a connected service answered is proof as a screen is: quoted, with where it came from.
                    self.tool_proof = [*getattr(self, "tool_proof", [])[-2:], {
                        "quote": shorten(str(event["excerpt"]), 300), "app": event.get("connector"),
                        "screen": event.get("tool"), "frameId": None}]
            elif kind == "frontier_action":
                self._action(event)
            elif kind == "system_dialog_shown":
                self.dialog_frame = self._grab()  # the prompt itself, before anything is pressed
            elif kind == "system_dialog" and event.get("ok") and event.get("button"):
                # iOS's own prompt, answered: a step in the thread, with the frame that shows the prompt.
                from .wda.system_dialogs import thread_text
                self._flush()
                frame, self.dialog_frame = getattr(self, "dialog_frame", None), None
                self._step(thread_text(event.get("kind"), event.get("app"), event["button"], event.get("by")),
                           event.get("step"), frame)
            if kind in ("frontier_decision", "frontier_action", "frontier_contract_update", "frontier_failed",
                        "frontier_chunk_stop"):
                self._receipts(event.get("step"))

    def _thought(self, event):
        """The turn's ``thought``: the model's own sentence, secrets masked, shown and never sent to a model again."""
        if event.get("source") == "program":
            return
        from .secret_filter import redact
        text = event.get("thought")
        text = " ".join(redact(text).split()) if isinstance(text, str) else ""
        # The model names elements by the short ids it was shown ("e32"); a person reads "on screen".
        text = re.sub(r"\s*\((?:e\d{1,4}(?:,\s*)?)+\)", "", text)
        text = re.sub(r"\b(?:(?:in|at|on) )?(?:the )?(?:element|button|field|row) e\d{1,4}\b", "on screen", text)
        text = re.sub(r"\be\d{1,4}\b", "it", text)[:300]
        ms = event.get("latency_ms")
        self.out({"event": "thought", "step": event.get("step"), "text": text or None,
                  "ms": round(ms) if isinstance(ms, (int, float)) and ms >= 0 else None})

    def inference(self, event):
        """Where ``meter`` sends its events (the same stream)."""
        with self.lock:
            self.out(event)

    def approving(self, approve):
        """``approve`` for FrontierAgent: a held typing step is shown before the user is asked."""
        def ask(request):
            from . import contract_screen as KS
            with self.lock:
                self._flush()
            answer = approve(request)
            if answer != "approved" and request.get("text"):
                with self.lock:
                    self.declined.add(KS.plain(request["text"]))
            return answer
        return ask

    def _step(self, text, step, data=None, private=None, focus=None, commit=None, changed=None):
        self.steps += 1
        event = self._with_frame({"event": "step", "n": self.steps, "step": step, "text": text}, data)
        # Left out when unknown, which the app reads as "may commit" and "may not have changed".
        if isinstance(commit, bool):
            event["commit"] = commit
        if isinstance(changed, bool):
            event["changed"] = changed
        if focus and "_frame" in event:
            event["focus"] = focus  # where the element it acted on sits in that frame, as fractions: data only
        if "_frame" in event:
            private = private if private is not None else self._private()
            if private is not None:
                event["redact"] = private
        self.out(event)

    def _action(self, event):
        op, label = event.get("operation"), event.get("target_label")
        contract = self.contract
        if op in TYPING and contract is not None and contract.typed:
            self.typed_at.setdefault(len(contract.typed) - 1, max(0, len(contract.screens) - 1))
        if op in TYPING:
            if self.held is not None and self.held["label"] == label:
                self.held.update(op=op, text=event.get("text"), step=event.get("step"), frame=self._grab(),
                                 private=self._private(), field=event.get("field") or self.held["field"],
                                 focus=event.get("element_rect"), changed=self.held["changed"] or event.get("changed"))
            else:
                self._flush()
                self.held = {"op": op, "label": label, "text": event.get("text"), "field": event.get("field"),
                             "step": event.get("step"), "frame": self._grab(), "private": self._private(),
                             "focus": event.get("element_rect"), "changed": event.get("changed")}
            index = event.get("index")
            following = self.ops[index + 1] if isinstance(index, int) and index + 1 < len(self.ops) else None
            if following is not None and following not in TYPING:
                self._flush()  # the chain goes on to something else: nothing to merge
            return
        self._flush()
        if op == "WATCH" and self.watched is not None:
            # The step shows the frames it kept (one picture) and keeps the timeline it read as evidence.
            watched, self.watched = self.watched, None
            self.steps += 1
            step = {"event": "step", "n": self.steps, "step": event.get("step"),
                    "text": watched.get("text") or step_text(op, label, self.apps)}
            if watched.get("frameId"):
                step["frameId"], step["sheet"] = watched["frameId"], True
            if isinstance(watched.get("media"), dict):
                step["media"] = watched["media"]
            self.out(step)
            return
        if op == "CALL_TOOL":
            # No phone picture: the step happened at a connected service, not on the screen.
            self._step(connector_step(getattr(self, "connector_call", None)), event.get("step"), data=b"")
            self.connector_call = None
            return
        said = self.tool_steps.pop(op, None)
        if said:
            # A tool said what its step did ("Read 10 DMs in Instagram"): that is the step's line.
            self._step(said, event.get("step"))
            return
        where = None
        if op == "READ_LIST":
            shown = contract.shown[-1] if contract is not None and contract.shown else None
            where = (shown[2] if shown and shown[2] else None) or event.get("app_name")
        self._step(step_text(op, label, self.apps, text=event.get("text"), where=where), event.get("step"),
                   focus=event.get("element_rect"), commit=event.get("consequential") if op in PRESSES else None,
                   changed=event.get("changed"))

    def _flush(self):
        held, self.held = self.held, None
        if held is None:
            return
        again = held["field"] == "composer" and held["label"] in self.written
        if held["field"] == "composer":
            self.written.add(held["label"])
        self._step(step_text(held["op"], held["label"], self.apps, text=held["text"], field=held["field"],
                             again=again), held["step"], held["frame"], held.get("private"), held.get("focus"),
                   changed=held.get("changed"))

    def _plan(self):
        from . import contract_asks as proof_asks
        self.planned = True
        contract = self.contract
        apps = []
        for item in (contract.items if contract else ()):
            if item.app and item.app not in apps:
                apps.append(item.app)
        self.out({"event": "plan", "line": proof_asks.plan_line(contract), "apps": apps,
                  "commits": proof_asks.plan_commits(contract)})
        for item in (contract.items if contract else ()):
            state = item_state(contract, item)
            self.states[item.id] = state
            self.out({"event": "contract_item", "id": item.id, "kind": item.kind.lower(), "label": item.label(),
                      "text": item_text(item), "state": state})

    def _cost(self):
        from .model_clients import cost_usd
        client = getattr(self.agent, "client", None)
        usage = getattr(client, "usage", None)
        if not usage:
            return
        usd = cost_usd(getattr(client, "model", ""), usage)
        if self.ledger is not None:
            from .costs import NANODOLLARS
            usd = round(self.ledger.cost_nanodollars / NANODOLLARS, 6)
        self.cost_usd = usd
        self.out({"event": "cost", "usd": usd, "calls": usage.get("calls", 0)})

    def _receipts(self, step):
        from . import contract_screen as KS
        contract = self.contract
        if contract is None:
            return
        by_id = {item.id: item for item in contract.items}
        fresh = contract.events[self.receipts_seen:]
        self.receipts_seen = len(contract.events)
        for entry in fresh:
            item = by_id.get(entry.get("item"))
            if entry.get("event") != "receipt" or item is None:
                continue
            self._receipt(item, self._commit_quote(contract, item, entry), entry.get("step", step),
                          screen=entry.get("screen"))
        for item in contract.items:
            if (item.kind == "REPORT" and item.status == "found" and item.value
                    and self.reports.get(item.id) != item.value):
                self.reports[item.id] = item.value
                row, screen = self._report_row(contract, item)
                label = None
                if row and KS.plain(row).endswith(KS.plain(item.value)) and len(row) > len(item.value):
                    label = row[:len(row) - len(item.value)].strip(" :,-–—")
                    # "iOS Version, 26.4" is how VoiceOver reads the row; the screen shows "iOS Version  26.4"
                    row = f"{label} {row[len(row) - len(item.value):]}" if label else row
                self._flush()
                taken = item.value_step if item.value_step is not None else step
                data = self._grab()
                self._step(f"Read {shorten(label or item.what, 40)}: {shorten(item.value, 60)}", taken, data)
                self._receipt(item, row or item.value, taken, screen=screen, data=data)
        self._writes(contract)
        self._states()

    def _commit_quote(self, contract, item, entry):
        from . import contract_screen as KS
        payload = " ".join(str(entry.get("payload") or "").split())
        if item.act in KS.MESSAGE_ACTS and payload:
            return payload
        if entry.get("shown"):
            return entry["shown"]
        receipt = str(entry.get("receipt") or "")
        if receipt == "screen changed":
            last = contract.shown[-1][3] if contract.shown else {}
            for typed in reversed(contract.typed):
                if typed["search"] or typed.get("picker") or not contract._app_ok(item, typed["app"]):
                    continue
                key = KS.plain(next((line for line in typed["text"].split("\n") if line.strip()), ""))[:40]
                if key and any(KS.shows(key, text) for text in last):
                    return " ".join(typed["text"].split())
            return item.payload or item.what or (item.receipts[-1]["label"] if item.receipts else "")
        return re.sub(r"^(sent|shown): ", "", receipt)

    @staticmethod
    def _report_row(contract, item):
        from . import contract_screen as KS
        value = KS.plain(item.value)
        if not value:
            return None, None
        # The value as a whole word: "on" is not in "chevron.forward" (B6, bench/state.bluetooth), and an SF
        # Symbol's name is never a quote.
        whole = re.compile(r"(?<![\w])" + re.escape(value) + r"(?![\w])")
        for (step, _, _, _), (_, _, title, originals) in reversed(list(zip(contract.screens, contract.shown))):
            if item.value_step is not None and step > item.value_step:
                continue
            for text, original in originals.items():
                if whole.search(text) and len(original) <= 80 and not KS.SF_SYMBOL.fullmatch(original.strip()):
                    return original, title
        return None, None

    def _writes(self, contract):
        """WRITE receipts: the latest text typed, once shown outside its field; a compose-box draft proves nothing."""
        from . import contract_screen as KS
        for item in contract.items:
            if item.kind != "WRITE":
                continue
            typed = [(index, t) for index, t in enumerate(contract.typed) if not t["search"] and not t.get("picker")
                     and contract._app_ok(item, t["app"])
                     and (not item.payload or KS.overlaps(item.payload, t["text"]))]
            if not typed:
                continue
            index, latest = typed[-1]
            key = KS.plain(next((line for line in latest["text"].split("\n") if line.strip()), ""))[:40]
            if not key:
                continue
            if self.write_keys.get(item.id) == key:
                self.stale.discard(item.id)
                continue
            found = self._read_back(contract, item, latest, key, self.typed_at.get(index))
            if found is None:
                if item.id in self.write_keys:
                    self.stale.add(item.id)
                continue
            quote, screen = found
            self.write_keys[item.id] = key
            self.stale.discard(item.id)
            self._receipt(item, quote, latest["step"], screen=screen)

    @staticmethod
    def _read_back(contract, item, typed, key, since):
        from . import contract_screen as KS
        field = KS.plain(readable(typed.get("field") or ""))
        composer = bool(KS.COMPOSER.search(typed.get("field") or "") or KS.COMPOSER.search(field))

        def showing(rows, keyboard):
            return [text for text, editable, _ in rows if key in text
                    and not (editable and (keyboard or composer or field and text.startswith(field + " ")))]

        pairs = list(zip(contract.screens, contract.shown))
        first = since if since is not None else 0
        before = pairs[first - 1] if first > 0 else None
        baseline = len(showing(before[0][2], before[0][3])) if before and before[0][1] == (
            pairs[first][0][1] if first < len(pairs) else None) else 0
        for position in range(len(pairs) - 1, first - 1, -1):
            (step, app, rows, keyboard), (_, _, title, originals) = pairs[position]
            if since is None and step < typed["step"] or not contract._app_ok(item, app):
                continue
            shown = showing(rows, keyboard)
            if len(shown) <= baseline:
                continue
            original = originals.get(shown[-1]) or typed["text"]
            same = KS.plain(original) == KS.plain(typed["text"])
            return (original if same else " ".join(typed["text"].split())), title
        return None

    def _receipt(self, item, quote, step, *, screen=None, data=None):
        quote = shorten(quote, 300)
        if not quote:
            return
        event = self._with_frame({"event": "receipt", "itemId": item.id, "kind": item.kind.lower(), "quote": quote,
                                  "app": item.app or None, "screen": screen or None, "step": step}, data)
        self.out(event)
        self.proof.pop(item.id, None)  # the latest receipt for an item replaces the one before
        self.proof[item.id] = {"itemId": item.id, "kind": item.kind, "quote": quote, "app": event["app"],
                               "screen": event["screen"], "frameId": event.get("frameId"),
                               **self._commit_fields(item)}

    @staticmethod
    def _commit_fields(item):
        from . import contract_screen as KS
        if item.kind != "COMMIT" or not item.act or item.act == "none":
            return {}
        who = KS.recipient(item)
        return {"act": item.act, **({"to": who} if who else {})}

    def _states(self):
        contract = self.contract
        for item in (contract.items if contract else ()):
            state = item_state(contract, item)
            if self.states.get(item.id) != state:
                self.states[item.id] = state
                self.out({"event": "contract_item", "id": item.id, "kind": item.kind.lower(), "label": item.label(),
                          "text": item_text(item), "state": state})

    def final_proof(self):
        from . import contract_screen as KS
        entries = [e for e in self.proof.values() if e["quote"]]
        commits = [e for e in entries if e["kind"] == "COMMIT"]
        kept, quotes = [], set()
        for entry in entries:
            if KS.plain(entry["quote"]) in self.declined and entry["kind"] != "COMMIT":
                continue  # a draft the user turned down
            if entry["kind"] == "WRITE" and (entry["itemId"] in self.stale or any(
                    (not c["app"] or not entry["app"] or KS.plain(c["app"]) == KS.plain(entry["app"]))
                    and KS.overlaps(c["quote"], entry["quote"]) for c in commits)):
                continue
            folded = KS.plain(entry["quote"])
            if folded in quotes:
                continue
            quotes.add(folded)
            kept.append({"quote": entry["quote"], "app": entry["app"], "screen": entry["screen"],
                         "frameId": entry.get("frameId"),
                         **{key: entry[key] for key in ("act", "to") if entry.get(key)}})
        for entry in getattr(self, "tool_proof", ()):
            if KS.plain(entry["quote"]) not in quotes:
                quotes.add(KS.plain(entry["quote"]))
                kept.append(dict(entry))
        return kept[-12:]

    def finish(self):
        """Final item states (the DONE gate may have marked some unproven) and the last held step."""
        with self.lock:
            self._receipts(None)
            self._flush()
