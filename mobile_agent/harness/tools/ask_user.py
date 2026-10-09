"""ASK_USER: Mobster's agent asks the person a clarifying question (SPEC §3.1 F2).

Offered only in a run a person watches (origin app, tui or cli) whose surface can show a question, which says so with
the run field ``askUser: true`` (until it does, a question would reach the person as an approval sheet), and with
Settings › Advanced › "Mobster may ask me questions" on (the default); at most ASK_USER_LIMIT (2) questions a task.
Never in workflows, schedules, MCP or the plain API.

It asks through the run's clarify asker (``Run.request_approval`` limited to kind "clarify", seam S5): the question
and up to 6 choices, with a typed answer allowed. The answer goes back to the model as feedback ("The user
answered: ..."); it is never journaled in ``approval_resolved``. Answering a question approves nothing: a send, buy,
post or delete after it still asks with its exact text. Nobody answering in 10 minutes ends the task
(``approval_timeout``: "Stopped safely").
"""

import time

from ...agent_hooks import Prepared, SkillResult
from ...secret_filter import redact
from ..settings import ASK_USER_LIMIT

PROMPT = ("- ASK_USER text: only when the request can be read two ways the screen can't settle and a wrong guess would "
          "change what is sent, bought, posted, deleted or answered. Never to confirm a send (that asks by itself). "
          "Put up to 4 short answers after the question, each after \" | \" (\"Which Sam? | Sam Lee | Sam Ortiz\").")
QUESTION_CHARS, LABEL_CHARS, CHOICE_CHARS, MAX_CHOICES = 300, 80, 120, 6

NO_QUESTION = "ASK_USER needs the question in text."
CANT_ASK = ("Refused: this task can't ask the user. Decide from the request and the screen, or finish with BLOCKED "
            "and say what you need to know.")
ASKED_ENOUGH = ("You already asked the user twice in this task. Decide from the request and the screen, or finish "
                "with BLOCKED and say what you need to know.")
DECLINED = ("The user chose not to answer. Decide from the request and the screen, or finish with BLOCKED and say "
            "what you need to know. Don't ask again.")
NO_ANSWER = "Nobody answered Mobster's question in 10 minutes, so it stopped without doing anything more."
STOPPED = "Stopped while Mobster waited for an answer to its question."


def split(text):
    """(question, choices) from "Question? | A | B": at most MAX_CHOICES distinct choices, none unless two or more."""
    parts = [" ".join(part.split()) for part in str(text or "").split("|")]
    question = parts[0][:QUESTION_CHARS]
    choices, seen = [], set()
    for part in parts[1:]:
        label = part[:CHOICE_CHARS]
        if label and label.casefold() not in seen:
            seen.add(label.casefold())
            choices.append({"id": f"c{len(choices) + 1}", "label": label})
        if len(choices) == MAX_CHOICES:
            break
    return question, (choices if len(choices) >= 2 else [])


def feedback_for(answer, choices):
    """(feedback for the model, run-ending status or None) for the asker's answer."""
    answer = answer if isinstance(answer, str) else "denied"
    if answer.startswith("answer:") or answer.startswith("redirected:"):
        text = " ".join(answer.split(":", 1)[1].split())[:500]
        return f"The user answered your question: {text!r}. Go on with that.", None
    if answer.startswith("choice:"):
        chosen = answer.split(":", 1)[1]
        label = next((c["label"] for c in choices if c["id"] == chosen), chosen)
        return f"The user answered your question: {label!r}. Go on with that.", None
    if answer == "timeout":
        return NO_ANSWER, "approval_timeout"
    if answer == "stopped":
        return STOPPED, "stopped"
    return DECLINED, None


class AskUser:
    """agent_hooks.Skill: ASK_USER (one instance per run: it counts its questions)."""

    op = "ASK_USER"
    prompt = PROMPT
    needs_target = False
    interactive_only = True

    def __init__(self, limit=ASK_USER_LIMIT, clock=time.monotonic):
        self.limit, self.clock, self.asked = limit, clock, 0

    def available(self, ctx):
        return callable(getattr(ctx, "clarify", None))

    def prepare(self, ctx, act, snapshot):
        return Prepared(None)   # a question is not a commit: nothing to approve

    def perform(self, ctx, prepared, act, snapshot):
        question, choices = split(redact(str(act.get("text") or "")))
        if not question:
            return SkillResult(NO_QUESTION)
        clarify = getattr(ctx, "clarify", None)
        if not callable(clarify):
            return SkillResult(CANT_ASK)
        if self.asked >= self.limit:
            return SkillResult(ASKED_ENOUGH)
        self.asked += 1
        started = self.clock()
        answer = clarify({"kind": "clarify", "operation": self.op, "label": question[:LABEL_CHARS],
                          "question": question, "choices": choices, "allow_text": True})
        waited = max(0.0, self.clock() - started)
        feedback, status = feedback_for(answer, choices)
        return SkillResult(feedback, changed=False, waited=waited, status=status)


def factory(ctx):
    """register_tool's factory: an AskUser for a run a person watches, whose surface shows questions (``askUser:
    true``), with questions allowed in Settings; else None."""
    from ..settings import ask_user
    if not getattr(ctx, "interactive", False) or getattr(ctx, "engine", None) != "smart":
        return None
    if (ctx.extras or {}).get("askUser") is not True:
        return None
    if not ask_user(ctx.runtime):
        return None
    return AskUser()
