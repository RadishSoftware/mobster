"""The iPhone tools an outside agent gets over MCP while it runs a Mobster task (ai_plans)."""

import base64
import json
import math
import re
import threading
import time
from types import SimpleNamespace

from .. import offers
from ..mcp_server.protocol import ACTS, READ_ONLY, ToolResult
from ..wda.point_gestures import DRAG_SCHEMA, FLICK_SCHEMA, GESTURE_OPS, HOLD_SCHEMA, validate_gesture_request

NAMES = ("screen", "act", "tap", "tap_point", "drag_point", "hold_point", "flick_point", "type_text", "swipe",
         "long_press", "open_app", "home", "read_list", "scroll_to",
         "dismiss", "open_url", "ask_user", "wait")
CHAINABLE = ("tap", "type_text", "swipe", "long_press", "open_app", "home", "scroll_to", "dismiss", "wait")
MAX_CHAIN = 8
COMMIT_OPS = frozenset({"TAP", "LONG_PRESS", "SUBMIT", "TYPE_SUBMIT"})
SWIPES = {"up": "SWIPE_UP", "down": "SWIPE_DOWN", "left": "SWIPE_LEFT", "right": "SWIPE_RIGHT"}
ELEMENT_ID = re.compile(r"e[0-9]{1,4}")
WAIT_MAX = 10.0
QUESTION_CHARS, CHOICE_CHARS = 300, 120
LIST_ROWS_SHOWN = 80
# Facts a screen can show word for word: amount, version, time, percentage, code, number.
FACT = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d+(?:[.:]\d+)+\b|\b\d+(?:\.\d+)?%|\b[A-Z]{2,}-?\d{3,}\b|\b\d[\d,]*\b")
PROOF_ITEMS = 12


def step_name(step):
    """A chain step as the agent wrote it: the tool and what it names. Never the text a step types."""
    tool = step.get("tool") if isinstance(step, dict) else None
    if not isinstance(tool, str) or not tool:
        return "a step with no tool"  # an unrun step is never checked, so it can be anything the agent sent
    keys = ("label", "element", "app", "direction") + (("text",) if tool == "scroll_to" else ())
    target = next((step[k] for k in keys if isinstance(step.get(k), str) and step[k]), None)
    return f"{tool} {json.dumps(target)}" if target else str(tool)


class ToolProblem(Exception):
    """A tool error: one plain sentence telling the agent what to do next (the result's isError)."""


class RunStopped(ToolProblem):
    """The person stopped the task, or the phone guard ended it: no more actions."""


def frame_bytes(driver):
    """The screen as JPEG bytes for the step list."""
    from ..frontier.screen import video_frame
    data = video_frame(driver)
    if data is None:
        capture = getattr(driver, "capture_preview", None)
        if callable(capture) and not getattr(driver, "ax_only_steps", 0):
            try:
                data = capture(timeout=5)
            except Exception:
                data = None
    if isinstance(data, str):
        try:
            data = base64.b64decode(data.split(",", 1)[1] if data.startswith("data:") else data)
        except (ValueError, TypeError):
            return None
    return bytes(data) if isinstance(data, (bytes, bytearray)) and data else None


def has_text(element):
    """Whether a field holds text of its own (not only its placeholder)."""
    value = " ".join(str(element.value or "").split())
    return bool(value) and value not in (" ".join(str(element.placeholder or "").split()),
                                         " ".join(str(element.label or "").split()))


class PhoneTools:
    """The tools of one run."""

    def __init__(self, driver, *, apps, goal, emit, approve=None, ask=None, cancelled=None, guard=None,
                 build=None, remembered=None, permissions):
        from .. import engines
        self.driver, self.apps, self.goal = driver, dict(apps or {}), goal or ""
        self.out = emit or (lambda event: None)
        self.approve, self.ask = approve, ask
        self.permissions = permissions
        # the person's "Don't ask again" list (permissions.exceptions.Exceptions.contains), or None
        self.remembered = remembered
        self.cancelled = cancelled or (lambda: False)
        build = build or engines.build_frontier
        kwargs = {"guard": guard} if guard is not None else {}
        # No model: only the frontier's reads, actuation, macros, masking and guard are used.
        self.agent = build(driver, None, apps=self.apps, emit=self._trace, cancelled=self.cancelled, **kwargs)
        self.lock = threading.RLock()
        self.snapshot, self.aliases = None, {}
        self.point_image = None  # the screenshot the agent last saw: a point it names belongs to this picture
        self.steps = 0
        self.actions = 0
        self.stopped = None   # the GuardVerdict that ended the run, once one did
        self.typed = None     # (app, text) last typed into a composer: what a Send tap sends, for its card
        # Every screen shown to the agent, as the contract records them,
        # and how many actions may have changed something.
        from ..contract import Contract
        self.ledger = Contract([], self.goal)
        self.changes = 0
        self.approved_send = None  # an approved Return that may not have sent: its Send tap isn't asked again
        self.tuned = False

    def proof(self, answer):
        """The result's proof (smart_events.final_proof's shape) for a run that changed nothing."""
        from ..contract_screen import plain, quoted
        if self.changes or not answer:
            return []
        facts = list(dict.fromkeys([plain(fact) for fact in FACT.findall(answer)] +
                                   [plain(name) for name in quoted(answer)]))
        if not facts:
            return []
        proof, rows_used = [], set()
        for fact in facts:
            pattern = re.compile(r"(?<![\w.:$])" + re.escape(fact) + r"(?![\w]|[.:]\d)")
            found = next(((step, app, title, row, original)
                          for step, app, title, originals in reversed(self.ledger.shown)
                          for row, original in originals.items() if pattern.search(row)), None)
            if found is None:
                return []
            step, app, title, row, original = found
            if (step, row) not in rows_used:
                rows_used.add((step, row))
                proof.append({"quote": original, "app": app, "screen": title or app, "step": step})
        return proof[:PROOF_ITEMS]

    @staticmethod
    def definitions():
        return [{"name": name, "description": DESCRIPTIONS[name], "inputSchema": SCHEMAS[name],
                 "annotations": ANNOTATIONS[name]} for name in NAMES]

    def call(self, name, arguments):
        """Run tool ``name``. Returns a ToolResult; a problem is one too, with is_error set."""
        if name == "act":
            return self._act_call(arguments if isinstance(arguments, dict) else {})
        handler = getattr(self, f"tool_{name}", None) if name in NAMES else None
        if handler is None:
            return ToolResult(f"Mobster has no tool named {str(name)[:60]}.", is_error=True)
        arguments = arguments if isinstance(arguments, dict) else {}
        from ..frontier import GuardStop
        try:
            with self.lock:
                self._check_running()
                return handler(arguments)
        except GuardStop as error:  # a BaseException: past the handler below
            self.stopped = error.verdict
            return ToolResult(error.verdict.message or "The phone stopped the task.", is_error=True)
        except ToolProblem as problem:
            return ToolResult(str(problem), is_error=True)
        except Exception as error:  # noqa: BLE001 -- a tool never takes the run down
            return ToolResult(f"That didn't work ({type(error).__name__}: {str(error)[:160]}). Call screen to "
                              "see the phone now.", is_error=True)

    def _check_running(self):
        if self.stopped is not None:
            raise RunStopped(self.stopped.message or "The task has ended.")
        if self.cancelled():
            raise RunStopped("The person stopped this task. Stop now and say nothing more.")

    def _act_call(self, arguments):
        """act through call's rules: a step that fails ends the chain with what was done before it."""
        done = []
        steps = arguments.get("steps") if isinstance(arguments.get("steps"), list) else []
        if not steps:
            return ToolResult("steps is a list of actions, such as [{\"tool\": \"tap\", \"label\": \"Alex\"}].",
                              is_error=True)
        last, unrun = None, []
        chain = steps[:MAX_CHAIN]
        for index, step in enumerate(chain):
            name = step.get("tool") if isinstance(step, dict) else None
            if name not in CHAINABLE:
                last = ToolResult(f"Step {index + 1}: tool is one of {', '.join(CHAINABLE)}.", is_error=True)
                break
            last = self.call(name, {k: v for k, v in step.items() if k != "tool" and v is not None})
            if last.is_error:
                break
            done.append(last.text.split("\n", 1)[0])
            if last.no_effect:
                unrun = chain[index + 1:]
                break
        if last is None:
            return ToolResult("Nothing to do.", is_error=True)
        if last.is_error:
            before = ("Done first: " + " ".join(done) + "\n") if done else ""
            failed = len(done) + 1
            return ToolResult(f"{before}Step {failed} didn't work: {last.text}", last.structured, last.images,
                              is_error=True)
        rest = last.text.split("\n\n", 1)[1] if "\n\n" in last.text else last.text
        stopped = (f" Step {len(done)} showed no effect ({last.no_effect}), so these didn't run: "
                   + ", ".join(step_name(step) for step in unrun) + ". Look at the screen before you send them again."
                   if unrun else "")
        return ToolResult("Done: " + " ".join(done) + stopped + "\n\n" + rest, last.structured, last.images,
                          no_effect=last.no_effect)

    def _trace(self, event):
        """The frontier's own trace events: the phone guard's stops are kept; the rest is noise here."""
        if isinstance(event, dict) and event.get("event") == "phone_guard":
            self.out(event)

    def _read(self):
        if not self.tuned:
            self.tuned = True
            self.agent._tune()
        self.agent._guard_check("resume")
        snapshot = self.agent._observe()
        self.agent._check_app(snapshot)
        return snapshot

    def _show(self, snapshot, image=False, head=""):
        """The screen as text the agent reads (ids e1 ...), remembered for the next action."""
        from ..frontier import screen_rows
        masked = self.agent._masked(snapshot)
        self.snapshot = snapshot
        self.aliases, lines = screen_rows(masked, self.agent.run_state.receipts)
        app = self.agent._app_name(snapshot) or "Home Screen"
        if snapshot.bundle_id == "com.apple.springboard":
            app = "Home Screen"
        self.ledger.observe(masked, app, self.steps)
        text = (head + "\n\n" if head else "") + f"Screen: {app}\n" + ("\n".join(lines) or "(nothing readable)")
        images = []
        self.point_image = None
        if image:
            part = self.agent._screenshot(snapshot)
            url = ((part or {}).get("image_url") or {}).get("url") or ""
            if url.startswith("data:image/jpeg;base64,"):
                images.append(url.split(",", 1)[1])
                self.point_image = part
        structured = {"app": app, "bundle_id": snapshot.bundle_id or None, "elements": len(self.aliases)}
        return ToolResult(text, structured, images)

    def _element(self, arguments, required=True):
        """The element ``element`` (an id from the latest screen) or ``label`` names."""
        from ..frontier import FrontierAgent
        ref, label = arguments.get("element"), arguments.get("label")
        snapshot = self.snapshot
        if snapshot is None or not self._still(snapshot):
            snapshot = self._read()
            fresh = True
        else:
            fresh = False
        if not ref and not label:
            if required:
                raise ToolProblem("Say which element: its id from the latest screen (element: \"e12\") or its "
                                  "visible label (label).")
            return None, snapshot
        known = self.aliases.get(ref) if ref else None
        if ref and known is None and not label:
            raise ToolProblem(f"{ref} isn't on the latest screen. Call screen for current ids.")
        if known is not None:
            found = known if not fresh else FrontierAgent._find(snapshot, known.label, known.role, known.locator)
        else:
            found = FrontierAgent._find(snapshot, label)
        if found is None:
            what = f"{ref} ({json.dumps(known.label)})" if known is not None else json.dumps(label)
            self._show(snapshot)
            raise ToolProblem(f"{what} isn't on the screen now. Call screen to see what is.")
        return found, snapshot

    def _still(self, snapshot):
        """Whether ``snapshot`` is still the screen: nothing moved since it was read (one fast check)."""
        from ..wda.guards import unchanged_since
        try:
            return unchanged_since(getattr(self.driver, "frame_clock", None), getattr(snapshot, "read_started", 0.0))
        except Exception:
            return False

    def _approval(self, op, element, snapshot, text, declared):
        """Ask the person before ``op`` on ``element`` when it commits something, raising ToolProblem unless approved;
        returns the target to act on, read again after a card, where a change from what the card showed refuses, its
        screen, and whether the gate read it as a commit or asked."""
        from .. import task_policy as policy
        gate = self.permissions
        subject = policy.approval_subject(snapshot.elements, element) if op in ("TAP", "LONG_PRESS") else element
        label, role = subject.label or "", subject.role
        # A hold on a control is read as a tap on it (Smart asks for both alike): holding Send can send.
        reading = "TAP" if op == "LONG_PRESS" else op
        commits = False
        if op in COMMIT_OPS:
            tier = policy.action_risk_tier(reading, label, self.goal)
            kind = policy.approval_kind(reading, label, risk_tier=getattr(tier, "value", tier), role=role)
            from ..permissions.consequence import FOLDERS, clears_query_field, keypad_key
            from ..permissions.rules import norm

            # A calculator's Clear key and a mailbox row named Trash are no act.
            from ..state import pixel_read
            pixels = pixel_read(snapshot)
            folder = (role == "Cell" or pixels) and norm(label) in FOLDERS and subject.rect[2] >= .6
            # A search bar's Clear empties a query, not a draft or a note: no act.
            query = clears_query_field(label, role, subject, snapshot.elements,
                                       getattr(snapshot, "bundle_id", "") or "", pixels)
            commits = (kind == "commit" and not folder and not query
                       and not keypad_key(label, role, snapshot.elements, pixels)) or declared
        if commits or op not in ("TAP", "LONG_PRESS"):
            self.changes += 1  # a commit, or typing: the result needs the screen to show it, which proof doesn't check
        act = policy.commit_act(label, self.goal, role, reading) if commits else None
        if op in ("TYPE_SUBMIT", "SUBMIT") and act == "submit" and self._field(element) == "composer":
            act = "send"  # Return in a message composer sends it, whatever the field is called ("iMessage")
        app_name = self.agent._app_name(snapshot) or None
        from ..permissions import action_from
        typed = text if op in ("TYPE", "SET_TEXT", "TYPE_SUBMIT") else None
        decision = gate.decide(action_from(op, subject, self.agent._masked(snapshot), app_name=app_name or "",
                                           text=typed), existing_act=act)
        if decision.decision == "allow":
            return element, snapshot, commits
        # It asks: with Ask before acting off too, since a one-time code asks in every mode.
        asker, reason, title = self.approve or self.ask, decision.reason or None, None
        if act is None:
            title, act = decision.title, decision.act or "other"
        remember = gate.remember(decision, getattr(snapshot, "bundle_id", "") or "", app_name or "")
        if asker is None:
            raise ToolProblem(f"{json.dumps(label or 'That')} needs the person's OK, and nobody is watching this "
                              "task to give it, so Mobster didn't do it. Finish and say what you need.")
        # The card shows the message as it will go and who it goes to.
        payload = text if op in ("TYPE_SUBMIT", "SUBMIT") and text else None
        if payload is None and act in policy.MESSAGE_ACTS and self.typed and self.typed[0] == app_name:
            payload = self.typed[1]
        from ..contract_screen import thread_title, visible_recipient
        recipient = (visible_recipient(self.agent._masked(snapshot), app_name, element)
                     if act in policy.MESSAGE_ACTS else None)
        # One message, one approval: a Return that didn't send isn't asked again for the Send tap after it.
        held = self.approved_send
        if (held and op in ("TAP", "LONG_PRESS") and act in policy.MESSAGE_ACTS and payload == held["text"]
                and held["app"] == app_name and held["thread"] == recipient):
            self.approved_send = None
            return element, snapshot, True
        x, y, w, h = element.rect
        request = {
            "kind": "commit", "step": self.steps + 1, "operation": op, "label": label[:200], "role": role,
            "text": self.agent._mask(payload) if payload else None,
            "title": title or policy.approval_title(act, recipient=recipient), "act": act, "app_name": app_name,
            "target": {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}}
        if act in offers.MONEY_ACTS:
            reason = offers.reason(self.agent._masked(snapshot), label, reason)   # what it buys, as the screen shows it
        if reason:
            request["reason"] = reason
        if remember:
            request["remember"] = remember
        from ..frontier import CHANGED_WHILE_ASKING, FrontierAgent, binding, same_binding
        bound = binding(snapshot, element)
        answer = asker(request)
        answer = str(answer or "denied")
        if answer == "approved":
            # The approval is bound to what the card showed: a fresh read must still show it.
            fresh = self._read()
            found = FrontierAgent._find(fresh, element.label, element.role, element.locator)
            if found is None or not same_binding(bound, binding(fresh, found)):
                self._show(fresh)
                raise ToolProblem(CHANGED_WHILE_ASKING + " Call screen, then make the call again.")
            # Return in a composer is a message whatever its label calls it ("Text Alex" names no send).
            thread = recipient or thread_title(self.agent._masked(snapshot), app_name)
            self.approved_send = ({"text": payload, "app": app_name, "thread": thread}
                                  if op in ("TYPE_SUBMIT", "SUBMIT") and payload else None)
            return found, fresh, True
        self._declined(answer, label)

    def _link_approval(self, decision):
        """A link deeplinks.decide says asks."""
        ask = self.approve or self.ask
        if ask is None:
            raise ToolProblem("Nobody is watching this task to approve that link, so Mobster didn't open it. Open "
                              "the app and use its screens instead, or finish and say what you need.")
        snapshot = self.snapshot if self.snapshot is not None else self._read()
        url = decision.url
        request = {"kind": "commit", "step": self.steps + 1, "operation": "OPEN_URL", "label": f"Open {url[:180]}",
                   "role": "Link", "text": self.agent._mask(url[:500]), "title": decision.title or "Open this link?",
                   "act": "open link", "app_name": decision.app_name or self.agent._app_name(snapshot) or None}
        if decision.remember() and self.remembered is not None:  # never for a run an outside agent started
            request["remember"] = decision.remember()
        self._declined(str(ask(request) or "denied"), url[:200])

    @staticmethod
    def _declined(answer, label):
        """Raises for any answer but approved: what the agent is told when the person said no, or nobody did."""
        if answer == "approved":
            return
        if answer == "stopped":
            raise RunStopped("The person stopped this task. Stop now and say nothing more.")
        if answer.startswith("redirected:"):
            raise ToolProblem(f"The person declined that and said what to do instead: {answer.split(':', 1)[1]}")
        if answer == "timeout":
            raise ToolProblem("Nobody answered the approval in time, so Mobster didn't do it. Finish, and say it "
                              "is still waiting for their OK.")
        raise ToolProblem(f"The person declined {json.dumps(label or 'that')}, so Mobster didn't do it. Don't try "
                          "another way: finish and say so, or ask them with ask_user.")

    @staticmethod
    def _refused(snapshot):
        """The iPhone's own passcode keypad is never touched: Mobster doesn't unlock phones."""
        from ..frontier import KEYPAD_REFUSAL
        from ..task_policy import springboard_keypad
        if springboard_keypad(snapshot):
            raise ToolProblem(KEYPAD_REFUSAL.replace("Refused:", "Mobster refused this:", 1))

    def _once(self, kind, execute, finish):
        """Send one action that must not be sent twice, then ``finish`` it; once its first byte may have left, any
        failure ends the run with the outcome unknown, and nothing here retries it."""
        from ..agent_hooks import GuardVerdict
        from ..wda.guards import DriverRejection
        sent = False
        try:
            before = execute()
            sent = True
            self.actions += 1
            return finish(before)
        except RunStopped:
            raise
        except Exception as error:  # noqa: BLE001 -- anything after dispatch is an unknown outcome
            if not sent and isinstance(error, DriverRejection) and error.code in DriverRejection.PRE_DISPATCH:
                raise
            self.stopped = GuardVerdict("stop", "action_unknown", f"The {kind} outcome is unknown. Inspect the phone "
                                        "before starting another task; it was not retried.")
            raise RunStopped(self.stopped.message) from None

    def _do(self, op, element, snapshot, *, text=None, app=None, step_label=None, field=None, where=None,
            image=False, point=None, asks=False):
        """One action through the frontier's _execute, then the step (with its frame) and the screen after."""
        from ..frontier import UNKNOWN_BLOCKS
        from ..frontier.stages.after import may_commit
        from ..smart_events import PRESSES, step_text
        self._check_running()
        label = step_label if step_label is not None else (element.label if element is not None else "")
        self.out({"event": "action_started", "operation": op, "target": label})

        def finish(before):
            if op in GESTURE_OPS:
                self.out({"event": "action_acknowledged", "step": self.steps + 1, "operation": op})
            after = self.agent._settle(before, last=True)
            self.agent._check_app(after)
            self.agent.last_screen = after
            words = step_text(op, label, self.apps, text=text, field=field, where=where)
            changed = getattr(before, "content_fingerprint", None) != getattr(after, "content_fingerprint", None)
            # A press commits by the permission gate's reading or by the loop's own test, where a test that fails counts
            # as a commit (may_commit), so a tap that worked never ends the run as an unknown outcome.
            self._step(words, commit=asks or may_commit(op, element, snapshot) if op in PRESSES else None,
                       changed=changed)
            feedback, self.agent.run_state.macro_feedback = self.agent.run_state.macro_feedback, None
            head = words + "." + ("" if changed or op in ("READ_LIST",) else " The screen didn't change.")
            if feedback:
                head += " " + feedback
            result = self._show(after, image=image, head=head)
            result.no_effect = self._no_effect(op, app, text, after, changed) or ""
            return result

        def execute():
            self.agent.run_state.last_receipt = None
            return self.agent._execute(op, element, app, text, snapshot, point=point)

        if op in UNKNOWN_BLOCKS or op in GESTURE_OPS:
            return self._once(GESTURE_OPS.get(op) or ("tap" if op in ("TAP", "TAP_XY", "LONG_PRESS") else "action"),
                              execute, finish)
        before = execute()
        self.actions += 1
        return finish(before)

    def _no_effect(self, op, app, text, after, changed):
        """Why the read after an action shows no effect, for act's chain, or None when it shows one. The screen moving
        is the plain case. An app in front, an item on screen and a write whose field reads back are effects without a
        move, and a scroll that moved without reaching its item isn't one. A moved screen isn't proof of the right
        screen: this only stops a chain from running on past a step that did nothing it can see."""
        if op == "LAUNCH_APP":
            return None if after.bundle_id == app else "the app isn't in front"
        if op == "HOME":
            return None if after.bundle_id == "com.apple.springboard" else "the Home Screen isn't showing"
        if op == "SCROLL_TO":
            return None if (text or "").casefold() in (after.text or "").casefold() else "the item isn't on screen"
        receipt = self.agent.run_state.last_receipt
        if receipt is not None and receipt.get("value") is not None:
            return None if receipt.get("matches") else "the field doesn't hold what was written"
        return None if changed else "the screen didn't change"

    def _step(self, words, commit=None, changed=None):
        self.steps += 1
        event = {"event": "step", "n": self.steps, "step": self.steps, "text": words}
        # Left out when unknown (smart_events.SmartEvents._step).
        if isinstance(commit, bool):
            event["commit"] = commit
        if isinstance(changed, bool):
            event["changed"] = changed
        data = frame_bytes(self.driver)
        if data:
            event["_frame"] = data
        self.out(event)

    def _field(self, element):
        from ..contract_screen import COMPOSER, SEARCHY
        words = f"{element.label} {element.placeholder}"
        if element.role == "SearchField" or SEARCHY.search(words):
            return "search"
        return "composer" if COMPOSER.search(words) else "field"

    def tool_screen(self, a):
        return self._show(self._read(), image=a.get("image", True))

    def tool_tap(self, a):
        element, snapshot = self._element(a)
        self._refused(snapshot)
        element, snapshot, asks = self._approval("TAP", element, snapshot, None, bool(a.get("irreversible")))
        return self._do("TAP", element, snapshot, image=a.get("image", False), asks=asks)

    def _point_approval(self, operation, points, label, snapshot):
        """Ask before a tap at a point or a gesture, as the gate reads it; a block at any point refuses the whole
        action, and what asks is refused with nobody to answer."""
        from ..contract_asks import point_spot
        from ..permissions import action_from
        masked = self.agent._masked(snapshot)
        app_name = self.agent._app_name(snapshot) or ""
        decision = None
        for location in points:
            _, under = point_spot(snapshot, *location)
            spot = under or SimpleNamespace(label="", role="Point", value="", locator="", placeholder="",
                                            identifier="", rect=(*location, .001, .001))
            decision = self.permissions.decide(action_from("TAP_XY", spot, masked, app_name=app_name),
                                               existing_act="other" if operation != "TAP_XY" else None)
            if decision.decision == "block":
                raise ToolProblem(f"Refused: {decision.reason or 'not allowed'}. Nothing was done.")
        if operation != "TAP_XY" or decision.act:
            self.changes += 1  # a gesture, or a point on something that acts
        if decision.decision == "allow":
            return False
        asker = self.approve or self.ask
        if asker is None:
            raise ToolProblem(f"{json.dumps(label or 'That')} needs the person's OK, and nobody is watching this task "
                              "to give it, so Mobster didn't do it. Finish and say what you need.")
        x, y = points[0]
        request = {"kind": "commit", "step": self.steps + 1, "operation": operation, "label": label[:200],
                   "role": "Point", "text": None, "act": decision.act or "other", "app_name": app_name or None,
                   "title": (decision.title or f"{label[:60]}?" if operation == "TAP_XY"
                             else f"{operation[:-3].capitalize()} {label[:60]}? This may change something."),
                   "target": {"x": round(x, 4), "y": round(y, 4), "w": .001, "h": .001}}
        if decision.reason:
            request["reason"] = decision.reason
        self._declined(str(asker(request) or "denied"), label)
        return True

    def tool_tap_point(self, a):
        x, y, label = a.get("x"), a.get("y"), a.get("label")
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 < v < 100
                for v in (x, y)) or not isinstance(label, str) or not label.strip()):
            raise ToolProblem("Name the visible control in label and give x and y between 0 and 100 percent.")
        self._point_image_required()

        def grounded():
            try:
                fresh = self.agent.point_screen(self.snapshot, self.point_image, [(x / 100, y / 100)])
            except ValueError as error:
                raise ToolProblem(str(error)) from None
            self._refused(fresh)
            return fresh

        from ..frontier import Action
        snapshot = grounded()
        point, refused = self.agent._tap_xy(self.goal, Action("TAP_XY", target=f"{x}%,{y}%", text=label), self.aliases,
                                            snapshot)
        if refused:
            raise ToolProblem(refused)
        if self._point_approval("TAP_XY", [point], label, snapshot):
            snapshot = grounded()  # the person approved the spot on the screen as it was
        self._check_running()
        self.point_image = None
        return self._do("TAP_XY", None, snapshot, point=point, step_label=label, image=True)

    def _point_image_required(self):
        if self.snapshot is None or self.point_image is None:
            raise ToolProblem("Call screen with image true before choosing a point on the screenshot.")

    def _point_gesture(self, kind, arguments):
        self._point_image_required()
        try:
            gesture = validate_gesture_request(kind, arguments)
            snapshot = self.agent.gesture_screen(self.snapshot, self.point_image, gesture, self.goal)
        except ValueError as error:
            raise ToolProblem(str(error)) from None
        self._refused(snapshot)
        if self._point_approval(gesture.operation, gesture.points, gesture.label, snapshot):
            try:
                snapshot = self.agent.gesture_screen(self.snapshot, self.point_image, gesture, self.goal)
            except ValueError as error:
                raise ToolProblem(str(error)) from None
        self._check_running()
        self.point_image = None  # a gesture consumes the picture even if reading its result fails
        return self._do(gesture.operation, None, snapshot, point=gesture, step_label=gesture.label, image=True)

    def tool_drag_point(self, a):
        return self._point_gesture("drag", a)

    def tool_hold_point(self, a):
        return self._point_gesture("hold", a)

    def tool_flick_point(self, a):
        return self._point_gesture("flick", a)

    def tool_long_press(self, a):
        element, snapshot = self._element(a)
        self._refused(snapshot)
        element, snapshot, asks = self._approval("LONG_PRESS", element, snapshot, None, bool(a.get("irreversible")))
        return self._do("LONG_PRESS", element, snapshot, image=a.get("image", False), asks=asks)

    def tool_type_text(self, a):
        text = a.get("text")
        if not isinstance(text, str) or not text:
            raise ToolProblem("Say what to type in text.")
        element, snapshot = self._element(a)
        self._refused(snapshot)
        if not element.editable:
            raise ToolProblem(f"{json.dumps(element.label or 'That element')} isn't a text field. Tap the field "
                              "first, or name the field itself.")
        from ..state import validate_input_text
        try:  # refused here, where it can be corrected: past this point a failed send ends the run
            validate_input_text(text, multiline=element.role == "TextView")
        except ValueError as error:
            raise ToolProblem(f"{error}. Shorten or correct the text and type it again.") from None
        submit, append = bool(a.get("submit")), bool(a.get("append"))
        field = self._field(element)
        if field == "composer":
            current = (element.value or "") if append and has_text(element) else ""
            self.typed = (self.agent._app_name(snapshot) or None, current + text)
        # The permission gate reads what is typed (a password is never typed); Return asks as a commit.
        element, snapshot, _ = self._approval("TYPE_SUBMIT" if submit else "TYPE", element, snapshot, text,
                                           bool(a.get("irreversible")))
        if submit and (append or not has_text(element)):
            return self._do("TYPE_SUBMIT", element, snapshot, text=text, field=field, image=a.get("image", False))
        # An empty field takes the text as it is: no clear first (a clear and a read cost a round trip).
        op = "TYPE" if append or not has_text(element) else "SET_TEXT"
        result = self._do(op, element, snapshot, text=text, field=field)
        if not submit:
            return result
        from ..frontier import FrontierAgent
        current = self.snapshot
        again = FrontierAgent._find(current, element.label, element.role, element.locator) or element
        return self._do("SUBMIT", again, current, step_label=element.label, image=a.get("image", False))

    def tool_swipe(self, a):
        direction = a.get("direction")
        if direction not in SWIPES:
            raise ToolProblem("direction is up, down, left or right (up shows what is below).")
        element, snapshot = self._element(a, required=False)
        op = SWIPES[direction]
        if op in ("SWIPE_UP", "SWIPE_DOWN"):
            element = None  # the frontier scrolls the screen's scroll view
        elif element is not None:
            # A row's swipe archives or deletes in some apps (Gmail, Outlook): the gate reads it like a tap.
            element, snapshot, _ = self._approval(op, element, snapshot, None, bool(a.get("irreversible")))
        return self._do(op, element, snapshot, image=a.get("image", False))

    def tool_open_app(self, a):
        wanted = " ".join(str(a.get("app") or "").split())
        if not wanted:
            raise ToolProblem("Say which app: its name as on the Home Screen, or its bundle id.")
        bundle = wanted if wanted in self.apps else next(
            (b for b, name in self.apps.items() if name.casefold() == wanted.casefold()), None)
        if bundle is None:
            near = [name for name in self.apps.values() if wanted.casefold() in name.casefold()][:5]
            raise ToolProblem(f"There's no app called {json.dumps(wanted)} on this iPhone."
                              + (" Did you mean " + ", ".join(near) + "?" if near else
                                 " The apps are listed in your instructions."))
        snapshot = self.snapshot if self.snapshot is not None else self._read()
        self._allowed_app(bundle)  # asks before the app opens, the first time
        return self._do("LAUNCH_APP", None, snapshot, app=bundle, step_label=bundle, image=a.get("image", False))

    def _allowed_app(self, bundle):
        """Raises ToolProblem unless the person lets Mobster use ``bundle`` (the run's guard asks the first time)."""
        from ..agent_hooks import safe_allows
        guard = getattr(self.agent, "guard", None)
        if not bundle or bundle == "com.apple.springboard" or guard is None or safe_allows(guard, bundle):
            return
        said = getattr(guard, "blocked_message", None)
        try:
            why = said(bundle, self.apps.get(bundle)) if callable(said) else None
        except Exception:
            why = None
        raise ToolProblem((why or f"Mobster may not use {self.apps.get(bundle, bundle)}.") + " Do the task another "
                          "way, or finish and say what is left for the person.")

    def tool_home(self, a):
        snapshot = self.snapshot if self.snapshot is not None else self._read()
        return self._do("HOME", None, snapshot, image=a.get("image", False))

    def tool_dismiss(self, a):
        snapshot = self._read()
        return self._do("DISMISS", None, snapshot)

    def tool_read_list(self, a):
        snapshot = self._read()
        stop_at = a.get("stop_at") or None
        app = self.agent._app_name(snapshot) or None
        self.out({"event": "action_started", "operation": "READ_LIST", "target": ""})
        self.agent._execute("READ_LIST", None, None, stop_at, snapshot)
        rows, self.agent.run_state.listing = list(self.agent.run_state.listing or ()), None
        from ..smart_events import step_text
        self.actions += 1
        self._step(step_text("READ_LIST", "", self.apps, where=app))
        after = self.agent._read_once()
        feedback, self.agent.run_state.macro_feedback = self.agent.run_state.macro_feedback, None
        shown = rows[:LIST_ROWS_SHOWN]
        head = (f"Read {len(rows)} row{'s' if len(rows) != 1 else ''} of the list, top to bottom"
                + (f" (showing the first {LIST_ROWS_SHOWN})" if len(rows) > LIST_ROWS_SHOWN else "") + ":\n"
                + "\n".join(f"- {self.agent._mask(row)}" for row in shown) + ("\n" + feedback if feedback else ""))
        return self._show(after, head=head)

    def tool_scroll_to(self, a):
        text = " ".join(str(a.get("text") or "").split())
        if not text:
            raise ToolProblem("Say the text of the item to scroll to.")
        snapshot = self._read()
        return self._do("SCROLL_TO", None, snapshot, text=text, step_label="")

    def tool_open_url(self, a):
        url = str(a.get("url") or "").strip()
        if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", url) or len(url) > 2000:
            raise ToolProblem("The link needs a scheme, such as https://example.com or maps://?q=coffee.")
        self._refused(self.snapshot if self.snapshot is not None else self._read())
        from ..deeplinks import LinkError, app_for, decide, open_link
        try:
            decision = decide(url, self.remembered)
        except LinkError as error:
            raise ToolProblem(str(error)) from None
        self._allowed_app(app_for(url))  # the app the link opens, before it opens
        if decision.action == "ask":
            self.changes += 1
            self._link_approval(decision)
        self.out({"event": "action_started", "operation": "OPEN_URL", "target": url[:80]})
        opener = getattr(self.driver, "open_url", None)
        if callable(opener):
            opener(url)
        else:
            open_link(self.driver, url, bundle=app_for(url) or None)
        self.actions += 1
        words = self._link_words(url)
        self._step(words)
        return self._show(self._read(), image=a.get("image", False), head=f"Opened {url[:200]}.")

    def _link_words(self, url):
        """A link's step in plain words, never its scheme or query: "Opened example.com", "Opened Settings", else
        "Opened a link"."""
        from urllib.parse import urlsplit

        from ..catalog import APPS
        from ..deeplinks import app_for
        host = urlsplit(url).hostname if url.casefold().startswith(("http://", "https://")) else None
        if host:
            return f"Opened {host.removeprefix('www.')[:60]}"
        bundle = app_for(url)
        name = self.apps.get(bundle) or next((app["name"] for app in APPS if app["bundleId"] == bundle), None)
        return f"Opened {name}" if name else "Opened a link"

    def tool_wait(self, a):
        try:
            seconds = min(WAIT_MAX, max(0.5, float(a.get("seconds", 2))))
        except (TypeError, ValueError):
            seconds = 2.0
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.cancelled():
                raise RunStopped("The person stopped this task. Stop now and say nothing more.")
            time.sleep(min(.25, max(0.0, deadline - time.monotonic())))
        return self._show(self._read(), head=f"Waited {seconds:g} s.")

    def tool_ask_user(self, a):
        question = " ".join(str(a.get("question") or "").split())[:QUESTION_CHARS]
        if not question:
            raise ToolProblem("Say what to ask in question.")
        if self.ask is None:
            raise ToolProblem("Nobody is watching this task to answer. Decide from the request, or finish and say "
                              "what you need to know.")
        choices = [" ".join(str(c).split())[:CHOICE_CHARS] for c in (a.get("choices") or ()) if str(c).strip()][:6]
        request = {"kind": "clarify", "operation": "ASK_USER", "label": question[:80], "question": question,
                   "choices": [{"id": f"c{i + 1}", "label": c} for i, c in enumerate(choices)],
                   "allow_text": True, "step": self.steps + 1}
        answer = str(self.ask(request) or "timeout")
        if answer == "stopped":
            raise RunStopped("The person stopped this task. Stop now and say nothing more.")
        if answer.startswith("answer:"):
            return ToolResult("They answered: " + answer.split(":", 1)[1], {"answer": answer.split(":", 1)[1]})
        if answer.startswith("choice:"):
            picked = answer.split(":", 1)[1]
            label = next((c for i, c in enumerate(choices) if f"c{i + 1}" == picked), picked)
            return ToolResult("They chose: " + label, {"answer": label})
        if answer == "approved" and choices:
            return ToolResult("They chose: " + choices[0], {"answer": choices[0]})
        raise ToolProblem("Nobody answered in time. Finish, and say what you need to know.")


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


ELEMENT = {"type": "string", "pattern": ELEMENT_ID.pattern,
           "description": "the element's id from the latest screen, such as e12"}
LABEL = {"type": "string", "minLength": 1, "maxLength": 200,
         "description": "or the element's visible label, exactly as the screen lists it"}
IMAGE = {"type": "boolean",
         "description": "also return a screenshot (default false: the text screen is usually enough)"}
IRREVERSIBLE = {"type": "boolean",
                "description": "true when this sends, posts, buys, books, deletes or can't be undone: "
                "Mobster asks the person first"}
DESCRIPTIONS = {
    "hold_point": "Hold a named visible screenshot point for 0.1 to 10 seconds (default 1.5), then release. Uses the "
                  "screenshot from your last screen call, existing approvals and one bounded gesture. Returns a fresh "
                  "screenshot; cannot be chained or interrupted during contact. An unknown outcome stops the run.",
    "flick_point": "Flick between named visible screenshot points in 0.08 to 0.3 seconds (default 0.12), without a "
                   "pickup hold. Existing approvals and screenshot and path checks apply. Returns a fresh screenshot; "
                   "cannot be chained. An unknown outcome stops the run.",
    "drag_point": "Drag between two visible screenshot points with named endpoints and screen percentages. Optionally "
                  "hold before moving with hold_seconds (0 to 2, default 0.1). Uses one gesture, returns a fresh "
                  "screenshot and cannot be chained. Existing approval rules apply; a changed screen, or a sensitive "
                  "control along the path, is refused. An unknown outcome stops the run.",
    "tap_point": "Tap a visible control that has no element id, such as an icon the screen listing leaves out. x and y "
                 "are screen percentages from your last screenshot, label names the control. Returns a fresh "
                 "screenshot; choose each point from it. Existing approval rules still apply.",
    "act": "Do several actions in a row in one call when you can predict the screens, such as opening a chat, typing "
           "and tapping Send: faster than one call per action. Name elements after the first by label, since the "
           "screen changes. Stops at the first step that fails, isn't approved or shows no effect, "
           "and returns the screen after the last.",
    "screen": "Read the iPhone's current screen: the app in front and every element with an id (e1, e2 ...), its role, "
              "label, value and position, plus a screenshot. Call it first, and whenever you are unsure what is shown.",
    "tap": "Tap an element, named by its id from the latest screen or by its label. Returns the screen after the tap. "
           "Mobster asks the person before a tap that sends, posts, buys, books or deletes; "
           "mark such a tap irreversible.",
    "type_text": "Type into a text field (replacing its text unless append is true); submit presses Return after "
                 "typing. Returns the screen after. Return that sends a message waits for the person's OK.",
    "swipe": "Swipe the screen: up shows what is below, down what is above; left and right on an element swipe that "
             "row or carousel. Returns the screen after.",
    "long_press": ("Press and hold an element (opens its menu). Returns the screen after. Mobster asks the person "
                   "first when the hold commits something, as for tap."),
    "open_app": "Open an app by its name as on the Home Screen (Messages, Notes) or its bundle id. Faster and surer "
                "than finding its icon. Returns the screen after.",
    "home": "Go to the Home Screen.",
    "read_list": "Read the whole list or feed on screen from here down in one call, scrolling as it goes (up to about "
                 "60 rows): faster than swiping and reading screen by screen. stop_at stops at the first row "
                 "containing that text.",
    "scroll_to": "Scroll down until an item containing this text is on screen. Returns the screen after.",
    "dismiss": "Close a pop-up, sheet or banner that covers the screen, by tapping outside it.",
    "open_url": ("Open a link or deep link on the iPhone (https://..., maps://?q=..., tel:, sms:). A web page, Maps or "
                 "the App Store opens at once; a call, FaceTime, a message, an email or a shortcut waits for the "
                 "person's OK each time, and another app's link the first time."),
    "ask_user": "Ask the person a question (with up to 6 choices) and wait for their answer. Use it when the "
                "request is "
                "ambiguous or you need something only they know; never for a password or passcode. A choice that names "
                "a person or a place says what tells it apart after \" \u2014 \" (\"Sam Lee \u2014 texted Tuesday\").",
    "wait": "Wait a few seconds (at most 10) for something to load, then read the screen.",
}
SCHEMAS = {
    "drag_point": DRAG_SCHEMA,
    "hold_point": HOLD_SCHEMA,
    "flick_point": FLICK_SCHEMA,
    "tap_point": _schema({"x": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 100},
                          "y": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 100},
                          "label": LABEL}, ("x", "y", "label")),
    "screen": _schema({"image": {"type": "boolean", "description": "include a screenshot (default true)"}}),
    "act": _schema({"steps": {"type": "array", "minItems": 1, "maxItems": MAX_CHAIN, "items": {
        "type": "object", "required": ["tool"], "additionalProperties": True,
        "properties": {"tool": {"type": "string", "enum": list(CHAINABLE)},
                       "element": ELEMENT, "label": LABEL,
                       "text": {"type": "string", "maxLength": 4000},
                       "submit": {"type": "boolean"}, "append": {"type": "boolean"},
                       "irreversible": IRREVERSIBLE,
                       "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                       "app": {"type": "string", "maxLength": 200},
                       "seconds": {"type": "number", "minimum": 0.5, "maximum": WAIT_MAX}}},
        "description": "the actions in order, each with its tool and that tool's arguments"}}, ("steps",)),
    "tap": _schema({"element": ELEMENT, "label": LABEL, "irreversible": IRREVERSIBLE, "image": IMAGE}),
    "type_text": _schema({"element": ELEMENT, "label": LABEL,
                          "text": {"type": "string", "minLength": 1, "maxLength": 4000, "description": "what to type"},
                          "submit": {"type": "boolean", "description": "press Return after typing"},
                          "append": {"type": "boolean",
                                     "description": "add to the field's text instead of replacing it"},
                          "irreversible": IRREVERSIBLE, "image": IMAGE}, ("text",)),
    "swipe": _schema({"direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                      "element": ELEMENT, "label": LABEL, "image": IMAGE}, ("direction",)),
    "long_press": _schema({"element": ELEMENT, "label": LABEL, "irreversible": IRREVERSIBLE, "image": IMAGE}),
    "open_app": _schema({"app": {"type": "string", "minLength": 1, "maxLength": 200,
                                 "description": "the app's name or bundle id"}, "image": IMAGE}, ("app",)),
    "home": _schema({"image": IMAGE}),
    "read_list": _schema({"stop_at": {"type": "string", "maxLength": 200,
                                      "description": "stop at the first row containing this text"}}),
    "scroll_to": _schema({"text": {"type": "string", "minLength": 1, "maxLength": 200}}, ("text",)),
    "dismiss": _schema({}),
    "open_url": _schema({"url": {"type": "string", "minLength": 3, "maxLength": 2000}, "image": IMAGE}, ("url",)),
    "ask_user": _schema({"question": {"type": "string", "minLength": 1, "maxLength": QUESTION_CHARS},
                         "choices": {"type": "array", "maxItems": 6,
                                     "items": {"type": "string", "minLength": 1, "maxLength": CHOICE_CHARS}}},
                        ("question",)),
    "wait": _schema({"seconds": {"type": "number", "minimum": 0.5, "maximum": WAIT_MAX}}),
}
# The same risk hints as mobster mcp's tools (mcp_server/protocol.py): a tool that changes the phone is destructive.
READS = {**READ_ONLY, "idempotentHint": True}
ANNOTATIONS = {name: dict(READS if name in ("screen", "read_list", "wait", "ask_user") else ACTS) for name in NAMES}
for _name in NAMES:
    ANNOTATIONS[_name]["title"] = _name.replace("_", " ").capitalize()
