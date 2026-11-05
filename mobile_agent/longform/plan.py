"""Detection and the plan card (DESIGN §3.5, §4.1): deterministic and model-free, so nothing is spent before Start."""

import hashlib
import re
import threading
import time
import weakref

from . import budget as B
from . import costs as C
from .notebook import FEED_KINDS

KINDS = ("feed", "survey", "chain", "browse")
# The collections a survey drafts openers over (inbox.wants_openers): posts, results and rows (READ_EACH reads them
# as rows, which the extractor reads as openers).
OPENER_KINDS = ("row", "result", "post")
PLAN_TTL_S = 15 * 60
MAX_PLANS = 64

FEED_APPS = {"instagram": ("com.burbn.instagram", "Instagram", "reel", "Reels"),
             "ig": ("com.burbn.instagram", "Instagram", "reel", "Reels"),
             "reels": ("com.burbn.instagram", "Instagram", "reel", "Reels"),
             "tiktok": ("com.zhiliaoapp.musically", "TikTok", "tiktok", "TikToks"),
             "tiktoks": ("com.zhiliaoapp.musically", "TikTok", "tiktok", "TikToks"),
             "youtube shorts": ("com.google.ios.youtube", "YouTube", "short", "Shorts"),
             "shorts": ("com.google.ios.youtube", "YouTube", "short", "Shorts"),
             "youtube": ("com.google.ios.youtube", "YouTube", "short", "Shorts")}
FEED_NOUN = re.compile(r"\b(reels?|tiktoks?|youtube shorts|shorts|my feed|for you page|fyp)\b", re.I)
FEED_APP = re.compile(r"\b(instagram|ig|tiktok|youtube)\b", re.I)
WATCH_VERB = re.compile(r"\b(scroll(?:ing)?|doom ?scroll(?:ing)?|watch(?:ing)?|brows(?:e|ing)|go(?:ing)? through|"
                        r"look(?:ing)? through|binge)\b", re.I)
# A survey reads a collection item by item: these verbs always mean that over a collection...
STRONG_SURVEY = re.compile(r"\b(go(?:ing)? through|look(?:ing)? through|review(?:ing)?|rank(?:ing)?|shortlist|triage|"
                           r"sort through|find (?:the |my )?(?:best|most|top)|most promising|which of my)\b", re.I)
# ...these only with every, all, each or a count ("read all my emails", "check 10 listings").
WEAK_SURVEY = re.compile(r"\b(read(?:ing)?|check(?:ing)?|look(?:ing)? at|summari[sz]e)\b", re.I)
EVERY = re.compile(r"\b(all|every|each)\b", re.I)
# "error messages" or "status messages" name what a screen shows, not a collection to read (found 13 Oct: a build check
# that looks for "error messages" on each screen was planned as a survey of 40 messages).
COLLECTION = re.compile(r"(?<!error )(?<!errors )(?<!warning )(?<!status )(?<!system )(?<!validation )(?<!alert )"
                        r"\b(matches|profiles|dms|direct messages|messages|chats|conversations|emails|inbox|"
                        r"listings|results|reviews|options|restaurants|places)\b", re.I)
# Verbs that change things: a bulk change is never a long read.
# A possessive ("the post's link", found 14 Oct) is a noun, never the verb.
CHANGE_VERB = re.compile(r"\b(delete|archive|remove|unsubscribe|mark|move|like|follow|unfollow|send|reply to all|"
                         r"block|report|unmatch|swipe (?:left|right)|post|pay|buy|order|favou?rite|hide|mute|accept|"
                         r"decline|approve|skip)\b(?!['’]s\b)", re.I)
# Words that say the person wants to be told something: with them, a change verb doesn't make a request a loop.
REPORT_INTENT = re.compile(r"\b(find|which|rank|shortlist|summari[sz]e|summary|draft|tell me|send me|what|best|most|"
                           r"promising|learn(?:ed|t)?|write up|list (?:each|every|all))\b", re.I)
BROWSE_VERB = re.compile(r"\b(scroll(?:ing)?|doom ?scroll(?:ing)?|brows(?:e|ing)|watch(?:ing)?|read(?:ing)?|"
                         r"look(?:ing)? (?:at|through)|go(?:ing)? through|explore|check out)\b", re.I)
RESEARCH = re.compile(r"\b(best|top \d+|compare|comparing|across|options|cheapest|highest[- ]rated|most promising)\b",
                      re.I)
CHAIN_SPLIT = re.compile(r"\s*(?:,?\s*\band then\b|,?\s*\bthen\b|"
                         r",?\s*\bafter that\b|;|\n\s*\d+[.)]\s+|^\s*\d+[.)]\s+)\s*",
                         re.I)
VERB = re.compile(r"^(?:and\s+)?(?:please\s+)?([a-z]+)", re.I)
FOCUS = (re.compile(r"\babout ([a-z0-9][\w' &-]{1,40}?)(?=\s+(?:for|and|then|on|in|from|to|,)\b|[,.;!?]|$)", re.I),
         re.compile(r"\bon ([a-z0-9][\w' &-]{1,30}?) (?:reels|tiktoks|videos|shorts)\b", re.I),
         re.compile(r"\b([a-z][\w'-]{2,24}(?: [a-z][\w'-]{2,24})?) (?:reels|tiktoks|videos|shorts)\b", re.I))
# One video named on its own ("the Reel that's open", "this TikTok", "a short about cats", "1 reel"): a normal task that
# watches it, never a feed plan. A plural ("Reels", "my feed") or a time budget is a feed.
ONE_ITEM = re.compile(r"\b(?:the|this|that|one|a|1)\s+(?:(?:open|current|next|first|last|same|playing)\s+)?"
                      r"(?:reel|tiktok|youtube short|short|video)\b", re.I)
MANY_ITEMS = re.compile(r"\b(?:reels|tiktoks|shorts|videos|my feed|for you page|fyp)\b", re.I)
NOT_FOCUS = re.compile(r"^(instagram|ig|tiktok|youtube|my|the|some|more|short|all|every|few|watch|scroll|doomscroll|"
                       r"go|through|see|ten|\d+.*|the next|next|first|top)$", re.I)


# Apps named with an everyday word (orchestrate._EVERYDAY): after another app's name, the word is that app's noun.
EVERYDAY_NAMES = frozenset({"news", "photos", "music", "clock", "files", "phone", "mail", "messages", "notes", "maps",
                            "books", "health", "wallet", "camera", "weather", "calendar", "contacts", "reminders"})


def _fold(text):
    return " ".join(str(text or "").split())


def chain_parts(goal):
    """The request's parts in order, split on then / and then / after that / ; / a numbered list."""
    parts = [p.strip(" ,.") for p in CHAIN_SPLIT.split(_fold(goal) if "\n" not in str(goal) else str(goal))]
    return [p for p in parts if p and len(p.split()) >= 2]


def _verb(part):
    found = VERB.match(part)
    return found.group(1).casefold() if found else ""


def named_apps(goal, apps):
    """[{bundleId, name}] of ``apps`` the request names (whole words), in the request's order."""
    folded = _fold(goal).casefold()
    hits = []
    for app in apps or ():
        name = str(app.get("name") or "")
        if len(name) > 2 and app.get("bundleId"):
            hit = re.search(rf"\b{re.escape(name.casefold())}\b", folded)
            if hit:
                hits.append((hit.start(), hit.end(), {"bundleId": app["bundleId"], "name": name}))
    found = []
    for start, end, app in hits:
        if app["name"].casefold() in EVERYDAY_NAMES and any(
                other is not app and re.fullmatch(r"(?:['’]s)?\s+", folded[other_end:start])
                for _, other_end, other in hits):
            continue
        found.append((start, app))
    found.sort(key=lambda pair: pair[0])
    out, seen = [], set()
    for _, app in found:
        if app["bundleId"] not in seen:
            seen.add(app["bundleId"])
            out.append(app)
    return out


def one_item(text, parsed=None):
    """Whether the request is about one video ("Watch the Reel that's open"): no feed plan for it (real-device QA,
    13 Oct: one open Reel got "Watches about 30 Reels")."""
    if parsed is not None and not (parsed.kind == "items" and parsed.items == 1):
        return False
    return bool(ONE_ITEM.search(text)) and not MANY_ITEMS.search(text)


def _single(goal, parsed):
    """feed | survey | browse | None for one part of a request."""
    text = _fold(goal)
    if CHANGE_VERB.search(text) and not REPORT_INTENT.search(text):
        return None
    collection = COLLECTION.search(text)
    counted = parsed is not None and parsed.kind in ("items", "all")
    one = one_item(text, parsed)
    if FEED_NOUN.search(text) and (WATCH_VERB.search(text) or parsed is not None) and not one:
        return "feed"
    if collection and (STRONG_SURVEY.search(text) or WEAK_SURVEY.search(text) and (EVERY.search(text) or counted)):
        return "survey"
    if FEED_APP.search(text) and WATCH_VERB.search(text) and not collection and not one:
        return "feed"
    if parsed is not None and parsed.kind in ("time", "items") and BROWSE_VERB.search(text) and not one:
        return "feed" if FEED_APP.search(text) else "browse"
    return None


def detect(goal, apps=()):
    """"feed" | "survey" | "chain" | "browse" | None."""
    text = _fold(goal)
    if not text:
        return None
    parsed = B.parse_budget(text)
    parts = chain_parts(goal)
    if len(parts) >= 2:
        verbs = {_verb(p) for p in parts}
        long_part = any(_single(p, B.parse_budget(p)) for p in parts)
        if len(verbs) >= 2 and (long_part or RESEARCH.search(text) or len(_part_apps(parts, apps)) >= 2):
            return "chain"
    # A goal that names two or more apps and passes nothing between them ("go through my messages across Messages,
    # WhatsApp and Instagram") is one plan across apps, the orchestrator's (orchestrate.apps_plan), not a long read of
    # one of them.
    from ..catalog import APPS
    from ..orchestrate import named_apps
    if len(named_apps(goal, [a for a in apps or () if a.get("name")] or APPS)) >= 2:
        return None
    return _single(text, parsed)


def _part_app(part, apps):
    """The app one part of a request works in (app_choice.infer_app over the phone's apps, else the catalog), or
    None: "check my calendar for tomorrow" is Calendar, "text the first attendee" is Messages."""
    from ..app_choice import infer_app
    from ..catalog import APPS
    known = [a for a in apps or () if a.get("id") and a.get("name")] or APPS
    try:
        found = infer_app(part, known)
    except Exception:  # noqa: BLE001 -- no app: the part names none
        return None
    return found if isinstance(found, dict) else None


def _part_apps(parts, apps):
    """The distinct apps a request's parts work in, in order."""
    out = []
    for part in parts:
        app = _part_app(part, apps)
        if app is not None and app.get("bundleId") not in [a.get("bundleId") for a in out]:
            out.append(app)
    return out


def focus_of(goal):
    """What the feed is about ("basketball"), or None."""
    text = _fold(goal)
    for pattern in FOCUS:
        for found in pattern.finditer(text):
            words = found.group(1).strip(" '-")
            words = re.sub(r"^(?:(?:the|some|my|scroll|scrolling|doomscroll|watch|watching|browse|instagram|ig|tiktok|"
                           r"youtube|through|more)\s+)+", "", words, flags=re.I)
            if words and not NOT_FOCUS.match(words) and not FEED_APP.fullmatch(words):
                return words.casefold()
    return None


def _feed_app(goal, apps):
    """(bundleId, name, item kind, noun) of the feed the request means."""
    folded = _fold(goal).casefold()
    for word, value in FEED_APPS.items():
        if re.search(rf"\b{re.escape(word)}\b", folded):
            bundle, name = value[0], value[1]
            installed = next((a for a in apps or () if a.get("bundleId") == bundle), None)
            return bundle, (installed or {}).get("name") or name, value[2], value[3]
    named = named_apps(goal, apps)
    if named:
        return named[0]["bundleId"], named[0]["name"], "reel", "videos"
    return "com.burbn.instagram", "Instagram", "reel", "Reels"


def _survey_app(goal, apps):
    """(bundleId, name) of the app a survey reads in: one the request names among the phone's apps, else the one
    app_choice infers from the request (the catalog's names and words), else (None, None)."""
    named = named_apps(goal, apps)
    if named:
        return named[0]["bundleId"], named[0]["name"]
    inferred = _part_app(goal, apps)
    if inferred is not None and inferred.get("bundleId"):
        return inferred["bundleId"], inferred.get("name")
    return None, None


def _collection(goal):
    found = COLLECTION.search(_fold(goal))
    return found.group(1).casefold() if found else "items"


def _models(nav_model, kind, items):
    from .models import extract_model, synth_model_for
    return {"navigate": nav_model, "extract": extract_model(nav_model), "report": synth_model_for(kind, items,
                                                                                                    nav_model)}


# A collection the person keeps ("my saved posts", "the reels I liked"): the plan goes there, not to the app's feed.
KEPT = re.compile(r"\b(saved|bookmarked|liked|favou?rited?)\s+(posts|reels|videos|tiktoks|shorts|pins|photos)\b|"
                  r"\b(posts|reels|videos|tiktoks|shorts|pins|photos)\s+(?:that\s+)?i(?:'ve|\s+have)?\s+"
                  r"(saved|bookmarked|liked)\b", re.I)


def kept_collection(goal):
    """"your saved posts" for a request over a collection the person kept, else None."""
    found = KEPT.search(_fold(goal))
    if not found:
        return None
    how, noun = (found.group(1), found.group(2)) if found.group(1) else (found.group(4), found.group(3))
    return f"your {how.casefold()} {noun.casefold()}"


def _steps(kind, goal, apps, budget, focus):
    if kind == "feed":
        _bundle, name, _, _ = _feed_app(goal, apps)
        kept = kept_collection(goal)
        where = f"find {focus} {budget.item_noun}" if focus else f"go to {kept or budget.item_noun}"
        count = f"about {budget.items}" if budget.items else f"for {B.minutes_label(budget.minutes or 10)}"
        verb = "Go through" if kept else "Watch"
        return [{"id": 1, "title": f"Open {name} and {where}", "app": name},
                {"id": 2, "title": f"{verb} {count} {budget.item_noun}" if budget.items else
                 f"{verb} {budget.item_noun} {count}", "app": name},
                {"id": 3, "title": "Write up what it learned", "app": None}]
    if kind == "survey":
        _bundle, name = _survey_app(goal, apps)
        place = name or "the app"
        collection = _collection(goal)
        collection = {"dms": "your DMs", "matches": "your matches", "messages": "your messages",
                      "chats": "your chats", "emails": "your inbox", "inbox": "your inbox"}.get(collection,
                                                                                               f"the {collection}")
        from .inbox import wants_drafts, wants_openers
        sweep = wants_drafts(goal) and budget.item_kind in ("chat", "row")
        openers = wants_openers(goal) and budget.item_kind in OPENER_KINDS
        return [{"id": 1, "title": f"Open {place} and go to {collection}", "app": name},
                {"id": 2, "title": f"Read each of up to {budget.items} {budget.item_noun}", "app": name},
                {"id": 3, "title": "Sort them and draft replies, sending nothing" if sweep else
                 "Draft a DM to each person who fits, sending nothing" if openers else
                 "Rank them and write it up", "app": None}]
    if kind == "chain":
        out = []
        for index, part in enumerate(chain_parts(goal)[:8], 1):
            named = named_apps(part, apps)
            inferred = None if named else _part_app(part, apps)
            app = named[0]["name"] if named else (inferred or {}).get("name")
            title = part[:1].upper() + part[1:]
            out.append({"id": index, "title": title[:120], "app": app})
        return out
    named = named_apps(goal, apps)
    name = named[0]["name"] if named else None
    return [{"id": 1, "title": f"Browse {name}" if name else "Browse", "app": name},
            {"id": 2, "title": "Write up what it found", "app": None}]


# A commit the request asks for is a verb that opens a clause: "and like the best ones", "then book Friday at 8",
# "reply to each one", "Text Sam ...". "I'd like to know" or "send me a summary" are not commits.
CLAUSE = r"(?:^|[,;.:!?]\s*|\b(?:and|then|also|please|to|after that)\s+)"
NOT_COMMIT = re.compile(r"^(?:send|share|post) (?:me|it to me|them to me|us|what|back)\b", re.I)
MESSAGE_WORDS = ("text", "message", "dm", "email", "e-mail", "imessage")
# The act a verb means when several families hold it; else the first act (by name) whose family does.
PREFERRED = {"reply": "reply", "comment": "comment", "like": "react", "react": "react", "send": "send_message",
             "post": "post", "publish": "post", "tweet": "post", "buy": "order", "purchase": "order",
             "confirm": "other", "submit": "other", "request": "request_money", "share": "share",
             **{word: "send_message" for word in MESSAGE_WORDS}}
# What a card says it asks before: "Asks before each reply." for a feed, survey or browse; "Asks before it books." for
# a chain.
EACH_NOUN = {"reply": "reply", "comment": "comment", "react": "like", "send_message": "message", "post": "post",
             "share": "share", "follow": "follow", "unfollow": "unfollow", "book": "booking", "order": "order",
             "pay": "payment", "transfer": "transfer", "request_money": "money request", "call": "call",
             "delete": "deletion", "cancel": "cancellation", "install": "install", "other": "action"}
CHAIN_VERB = {"send_message": "send", "react": "like", "other": "confirm"}


def _commit_verbs():
    """[(verb, act)] for every act that asks (contract_asks.ASK_ACTS, "like" among them) and the message verbs, longest
    first."""
    from ..contract_asks import ASK_ACTS
    from ..contract_screen import FAMILIES
    verbs = {}
    for act in sorted(ASK_ACTS):
        for verb in FAMILIES.get(act, ()):
            if verb != "get":
                verbs.setdefault(verb, PREFERRED.get(verb, act))
    for word in MESSAGE_WORDS:
        verbs.setdefault(word, "send_message")
    return sorted(verbs.items(), key=lambda pair: -len(pair[0]))


# A negation that opens a list of acts the request rules out: "don't like, follow, reply to or message anyone",
# "never post", "without liking or replying". Only the verbs listed right after it are ruled out ("don't forget to send
# it" rules nothing out, and "if it's not there, book it" still books).
NEGATION = re.compile(r"\b(?:don['’]t|dont|do not|never|without|no need to|nor)\s+(?:to\s+)?", re.I)
LIST_GAP = re.compile(r"\s*(?:,\s*(?:or\s+|and\s+|nor\s+)?|\s+or\s+|\s+and\s+|\s+nor\s+|/)\s*")
PARTICLE = re.compile(r"\s+(?:to|on|at|back|out)\b")


def _ing(verb):
    head, _, rest = verb.partition(" ")
    stem = head[:-1] if head.endswith("e") and not head.endswith("ee") else head
    return (stem + "ing") + (" " + rest if rest else "")


def negated_spans(folded):
    """[(start, end)] of the verbs a negation rules out in ``folded`` (casefolded text)."""
    words = sorted({form for verb, _ in _commit_verbs() for form in (verb, _ing(verb))}, key=len, reverse=True)
    verb = re.compile("(?:" + "|".join(re.escape(w) for w in words) + r")\b")
    spans = []
    for cue in NEGATION.finditer(folded):
        at = cue.end()
        while True:
            hit = verb.match(folded, at)
            if hit is None:
                break
            spans.append((hit.start(), hit.end()))
            at = hit.end()
            particle = PARTICLE.match(folded, at)
            if particle is not None:
                at = particle.end()
            gap = LIST_GAP.match(folded, at)
            if gap is None:
                break
            at = gap.end()
    return spans


def commits_in(text):
    """[(act, verb)] the text asks Mobster to commit, in the text's order (each act once)."""
    folded = _fold(text).casefold()
    ruled_out = negated_spans(folded)
    found = []
    for verb, act in _commit_verbs():
        for hit in re.finditer(CLAUSE + "(" + re.escape(verb) + r")\b", folded):
            if NOT_COMMIT.match(folded[hit.start(1):]):
                continue
            if any(start <= hit.start(1) < end for start, end in ruled_out):
                continue
            found.append((hit.start(1), act, verb))
            break
    out, acts = [], set()
    for _, act, verb in sorted(found):
        if act not in acts:
            acts.add(act)
            out.append((act, verb))
    return out


def bulk_change(goal):
    """A request that changes things and asks to be told nothing ("delete every email")."""
    text = _fold(goal)
    return bool(CHANGE_VERB.search(text) and not REPORT_INTENT.search(text))


def _commits(kind, goal, steps):
    """[{act, app, verb}] the task will commit: every part of a chain (each with its step's app), the whole request
    for a feed, survey or browse."""
    out = []
    if kind == "chain":
        parts = chain_parts(goal)
        for index, part in enumerate(parts):
            step = steps[index] if index < len(steps) else {}
            app = step.get("app") or next((s.get("app") for s in reversed(steps) if s.get("app")), None)
            out += [{"act": act, "app": app, "verb": verb} for act, verb in commits_in(part)
                    if act not in [c["act"] for c in out]]
        return out
    app = next((s.get("app") for s in steps if s.get("app")), None)
    return [{"act": act, "app": app, "verb": verb} for act, verb in commits_in(goal)]


def commit_lines(kind, commits):
    """The card's lines for its commits: "Asks before each reply." (a feed, survey or browse repeats the act),
    "Asks before it books." (a chain's step)."""
    lines = []
    for commit in commits[:3]:
        if kind == "chain":
            verb = CHAIN_VERB.get(commit["act"]) or commit["verb"]
            lines.append(f"Asks before it {_third_person(verb)}.")
        else:
            lines.append(f"Asks before each {EACH_NOUN.get(commit['act'], 'action')}.")
    return list(dict.fromkeys(lines))


def _third_person(verb):
    if " " in verb:
        head, _, rest = verb.partition(" ")
        return f"{_third_person(head)} {rest}"
    if re.search(r"[^aeiou]y$", verb):
        return verb[:-1] + "ies"
    if re.search(r"(s|sh|ch|x|z)$", verb):
        return verb + "es"
    return verb + "s"


def _summary(kind, goal, budget, focus, steps):
    if kind == "feed":
        topic = f"{focus} " if focus else ""
        kept = kept_collection(goal)
        if kept and budget.items and not budget.strict:
            return f"Goes through up to {budget.items} of {kept}, then writes up what it found."
        if budget.strict:
            return (f"Watches {topic}{budget.item_noun} for the full {B.minutes_label(budget.minutes)}, "
                    "then writes up what it learned.")
        said = (f", the number you'd scroll in {B.minutes_label(budget.minutes)}" if budget.kind == "time" else "")
        return f"Watches about {budget.items} {topic}{budget.item_noun}{said}, then writes up what it learned."
    if kind == "survey":
        place = next((s["app"] for s in steps if s.get("app")), None)
        where = f" in {place}" if place else ""
        from .inbox import wants_drafts, wants_openers
        if wants_drafts(goal) and budget.item_kind in ("chat", "row"):
            # An inbox sweep (longform/inbox.py): drafts for review, none sent.
            return (f"Reads up to {budget.items} {budget.item_noun}{where} and drafts replies to the ones that need "
                    "one, for you to review. Nothing is sent.")
        if wants_openers(goal) and budget.item_kind in OPENER_KINDS:
            # Openers (longform/inbox.py): a first message to each person who fits, for review, none sent.
            return (f"Reads up to {budget.items} {budget.item_noun}{where} and drafts a DM to each person who fits, "
                    "for you to review. Nothing is sent.")
        return f"Reads up to {budget.items} {budget.item_noun}{where}, then ranks the most promising and says why."
    if kind == "chain":
        return f"Does {len(steps)} steps in order, with a checkpoint after each."
    return f"Browses for about {budget.items or 20} {budget.item_noun}, then writes up what it found."


def plan_id(goal, kind, budget, limit_usd, nav_model):
    raw = f"{_fold(goal)}|{kind}|{budget.to_dict()}|{limit_usd}|{nav_model}"
    return "p_" + hashlib.sha1(raw.encode()).hexdigest()[:10]


def make_plan(goal, *, apps=(), limit_usd=None, nav_model="claude-sonnet-5-5",
              kind=None, change=None, alternative=None, now=None):
    """The plan card (a dict, DESIGN §4.1) for ``goal``, or None if it isn't a long task and ``kind`` isn't forced."""
    goal = _fold(goal)
    kind = kind or detect(goal, apps)
    if kind is None:
        return None
    if kind not in KINDS:
        raise ValueError("kind is feed, survey, chain or browse")
    parsed = B.parse_budget(goal)
    if kind == "feed":
        _bundle, _name, item_kind, noun = _feed_app(goal, apps)
        if parsed is not None and parsed.item_noun not in ("items",) and parsed.item_kind in FEED_KINDS:
            noun = parsed.item_noun if parsed.item_noun != "videos" else noun
    elif kind == "survey":
        item_kind, noun = B.noun_in(goal)
        if item_kind is None:
            item_kind, noun = "row", "items"
    else:
        item_kind, noun = B.noun_in(goal)
        item_kind, noun = (item_kind or "result"), (noun or "results")
    budget = B.for_kind(kind, parsed, item_kind, noun)
    if alternative == "full" and budget.minutes:
        budget = B.time_budget(budget.minutes, budget.item_kind, budget.item_noun, strict=True)
    elif alternative == "more" and budget.kind == "all":
        budget = B.all_budget(budget.item_kind, budget.item_noun, B.ALL_MAX)
    budget = B.override(budget, change)
    focus = focus_of(goal) if kind in ("feed", "browse") else None
    steps = _steps(kind, goal, apps, budget, focus)
    models = _models(nav_model, kind, budget.items or 60)
    estimate = C.estimate(kind, items=budget.items, item_kind=budget.item_kind, steps=len(steps),
                          nav_model=nav_model, synth_model=models["report"],
                          phone_seconds_cap=budget.phone_seconds_cap)
    alternatives = []
    if kind in ("feed", "browse") and budget.kind == "time" and not budget.strict:
        full = C.estimate(kind, items=None, item_kind=budget.item_kind, steps=len(steps), nav_model=nav_model,
                          synth_model=models["report"], phone_seconds_cap=budget.phone_seconds_cap)
        alternatives.append({"id": "full", "label": f"Use the full {B.minutes_label(budget.minutes)}", "items": None,
                             "phoneSeconds": round(budget.phone_seconds_cap), "lowUsd": full["lowUsd"],
                             "highUsd": full["highUsd"]})
    if budget.kind == "all" and (budget.items or 0) < B.ALL_MAX:
        more = C.estimate(kind, items=B.ALL_MAX, item_kind=budget.item_kind, steps=len(steps), nav_model=nav_model,
                          synth_model=models["report"])
        alternatives.append({"id": "more", "label": f"Up to {B.ALL_MAX} {budget.item_noun}", "items": B.ALL_MAX,
                             "phoneSeconds": more["phoneSeconds"], "lowUsd": more["lowUsd"],
                             "highUsd": more["highUsd"]})
    cap = cap_check(estimate["highUsd"], limit_usd)
    for option in alternatives:
        # Each alternative carries its own cap check: the card proposes the raise for the one the person picks.
        option.update({k: v for k, v in cap_check(option["highUsd"], limit_usd).items() if k != "limitUsd"})
    commits = _commits(kind, goal, steps)
    # A long run asks before every act that asks, with Ask before acting off too (frontier._long_asks), so the card
    # says so whatever the setting.
    policy_lines = {"readOnly": not commits, "lines": commit_lines(kind, commits)}
    now = time.time() if now is None else now
    card = {"id": plan_id(goal, kind, budget, limit_usd, nav_model), "kind": kind, "title": goal[:300],
            "focus": focus, "summary": _summary(kind, goal, budget, focus, steps),
            "apps": list(_plan_apps(kind, goal, apps)), "steps": steps, "budget": budget.public(),
            "alternatives": alternatives,
            "estimate": {k: estimate[k] for k in ("phoneSeconds", "reportSeconds", "usd", "lowUsd", "highUsd")},
            "cap": cap, "policy": policy_lines, "commits": [{"act": c["act"], "app": c["app"]} for c in commits],
            "models": models, "expiresAt": int((now + PLAN_TTL_S) * 1000)}
    card["_budget"] = budget.to_dict()
    return card


def cap_check(high_usd, limit_usd):
    """{"limitUsd", "exceeds", "proposedUsd"}: a plan whose high estimate passes the per-task limit proposes a raise to
    ceil_to_0.50(1.2 x high), which the person switches on before Start."""
    exceeds = limit_usd is not None and high_usd > limit_usd
    return {"limitUsd": limit_usd, "exceeds": bool(exceeds),
            "proposedUsd": C.ceil_half(1.2 * high_usd) if exceeds else None}


def _plan_apps(kind, goal, apps):
    named = named_apps(goal, apps)
    if named:
        return named
    if kind == "feed":
        bundle, name, _, _ = _feed_app(goal, apps)
        return [{"bundleId": bundle, "name": name}]
    if kind == "survey":
        bundle, name = _survey_app(goal, apps)
        return [{"bundleId": bundle, "name": name}] if bundle else []
    return []


def public(card):
    """The card without its private keys (``_budget``)."""
    return {k: v for k, v in (card or {}).items() if not k.startswith("_")}


def budget_of(card):
    return B.Budget.from_dict(card["_budget"]) if card and card.get("_budget") else None


class PlanStore:
    """Plans shown on cards, per Runtime: at most MAX_PLANS, each for PLAN_TTL_S."""

    def __init__(self):
        self.plans = {}
        self.lock = threading.Lock()

    def put(self, card, now=None):
        now = time.time() if now is None else now
        with self.lock:
            self.plans = {k: v for k, v in self.plans.items() if v[0] > now}
            self.plans[card["id"]] = (now + PLAN_TTL_S, card)
            while len(self.plans) > MAX_PLANS:
                oldest = min(self.plans, key=lambda key: self.plans[key][0])
                del self.plans[oldest]
        return card

    def get(self, plan_id, now=None):
        now = time.time() if now is None else now
        with self.lock:
            found = self.plans.get(plan_id)
            if found is None or found[0] <= now:
                self.plans.pop(plan_id, None)
                return None
            return found[1]


_stores = weakref.WeakKeyDictionary()
_stores_lock = threading.Lock()


def store_for(runtime):
    with _stores_lock:
        store = _stores.get(runtime)
        if store is None:
            store = _stores[runtime] = PlanStore()
        return store


def nav_model():
    from ..engines import smart_model
    try:
        return smart_model()
    except Exception:  # noqa: BLE001
        return "claude-sonnet-5-5"


def runtime_apps(runtime):
    try:
        return [a for a in runtime.apps() if a.get("installed") is not False]
    except Exception:  # noqa: BLE001
        return []
