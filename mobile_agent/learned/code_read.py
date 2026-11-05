"""A taught question answered by reading the screen it ends on, in code, with no model turn.

A lesson that navigates to a Settings page and is asked as a question ("What iOS version is this iPhone on?") ends on a
screen whose rows already hold the answer. The words of the request are matched against the rows' names: a row whose
every word is in the request answers it, and exactly one row must (the longest name wins only when it is unique). No
row, or two equal matches, and the model finishes as before. Only Settings' page reader is covered; other apps keep
their one model turn. It reads what is on the screen and never acts.
"""

import re

SETTINGS_BUNDLE = "com.apple.Preferences"


def _words(text):
    return set(re.findall(r"\w+", str(text or "").casefold()))


def answer(snapshot, request):
    """"<name>: <value>" for the one row of the Settings page in front the request names, else None."""
    if getattr(snapshot, "bundle_id", None) != SETTINGS_BUNDLE:
        return None
    from ..packs import settings
    try:
        rows = settings.page_rows(snapshot)
    except Exception:  # noqa: BLE001 -- an unreadable screen is the model's
        return None
    asked = _words(request)
    if "and" in asked or "or" in asked:
        return None                      # two questions: one row would answer half of them
    hits = [row for row in rows if row.get("value") and _words(row["setting"]) and _words(row["setting"]) <= asked]
    if not hits:
        return None
    best = max(len(_words(row["setting"])) for row in hits)
    top = [row for row in hits if len(_words(row["setting"])) == best]
    if len({(row["setting"], row["value"]) for row in top}) != 1:
        return None
    return f"{top[0]['setting']}: {top[0]['value']}"
