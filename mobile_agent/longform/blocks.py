"""A long task's results as result blocks: the report's ``blocks`` envelope, which the run view draws as cards."""

from collections import Counter

from .notebook import cut

VIDEO_KINDS = ("reel", "tiktok", "short")
NOUNS = {"reel": "Reels", "tiktok": "TikToks", "short": "Shorts", "post": "posts"}
MAX_ITEMS = 60
MAX_BLOCKS = 8
MIN_FORMAT_COUNT = 2


def review_queue(session, app_names=None):
    """The inbox sweep's review queue as a review_queue block, or None when it read no conversation."""
    from .inbox import OPENER_BUCKETS, REPLY_BUCKETS, queue_report, title, wants_openers
    queue = queue_report(session, app_names)
    items = []
    for row in queue["items"]:
        if not row["to"]:
            continue    # a queue item goes to someone the screen named; without one it can't be a draft to approve
        items.append({key: value for key, value in {
            "id": row["id"], "bucket": row["bucket"], "to": row["to"], "handle": row.get("handle"), "app": row["app"],
            "bundleId": row["bundleId"],
            "context": row["summary"], "lastMessage": row["lastMessage"], "draft": row["draft"],
            "decision": row["decision"], "frameId": row["frameId"]}.items() if value is not None})
    if not items:
        return None
    budget = getattr(session, "budget", None)
    return {"type": "review_queue", "id": "queue", "title": title(session, getattr(budget, "item_noun", None)),
            "action": "send_message", "items": items,
            "buckets": [{"id": bucket, "label": label} for bucket, label in (
                OPENER_BUCKETS if wants_openers(getattr(session, "request", "")) else REPLY_BUCKETS)]}


def _kept(session, kinds):
    book = getattr(session, "notebook", None)
    return [item for item in (book.kept_items() if book is not None else ()) if item.kind in kinds]


def media_list(session):
    """The kept videos as a media_list block: what each was about, its creator, its key frame and its sound."""
    kept = _kept(session, VIDEO_KINDS)[:MAX_ITEMS]
    items = []
    for item in kept:
        extract = item.extract or {}
        title = extract.get("topic") or extract.get("summary") or item.text.get("caption")
        if extract.get("format"):
            title = f"{title}: {extract['format']}" if title else extract["format"]
        entry = {"id": item.id, "title": cut(title, 200) or None, "creator": item.who() or None,
                 "frameId": item.frame_id, "sounds": [cut(item.text["audio"], 80)] if item.text.get("audio") else None}
        entry = {key: value for key, value in entry.items() if value}
        if len(entry) > 1:
            items.append(entry)
    if not items:
        return None
    noun = NOUNS.get(kept[0].kind, "videos")
    return {"type": "media_list", "id": "media", "title": f"{len(items)} {noun} watched", "items": items}


def formats_chart(session):
    """How many kept videos share each format, when at least two formats repeat: a bar chart."""
    counts = Counter(" ".join(str((item.extract or {}).get("format") or "").casefold().split())
                     for item in _kept(session, VIDEO_KINDS))
    counts.pop("", None)
    top = [(name, count) for name, count in counts.most_common(8) if count >= MIN_FORMAT_COUNT]
    if len(top) < 2:
        return None
    total = len(_kept(session, VIDEO_KINDS))
    noun = NOUNS.get(_kept(session, VIDEO_KINDS)[0].kind, "videos")
    return {"type": "chart", "id": "formats", "title": f"Formats in {total} {noun}", "kind": "bar",
            "x": [name[:1].upper() + name[1:] for name, _ in top],
            "series": [{"label": noun, "values": [c for _, c in top]}]}


def posts_table(session):
    """The kept posts as a table: creator, topic and what each says, each row with its frame."""
    kept = _kept(session, ("post",))[:MAX_ITEMS]
    rows = []
    for item in kept:
        extract = item.extract or {}
        row = {"cells": {"creator": item.who() or "", "topic": cut(extract.get("topic"), 80),
                         "says": cut(extract.get("summary") or item.text.get("caption"), 300)}}
        if item.frame_id:
            row["frameId"] = item.frame_id
        if any(row["cells"].values()):
            rows.append(row)
    if not rows:
        return None
    return {"type": "table", "id": "posts", "title": f"{len(rows)} posts", "rows": rows,
            "columns": [{"key": "creator", "label": "Creator", "type": "text"},
                        {"key": "topic", "label": "Topic", "type": "text"},
                        {"key": "says", "label": "What it says", "type": "text"}]}


def for_session(session, app_names=None):
    """The blocks a long task's report carries, in the order the run view shows them; [] when it has none."""
    from .inbox import wants_queue
    blocks = []
    if wants_queue(getattr(session, "request", "")):
        blocks.append(review_queue(session, app_names))
    blocks += [formats_chart(session), media_list(session), posts_table(session)]
    return [block for block in blocks if block][:MAX_BLOCKS]


def attach(session, report, app_names=None):
    """The report with ``blocks`` (the envelope {"v": 1, "blocks": [...]})
    when the task has any; unchanged otherwise."""
    if not isinstance(report, dict):
        return report
    try:
        blocks = for_session(session, app_names)
    except Exception:  # noqa: BLE001 -- the report stands without its cards
        blocks = []
    if blocks:
        report["blocks"] = {"v": 1, "blocks": blocks}
    return report
