"""RECALL (DESIGN §6.4, §8.3): the model searches this conversation's earlier tasks and this task's notebook."""

import re

from ..agent_hooks import Prepared, SkillResult
from .notebook import cut

OP = "RECALL"
PROMPT = ("- RECALL text: search this conversation's earlier tasks and this task's notebook for text. The matching "
          "lines come back on the next turn.")
MAX_LINES = 12
LINE_CHARS = 240
STOP = frozenset({"the", "and", "for", "with", "that", "this", "what", "which", "from", "about", "was", "were", "are",
                  "you", "your", "my", "one", "ones", "did", "does", "its", "it's", "of", "to", "in", "on", "a", "an"})
NO_TEXT = "RECALL needs the words to look for in text."
NOTHING = "RECALL found nothing for {query!r} in this conversation or this task's notebook."


def words(query):
    return [w for w in re.findall(r"[a-z0-9@#$'.-]{2,}", str(query or "").casefold()) if w.strip(".-'") not in STOP]


def notebook_lines(book, label=None):
    """(line, recency) for a notebook's items and facts; ``label`` ("turn 3") marks an earlier run's."""
    out = []
    prefix = f"{label}, " if label else ""
    with book.lock if hasattr(book, "lock") else _Null():
        for index, item in enumerate(book.items):
            if item.skip is not None:
                continue
            extract = item.extract or {}
            text = " ".join(str(x) for x in (item.who(), item.summary(), " ".join(extract.get("tags") or ()),
                                             " ".join(str(c) for c in extract.get("claims") or ()),
                                             item.text.get("caption") or "") if x)
            out.append((f"[{prefix}{item.id}] {text}", index))
        for index, fact in enumerate(book.facts):
            out.append((f"[{prefix}{fact.id}] {fact.text}", len(book.items) + index))
    return out


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def thread_lines(ctx):
    """(line, recency) for the thread's earlier exchanges, and their notebooks while retained."""
    thread_id = getattr(ctx, "thread_id", None)
    if not thread_id:
        return []
    try:
        from ..threads.context import exchanges
        from ..threads.service import service_for
    except ImportError:
        return []
    service = service_for(ctx.runtime)
    if service is None:
        return []
    try:
        done = exchanges(service.store.items(thread_id), exclude_run=ctx.run_id)
    except Exception:  # noqa: BLE001
        return []
    out = []
    journal = getattr(ctx.runtime, "journal", None)
    for turn, exchange in enumerate(done, 1):
        label = f"turn {turn}"
        recency = turn * 1000
        out.append((f"[{label}] asked: {exchange['ask']}", recency))
        context = exchange.get("context") or {}
        answer = context.get("answer") or (exchange.get("result") or {}).get("answer")
        if answer:
            out.append((f"[{label}] answer: {answer}", recency + 1))
        for note in context.get("notes") or ():
            out.append((f"[{label}] note: {note}", recency + 2))
        for referent in context.get("referents") or ():
            out.append((f"[{label}] {referent.get('rank')}. {referent.get('text')}", recency + 3))
        if context.get("report"):
            for line in str(context["report"]).splitlines():
                out.append((f"[{label}] report: {line}", recency + 4))
        try:
            from .notebook import Notebook
            from .store import load_notebook
            data = load_notebook(journal, exchange["runId"])
        except Exception:  # noqa: BLE001
            data = None
        if data:
            book = Notebook.from_dict(data)
            out += [(line, recency + 5) for line, _ in notebook_lines(book, label)]
    return out


def search(query, lines):
    """The best MAX_LINES of ``lines`` for ``query``: most query words matched, then the newest."""
    wanted = words(query)
    if not wanted:
        return []
    scored = []
    for line, recency in lines:
        folded = line.casefold()
        score = sum(1 for word in wanted if word in folded)
        if score:
            scored.append((score, recency, line))
    scored.sort(key=lambda entry: (-entry[0], -entry[1]))
    best = scored[:MAX_LINES]
    best.sort(key=lambda entry: entry[1])
    return [cut(line, LINE_CHARS) for _, _, line in best]


class Recall:
    """agent_hooks.Skill: RECALL."""

    op = OP
    prompt = PROMPT
    needs_target = False

    def __init__(self, ctx):
        self.ctx = ctx

    def prepare(self, ctx, act, snapshot):
        return Prepared(None)   # reading notes approves nothing

    def perform(self, ctx, prepared, act, snapshot):
        query = " ".join(str(act.get("text") or act.get("target_label") or "").split())[:200]
        if not query:
            return SkillResult(NO_TEXT)
        from .session import for_run
        lines = thread_lines(self.ctx)
        session = for_run(self.ctx.run_id)
        if session is not None:
            lines += [(line, 10 ** 7 + recency) for line, recency in notebook_lines(session.notebook)]
        found = search(query, lines)
        if not found:
            return SkillResult(NOTHING.format(query=query))
        from ..harness_api import fence
        text = "\n".join(found).replace("<<<", "‹‹‹").replace(">>>", "›››")
        return SkillResult(fence(f"RECALL found {len(found)} lines for {query!r}", text), changed=False)


def factory(ctx):
    """register_tool's factory: RECALL in a long run, or in a run in a thread with earlier tasks; else None."""
    if getattr(ctx, "engine", None) != "smart":
        return None
    from .session import for_run
    if for_run(ctx.run_id) is not None:
        return Recall(ctx)
    thread_id = getattr(ctx, "thread_id", None)
    if not thread_id:
        return None
    try:
        from ..threads.service import service_for
        service = service_for(ctx.runtime)
        earlier = service is not None and any(
            item.get("kind") == "run" and item.get("runId") != ctx.run_id for item in service.store.items(thread_id))
    except Exception:  # noqa: BLE001
        earlier = False
    return Recall(ctx) if earlier else None


EARLIER_RUNS = 5


def earlier_identities(runtime, thread_id, run_id, kind=None):
    """The item identities the conversation's earlier long tasks (the newest EARLIER_RUNS, of ``kind`` when given)
    saw and kept, from their notebooks while their runs are retained: a "scroll again" skips what the person already
    has."""
    if not thread_id:
        return set()
    try:
        from ..threads.context import exchanges
        from ..threads.service import service_for
        from .store import load_notebook
        service = service_for(runtime)
        if service is None:
            return set()
        done = exchanges(service.store.items(thread_id), exclude_run=run_id)
    except Exception:  # noqa: BLE001
        return set()
    journal = getattr(runtime, "journal", None)
    found, runs = set(), 0
    for exchange in reversed(done):
        if not (exchange.get("context") or {}).get("report"):
            continue
        data = load_notebook(journal, exchange["runId"])
        if not data or (kind and data.get("kind") != kind):
            continue
        found |= {item.get("identity") for item in data.get("items") or ()
                  if item.get("identity") and item.get("skip") in (None, "off_topic")}
        found |= set(data.get("earlier") or ())
        runs += 1
        if runs == EARLIER_RUNS:
            break
    return found
