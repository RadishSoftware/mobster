"""What a finished run leaves in its thread: its public result, what the next task can refer to, its private context."""

import re

from .. import secret_filter

NOTE_CHARS = 600
ANSWER_CONTEXT_CHARS = 600
FACTS = 6
FACT_CHARS = 200


def group_fields(group):
    """A group item's fields from fanout's public group: its phones (each with its run, state and why it didn't
    start), the combined state and counts, the warnings, the cost estimate and, once every phone is done, the merged
    summary."""
    members = [{key: member.get(key) for key in ("device", "deviceName", "runId", "state", "error", "code")}
               for member in group["members"]]
    return {"groupId": group["id"], "goal": group["goal"], "members": members, "state": group["state"],
            "counts": group["counts"], "warning": group.get("warning"), "held": group.get("held"),
            "estimateUsd": group.get("estimateUsd"), "estimateTotalUsd": group.get("estimateTotalUsd"),
            "summary": group.get("summary"), "finishedAt": group.get("finishedAt")}


def result_of(summary, run=None):
    """The run item's public result: status, outcome, the answer's headline, facts, cost and time."""
    summary = summary if isinstance(summary, dict) else {}
    facts = []
    for entry in summary.get("proof") or ():
        # A quote of only spaces is no proof (the app's runOutcome trims it too), so it never becomes a fact.
        if not isinstance(entry, dict) or not str(entry.get("quote") or "").strip():
            continue
        fact = secret_filter.redact(" ".join(str(entry["quote"]).split()))[:FACT_CHARS]
        if entry.get("app"):
            fact += f" ({str(entry['app'])[:40]})"
        facts.append(fact)
        if len(facts) >= FACTS:
            break
    answer = summary.get("answer")
    cost = summary.get("costUsd")
    if cost is None and run is not None:
        cost = getattr(run, "cost_usd", None)
    elapsed = summary.get("elapsed_ms")
    return {"status": summary.get("status") or (getattr(run, "status", None) or "error"),
            "outcome": summary.get("outcome") if isinstance(summary.get("outcome"), str) else None,
            "answer": secret_filter.redact(answer)[:1000] if isinstance(answer, str) else None,
            "facts": facts, "costUsd": cost if isinstance(cost, (int, float)) else None,
            "elapsedMs": elapsed if isinstance(elapsed, (int, float)) else None}


REFERENTS = 12
NUMBERED = re.compile(r"^\s*(\d+)[.)]\s+(.+)$")
BULLETED = re.compile(r"^\s*[-•*]\s+(.+)$")


def referents_of(summary):
    """Up to REFERENTS numbered things a run produced, so "the second one" resolves later (DESIGN §8.1)."""
    summary = summary if isinstance(summary, dict) else {}
    out = []
    report = summary.get("report")
    if isinstance(report, dict) and report.get("ranked"):
        from ..longform.report import referents
        out = referents(report)
    if not out:
        full = summary.get("full_answer") or (summary.get("data") if isinstance(summary.get("data"), str) else None) \
            or summary.get("answer") or ""
        numbered, bulleted = [], []
        for line in str(full).splitlines():
            found = NUMBERED.match(line)
            if found:
                numbered.append({"rank": int(found.group(1)), "text": found.group(2)})
                continue
            found = BULLETED.match(line)
            if found:
                bulleted.append({"rank": len(bulleted) + 1, "text": found.group(1)})
        out = numbered or bulleted
    if not out:
        quotes = [entry.get("quote") for entry in summary.get("proof") or () if isinstance(entry, dict)
                  and entry.get("quote")]
        out = [{"rank": n, "text": quote} for n, quote in enumerate(quotes, 1)] if len(quotes) >= 2 else []
    clean = []
    for ref in out[:REFERENTS]:
        text = secret_filter.redact(" ".join(str(ref.get("text") or "").split()).strip("*"))[:200]
        if text and not secret_filter.is_secret(text):
            clean.append({"rank": int(ref.get("rank") or len(clean) + 1), "text": text})
    return clean


def context_of(summary, private, last_app):
    """What the next task in the thread reads about this one (private: never sent by the API)."""
    full = summary.get("full_answer") or (summary.get("data") if isinstance(summary.get("data"), str) else None) \
        or summary.get("answer")
    notes, total = [], 0
    for note in (private or {}).get("notes") or ():
        if not isinstance(note, str) or not note.strip():
            continue
        clean = secret_filter.redact(" ".join(note.split()))
        if secret_filter.is_secret(clean):
            continue
        if total + len(clean) > NOTE_CHARS:
            clean = clean[:max(0, NOTE_CHARS - total - 1)] + "…"
        if len(clean) > 1:
            notes.append(clean)
            total += len(clean)
        if total >= NOTE_CHARS:
            break
    out = {"notes": notes, "lastApp": last_app}
    if isinstance(full, str) and full.strip():
        out["answer"] = secret_filter.redact(" ".join(full.split()))[:ANSWER_CONTEXT_CHARS]
    report = summary.get("report")
    if isinstance(report, dict):
        # A long task's report outlives the run's retention here: its TL;DR, headings and shortlist (DESIGN §8.3).
        from ..longform.report import short_form
        short = short_form(report)
        if short:
            out["report"] = secret_filter.redact(short)
    return out
