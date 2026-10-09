"""A scripted iPhone and a scripted Smart model for the end-to-end tests of the SOTA tracks (test_sota_e2e.py) and the
dashboard's live demo (dashboard/qa/live_demo.py): the real service, runtime, frontier loop, threads, memory,
attachments, steering, clarifying questions and approvals, with no phone and no model calls. This module holds no
tests.

The phone is a few fixed apps (Messages, Notes, Maps, Settings) whose screens change with the agent's actions. The
"model" reads the frontier's real prompt (the request, the screen's rows, the blocks the tracks add, steering messages,
tool feedback) and picks the next action by rules. Where a task depends on context (a follow-up, a memory, an
attachment), its rule succeeds only when that context is in the prompt, so a missing block shows up as BLOCKED, and a
send is claimed only after the agent tapped Send in this task. Names are examples only.
"""

import base64
import re
import threading
import time

from mobile_agent.drivers import Driver
from mobile_agent.state import Element, Snapshot

SOURCE = "synthetic_fixture"
HOME, MESSAGES, NOTES, MAPS, SETTINGS = ("com.apple.springboard", "com.apple.MobileSMS", "com.apple.mobilenotes",
                                         "com.apple.Maps", "com.apple.Preferences")
APP_NAMES = {MESSAGES: "Messages", NOTES: "Notes", MAPS: "Maps", SETTINGS: "Settings"}
CHATS = {"Alex Rivera": "Dinner Friday? Luca, 214 Pine St, 7:30",
         "Sam Lee": "Are you at the park yet?",
         "Sam Ortiz": "Heading to the store, need anything?",
         "Kate Bell": "Can you send me that recipe?"}
PLACES = {"luca": ("Luca, 214 Pine St", "Open until 10 PM"),
          "equinox": ("Equinox, 5th Street", "Open until 11 PM")}

# Seconds each read and action takes, times this (0 in tests; the live demo slows it so a person can follow).
PACE = {"value": 0.0}


def el(index, label, role, rect, **extra):
    return Element(f"n{index}", label, role, rect, **extra)


class ScriptedPhone(Driver):
    """One phone. ``state`` survives between runs of one service (a follow-up finds the phone where it was)."""

    can_type = True

    def __init__(self, state, frames=False):
        """``frames``: a stand-in for the phone's video stream, so each step keeps its screen (the live demo)."""
        self.state = state
        self.lock = threading.RLock()
        self.http = LockReader(state)  # what the phone guard reads (lockscreen.read_locked)
        self.frame_clock = ScriptedVideo(self) if frames else None

    @staticmethod
    def wait(seconds):
        if PACE["value"] > 0:
            time.sleep(seconds * PACE["value"])

    # -- screens -------------------------------------------------------------------------------------------------

    def screen(self):
        s = self.state
        app, stage = s["app"], s["stage"]
        rows = []
        if app == HOME:
            rows = [el(i, name, "Icon", (.06 + .22 * i, .12, .18, .09)) for i, name in enumerate(
                ("Messages", "Notes", "Maps", "Settings"))]
        elif app == MESSAGES and stage == "inbox":
            rows = [el(0, "Messages", "NavigationBar", (0, .05, 1, .08)),
                    el(1, "Search", "SearchField", (.04, .15, .92, .045), editable=True, actions=("TAP", "TYPE"))]
            rows += [el(2 + i, name, "Cell", (0, .22 + .1 * i, 1, .09)) for i, name in enumerate(CHATS)]
            rows += [el(10 + i, CHATS[name], "StaticText", (.2, .26 + .1 * i, .7, .03)) for i, name in enumerate(CHATS)]
        elif app == MESSAGES:
            contact = stage
            rows = [el(0, contact, "NavigationBar", (0, .05, 1, .08)), el(1, "Back", "Button", (0, .06, .15, .04)),
                    el(2, CHATS[contact], "StaticText", (.04, .2, .84, .05))]
            for i, text in enumerate(s["sent"].get(contact, ())):
                rows.append(el(3 + i, text, "StaticText", (.12, .28 + .07 * i, .84, .05)))
            if s["sent"].get(contact):
                rows.append(el(20, "Delivered", "StaticText", (.75, .28 + .07 * len(s["sent"][contact]), .2, .03)))
            typed = s["typed"]
            rows.append(el(21, "iMessage", "TextField", (.12, .9, .72, .045), editable=True, value=typed,
                           actions=("TAP", "TYPE")))
            if typed:
                rows.append(el(22, "Send", "Button", (.86, .9, .1, .045)))
        elif app == NOTES and stage == "list":
            rows = [el(0, "Notes", "NavigationBar", (0, .05, 1, .08))]
            rows += [el(1 + i, title, "Cell", (0, .18 + .08 * i, 1, .07)) for i, title in enumerate(s["notes"])]
            rows.append(el(30, "New Note", "Button", (.85, .92, .1, .05)))
        elif app == NOTES:
            rows = [el(0, "Notes", "Button", (0, .06, .2, .04)), el(1, "Done", "Button", (.82, .06, .15, .04)),
                    el(2, "Note", "TextView", (.04, .14, .92, .7), editable=True, value=s["typed"],
                       actions=("TAP", "TYPE"))]
        elif app == MAPS and stage == "search":
            rows = [el(0, "Search Maps", "SearchField", (.04, .82, .92, .05), editable=True, actions=("TAP", "TYPE"))]
        elif app == MAPS:
            place, hours = PLACES[stage]
            rows = [el(0, s["typed"] or "Search Maps", "SearchField", (.04, .1, .92, .05), editable=True,
                       actions=("TAP", "TYPE")),
                    el(1, place, "Cell", (0, .2, 1, .08)), el(2, hours, "StaticText", (.05, .29, .6, .03))]
        elif app == SETTINGS and stage == "top":
            rows = [el(0, "Settings", "NavigationBar", (0, .05, 1, .08))]
            rows += [el(1 + i, name, "Cell", (0, .18 + .07 * i, 1, .06)) for i, name in enumerate(
                ("Wi-Fi", "Bluetooth", "General", "Display & Brightness"))]
        elif app == SETTINGS and stage == "general":
            rows = [el(0, "General", "NavigationBar", (0, .05, 1, .08)), el(1, "About", "Cell", (0, .18, 1, .06)),
                    el(2, "Software Update", "Cell", (0, .25, 1, .06))]
        elif app == SETTINGS:
            rows = [el(0, "About", "NavigationBar", (0, .05, 1, .08)), el(1, "iOS Version", "StaticText", (.05, .2, .4, .04)),
                    el(2, "26.4", "StaticText", (.7, .2, .25, .04)), el(3, "Model Name", "StaticText", (.05, .26, .4, .04)),
                    el(4, "iPhone 17 Pro", "StaticText", (.6, .26, .35, .04))]
        text = "\n".join(e.label for e in rows)
        return Snapshot(rows, text, 390, 844, SOURCE, bundle_id=app, revision=f"{app}-{stage}-{s['revision']}")

    def observe(self, timeout=10):
        self.wait(.5)
        with self.lock:
            return self.screen()

    def active_app(self, *args, **kwargs):
        return self.state["app"]

    # -- actions -------------------------------------------------------------------------------------------------

    def execute(self, operation, target, snapshot, text=None, timeout=10):
        self.wait(.6)
        s = self.state
        label = target.label if isinstance(target, Element) else (target or "")
        with self.lock:
            s["revision"] += 1
            s["log"].append((operation, label, text))
            if operation == "LAUNCH_APP":
                bundle = str(target)
                s["app"], s["typed"] = bundle, ""
                s["stage"] = {MESSAGES: "inbox", NOTES: "list", MAPS: "search", SETTINGS: "top"}.get(bundle, "home")
                return
            if operation == "HOME":
                s["app"], s["stage"], s["typed"] = HOME, "home", ""
                return
            app, stage = s["app"], s["stage"]
            gate = s.get("gate")
            if gate and operation == gate["op"] and label == gate["label"] and not gate["reached"].is_set():
                # A test holds the task here (inside one action) while it sends a message, so the message arrives
                # mid-task at a known point whatever the machine's speed.
                gate["reached"].set()
                self.lock.release()
                try:
                    gate["release"].wait(10)
                finally:
                    self.lock.acquire()
            if app == HOME and operation == "TAP":
                bundle = {v: k for k, v in APP_NAMES.items()}.get(label)
                if bundle:
                    self.state["stage"] = "x"
                    return self.execute("LAUNCH_APP", bundle, snapshot)
            if app == MESSAGES:
                if stage == "inbox" and operation == "TAP" and label in CHATS:
                    s["stage"], s["typed"] = label, ""
                elif stage in CHATS and operation == "TAP" and label == "Back":
                    s["stage"], s["typed"] = "inbox", ""
                elif stage in CHATS and operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT") and label == "iMessage":
                    s["typed"] = (text or "") if operation == "SET_TEXT" else s["typed"] + (text or "")
                elif stage in CHATS and operation == "TAP" and label == "Send" and s["typed"]:
                    s["sent"].setdefault(stage, []).append(s["typed"])
                    s["typed"] = ""
            elif app == NOTES:
                if stage == "list" and operation == "TAP" and label == "New Note":
                    s["stage"], s["typed"] = "editor", ""
                elif stage == "editor" and operation in ("TYPE", "TYPE_SUBMIT", "SET_TEXT") and label == "Note":
                    s["typed"] = (text or "") if operation == "SET_TEXT" else s["typed"] + (text or "")
                elif stage == "editor" and operation == "TAP" and label in ("Done", "Notes"):
                    if s["typed"]:
                        s["notes"].insert(0, s["typed"].splitlines()[0][:60])
                    s["stage"], s["typed"] = "list", ""
            elif app == MAPS:
                if operation in ("TYPE_SUBMIT", "TYPE", "SET_TEXT") and "Search" in label or label == s["typed"]:
                    query = (text or "").casefold()
                    hit = next((key for key in PLACES if key in query), None)
                    s["typed"] = text or ""
                    if hit and operation == "TYPE_SUBMIT":
                        s["stage"] = hit
            elif app == SETTINGS:
                if stage == "top" and operation == "TAP" and label == "General":
                    s["stage"] = "general"
                elif stage == "general" and operation == "TAP" and label == "About":
                    s["stage"] = "about"

    def tap_point(self, x, y, snapshot, timeout=10, purpose=None):
        self.wait(.3)

    def clear_text(self, target, timeout=10):
        self.state["typed"] = ""

    def capture_preview(self, timeout=3):
        """A picture of the current screen (sota_screens: drawn like iOS), for the live view and run frames."""
        from mobile_agent.tests import sota_screens
        with self.lock:
            snap = self.screen()
        return "data:image/png;base64," + base64.b64encode(sota_screens.render(snap)).decode()

    def close(self):
        pass


class ScriptedVideo:
    """What frontier.video_frame reads from a real phone's FrameClock: the newest JPEG of the screen, which never
    reads as still (so nothing skips a read on its say-so)."""

    def __init__(self, phone):
        self.phone, self.sequence = phone, 0
        self.video = self

    def still_for(self, exact=True):
        return 0.0

    def latest(self):
        from mobile_agent.tests import sota_screens
        with self.phone.lock:
            snap = self.phone.screen()
        self.sequence += 1
        return (self.sequence, "image/jpeg", sota_screens.jpeg(snap), int(time.time() * 1000), time.monotonic(),
                "scripted")


def new_phone_state():
    return {"app": HOME, "stage": "home", "typed": "", "sent": {}, "notes": ["Groceries", "Trip ideas"],
            "revision": 0, "log": [], "gate": None, "locked": False}


class LockReader:
    """WDA's sessionless ``GET /wda/locked`` for the scripted phone: ``state["locked"]``, which a test (or the live demo's
    --locked) flips the way a person unlocks a phone. Every other request is refused: the guard only reads."""

    def __init__(self, state):
        self.state = state

    def request(self, method, path, body=None, timeout=None):
        if method == "GET" and path == "/wda/locked":
            return {"value": bool(self.state.get("locked"))}
        raise OSError(f"the scripted phone takes no {method} {path}")


def preflight(runtime, run, driver):
    """The real phone guard's preflight on the scripted phone (server.prepare_wda_phone without its Home press): a
    locked phone stops the task, or waits for the unlock when a person started it (Look at your iPhone)."""
    from mobile_agent.agent_hooks import safe_check
    from mobile_agent.server import PhoneNotReady
    run.guard = runtime.make_guard(run, None)
    verdict = safe_check(run.guard, driver, "preflight")
    if verdict.state == "stop":
        raise PhoneNotReady(verdict.message, verdict.code or "phone_locked")


def hold_at(state, op, label):
    """Hold the next ``op`` on ``label`` until the returned gate's ``release`` is set; ``reached`` says it's held."""
    gate = {"op": op, "label": label, "reached": threading.Event(), "release": threading.Event()}
    state["gate"] = gate
    return gate


# -- the scripted model ---------------------------------------------------------------------------------------------

ROW = re.compile(r'^(e\d+|n\d+) (\S+) "((?:[^"\\]|\\.)*)"', re.M)


class Turn:
    """One step prompt, parsed."""

    def __init__(self, prompt, operations):
        self.prompt = prompt
        self.operations = set(operations or ())
        match = re.search(r"^Request: (.*)$", prompt, re.M)
        self.request = match.group(1).strip() if match else ""
        # "In Messages: text Sam …" when the task names its start app: the rules read the words after it.
        self.r = re.sub(r"^in [^:]{1,40}: ", "", self.request.casefold())
        match = re.search(r"^Current app: (.*)$", prompt, re.M)
        self.app = match.group(1).strip() if match else ""
        screen = prompt.split("Screen elements:\n", 1)[1] if "Screen elements:\n" in prompt else ""
        self.rows = [(m.group(1), m.group(2), m.group(3)) for m in ROW.finditer(screen)]
        # Everything but the request line: the tracks' blocks, steering, feedback.
        self.context = prompt.replace(self.request, "", 1) if self.request else prompt

    def in_app(self, bundle):
        return self.app in (bundle, APP_NAMES.get(bundle))

    def find(self, label, role=None):
        return next((rid for rid, r, lab in self.rows if lab == label and (role is None or r == role)), None)

    def has(self, label):
        return any(lab == label for _, _, lab in self.rows)

    def labels(self):
        return [lab for _, _, lab in self.rows]


def act(op, target=None, text=None):
    return {"op": op, "target": target, "text": text}


class ScriptedModel:
    """The Smart model's stand-in for one run: ``complete`` as frontier.OpenAIChat's, no network."""

    model = "scripted-qa-demo"
    per_call_reasoning = False

    def __init__(self, log=None):
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}
        self.model_failed = False
        self.reasoning = "low"
        self.log = log
        self.connection = None

    def complete(self, messages, schema, timeout=60, reasoning=None, **kwargs):
        from mobile_agent.frontier import prompt_text
        if "items" in schema.get("properties", {}):
            item = schema["properties"]["items"].get("items", {}).get("properties", {})
            if "kind" in item:  # the task contract (contract.compile_contract)
                return {"items": contract_items(prompt_text(messages))}, {}
            return {"items": []}, {}
        self.usage["calls"] += 1
        prompt = prompt_text(messages)
        try:
            operations = schema["properties"]["actions"]["items"]["properties"]["op"]["enum"]
        except (KeyError, TypeError):
            operations = ()
        turn = Turn(prompt, operations)
        actions, answer, notes = decide(turn)
        if self.log is not None:
            self.log.append({"request": turn.request, "app": turn.app, "actions": actions, "answer": answer,
                             "prompt": prompt})
        updates = []
        for words, _app, what in REPORTS:
            if all(word in turn.r for word in words):
                if turn.has(FOUND[what]):
                    updates = [{"id": 1, "status": "found", "value": FOUND[what]}]
                break
        if "note" in turn.r and any(a.get("op") == "DONE" for a in actions):
            updates = [{"id": 1, "status": "done", "value": None}]
        out = {"thought": "", "plan": None, "notes_add": notes, "checklist_updates": updates, "actions": actions,
               "answer": answer}
        if "data_json" in schema.get("properties", {}):
            out["data_json"] = None
        return out, {}


def contract_items(prompt):
    """A small contract for the request, as a model would write it: what to read, and the send it asks for."""
    match = re.search(r"^Request: (.*)$", prompt, re.M)
    request = match.group(1).strip() if match else ""
    request = re.sub(r"^In [^:]{1,40}: ", "", request)
    r = request.casefold()
    blank = {"payload": "", "act": "none", "count": "", "condition": "", "quote": ""}
    if r.startswith(("text ", "message ", "tell ")):
        quote = " ".join(request.split()[:2])
        return [{**blank, "kind": "COMMIT", "app": "Messages", "what": "send the message", "act": "send_message",
                 "quote": quote}]
    for words, app, what in REPORTS:
        if all(word in r for word in words):
            return [{**blank, "kind": "REPORT", "app": app, "what": what}]
    if "note" in r:
        return [{**blank, "kind": "WRITE", "app": "Notes", "what": "a new note with the address",
                 "payload": "Luca, 214 Pine St"}]
    return [{**blank, "kind": "READ", "app": "", "what": request[:120]}]


# (words in the request, app, what to report) -> the screen value that proves it (FOUND).
REPORTS = ((("ios version",), "Settings", "the iOS version"),
           (("address", "alex"), "Messages", "the dinner place's address"),
           (("gym", "close"), "Maps", "when the gym closes"),
           (("close",), "Maps", "when the place closes"))
FOUND = {"the iOS version": "26.4", "the dinner place's address": "Dinner Friday? Luca, 214 Pine St, 7:30",
         "when the gym closes": "Open until 11 PM", "when the place closes": "Open until 10 PM"}


def launch(bundle):
    return act("LAUNCH_APP", bundle)


def done(answer, notes=()):
    return [act("DONE")], answer, list(notes)


def blocked(why):
    return [act("BLOCKED", None, why)], why, []


def in_messages_with(t, contact):
    """Actions to reach ``contact``'s conversation, or None when there."""
    if not t.in_app(MESSAGES):
        return [launch(MESSAGES)]
    if t.has("Messages") and t.find(contact):
        return [act("TAP", t.find(contact))]
    if not t.find(contact, "NavigationBar"):
        back = t.find("Back")
        return [act("TAP", back)] if back else [launch(MESSAGES)]
    return None


def decide(t):
    """(actions, answer, notes) for one turn."""
    r = t.r
    # A clarifying answer, a steering message: both arrive in the prompt outside the request line.
    answered = re.search(r"The user answered(?: your question)?: '?([^'\n]+)", t.context)

    # 1. "What's the address of the dinner place Alex sent me?"
    if "alex" in r and ("address" in r or "where" in r) and "text" not in r:
        steps = in_messages_with(t, "Alex Rivera")
        if steps:
            return steps, None, []
        return done("Alex suggested dinner at Luca, 214 Pine St, on Friday at 7:30.",
                    ["Alex: dinner at Luca, 214 Pine St, Friday 7:30"])

    # 2. Hours: of "that place" (needs the conversation) or "my gym" (needs memory).
    if "close" in r or "open until" in r or "hours" in r:
        if "gym" in r:
            key = "equinox" if re.search(r"equinox", t.context, re.I) else None
            if key is None:
                return blocked("Which gym do you mean? Tell me its name and I'll look it up.")
        elif "luca" in r:
            key = "luca"
        else:
            key = "luca" if re.search(r"214 Pine St", t.context) else None
            if key is None:
                return blocked("Which place do you mean? I don't know which one you're asking about.")
        place, hours = PLACES[key]
        if not t.in_app(MAPS):
            return [launch(MAPS)], None, []
        if t.has(place):
            return done(f"{place} is {hours.lower()} tonight.")
        field = t.find("Search Maps")
        query = "Luca 214 Pine St" if key == "luca" else "Equinox 5th Street"
        return [act("TYPE_SUBMIT", field, query)], None, []

    # 3. Sending a message: "Text Alex that I'm running late", "Text Sam I'm here".
    if r.startswith(("text ", "message ", "tell ")):
        if "sam" in r:
            if answered:
                contact = "Sam Ortiz" if "ortiz" in answered.group(1).casefold() else "Sam Lee"
            elif "ASK_USER" in t.operations:
                return [act("ASK_USER", None, "Which Sam do you mean? | Sam Lee | Sam Ortiz")], None, []
            else:
                return blocked("You have two contacts named Sam (Sam Lee and Sam Ortiz). Which one?")
        else:
            contact = "Alex Rivera"
        minutes = "15" if re.search(r"15 minutes", t.context) else "10"
        message = (f"Running {minutes} minutes late, sorry!" if "late" in r else
                   "I'm here, by the entrance" if "here" in r else "On my way")
        steps = in_messages_with(t, contact)
        if steps:
            return steps, None, []
        recent = t.prompt.split("Recent actions", 1)[-1].split("Screen elements:", 1)[0]
        if t.has(message) and t.has("Delivered") and re.search(r"TAP\b[^\n]*Send", recent):
            return done(f"Sent “{message}” to {contact}.")
        field = t.find("iMessage", "TextField")
        current = next((lab for _, role, lab in t.rows if role == "TextField"), "")
        typed_row = re.search(r'TextField "iMessage"[^\n]*value[:=]\s*"?([^"\n]+)', t.prompt)
        if t.has("Send"):
            if typed_row and message not in typed_row.group(1):
                return [act("SET_TEXT", field, message)], None, []
            return [act("TAP", t.find("Send"))], None, []
        return [act("TYPE", field, message)], None, []

    # 4. "Save that address in a new note"
    if "note" in r:
        if re.search(r"214 Pine St", t.context):
            body = "Luca, 214 Pine St (dinner Friday 7:30)"
        else:
            return blocked("Which address do you mean?")
        if not t.in_app(NOTES):
            return [launch(NOTES)], None, []
        if t.has("New Note"):
            if any(lab.startswith("Luca, 214 Pine St") for lab in t.labels()):
                return done("Saved a new note: “Luca, 214 Pine St (dinner Friday 7:30)”.")
            return [act("TAP", t.find("New Note"))], None, []
        note = t.find("Note", "TextView")
        if note and "Luca" not in t.prompt.split("Screen elements:", 1)[1]:
            return [act("TYPE", note, body)], None, []
        return [act("TAP", t.find("Done"))], None, []

    # 5. An attachment: "Which dishes on this menu are vegetarian?"
    if "menu" in r or "vegetarian" in r or "attached" in r:
        if re.search(r"Mushroom risotto", t.context):
            return done("The vegetarian dishes are the Mushroom risotto, the Eggplant parmigiana and the "
                        "Charred broccolini.")
        if "READ_ATTACHMENT" in t.operations and "READ_ATTACHMENT" not in t.context.split("Recent actions", 1)[-1]:
            return [act("READ_ATTACHMENT", None, "dinner-menu.txt")], None, []
        return blocked("I can't see the menu you mean. Attach it and ask again.")

    # 6. Settings, for a check: "What iOS version is this iPhone on?"
    if "ios version" in r or "software version" in r:
        if not t.in_app(SETTINGS):
            return [launch(SETTINGS)], None, []
        if t.has("26.4"):
            return done("This iPhone is on iOS 26.4.")
        if t.has("About"):
            return [act("TAP", t.find("About"))], None, []
        if t.has("General"):
            return [act("TAP", t.find("General"))], None, []
        return [launch(SETTINGS)], None, []

    # 7. "Remember ..." and anything else: nothing to do on the phone.
    if r.startswith("remember") or "remember" in r:
        return done("Okay. Mobster asks before it remembers anything.")
    return done("Done.")
