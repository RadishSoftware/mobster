"""The thread context provider (key ``thread``, order 10, SPEC §3.2 feature 3).

A follow-up in a conversation ("now reply to that") needs what the earlier tasks found. At the start of a Smart
run in a thread, this gives the frontier two stable blocks, together at most 3,000 characters:

- ``thread`` (trusted): the user's own words from the last three exchanges (what they asked, what they added while
  Mobster worked, how they answered its questions) and the rules they gave in this conversation, copied by code
  (plans.py's prohibition rule), never summarised.
- ``thread_results`` (untrusted, so fenced as data): a condensed line for older exchanges, then per recent exchange
  Mobster's outcome, the app it ended in, its answer and up to 600 characters of its notes. Mobster's earlier
  answers are text read off screens, so they are data, never instructions. Notes went through
  secret_filter.redact when they were stored, and a note that still looks secret was dropped then.

The condensation is deterministic (v1): oldest exchanges first move into the condensed line until the blocks fit;
the rules are never dropped. A thread's first task gets no block, so its prompt is exactly a one-shot task's.
"""

from .. import harness_api
from .. import secret_filter

KEY = "thread"
ORDER = 10
MAX_CHARS = 3000
RECENT = 3
ASK_CHARS = 500          # one message, quoted
EXTRA_CHARS = 300        # a message sent while Mobster worked, an answer to its question
EXTRAS_PER_EXCHANGE = 3
ANSWER_CHARS = 400
NOTES_CHARS = 600
CONDENSED_CHARS = 500
TRUSTED_TITLE = "Earlier in this conversation (the user's own words)"
RESULTS_TITLE = "What Mobster's earlier tasks in this conversation found"
OUTCOMES = {"done": "Done", "check": "Finished, worth a check", "answered": "Answered", "blocked": "Couldn't finish",
            "stopped": "Stopped", "failed": "Failed"}


def cut(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def defang(text):
    """Untrusted text can't close the fence it sits in."""
    return str(text).replace("<<<", "‹‹‹").replace(">>>", "›››")


def exchanges(items, exclude_run=None):
    """The thread's exchanges in order: one per run, with the user's words and the run's result.

    Each is {"runId", "ask", "extras": [(label, text)], "result": dict|None, "context": dict, "appName"}."""
    by_run, order = {}, []
    for item in items:
        run_id = item.get("runId")
        if not run_id or run_id == exclude_run:
            continue
        exchange = by_run.get(run_id)
        if exchange is None:
            exchange = by_run[run_id] = {"runId": run_id, "ask": None, "extras": [], "result": None, "context": {},
                                         "appName": None, "goal": None, "seq": item.get("seq", 0)}
        kind = item.get("kind")
        if kind == "user":
            routed = item.get("routed")
            if routed == "new_run" and exchange["ask"] is None:
                exchange["ask"] = item.get("text") or ""
            elif routed == "steer":
                exchange["extras"].append(("Added while it ran", item.get("text") or ""))
            elif routed == "answer":
                exchange["extras"].append(("Answer to Mobster's question", item.get("text") or ""))
            elif routed == "unread":
                exchange["extras"].append(("Sent after it finished (not read)", item.get("text") or ""))
        elif kind == "run":
            exchange["goal"] = item.get("goal")
            exchange["result"] = item.get("result")
            exchange["context"] = item.get("_context") or {}
            exchange["appName"] = item.get("appName")
            if item.get("seq") is not None:
                exchange["seq"] = item["seq"]
            order.append(run_id)
    out = []
    for run_id in order:
        exchange = by_run[run_id]
        if exchange["ask"] is None:
            exchange["ask"] = exchange["goal"] or ""
        out.append(exchange)
    return out


def outcome_words(result):
    """"Done, in Messages" from a run item's result."""
    if not result:
        return "Still running or interrupted"
    status = result.get("status") or ""
    outcome = result.get("outcome") or ""
    if status == "completed" or outcome == "done":
        return OUTCOMES["done"]
    if status == "completion_not_confirmed" or outcome == "check":
        return OUTCOMES["check"]
    if status in ("stopped", "approval_denied", "approval_timeout"):
        return {"approval_denied": "The user declined the action", "approval_timeout": "Nobody answered the "
                "approval in time"}.get(status, OUTCOMES["stopped"])
    if status in ("blocked", "no_progress", "max_steps", "timeout", "spend_cap"):
        return OUTCOMES["blocked"]
    if status == "interrupted":
        return "Interrupted"
    return OUTCOMES["failed"] if status else "Unknown"


def condensed_line(older):
    """One line for the exchanges that no longer fit, newest kept when the line runs long."""
    if not older:
        return ""
    parts = []
    for exchange in reversed(older):
        where = exchange["context"].get("lastApp") or exchange["appName"]
        part = f"“{cut(exchange['ask'], 60)}” → {outcome_words(exchange['result']).lower()}" + (
            f" in {where}" if where else "")
        if sum(len(p) + 2 for p in parts) + len(part) > CONDENSED_CHARS - 40:
            break
        parts.append(part)
    left = len(older) - len(parts)
    text = "; ".join(reversed(parts))
    if left:
        text = f"{left} earlier task{'s' if left != 1 else ''}, then " + text
    return f"Earlier, condensed: {text}."


def trusted_text(recent, rules, first_number):
    lines = []
    if recent:
        lines.append("Earlier requests, oldest first:")
        for offset, exchange in enumerate(recent):
            lines.append(f"{first_number + offset}. “{cut(exchange['ask'], ASK_CHARS)}”")
            for label, text in exchange["extras"][-EXTRAS_PER_EXCHANGE:]:
                lines.append(f"   {label}: “{cut(text, EXTRA_CHARS)}”")
    if rules:
        lines.append("Rules the user gave earlier in this conversation (they still apply unless this task says "
                     "otherwise):")
        lines += [f"- {rule}" for rule in rules]
    return "\n".join(lines)


def result_text(recent, older, first_number, with_notes=True):
    lines = []
    line = condensed_line(older)
    if line:
        lines.append(line)
    for offset, exchange in enumerate(recent):
        result = exchange["result"] or {}
        where = exchange["context"].get("lastApp") or exchange["appName"]
        lines.append(f"{first_number + offset}. {outcome_words(exchange['result'])}" + (f", in {where}." if where
                                                                                         else "."))
        answer = exchange["context"].get("answer") or result.get("answer")
        if answer:
            lines.append(f"   Answer: {cut(secret_filter.redact(answer), ANSWER_CHARS)}")
        facts = [cut(secret_filter.redact(f), 160) for f in (result.get("facts") or [])[:4] if f]
        if facts:
            lines.append("   Seen: " + "; ".join(facts))
        notes = exchange["context"].get("notes") or []
        if with_notes and notes:
            lines.append("   Notes: " + cut("; ".join(notes), NOTES_CHARS))
    return defang("\n".join(lines))


def build(thread, items, *, current_run=None, current_goal="", limit=MAX_CHARS):
    """The (trusted, untrusted) texts for a run in ``thread``; ("", "") when there is nothing earlier."""
    done = exchanges(items, exclude_run=current_run)
    goal = " ".join(str(current_goal or "").split()).casefold()
    # A rule from the message that started this run is in its goal already.
    rules = [rule for rule in (thread.get("rules") or []) if " ".join(rule.split()).casefold() not in goal]
    if not done and not rules:
        return "", ""
    split = max(0, len(done) - RECENT)
    older, recent = done[:split], done[split:]
    with_notes = True
    while True:
        first = len(older) + 1
        trusted = trusted_text(recent, rules, first)
        results = result_text(recent, older, first, with_notes) if (recent or older) else ""
        size = len(trusted) + len(results)
        if size <= limit:
            return trusted, results
        if len(recent) > 1:
            older, recent = older + recent[:1], recent[1:]
            continue
        if with_notes:
            with_notes = False
            continue
        # One exchange left and still too long: cut the results first; the user's own words and rules stay.
        room = max(0, limit - len(trusted))
        if room < 40:
            trusted = trusted[:limit - 1] + "…"
            return trusted, ""
        return trusted, results[:room - 1] + "…"


class ThreadProvider:
    """harness_api.ContextProvider for one run in a thread."""

    key = KEY
    max_chars = MAX_CHARS

    def __init__(self, service):
        self.service = service

    def blocks(self, ctx):
        thread_id = ctx.thread_id
        thread = self.service.store.get(thread_id) if thread_id else None
        if thread is None:
            return ()
        items = self.service.store.items(thread_id)
        trusted, results = build(thread, items, current_run=ctx.run_id, current_goal=ctx.goal)
        out = []
        if trusted:
            out.append(harness_api.ContextBlock(KEY, TRUSTED_TITLE, trusted, stable=True))
        if results:
            out.append(harness_api.ContextBlock(KEY, RESULTS_TITLE, results, stable=True, untrusted=True))
        return out

    def turn_blocks(self, ctx, turn):
        return ()
