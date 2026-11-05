"""The deterministic front door: a request that asks only for what a pack reader shows, or for a Settings page, is
answered by code before any model call. It never acts: a request that holds an act word is never routed."""

import os
import re
import time
from dataclasses import dataclass, replace

from .. import harness_api
from . import core, health, pins, settings, tools

ORDER = 2                       # after harness and long-form pick-up (0, 1), before the packs' own run fields (3)
MAX_GOAL = 200
MAX_N = 40
# Run fields a routed run may carry: a conversation's id, who asked, the permission mode. Anything else (a workflow's
# input, a destination, an account, a trigger, attachments, a replay mode) is a run the table wasn't written for.
SAFE_EXTRAS = frozenset({"threadId", "via", "permissionMode"})
# An app's name that is also what a request asks for ("messages"): the app is named by an alias, or started in.
NOUNS = frozenset({"messages"})
ALIASES = {"ig": "instagram", "insta": "instagram", "twitter": "x", "imessage": "messages",
           "imessages": "messages"}

# Words a request may hold besides an ask's own: politeness, question frames, determiners, "latest".
STOP = frozenset("""a an the my of on in for to at up me please show tell give get read list check what whats what's
is are do does i have how much many now currently right latest last newest recent recently most top first this that
app""".split())
# Any of these in a request, and nothing is routed. Wider than the table needs: a word here only costs a model call.
ACTS = frozenset("""send sending sent post posting repost share buy purchase pay order book delete remove erase reset
like unlike follow unfollow block report mute restrict reply respond comment subscribe accept decline approve confirm
turn toggle enable disable change set update install uninstall clear add create write type call dial save unsend
submit cancel forward archive move rename edit join leave sign log unlock restart shut allow deny schedule invite
tag pin hide unhide""".split())
OPEN_VERBS = frozenset({"open", "go", "launch", "navigate", "take"})
OPEN_FILLER = frozenset({"to", "the", "my", "me", "app", "please", "up", "in", "and"})
SEPARATORS = re.compile(r"\s*(?:›|»|→|->|=>|>|/)\s*")
POLITE = re.compile(r"^(?:(?:hey|hi|ok|okay)[ ,]+)?(?:please[ ,]+)?"
                    r"(?:(?:can|could|would|will) you[ ,]+(?:please[ ,]+)?)?"
                    r"|[ ,.]*please[ .!?]*$", re.I)


def phrases(*sets):
    return tuple(frozenset(s.split()) for s in sets)


def with_phone(*sets):
    """Each set as is, and with the phone named ("what iOS version is this iPhone on")."""
    out = []
    for base in sets:
        out.append(base)
        out += [f"{base} {referent}" for referent in ("phone", "iphone", "device")]
    return phrases(*out)


@dataclass(frozen=True)
class Ask:
    pack: str                       # the pack's key
    reader: str                     # one of the pack's readers
    words: tuple                    # the content words the request may hold, as frozensets: one of these exactly
    pick: tuple = ()                # (column, regex): answer only with the rows whose column matches
    counted: bool = False           # "my last 10 DMs": a count is part of the ask
    needs_app: bool = True          # the app must be named, started in, or in front (False: Settings' own words)


ASKS = (
    Ask("settings", "about", with_phone("ios version", "software version", "version ios"),
        pick=("setting", r"^iOS Version$"), needs_app=False),
    Ask("settings", "about", with_phone("model", "model name"), pick=("setting", r"^Model Name$"), needs_app=False),
    Ask("settings", "storage", with_phone("storage", "free storage", "storage left", "storage space", "space left",
                                          "free space", "available storage", "storage available",
                                          "storage remaining", "free storage space"),
        pick=("item", r"^(Used|Capacity|Free)$"), needs_app=False),
    Ask("settings", "battery", with_phone("battery", "battery level", "battery percentage", "battery percent"),
        pick=("setting", r"^Level$"), needs_app=False),
    Ask("settings", "battery", with_phone("battery health"), pick=("setting", r"^Battery Health$"), needs_app=False),
    Ask("instagram", "dms", phrases("dms", "dm", "direct messages", "messages"), counted=True),
    Ask("tiktok", "dms", phrases("dms", "dm", "inbox", "messages"), counted=True),
    Ask("x", "messages", phrases("dms", "dm", "messages"), counted=True),
    Ask("messages", "inbox", phrases("conversations", "messages", "texts", "chats"), counted=True),
    Ask("messages", "unread", phrases("unread messages", "unread texts", "unread conversations", "unread"),
        counted=True),
    Ask("calendar", "events", phrases("events", "upcoming events", "upcoming"), counted=True),
)

# Settings pages a request may open, as Settings names them, and the one parent a nested path must name. A page
# nobody listed here (and a path nobody checked) goes to the model.
SETTINGS_PAGES = {name.casefold(): name for name in settings.PAGES}
PARENTS = {"about": "General", "iphone storage": "General", "software update": "General"}
OWN_ROUTES = {"About": "about", "iPhone Storage": "storage", "Battery": "battery"}


@dataclass(frozen=True)
class Plan:
    pack: core.Pack
    kind: str                       # "read" or "open"
    what: str                       # the reader or the route
    n: int = 10
    arg: str = None
    ask: Ask = None
    page: str = None                # an open's page, as Settings names it


def tokens(text):
    return re.findall(r"[a-z0-9&']+", str(text or "").casefold().replace("’", "'"))


def packs_by_word():
    """{word: pack} for every pack's name and key and the aliases people use."""
    out = {}
    for pack in registered():
        for word in {pack.key, pack.name.casefold()} - NOUNS:
            out[word] = pack
    for alias, key in ALIASES.items():
        pack = next((p for p in registered() if p.key == key), None)
        if pack is not None:
            out[alias] = pack
    return out


def registered():
    from . import register
    return register.all_packs()


def reads_an_act(goal, words):
    """Whether the request holds an act: an act word, or what the safety gate reads as one."""
    from .. import harness_api as api
    return (any(word in ACTS for word in words) or bool(core.NEVER.search(goal))
            or api.commit_act("", goal, None, None) is not None)


def parse(goal, *, app_bundle=None, front_bundle=None):
    """The ``Plan`` for a request that is exactly an ask in the table or an open of a Settings page, else None."""
    text = POLITE.sub("", str(goal or "").strip())
    if not text or len(text) > MAX_GOAL or "\n" in text:
        return None
    words = tokens(text)
    if not words or reads_an_act(text, words):
        return None
    if words[0] in OPEN_VERBS:
        return parse_open(text)
    return parse_read(words, app_bundle, front_bundle)


def parse_read(words, app_bundle, front_bundle):
    by_word = packs_by_word()
    named = {by_word[w].key: by_word[w] for w in words if w in by_word}
    if len(named) > 1:
        return None                                    # two apps in one request: the model decides what is meant
    numbers = [w for w in words if w.isdigit()]
    if len(numbers) > 1 or (numbers and not 1 <= int(numbers[0]) <= MAX_N):
        return None
    content = frozenset(w for w in words if w not in STOP and not w.isdigit() and w not in by_word)
    matches = []
    for ask in ASKS:
        pack = next((p for p in registered() if p.key == ask.pack), None)
        if pack is None or ask.reader not in pack.readers or (named and ask.pack not in named):
            continue
        if content not in ask.words or (numbers and not ask.counted):
            continue
        if ask.needs_app and not (ask.pack in named or pack.bundle in (app_bundle, front_bundle)):
            continue
        matches.append((pack, ask))
    if len(matches) != 1:
        return None
    pack, ask = matches[0]
    n = int(numbers[0]) if numbers else pack.read_default.get(ask.reader, 10)
    return Plan(pack, "read", ask.reader, n, None, ask)


def parse_open(text):
    """"open Settings", "go to Settings › General › About", "open settings > battery": Settings' own pages."""
    parts = [p for p in SEPARATORS.split(text) if p.strip()]
    first = [w for w in tokens(parts[0]) if w not in OPEN_VERBS and w not in OPEN_FILLER]
    rest = [" ".join(tokens(p)) for p in parts[1:]]
    if len(parts) == 1 and first and first[0] == "settings":
        rest, first = [" ".join(first[1:])] if len(first) > 1 else [], ["settings"]
    if first != ["settings"] or len(rest) > 2:
        return None
    pack = next((p for p in registered() if p.key == "settings"), None)
    if pack is None:
        return None
    if not rest:
        return Plan(pack, "open", "root")
    pages = [SETTINGS_PAGES.get(name) for name in rest]
    if None in pages:
        return None
    page = pages[-1]
    if len(pages) == 2 and PARENTS.get(page.casefold()) != pages[0]:
        return None                                    # a path nobody checked: not claimed
    route = OWN_ROUTES.get(page, "find")
    return Plan(pack, "open", route, arg=page if route == "find" else None, page=page)


def enabled():
    return os.environ.get("MOBSTER_FRONT_DOOR", "").strip().lower() not in ("off", "0", "false", "no")


def eligible(ctx):
    if not enabled() or getattr(ctx, "engine", None) != "smart" or not getattr(ctx, "goal", None):
        return False
    if set(ctx.extras or {}) - SAFE_EXTRAS:
        return False
    run = (getattr(getattr(ctx, "runtime", None), "runs", None) or {}).get(ctx.run_id)
    if run is not None and (getattr(run, "output_schema", None) is not None
                            or getattr(run, "output_format", None) in _structured()
                            or getattr(run, "longform", None) or getattr(run, "pinned_apps", None)):
        return False                                   # these need the model's structuring or its plan
    return True


def _structured():
    from .. import smart_result
    return smart_result.AUTOMATIC_FORMATS


def front_app(driver):
    """The bundle id in front on the phone when the connection can say, else None."""
    reader = getattr(driver, "active_app", None)
    if callable(reader) and getattr(driver, "reads_front_app", True):
        try:
            bundle = reader()
        except Exception:  # noqa: BLE001 -- can't tell: not named
            return None
        return bundle if isinstance(bundle, str) else None
    return None


def hook(ctx, driver, approve):
    """harness_api pre-run hook: answer the request by code, or None and the run goes on as it would."""
    if driver is None or not eligible(ctx):
        return None
    words = tokens(ctx.goal)
    # The phone is asked which app is in front only for a request that could use it (a bare "my DMs").
    plan = parse(ctx.goal, app_bundle=ctx.app_bundle) or (
        parse(ctx.goal, app_bundle=ctx.app_bundle, front_bundle=front_app(driver)) if words else None)
    if plan is None:
        return None
    pinned = pins.aside(plan.pack)
    if pinned:
        ctx.emit({"event": "front_door", "routed": False, "pack": plan.pack.key, "why": pinned})
        return None                                    # the general agent does it
    gate = tools.run_gate(ctx.run_id, ctx.data_dir)
    version = core.app_version(ctx.runtime, plan.pack.bundle)
    trusted, why = gate.trust("pack", plan.pack.bundle, plan.what, None, version)
    if not trusted:
        ctx.emit({"event": "front_door", "routed": False, "pack": plan.pack.key, "why": why})
        return None
    from .register import _io
    pack, started = plan.pack, time.monotonic()
    ctx.emit({"event": "skill_started", "op": f"{pack.prefix}_{'READ' if plan.kind == 'read' else 'OPEN'}"})
    io = _io(ctx, driver, pack)
    try:
        answer = _read(pack, io, plan) if plan.kind == "read" else _open(pack, io, plan)
    except core.Lost as lost:
        answer, why = None, str(lost).rstrip(".")
    ok = answer is not None
    gate.note(ok, "pack", pack.bundle)
    event = {"pack": pack.key, "op": f"{pack.prefix}_{plan.kind.upper()}", "step": plan.what, "ok": ok, "door": True,
             "ms": round((time.monotonic() - started) * 1000), "taps": io.taps, "version": version,
             "how": "code", **({} if ok else {"why": why})}
    ctx.emit({"event": "app_pack", **event})
    health.record(ctx.data_dir,
                  {k: v for k, v in event.items() if k != "why"} | ({} if ok else {"why": tools._quiet(why)}))
    if not ok:
        ctx.emit({"event": "front_door", "routed": False, "pack": pack.key, "why": why})
        return None                                    # the model takes the phone where the code left it
    return harness_api.PreRun(result=_result(ctx, answer, started))


def _read(pack, io, plan):
    result = pack.read(io, plan.what, plan.n, plan.arg)
    rows = list(result.rows)
    if plan.ask.pick:
        column, pattern = plan.ask.pick
        rows = [row for row in rows if re.search(pattern, str(row.get(column) or ""), re.I)]
    if not rows:
        raise core.Lost(f"{pack.read_titles.get(plan.what, plan.what)} showed nothing it could name")
    columns = result.columns
    keep = rows[:tools.FEEDBACK_ROWS]
    lines = "\n".join(_pair(row, columns) if plan.ask.pick else
                      tools.row_line(i + 1, row, columns, pack.required.get(plan.what, ()))
                      for i, row in enumerate(keep))
    note = f" It read {len(rows)} of the {plan.n} you asked for: {result.stopped}." if (
        plan.ask.counted and len(rows) < plan.n and result.stopped) else ""
    block = None if plan.ask.pick else tools.block_for(pack, replace(result, rows=rows))
    return {"text": f"{pack.name}, {result.title}, as the screen shows it:\n{lines}{note}", "rows": rows,
            "columns": columns, "title": result.title, "stopped": result.stopped, "block": block}


def _pair(row, columns):
    """One picked row as "iOS Version: 26.0.1" (its first two columns)."""
    keys = [key for key, _label, _kind in columns][:2]
    return ": ".join(core.plain(row.get(key)) for key in keys if row.get(key) not in (None, ""))


def _open(pack, io, plan):
    landing = core.navigate(pack, io, plan.what, plan.arg)
    if not landing.ok:
        raise core.Lost(landing.why)
    page = plan.page or "Settings"
    return {"text": f"Opened {page} in {pack.name}. The screen is {landing.seen.title}.", "rows": [], "columns": [],
            "title": page, "stopped": None, "block": None}


def _result(ctx, answer, started):
    from .. import result_blocks as rb
    scalars = (str, int, float, bool, type(None))
    rows = [{k: v for k, v in row.items() if isinstance(v, scalars)} for row in answer["rows"]]
    columns = [{"key": k, "label": label, "type": kind} for k, label, kind in answer["columns"]]
    data = {"rows": rows, "columns": columns, "title": answer["title"], "stopped": answer["stopped"] or None} \
        if rows else None
    blocks = [answer["block"]] if answer["block"] else []
    return {"event": "result", "status": "completed", "engine": "smart", "outcome": "done", "answer": answer["text"],
            "reason": None, "data": data, "actions": 1, "decisions": 0, "costUsd": 0.0, "model": None,
            "elapsed_ms": round((time.monotonic() - started) * 1000), "blocks": rb.envelope(blocks),
            "independently_verified": True, "proof": []}
