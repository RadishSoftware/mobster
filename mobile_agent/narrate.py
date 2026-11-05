"""Agent events as the steps a person reads: shared by the terminal UI and `mobster run` on a terminal.

The agent emits one event per boundary it crosses (observation, decision, action
check, dispatch, settle). A reader wants one line per step: what Mobster did, to
what, and whether the screen answered. ``Narrator`` folds the event stream into
those steps without changing or dropping any event; ``--json`` output and the
journal keep the raw events.

The wording follows the Mac app's presentation (dashboard/src/lib/run-presentation.ts)
so a task reads the same in both. Nothing here imports a UI library.
"""

from dataclasses import dataclass, field

# The Mac app's labels (dashboard/src/lib/api.ts statusLabels), so both products agree; a test keeps every shared
# key equal. The last few are statuses only the terminal shows.
STATUS_LABELS = {
    "queued": "Queued", "running": "Running", "stopped": "Stopped", "interrupted": "Interrupted",
    "user_condition_met": "Stop condition met", "needs_clarification": "Needs clarification",
    "error": "Failed", "blocked": "Needs attention", "completed_unverified": "Take a look",
    "expected_text_visible": "Expected text visible", "demo_complete": "Demo complete",
    "inconsistent_completion": "Take a look", "completion_not_confirmed": "Take a look",
    "needs_text_helper": "Text entry unavailable", "no_progress": "Needs attention",
    "max_steps": "Step limit reached", "timeout": "Time limit reached", "approval_denied": "Declined",
    "approval_timeout": "Approval expired", "spend_cap": "Spending limit reached",
    # Smart's proven finish: every item of the task's contract was shown done (engine-contract.md).
    "completed": "Done",
    "preview": "Preview", "duplicate_effect_blocked": "Duplicate refused",
    "duplicate_text_blocked": "Duplicate refused",
    "invalid_action": "Stopped before acting", "invalid_target": "Stopped before acting",
    "expected_text_missing": "Expected text missing",
}

STATUS_TONES = {
    "completed": "success", "completed_unverified": "success", "expected_text_visible": "success",
    "user_condition_met": "neutral",
    "preview": "neutral", "stopped": "neutral", "approval_denied": "neutral", "demo_complete": "review",
    "inconsistent_completion": "review", "completion_not_confirmed": "review",
    "error": "error", "interrupted": "error", "approval_timeout": "warning", "spend_cap": "warning",
    "max_steps": "warning", "timeout": "warning", "blocked": "error", "no_progress": "error",
    "needs_text_helper": "error", "invalid_action": "error", "invalid_target": "error",
    "expected_text_missing": "error", "needs_clarification": "error",
    "duplicate_effect_blocked": "neutral", "duplicate_text_blocked": "neutral",
}

DONE, FOUND, TAKE_A_LOOK, COULDNT, STOPPED_SAFELY, DECLINED, STOPPED = (
    "Done", "Here's what Mobster found", "Take a look", "Couldn't finish", "Stopped safely", "You declined", "Stopped")
TAKE_A_LOOK_LINE = "Mobster couldn't confirm all of this on the screen. Look at your iPhone."


def where_to_look(summary):
    summary = summary if isinstance(summary, dict) else {}
    if summary.get("deviceKind") == "simulator":
        return "the simulator"
    name = " ".join(str(summary.get("deviceName") or "").split())[:60]
    return name or "your iPhone"


def take_a_look_line(summary=None):
    return f"Mobster couldn't confirm all of this on the screen. Look at {where_to_look(summary)}."
UNPROVEN = {"completed_unverified", "expected_text_visible", "inconsistent_completion", "completion_not_confirmed",
            "demo_complete"}


def result_head(status, summary=None):
    summary = summary if isinstance(summary, dict) else {}
    outcome = summary.get("outcome")
    found = summary.get("data") is not None and summary.get("data_status") in (None, "extracted")
    if status == "completed" or (status in UNPROVEN and (outcome == "done" or (outcome is None and
                                                                                summary.get("proof")))):
        return (FOUND if found else DONE), "success"
    if status in UNPROVEN or outcome == "check":
        return TAKE_A_LOOK, "review"
    if status == "preview":
        return "Preview", "neutral"
    if status == "approval_denied":
        return DECLINED, "neutral"
    if status in ("stopped", "user_condition_met"):
        return STOPPED, "neutral"
    if status in ("approval_timeout", "spend_cap", "duplicate_effect_blocked", "duplicate_text_blocked",
                  "max_steps", "timeout"):
        return STOPPED_SAFELY, "warning" if status in ("spend_cap", "max_steps", "timeout") else "neutral"
    return COULDNT, "error"


def proof_line(summary, apps=None):
    """"Seen on screen: “Dark: On” in Display & Brightness" from the result's first proof, or ""."""
    proof = (summary or {}).get("proof") if isinstance(summary, dict) else None
    if not proof or not isinstance(proof, list) or not isinstance(proof[0], dict):
        return ""
    item = proof[0]
    quote = str(item.get("quote") or "").strip()
    if not quote:
        return ""
    where = str(item.get("screen") or "").strip() or app_name(item.get("app"), apps)
    return f"Seen on screen: “{quote}”" + (f" in {where}" if where else "")


def hero_line(summary):
    """The result's answer, when there is one: what Mobster found, or what it did in one sentence."""
    summary = summary if isinstance(summary, dict) else {}
    answer = summary.get("answer")
    if isinstance(answer, str) and answer.strip():
        return answer.strip()
    data = summary.get("data")
    if isinstance(data, str) and data.strip():
        return data.strip()
    return ""


def cents(nanodollars):
    """What a task cost, as MESSAGING's facts say it: 2¢, under 1¢, $1.20."""
    dollars = (nanodollars or 0) / 1e9
    if dollars >= 1:
        return f"${dollars:.2f}"
    value = dollars * 100
    if value < 1:
        return "under 1¢"
    return f"{value:.0f}¢"


def facts(actions, elapsed_ms, cost_nanodollars=None, priced=False, plan=False):
    """"3 steps, 6.2 s, 2¢"; ``plan``: every call ran on the person's plan, so no cents: "on your plan"."""
    out = [f"{actions} step{'s' if actions != 1 else ''}"]
    if elapsed_ms:
        out.append(seconds(elapsed_ms))
    if plan and not cost_nanodollars:
        out.append("on your plan")
    elif priced:
        out.append(cents(cost_nanodollars))
    return ", ".join(out)

TERMINAL_MESSAGES = {
    "approval_denied": "You declined the action, so Mobster did not take it. Earlier steps may have completed.",
    "approval_timeout": "Nobody answered the approval request in time, so Mobster did not take the action.",
    "stopped": "You stopped this task.",
    "user_condition_met": "Stopped at your requested condition.",
    "needs_clarification": "Mobster needs clarification before taking further action. "
                           "Edit the instruction to make the next step clear.",
}

ANSWER_FAILURES = {
    "not_extracted": "The requested answer was not produced. The task stopped before result extraction.",
    "no_observed_evidence": "The app did not expose enough evidence to return the requested answer.",
    "insufficient_evidence": "The app did not expose enough evidence to return the requested answer.",
    "unsupported_answer": "The extracted answer could not be verified against the observed app evidence. "
                          "No answer was returned.",
    "verification_timeout": "Answer verification timed out. No verified answer was returned.",
    "verification_failed": "Answer verification could not be completed. No verified answer was returned.",
    "extraction_failed": "Result extraction failed. The requested answer was not produced.",
    "extraction_timeout": "Result extraction timed out. The requested answer was not produced.",
    "helper_unavailable": "No helper model is configured, so the requested answer was not produced.",
    "helper_budget_exhausted": "The helper budget ran out before the requested answer was produced.",
}

OPERATIONS = {
    "TAP": ("Tap", "Tapped"), "DOUBLE_TAP": ("Double-tap", "Double-tapped"),
    "LONG_PRESS": ("Press and hold", "Pressed and held"),
    "TYPE": ("Type into", "Typed into"), "TYPE_SUBMIT": ("Type and submit in", "Typed and submitted in"),
    "SUBMIT": ("Submit", "Submitted"),
    # A swipe up moves the content up: the reader sees the page scroll down.
    "SWIPE_UP": ("Scroll down", "Scrolled down"), "SWIPE_DOWN": ("Scroll up", "Scrolled up"),
    "SWIPE_LEFT": ("Swipe left", "Swiped left"), "SWIPE_RIGHT": ("Swipe right", "Swiped right"),
    "BACK": ("Go back", "Went back"), "HOME": ("Go home", "Went home"),
    "LAUNCH_APP": ("Open", "Opened"), "OPEN_URL": ("Open the address", "Opened the address"),
    "INCREMENT": ("Increase", "Increased"), "DECREMENT": ("Decrease", "Decreased"),
    "VOLUME_UP": ("Volume up", "Pressed volume up"), "VOLUME_DOWN": ("Volume down", "Pressed volume down"),
    "WAIT": ("Waiting for the screen", "Waited for the screen"),
    "DONE": ("Check the task is done", "Checked the task is done"),
    # A task as one of your profiles (accounts/screen.py); a run journaled before the rename names CHECK_ACCOUNT.
    "CHECK_PROFILE": ("Check the profile", "Checked the profile"),
    "CHECK_ACCOUNT": ("Check the profile", "Checked the profile"),
    "BLOCKED": ("Looking for a way forward", "Looking for a way forward"),
}

# Events a reader never needs; the raw stream (--json, the journal) keeps them.
QUIET = {"run_started", "latency_trace", "inference_started", "inference_finished", "stop_requested",
         "decision_memo_hit", "decision_predicted", "decision_first_screen_predicted", "decision_speculation_used",
         "decision_speculation_discarded", "decision_input_tightened", "refresh_skipped_fresh",
         "refresh_skipped_pixels_still", "refresh_target_stable", "completion_reread_skipped_pixels_still",
         "answer_signals", "stale_decision", "text_selected", "result"}

NOTES = {
    "unclear_bypassed": ("warning", "Bypass: acted although the action check was unclear"),
    "unclear_put_to_user": ("neutral", "The action check was unclear, so you are asked"),
    "effect_duplicate_refused": ("warning", "Refused to repeat an action that already took effect"),
    "duplicate_text_refused": ("warning", "Refused to type the same text twice"),
    "completion_continued": ("neutral", "Not finished yet: continuing"),
    "demotion_lifted": ("neutral", "Allowed a held-back action"),
    "replay_abandoned": ("neutral", "The screen changed: stopped replaying a recorded run"),
    "plan_compiled": ("neutral", "Planned the steps across apps"),
    "loop_compiled": ("neutral", "Compiled the repeated action into a loop"),
    "milestones_compiled": ("neutral", "Split the request into milestones"),
    "page_probe_answered": ("neutral", "Found the answer in the page text"),
    "visual_answer": ("neutral", "Answered from the screen image"),
    "completion_by_route": ("neutral", "Reached the requested page"),
    "route_decision": ("neutral", "Followed a known route"),
    "helper": ("neutral", "Asked the helper model for another route"),
}


# The phone guard's and the skills' events: one plain line each, never a digit of a passcode or a code.
GUARD_NOTES = {
    ("unlock_finished", True): ("neutral", "Unlocked the iPhone"),
    ("unlock_finished", False): ("warning", "The saved passcode didn't unlock the iPhone"),
    ("handoff_waiting", "face_id"): ("neutral", "Waiting for you: approve Face ID on your iPhone"),
    ("handoff_waiting", "apple_confirmation"): ("neutral", "Waiting for you: confirm on your iPhone"),
    # Look at your iPhone: a task that started on a locked iPhone waits for the unlock.
    ("handoff_waiting", "lock_screen"): ("warning", "Look at your iPhone: unlock it and Mobster starts"),
    ("handoff_waiting", "passcode_keypad"): ("warning", "Look at your iPhone: unlock it and Mobster starts"),
    ("handoff_finished", True): ("neutral", "Done on your iPhone: continuing"),
    ("handoff_finished", False): ("warning", "Nothing was confirmed on your iPhone"),
    ("handoff_finished", "lock_screen", True): ("neutral", "Your iPhone is unlocked: starting"),
    ("handoff_finished", "passcode_keypad", True): ("neutral", "Your iPhone is unlocked: starting"),
    ("handoff_finished", "lock_screen", False): None,  # the run's own stop says it ("Still locked, so …")
    ("handoff_finished", "passcode_keypad", False): None,
    # The phone locked in the middle of a task a person started: it waits for them, then carries on from there.
    ("handoff_waiting", "lock_screen", "midrun"): ("warning", "Your iPhone locked: unlock it and Mobster carries on"),
    ("handoff_waiting", "passcode_keypad", "midrun"): ("warning",
                                                       "Your iPhone locked: unlock it and Mobster carries on"),
    ("handoff_finished", "lock_screen", True, "midrun"): ("neutral", "Your iPhone is unlocked: carrying on"),
    ("handoff_finished", "passcode_keypad", True, "midrun"): ("neutral", "Your iPhone is unlocked: carrying on"),
    # The cable came out, or the Wi-Fi link went down, under a task a person started: it waits for the phone to return.
    ("handoff_waiting", "unplugged"): ("warning", "Your iPhone isn't plugged in: plug it in and Mobster starts"),
    ("handoff_waiting", "unplugged", "midrun"): ("warning", "Your iPhone was unplugged: plug it back in and Mobster "
                                                            "carries on"),
    ("handoff_waiting", "wifi_lost"): ("warning", "Your iPhone is out of reach over Wi-Fi: bring it back or plug it "
                                                  "in and Mobster starts"),
    ("handoff_waiting", "wifi_lost", "midrun"): ("warning", "Your iPhone went out of reach over Wi-Fi: bring it back "
                                                            "or plug it in and Mobster carries on"),
    ("handoff_finished", "unplugged", True): ("neutral", "Your iPhone is back: starting"),
    ("handoff_finished", "unplugged", True, "midrun"): ("neutral", "Your iPhone is back: carrying on"),
    ("handoff_finished", "wifi_lost", True): ("neutral", "Your iPhone is back: starting"),
    ("handoff_finished", "wifi_lost", True, "midrun"): ("neutral", "Your iPhone is back: carrying on"),
    ("handoff_finished", "unplugged", False): None,
    ("handoff_finished", "wifi_lost", False): None,
    ("skill_finished", "USE_CODE"): ("neutral", "Entered the verification code"),
    # USE_CODE looked before the code arrived (skills/codes.await_code): it watches for it,
    # then goes on or says why not.
    ("code_waiting", None): ("neutral", "Waiting for the sign-in code to arrive"),
    ("code_wait_finished", True): ("neutral", "The sign-in code arrived"),
    ("skill_finished", "READ_NOTIFICATIONS"): ("neutral", "Read the notifications"),
    ("frontier_ax_only", None): ("warning", "The iPhone's screenshots are timing out: reading its screen without them "
                                            "for a few steps"),
}


def guard_note(event):
    """(tone, text) for a phone-guard or skill event, or None."""
    kind = event.get("event")
    if kind == "unlock_finished":
        return GUARD_NOTES[(kind, event.get("ok") is True)]
    midrun = event.get("midrun") is True
    if kind == "handoff_waiting":
        if midrun and (kind, event.get("kind"), "midrun") in GUARD_NOTES:
            return GUARD_NOTES[(kind, event.get("kind"), "midrun")]
        return GUARD_NOTES.get((kind, event.get("kind")))
    if kind == "handoff_finished":
        ok = event.get("ok") is True
        if midrun and (kind, event.get("kind"), ok, "midrun") in GUARD_NOTES:
            return GUARD_NOTES[(kind, event.get("kind"), ok, "midrun")]
        if (kind, event.get("kind"), ok) in GUARD_NOTES:
            return GUARD_NOTES[(kind, event.get("kind"), ok)]
        return GUARD_NOTES[(kind, ok)]
    if kind == "skill_finished" and event.get("ok") is True:
        return GUARD_NOTES.get((kind, event.get("op")))
    if kind == "frontier_ax_only":
        return GUARD_NOTES[(kind, None)]
    if kind == "code_waiting":
        return GUARD_NOTES[(kind, None)]
    if kind == "code_wait_finished":
        return GUARD_NOTES.get((kind, event.get("ok") is True))
    return None


ONCE = {"route_decision", "completion_continued", "replay_abandoned", "helper"}
STALLS = {"WAIT", "BLOCKED"}


def status_label(status):
    return STATUS_LABELS.get(status) or str(status or "").replace("_", " ").capitalize()


def status_tone(status):
    return STATUS_TONES.get(status, "neutral")


STATUS_GLYPHS = {"success": "✓", "error": "✗", "warning": "!", "review": "◆"}


def status_glyph(tone, default="■"):
    return STATUS_GLYPHS.get(tone, default)


def completion_message(status, reason="", data_status=None, summary=None):
    """The sentence under a finished run's status (dashboard completionMessage)."""
    reason = reason if isinstance(reason, str) else ""
    if status == "needs_clarification" and "exact effect could not be established" in reason:
        return ("The proposed action could not be checked safely and was not dispatched. "
                "Earlier actions may have completed.")
    if status in TERMINAL_MESSAGES:
        return TERMINAL_MESSAGES[status]
    if status in {"inconsistent_completion", "completion_not_confirmed"}:
        return (ANSWER_FAILURES.get(data_status)
                or "Mobster could not confidently confirm completion. Review the final screen.")
    if status in {"blocked", "no_progress"} and (not reason or reason == "No supported progress"):
        return "Nothing on the screen moved the task forward, so Mobster stopped without acting further. " \
               "Look at the phone, or add detail to the task."
    if status in {"completed_unverified", "expected_text_visible", "demo_complete"} and (
            not reason or reason.startswith(("Model agreement only", "Text assertion passed"))):
        # The head (Done with its proof, or Take a look) already says it; the agent's internal wording stays in
        # the raw events.
        return take_a_look_line(summary) if status != "expected_text_visible" else ""
    return reason


def operation_words(operation):
    """(in progress, done) phrases for an operation."""
    operation = str(operation or "").upper()
    if operation in OPERATIONS:
        return OPERATIONS[operation]
    word = operation.replace("_", " ").capitalize() or "Act"
    return word, word


# The Mac app's words for the two answers (dashboard approval-prompt.tsx, approvalVerbs), so the terminal names the
# same buttons the app shows. An act this doesn't know keeps Approve and Decline.
_SENT, _POSTED = ("Send", "Don't send"), ("Post", "Don't post")
ACTS = {
    "send_message": _SENT, "send": _SENT, "reply": _SENT, "send message": _SENT,
    "post": _POSTED, "comment": _POSTED, "publish": ("Publish", "Don't publish"), "tweet": _POSTED,
    "pay": ("Pay", "Don't pay"), "transfer": ("Transfer", "Don't transfer"),
    "request_money": ("Request", "Don't request"), "donate": ("Donate", "Don't donate"), "tip": ("Tip", "Don't tip"),
    "order": ("Place order", "Don't order"), "place order": ("Place order", "Don't order"), "buy": ("Buy", "Don't buy"),
    "purchase": ("Buy", "Don't buy"), "checkout": ("Check out", "Don't check out"),
    "book": ("Book", "Don't book"), "reserve": ("Reserve", "Don't reserve"),
    "delete": ("Delete", "Don't delete"), "erase": ("Erase", "Don't erase"), "remove": ("Remove", "Don't remove"),
    "trash": ("Move to trash", "Keep it"), "clear": ("Clear", "Don't clear"),
    "follow": ("Follow", "Don't follow"), "unfollow": ("Unfollow", "Don't unfollow"), "share": ("Share", "Don't share"),
    "call": ("Call", "Don't call"), "dial": ("Call", "Don't call"), "facetime": ("Call", "Don't call"),
    "subscribe": ("Subscribe", "Don't subscribe"), "unsubscribe": ("Unsubscribe", "Don't unsubscribe"),
    "sign up": ("Sign up", "Don't sign up"), "request": ("Send request", "Don't send"),
    "react": ("React", "Don't react"), "use app": ("Allow", "Not now"),
    "add": ("Add", "Don't add"), "save": ("Save", "Don't save"), "archive": ("Archive", "Don't archive"),
    "submit": ("Submit", "Don't submit"), "confirm": ("Confirm", "Don't confirm"), "cancel": ("Cancel it", "Keep it"),
    "accept": ("Accept", "Don't accept"), "join": ("Join", "Don't join"), "leave": ("Leave", "Stay"),
    "block": ("Block", "Don't block"), "report": ("Report", "Don't report"), "install": ("Install", "Don't install"),
    "upload": ("Upload", "Don't upload"),
}
ASK = ("Approve", "Decline")


def verbs(approval):
    if approval.get("kind") == "loop":
        return ASK
    if approval.get("kind") == "plan":
        return ("Start", "Skip")
    if approval.get("kind") == "proposals":
        # Yes and no answer the whole batch here: every draft not yet decided is sent, or skipped.
        yes, no = verbs({"act": approval.get("act")}) if approval.get("act") else ASK
        return (f"{yes} all", "Skip all")
    act = approval.get("act")
    if not act and approval.get("operation") == "TAP":
        act = (str(approval.get("label") or "").split() or [""])[0]
    raw = str(act or "").strip().lower()
    key = raw if raw in ACTS else " ".join(raw.replace("_", " ").replace("-", " ").split())
    if key in ACTS:
        return ACTS[key]
    if key == "cancel" or key.startswith("cancel "):
        return ACTS["cancel"]
    if key == "end call":
        return ("End call", "Stay on")
    return ASK


def approval_title(approval):
    """What Mobster asks to do, in its own words (the approval's title), else from the control it would tap."""
    if approval.get("act") == "use app" and approval.get("apps"):
        # The wire title is shortened; a terminal still needs to name every app it would allow.
        names = approval["apps"]
        listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        return f"Allow Mobster to use {listed}?"
    if approval.get("title"):
        return str(approval["title"])
    label = approval.get("label") or ""
    title = operation_words(approval.get("operation"))[0] + (f" “{label}”" if label else "")
    app = approval.get("app")
    return f"{title} in {app}" if app and app.casefold() not in title.casefold() else title


def seconds(ms):
    """A duration for a reader: 180 ms, 1.4 s, 2 min 5 s."""
    if ms is None:
        return ""
    ms = float(ms)
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    return f"{int(ms // 60_000)} min {int(ms % 60_000 // 1000)} s"


def usd(nanodollars):
    """Spend for a status line: $0.0042, $0.12, $3.40."""
    dollars = (nanodollars or 0) / 1e9
    return f"${dollars:.4f}" if dollars < 0.1 else f"${dollars:.2f}"


def app_name(bundle, apps=None):
    """An app's display name from the catalog (or ``apps``), else its bundle id."""
    if not bundle:
        return ""
    from .catalog import APPS
    for app in list(apps or ()) + APPS:
        if app.get("bundleId") == bundle:
            return app["name"]
    return bundle


@dataclass
class Step:
    """One observe-decide-verify-act iteration of the agent."""
    key: str
    index: int
    app: str = ""
    elements: int | None = None
    observe_ms: float | None = None
    operation: str | None = None
    target: str = ""
    confidence: float | None = None
    goal_probability: float | None = None
    decide_ms: float | None = None
    model: str = ""
    check: str | None = None
    act: str | None = None
    act_ms: float | None = None
    changed: bool | None = None
    settle_ms: float | None = None
    started_at: float | None = None
    ended_at: float | None = None
    completion: str | None = None
    note: str = ""                    # why a decided action was never sent
    target_id: str | None = None
    tries: int = 1                    # consecutive steps that did not act, collapsed into this one
    merged_into: object = None        # set on a step folded into the one before it

    @property
    def state(self):
        """thinking, acting, done, unchanged, failed."""
        if self.act == "not_dispatched":
            return "failed"
        if self.act is None and self.note:
            return "skipped"
        if self.act == "started":
            return "acting"
        if self.act == "acknowledged":
            return "done" if self.changed is not False else "unchanged"
        if self.operation in STALLS:
            return "stalled"  # a decision not to act ends its step; the next read starts another
        if self.operation == "DONE" and self.ended_at is not None:
            return "done"
        return "thinking"

    @property
    def title(self):
        if not self.operation:
            if self.ended_at is not None:
                return "Read the screen"
            return "Deciding" if self.elements is not None else "Reading the screen"
        present, past = operation_words(self.operation)
        finished = self.operation in {"DONE", "WAIT"} and self.ended_at is not None
        verb = past if self.act == "acknowledged" or finished else present
        if self.operation in {"TAP", "DOUBLE_TAP", "LONG_PRESS", "TYPE", "TYPE_SUBMIT", "SUBMIT",
                              "INCREMENT", "DECREMENT"} and self.target:
            return f"{verb} “{self.target}”"
        if self.operation == "LAUNCH_APP" and self.target:
            return f"{verb} {self.target}"
        return verb

    @property
    def duration_ms(self):
        if self.started_at is None or self.ended_at is None:
            return None
        return max(0.0, self.ended_at - self.started_at)

    def phases(self):
        """[(phase, text)] for observe, decide, verify and act, the ones that happened."""
        rows = []
        if self.elements is not None or self.observe_ms is not None:
            where = f"{self.app}, " if self.app else ""
            count = f"{self.elements} elements" if self.elements is not None else "screen read"
            rows.append(("observe", f"{where}{count}" + (f", {seconds(self.observe_ms)}" if self.observe_ms else "")))
        if self.operation:
            sure = f"{self.confidence:.0%} sure" if isinstance(self.confidence, (int, float)) else ""
            if self.operation == "DONE" and isinstance(self.goal_probability, (int, float)):
                sure = f"goal {self.goal_probability:.0%} likely met"
            parts = [operation_words(self.operation)[0] + (f" “{self.target}”" if self.target else ""), sure,
                     seconds(self.decide_ms) if self.decide_ms else ""]
            rows.append(("decide", ", ".join(p for p in parts if p)))
        if self.check:
            rows.append(("verify", self.check))
        if self.act:
            if self.act == "not_dispatched":
                text = "not sent: the screen changed first"
            elif self.act == "started":
                text = "sending…"
            else:
                outcome = {True: "screen changed", False: "no visible change", None: "sent"}[self.changed]
                text = ", ".join(p for p in (outcome, seconds((self.act_ms or 0) + (self.settle_ms or 0))
                                              if self.act_ms is not None else "") if p)
            rows.append(("act", text))
        elif self.note:
            rows.append(("act", self.note))
        if self.completion:
            rows.append(("check", "done" if self.completion == "confirmed" else "not finished yet"))
        return rows


@dataclass
class Launch:
    key: str
    app: str
    done: bool = False


@dataclass
class Note:
    key: str
    tone: str
    text: str


@dataclass
class Approval:
    key: str
    approval_id: str
    operation: str
    label: str
    kind: str = "action"
    text_present: bool = False
    choices: tuple = ()
    decision: str | None = None
    request: dict = field(default_factory=dict)  # the live request (with any text), never journaled

    @property
    def title(self):
        if self.kind != "action":
            return self.request.get("question") or self.label or "Mobster has a question"
        present = operation_words(self.operation)[0]
        return f"{present} “{self.label}”" if self.label else present


@dataclass
class Outcome:
    key: str
    status: str
    summary: dict

    @property
    def label(self):
        return status_label(self.status)

    @property
    def tone(self):
        return status_tone(self.status)

    @property
    def message(self):
        return completion_message(self.status, self.summary.get("reason"), self.summary.get("data_status"),
                                  self.summary)

    @property
    def head(self):
        """MESSAGING §11's head: Done, Here's what Mobster found, Take a look, Couldn't finish, …"""
        return result_head(self.status, self.summary)[0]

    @property
    def head_tone(self):
        return result_head(self.status, self.summary)[1]

    @property
    def hero(self):
        return hero_line(self.summary) if self.head in (DONE, FOUND, TAKE_A_LOOK) else ""

    @property
    def proof(self):
        return proof_line(self.summary) if self.head in (DONE, FOUND) else ""

    @property
    def detail(self):
        head = self.head
        if head in (DONE, FOUND):
            return ""
        if head == TAKE_A_LOOK:
            return take_a_look_line(self.summary)
        return self.message


class Narrator:
    """Folds one run's events into items, in order; a target resolves against the latest earlier observation only."""

    def __init__(self, apps=None):
        self.apps = apps
        self.items = []
        self.by_key = {}
        self.labels = {}
        self.app = ""
        self.step = None
        self.cost_nanodollars = 0
        self.priced_calls = 0
        self.unpriced_calls = 0
        self.plan_calls = 0
        self.decisions = 0
        self.actions = 0
        self.started_at = None
        self.finished_at = None
        self.status = "running"
        self.noted = set()
        # The latest screen Mobster read: {"app", "bundle", "elements", "keyboard"} (elements as in events).
        self.screen = None

    def _add(self, item):
        self.items.append(item)
        self.by_key[item.key] = item
        return item

    def _close_unsent(self, step, note):
        """A step whose decided action was never sent: say why instead of spinning forever."""
        if step is not None and step.act is None and step.operation not in {None, "DONE", "WAIT", "BLOCKED"}:
            step.note = step.note or note

    def _merge_stall(self, step):
        """Fold a step that did not act into the stalled step right before it. Returns that step, or None."""
        previous = next((item for item in reversed(self.items) if isinstance(item, Step) and item is not step), None)
        between = self.items[self.items.index(previous) + 1:self.items.index(step)] if previous else []
        if (previous is None or previous.operation not in STALLS or previous.act is not None
                or any(not isinstance(item, Note) for item in between)):
            return None
        previous.tries += 1
        for name in ("operation", "confidence", "goal_probability", "decide_ms", "observe_ms", "elements", "app",
                     "ended_at", "target_id"):
            setattr(previous, name, getattr(step, name))
        step.merged_into = previous
        self.items.remove(step)
        self.by_key[step.key] = previous
        self.step = previous
        return previous

    def _step(self, index, timestamp=None, key=None):
        key = key or f"step-{index}"
        step = self.by_key.get(key)
        if step is None:
            if self.step is not None and self.step.ended_at is None:
                self.step.ended_at = timestamp
            self._close_unsent(self.step, "not sent: the screen moved first, so Mobster decided again")
            step = self._add(Step(key, index, app=self.app, started_at=timestamp))
        self.step = step
        return step

    def feed(self, event):
        if not isinstance(event, dict):
            return []
        kind = event.get("event")
        stamp = event.get("timestamp")
        if stamp is not None and self.started_at is None:
            self.started_at = stamp
        if kind == "inference_finished":
            cost = event.get("cost_nanodollars")
            if event.get("cost_basis") == "plan":
                self.plan_calls += 1
            if type(cost) is int and cost >= 0:
                self.cost_nanodollars += cost
                self.priced_calls += 1
            else:
                self.unpriced_calls += 1
            return []
        if kind in ("frontier_decision", "frontier_action"):
            return self._frontier(kind, event, stamp)
        if kind == "app_launch_started":
            return [self._add(Launch("launch", event.get("app") or ""))]
        if kind == "app_launch_acknowledged":
            launch = self.by_key.get("launch")
            if launch:
                launch.done = True
                return [launch]
            return []
        if kind == "observation":
            self.labels = {e.get("id"): (e.get("label") or "").strip() for e in event.get("elements") or ()
                           if isinstance(e, dict)}
            if event.get("bundle_id"):
                self.app = app_name(event["bundle_id"], self.apps)
            self.screen = {"app": self.app, "bundle": event.get("bundle_id") or "",
                           "elements": [e for e in event.get("elements") or () if isinstance(e, dict)],
                           "keyboard": event.get("keyboard") or ""}
            step = self._step(event.get("step", 0), stamp)
            step.app = self.app or step.app
            step.elements = len(event.get("elements") or ())
            return [step]
        if kind == "decision":
            self.decisions += 1
            step = self._step(event.get("step", self.step.index if self.step else 0), stamp)
            step.operation = event.get("operation")
            target = event.get("target")
            if step.operation == "LAUNCH_APP":
                step.target = app_name(target, self.apps)
            else:
                step.target = (event.get("target_label") or self.labels.get(target) or
                               ("an element on screen" if target else ""))
            step.confidence = event.get("confidence")
            step.goal_probability = event.get("goal_probability")
            step.decide_ms = event.get("latency_ms")
            step.observe_ms = event.get("observe_ms", step.observe_ms)
            step.model = event.get("model") or ""
            step.target_id = target if isinstance(target, str) else None
            if step.operation in STALLS | {"DONE"}:
                step.ended_at = stamp
            if step.operation in STALLS:
                merged = self._merge_stall(step)
                if merged is not None:
                    return [step, merged]
            return [step]
        if kind == "action_check" and self.step is not None:
            skipped = event.get("skipped")
            self.step.check = ({"plain_navigation": "skipped: plain navigation", "keypad": "skipped: keypad key"}
                               .get(skipped) or {"allowed": "allowed", "unclear": "unclear", "mismatch":
                                                 "does not match the request"}.get(event.get("support"),
                                                                                    event.get("support")))
            return [self.step]
        if kind == "approval_requested":
            item = self._add(Approval(f"approval-{event.get('approval_id')}", event.get("approval_id") or "",
                                      event.get("operation") or "", event.get("label") or "",
                                      kind=event.get("kind") or "action",
                                      text_present=bool(event.get("text_present")),
                                      choices=tuple(event.get("choices") or ())))
            return [item]
        if kind == "approval_resolved":
            item = self.by_key.get(f"approval-{event.get('approval_id')}")
            if item:
                item.decision = event.get("decision")
                return [item]
            return []
        if kind == "action_started":
            self.actions += 1
            step = self.step if self.step is not None else self._step(event.get("step", 0), stamp)
            if event.get("operation") == "OPEN_URL":
                step.operation, step.target = "OPEN_URL", ""
            step.act = "started"
            return [step]
        if kind == "action_acknowledged" and self.step is not None:
            self.step.act, self.step.act_ms = "acknowledged", event.get("act_ms")
            return [self.step]
        if kind == "action_not_dispatched" and self.step is not None:
            self.step.act = "not_dispatched"
            self.step.ended_at = stamp
            return [self.step]
        if kind == "observation_after_action" and self.step is not None:
            self.step.changed, self.step.settle_ms = event.get("changed"), event.get("settle_ms")
            self.step.ended_at = stamp
            return [self.step]
        if kind == "completion_check" and self.step is not None:
            self.step.completion = "confirmed" if event.get("operation") == "DONE" else "not_yet"
            return [self.step]
        if kind == "step_interrupted":
            return [self._add(Note(f"note-{len(self.items)}", "neutral",
                                   "A device or network request timed out; re-reading the screen"))]
        if kind == "error":
            detail = event.get("native_code") or event.get("type") or "error"
            return [self._add(Note(f"note-{len(self.items)}", "error", f"Error: {detail}"))]
        note = guard_note(event)
        if note is not None:
            return [self._add(Note(f"note-{len(self.items)}", note[0], note[1]))]
        if kind in NOTES:
            if kind in ONCE and kind in self.noted:
                return []
            self.noted.add(kind)
            tone, text = NOTES[kind]
            return [self._add(Note(f"note-{len(self.items)}", tone, text))]
        if kind == "run_finished":
            summary = event.get("summary") if isinstance(event.get("summary"), dict) else {}
            self.status = event.get("status") or summary.get("status") or "error"
            self.finished_at = stamp
            if self.step is not None and self.step.ended_at is None:
                self.step.ended_at = stamp
            self._close_unsent(self.step, "preview: nothing was sent" if self.status == "preview" else "not sent")
            changed = [self.step] if self.step is not None else []
            return changed + [self._add(Outcome("outcome", self.status, summary))]
        return []

    def _frontier(self, kind, event, stamp):
        index = event.get("step", 0)
        if kind == "frontier_decision":
            self.decisions += 1
            step = self._step(index, stamp)
            step.operation, step.target = event.get("operation"), event.get("target_label") or ""
            step.decide_ms, step.model = event.get("latency_ms"), event.get("model") or ""
            if step.operation in STALLS | {"DONE"}:
                step.ended_at = stamp
            return [step]
        position = event.get("index") or 0
        step = self._step(index, stamp, key=f"step-{index}.{position}" if position else None)
        step.operation = event.get("operation") or step.operation
        step.target = event.get("target_label") or step.target or ""
        step.app = event.get("app_name") or step.app
        self.actions += 1
        step.act, step.changed, step.settle_ms = "acknowledged", event.get("changed"), event.get("settle_ms")
        step.ended_at = stamp
        return [step]

    def elapsed_ms(self, now_ms=None):
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else now_ms
        return max(0.0, (end or self.started_at) - self.started_at)
