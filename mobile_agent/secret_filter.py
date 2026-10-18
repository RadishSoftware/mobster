"""The one secret filter (seam S2.1). Frozen: a change may only widen what it catches.

Memory's facts and proposals, conversations' stored notes, harness's checkpoints and anything a listener keeps use
it; nobody builds their own.

- ``is_secret(text)``: True when the text holds, or talks about, something that must never be stored: a password,
  passcode or PIN (the word, or "code is 4821"), a 6–8-digit one-time code, a Luhn-valid card number, a US Social
  Security number's shape, an IBAN, an API key's shape (sk-, sk_/rk_ live and test, ghp_,
  github_pat_, xox?-, AKIA/ASIA, AIza, a JWT, a Google ya29. or 1//0 token), a Bearer token, the value in
  ``NAME_API_KEY=``, ``NAME_TOKEN=`` and ``NAME_SECRET=`` lines, an email
  with a password beside it, or agent_hooks.MASK itself (something already masked).
- ``redact(text)``: each of those values replaced by agent_hooks.MASK; the word "password" stays, its value goes.
  Idempotent: ``redact(redact(t)) == redact(t)``.
"""

import re

from .agent_hooks import MASK

_MASK = re.escape(MASK)
# Words that name a secret. PIN counts in capitals, or as "pin code", "pin number", "pin is", "pin:".
_WORDS = (r"(?:(?i:\b(?:password|passcode|passwd|pass\s?word|pin\s?code|pin\s?number|one[- ]time\s+(?:pass)?code|"
          r"verification\s+code|security\s+code|cvv|cvc|pin(?=\s*(?:is\b|was\b|:|=))))|\bPIN\b)")
# A quoted passphrase is one value, including escaped quotes and punctuation attached to its closing quote.
_VALUE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')[^\s,;]*|[^\s,;]+'''
# "password: hunter2", "password for the gym is hunter2" (a value after is/was/:/=, up to four words later), or a
# value straight after the word that looks like one (has a digit or a symbol): "my password hunter2!".
KEYWORD_VALUE = re.compile(_WORDS + r"(?:(?:\s+[\w'’]+){0,4}?\s*(?:\bis\b|\bwas\b|:|=)\s*(" + _VALUE + r")"
                           r"|\s+((?=[^\s,;]*(?:[\d_]|[^\w\s]))(?:" + _VALUE + r")))")
KEYWORD = re.compile(_WORDS)
# "the code is 551203", "code: 4821", "OTP 551 203": a code word with a value that has a digit.
CODE_VALUE = re.compile(r"(?i:\b(?:code|otp)\b)(?:\s*(?:is|was|:|=)\s*|\s+)(" + _MASK
                        + r"|(?=[^\s,;]*\d)(?:" + _VALUE + r")(?:\s\d{3,4}\b)?)")
OTP = re.compile(r"(?<![\w.,/-])\d{6,8}(?![\w.,/]|-\d)")
CARD = re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")
SSN = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")
API_KEY = re.compile(r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{16,}|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|"
                     r"gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|"
                     r"(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|sb_secret_[A-Za-z0-9_-]{16,}|"
                     r"whsec_[A-Za-z0-9+/=_-]{16,}|re_[A-Za-z0-9_]{16,}|mob_(?:at|rt|rk|dk)_[A-Za-z0-9_-]{16,}|"
                     r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*|ya29\.[A-Za-z0-9_-]{20,})|"
                     r"(?<![\w/])1//0[A-Za-z0-9_-]{20,}")
# "Authorization: Bearer abc123...": the token goes, the word stays. A token has a digit or mixed case, so
# "Bearer authentication" in a sentence stays.
BEARER = re.compile(r"\b[Bb]earer\s+((?=[A-Za-z0-9._~+/=-]*(?:\d|[a-z][A-Za-z0-9._~+/=-]*[A-Z]))[A-Za-z0-9._~+/=-]{12,})")
# "OPENAI_API_KEY=value", "GH_TOKEN='value'", "APP_SECRET=value": the name stays, the value goes.
ENV_VALUE = re.compile(r"\b(?:[A-Z][A-Z0-9]*_)*(?:API_KEY|TOKEN|SECRET)\s*=\s*[\"']?([^\s\"',;]+)")
EMAIL_PASSWORD = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?:\s*[:/|]\s*|\s+(?:and|with|pw|pwd)\s+)("
                            + _VALUE + r")", re.I)


def _luhn(digits):
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _cards(text):
    """Spans of Luhn-valid 13–19-digit card numbers (spaces or dashes allowed between digits)."""
    spans = []
    for match in CARD.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            spans.append(match.span())
    return spans


def _value_spans(text):
    spans = []
    for pattern in (API_KEY, SSN, IBAN, OTP):
        spans += [m.span() for m in pattern.finditer(text)]
    spans += _cards(text)
    for pattern in (KEYWORD_VALUE, CODE_VALUE, EMAIL_PASSWORD, BEARER, ENV_VALUE):
        for match in pattern.finditer(text):
            group = next((g for g in range(1, (pattern.groups or 0) + 1) if match.group(g) is not None), None)
            if group is not None:
                spans.append(match.span(group))
    return spans


def is_secret(text):
    """Whether ``text`` holds or names a secret (see the module docstring). Non-strings are never secret."""
    if not isinstance(text, str) or not text:
        return False
    return MASK in text or bool(KEYWORD.search(text)) or bool(_value_spans(text))


def redact(text):
    """``text`` with every secret value replaced by MASK. Idempotent; non-strings come back unchanged."""
    if not isinstance(text, str) or not text:
        return text
    spans = sorted(_value_spans(text))
    if not spans:
        return text
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out = text
    for start, end in reversed(merged):
        out = out[:start] + MASK + out[end:]
    return out


def redact_deep(value, depth=0):
    """``value`` (JSON-shaped) with ``redact`` applied to every string in it, keys kept. Long unbroken blobs
    (an inlined picture) pass through, and so does everything past 8 levels."""
    if isinstance(value, str):
        return value if len(value) > 4096 and " " not in value[:4096] else redact(value)
    if depth > 8:
        return value
    if isinstance(value, dict):
        return {key: redact_deep(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_deep(item, depth + 1) for item in value]
    return value
