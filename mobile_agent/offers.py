"""What a buy approval says it buys, read off the screen and never guessed (DR-55): what the screen doesn't show is said
("Price not shown"), and screen text fills the card's words and instructs nothing."""

import re

MONEY_ACTS = frozenset({"buy", "pay", "subscribe"})
PRICE = re.compile(r"(?:[$€£¥]\s?\d[\d,]*(?:\.\d{1,2})?|\d[\d,]*(?:\.\d{1,2})?\s?(?:USD|EUR|GBP|US\$|CAD|AUD)\b)")
PERIOD = re.compile(r"(?:/|\bper\s+|\ba\s+|\bevery\s+|\bfor\s+(?:a\s+|1\s+)?)(year|yr|annual|month|mo|week|wk|day)\b"
                    r"|\b(yearly|annually|monthly|weekly|daily|annual|lifetime|one[- ]time)\b", re.I)
TRIAL = re.compile(r"\b(\d{1,3}[- ](?:day|week|month)s?[- ]free[- ]trial|"
                   r"free[- ]trial\s+for\s+\d{1,3}\s+(?:day|week|month)s?"
                   r"|\d{1,3}\s+(?:day|week|month)s?\s+free)\b", re.I)
PERIOD_WORD = {"year": "year", "yr": "year", "annual": "year", "yearly": "year", "annually": "year",
               "month": "month", "mo": "month", "monthly": "month", "week": "week", "wk": "week", "weekly": "week",
               "day": "day", "daily": "day", "lifetime": "once", "one-time": "once", "one time": "once",
               "onetime": "once"}
LINE_CHARS = 100


def _lines(snapshot):
    for element in getattr(snapshot, "elements", None) or ():
        for text in (element.label, element.value):
            text = " ".join(str(text or "").split())
            if text:
                yield text, bool(getattr(element, "selected", False))


def _offer(text):
    """{"price", "period", "text"} for a line that shows a price, else None."""
    price = PRICE.search(text)
    if price is None:
        return None
    period = PERIOD.search(text)
    word = None
    if period:
        word = PERIOD_WORD.get((period.group(1) or period.group(2)).lower().replace("-", " ").replace(" ", "-"),
                               PERIOD_WORD.get((period.group(1) or period.group(2)).lower()))
    return {"price": re.sub(r"\s+", "", price.group(0)), "period": word, "text": text[:LINE_CHARS]}


def _plan(offer):
    """The plan's name when the price's own line gives one ("Annual $39.99 per year" is "Annual"), else ""."""
    rest = PERIOD.sub(" ", PRICE.sub(" ", offer["text"]))
    rest = re.sub(
        r"\b(?:billed|after|then|only|just|for|per|a|an|the|trial|free|subscribe|buy|purchase|pay|start|continue|get|"
        r"try|unlock|upgrade|now|with|to|total|subtotal|price|amount|tax|due)\b", " ", rest, flags=re.I)
    rest = " ".join(re.sub(r"[^\w+ ]+", " ", rest).split())
    return rest if 2 <= len(rest) <= 40 and not re.search(r"\d", rest) else ""


def _said(offer):
    when = {"year": " a year", "month": " a month", "week": " a week", "day": " a day", "once": " once", None: ""}
    plan = _plan(offer)
    return (plan + ", " if plan else "") + offer["price"] + when[offer["period"]] \
        + ("" if offer["period"] else " (period not shown)")


def reason(snapshot, control_label="", gate_reason=None):
    """The approval's ``reason`` for a money act: the gate's own line, then the offer as the screen shows it."""
    lines = list(_lines(snapshot))
    found, trial = [], None
    for text, selected in [(str(control_label or ""), False), *lines]:
        if trial is None and (match := TRIAL.search(text)):
            trial = match.group(1).lower()
        offer = _offer(text)
        if offer is not None:
            found.append((offer, selected))
    distinct = {}
    for offer, selected in found:
        key = (offer["price"], offer["period"])
        first = distinct[key][0] if key in distinct else offer
        distinct[key] = (first, selected or distinct.get(key, (None, False))[1])
    chosen = [offer for offer, selected in distinct.values() if selected]
    if len(distinct) == 1:
        said = _said(next(iter(distinct.values()))[0])
    elif len(chosen) == 1:
        said = _said(chosen[0])
    elif distinct:
        said = "Several prices on the screen: " + ", ".join(_said(o) for o, _ in distinct.values())[:200] \
               + ". Check which is selected"
    else:
        said = "Price not shown"
    if trial and (len(distinct) == 1 or len(chosen) == 1):
        said += f", after a {trial}" if "trial" in trial else f", after {trial}"
    base = f"{gate_reason.rstrip('. ')}. " if gate_reason else ""
    return f"{base}{said}."
