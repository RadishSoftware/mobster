"""Scripted navigation, extraction and report models for long-form runs on longform_world's phone."""

import json
import re
import threading
import time

from mobile_agent.tests import longform_scripted
from mobile_agent.tests import sota_world as world
from mobile_agent.tests.longform_world import (
    BEST_RAMEN,
    HINGE,
    INSTAGRAM,
    MAPS,
    NEAR_BROOKLYN,
    PLACE_BY_NAME,
    PROFILE_BY_NAME,
    PROFILES,
    RAMEN,
    STATE_LABELS,
    THEME_NAMES,
    YELP,
    item_by_handle,
    last_message,
    profile_score,
    sleep,
)

# List prices per MTok (input, output, cache read, 5-minute cache write), Claude API, read 2026-10-06; Haiku 5.5's are
# for prompts up to 100K tokens. The bench prices every scripted call with them.
PRICES = {"claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125), "claude-sonnet-5-5": (2.0, 10.0, 0.2, 2.5),
          "claude-opus-5-5": (4.0, 20.0, 0.2, 5.0)}
CHARS_PER_TOKEN = 3.8
SCREENSHOT_TOKENS = 1150      # a 640 px screenshot
TILE_TOKENS_PER_FRAME = 410   # a 384 px frame in a tile
# Extraction calls bill what real ones did (8 Oct 2026: 57 real claude-haiku-5-5 calls on the fake frames, 384 px
# tiles, thinking off; the long-form bench's extraction check): about 1,670 prompt tokens before the picture
# (the system prompt, the schema and the item's text, on Haiku 5.5's tokenizer), 84% of the prompt read from the
# cache, about 210 tokens out.
EXTRACT_PROMPT_TOKENS, EXTRACT_CACHED_SHARE, EXTRACT_OUT_TOKENS = 1670, .84, 210


def usd(model, usage):
    price = PRICES.get(model)
    if price is None or not usage:
        return 0.0
    cached, written = usage.get("cached_tokens", 0), usage.get("cache_write_tokens", 0)
    fresh = usage.get("prompt_tokens", 0) - cached - written
    return (fresh * price[0] + cached * price[2] + written * price[3] + usage.get("completion_tokens", 0)
            * price[1]) / 1e6


def _parts(messages):
    """(system text, stable text, other text, image count) of Chat-style ``messages``."""
    system, stable, rest, images = "", "", "", 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            if message.get("role") == "system":
                system += content
            else:
                rest += content
            continue
        for part in content or ():
            if part.get("type") == "image_url":
                images += 1
            elif message.get("role") == "system":
                system += part.get("text", "")
            elif part.get("cache"):
                stable += part.get("text", "")
            else:
                rest += part.get("text", "")
    return system, stable, rest, images


class Usage:
    """Token counts a real call of this shape would bill: the system prompt and the parts marked ``cache`` are written
    to the cache on a client's first call with that prefix and read from it after; images by their size."""

    def __init__(self):
        self.prefixes = set()

    def of(self, messages, out, *, image_tokens, thinking=0):
        system, stable, rest, images = _parts(messages)
        prefix_tokens = int((len(system) + len(stable)) / CHARS_PER_TOKEN)
        fresh = int(len(rest) / CHARS_PER_TOKEN) + images * image_tokens
        key = hash(system + stable)
        cached = written = 0
        if prefix_tokens >= 512:
            if key in self.prefixes:
                cached = prefix_tokens
            else:
                written = prefix_tokens
                self.prefixes.add(key)
        else:
            fresh += prefix_tokens
        completion = int(len(json.dumps(out, ensure_ascii=False)) / 3.2) + thinking
        return {"prompt_tokens": fresh + cached + written, "completion_tokens": completion, "cached_tokens": cached,
                "cache_write_tokens": written}

    def extraction(self, image_tokens):
        """One extraction call, at the real calls' measured shape (the first one of a client writes the cache)."""
        prompt = EXTRACT_PROMPT_TOKENS + image_tokens
        prefix = min(int(EXTRACT_CACHED_SHARE * (EXTRACT_PROMPT_TOKENS + TILE_TOKENS_PER_FRAME)), prompt - 100)
        first = "extract" not in self.prefixes
        self.prefixes.add("extract")
        return {"prompt_tokens": prompt, "completion_tokens": EXTRACT_OUT_TOKENS,
                "cached_tokens": 0 if first else prefix, "cache_write_tokens": prefix if first else 0}


class ScriptedLongModel:
    """A model for one role in a long-form run, with ``complete`` as model_clients.AnthropicChat's: navigation for the
    three tasks (``variant`` "after" uses the macros, "before" plays today's loop), the task contract, per-item
    extraction from the fixture's ground truth (the on-screen text only when a picture was sent), and
    longform_scripted's digests and reports. It sleeps the latency model's time for its model and reports the tokens
    such a call would bill."""

    per_call_reasoning = True

    def __init__(self, model="claude-sonnet-5-5", *, variant="after", log=None, fail=0, role=None):
        self.model, self.variant, self.log = model, variant, log
        self.role = role
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
                      "calls": 0, "hedges": 0}
        self.model_failed = False
        self.reasoning = "low"
        self.connection = None
        self.fail = fail                     # the next ``fail`` calls raise (a model error)
        self.calls = []                      # (schema key, seconds) per call
        self._usage = Usage()
        self._lock = threading.Lock()

    def complete(self, messages, schema, timeout=60, reasoning=None, **kwargs):
        from mobile_agent.frontier import prompt_text
        props = schema.get("properties") or {}
        key = next((k for k in ("actions", "item", "digest", "report", "answerable", "items") if k in props), "other")
        started = time.monotonic()
        with self._lock:
            if self.fail > 0:
                self.fail -= 1
                raise RuntimeError("HTTP 529; Anthropic HTTP 529: overloaded (scripted)")
        image_tokens, thinking = SCREENSHOT_TOKENS, 0
        if key == "actions":
            prompt = prompt_text(messages)
            sleep("sonnet")
            out = navigate(prompt, props, self.variant)
            thinking = 60
            if self.log is not None:
                self.log.append({"request": Turn(prompt, ()).request, "out": out, "prompt": prompt,
                                 "variant": self.variant})
        elif key == "items":
            sleep("contract")
            out = {"items": contract_items(prompt_text(messages))}
        elif key == "item":
            sleep("haiku")
            out = extract_answer(messages, schema)
            image_tokens = TILE_TOKENS_PER_FRAME * max(1, frames_in_tile(messages))
        elif key in ("digest", "report", "answerable"):
            sleep({"digest": "digest", "answerable": "sonnet"}.get(key, "opus_synth" if "opus" in self.model
                                                                     else "sonnet_synth"))
            out = longform_scripted.respond(messages, schema)
            thinking = 400 if key == "report" else 0
        else:
            out = {}
        if key == "item":
            usage = self._usage.extraction(image_tokens * image_count(messages))
        else:
            usage = self._usage.of(messages, out, image_tokens=image_tokens, thinking=thinking)
        with self._lock:
            for name in ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens"):
                self.usage[name] += usage[name]
            self.usage["calls"] += 1
            self.calls.append((key, round(time.monotonic() - started, 3)))
        return out, usage


def image_count(messages):
    return _parts(messages)[3]


def frames_in_tile(messages):
    """How many frames the extraction's tile holds (the prompt says), else 1 per image."""
    text = "".join(_parts(messages)[1:3])
    match = re.search(r"(\d) frames? side by side", text)
    return int(match.group(1)) if match else image_count(messages)


Turn = world.Turn
act = world.act


def reply(actions, *, notes=(), answer=None, plan=None, thought="", updates=(), props=None):
    out = {"thought": thought, "plan": plan, "notes_add": list(notes), "checklist_updates": list(updates),
           "actions": actions, "answer": answer}
    if props and "data_json" in props:
        out["data_json"] = None
    if props and "step" in props:
        out["step"] = None
    return out


def task_of(request):
    r = request.casefold()
    if "ramen" in r and ("book" in r or "reserve" in r):
        return "chain"
    if "hinge" in r or "matches" in r:
        return "survey"
    if any(word in r for word in ("reels", "instagram", "doomscroll", "tiktok", "shorts")):
        return "feed"
    return None


def focus_of(request):
    match = re.search(r"\babout ([a-z][\w' -]{1,30}?)(?: for | and |,|\.|$)", request.casefold())
    return match.group(1).strip() if match else "basketball"


def finished_turn(t, props):
    """The last turn of a budget: only DONE or BLOCKED may be chosen."""
    try:
        operations = props["actions"]["items"]["properties"]["op"]["enum"]
    except (KeyError, TypeError):
        try:
            operations = props["actions"]["items"]["properties"]["operation"]["enum"]
        except (KeyError, TypeError):
            return False
    return set(operations) <= {"DONE", "BLOCKED"}


def operations_of(props):
    for name in ("op", "operation"):
        try:
            return tuple(props["actions"]["items"]["properties"][name]["enum"])
        except (KeyError, TypeError):
            continue
    return ()


def navigate(prompt, props, variant="after"):
    t = Turn(prompt, operations_of(props))
    kind = task_of(t.request)
    final = finished_turn(t, props)
    if kind == "feed":
        out = feed_turn(t, final, variant)
    elif kind == "survey":
        out = survey_turn(t, final, variant)
    elif kind == "chain":
        out = chain_turn(t, final, variant)
    else:
        out = (world.decide(t))
        out = {"actions": out[0], "answer": out[1], "notes": out[2]}
    actions = out["actions"]
    updates = [{"id": 1, "status": "done", "value": None}] if any(a.get("op") == "DONE" for a in actions) else []
    return reply(actions, notes=out.get("notes", ()), answer=out.get("answer"), props=props, updates=updates,
                 thought=out.get("thought", ""))


def _row(t, prefix):
    return next((rid for rid, role, label in t.rows if label.startswith(prefix)), None)


def _feedback(t):
    match = re.search(r"Feedback on your last action: (.*)", t.prompt)
    return match.group(1) if match else ""


def feed_turn(t, final, variant):
    steer = re.search(r"focus on (?:the )?([\w' -]+?) instead", t.context, re.I)
    focus = steer.group(1) if steer else focus_of(t.request)
    feedback = _feedback(t)
    if final:
        return {"actions": [act("DONE")], "answer": feed_answer(t, variant)}
    if not t.in_app(INSTAGRAM):
        return {"actions": [act("LAUNCH_APP", INSTAGRAM)]}
    alert_button = t.find("Not Now")
    if alert_button:
        return {"actions": [act("TAP", alert_button)]}
    if _row(t, "Reel by @") and t.has("Back") and t.has("Like"):
        # In the viewer.
        if variant == "before":
            notes = []
            reel = _row(t, "Reel by @")
            label = next(label for rid, _, label in t.rows if rid == reel)
            item = item_by_handle(label.removeprefix("Reel by @"))
            if item is not None and item.kind != "ad":
                notes.append(f"@{item.handle}: {item.caption} (on screen: {item.title}, {item.stat})")
            return {"actions": [act("SWIPE_UP")], "notes": notes}
        left = re.findall(r"(\d+) Reels? left in the budget", feedback)
        if ("Stopped because: budget" in feedback or (left and left[-1] == "0")
                or "budget is covered" in feedback):
            return {"actions": [act("DONE")], "answer": feed_answer(t, variant)}
        if "drifted" in feedback or "weren't about" in feedback:
            return {"actions": [act("TAP", t.find("Back"))]}
        if "Stopped because: stuck" in feedback or "end of the feed" in feedback:
            return {"actions": [act("DONE")], "answer": feed_answer(t, variant)}
        return {"actions": [act("WATCH_FEED", None, focus)]}
    field_id = t.find("Search", "SearchField")
    if t.has("Reels") and t.has("Top") and field_id:
        first = _row(t, "Reel by @")
        if first:
            return {"actions": [act("TAP", first)]}
        return {"actions": [act("TAP", t.find("Reels", "Button"))]}
    if field_id:
        return {"actions": [act("TYPE_SUBMIT", field_id, focus)]}
    if t.find("Search", "Button"):
        return {"actions": [act("TAP", t.find("Search", "Button"))]}
    return {"actions": [act("LAUNCH_APP", INSTAGRAM)]}


def feed_answer(t, variant):
    if variant == "after":
        saved = re.findall(r"Watched \d+ Reels in \d+ s: (\d+) saved", t.context)
        return f"Watched {sum(int(n) for n in saved)} basketball Reels; the report has what they covered."
    notes = re.findall(r"^- (@[\w.]+: .*)$", t.prompt, re.M)
    return answer_from_notes(notes)


def answer_from_notes(notes):
    """Today's DONE answer for the feed: the notes grouped by what they're about (what a model writes from them)."""
    groups = {}
    for note in notes:
        handle = note.split(":", 1)[0].lstrip("@")
        item = item_by_handle(handle)
        theme = item.theme if item is not None else "other"
        groups.setdefault(theme, []).append(note)
    lines = [f"I watched {len(notes)} Reels about basketball. What they covered:"]
    for theme, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        name = THEME_NAMES.get(theme, theme.capitalize())
        lines.append(f"## {name}")
        for note in group[:6]:
            handle, rest = note.split(":", 1)
            lines.append(f"- {rest.strip()} ({handle})")
    return "\n".join(lines)


def survey_turn(t, final, variant):
    feedback = _feedback(t)
    if final:
        return {"actions": [act("DONE")], "answer": survey_answer(t, variant)}
    if not t.in_app(HINGE):
        return {"actions": [act("LAUNCH_APP", HINGE)]}
    if t.has("Matches") and t.find("Matches", "NavigationBar"):
        first = next((rid for rid, role, label in t.rows if role == "Cell" and label in PROFILE_BY_NAME), None)
        if variant == "after":
            if re.search(r"Stopped because: (the list ended|budget|every row)",
                         feedback) or "end of the list" in feedback:
                return {"actions": [act("DONE")], "answer": survey_answer(t, variant)}
            return {"actions": [act("READ_EACH", first, "open: Profile; read: prompts, details, conversation")]}
        # Before: one match at a time, the next one not in the notes yet.
        done = set(re.findall(r"^- Match (\w+):", t.prompt, re.M))
        visible = [(rid, label) for rid, role, label in t.rows if role == "Cell" and label in PROFILE_BY_NAME]
        todo = [(rid, label) for rid, label in visible if label not in done]
        if todo:
            return {"actions": [act("TAP", todo[0][0])]}
        if len(done) >= len(PROFILES):
            return {"actions": [act("DONE")], "answer": survey_answer(t, variant)}
        return {"actions": [act("SWIPE_UP")]}
    if t.find("Profile", "Button") and t.find("Send a message", "TextField"):
        if variant == "before":
            return {"actions": [act("TAP", t.find("Profile", "Button"))]}
        return {"actions": [act("TAP", t.find("Back"))]}
    if t.has("Like prompt") and t.has("Back"):
        if variant == "before":
            name = next((label.split(",")[0] for _, role, label in t.rows
                         if role == "StaticText" and re.match(r"^\w+, \d+$", label)), None)
            if t.has("Photo 1 of 6"):
                return {"actions": [act("SWIPE_UP")]}
            profile = PROFILE_BY_NAME.get(name)
            note = f"Match {name}: " + profile_note(profile) if profile else None
            # Back to the chat, then back to Matches (a label on the next screen), and the note.
            return {"actions": [act("TAP", t.find("Back")), act("TAP", "Back")], "notes": [note] if note else []}
        return {"actions": [act("TAP", t.find("Back"))]}
    tab = t.find("Matches", "Button")
    if tab:
        return {"actions": [act("TAP", tab)]}
    back = t.find("Back")
    return {"actions": [act("TAP", back)] if back else [act("LAUNCH_APP", HINGE)]}


def profile_note(profile):
    convo = STATE_LABELS[profile.state].casefold() + (f", {profile.questions} unanswered question"
                                                      + ("s" if profile.questions > 1 else "") if profile.questions
                                                      else "")
    return (f"{profile.age}, {profile.location}, {profile.work}; children: {profile.kids}; into "
            f"{', '.join(profile.interests)}; {convo}")


def survey_answer(t, variant):
    if variant == "after":
        read = sum(int(n) for n in re.findall(r"Feedback on your last action: Read (\d+) profiles", t.context))
        return f"Read {read} match profiles; the report ranks the most promising ones."
    notes = re.findall(r"^- Match (\w+): (.*)$", t.prompt, re.M)
    ranked = sorted(notes, key=lambda kv: -profile_score(PROFILE_BY_NAME[kv[0]]))
    lines = [f"I read {len(notes)} of your matches. The most promising:"]
    for rank, (name, note) in enumerate(ranked[:3], 1):
        lines.append(f"{rank}. {name}: {note}")
    if len(notes) < len(PROFILES):
        lines.append(f"I ran out of turns after {len(notes)} matches.")
    return "\n".join(lines)


def chain_turn(t, final, variant):
    if final:
        return {"actions": [act("DONE")], "answer": chain_answer(t)}
    context = t.context
    maps_read = "Maps ramen:" in context
    yelp_read = "Yelp ramen:" in context
    if not maps_read:
        if t.app not in ("Maps", MAPS):
            return {"actions": [act("LAUNCH_APP", MAPS)], "thought": "Step 1: Maps"}
        if t.has(RAMEN[0].name):
            ratings = "; ".join(f"{p.name} {p.maps} ({p.maps_reviews})" for p in RAMEN)
            return {"actions": [act("READ_LIST")], "notes": [f"Maps ramen: {ratings}"]}
        field_id = next((rid for rid, role, _ in t.rows if role == "SearchField"), None)
        return {"actions": [act("TYPE_SUBMIT", field_id, "ramen")]}
    if not yelp_read:
        if not t.in_app(YELP):
            return {"actions": [act("LAUNCH_APP", YELP)], "thought": "Step 2: Yelp"}
        if t.has(RAMEN[0].name):
            ratings = "; ".join(f"{p.name} {p.yelp} ({p.yelp_reviews})" for p in RAMEN)
            return {"actions": [act("READ_LIST")], "notes": [f"Yelp ramen: {ratings}"]}
        field_id = next((rid for rid, role, _ in t.rows if role == "SearchField"), None)
        return {"actions": [act("TYPE_SUBMIT", field_id, "ramen")]}
    if not t.in_app(YELP):
        return {"actions": [act("LAUNCH_APP", YELP)]}
    best = BEST_RAMEN
    if any(label.startswith("You're all set") for label in t.labels()):
        return {"actions": [act("DONE")], "answer": chain_answer(t), "notes": [f"Booked {best}, Friday 8:00 PM"]}
    if t.find(best) and t.find(best, "Cell"):
        return {"actions": [act("TAP", t.find(best, "Cell")), act("TAP", "Reserve"), act("TAP", "Fri"),
                            act("TAP", "8:00 PM"), act("TAP", "Confirm reservation")],
                "notes": [f"Best across both: {best} (Maps 4.8, Yelp 4.7)"]}
    if t.has("Reserve"):
        return {"actions": [act("TAP", t.find("Reserve")), act("TAP", "Fri"), act("TAP", "8:00 PM"),
                            act("TAP", "Confirm reservation")]}
    if t.has("Fri"):
        steps = []
        if not t.has("Confirm reservation"):
            steps = [act("TAP", t.find("Fri")), act("TAP", "8:00 PM")]
        return {"actions": steps + [act("TAP", t.find("Confirm reservation") or "Confirm reservation")]}
    if t.has("Back"):
        return {"actions": [act("TAP", t.find("Back"))]}
    field_id = next((rid for rid, role, _ in t.rows if role == "SearchField"), None)
    return {"actions": [act("TYPE_SUBMIT", field_id, "ramen")]}


def chain_answer(t):
    booked = any(label.startswith("You're all set") for label in t.labels()) or "Booked " in t.context
    best = PLACE_BY_NAME[BEST_RAMEN]
    head = (f"The best ramen across Maps and Yelp is {best.name} ({best.address}): {best.maps} on Maps, {best.yelp} on "
            "Yelp.")
    return head + (" Booked Friday at 8:00 PM for 2." if booked else " I couldn't book it.")


def contract_items(prompt):
    match = re.search(r"^Request: (.*)$", prompt, re.M)
    request = re.sub(r"^In [^:]{1,40}: ", "", match.group(1).strip() if match else "")
    blank = {"payload": "", "act": "none", "count": "", "condition": "", "quote": ""}
    kind = task_of(request)
    if kind == "chain":
        return [{**blank, "kind": "REPORT", "app": "Maps", "what": "the best ramen near me"},
                {**blank, "kind": "COMMIT", "app": "Yelp", "what": "book Friday at 8", "act": "book",
                 "quote": "book Friday at 8"}]
    if kind in ("feed", "survey"):
        return [{**blank, "kind": "READ", "app": "Instagram" if kind == "feed" else "Hinge", "what": request[:120]}]
    return world.contract_items(prompt)


def extract_answer(messages, schema=None):
    """The extractor's answer for one item, from the fixture: the reel or profile the prompt's text names. The text on
    the video (title card, key stat, call to action) is answered only when a picture was sent."""
    text = "".join(_parts(messages)[1:3])
    pictures = image_count(messages) > 0
    handle = re.search(r"Creator: @([\w.]+)", text)
    fields = (((schema or {}).get("properties") or {}).get("item") or {}).get("properties") or {}
    if "prompts" in fields:
        return {"item": profile_extract(text, _parts(messages)[0])}
    if "bucket" in fields:
        return {"item": chat_extract(text)}
    item = item_by_handle(handle.group(1)) if handle else None
    if item is None:
        return {"item": {"relevant": False, "topic": "unreadable", "summary": "Nothing readable on this item.",
                         "claims": [], "on_screen_text": "", "tags": [], "quality": 1, "key_frame": 1, "ad": False}}
    theme = THEME_NAMES.get(item.theme, (item.theme or "ad").capitalize())
    on_screen = item.on_screen if pictures else ""
    summary = f"{item.caption}." if not pictures else f"{item.caption}: {item.title.title()}, {item.stat}."
    return {"item": {"relevant": item.relevant, "topic": theme.casefold(), "summary": summary[:200],
                     "claims": list(item.claims) if pictures else [], "on_screen_text": on_screen,
                     "tags": list(item.tags), "quality": item.quality, "key_frame": 1, "ad": item.kind == "ad"}}


def chat_extract(text):
    """An inbox sweep's read of one conversation (extract.CHAT_SCHEMA), as a real model writes it: whose turn it is,
    their last message, a draft when it's the person's turn, and what the person must decide when they're invited."""
    name = next((name for name in PROFILE_BY_NAME if re.search(rf"\b{name}\b", text)), None)
    profile = PROFILE_BY_NAME.get(name)
    if profile is None or not profile.messages:
        return {"title": name or "", "summary": "A new match with no messages yet." if profile else "",
                "last_from": "unknown", "last_message": "", "needs_reply": False, "bucket": "wait", "decision": "",
                "draft": "", "relevant": True, "quality": 1}
    who, last = profile.messages[-1]
    theirs = who == "them" and profile.state == "your_turn"
    invite = theirs and re.search(r"\b(come|show|friday|sometime)\b", last, re.I)
    draft = ("I'd love to, [yes / not this time]!" if invite
             else "Good question! Let me tell you over a coffee.") if theirs else ""
    return {"title": profile.name, "summary": f"{profile.name} wrote: {last}"[:200],
            "last_from": "them" if who == "them" else "person", "last_message": last, "needs_reply": theirs,
            "bucket": "reply" if theirs else "wait", "decision": f"Whether you want to: {last}"[:120] if invite else "",
            "draft": draft, "relevant": True, "quality": 2}


def profile_extract(text, asked=""):
    """``asked``: the extractor's system prompt (it carries the request): a field the request asks for that the
    profile states (a religion) is a fit note, as a real model writes it."""
    match = re.search(r"^Name: (\w+)", text, re.M) or re.search(r"\b(" + "|".join(PROFILE_BY_NAME) + r"), \d+", text)
    profile = PROFILE_BY_NAME.get(match.group(1)) if match else None
    if profile is None:
        return {"name": "", "age": "", "location": "", "work": "", "prompts": [], "details": [], "interests": [],
                "conversation": {"state": "unknown", "last_message": "", "their_questions": 0}, "photo_notes": [],
                "fit_notes": [], "summary": "Couldn't read this profile."}
    fit = []
    interests = " ".join([*profile.interests, *(a for _, a in profile.prompts)]).casefold()
    if "climb" in interests or "boulder" in interests:
        fit.append("Climbs, which you asked for")
    if "ramen" in interests:
        fit.append("Loves ramen, like you")
    if profile.kids in ("Want someday", "Open to children"):
        fit.append(f"Children: {profile.kids.casefold()}")
    if any(place in profile.location.casefold() for place in NEAR_BROOKLYN):
        fit.append(f"Lives in {profile.location}")
    if profile.religion and re.search(rf"\b{re.escape(profile.religion)}\b", asked or "", re.I):
        fit.append(f"Religion: {profile.religion}, which you asked for")
    return {"name": profile.name, "age": str(profile.age), "location": profile.location, "work": profile.work,
            "prompts": [{"prompt": p, "answer": a} for p, a in profile.prompts],
            "details": [f"Children: {profile.kids}"] + ([f"Religion: {profile.religion}"] if profile.religion else []),
            "interests": list(profile.interests),
            "conversation": {"state": profile.state, "last_message": last_message(profile),
                             "their_questions": profile.questions},
            "photo_notes": ["a photo outdoors"], "fit_notes": fit,
            "summary": f"{profile.name}, {profile.age}, {profile.work} in {profile.location}; "
                       f"into {', '.join(profile.interests)}."}



def claims(messages, schema):
    """True when a call is the long-form world's: extraction, a digest, a report, a follow-up, or any call for one of
    the three long-form tasks (sota_world.ScriptedModel hands it over)."""
    props = (schema or {}).get("properties") or {}
    if any(key in props for key in ("item", "digest", "report", "answerable")):
        return True
    try:
        from mobile_agent.frontier import prompt_text
        text = prompt_text(messages)
    except Exception:
        return False
    match = re.search(r"^Request: (.*)$", text, re.M)
    return bool(match and task_of(re.sub(r"^In [^:]{1,40}: ", "", match.group(1))))
