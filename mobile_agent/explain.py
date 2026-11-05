"""The one sentence before an outward step: what Mobster will do, and why it asks. The approval card, the terminal and
the MCP status all show this sentence, built here from the request the approval carries."""

from .narrate import operation_words

# What a tap on a control does, by operation, when the card names no act.
_SKIP = frozenset({"PLAN", "PROPOSE"})


def _lower_first(text):
    return text[:1].lower() + text[1:] if text and not text[:2].isupper() else text


def explain(request):
    """One sentence for an approval ``request`` of a step, or None for a question, a plan or a draft review."""
    if not isinstance(request, dict) or (request.get("kind") or "commit") not in ("commit", "action"):
        return None
    operation = str(request.get("operation") or "").upper()
    if operation in _SKIP:
        return None
    if request.get("act") == "use app":
        names = request.get("apps") or [request.get("app_name") or request.get("label") or "an app"]
        opened = ", ".join(map(str, names))
        return (f"Mobster will open {opened} and read and move around in it, "
                "and asks because you haven't allowed it yet.")
    label = " ".join(str(request.get("label") or "").split())[:60]
    app = " ".join(str(request.get("app_name") or request.get("app") or "").split())[:60]
    verb = operation_words(operation)[0].lower() if operation else "act on"
    what = f"{verb} “{label}”" if label else verb
    where = f" in {app}" if app and app.casefold() not in what.casefold() else ""
    who = f" to {request['recipient']}" if request.get("recipient") else ""
    why = " ".join(str(request.get("reason") or "").split()).rstrip(".")
    why = _lower_first(why) if why else "it has an effect outside your iPhone"
    return f"Mobster will {what}{where}{who}, and asks because {why}."[:300]
