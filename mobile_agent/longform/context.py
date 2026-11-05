"""What the navigation model sees of a long task (DESIGN §5.2, §5.3, §6.1, §6.2)."""

import re

from .notebook import cut

LONGFORM_SYSTEM = """This is a long task with a budget and a notebook.
- The code keeps a notebook for you and writes the final report from it after you finish. Your DONE answer needs only \
one or two sentences; the report does the rest.
- Use the macros for repeated work. On a full-screen vertical video feed (Reels, TikTok, Shorts) use WATCH_FEED; do \
not swipe through videos one by one. For a list whose rows each open a page (matches, chats, results) use READ_EACH. \
Navigate yourself to where a macro starts: search the topic, open the right tab, tap the first video.
- When a macro returns, read its summary: if the feed drifted off the topic, search again; if the budget is covered, \
finish.
- Put what you learn in notes_add, citing items by id ("i12, i15: two drills for footwork"). Facts, not narration.
- Set step to the plan step you are working on. If a step can't be done, say why in notes_add and go on to the next \
step unless it depends on this one.
- The budget line says how much is left. When it says wrap up, finish with DONE."""

EPOCH_HEAD = "Notebook (the code keeps everything here for the report; you see a summary):"
DELTA_HEAD = "Notebook since the summary above:"
EPOCH_FACTS, EPOCH_ITEMS = 15, 6
DELTA_FACTS, DELTA_ITEMS = 10, 6
EPOCH_CHARS, DELTA_CHARS = 9000, 3000
EPOCH_EVERY = 10            # new items or facts that make a new epoch
COMPACT_EVERY = 8           # history lines per log line
LOG_SHOWN = 6
STEP_WORDS = {"done": "done", "now": "now", "next": "next", "skipped": "skipped", "failed": "failed"}


def plan_steps(plan):
    """The stable prompt's lines for the plan's steps."""
    steps = (plan or {}).get("steps") or ()
    if not steps:
        return ""
    lines = [f"{s['id']}. {s['title']}" + (f" ({s['app']})" if s.get("app") else "") for s in steps]
    text = ("\n\nPlan steps (set step to the one you work on; the code marks them done as you move on):\n"
            + "\n".join(lines))
    from .inbox import wants_drafts, wants_openers
    if (plan or {}).get("kind") == "survey" and wants_drafts((plan or {}).get("title")):
        # Real iPhone, 13 Oct: a DM sweep read the inbox's previews with READ_LIST and finished without opening one
        # conversation, so it drafted replies to messages it never saw in full.
        text += SWEEP_NOTE
    elif (plan or {}).get("kind") == "survey" and wants_openers((plan or {}).get("title")):
        text += OPENERS_NOTE
    return text


SWEEP_NOTE = ("\nThis is an inbox sweep: sort and draft from the conversations themselves. Use READ_EACH on the first "
              "conversation row of each list (newest first) so code opens every one; READ_LIST reads only the "
              "previews, which are not enough to sort or draft from. Check the other inbox tabs (requests, general) "
              "the same way. Finish when READ_EACH reaches the end of the lists or the budget.")


OPENERS_NOTE = ("\nThis drafts a first message to each person who fits: code drafts them from the posts themselves. "
                "Use READ_EACH on the first post row of the list so code opens every post; READ_LIST reads only the "
                "previews. Never open a conversation or send anything: the person reviews every draft. Finish when "
                "READ_EACH reaches the end of the list or the budget.")


def counts_line(notebook):
    c = notebook.counters
    return (f"Counts: {c['seen']} seen, {c['kept']} kept, {c['ads']} ads skipped, {c['repeats']} repeats skipped, "
            f"{c['off_topic']} off topic.")


def steps_line(notebook):
    if not notebook.steps:
        return ""
    parts = []
    for step in notebook.steps:
        parts.append(f"{step['id']} {STEP_WORDS.get(step['state'], step['state'])}")
    return "Steps: " + ". ".join(parts) + "."


def item_line(item):
    who = item.who()
    label = f"{who}: " if who else ""
    state = {None: "kept", "ad": "ad, skipped", "repeat": "repeat, skipped", "off_topic": "off topic",
             "unreadable": "unreadable"}[item.skip]
    summary = item.summary()
    return f"- {item.id} {label}" + (f"\"{cut(summary, 120)}\" " if summary else "") + f"({state})"


def fact_line(fact):
    ids = f"[{', '.join(fact.item_ids[:6])}] " if fact.item_ids else ""
    return f"- {fact.id} {ids}{cut(fact.text, 220)}"


def render_epoch(notebook):
    """The notebook's summary for the cached epoch (at most about 2,500 tokens)."""
    with notebook.lock:
        lines = [EPOCH_HEAD, counts_line(notebook)]
        digest = notebook.digest or {}
        if digest.get("text"):
            lines.append("Digest: " + cut(digest["text"], 700))
        themes = digest.get("themes") or ()
        if themes:
            lines.append("Themes: " + ", ".join(f"{t.get('name')} ({len(t.get('item_ids') or ())})"
                                                for t in themes[:10]))
        facts = notebook.facts[-EPOCH_FACTS:]
        if facts:
            lines.append(f"Facts (newest {len(facts)}):")
            lines += [fact_line(f) for f in reversed(facts)]
        items = notebook.items[-EPOCH_ITEMS:]
        if items:
            lines.append(f"Recent items (newest {len(items)}):")
            lines += [item_line(i) for i in reversed(items)]
        steps = steps_line(notebook)
        if steps:
            lines.append(steps)
        text = "\n".join(lines)
    return text[:EPOCH_CHARS]


def render_delta(notebook, since):
    """What changed since the epoch ``since`` = (items, facts) counts: new facts and items, counts and steps now."""
    items_at, facts_at = since
    with notebook.lock:
        new_items = notebook.items[items_at:]
        new_facts = notebook.facts[facts_at:]
        lines = [DELTA_HEAD, counts_line(notebook).replace("Counts:", "Counts now:")]
        if new_facts:
            shown = new_facts[-DELTA_FACTS:]
            lines.append(f"New facts ({len(new_facts)}):" if len(shown) == len(new_facts)
                         else f"New facts (newest {len(shown)} of {len(new_facts)}):")
            lines += [fact_line(f) for f in reversed(shown)]
        if new_items:
            shown = new_items[-DELTA_ITEMS:]
            lines.append(f"New items ({len(new_items)}):" if len(shown) == len(new_items)
                         else f"New items (newest {len(shown)} of {len(new_items)}):")
            lines += [item_line(i) for i in reversed(shown)]
        steps = steps_line(notebook)
        if steps:
            lines.append(steps)
        if notebook.log:
            lines.append(f"Earlier turns, compacted (newest {min(LOG_SHOWN, len(notebook.log))}):")
            lines += [f"- {line}" for line in notebook.log[-LOG_SHOWN:]]
        text = "\n".join(lines)
    return text[:DELTA_CHARS]


class Epochs:
    """When the cached epoch is re-rendered (DESIGN §6.2)."""

    def __init__(self):
        self.text = None
        self.since = (0, 0)
        self.digest_key = None
        self.steps_key = None
        self.renders = 0

    def current(self, notebook):
        """(epoch text, (items, facts) at that epoch): the same text until a refresh trigger."""
        with notebook.lock:
            digest = notebook.digest or {}
            digest_key = (digest.get("turn"), digest.get("up_to_item"), digest.get("text"))
            steps_key = tuple((s["id"], s["state"]) for s in notebook.steps)
            items, facts = len(notebook.items), len(notebook.facts)
            fresh = (self.text is None or digest_key != self.digest_key or steps_key != self.steps_key
                     or items - self.since[0] >= EPOCH_EVERY or facts - self.since[1] >= EPOCH_EVERY)
            if fresh:
                self.text = render_epoch(notebook)
                self.since = (items, facts)
                self.digest_key, self.steps_key = digest_key, steps_key
                self.renders += 1
        return self.text, self.since


def money(usd):
    return f"${usd:,.2f}"


def duration(seconds):
    seconds = max(0, int(round(seconds)))
    minutes, rest = divmod(seconds, 60)
    if minutes and rest:
        return f"{minutes} min {rest} s"
    if minutes:
        return f"{minutes} min"
    return f"{rest} s"


def budget_line(session):
    """"Budget: 41 of about 50 Reels. 1 min 12 s of phone time used (cap 10 min). $0.08 spent of $1.00 (report
    reserve $0.06). Wrap up when the items are covered.\""""
    budget = session.budget
    kept = session.notebook.watched()
    parts = []
    if budget.items and not budget.strict:
        about = "about " if budget.kind in ("time", "default") else "up to " if budget.kind == "all" else ""
        parts.append(f"{kept} of {about}{budget.items} {budget.item_noun}.")
    else:
        parts.append(f"{kept} {budget.item_noun} so far.")
    parts.append(f"{duration(session.phone_seconds())} of phone time used (cap {duration(budget.phone_seconds_cap)}).")
    cap = session.cap_usd
    spent, reserve = session.spent_usd(), session.reserve_usd()
    if cap is not None:
        parts.append(f"{money(spent)} spent of {money(cap)} (report reserve {money(reserve)}).")
    else:
        parts.append(f"{money(spent)} spent (report reserve {money(reserve)}).")
    advice = session.advice()
    if advice:
        parts.append(advice)
    return "Budget: " + " ".join(parts)


HISTORY = re.compile(r"^(\d+)(?:\.(\d+))?: (.*)$")
OP = re.compile(r"^([A-Z][A-Z_]+)\b")
TEXT = re.compile(r"text='([^']{1,60})")
WATCHED = re.compile(r"(?:Watched|Read) (\d+) ")


def compact_lines(lines, apps_by_turn=None):
    """One log line for ``lines`` (history lines that left the window): the turns, the apps, op counts, macro
    outcomes, searches, refusals and failures."""
    turns, ops, searches, refusals, failures, covered = [], {}, [], 0, 0, 0
    for line in lines:
        found = HISTORY.match(str(line))
        if not found:
            continue
        turns.append(int(found.group(1)))
        rest = found.group(3)
        op = OP.match(rest)
        name = op.group(1) if op else None
        if "refused" in rest:
            refusals += 1
        elif "failed" in rest:
            failures += 1
        if name:
            ops[name] = ops.get(name, 0) + 1
        typed = TEXT.search(rest)
        if typed and name in ("TYPE_SUBMIT", "TYPE", "SET_TEXT"):
            searches.append(typed.group(1))
        watched = WATCHED.search(rest)
        if watched and name in ("WATCH_FEED", "READ_EACH"):
            covered += int(watched.group(1))
    if not turns:
        return None
    first, last = min(turns), max(turns)
    apps = []
    for turn in range(first, last + 1):
        app = (apps_by_turn or {}).get(turn)
        if app and app not in apps:
            apps.append(app)
    head = f"Turns {first}-{last}" if first != last else f"Turn {first}"
    if apps:
        head += ", " + "/".join(apps[:3])
    parts = []
    if searches:
        parts.append("typed " + ", ".join(f"'{s}'" for s in searches[:3]))
    for name in ("WATCH_FEED", "READ_EACH", "READ_LIST", "RECALL"):
        if ops.get(name):
            times = {1: "once", 2: "twice"}.get(ops[name], f"{ops[name]} times")
            parts.append(f"{name} {times}" + (f" ({covered} items)" if name in ("WATCH_FEED", "READ_EACH")
                                              and covered else ""))
    rest = {k: v for k, v in ops.items() if k not in ("WATCH_FEED", "READ_EACH", "READ_LIST", "RECALL", "TYPE_SUBMIT",
                                                        "TYPE", "SET_TEXT")}
    if rest:
        parts.append(", ".join(f"{k} x{v}" for k, v in sorted(rest.items(), key=lambda kv: -kv[1])[:4]))
    if refusals:
        parts.append(f"{refusals} refused")
    if failures:
        parts.append(f"{failures} failed")
    return head + ": " + ("; ".join(parts) if parts else "no actions") + "."


class Compactor:
    """Folds history lines that leave the prompt's window into the notebook's log, 8 at a time."""

    def __init__(self, window=16):
        self.window = window
        self.done = 0

    def fold(self, history, notebook, apps_by_turn=None):
        """Compact what is due; returns the number of new log lines."""
        added = 0
        while len(history) - self.window - self.done >= COMPACT_EVERY:
            chunk = history[self.done:self.done + COMPACT_EVERY]
            self.done += COMPACT_EVERY
            line = compact_lines(chunk, apps_by_turn)
            if line:
                notebook.add_log(line)
                added += 1
        return added
