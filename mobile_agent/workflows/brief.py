"""The brief: one result put together from the tasks a workflow ran, and the Morning brief job built on it.

A workflow's ``brief`` step (batch.register_step, kind ``brief``) runs after its tasks, with no phone and no model. It
takes the result of every task this round finished, in order, and makes one finished run of them in the workflow's
conversation (Runtime.compose_result): their result blocks, each kept as it was (a review queue, a media list, a
table, notes), and a task with an answer but no blocks as a notes card titled with its words. That run is the
workflow's last result, so Home's job card, a PDF (POST /api/runs/{id}/artifact) and a share link work on it as on any
task. It then says "Your brief is ready" as a Mac notification (``batch_notification`` on the workflows topic).

- **Nothing new acts.** The brief only reads results that are already stored. A review queue's Approve on the brief
  is the Approve of the task that found the item (Runtime.decide_block_item follows ``summary.brief.sources``): the
  same stored item, the same one follow-up task per item, so approving in both places never sends twice.
- **Fails closed.** A brief with no finished task before it stops the workflow and says why.
- **Screen text is data.** The cards are the tasks' validated blocks (result_blocks.validate); nothing from them reaches
  an instruction.

``morning_brief(niche, at, timezone)`` is the Morning brief job (POST /api/workflows/templates/morning-brief): every
morning, your DMs that need you, what's trending in your niche and ideas from your saved posts, on Instagram, in one
result. App packs (feature/app-packs) take its tasks over when they land; until then Mobster's agent runs them.
"""

import re

from .. import result_blocks
from ..api_errors import APIError
from ..graph.catalog import app_missing
from . import records as batch
from .health import run_outcome

KIND = "brief"
TITLE_CHARS = 80
NOTES_CHARS = 6000

MORNING_BRIEF = "morning-brief"
INSTAGRAM = "com.burbn.instagram"
NICHE_CHARS = 60
DM_TASK = ("Go through my Instagram DMs and sort them into a review queue: needs a reply, can wait, and spam. Draft a "
           "one-line reply for each one that needs a reply. Don’t send anything.")
TRENDS_TASK = ("Watch 20 Reels about {niche} and tell me what’s trending: the formats, sounds and topics that keep "
               "coming up, with an example of each. Don’t like, comment or follow.")
IDEAS_TASK = ("Go through my newest saved posts on Instagram and give me ideas I could try next, each with the saved "
              "post it comes from. Don’t like, comment or save anything.")


def validate(step, runtime):
    """The step as saved: an optional ``title`` for the result (default: the workflow's name)."""
    if set(step) - {"id", "kind", "title"}:
        raise ValueError("A brief step has only a title")
    title = step.get("title")
    if title is not None and (not isinstance(title, str) or not 0 < len(title.strip()) <= TITLE_CHARS):
        raise ValueError(f"A brief's title is 1 to {TITLE_CHARS} characters")
    return {"title": " ".join(title.split())} if title else {}


def _summary(run):
    return run.summary if isinstance(getattr(run, "summary", None), dict) else {}


def _short(label, limit=60):
    words = " ".join(str(label).split())
    first = re.split(r"(?<=[.:!?])\s", words, maxsplit=1)[0]
    return first if len(first) <= limit else first[:limit - 1].rstrip() + "…"


def compose(sources):
    """(envelope, mapping, answer) for ``sources``: [(label, run)]."""
    blocks, mapping = [], {}
    for index, (label, run) in enumerate(sources, start=1):
        summary = _summary(run)
        envelope = summary.get("blocks") if isinstance(summary.get("blocks"), dict) else None
        own = [dict(block) for block in (envelope or {}).get("blocks") or () if isinstance(block, dict)]
        if own:
            for block in own:
                new_id = f"s{index}-{block.get('id') or 'b'}"[:64]
                mapping[new_id] = [run.id, block.get("id")]
                blocks.append({**block, "id": new_id, "title": block.get("title") or _short(label)})
            continue
        text = summary.get("full_answer") or summary.get("data") or summary.get("answer")
        if isinstance(text, str) and text.strip():
            blocks.append({"type": "markdown", "id": f"s{index}-notes", "title": _short(label),
                           "text": text.strip()[:NOTES_CHARS]})
    envelope, _problems = result_blocks.validate(blocks[:result_blocks.MAX_BLOCKS])
    kept = {block["id"] for block in (envelope or {}).get("blocks") or ()}
    mapping = {key: value for key, value in mapping.items() if key in kept}
    return envelope, mapping, answer_of(envelope)


def answer_of(envelope):
    """The brief's one line: what it holds, counted from its cards ("6 drafts to approve, 20 videos, 3 notes.")."""
    parts = []
    for block in (envelope or {}).get("blocks") or ():
        kind = block.get("type")
        if kind == "review_queue":
            drafts = sum(1 for item in block.get("items") or () if item.get("draft"))
            items = len(block.get("items") or ())
            parts.append(f"{drafts} {'draft' if drafts == 1 else 'drafts'} to approve" if drafts
                         else f"{items} {'conversation' if items == 1 else 'conversations'} sorted")
        elif kind == "media_list":
            n = len(block.get("items") or ())
            parts.append(f"{n} {'video' if n == 1 else 'videos'}")
        elif kind == "table":
            n = len(block.get("rows") or ())
            parts.append(f"{n} {'row' if n == 1 else 'rows'}")
        elif kind == "checklist":
            n = len(block.get("items") or ())
            parts.append(f"{n} {'check' if n == 1 else 'checks'}")
    notes = sum(1 for block in (envelope or {}).get("blocks") or () if block.get("type") == "markdown")
    if notes:
        parts.append(f"{notes} {'note' if notes == 1 else 'notes'}")
    return ("Your brief: " + ", ".join(parts) + ".") if parts else "Your brief is ready."


def run(going, step, values, *, sources, key=None):
    if not sources:
        raise batch.Stop("failed", "There's nothing to put in the brief: no task before it finished.")
    envelope, mapping, answer = compose(sources)
    if envelope is None:
        raise batch.Stop("failed", "The tasks before the brief found nothing to put in it.")
    runtime, workflow, record = going.runtime, going.workflow, going.record
    title = step.get("title") or workflow.get("name") or "Brief"
    first = sources[0][1]
    # Done only when each task it holds is done (proof on the screen, or you marked it OK); its proof is theirs.
    verified = all(run_outcome(run) == "done" for _, run in sources)
    proof = [item for _, run in sources for item in (_summary(run).get("proof") or ())[:2]
             if isinstance(item, dict)][:6]
    summary = {"event": "result", "status": "completed", "outcome": "done" if verified else "check",
               "engine": None, "answer": answer, "reason": None if verified else
               "Mobster put the brief together, but one of its tasks finished without proof on the screen. Check it.",
               "independently_verified": verified, "actions": 0, "decisions": 0, "elapsed_ms": 0, "model": None,
               "full_answer": "\n\n".join(
                   f"**{_short(label, 120)}**\n"
                   f"{(_summary(run).get('full_answer') or _summary(run).get('answer') or '').strip()}"
                   for label, run in sources)[:12000],
               "blocks": envelope, "proof": proof if verified else [],
               "brief": {"title": title, "runs": [run.id for _, run in sources], "sources": mapping,
                         "spentUsd": round(going.spent, 6), "seconds": round(going.clock() - going.started, 1),
                         "workflowId": workflow.get("id")}}
    extras = {"threadId": record["threadId"]} if record.get("threadId") else {}
    origin = getattr(first, "origin", None) or "workflow"
    key = key or f"{record['key']}:b{record['id']}:{KIND}"[:128]
    made = runtime.compose_result(app=dict(first.app), goal=f"Put together {title}", summary=summary,
                                  idempotency_key=key, extras=extras, origin=origin,
                                  device=(getattr(first, "device_id", None), getattr(first, "device_name", None)))
    # The workflow's last result is the brief: Home's card, its PDF and the run's Needs you read it.
    going.last = made
    going._publish({"event": "batch_notification", "title": "Your brief is ready", "body": answer[:240],
                    "runId": made.id})
    return {"answer": answer, "line": f"Put together {title}"}


def register():
    if KIND not in batch.step_kinds():
        batch.register_step(KIND, validate, run)


def clean_niche(value):
    """The niche as the trends task says it: plain words, no braces (a blank's mark), 1 to NICHE_CHARS characters."""
    if not isinstance(value, str):
        raise ValueError("Say your niche, like home cooking or streetwear")
    niche = " ".join(re.sub(r"[{}\[\]<>]", " ", value).split())
    if not 1 <= len(niche) <= NICHE_CHARS:
        raise ValueError(f"Say your niche in 1 to {NICHE_CHARS} characters, like home cooking")
    return niche


def morning_brief(niche, at="08:00", timezone=None, app_id=INSTAGRAM):
    niche = clean_niche(niche)
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", at or "")
    if not match:
        raise ValueError("Choose a time like 08:00")
    hour, minute = int(match[1]), int(match[2])
    body = {"name": "Morning brief", "appId": app_id, "appName": "Instagram",
            "cron": f"{minute} {hour} * * *", "enabled": True, "sameConversation": True,
            "produces": ["review_queue", "media_list", "markdown"],
            "steps": [{"id": "dms", "kind": "task", "text": DM_TASK, "appId": app_id},
                      {"id": "trends", "kind": "task", "text": TRENDS_TASK.format(niche=niche), "appId": app_id},
                      {"id": "ideas", "kind": "task", "text": IDEAS_TASK, "appId": app_id},
                      {"id": "brief", "kind": KIND}],
            "limits": {"maxSpendUsd": 2.0, "maxMinutes": 60}}
    if timezone:
        body["timezone"] = timezone
    return body


TEMPLATES = {MORNING_BRIEF: morning_brief}


def create_from_template(workflows, name, body):
    """POST /api/workflows/templates/{name}: the template's workflow, saved and scheduled (Workflows.create)."""
    if name not in TEMPLATES:
        raise APIError("There's no job template by that name.", 404, "template_not_found")
    if not isinstance(body, dict) or set(body) - {"niche", "time", "timezone"}:
        raise ValueError("A Morning brief takes niche, time (HH:MM) and timezone")
    made = TEMPLATES[name](body.get("niche"), body.get("time") or "08:00", body.get("timezone"))
    try:
        return workflows.create(made)
    except ValueError as error:
        if str(error) == app_missing(INSTAGRAM):
            raise ValueError("The Morning brief runs in Instagram, and Mobster doesn't see Instagram on your iPhone. "
                             "Install it or open it once, then set up the brief again.") from None
        raise
