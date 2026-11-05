"""What a pack read in this run, kept in front of the model on every later turn (a harness_api context provider)."""

import threading
from collections import OrderedDict, deque

from .. import harness_api

KEY = "packs"
MAX_CHARS = 4000
READS = 3                   # the newest reads kept per run
RUNS = 64
_reads = OrderedDict()      # run id -> deque of (title, lines)
_lock = threading.Lock()


def keep(run_id, title, lines):
    """A tool's feedback reaches the model for one turn only, so the run's reads are kept here for every later turn."""
    if not run_id or not lines:
        return
    with _lock:
        kept = _reads.pop(run_id, None) or deque(maxlen=READS)
        kept = deque((entry for entry in kept if entry[0] != title), maxlen=READS)
        kept.append((title, lines))
        _reads[run_id] = kept
        while len(_reads) > RUNS:
            _reads.popitem(last=False)


def reads(run_id):
    with _lock:
        return list(_reads.get(run_id) or ())


def forget(run_id):
    with _lock:
        _reads.pop(run_id, None)


class Provider:
    key = KEY
    max_chars = MAX_CHARS

    def blocks(self, ctx):
        forget(ctx.run_id)   # a run id is used once; nothing of an earlier run with the same id is shown
        return ()

    def turn_blocks(self, ctx, turn):
        kept = reads(ctx.run_id)
        if not kept:
            return ()
        text, left = [], MAX_CHARS
        for title, lines in reversed(kept):          # newest first, so the newest survives the cap
            part = f"{title}:\n{lines}"
            if len(part) > left:
                part = part[:max(0, left - 1)] + "…"
            text.insert(0, part)
            left -= len(part)
            if left <= 0:
                break
        return (harness_api.ContextBlock(KEY, "What Mobster read in the app earlier in this task", "\n\n".join(text),
                                         stable=False, untrusted=True),)


def factory(packs):
    """register_context_provider's factory: a Provider for a Smart run any pack's tools are offered to."""
    def make(ctx):
        return Provider() if any(pack.offered_to(ctx) for pack in packs) else None
    return make
