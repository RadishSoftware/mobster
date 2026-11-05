"""Deterministic answers for core's long-form digest and report calls, and two recorded notebooks."""

import re

from mobile_agent.longform.notebook import Item, Notebook

THEMES = {
    "footwork": ("footwork drill", ["3-cone first-step drill at game speed", "jab-step series to beat a closeout",
                                    "ladder footwork for a quicker first step", "pivot drill out of the triple threat",
                                    "euro-step footwork, slowed down", "two-foot jump stops in traffic",
                                    "drop-step footwork for post players", "hesitation footwork to freeze defenders"],
                 ["Keep your hips low through the cone", "Sell the jab with your eyes", "Land on two feet to keep both "
                  "pivots"]),
    "shooting": ("shooting form",
                 ["one-motion jumper vs two-motion, side by side", "elbow under the ball, cued by tape",
                  "form shooting from 3 feet, 50 makes", "free throw routine with a breath",
                  "fixing a thumb flick on the guide hand", "corner three footwork into the shot",
                  "game-speed catch and shoot off a screen"],
                 ["Start close and move back only after 5 makes in a row", "The guide hand never pushes"]),
    "trades": ("trade rumors", ["deadline rumor: a contender wants a backup point guard",
                                "rumor roundup of three teams chasing a rim protector",
                                "a creator disputes the backup point guard rumor",
                                "salary-matching explainer for a deadline deal",
                                "fans react to a rumored three-team trade", "insider says the star stays put"],
               ["Two creators disagree on whether the guard is available"]),
    "wnba": ("WNBA highlights", ["a rookie's 30-point game in the WNBA", "WNBA playoff buzzer beater",
                                 "WNBA assist leader's best passes", "a WNBA defensive player of the year case",
                                 "WNBA attendance record explained"],
             ["Attendance hit a record this season, one creator says"]),
    "defense": ("defense tips",
                ["closeout technique: short choppy steps", "help defense rotations, drawn on a whiteboard",
                 "how to take a charge safely", "on-ball defense: hand on the ball side",
                 "switching on pick-and-roll, explained", "denying the wing without fouling",
                 "boxing out before the shot goes up", "communication drill: call every screen"],
                ["Talk on every screen", "Chop your feet on the closeout"]),
    "conditioning": ("conditioning",
                     ["suicides vs shuttle runs for game shape", "a 20-minute court conditioning circuit",
                      "jump rope routine for footwork and wind", "recovery: mobility after a game",
                      "single-leg strength for knee health", "sprint intervals in season",
                      "vertical jump plyometrics, 3 days a week", "why rest days help your shot",
                      "core work for balance on contact", "a pro trainer's off-season week"],
                     ["Rest days are part of the program"]),
}
CREATORS = ["@hoopslab", "@courtvision", "@buckets.daily", "@rimrunner", "@thepostmove", "@fullcourtpress",
            "@swishschool", "@benchmob.tv", "@paintpatrol", "@deeprange"]
ADS = [(7, "@shoesdrop", "Sponsored: new court shoes, 20% off. Shop now"),
       (19, "@protein.co", "Sponsored: protein shakes for athletes. Learn more"),
       (28, "@hoopcamp", "Sponsored: summer camp sign-ups. Sign up"),
       (41, "@courtapp", "Sponsored: book a court near you. Install now"),
       (53, "@ballbrand", "Sponsored: the official game ball. Shop now")]
OFF_TOPIC = [(13, "@pastaqueen", "a 10-minute weeknight pasta", "cooking"),
             (37, "@roamfar", "three days in Lisbon on a budget", "travel")]
REPEATS = (24, 33, 47)        # positions where an earlier Reel comes back


def feed_items():
    """(position, Item, extraction or None) in the order the feed showed them: 46 on basketball across 6 themes,
    2 off topic, 5 ads, 3 repeats (56 seen)."""
    basketball = []
    for theme, (topic, summaries, claims) in THEMES.items():
        for index, summary in enumerate(summaries):
            basketball.append((theme, topic, summary, claims[index % len(claims)]))
    # Interleave the themes so they don't come in blocks, as a feed wouldn't.
    order = sorted(range(len(basketball)), key=lambda i: (i * 7) % len(basketball))
    basketball = [basketball[i] for i in order]
    ads = {pos: (who, text) for pos, who, text in ADS}
    off = {pos: (who, text, tag) for pos, who, text, tag in OFF_TOPIC}
    out, kept_so_far, position, b = [], [], 1, 0
    while b < len(basketball) or position in ads or position in off:
        if position in ads:
            who, text = ads[position]
            out.append((position, Item(kind="reel", app="com.burbn.instagram", identity=f"ad-{position}",
                                       source={"creator": who}, text={"caption": text}), None, "ad"))
        elif position in off:
            who, text, tag = off[position]
            out.append((position, Item(kind="reel", app="com.burbn.instagram", identity=f"off-{position}",
                                       source={"creator": who}, text={"caption": text}),
                        {"relevant": False, "topic": tag, "summary": text, "claims": [], "tags": [tag],
                         "quality": 1, "key_frame": 0, "ad": False}, None))
        elif position in REPEATS and kept_so_far:
            earlier = kept_so_far[(position * 3) % len(kept_so_far)]
            out.append((position, Item(kind="reel", app="com.burbn.instagram", identity=earlier.identity,
                                       source=dict(earlier.source), text=dict(earlier.text)), None, "repeat"))
        else:
            theme, topic, summary, claim = basketball[b]
            b += 1
            creator = CREATORS[(b * 3) % len(CREATORS)]
            item = Item(kind="reel", app="com.burbn.instagram", identity=f"reel-{b}", source={"creator": creator},
                        text={"caption": f"{summary} #basketball", "on_screen": summary.split(",")[0].upper()[:60]})
            extract = {"relevant": True, "topic": topic, "summary": summary[:1].upper() + summary[1:] + ".",
                       "claims": [claim], "on_screen_text": summary.split(",")[0].upper()[:60],
                       "tags": [theme, "basketball"], "quality": 3 if b % 4 == 0 else 2, "key_frame": 1, "ad": False}
            out.append((position, item, extract, None))
            kept_so_far.append(item)
        position += 1
    return out


def feed_notebook(run_id="r_feed"):
    """The recorded feed notebook: "Scroll Instagram Reels about basketball for 10 minutes and tell me what you
    learned", with every item extracted, a digest and a few facts the model noted."""
    book = Notebook(run_id, "Scroll Instagram Reels about basketball for 10 minutes and tell me what you learned",
                    "feed", "basketball", (), [{"id": 1, "title": "Open Instagram and find basketball Reels",
                                                "app": "Instagram", "state": "done"},
                                               {"id": 2, "title": "Watch about 50 Reels", "app": "Instagram",
                                                "state": "done"},
                                               {"id": 3, "title": "Write up what it learned", "app": None,
                                                "state": "now"}])
    for position, item, extract, skip in feed_items():
        item.at = position * 2.0
        item.skip = "ad" if skip == "ad" else None
        stored = book.add_item(item)
        if extract is not None and stored.skip is None:
            book.update_item(stored.id, extract=extract, skip="off_topic" if extract.get("relevant") is False else None)
    book.add_fact("Two creators teach the same 3-cone first-step drill with different rep counts", ())
    book.add_fact("Creators disagree on whether the backup point guard is available at the deadline", ())
    book.set_digest({"text": "Footwork and defense drills dominate; trade rumors disagree with each other.",
                     "themes": [], "up_to_item": f"i{len(book.items)}", "turn": 9})
    return book


PREFERENCES = ["into climbing", "into ramen", "wants kids someday", "lives near Brooklyn"]
PROFILES = [
    ("Riley", 29, "Brooklyn", "Product designer", ["climbing", "ramen"], "Wants children: open to", "your_turn"),
    ("Jordan", 31, "Brooklyn", "Nurse", ["climbing", "jazz"], "Wants children: wants someday", "your_turn"),
    ("Avery", 28, "Queens", "Chef", ["ramen", "cycling"], "Wants children: wants someday", "their_turn"),
    ("Sam", 33, "Manhattan", "Lawyer", ["golf"], "Wants children: doesn't want", "their_turn"),
    ("Casey", 30, "Brooklyn", "Teacher", ["climbing", "ramen", "hiking"], "Wants children: wants someday",
     "your_turn"),
    ("Morgan", 27, "Jersey City", "Analyst", ["running"], "Wants children: not sure", "their_turn"),
    ("Quinn", 34, "Brooklyn", "Architect", ["ramen", "museums"], "Wants children: open to", "no_messages"),
    ("Drew", 29, "Bronx", "Musician", ["guitar"], "Wants children: not sure", "their_turn"),
    ("Taylor", 32, "Brooklyn", "Engineer", ["board games"], "Wants children: wants someday", "your_turn"),
    ("Parker", 26, "Hoboken", "Barista", ["coffee", "climbing"], "Wants children: doesn't want", "their_turn"),
    ("Reese", 30, "Manhattan", "Doctor", ["tennis"], "Wants children: wants someday", "your_turn"),
    ("Rowan", 28, "Queens", "Photographer", ["photography", "ramen"], "Wants children: open to", "their_turn"),
    ("Emerson", 35, "Brooklyn", "Writer", ["reading"], "Religion: Catholic", "no_messages"),
    ("Hayden", 29, "Staten Island", "Firefighter", ["fishing"], "Wants children: wants someday", "their_turn"),
    ("Blake", 31, "Brooklyn", "Data scientist", ["climbing"], "Wants children: open to", "their_turn"),
]
GROUND_TRUTH = ["Casey", "Riley", "Jordan"]


def matches_notebook(run_id="r_matches"):
    request = ("Go through my Hinge matches and find the most promising ones. I'm into climbing and ramen, want kids "
               "someday, and live in Brooklyn.")
    book = Notebook(run_id, request, "survey", None, PREFERENCES,
                    [{"id": 1, "title": "Open Hinge and go to your matches", "app": "Hinge", "state": "done"},
                     {"id": 2, "title": "Read each of up to 40 matches", "app": "Hinge", "state": "done"},
                     {"id": 3, "title": "Rank them and write it up", "app": None, "state": "now"}])
    for index, (name, age, where, work, interests, detail, state) in enumerate(PROFILES):
        item = book.add_item(Item(kind="profile", app="co.hinge.mobile.ios", identity=f"profile-{name}",
                                  source={"name": name}, text={"details": [detail, where]}))
        fit = []
        if "climbing" in interests:
            fit.append("Climbs, which you asked for")
        if "ramen" in interests:
            fit.append("Likes ramen, which you asked for")
        if "wants someday" in detail or "open to" in detail:
            fit.append("Open to kids")
        if where == "Brooklyn":
            fit.append("Lives in Brooklyn")
        book.update_item(item.id, extract={
            "name": name, "age": str(age), "location": where, "work": work,
            "prompts": [{"prompt": "A perfect Sunday", "answer": f"{interests[0]} and then dinner"}],
            "details": [detail], "interests": interests,
            "conversation": {"state": state, "last_message": "See you soon?" if state == "your_turn" else "",
                             "their_questions": 1 if state == "your_turn" else 0},
            "photo_notes": ["a hiking photo"] if "hiking" in interests else ["a photo at a concert"],
            "fit_notes": fit, "summary": f"{name}, {age}, {work} in {where}; into {', '.join(interests)}."})
    return book


LINE = re.compile(r"^(i\d+) \| ([^|]*) \| ([^|]*) \| ([^|]*)(.*)$", re.M)
NOTE = re.compile(r"^- (f\d+)(?: \[[^\]]*\])? (.+)$", re.M)


def _text(messages):
    out = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            if message.get("role") != "system":
                out.append(content)
            continue
        for part in content or ():
            if part.get("type") == "text":
                out.append(part["text"])
    return "\n".join(out)


def _lines(text):
    rows = []
    for found in LINE.finditer(text):
        rest = found.group(5)
        tags = re.search(r"\| tags: ([^|]+)", rest)
        rows.append({"id": found.group(1), "who": found.group(2).strip(), "topic": found.group(3).strip(),
                     "summary": found.group(4).strip(), "rest": rest,
                     "tags": [t.strip() for t in tags.group(1).split(",")] if tags else []})
    return rows


def _groups(rows):
    groups, order = {}, []
    for row in rows:
        key = next((t for t in row["tags"] if t != "basketball"), None) or row["topic"] or "other"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    order.sort(key=lambda k: -len(groups[k]))
    return groups, order


def digest_answer(text):
    rows = _lines(text)
    groups, order = _groups(rows)
    themes = [{"name": key, "item_ids": [r["id"] for r in groups[key]]} for key in order[:8]]
    summary = "; ".join(f"{key} ({', '.join(r['id'] for r in groups[key][:3])})" for key in order[:4])
    return {"digest": {"text": f"So far: {summary}."[:600], "themes": themes}}


def chunk_answer(text):
    rows = _lines(text)
    groups, order = _groups(rows)
    return {"chunk": {"themes": [{"name": key, "summary": groups[key][0]["summary"],
                                  "item_ids": [r["id"] for r in groups[key]]} for key in order]}}


def asked_for(text):
    """What the person asked a ranking for, as a model reads it from the synthesis prompt: the request after its first
    sentence (the task itself) and the conversation's standing preferences."""
    request = re.search(r"^Request: (.+)$", text, re.M)
    parts = re.split(r"(?<=[.!?])\s+", request.group(1).strip(), maxsplit=1) if request else []
    wanted = [parts[1]] if len(parts) > 1 else []
    standing = re.search(r"standing preferences in this conversation \(rank against these too\): (.+)$", text, re.M)
    if standing:
        wanted += [p.strip() for p in standing.group(1).split(";")]
    return wanted


def report_answer(text):
    rows = _lines(text)
    if "Kind: survey" in text:
        return survey_report(rows, asked_for(text))
    if not rows:
        # A chain: no items, the agent's notes and its final answer.
        notes = NOTE.findall(text)
        if not notes:
            return {"report": {"title": "What Mobster did", "tldr": "", "sections": [], "ranked": [], "gaps": []}}
        final = next((note for note in reversed(notes) if note[1].startswith("Final answer:")), None)
        found = [note for note in notes if note is not final]
        sections = [{"heading": "What it found", "bullets": [{"text": body, "sources": [ident]}
                                                             for ident, body in found[:6]]}] if found else []
        if final is not None:
            sections.append({"heading": "What it did", "bullets": [
                {"text": final[1].split(":", 1)[1].strip(), "sources": [final[0]]}]})
        tldr = final[1].split(":", 1)[1].strip() if final is not None else found[0][1]
        return {"report": {"title": "What Mobster did", "tldr": tldr, "sections": sections, "ranked": [],
                           "gaps": []}}
    groups, order = _groups(rows)
    sections = []
    for key in order[:6]:
        bullets = [{"text": row["summary"], "sources": [row["id"]]} for row in groups[key][:4]]
        sections.append({"heading": f"{key[:1].upper() + key[1:]} keeps coming up ({len(groups[key])} Reels)",
                         "bullets": bullets})
    return {"report": {"title": f"What I learned from {len(rows)} Reels",
                       "tldr": f"{order[0] if order else 'Nothing'} was the biggest theme." if order else "",
                       "sections": sections, "ranked": [], "gaps": []}}


def followup_answer(text):
    """A follow-up about an earlier report: answerable when the question names something in the report's lines (a
    name, a handle or a theme), with the lines' item ids as sources."""
    question = text.rsplit("Question:", 1)[-1].strip()
    words = {w for w in re.findall(r"[a-z@][a-z0-9@._-]{3,}", question.casefold())
             if w not in ("what", "which", "about", "that", "with", "from", "were", "does", "they", "there", "their",
                          "this", "have", "most", "best", "first", "second", "third", "report", "earlier")}
    rows = [row for row in _lines(text) if words & set(re.findall(r"[a-z@][a-z0-9@._-]{3,}",
                                                                   (row["who"] + " " + row["summary"]).casefold()))]
    if not rows:
        return {"answerable": False, "answer": "", "sources": []}
    return {"answerable": True, "answer": "; ".join(f"{row['who']}: {row['summary']}" for row in rows[:3]),
            "sources": [row["id"] for row in rows[:3]]}


def _field(row, name):
    found = re.search(rf"\| {name}: ([^|]+)", row["rest"])
    return found.group(1).strip() if found else ""


def survey_report(rows, wanted):
    """A shortlist from the profile lines: scored on the stated preferences' words, then whose turn it is; each bullet
    states what the profile says (interests, details, the conversation) in its own words."""
    words = {w for p in wanted for w in re.findall(r"[a-z]{4,}", p.casefold())
             if w not in ("into", "wants", "want", "lives", "live", "near", "prefer", "someone", "would", "like")}
    scored = []
    for row in rows:
        blob = (row["topic"] + " " + row["summary"] + " " + row["rest"]).casefold()
        score = sum(1 for word in words if word in blob)
        if re.search(r"(?:don'?t|doesn'?t|do not|does not) want (?:kids|children)", blob):
            score -= 1
        score += 0.5 if "your_turn" in blob else 0
        scored.append((score, row))
    scored.sort(key=lambda pair: -pair[0])

    def said(row):
        parts = [row["summary"].rstrip(".")]
        details = _field(row, "details")
        if details:
            parts.append(details)
        conversation = _field(row, "conversation")
        if "your_turn" in conversation:
            last = re.search(r"last_message=([^,]+(?:,[^=,]+)*?)(?:, \w+=|$)", conversation)
            parts.append("your turn: they asked " + (last.group(1).strip() if last else "you a question"))
        elif "their_turn" in conversation:
            parts.append("their turn")
        text = "; ".join(parts) + "."
        return text if text.startswith(row["who"]) else f"{row['who']}: {text}"

    ranked = []
    for n, (score, row) in enumerate(scored[:5], 1):
        fits = [w.strip() for w in _field(row, "fit").split(";") if w.strip()]
        fits.sort(key=lambda w: -sum(word in w.casefold() for word in words))   # what was asked for first
        why = fits[:3] or [row["summary"]]
        conversation = _field(row, "conversation")
        if "your_turn" in conversation:
            why.append("Asked you a question: your turn")
        ranked.append({"rank": n, "itemId": row["id"], "name": row["who"], "why": why, "cautions": [],
                       "state": "Your turn" if "your_turn" in conversation else
                       "Their turn" if "their_turn" in conversation else None})
    groups = (("Best fits for what you asked", scored[:3]), ("Close, with one gap each", scored[3:6]),
              ("The rest", scored[6:12]))
    sections = [{"heading": heading, "bullets": [{"text": said(row), "sources": [row["id"]]} for _, row in group]}
                for heading, group in groups if group]
    top = " and ".join(entry["name"] for entry in ranked[:2])
    return {"report": {"title": f"The most promising of {len(rows)} matches",
                       "tldr": f"{top} fit what you asked for best." if ranked else "Nobody fits yet.",
                       "sections": sections, "ranked": ranked, "gaps": []}}


def respond(messages, schema):
    """The deterministic answer for a long-form schema (``digest``, ``report``, ``chunk``, ``answerable``), or
    None."""
    keys = set((schema or {}).get("properties") or {})
    text = _text(messages)
    if "digest" in keys:
        return digest_answer(text)
    if "report" in keys:
        return report_answer(text)
    if "chunk" in keys:
        return chunk_answer(text)
    if "answerable" in keys:
        return followup_answer(text)
    return None


class ScriptedLongClient:
    """A client for the digest, chunk and report calls: ``complete`` answers with ``respond``; ``fail`` makes the next
    calls raise (a synthesis that fails); ``calls`` records (schema key, prompt text)."""

    def __init__(self, model="claude-sonnet-5-5", fail=0, error=RuntimeError):
        self.model, self.fail, self.error = model, fail, error
        self.calls = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}
        self.provider = "anthropic"

    def complete(self, messages, schema, timeout=60, **kwargs):
        key = next(iter((schema or {}).get("properties") or {}), None)
        self.calls.append((key, _text(messages)))
        if self.fail:
            self.fail -= 1
            raise self.error("scripted failure")
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += len(_text(messages)) // 4
        self.usage["completion_tokens"] += 300
        return respond(messages, schema), {"prompt_tokens": len(_text(messages)) // 4, "completion_tokens": 300}
