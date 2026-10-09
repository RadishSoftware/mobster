"""Which facts go with a task (SPEC §3.3 F1–F2): at most 12, at most 1,200 characters.

- Pinned facts always go: the ones for every app, and an app's own whenever that app is in play.
- The rest go when they share words with the task: the goal, and the names of the apps in play (the start app and
  the conversation's last app). Rare words count more than common ones (idf), so "gym" beats "my".
- An app's facts go only when that app is in play: it's the start app, the conversation's last app, or the goal
  names it. Then they rank ahead of other matches.

Scoring all of them in Python is enough: there are at most 500 facts, so it takes about a millisecond, and there is no
search index to keep in step with the table (the spec's FTS5 table isn't needed at this size).
"""

from functools import lru_cache
import math
import re

from .store import app_name

MAX_FACTS = 12
MAX_CHARS = 1200
TITLE = "What the user asked Mobster to remember (their words; the screen wins when it disagrees)"

_WORD = re.compile(r"[a-z0-9]+(?:['’][a-z]+)?")
STOPWORDS = frozenset("""
a about above after again all also am an and any are as at be because been before being below between both but by
can could did do does doing done down during each few for from further had has have having he her here hers herself
him himself his how i i'm if in into is it its itself just me more most my myself no nor not now of off on once only
or other our ours out over own same she should so some such than that the their them then there these they this
those through to too under until up very was we were what when where which while who whom why will with would you
your yours yourself please thanks thank ok okay can't don't let let's get got go going make one two app apps want
""".split())


def stem(word):
    """A light English stemmer: "texts" and "texting" both become "text", "closes" and "closed" "clos"."""
    word = re.sub(r"['’]s$", "", word)
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 4 and word.endswith("es") and word[:-2].endswith(("s", "x", "z", "ch", "sh")):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ed"):
        word = word[:-2]
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    return word


def tokens(text):
    """The words of ``text`` that carry meaning, stemmed. Numbers count ("5th", "2"), stopwords don't."""
    out = set()
    for word in _WORD.findall(str(text or "").lower().replace("’", "'")):
        if word in STOPWORDS or (len(word) < 3 and not any(ch.isdigit() for ch in word)):
            continue
        out.add(stem(word))
    return out


@lru_cache(maxsize=1024)
def fact_tokens(text):
    """``tokens(text)`` for a fact, kept: the same facts are scored for every task, so after the first task in a
    process scoring 500 of them takes about a millisecond instead of four."""
    return frozenset(tokens(text))


def _bundle(fact):
    scope = fact.get("scope") or ""
    return scope[4:] if scope.startswith("app:") else None


def _named_in(bundle, goal_tokens):
    name = app_name(bundle)
    if not name:
        return False
    words = tokens(name)
    return bool(words) and words <= goal_tokens


def select(facts, goal, apps=()):
    """The facts for a task with ``goal`` while ``apps`` (bundle ids) are in play, in the order they're shown.
    ``facts``: rows from MemoryStore.all_facts()."""
    goal_tokens = tokens(goal)
    in_play = {bundle for bundle in apps if bundle}
    query = set(goal_tokens)
    for bundle in in_play:
        query |= tokens(app_name(bundle) or "")
    eligible = []
    for fact in facts:
        bundle = _bundle(fact)
        if bundle is not None and bundle not in in_play and not _named_in(bundle, goal_tokens):
            continue
        eligible.append(fact)
    if not eligible:
        return []
    words_of = {fact["id"]: fact_tokens(fact["text"]) for fact in eligible}
    frequency = {}
    for words in words_of.values():
        for word in words:
            frequency[word] = frequency.get(word, 0) + 1
    count = len(eligible)

    def score(fact):
        overlap = words_of[fact["id"]] & query
        value = sum(math.log(1 + count / frequency[word]) for word in overlap)
        if value and _bundle(fact) is not None:
            value += 3.0
        elif _bundle(fact) is not None:
            value = 2.0          # an app's own fact, with that app in play, goes even without a shared word
        return value

    pinned = sorted((fact for fact in eligible if fact.get("pinned")),
                    key=lambda fact: (_bundle(fact) is not None, -(fact.get("updated_at") or 0)))
    scored = sorted(((score(fact), fact) for fact in eligible if not fact.get("pinned")),
                    key=lambda pair: (-pair[0], -(pair[1].get("last_used_at") or 0), -(pair[1].get("updated_at") or 0)))
    chosen, chars = [], 0
    for fact in pinned + [fact for value, fact in scored if value > 0]:
        if len(chosen) >= MAX_FACTS:
            break
        size = len(render_line(fact)) + (1 if chosen else 0)    # a newline between lines
        if chars + size > MAX_CHARS:
            continue    # a shorter one further down may still fit
        chosen.append(fact)
        chars += size
    return chosen


def render_line(fact):
    bundle = _bundle(fact)
    if bundle:
        return f"- In {app_name(bundle) or bundle}: {fact['text']}"
    return f"- {fact['text']}"


def render(facts):
    """The block's text: one line per fact, at most MAX_CHARS."""
    return "\n".join(render_line(fact) for fact in facts)[:MAX_CHARS]
