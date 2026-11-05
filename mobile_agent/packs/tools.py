"""A pack as two agent tools: ``<PREFIX>_OPEN where`` and ``<PREFIX>_READ what [n]`` (IG_OPEN, IG_READ, TT_OPEN ...)."""

import re
import time
from dataclasses import dataclass

from .. import harness_api
from ..agent_hooks import Prepared, SkillResult, safe_allows
from . import context, core, heal, health, pins

READ_SECONDS = 75.0
OPEN_SECONDS = 30.0
MAX_N = 40
SCREENS_BACK = 40           # screens a call hands to the loop's checklist, newest
FEEDBACK_ROWS = 40
CELL_CHARS = 220
_runs = {}                  # run id -> hints.Gate (both tools of a run share it)


@dataclass(frozen=True)
class PackResult(SkillResult):
    screens: tuple = ()     # the screens the call read, for the loop's checklist (frontier._run_skill)


def run_gate(run_id, data_dir=None):
    """The run's hints.Gate: every source's one gate (two misses in a row, or a step stale at this app version, and the
    general agent does it)."""
    from .. import hints
    if len(_runs) > 256:
        _runs.clear()
    return _runs.setdefault(run_id or "", hints.Gate(data_dir))


def parse_open(pack, text):
    """(route name, argument, act or None) from an OPEN's text, or ValueError with what it takes."""
    words = core.plain(text)
    head, _, rest = words.partition(" ")
    head = head.casefold().rstrip(":")
    alias = pack.open_aliases.get(head, head)
    if alias in pack.controls:
        spec = pack.controls[alias]
        return spec.route, rest.strip() or None, alias
    if alias in pack.routes:
        arg = rest.strip() or None
        if arg and alias in pack.route_with_arg:
            alias = pack.route_with_arg[alias]
        if pack.routes[alias].arg is None:
            arg = None
        return alias, arg, None
    raise ValueError(f"{pack.prefix}_OPEN takes one of: {', '.join(pack.open_words)}")


def parse_read(pack, text):
    """(what, n, argument) from a READ's text: "dms 10", "thread Sam Lee", "reels 20", "profile @nasa"."""
    words = core.plain(text)
    head, _, rest = words.partition(" ")
    what = pack.read_aliases.get(head.casefold(), head.casefold())
    if what not in pack.readers:
        raise ValueError(f"{pack.prefix}_READ takes one of: {', '.join(pack.read_words)}")
    n, arg = pack.read_default.get(what, 10), rest.strip() or None
    if arg:
        found = re.match(r"^(\d{1,3})\b\s*(.*)$", arg)
        if found:
            n, arg = int(found.group(1)), found.group(2).strip() or None
        else:
            found = re.search(r"\s(\d{1,3})$", arg)
            if found:
                n, arg = int(found.group(1)), arg[:found.start()].strip() or None
    return what, max(1, min(MAX_N, n)), arg


def where(element):
    """Where a control sits, as a person says it: "bottom right"."""
    x, y = element.rect[0] + element.rect[2] / 2, element.rect[1] + element.rect[3] / 2
    row = "top" if y < .33 else "middle" if y < .66 else "bottom"
    column = "left" if x < .33 else "centre" if x < .66 else "right"
    return f"{row} {column}"


def row_line(index, row, columns, required=()):
    """One row as a line."""
    parts = []
    for key, label, _kind in columns:
        value = row.get(key)
        if value is None or value == "" or value is False:
            if key in required and value is not False:
                parts.append(f"{label}: not on the screen")
            continue
        value = "yes" if value is True else core.plain(value)[:CELL_CHARS]
        parts.append(f"{label}: {value}")
    return f"{index}. " + "; ".join(parts)


def block_for(pack, result):
    """The result card for ``result``: a media list for Reels and posts, else a table. None for no rows."""
    from .. import result_blocks as rb
    if not result.rows:
        return None
    block_id = f"{pack.key}-{result.kind}"
    if result.block == "media_list":
        items = []
        for index, row in enumerate(result.rows[:rb.MAX_ITEMS]):
            noun = pack.noun(result.kind)
            title = core.plain(row.get("caption") or "")[:120] or f"{noun[:1].upper()}{noun[1:]} by {row.get('author')}"
            item = {"id": f"i{index + 1}", "title": title}
            if row.get("author"):
                item["creator"] = row["author"]
            if row.get("audio") or row.get("sound"):
                item["sounds"] = [row.get("audio") or row.get("sound")]
            if str(row.get("link") or "").startswith("https://"):
                item["link"] = row["link"]
            items.append(item)
        return rb.media_list(items, title=result.title, id=block_id)
    columns = [{"key": key, "label": label, "type": kind} for key, label, kind in result.columns]
    rows = [{"cells": {key: row.get(key) for key, _label, _kind in result.columns}}
            for row in result.rows[:rb.MAX_ROWS]]
    return rb.table(columns, rows, title=result.title, id=block_id)


def _quiet(text):
    """``text`` for the health log: quoted words (names, messages) left out."""
    return re.sub(r"“[^”]*”", "“…”", str(text or ""))[:160]


class PackTool:
    """agent_hooks.Skill: one of a pack's two tools for one run."""

    needs_target = False

    def __init__(self, pack, kind, run_context=None, clock=time.monotonic, sleep=time.sleep):
        self.pack, self.kind = pack, kind
        self.op = f"{pack.prefix}_{kind}"
        self.prompt = pack.prompts[kind]
        self.run_context = run_context
        self.clock, self.sleep = clock, sleep
        self._version = ()
        self._run_id = getattr(run_context, "run_id", None)

    def available(self, ctx):
        driver = getattr(ctx, "driver", None)
        return callable(getattr(driver, "observe", None)) and callable(getattr(driver, "execute", None))

    def prepare(self, ctx, act, snapshot):
        try:
            if self.kind == "OPEN":
                route, arg, act_name = parse_open(self.pack, act.get("text"))
                state = {"route": route, "arg": arg, "act": act_name}
            else:
                what, n, arg = parse_read(self.pack, act.get("text"))
                state = {"what": what, "n": n, "arg": arg}
        except ValueError as error:
            return Prepared(None, {"error": str(error)})
        guard = getattr(ctx, "guard", None)
        if guard is not None and not safe_allows(guard, self.pack.bundle):
            return Prepared(None, {"error": f"{self.pack.name} isn't allowed in this task."})
        return Prepared(None, state)   # it moves around and reads: nothing to approve

    def perform(self, ctx, prepared, act, snapshot):
        state = prepared.state or {}
        if state.get("error"):
            return PackResult(feedback=state["error"])
        run = getattr(ctx, "run", None) or self.run_context
        self._run_id = getattr(run, "run_id", None)
        gate = run_gate(self._run_id, getattr(run, "data_dir", None))
        version = self._installed(run)
        pinned = pins.aside(self.pack)
        if pinned:
            ctx.emit({"event": "tool_step", "op": self.op, "text": f"Left {self.pack.name} to the usual way"})
            return PackResult(feedback=f"{self.op} steps aside for {self.pack.name}: {pinned} Do it the usual way.")
        trusted, why = gate.trust("pack", self.pack.bundle, state.get("route") or state.get("what"), None, version)
        if not trusted:
            ctx.emit({"event": "tool_step", "op": self.op, "text": f"Left {self.pack.name} to the usual way"})
            return PackResult(
                feedback=f"{self.op} steps aside for {self.pack.name} here: it {why}. Do it the usual way.")
        guard = getattr(ctx, "guard", None)
        cancelled = getattr(run, "cancelled", None) or (lambda: False)
        io = core.PackIO(ctx.driver, self.pack, cancelled=cancelled, clock=self.clock, sleep=self.sleep,
                         allows_app=lambda bundle: guard is None or safe_allows(guard, bundle),
                         deadline=OPEN_SECONDS if self.kind == "OPEN" else READ_SECONDS)
        store = heal.attach([self.pack], getattr(run, "data_dir", None))
        if version:
            store.version_on_phone[self.pack.key] = version
        started = self.clock()
        ok, feedback, event, block, step = self._step(io, state)
        missed = event.get("unsure_screen")
        if missed and io.screens:
            # The app changed under the pack (heal.py): a confirmed variant that passes the replay check on this very
            # screen is trusted and the step runs once more; else this screen becomes a proposal, and the general
            # agent takes the step now.
            screen_now = io.screens[-1]
            if store.check(self.pack, screen_now, missed, version):
                ok, feedback, event, block, step = self._step(io, state)
                event["healed"] = True
            else:
                # The miss is drift: hints.HintListener records it from this step's app_pack event, with the screen
                # where the general agent takes over.
                store.propose(self.pack, screen_now, missed, version, self._run_id)
                event["fingerprint"] = heal.fingerprint(screen_now)
                event["confidence"] = self.pack.recognize(screen_now).confidence
        event.update(pack=self.pack.key, op=self.op, ok=ok, ms=round((self.clock() - started) * 1000),
                     taps=io.taps, version=self._installed(run), verified=getattr(self.pack.verified, "version", None))
        gate.note(ok, "pack", self.pack.bundle)
        ctx.emit({"event": "app_pack", **event})
        ctx.emit({"event": "tool_step", "op": self.op, "text": step})
        if block is not None:
            ctx.emit({"event": "result_blocks", "op": self.op, "blocks": [block]})
        health.record(getattr(run, "data_dir", None), {key: (_quiet(value) if key == "why" else value)
                                                        for key, value in event.items()})
        return PackResult(feedback=feedback, changed=bool(io.taps) or len(io.screens) > 1,
                          screens=tuple(io.screens[-SCREENS_BACK:]))

    def _step(self, io, state):
        return self._open(io, state) if self.kind == "OPEN" else self._read(io, state)

    def _open(self, io, state):
        pack, route, arg, act = self.pack, state["route"], state.get("arg"), state.get("act")
        # For an act this only reaches its place and names the control: the model taps it, so that tap goes through the
        # run's approvals, the journal and the duplicate guard.
        landing = core.navigate(pack, io, route, arg)
        event = {"step": act or route, "how": landing.how, "screen": landing.seen.screen,
                 "confidence": landing.seen.confidence, "steps": landing.steps}
        title = pack.routes[route].title
        if not landing.ok:
            event["why"] = landing.why
            if not landing.seen.sure and landing.seen.app in ("", pack.bundle) and landing.snapshot is not None:
                event["unsure_screen"] = pack.routes[route].lands[0]
            return (False, f"{self.op} couldn't open {title}: {landing.why}. Go on the usual way from this screen.",
                    event, None, f"Tried to open {title} in {pack.name}")
        said = f"Now on {landing.seen.title} in {pack.name}" + (" (it was already there)" if landing.how == "already"
                                                                  else f" ({landing.steps} steps, no model call)")
        if act:
            spec = pack.controls[act]
            control = landing.control or core.control_for(pack, act, landing.snapshot)
            if control is None:
                return (True, f"{said}. Its {spec.title} control isn't on the screen: look for it.", event, None,
                        f"Opened {title} in {pack.name}")
            said += (f". The {spec.title} control is “{core.plain(control.label)[:60]}” ({control.role}, "
                     f"{where(control)}). {self.op} doesn't tap it: tap it yourself only if the task asks for it, and "
                     "Mobster asks the person first.")
            return True, said, event, None, f"Went to {spec.title} in {pack.name}"
        return True, said + ".", event, None, f"Opened {title} in {pack.name}"

    def _read(self, io, state):
        pack, what, n, arg = self.pack, state["what"], state["n"], state.get("arg")
        event = {"step": what, "asked": n}
        try:
            result = pack.read(io, what, n, arg)
        except core.Lost as lost:
            event["why"] = str(lost)
            screen_now = io.screens[-1] if io.screens else None
            if screen_now is not None and str(getattr(screen_now, "bundle_id", "") or "") in ("", pack.bundle) \
                    and not pack.recognize(screen_now).sure:
                event["unsure_screen"] = pack.readers[what].screens[0]
            return (False, f"{self.op} couldn't read {pack.read_titles.get(what, what)}: {lost}. Read it the usual "
                           "way.", event, None, f"Tried to read {pack.read_titles.get(what, what)} in {pack.name}")
        event.update(screen=result.screen, rows=len(result.rows), how="code")
        if result.stopped:
            event["stopped"] = result.stopped
        if not result.rows:
            return (True, f"{self.op} found no {pack.read_titles.get(what, what)} on {pack.screen_title(result.screen)}"
                          + (f" ({result.stopped})" if result.stopped else "") + ".", event, None,
                    f"Read {pack.read_titles.get(what, what)} in {pack.name}: none")
        lines = "\n".join(row_line(i + 1, row, result.columns, pack.required.get(what, ()))
                          for i, row in enumerate(result.rows[:FEEDBACK_ROWS]))
        head = (f"{self.op} read {len(result.rows)} of {n} asked ({result.title}) by code"
                + (f"; it stopped because {result.stopped}, so what it read is all there is for now" if result.stopped
                   else "")
                + ". These rows were on the screen: answer, and fill the checklist, with the values they hold (the "
                  "names, words and numbers themselves). The person also sees them as a card in the result.")
        block = block_for(pack, result)
        context.keep(self._run_id, f"{pack.name} {result.title}", lines)
        noun = pack.read_titles.get(what, what)
        return (True, head + "\n" + harness_api.fence(f"{pack.name} {result.title}", lines), event, block,
                f"Read {len(result.rows)} {noun if len(result.rows) != 1 else pack.noun(what)} in {pack.name}"
                if what in pack.read_default
                else f"Read the {noun} in {pack.name}")

    def _installed(self, run):
        if not self._version:
            self._version = (core.app_version(getattr(run, "runtime", None), self.pack.bundle),)
        return self._version[0]


def factory(pack, kind):
    """register_tool's factory for one of ``pack``'s tools: offered to a Smart run that starts in the pack's app or
    whose task names it."""
    def make(ctx):
        return PackTool(pack, kind, ctx) if pack.offered_to(ctx) else None
    make.__name__ = f"{pack.key}_{kind.lower()}_factory"
    return make
