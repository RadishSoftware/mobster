"""A paywall and its plans, read from a screen's text. The crawler taps nothing on one, and nothing here acts on it."""

import re
from dataclasses import asdict, dataclass, field

SYMBOLS = {"$": "USD", "US$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "₹": "INR", "₩": "KRW", "A$": "AUD",
           "C$": "CAD", "CA$": "CAD", "AU$": "AUD", "CHF": "CHF", "R$": "BRL", "kr": "SEK", "zł": "PLN", "₺": "TRY"}
CODES = ("USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "INR", "BRL", "SEK", "NOK", "DKK", "PLN", "KRW", "TRY")
_SYMBOL = "|".join(re.escape(s) for s in sorted(SYMBOLS, key=len, reverse=True))
_AMOUNT = r"\d{1,3}(?:[,.  ]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
PRICE = re.compile(
    rf"(?P<pre>{_SYMBOL}|\b(?:{'|'.join(CODES)})\b)\s?(?P<a1>{_AMOUNT})"
    rf"|(?P<a2>{_AMOUNT})\s?(?P<post>{_SYMBOL}|\b(?:{'|'.join(CODES)})\b)")
# A billing period, after a price or on its own ("/mo", "per week", "a year", "Annual", "Monthly").
PERIODS = (
    ("week", r"/\s?w(?:ee)?k\b|\bper week\b|\ba week\b|\bweekly\b|\bevery week\b|\b1 week\b(?! free)"),
    ("month", r"/\s?mo(?:nth)?\b|\bper month\b|\ba month\b|\bmonthly\b|\bevery month\b|\bmo\.?\b|\b1 month\b"),
    ("quarter", r"\bper quarter\b|\bquarterly\b|\b3 months\b|/\s?3\s?mo\b"),
    ("half-year", r"\b6 months\b|/\s?6\s?mo\b|\bhalf[- ]year(?:ly)?\b"),
    ("year", r"/\s?y(?:ea)?r\b|\bper year\b|\ba year\b|\byearly\b|\bannual(?:ly)?\b|\bevery year\b|\b12 months\b"
             r"|\b1 year\b"),
    ("lifetime", r"\blifetime\b|\bone[- ]time\b|\bpay once\b|\bforever\b"),
)
_PERIODS = [(name, re.compile(pattern, re.I)) for name, pattern in PERIODS]
TRIAL = re.compile(r"\b(?P<n>\d{1,2}|one|two|three|seven|fourteen|thirty)[- ](?P<unit>day|week|month)s?\b"
                   r"(?:[- ]long)?\s+(?:free\s+)?trial\b|"
                   r"\bfree (?:for )?(?P<n2>\d{1,2})[- ](?P<unit2>day|week|month)s?\b"
                   r"|\b(?P<n3>\d{1,2})[- ](?P<unit3>day|week|month)s? free\b|\bfree trial\b|\btry (?:it )?free\b",
                   re.I)
WORDS_N = {"one": 1, "two": 2, "three": 3, "seven": 7, "fourteen": 14, "thirty": 30}
SELLS = re.compile(r"\b(subscribe|subscription|restore purchases?|free trial|start (?:my |your )?(?:free )?trial|"
                   r"try (?:it )?free|premium|pro\b|plus\b|unlock|upgrade|unlimited|cancel any ?time|"
                   r"billed|auto[- ]renew|renews?)\b", re.I)
RESTORE = re.compile(r"\brestore purchases?\b|\brestore\b", re.I)
PLAN_NAME = re.compile(r"\b(weekly|monthly|quarterly|yearly|annual|annually|lifetime|family|individual|student|"
                       r"basic|standard|premium|pro|plus|ultimate|starter)\b", re.I)
NEAR = 2
MONTHS = {"week": 12 / 52, "month": 1, "quarter": 3, "half-year": 6, "year": 12}


@dataclass
class Plan:
    price: str                 # as shown: "$59.99"
    amount: float              # 59.99
    currency: str              # "USD"
    period: str = ""           # week, month, quarter, half-year, year, lifetime, or "" when the screen doesn't say
    trial: str = ""            # "7 days", "1 week", "free trial"
    name: str = ""             # "Annual", "Premium"
    per_month: float = 0.0     # the price spread over a month (0 for lifetime or an unknown period)
    line: str = ""             # the line the price was read from

    def key(self):
        """What a plan is across two crawls: its period, else its name, else its place."""
        return self.period or self.name.casefold() or self.line.casefold()


@dataclass
class Paywall:
    plans: list = field(default_factory=list)
    trial: str = ""            # the screen's trial, when it names one apart from a plan
    restore: bool = False      # a Restore Purchases control is shown
    signals: list = field(default_factory=list)  # why it reads as a paywall, for the report
    headline: str = ""         # the screen's first long line ("Unlock everything with Premium")

    def to_json(self):
        return {**asdict(self), "plans": [asdict(plan) for plan in self.plans]}


def _amount(text):
    """'1,299.99' -> 1299.99; '9,99' -> 9.99; '1 299' -> 1299."""
    text = text.replace("\u00a0", "").replace(" ", "")
    cents = re.search(r"[.,](\d{1,2})$", text)
    whole = re.sub(r"[.,]", "", text[:cents.start()] if cents else text)
    try:
        return float(whole + ("." + cents.group(1) if cents else ""))
    except ValueError:
        return 0.0


def prices(line):
    """Every price in ``line``: [(shown, amount, currency, start, end)]."""
    found = []
    for match in PRICE.finditer(line):
        symbol = match.group("pre") or match.group("post") or ""
        amount = _amount(match.group("a1") or match.group("a2") or "")
        if amount <= 0:
            continue
        currency = SYMBOLS.get(symbol, symbol.upper())
        found.append((match.group(0).strip(), amount, currency, match.start(), match.end()))
    return found


def period(text):
    for name, pattern in _PERIODS:
        if pattern.search(text):
            return name
    return ""


def trial(text):
    match = TRIAL.search(text)
    if not match:
        return ""
    n = match.group("n") or match.group("n2") or match.group("n3")
    unit = match.group("unit") or match.group("unit2") or match.group("unit3")
    if n and unit:
        count = WORDS_N.get(n.casefold(), None) or int(n)
        return f"{count} {unit}{'s' if count != 1 else ''}"
    return "free trial"


def _clean(lines):
    out = []
    for line in lines or ():
        text = " ".join(str(line or "").split())
        if text:
            out.append(text[:300])
    return out


def detect(lines):
    """The screen's paywall, or None. ``lines``: its text in reading order."""
    lines = _clean(lines)
    if not lines:
        return None
    every = "\n".join(lines)
    priced = [(index, found) for index, line in enumerate(lines) for found in prices(line)]
    signals = []
    if priced:
        signals.append("price")
    periods = [name for name, pattern in _PERIODS if pattern.search(every)]
    if periods and priced:
        signals.append("billing period")
    screen_trial = trial(every)
    if screen_trial:
        signals.append("free trial")
    restore = bool(RESTORE.search(every)) and bool(re.search(r"\brestore purchases?\b", every, re.I)
                                                    or SELLS.search(every))
    if restore:
        signals.append("restore purchases")
    sells = sorted({m.group(1).casefold() for m in SELLS.finditer(every)})
    if sells:
        signals.append("sells: " + ", ".join(sells[:4]))
    is_paywall = bool(priced and (periods or screen_trial or restore or sells)) or bool(
        restore and (screen_trial or sells and any(s in sells for s in ("subscribe", "subscription"))))
    if not is_paywall:
        return None
    plans, seen = [], {}
    for index, (shown, amount, currency, start, end) in priced:
        line = lines[index]
        own = line[end:end + 24]  # "$9.99/month": the period right after the price is its own
        near = lines[max(0, index - NEAR):index + NEAR + 1]
        plan_period = period(own) or period(line) or _nearest(lines, index, period)
        plan_trial = trial(line) or _nearest(lines, index, trial, reach=1)
        name_match = PLAN_NAME.search(line) or next((PLAN_NAME.search(text) for text in reversed(near[:NEAR])
                                                     if PLAN_NAME.search(text)), None)
        name = name_match.group(1).title() if name_match else ""
        if not plan_period and name.casefold() in ("weekly", "monthly", "quarterly", "yearly", "annual", "annually",
                                                    "lifetime"):
            plan_period = period(name)
        # A per-month equivalent under an annual price ("$4.99/mo, billed yearly") is the same plan.
        if re.search(r"\bbilled (?:annually|yearly)\b|\bequivalent\b|\bjust\b.*\bper\b", line, re.I) and plans:
            continue
        key = (shown, plan_period)
        if key in seen:
            # The same price again ("7-day free trial, then $59.99/year"): one plan.
            known = seen[key]
            known.trial = known.trial or plan_trial
            known.name = known.name or name
            continue
        per_month = round(amount / MONTHS[plan_period], 2) if plan_period in MONTHS else 0.0
        plans.append(Plan(price=shown, amount=amount, currency=currency, period=plan_period, trial=plan_trial,
                          name=name, per_month=per_month, line=line))
        seen[key] = plans[-1]
    headline = next((line for line in lines if len(line.split()) >= 3 and not prices(line)), "")
    return Paywall(plans=plans, trial=screen_trial, restore=restore, signals=signals, headline=headline[:120])


def _nearest(lines, index, read, reach=NEAR):
    for distance in range(1, reach + 1):
        for other in (index + distance, index - distance):
            if 0 <= other < len(lines):
                if prices(lines[other]) and distance > 0:
                    continue
                value = read(lines[other])
                if value:
                    return value
    return ""
