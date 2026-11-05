"""Today's rules as the permission gate reads them: a one-time code, and a change that stays on the phone. No I/O."""

import re
import unicodedata
from dataclasses import dataclass

TYPE_OPS = frozenset({"TYPE", "SET_TEXT"})
RETURN_OPS = frozenset({"SUBMIT", "TYPE_SUBMIT"})
TAP_OPS = frozenset({"TAP", "LONG_PRESS", "TAP_XY"})
ADJUST_OPS = frozenset({"INCREMENT", "DECREMENT"})
SWITCH_ROLES = frozenset({"Switch", "Toggle", "CheckBox"})


@dataclass(frozen=True)
class Action:
    """One action as the gate sees it. ``typed``: what already waits in the screen's composer (None: unknown)."""
    op: str
    label: str = ""
    role: str | None = None
    value: str | None = None
    app: str = ""
    app_name: str = ""
    text: str | None = None
    typed: str | None = None
    screen: str = ""
    context: str = ""
    goal: str = ""
    # The field's placeholder and accessibility identifier, where an app's textContentType ("oneTimeCode") shows.
    hint: str = ""
    # The rows the run saw that hold the typed digits; () when none, None when it can't say (code_entry then asks).
    origins: tuple | None = None
    # The control's frame and the screen's elements, for the consequence signals that read the screen around it.
    rect: tuple = ()
    around: tuple = ()
    # The screen was read from pixels only (iPhone Mirroring, a video and Bluetooth connection).
    pixels: bool = False
    # A named icon's glyph ("paper plane", "heart", "trash"), read beside its name: a glyph that commits still asks.
    glyph: str = ""


_PUNCT = re.compile(r"[\"“”'‘’«»!?¡¿…]+")
_TAIL = re.compile(r"\s*(?:[·•|,\-–—:]\s*)?(?:[$€£¥₩₽₹]\s?\d[\d.,]*|\d[\d.,]*\s?(?:[$€£¥₩₽₹]|usd|eur|円|元|원)|"
                   r"\(\d+\)|\d+)\s*$", re.I)


def norm(text):
    """``text`` folded for matching: NFKC, lower case,
    quotes and trailing prices or counts dropped, spaces collapsed."""
    text = unicodedata.normalize("NFKC", text or "").replace(" ", " ")
    text = " ".join(_PUNCT.sub(" ", text.casefold()).split())
    for _ in range(2):
        shorter = _TAIL.sub("", text).strip(" ·•|,-–—:")
        if not shorter:
            break
        text = shorter
    return text


def words_of(label):
    """The words of ``label``, folded by ``norm``."""
    return re.findall(r"[\w'’-]+", norm(label))


CODE_WORDS = ("verification code", "one-time code", "one time code", "one-time passcode", "one-time password", "otp",
              "2fa", "2fa code", "two-factor code", "authentication code", "authenticator code", "login code",
              "sign-in code", "security code sent", "sms code", "text code", "enter code", "enter the code",
              "6-digit code", "enter the 6-digit code", "confirmation code", "código de verificación",
              "código de verificação", "código de confirmación", "código de confirmação", "code de vérification",
              "code de confirmation", "code à usage unique", "bestätigungscode", "verifizierungscode",
              "sicherheitscode", "einmalcode", "codice di verifica", "codice di conferma", "verificatiecode",
              "bevestigingscode", "verifieringskod", "bekräftelsekod", "kod weryfikacyjny", "kod sms",
              "doğrulama kodu", "onay kodu", "код подтверждения", "код из смс", "код из sms", "одноразовый код",
              "код підтвердження", "認証コード", "確認コード", "ワンタイムパスワード", "验证码", "驗證碼", "校验码",
              "인증번호", "인증 번호", "인증 코드", "mã xác minh", "mã xác nhận", "mã otp", "kode verifikasi",
              "kode otp",
              "รหัสยืนยัน", "รหัส otp", "קוד אימות", "رمز التحقق", "رمز التأكيد", "सत्यापन कोड", "ओटीपी")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿฀-๿֐-׿؀-ۿऀ-ॿ]")


def _phrases(phrases):
    """One regex for ``phrases``: whole words in spaced scripts, substrings in scripts without spaces."""
    spaced = sorted((p for p in phrases if not _CJK.search(p)), key=len, reverse=True)
    other = sorted((p for p in phrases if _CJK.search(p)), key=len, reverse=True)
    parts = []
    if spaced:
        parts.append(r"(?<![\w-])(?:" + "|".join(re.escape(p) for p in spaced) + r")(?![\w-])")
    if other:
        parts.append("(?:" + "|".join(re.escape(p) for p in other) + ")")
    return re.compile("|".join(parts))


_CODE = _phrases(CODE_WORDS)
# A field that may ask for a code or a PIN, in any language ("Code", "Access code", "Código SMS", "PIN").
_CODE_FIELD = re.compile(r"(?:\b(?:code|codes|pin|otp|2fa|passcode|código|codigo|codice|kod|kode|kodu|mã)\b|код|コード|"
                         r"暗証|验证|驗證|密码|코드|인증|รหัส|קוד|رمز)", re.I)
# An app's own name for a one-time code field (textContentType oneTimeCode, an "otp" identifier).
_ONE_TIME_HINT = re.compile(r"one.?time.?code|onetimecode|\botp\b|verification.?code|sms.?code|2fa|two.?factor", re.I)
# A screen asking for a code without naming the field: "We sent a code to •••• 42", a two-factor title.
_CODE_SCREEN = re.compile(r"\b(?:sent (?:you )?(?:a|an|the|your) (?:\d+.digit )?(?:code|pin)|texted you|"
                          r"code (?:was )?sent|"
                          r"two.?factor|2.?step|two.?step|2fa|verify (?:it'?s you|your (?:identity|account|number|"
                          r"email|phone))|authentication|security code|login code|sign.?in code|enter (?:the )?code|"
                          r"check your (?:phone|email|messages|texts))\b", re.I)
# Fields that take digits and aren't codes: typing digits there is just typing.
_NOT_CODE_FIELD = re.compile(r"\b(?:quantity|qty|amount|price|total|tip|phone|mobile number|telephone|zip|postal|"
                             r"post ?code|postcode|year|age|guests?|people|adults|children|size|weight|height|count|"
                             r"number of|minutes?|hours?|days?|page|search|find|rating|score|reps|sets|calories|"
                             r"steps|distance|duration|timer|alarm)\b", re.I)
# A row that sends a one-time code, beyond the code words.
_OTP_TEXT = re.compile(r"\bis your (?:[\w.&'’-]+ ){0,3}(?:code|pin|passcode)\b|"
                       r"\byour (?:[\w.&'’-]+ ){0,3}(?:code|pin|passcode)"
                       r"(?: is)?\s*[:：]?\s*[A-Z]?-?\d|"
                       r"\b(?:do not|don'?t|never) share\b|(?:^|[\s(])[A-Z]{1,3}-\d{4,8}\b|"
                       r"\bexpires? in \d+\s*(?:min|mins|minutes|m)\b|\buse \d{4,10} to\b", re.I)
# Controls whose value a wheel or a slider sets: a timer's minutes, a date. Never a code box.
_PICKER_ROLES = frozenset({"PickerWheel", "Picker", "DatePicker", "Slider", "Stepper", "SegmentedControl"})
_DIGITS = re.compile(r"^\s*(?:\d[\s-]?){4,10}\s*$")
_ONE_DIGIT = re.compile(r"^\s*\d\s*$")
SEARCH_WORDS = re.compile(r"\b(?:search|find|where to|look up|query|filter)\b", re.I)


def looks_like_otp(text):
    """Whether a row reads like a one-time code sent to the person."""
    text = str(text or "")
    return bool(_CODE.search(norm(text)) or _CODE_SCREEN.search(text) or _OTP_TEXT.search(text))


def code_entry(action, text=None):
    """Typing or submitting a one-time code, which asks in every mode; a wheel or a slider never does."""
    op = (action.op or "").upper()
    if op not in TYPE_OPS and op not in RETURN_OPS:
        return False
    text = text if text is not None else (action.text or (action.typed if op in RETURN_OPS else None) or "")
    if not text.strip() or action.role in _PICKER_ROLES:
        return False
    field = norm(f"{action.label} {action.hint}")
    if _CODE.search(field) or _ONE_TIME_HINT.search(f"{action.label} {action.hint}"):
        return True
    around = f"{action.screen} {action.context}"
    asked = bool(_CODE.search(norm(around)) or _CODE_SCREEN.search(around))
    sent = action.origins is not None and any(looks_like_otp(row) for row in action.origins)
    if _DIGITS.match(text):
        if _CODE_FIELD.search(field) or asked or sent:
            return True
        if action.role == "SearchField" or _NOT_CODE_FIELD.search(field):
            return False
        return action.origins is None  # digits from nowhere a code was sent: just typing; unknown: ask
    if _ONE_DIGIT.match(text):
        # One box of a code ("_ _ _ _ _ _"): a code screen, a code field, digits a code sent, or (unknown) a bare box.
        return asked or sent or bool(_CODE_FIELD.search(field)) or (not field.strip() and action.origins is None)
    return False


# A control's first word that changes something on the phone: a list, a note, a setting, a draft.
CHANGE_FIRST = frozenset({
    "save", "add", "remove", "archive", "unarchive", "mark", "pin", "unpin", "mute", "unmute", "hide", "unhide",
    "turn", "enable", "disable", "allow", "deny", "block", "unblock", "reset", "clear", "set", "rename", "move",
    "favorite", "favourite", "unfavorite", "star", "unstar", "flag", "unflag", "restore", "lock", "unlock", "apply",
    "update", "change", "create", "accept", "decline", "confirm", "submit", "done", "like", "unlike", "complete",
    "join", "leave", "download", "upload", "install", "uninstall", "delete", "erase", "trash", "duplicate", "copy to",
    "keep", "approve", "reject", "dismiss all", "snooze"})


def changes(action):
    """The act of a change that stays on the phone, else None."""
    op = (action.op or "").upper()
    label = norm(action.label)
    if op in ADJUST_OPS:
        return "change"
    if op in RETURN_OPS:
        searching = action.role == "SearchField" or bool(SEARCH_WORDS.search(label))
        return None if searching else "submit"
    if op not in TAP_OPS:
        return None
    if action.role in SWITCH_ROLES:
        return "setting"
    words = label.split()
    if not words:
        return None
    first = words[0]
    if first in CHANGE_FIRST or " ".join(words[:2]) in CHANGE_FIRST:
        return "save" if first == "save" else "change"
    return None


# A control's first word that reacts, follows or reposts where others see it, or blocks or reports someone, -> its act.
OUTSIDE_FIRST = {"like": "react", "unlike": "react", "react": "react", "follow": "follow", "unfollow": "unfollow",
                 "repost": "post",
                 "retweet": "post", "reblog": "post", "block": "block", "unblock": "block", "report": "report"}
# Rows (not controls) whose words only say what they show: a song "Like a Prayer, Madonna" names no act.
CONTENT_ROLES = frozenset({"Cell", "StaticText", "Image", "Icon"})
ROW_WORDS = 4


def outside_act(action):
    """The act of a tap that reacts, follows, reposts, blocks or reports, else None; only a first word names one."""
    if (action.op or "").upper() not in TAP_OPS or action.role in ("TextField", "TextView", "SecureTextField",
                                                                     "SearchField"):
        return None
    label = " ".join((action.label or "").split())
    words = words_of(label)
    if not words or words[0] not in OUTSIDE_FIRST:
        return None
    if action.role in CONTENT_ROLES and ("," in label or len(words) > ROW_WORDS):
        return None
    return OUTSIDE_FIRST[words[0]]


# A commit's or change's act -> the kind an allow rule remembers; an act not here is never remembered.
RULE_KINDS = {
    "send_message": "send", "send": "send", "reply": "send", "comment": "comment", "post": "post", "publish": "post",
    "tweet": "post", "react": "react", "like": "react", "follow": "follow", "subscribe": "follow",
    "unfollow": "unfollow",
    "unsubscribe": "unfollow", "share": "share", "call": "call", "dial": "call", "facetime": "call", "join": "join",
    "leave": "leave", "accept": "accept", "save": "save", "archive": "archive", "setting": "setting",
    "change": "change"}
# How the card and Settings › Safety name each kind: "Don't ask again for sending messages in Messages".
RULE_WORDS = {
    "send": "sending messages", "comment": "comments", "post": "posting", "react": "reactions",
    "follow": "following", "unfollow": "unfollowing", "share": "sharing", "call": "calls", "join": "joining",
    "leave": "leaving", "accept": "accepting", "save": "saving", "archive": "archiving",
    "setting": "switching settings",
    "change": "changes"}
RULE_OP = "ACT"


# Words that undo something for good: never remembered, whatever kind of action they're part of.
DESTRUCTIVE = re.compile(r"\b(?:delete|erase|trash|remove|clear|reset|wipe|discard|destroy|deactivate|"
                         r"close (?:my |your |the )?account)\b", re.I)
# A price on a control or the screen ("$4.99/month", "Send $50.00", "€12", "1,200 coins").
PRICE = re.compile(r"(?:[$€£¥₩₽₹]\s?\d|\d[\d.,]*\s?(?:€|zł|kr|₽|円|元|원|usd|eur|gbp|coins?|gems?)\b|"
                   r"/\s?(?:mo|month|yr|year|wk|week)\b|\bper (?:month|year|week)\b|\bfree trial\b)", re.I)
# A screen about money: sending or moving it, paying, a balance, a checkout.
MONEY_WORDS = re.compile(r"\b(?:pay|payment|payments|paid|send money|request money|transfer|zelle|venmo|paypal|"
                         r"cash app|wallet|balance|bank|checkout|check out|purchase|buy|order total|subtotal|total|"
                         r"tip|donate|donation|subscription|billing|invoice|card ending|apple pay|google pay|crypto|"
                         r"bitcoin|invest|trade|deposit|withdraw)\b", re.I)
# Apps that span other apps (the Home Screen, iOS's own sheets): a rule there would follow the person everywhere.
SYSTEM_BUNDLES = frozenset({"com.apple.springboard", "com.apple.PassbookUIService", "com.apple.ios.StoreKitUIService",
                            "com.apple.AuthKitUIService", "com.apple.SafariViewService", "com.apple.sharing",
                            "com.apple.ShareSheetUI", "com.apple.InCallService", "com.apple.purplebuddy"})
_SYSTEM = re.compile(r"(?:UIService|ViewService|SheetUI)$")


def money_context(action):
    """Whether ``action`` is near money: an amount on it or the screen, money words, or a finance or password app."""
    from .policy import sensitive_category
    text = " ".join(str(part or "") for part in (action.label, action.value, action.text, action.typed, action.screen,
                                                  action.context))
    return bool(AMOUNT.search(text) or PRICE.search(text) or MONEY_WORDS.search(norm(text))
                or sensitive_category(action.app, action.app_name))


def system_app(bundle):
    return not bundle or bundle in SYSTEM_BUNDLES or bool(_SYSTEM.search(bundle))


def rememberable(action, act):
    """The kind of allow rule a "Don't ask again" may add or use for ``act`` here, or None."""
    kind = RULE_KINDS.get((act or "").strip().casefold())
    if kind is None or system_app(action.app) or code_entry(action):
        return None
    if DESTRUCTIVE.search(f"{action.label} {act or ''}") or money_context(action):
        return None
    return kind


def rule_words(kind, app_name):
    words = RULE_WORDS.get(kind, kind)
    return f"Don’t ask again for {words} in {app_name}" if app_name else f"Don’t ask again for {words}"


AMOUNT = re.compile(r"(?:[$€£¥₩₽₹]\s?\d[\d,]*(?:\.\d{1,2})?|\d[\d.,]*\s?(?:€|zł|kr|₽|円|元|원|usd|eur))", re.I)


def screen_context(elements, limit=300):
    """The few words on a screen that say what an action there means."""
    picked = []
    for element in elements or ():
        label, value = getattr(element, "label", "") or "", getattr(element, "value", "") or ""
        text = " ".join(f"{label} {value if value != label else ''}".split())
        if not text or getattr(element, "role", None) not in ("StaticText", "Other", "Cell", "Alert", "Sheet", "Button",
                                                             "Link"):
            continue
        folded = norm(text)
        if AMOUNT.search(text) or element.role in ("Alert", "Sheet") or _CODE.search(folded) or any(
                word in folded for word in ("total", "cannot be undone", "can't be undone", "permanently")):
            picked.append(text[:80])
        if sum(len(p) + 3 for p in picked) >= limit:
            break
    return " · ".join(picked)[:limit]
