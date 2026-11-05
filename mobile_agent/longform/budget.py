"""Budgets for long tasks (DESIGN §3): what "for 10 minutes", "30 reels" or "every match" means, decided by code."""

import math
import re
from dataclasses import asdict, dataclass, replace

# What a person spends on one item, in seconds: the human-equivalent conversion.
HUMAN_SECONDS_PER_ITEM = {"reel": 12, "tiktok": 10, "short": 12, "post": 8, "profile": 45, "chat": 30, "row": 20,
                          "result": 20}
MINUTES_RANGE = (1.0, 120.0)
ITEMS_RANGE = (1, 500)
# "every match", "all my DMs": up to ALL_CAP items, which the plan card can raise to ALL_MAX.
ALL_CAP, ALL_MAX = 40, 100
DEFAULT_ITEMS = {"feed": 30, "browse": 20}
# The longest a long task may hold the phone when nothing else bounds it (a chain, a survey).
DEFAULT_PHONE_SECONDS = {"feed": 600.0, "survey": 900.0, "chain": 900.0, "browse": 600.0}

NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
                "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "hundred": 100,
                "a dozen": 12, "a couple": 2, "a couple of": 2, "a few": 5, "several": 5}
UNITS = {"s": 1 / 60, "sec": 1 / 60, "secs": 1 / 60, "second": 1 / 60, "seconds": 1 / 60, "m": 1.0, "min": 1.0,
         "mins": 1.0, "minute": 1.0, "minutes": 1.0, "h": 60.0, "hr": 60.0, "hrs": 60.0, "hour": 60.0,
         "hours": 60.0}
# Collection nouns -> (item kind, the noun the plan card shows).
NOUNS = {"reels": ("reel", "Reels"), "reel": ("reel", "Reels"), "videos": ("reel", "videos"),
         "video": ("reel", "videos"), "shorts": ("short", "Shorts"), "tiktoks": ("tiktok", "TikToks"),
         "tiktok": ("tiktok", "TikToks"), "posts": ("post", "posts"), "profiles": ("profile", "profiles"),
         "matches": ("profile", "matches"), "messages": ("chat", "messages"), "chats": ("chat", "chats"),
         "conversations": ("chat", "chats"), "dms": ("chat", "DMs"), "emails": ("row", "emails"),
         "results": ("result", "results"), "places": ("result", "places"), "restaurants": ("result", "restaurants"),
         "listings": ("result", "listings"), "items": ("row", "items"), "match": ("profile", "matches"),
         "profile": ("profile", "profiles"), "dm": ("chat", "DMs"), "chat": ("chat", "chats"),
         "message": ("chat", "messages"), "email": ("row", "emails"), "result": ("result", "results"),
         "place": ("result", "places"), "restaurant": ("result", "restaurants"), "listing": ("result", "listings"),
         "post": ("post", "posts")}

_NUM = r"(\d+(?:\.\d+)?|" + "|".join(sorted((re.escape(w) for w in NUMBER_WORDS), key=len, reverse=True)) + r")"
# Item counts take digits or a real number word: "a short video" or "a profile" is no count.
_COUNT = r"(\d+|" + "|".join(sorted((re.escape(w) for w in NUMBER_WORDS if w not in (
    "a", "an", "a couple", "a couple of", "a few", "several")), key=len, reverse=True)) + r")"
_UNIT = r"(" + "|".join(sorted(UNITS, key=len, reverse=True)) + r")\b"
_LEAD = (
    r"(?:for|about|around|spend|spending|scroll(?:ing)? for|watch(?:ing)? for|over|during|keep going for|"
    r"nonstop for)")
TIME = re.compile(rf"\b{_LEAD}\s+(?:the\s+(?:full|whole|entire)\s+|a\s+good\s+)?{_NUM}\s*-?\s*{_UNIT}", re.I)
TIME_PHRASES = ((re.compile(r"\b(?:for|about|around|spend|over)\s+(?:the\s+(?:full|whole|entire)\s+)?half an hour\b",
                            re.I), 30.0),
                (re.compile(r"\b(?:for|about|around|spend|over)\s+(?:the\s+(?:full|whole|entire)\s+)?an hour\b", re.I),
                 60.0),
                (re.compile(r"\b(?:for|about|around|spend|over)\s+a few minutes\b", re.I), 5.0),
                (re.compile(r"\b(?:for|about|around|spend|over)\s+a couple (?:of )?minutes\b", re.I), 2.0))
STRICT = re.compile(r"\b(?:the\s+(?:full|whole|entire)|keep going for|nonstop for)\b", re.I)
# "the whole 10 minutes" with no lead word ("watch the whole 10 minutes of Reels"): strict time on its own.
STRICT_TIME = re.compile(rf"\bthe\s+(?:full|whole|entire)\s+{_NUM}\s*-?\s*{_UNIT}", re.I)
ITEMS = re.compile(r"\b(?:the\s+(?:next|first|last|top)\s+|next\s+|first\s+|top\s+)?" + _COUNT
                   + r"\s+(?:[a-z-]+\s+){0,2}?("
                   + "|".join(sorted(NOUNS, key=len, reverse=True)) + r")\b", re.I)
ALL = re.compile(r"\b(?:every|each of my|all (?:of )?my|go(?:ing)? through (?:all (?:of )?)?my|my matches|my dms|"
                 r"all the|all\s+(?:" + "|".join(sorted(NOUNS, key=len, reverse=True)) + r"))\b", re.I)


@dataclass(frozen=True)
class Budget:
    kind: str                 # "time" | "items" | "all" | "default"
    minutes: float | None     # what the person said (time)
    items: int | None         # item target (time is converted)
    strict: bool              # True: run the whole time, no item target
    phone_seconds_cap: float  # never hold the phone longer than this
    item_noun: str            # "Reels", "TikToks", "profiles", "chats", "results"
    label: str                # "About 50 Reels", "Up to 40 matches", "10 minutes"
    item_kind: str = "reel"   # HUMAN_SECONDS_PER_ITEM's key
    clamped: bool = False     # the request's number was outside the range and was moved into it

    def public(self):
        """The plan card's camelCase shape."""
        return {"kind": self.kind, "minutes": self.minutes, "items": self.items, "strict": self.strict,
                "phoneSecondsCap": round(self.phone_seconds_cap), "itemNoun": self.item_noun, "label": self.label,
                "clamped": self.clamped}

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        fields = {name: value.get(name) for name in ("kind", "minutes", "items", "strict", "phone_seconds_cap",
                                                     "item_noun", "label", "item_kind", "clamped")}
        fields["strict"] = bool(fields["strict"])
        fields["clamped"] = bool(fields["clamped"])
        fields["item_kind"] = fields["item_kind"] or "reel"
        fields["phone_seconds_cap"] = float(fields["phone_seconds_cap"] or 600.0)
        return cls(**fields)


def _number(word):
    word = word.casefold().strip()
    if word in NUMBER_WORDS:
        return float(NUMBER_WORDS[word])
    try:
        return float(word)
    except ValueError:
        return None


def noun_in(goal):
    """(item kind, card noun) for the collection the request names, or (None, None)."""
    folded = goal.casefold()
    best = None
    for word, value in NOUNS.items():
        found = re.search(rf"\b{re.escape(word)}\b", folded)
        if found and (best is None or found.start() < best[0]):
            best = (found.start(), value)
    return best[1] if best else (None, None)


# A time that is the object of the task, not its budget: "set a timer for 10 minutes", "snooze for 5 minutes".
NOT_A_BUDGET = re.compile(r"\b(timers?|alarms?|countdown|snooze|remind(?:er|ers)?(?: me)?)\b[^,;.]{0,24}$", re.I)


def _minutes(goal):
    """(minutes, strict) the request asks for, or (None, False)."""
    for pattern, minutes in TIME_PHRASES:
        found = pattern.search(goal)
        if found and not NOT_A_BUDGET.search(goal[:found.start()]):
            return minutes, bool(STRICT.search(found.group(0)))
    found = TIME.search(goal) or STRICT_TIME.search(goal)
    if not found or NOT_A_BUDGET.search(goal[:found.start()]):
        return None, False
    number = _number(found.group(1))
    if number is None:
        return None, False
    return number * UNITS[found.group(2).casefold()], bool(STRICT.search(found.group(0)))


def parse_budget(goal):
    """The request's budget, or None when it names none (the plan then picks its kind's default)."""
    text = " ".join(str(goal or "").split())
    if not text:
        return None
    kind_noun = noun_in(text)
    item_kind, noun = kind_noun if kind_noun[0] else ("reel", "items")
    minutes, strict = _minutes(text)
    if minutes is not None:
        return time_budget(minutes, item_kind, noun, strict=strict)
    found = ITEMS.search(text)
    if found:
        number = _number(found.group(1))
        if number is not None and number >= 1:
            item_kind, noun = NOUNS[found.group(2).casefold()]
            return items_budget(int(number), item_kind, noun)
    if ALL.search(text):
        return all_budget(item_kind, noun)
    return None


def time_budget(minutes, item_kind="reel", noun="Reels", *, strict=False):
    clamped = not MINUTES_RANGE[0] <= minutes <= MINUTES_RANGE[1]
    minutes = min(max(minutes, MINUTES_RANGE[0]), MINUTES_RANGE[1])
    minutes = round(minutes, 2)
    cap = minutes * 60
    if strict:
        return Budget("time", minutes, None, True, cap, noun, minutes_label(minutes), item_kind, clamped)
    items = human_items(cap, item_kind)
    return Budget("time", minutes, items, False, cap, noun, f"About {items} {noun}", item_kind, clamped)


def items_budget(count, item_kind="reel", noun="Reels"):
    clamped = not ITEMS_RANGE[0] <= count <= ITEMS_RANGE[1]
    count = min(max(int(count), ITEMS_RANGE[0]), ITEMS_RANGE[1])
    from .costs import ITEM_S
    cap = max(120.0, min(7200.0 - 300, 120 + count * ITEM_S.get(item_kind, 3.0) * 3))
    return Budget("items", None, count, False, cap, noun, f"{count} {noun}", item_kind, clamped)


def all_budget(item_kind="profile", noun="matches", cap_items=ALL_CAP):
    cap_items = min(max(int(cap_items), 1), ALL_MAX)
    from .costs import ITEM_S
    cap = max(300.0, 120 + cap_items * ITEM_S.get(item_kind, 3.0) * 3)
    return Budget("all", None, cap_items, False, cap, noun, f"Up to {cap_items} {noun}", item_kind)


def human_items(seconds, item_kind):
    """How many items a person sees in ``seconds``."""
    return max(1, math.ceil(seconds / HUMAN_SECONDS_PER_ITEM.get(item_kind, 12)))


def minutes_label(minutes):
    if minutes >= 60 and minutes % 60 == 0:
        hours = int(minutes // 60)
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if minutes < 1:
        return f"{round(minutes * 60)} seconds"
    whole = int(minutes) if float(minutes).is_integer() else minutes
    return f"{whole} minute{'s' if whole != 1 else ''}"


def for_kind(kind, parsed, item_kind, noun):
    """The budget a plan of ``kind`` runs with: the request's own (``parsed``), else the kind's default."""
    if parsed is not None:
        if parsed.kind == "time" and parsed.item_noun == "items" and noun != "items":
            # "for 10 minutes" with the noun named elsewhere ("Scroll TikTok for 10 minutes")
            return time_budget(parsed.minutes, item_kind, noun, strict=parsed.strict)
        if parsed.kind == "all" and parsed.item_noun == "items" and noun != "items":
            return all_budget(item_kind, noun)
        return parsed
    if kind == "survey":
        return all_budget(item_kind, noun)
    if kind == "chain":
        return Budget("default", None, None, False, DEFAULT_PHONE_SECONDS["chain"], noun, "Each step once",
                      item_kind)
    count = DEFAULT_ITEMS.get(kind, 20)
    budget = items_budget(count, item_kind, noun)
    return replace(budget, kind="default", label=f"About {count} {noun}")


def override(budget, change):
    """``budget`` changed by the plan card or the API's ``longform.budget`` ({items?, minutes?, strict?})."""
    if not change:
        return budget
    if not isinstance(change, dict) or set(change) - {"items", "minutes", "strict"}:
        raise ValueError("longform.budget takes items, minutes and strict")
    items, minutes, strict = change.get("items"), change.get("minutes"), change.get("strict")
    if strict is not None and type(strict) is not bool:
        raise ValueError("longform.budget.strict is true or false")
    if minutes is not None:
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or \
                not MINUTES_RANGE[0] <= minutes <= MINUTES_RANGE[1]:
            raise ValueError("longform.budget.minutes is between 1 and 120")
        return time_budget(float(minutes), budget.item_kind, budget.item_noun, strict=bool(strict))
    if items is not None:
        if isinstance(items, bool) or not isinstance(items, int) or not ITEMS_RANGE[0] <= items <= ITEMS_RANGE[1]:
            raise ValueError("longform.budget.items is a whole number from 1 to 500")
        if budget.kind == "all":
            return all_budget(budget.item_kind, budget.item_noun, min(items, ALL_MAX))
        return items_budget(items, budget.item_kind, budget.item_noun)
    if strict is not None and budget.minutes is not None:
        return time_budget(budget.minutes, budget.item_kind, budget.item_noun, strict=strict)
    return budget


def from_cli(value):
    """``--budget``: 10m, 90s, 1h, 30 (items) or all. ValueError with a sentence otherwise."""
    text = str(value or "").strip().casefold()
    if text == "all":
        return {"items": ALL_CAP}
    found = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|hr)?", text)
    if not found:
        raise ValueError("--budget is a time (10m, 90s, 1h), a number of items (30) or all")
    number, unit = float(found.group(1)), found.group(2)
    if unit is None:
        if not number.is_integer() or not ITEMS_RANGE[0] <= number <= ITEMS_RANGE[1]:
            raise ValueError("--budget takes 1 to 500 items")
        return {"items": int(number)}
    minutes = number * UNITS[unit]
    if not MINUTES_RANGE[0] <= minutes <= MINUTES_RANGE[1]:
        raise ValueError("--budget takes 1 minute to 2 hours")
    return {"minutes": round(minutes, 2)}


def turn_caps(budget, steps=3):
    """(max_steps, max_seconds) for a long run (DESIGN §3.3)."""
    items = budget.items or 0
    max_steps = min(600, 60 + 12 * max(1, steps) + math.ceil(items / 10))
    max_seconds = min(7200.0, budget.phone_seconds_cap + 300)
    return max_steps, max_seconds
