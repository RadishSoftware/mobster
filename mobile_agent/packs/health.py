"""Pack health: which app version a pack was checked on, how its steps go on this Mac, and when it steps aside."""

RECENT = 200


def bundle_of(pack_key):
    """The bundle id of the pack named ``pack_key`` (the ledger's ``app``), or the key when no pack has it."""
    try:
        from .register import all_packs
        return next((pack.bundle for pack in all_packs() if pack.key == pack_key), pack_key)
    except Exception:  # noqa: BLE001
        return pack_key


def record(data_dir, entry):
    """Append one pack step (``entry``, no screen text) to the hint ledger."""
    from .. import hints
    hints.record(data_dir, {**entry, "kind": "use", "source": "pack", "app": bundle_of(entry.get("pack")),
                            "key": entry.get("step"), "ok": bool(entry.get("ok"))})


def recent(data_dir, pack_key=None, limit=RECENT):
    """The newest ``limit`` pack steps (of one pack), oldest first."""
    from .. import hints
    lines = [e for e in hints.recent(data_dir, source="pack", limit=10 ** 6)
             if e.get("kind", "use") == "use" and (pack_key is None or e.get("pack") == pack_key)]
    return lines[-limit:]


def status(pack, installed=None, entries=()):
    """One pack's health: what it was checked on, what the phone has, and how its recent steps went."""
    verified = pack.verified
    steps = [e for e in entries if e.get("pack") == pack.key]
    landed = sum(1 for e in steps if e.get("ok"))
    aside = [e for e in steps if not e.get("ok")]
    state = "unknown"
    if steps:
        state = "healthy" if not aside or landed / len(steps) >= .8 else "degraded"
    changed = changed_screens(steps)
    version = installed or (verified.version if verified else None)
    if not steps:
        line = f"{pack.name} pack: not used on this Mac yet"
    elif changed:
        line = (f"{pack.name} pack: working on {version}; {len(changed)} screen{'s' if len(changed) != 1 else ''} "
                "changed")
    else:
        line = f"{pack.name} pack: working on {version}"
    learned = (getattr(pack, "learned", None).summary(pack.key) if getattr(pack, "learned", None) is not None else {})
    return {"pack": pack.key, "name": pack.name, "bundleId": pack.bundle, "summary": line,
            "changedScreens": [{"screen": name, "title": pack.screen_title(name), "since": at, "version": v}
                               for name, (at, v) in changed.items()],
            "learned": learned,
            "verified": ({"version": verified.version, "date": verified.date, "device": verified.device or None}
                         if verified else None),
            "pin": _pin(pack),
            "installed": installed, "sameVersion": (installed == verified.version) if installed and verified else None,
            "screens": [{"name": s.name, "title": s.title, "evidence": s.evidence} for s in pack.screens],
            "steps": len(steps), "landed": landed, "steppedAside": len(aside), "state": state,
            "lastProblem": ({"step": aside[-1].get("step"), "why": aside[-1].get("why"), "at": aside[-1].get("at"),
                             "version": aside[-1].get("version")} if aside else None)}


def _pin(pack):
    """What the team pinned this pack to ("off" or an app version), and whether it steps aside; None with no pin."""
    from . import pins
    to = pins.current().get(pack.key)
    return None if to is None else {"to": to, "aside": pins.aside(pack) is not None}


def changed_screens(steps):
    """{screen: (since, version)} for each screen a pack last failed to recognize (``unsure_screen``) with no
    recognition of it since, in log order: the screens the app changed under the pack."""
    missed = {}
    for entry in steps:
        if entry.get("unsure_screen"):
            missed[entry["unsure_screen"]] = (entry.get("at") or 0, entry.get("version"))
        elif entry.get("ok") and entry.get("screen"):
            missed.pop(entry["screen"], None)
    return missed


def tab_routes(pack):
    """The routes a health run visits: each reaches a screen from the tab bar by taps alone (no argument, no link
    needed,
    no act)."""
    acts = {spec.route for spec in pack.controls.values()}
    return [route for route in pack.routes.values()
            if route.from_tabs and not route.arg and route.name not in acts and route.steps
            and all(type(step).__name__ == "Tap" for step in route.steps)]


def check(pack, io):
    """Visit each tab route and say whether the pack named the screen it reached: [{route, screen, landed, confidence,
    evidence, why}]."""
    from . import core
    rows = []
    for route in tab_routes(pack):
        landing = core.navigate(pack, io, route.name)
        rows.append({"route": route.name, "title": route.title, "screen": landing.seen.screen, "landed": landing.ok,
                     "confidence": landing.seen.confidence, "evidence": landing.seen.evidence or None,
                     "why": landing.why or None})
        if io.cancelled():
            break
    return rows


def check_result(pack, rows, version, seconds):
    """A health run's result, as a task's: the answer line and a checklist card."""
    from .. import result_blocks as rb
    landed = sum(1 for row in rows if row["landed"])
    items = [{"id": f"s{i + 1}", "label": row["title"][:1].upper() + row["title"][1:],
              "status": "pass" if row["landed"] else "fail",
              "expected": f"{pack.name} shows {row['title']}",
              "observed": (f"recognized ({row['confidence']:.2f})" if row["landed"] else (row["why"] or "not named"))}
             for i, row in enumerate(rows)]
    changed = len(rows) - landed
    answer = (f"{pack.name} pack: working on {version or 'this version'}"
              + (f"; {changed} screen{'s' if changed != 1 else ''} changed." if changed else "."))
    return {"event": "result", "status": "completed", "engine": "smart", "outcome": "done" if not changed else "check",
            "answer": answer, "actions": len(rows), "decisions": 0, "elapsed_ms": round(seconds * 1000),
            "costUsd": 0.0, "model": None,
            "blocks": rb.envelope([rb.checklist(items, title=f"{pack.name} pack health", id=f"{pack.key}-health")])}
