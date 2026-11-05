"""The report (DESIGN §11.1): its schema, the checks code makes on what the model wrote, and its Markdown."""

import re

from .notebook import FEED_KINDS, cut

MAX_SECTIONS, MAX_BULLETS, MAX_RANKED = 6, 6, 12
AUDIO_GAP = "Mobster read captions, on-screen text and a few frames of each video. It doesn't hear the audio."
# A source chip's label: "Reel 12", "Profile 3".
KIND_LABELS = {"reel": "Reel", "tiktok": "TikTok", "short": "Short", "post": "Post", "profile": "Profile",
               "chat": "Chat", "row": "Item", "result": "Result"}
REPORT_KEY = "report"

_STR = {"type": "string"}
_IDS = {"type": "array", "items": {"type": "string"}}
SCHEMA = {"type": "object", "additionalProperties": False, "required": [REPORT_KEY], "properties": {REPORT_KEY: {
    "type": "object", "additionalProperties": False, "required": ["title", "tldr", "sections", "ranked", "gaps"],
    "properties": {
        "title": _STR, "tldr": _STR,
        "sections": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["heading", "bullets"],
            "properties": {"heading": _STR, "bullets": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["text", "sources"],
                "properties": {"text": _STR, "sources": _IDS}}}}}},
        "ranked": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["rank", "itemId", "name", "why", "cautions", "state"],
            "properties": {"rank": {"type": "integer"}, "itemId": _STR, "name": _STR, "why": _IDS,
                           "cautions": _IDS, "state": {"type": ["string", "null"]}}}},
        "gaps": _IDS}}}}


def validate(raw, notebook, *, kind):
    """The model's report checked against the notebook: (report, notes of what was removed)."""
    raw = raw if isinstance(raw, dict) else {}
    known = {item.id: item for item in notebook.items}
    kept = {item_id for item_id, item in known.items() if item.skip is None}
    # A note the agent wrote (a fact, a step's result, its final answer) is a source too: a chain has few items.
    kept |= {fact.id for fact in notebook.facts}
    notes = []
    sections = []
    for section in (raw.get("sections") or ())[:MAX_SECTIONS * 2]:
        if not isinstance(section, dict):
            continue
        bullets = []
        for bullet in section.get("bullets") or ():
            if not isinstance(bullet, dict) or not str(bullet.get("text") or "").strip():
                continue
            sources = []
            for source in bullet.get("sources") or ():
                source = str(source).strip()
                if source in kept and source not in sources:
                    sources.append(source)
                elif source in known or source:
                    notes.append(f"dropped source {source[:12]}")
            if not sources:
                notes.append("dropped a bullet with no source")
                continue
            bullets.append({"text": cut(bullet["text"], 400), "sources": sources[:8]})
            if len(bullets) == MAX_BULLETS:
                break
        heading = cut(section.get("heading"), 120)
        if bullets and heading:
            sections.append({"heading": heading, "bullets": bullets})
        if len(sections) == MAX_SECTIONS:
            break
    ranked = []
    if kind == "survey":
        for row in raw.get("ranked") or ():
            if not isinstance(row, dict):
                continue
            item_id = str(row.get("itemId") or "").strip()
            if item_id not in known or known[item_id].skip is not None:
                notes.append("dropped a shortlist row with no source")
                continue
            ranked.append({"rank": len(ranked) + 1, "itemId": item_id,
                           "name": cut(row.get("name") or known[item_id].who(), 80),
                           "why": [cut(w, 200) for w in row.get("why") or () if str(w).strip()][:4],
                           "cautions": [cut(c, 200) for c in row.get("cautions") or () if str(c).strip()][:3],
                           "state": cut(row.get("state"), 40) or None})
            if len(ranked) == MAX_RANKED:
                break
    report = {"title": cut(raw.get("title") or "", 160), "tldr": cut(raw.get("tldr") or "", 600),
              "kind": notebook.kind, "sections": sections, "ranked": ranked,
              "gaps": [cut(g, 200) for g in raw.get("gaps") or () if str(g).strip()][:6]}
    return report, notes


def finish(report, notebook, *, partial=False, stop_reason=None, phone_seconds=None, report_seconds=None,
           cost_usd=None, failed=False):
    """Code's part of the report: stats, sources, partial, stopReason and the gaps code knows."""
    counters = notebook.counters
    cited = []
    for section in report.get("sections") or ():
        for bullet in section.get("bullets") or ():
            cited += [s for s in bullet.get("sources") or () if s not in cited]
    cited += [row["itemId"] for row in report.get("ranked") or () if row["itemId"] not in cited]
    sources = {}
    facts = {fact.id: fact for fact in notebook.facts}
    for item_id in cited:
        fact = facts.get(item_id)
        if fact is not None:
            sources[item_id] = {"creator": None, "name": None, "title": None, "frameId": None,
                                "summary": cut(fact.text, 200), "n": int(item_id[1:]), "kind": "Note",
                                "caption": None, "app": None, "note": True}
            continue
        item = notebook.item(item_id)
        if item is None:
            continue
        sources[item_id] = {"creator": item.source.get("creator"), "name": item.source.get("name"),
                            "title": item.source.get("title"), "frameId": item.frame_id,
                            "summary": item.summary() or None, "n": item.n,
                            "kind": f"{KIND_LABELS.get(item.kind, 'Item')} {item.n}",
                            "caption": cut(item.text.get("caption"), 300) or None,
                            "app": item.app}
    gaps = list(report.get("gaps") or [])
    kinds = {item.kind for item in notebook.items}
    if kinds & FEED_KINDS and AUDIO_GAP not in gaps:
        gaps.append(AUDIO_GAP)
    if counters.get("seen_before"):
        line = (f"{counters['seen_before']} {'was' if counters['seen_before'] == 1 else 'were'} already in an earlier "
                f"task in this conversation, so this covers only what's new since then.")
        if line not in gaps:
            gaps.insert(0, line)
    if counters.get("extract_failed"):
        line = f"{counters['extract_failed']} items couldn't be read in full; the report uses what was on screen."
        if line not in gaps:
            gaps.append(line)
    from .receipts import overall
    receipts = notebook.to_dict(items=False)["receipts"]
    report = {**report, "gaps": gaps[:8], "partial": bool(partial), "stopReason": stop_reason,
              "outcomes": {"status": overall(receipts, notebook.run_id), "currentRunId": notebook.run_id,
                           "historyOmitted": notebook.receipt_runs_omitted, "runs": receipts},
              "stats": {"items": counters.get("kept", 0), "seen": counters.get("seen", 0),
                        "ads": counters.get("ads", 0),
                        "repeats": counters.get("repeats", 0), "offTopic": counters.get("off_topic", 0),
                        "seenBefore": counters.get("seen_before", 0),
                        "phoneSeconds": round(phone_seconds) if phone_seconds is not None else None,
                        "reportSeconds": round(report_seconds) if report_seconds is not None else None,
                        "costUsd": round(cost_usd, 4) if cost_usd is not None else None},
              "sources": sources}
    if failed:
        # The deterministic report (no prose): the app shows "Mobster couldn't write the report. Here's what it saw."
        report["synthesisFailed"] = True
        report["fallback"] = True
    return report


STOP_WORDS = {"items": None, "phone_time": "the phone time it was given ran out",
              "spend": "it reached the spending cap", "stopped": "you stopped it", "error": "it hit an error",
              "blocked": "it couldn't go on", "max_steps": "it used all its turns", "timeout": "it ran out of time",
              "budget": "it reached the spending cap"}


def partial_line(report, budget):
    """"Stopped after 31 of 50 Reels. This covers those 31." for a partial report, else None."""
    if not report.get("partial"):
        return None
    kept = (report.get("stats") or {}).get("items") or 0
    if not kept and report.get("kind") == "chain":
        # A chain keeps no items: say why it stopped ("it made no progress (nothing new in 6 turns)").
        stop = report.get("stopReason")
        return f"Stopped before it finished: {stop}." if stop else "Stopped before it finished."
    noun = budget.item_noun if budget is not None else "items"
    if budget is not None and budget.items and not budget.strict:
        return f"Stopped after {kept} of {budget.items} {noun}. This covers those {kept}."
    return f"Stopped after {kept} {noun}. This covers those {kept}."


def deterministic(notebook, *, title=None):
    """A report with no prose, from the notebook alone: items grouped by their first tag (the synthesis failed)."""
    groups, order = {}, []
    for item in notebook.kept_items():
        extract = item.extract or {}
        tag = str((extract.get("tags") or [extract.get("topic") or "Other"])[0] or "Other").strip() or "Other"
        if tag not in groups:
            groups[tag] = []
            order.append(tag)
        groups[tag].append(item)
    order.sort(key=lambda t: -len(groups[t]))
    sections = []
    for tag in order[:MAX_SECTIONS]:
        bullets = [{"text": cut(f"{item.who() + ': ' if item.who() else ''}{item.summary() or 'no text read'}", 300),
                    "sources": [item.id]} for item in groups[tag][:MAX_BULLETS]]
        sections.append({"heading": cut(tag[:1].upper() + tag[1:], 80), "bullets": bullets})
    notes = [fact for fact in notebook.facts if fact.kind != "step_result"]
    if notes and len(sections) < MAX_SECTIONS:
        # What the agent noted and did (a chain's steps, its final answer), newest last.
        sections.append({"heading": "What Mobster noted", "bullets": [
            {"text": cut(fact.text, 300), "sources": [fact.id]} for fact in notes[-MAX_BULLETS:]]})
    kept = notebook.counters.get("kept", 0)
    return {"title": title or f"What Mobster saw in {kept} items", "tldr": "", "kind": notebook.kind,
            "sections": sections, "ranked": [],
            "gaps": ["Mobster couldn't write the report, so this lists what it saw."]}


def _who(source, item_id):
    """The name a Markdown bullet cites: the creator, the person or the title; None for the agent's own note."""
    if not source:
        return item_id
    if source.get("note"):
        return None
    return source.get("creator") or source.get("name") or source.get("title") or item_id


def markdown(report, budget=None):
    """The report as Markdown, for the CLI and Copy or Save as Markdown."""
    lines = [f"# {report.get('title') or 'Report'}", ""]
    partial = partial_line(report, budget)
    if partial:
        lines += [f"*{partial}*", ""]
    if report.get("tldr"):
        lines += [report["tldr"], ""]
    outcomes = report.get("outcomes") or {}
    if outcomes.get("runs"):
        lines += ["## Requested outcomes", "", f"Current run: {outcomes.get('status', 'unverified')}.", ""]
        current = outcomes.get("currentRunId")
        runs = sorted(outcomes["runs"], key=lambda run: run.get("runId") != current)
        for run in runs:
            lines += [f"### {'Current' if run.get('runId') == current else 'Earlier'} task {run.get('runId')}"
                      + ("" if run.get("final") else " (checkpoint only)"), ""]
            for item in run.get("items") or ():
                state = "avoid-action constraint" if item.get("kind") == "FORBID" else item.get("outcome", "unverified")
                line = (f"- {item.get('what') or item.get('kind')}: "
                        f"{state}")
                if item.get("requested"):
                    line += f"; requested: {item['requested']}"
                if item.get("recordedValue"):
                    line += f"; recorded value: {item['recordedValue']}"
                lines.append(line)
                for source in item.get("evidence") or ():
                    label = "Screen change" if source.get("kind") == "screen_change" else "Screen evidence"
                    lines.append(f"  - {label} ({source.get('runId')}, turn {source.get('turn') or '?'}): "
                                 f"{source.get('text')}")
                if item.get("evidenceOmitted"):
                    lines.append(f"  - {item['evidenceOmitted']} additional receipt excerpts omitted.")
            lines.append("")
        if outcomes.get("historyOmitted"):
            lines.append(f"{outcomes['historyOmitted']} older task receipt snapshots omitted.")
        lines.append("")
    sources = report.get("sources") or {}
    for section in report.get("sections") or ():
        lines += [f"## {section['heading']}", ""]
        for bullet in section.get("bullets") or ():
            names = []
            for item_id in bullet.get("sources") or ():
                who = _who(sources.get(item_id), item_id)
                if who and who not in names:
                    names.append(who)
            lines.append(f"- {bullet['text']}" + (f" ({', '.join(names[:4])})" if names else ""))
        lines.append("")
    if report.get("ranked"):
        lines += ["## Shortlist", ""]
        for row in report["ranked"]:
            state = f" ({row['state']})" if row.get("state") else ""
            why = "; ".join(row.get("why") or ())
            cautions = "; ".join(row.get("cautions") or ())
            lines.append(f"{row['rank']}. **{row['name']}**{state}" + (f": {why}" if why else "")
                         + (f". Cautions: {cautions}" if cautions else ""))
        lines.append("")
    if report.get("gaps"):
        lines += ["## Not covered", ""] + [f"- {gap}" for gap in report["gaps"]] + [""]
    stats = stats_line(report, budget)
    if stats:
        lines.append(stats)
    return "\n".join(lines).rstrip() + "\n"


def stats_line(report, budget=None):
    stats = report.get("stats") or {}
    noun = budget.item_noun if budget is not None else "items"
    parts = []
    if stats.get("items") is not None:
        parts.append(f"{stats['items']} {noun}")
    if stats.get("phoneSeconds") is not None:
        from .context import duration
        parts.append(f"{duration(stats['phoneSeconds'])} on your iPhone")
    if stats.get("costUsd") is not None:
        parts.append(f"${stats['costUsd']:.2f}")
    if stats.get("reportSeconds") is not None:
        parts.append(f"report in {stats['reportSeconds']} s")
    return (", ".join(parts) + ".") if parts else ""


def first_sentence(text):
    text = " ".join(str(text or "").split())
    found = re.search(r"(?<=[.!?])\s", text)
    return text[:found.start()] if found else text


def short_form(report, limit=1200):
    """The thread's copy (``_context.report``): the TL;DR, the section headings and the shortlist."""
    if not isinstance(report, dict):
        return None
    lines = [cut(report.get("title"), 160)]
    if report.get("tldr"):
        lines.append(cut(report["tldr"], 500))
    headings = [section.get("heading") for section in report.get("sections") or () if section.get("heading")]
    if headings:
        lines.append("Sections: " + "; ".join(headings))
    for row in report.get("ranked") or ():
        lines.append(f"{row['rank']}. {row['name']}" + (f": {'; '.join(row.get('why') or ())}" if row.get("why")
                                                        else ""))
    text = "\n".join(line for line in lines if line)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def referents(report):
    """Up to 12 numbered things the report produced (its shortlist), for "the second one" later."""
    out = []
    for row in (report or {}).get("ranked") or ():
        reason = (row.get("why") or [None])[0]
        out.append({"rank": row["rank"], "text": cut(row["name"] + (f": {reason}" if reason else ""), 200)})
        if len(out) == 12:
            break
    return out


def proof(report, apps=None, limit=3):
    """The result's proof for a long task: up to ``limit`` of the report's first sources, each as the Mac app quotes
    proof ({"quote", "app", "screen", "frameId"}): what was on screen, where it was read, and its key frame."""
    out = []
    sources = (report or {}).get("sources") or {}
    order = []
    for section in (report or {}).get("sections") or ():
        for bullet in section.get("bullets") or ():
            order += [s for s in bullet.get("sources") or () if s not in order]
    order += [row.get("itemId") for row in (report or {}).get("ranked") or () if row.get("itemId") not in order]
    for item_id in order:
        source = sources.get(item_id) or {}
        if source.get("note"):
            continue
        quote = source.get("caption") or source.get("summary")
        if not quote:
            continue
        who = source.get("creator") or source.get("name")
        bundle = source.get("app")
        app = (apps or {}).get(bundle) or bundle
        out.append({"quote": cut((f"{who}: " if who and not str(quote).startswith(str(who)) else "") + quote, 300),
                    "app": app, "screen": source.get("kind") or item_id, "frameId": source.get("frameId")})
        if len(out) == limit:
            break
    return out
