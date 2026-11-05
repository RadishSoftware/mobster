"""Two X-ray maps of one app compared: the screens added, removed and changed, and the paywalls' prices."""

from .screens import jaccard, token

MATCH = .5   # the least overlap of words for two screens with one title to be the same screen


def _path(screen):
    return tuple(step.get("key") or token(step.get("label", "")) for step in screen.get("path") or ())


def _name(screen):
    return screen.get("name") or screen.get("title") or screen.get("id") or "Untitled"


def _where(screen):
    labels = [step.get("label", "") for step in screen.get("path") or ()]
    return " › ".join(labels) if labels else "the first screen"


def match(before, after):
    """{after id: before id} for the screens that are the same in both maps."""
    pairs, used = {}, set()
    by_path = {}
    for screen in before:
        by_path.setdefault(_path(screen), screen)
    for screen in after:
        old = by_path.get(_path(screen))
        if old is not None and old["id"] not in used and (
                token(_name(old)) == token(_name(screen))
                or jaccard(old.get("tokens", ()), screen.get("tokens", ())) >= MATCH):
            pairs[screen["id"]] = old["id"]
            used.add(old["id"])
    for screen in after:
        if screen["id"] in pairs:
            continue
        best, score = None, MATCH
        for old in before:
            if old["id"] in used or token(old.get("title", "")) != token(screen.get("title", "")):
                continue
            overlap = jaccard(old.get("tokens", ()), screen.get("tokens", ()))
            if overlap >= score:
                best, score = old, overlap
        if best is not None:
            pairs[screen["id"]] = best["id"]
            used.add(best["id"])
    return pairs


def _plan_key(plan):
    return plan.get("period") or (plan.get("name") or "").casefold() or plan.get("line", "").casefold()


def compare_paywalls(old, new):
    """What changed between two paywalls: [{"kind", "plan", "before", "after"}]."""
    changes = []
    old_plans = {_plan_key(p): p for p in (old or {}).get("plans") or ()}
    new_plans = {_plan_key(p): p for p in (new or {}).get("plans") or ()}
    for key, plan in new_plans.items():
        was = old_plans.get(key)
        if was is None:
            changes.append({"kind": "plan added", "plan": key, "before": None, "after": plan.get("price")})
            continue
        if (was.get("amount"), was.get("currency")) != (plan.get("amount"), plan.get("currency")):
            changes.append({"kind": "price", "plan": key, "before": was.get("price"), "after": plan.get("price")})
        if (was.get("trial") or "") != (plan.get("trial") or ""):
            changes.append({"kind": "trial", "plan": key, "before": was.get("trial") or None,
                            "after": plan.get("trial") or None})
    for key, plan in old_plans.items():
        if key not in new_plans:
            changes.append({"kind": "plan removed", "plan": key, "before": plan.get("price"), "after": None})
    if (old or {}).get("trial", "") != (new or {}).get("trial", ""):
        changes.append({"kind": "trial", "plan": "", "before": (old or {}).get("trial") or None,
                        "after": (new or {}).get("trial") or None})
    return changes


def compare(before, after):
    old_screens = before.get("screens") or []
    new_screens = after.get("screens") or []
    pairs = match(old_screens, new_screens)
    old_by_id = {s["id"]: s for s in old_screens}
    new_by_id = {s["id"]: s for s in new_screens}
    matched_old = set(pairs.values())
    added = [s for s in new_screens if s["id"] not in pairs]
    removed = [s for s in old_screens if s["id"] not in matched_old]
    changed, paywalls = [], []
    for new_id, old_id in pairs.items():
        new, old = new_by_id[new_id], old_by_id[old_id]
        gained = sorted(set(new.get("tokens", ())) - set(old.get("tokens", ())))
        lost = sorted(set(old.get("tokens", ())) - set(new.get("tokens", ())))
        if gained or lost:
            changed.append({"before": old_id, "after": new_id, "name": _name(new), "where": _where(new),
                            "added_words": gained[:20], "removed_words": lost[:20]})
        if old.get("paywall") or new.get("paywall"):
            moves = compare_paywalls(old.get("paywall"), new.get("paywall"))
            if moves or bool(old.get("paywall")) != bool(new.get("paywall")):
                paywalls.append({"screen": new_id, "name": _name(new), "where": _where(new),
                                 "was_paywall": bool(old.get("paywall")), "is_paywall": bool(new.get("paywall")),
                                 "changes": moves})
    for screen in added:
        if screen.get("paywall"):
            paywalls.append({"screen": screen["id"], "name": _name(screen), "where": _where(screen),
                             "was_paywall": False, "is_paywall": True, "new_screen": True,
                             "changes": compare_paywalls(None, screen["paywall"])})
    for screen in removed:
        if screen.get("paywall"):
            paywalls.append({"screen": screen["id"], "name": _name(screen), "where": _where(screen),
                             "was_paywall": True, "is_paywall": False, "gone": True, "changes": []})
    under = {}
    for screen in added:
        parent = new_by_id.get(screen.get("parent")) if screen.get("parent") else None
        if parent is not None and parent["id"] in pairs:
            under.setdefault(_name(parent), []).append(_name(screen))
    return {"schema": "mobster.xray.diff/1", "app": (after.get("app") or before.get("app") or {}).get("bundle"),
            "before": {"created_at": before.get("created_at"), "screens": len(old_screens)},
            "after": {"created_at": after.get("created_at"), "screens": len(new_screens)},
            "added": [{"id": s["id"], "name": _name(s), "where": _where(s)} for s in added],
            "removed": [{"id": s["id"], "name": _name(s), "where": _where(s)} for s in removed],
            "changed": changed, "paywalls": paywalls, "added_under": under}


PERIOD_WORDS = {"week": "Weekly", "month": "Monthly", "quarter": "Quarterly", "half-year": "Six-month",
                "year": "Yearly",
                "lifetime": "Lifetime"}


def summary(result):
    """The comparison in plain sentences, most important first."""
    lines = []
    for item in result.get("paywalls") or ():
        if item.get("new_screen"):
            prices = ", ".join(c["after"] for c in item["changes"] if c.get("after"))
            lines.append(f"New paywall screen: {item['name']} ({item['where']})" + (f", {prices}" if prices else ""))
        elif item.get("gone"):
            lines.append(f"Paywall gone: {item['name']} ({item['where']})")
        elif item["is_paywall"] and not item["was_paywall"]:
            lines.append(f"{item['name']} is a paywall now ({item['where']})")
        for change in item.get("changes") or ():
            if item.get("new_screen"):
                continue
            plan = PERIOD_WORDS.get(change["plan"], change["plan"].title() or "The")
            if change["kind"] == "price":
                lines.append(f"{plan} price went {change['before']} → {change['after']} on {item['name']}")
            elif change["kind"] == "plan added":
                lines.append(f"New {plan.lower()} plan at {change['after']} on {item['name']}")
            elif change["kind"] == "plan removed":
                lines.append(f"{plan} plan ({change['before']}) dropped from {item['name']}")
            elif change["kind"] == "trial":
                lines.append(f"Trial went {change['before'] or 'none'} → {change['after'] or 'none'} on {item['name']}")
    for parent, names in (result.get("added_under") or {}).items():
        count = len(names)
        lines.append(f"{count} new screen{'s' if count != 1 else ''} under {parent}: {', '.join(names[:5])}")
    listed = {n for names in (result.get("added_under") or {}).values() for n in names}
    for item in result.get("added") or ():
        if item["name"] not in listed:
            lines.append(f"New screen: {item['name']} ({item['where']})")
    for item in result.get("removed") or ():
        lines.append(f"Gone: {item['name']} ({item['where']})")
    for item in result.get("changed") or ():
        bits = []
        if item["added_words"]:
            bits.append("now shows " + ", ".join(f"“{w}”" for w in item["added_words"][:4]))
        if item["removed_words"]:
            bits.append("no longer shows " + ", ".join(f"“{w}”" for w in item["removed_words"][:4]))
        lines.append(f"{item['name']} changed: {'; '.join(bits)}")
    if not lines:
        lines.append("No changes: the same screens, words and prices.")
    before, after = result.get("before") or {}, result.get("after") or {}
    lines.append(f"Screens: {before.get('screens', 0)} before, {after.get('screens', 0)} after.")
    return lines
