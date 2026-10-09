"""Suggestions to remember something, read out of what the person wrote (SPEC §3.3 F3).

Deterministic: no model call, so no prompt changes and no cost. It looks for the few ways people say something worth
keeping, sentence by sentence:

- "remember (that) …", "don't forget (that) …", "keep in mind …"  ->  the rest, as they said it
- "my X is Y"                       ->  "My X is Y" (X up to four words; never a passing state such as "my battery
                                        is low", never inside a question, never inside a task such as "text Sam that
                                        my train is late")
- "I always / never / usually / prefer …", "I'd rather …"  ->  as said (a preference, so it's suggested pinned)
- "call me …"                       ->  "Call me …" (suggested pinned)

A suggestion is only that: nothing is stored until the person accepts it (store.MemoryStore.resolve_proposal).
"""

from dataclasses import dataclass
import re

from ..secret_filter import is_secret

MAX_PER_TEXT = 2
MIN_CHARS = 8

_BREAK = re.compile(r"[.!?;]+\s+|\n+")
# The word right before a full stop. Searched in a short window before the break (the lookbehind still sees the text
# before it), so a message full of "Dr. Dr. Dr." stays linear: a 4,000-character one took half a second before.
_LAST_WORD = re.compile(r"(?<![\w.])([\w.]+)[.]$")
_WORD_WINDOW = 24
# A full stop after these doesn't end the sentence ("Dr. Lee on Valencia St. by the park").
_ABBREVIATIONS = {"dr", "mr", "mrs", "ms", "mx", "prof", "st", "ave", "rd", "blvd", "apt", "no", "vs", "etc", "jr",
                  "sr", "mt", "ft", "e.g", "i.e", "approx", "dept", "inc", "ltd", "co"}
_REMEMBER = re.compile(r"^(?:(?:please|pls|and|also|oh|ok|okay|so|hey)[,\s]+)*"
                       r"(?:(?:can|could|would|will) you\s+)?(?:please\s+)?"
                       r"(?:remember|don'?t forget|don’t forget|do not forget|keep in mind)\b[,:]?\s*"
                       r"(?:that\s+)?(?P<rest>.+)$", re.I)
_MY_IS = re.compile(r"\bmy\s+(?P<thing>(?:[\w'’-]+\s+){0,3}?[\w'’-]+)\s+(?P<verb>is|are)\s+(?P<value>.+)$", re.I)
_PREFERENCE = re.compile(r"\b(?P<rest>I(?:\s+(?:always|never|usually|prefer|would rather)|['’]d rather)\s+"
                         r"(?P<next>[\w'’-]+).*)$", re.I)
_CALL_ME = re.compile(r"\b(?i:call me)\s+(?P<name>[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)?)")

# "my X is Y" about the moment, not about the person: never worth suggesting.
_PASSING = {"battery", "phone", "iphone", "screen", "order", "package", "delivery", "flight", "train", "bus",
            "ride", "uber", "car", "wifi", "wi-fi", "internet", "connection", "data", "storage", "volume",
            "alarm", "timer", "meeting", "call", "inbox", "cart", "code", "password", "passcode", "pin", "otp",
            "account", "app", "message", "email", "text", "reply", "question", "answer", "task", "request",
            "food", "coffee", "lunch", "dinner", "breakfast", "day", "week", "morning", "night", "plan", "plans",
            "schedule", "calendar", "location", "card", "balance", "bill", "payment", "verification", "turn",
            "time", "guess", "point", "bad", "fault", "pleasure", "problem", "issue", "mistake", "head", "back"}
_PASSING_VALUE = re.compile(r"^(?:low|dead|dying|charging|broken|late|early|running|on|off|full|empty|down|up|"
                            r"out|here|there|ready|done|gone|lost|stuck|frozen|slow|fast|open|closed|busy|free|"
                            r"almost|nearly|about|now|today|tomorrow|tonight|still|already|not|being|getting|"
                            r"going|coming|at\s+\d|\d+\s*%|\d+\s*percent)\b", re.I)
_PRONOUN_VALUE = re.compile(r"^(?:it|this|that|these|those|them|him|her)\b", re.I)
_QUESTION_START = re.compile(r"^(?:what|when|where|who|whom|which|why|how|is|are|does|do|did|can|could|would|"
                             r"should|will|whats|what's)\b", re.I)
# Words that start a task ("text Sam that my train is late"): what follows is the task's, not the person's.
_COMMAND = re.compile(r"^(?:text|send|message|call|email|reply|tell|ask|book|buy|order|post|delete|open|find|check|"
                      r"show|look|search|set|turn|add|remind|schedule|play|go|get|make|write|create|share|pay|move|"
                      r"copy|read|say|let)\b", re.I)
# "I never got your text": something that happened, not a preference.
_PAST = re.compile(r"^(?:\w+ed|got|saw|heard|went|did|had|was|were|said|sent|made|knew|thought|told|took|came|"
                   r"found|gave|left|read|ran|bought|paid|met|forgot|lost|won|ate|drank|wrote|spoke)$", re.I)
_WHEN_WORDS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "tomorrow", "today",
               "tonight", "later", "back", "asap", "when", "if", "after", "before", "at", "on", "in", "around"}


@dataclass(frozen=True)
class Candidate:
    text: str
    pin: bool = False       # a preference or a name: it helps in every task, so it's suggested pinned


def _tidy(text):
    text = " ".join(text.split()).strip(" ,;:-–—\"'“”‘’")
    text = re.sub(r"[.!]+$", "", text).strip()
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def _inside_task(before):
    """The words before a match make it part of a task or a quote ("Text Sam that …", "Tell her I always …")."""
    before = before.strip(" ,")
    if not before:
        return False
    words = re.sub(r"^(?:(?:please|pls|and|also|oh|ok|okay|so|hey|then|now)[,\s]+)*", "", before, flags=re.I)
    return bool(_COMMAND.match(words) or re.search(r"\b(?:that|say|says|said|saying|tell|telling|asks?)$", before,
                                                   re.I))


def _sentences(text):
    start = 0
    for found in _BREAK.finditer(text):
        word = _LAST_WORD.search(text, max(start, found.start() - _WORD_WINDOW), found.start() + 1)
        if found.group().startswith(".") and word and (word.group(1).lower() in _ABBREVIATIONS
                                                        or re.fullmatch(r"[A-Z]", word.group(1))):
            continue
        part = text[start:found.start() + (0 if found.group()[0] == "\n" else 1)].strip()
        start = found.end()
        if part:
            yield part
    part = text[start:].strip()
    if part:
        yield part


def _remember(sentence):
    found = _REMEMBER.match(sentence)
    if not found:
        return None
    rest = found.group("rest")
    # "remember to buy milk" is a reminder, not something about the person; a question asks, it doesn't tell.
    if re.match(r"^to\s", rest, re.I) or sentence.rstrip().endswith("?"):
        return None
    return Candidate(_tidy(rest), pin=bool(_PREFERENCE.match(rest.strip()) or _CALL_ME.match(rest.strip())))


def _call_me(sentence):
    found = _CALL_ME.search(sentence)
    if not found or _inside_task(sentence[:found.start()]):
        return None
    name = found.group("name")
    if name.split()[0].lower() in _WHEN_WORDS:
        return None
    return Candidate(f"Call me {name}", pin=True)


def _preference(sentence):
    found = _PREFERENCE.search(sentence)
    if not found or _inside_task(sentence[:found.start()]) or _PAST.match(found.group("next")):
        return None
    return Candidate(_tidy(found.group("rest")), pin=True)


def _my_is(sentence):
    found = _MY_IS.search(sentence)
    if not found or _inside_task(sentence[:found.start()]):
        return None
    thing, verb, value = found.group("thing"), found.group("verb").lower(), found.group("value").strip()
    words = [re.sub(r"['’]s$", "", word) for word in thing.lower().split()]
    lasting = any(word in ("favorite", "favourite", "usual", "go-to", "regular", "preferred") for word in words)
    if not lasting and any(word in _PASSING for word in words):
        return None
    if _PASSING_VALUE.match(value) or _PRONOUN_VALUE.match(value) or len(value) < 2:
        return None
    return Candidate(_tidy(f"My {thing} {verb} {value}"))


def _from_sentence(sentence):
    """At most one candidate per sentence: the clearest of the patterns."""
    remembered = _remember(sentence)
    if remembered is not None or _REMEMBER.match(sentence):
        return remembered
    if sentence.rstrip().endswith("?") or _QUESTION_START.match(sentence):
        return None
    return _call_me(sentence) or _preference(sentence) or _my_is(sentence)


def candidates(text):
    """Up to two suggestions from ``text`` (a goal, or a message sent while a task worked). Never a secret, never
    shorter than 8 or longer than 300 characters."""
    if not isinstance(text, str) or not text.strip():
        return []
    out, seen = [], set()
    for sentence in _sentences(text[:4000]):
        candidate = _from_sentence(sentence)
        if candidate is None:
            continue
        if not MIN_CHARS <= len(candidate.text) <= 300 or is_secret(candidate.text):
            continue
        key = candidate.text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
        if len(out) >= MAX_PER_TEXT:
            break
    return out


# A task joined onto what to remember ("…, and text Sam I'm on my way"): the message is a task, not only a note.
_JOINED_TASK = re.compile(r"(?:,|;|\band\b|\bthen\b|\balso\b)\s+(?:please\s+)?" + _COMMAND.pattern[1:], re.I)


def remember_only(text):
    """The things to remember when ``text`` asks only that ("Remember that my gym is the one on 5th Street", "Don't
    forget I sign my texts with Sam"), else None.

    Such a message is a note for Mobster, not a task for the phone: the conversation offers to remember it and runs
    nothing. A message that also asks for something ("…, and text Sam"), asks a question, or says "remember to …"
    (a reminder, which is a task) is None and runs as a task. A secret comes back too, so the caller can say it isn't
    kept rather than run it."""
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        return None
    found = []
    for sentence in _sentences(text):
        candidate = _remember(sentence)
        if candidate is None or _JOINED_TASK.search(sentence[_REMEMBER.match(sentence).start("rest"):]):
            return None
        if len(candidate.text) < 3:
            return None
        found.append(candidate)
    return found or None
