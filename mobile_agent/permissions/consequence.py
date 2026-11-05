"""Which actions the person must approve first: independent signals that only add asks, never remove one. No I/O."""

import re
import unicodedata
from dataclasses import dataclass

from ..contract_screen import APP_STORE
from .rules import CONTENT_ROLES, RETURN_OPS, ROW_WORDS, SEARCH_WORDS, TAP_OPS, _phrases, norm, words_of

FIELD_ROLES = frozenset({"TextField", "TextView", "SecureTextField", "SearchField"})
CONTAINERS = frozenset({"Alert", "Sheet", "Menu", "Popover", "ActionSheet"})

MESSAGE_APPS = frozenset({
    "com.apple.MobileSMS", "com.apple.mobilemail", "net.whatsapp.WhatsApp", "com.facebook.Messenger",
    "ph.telegra.Telegraph", "org.whispersystems.signal", "com.tinyspeck.chatlyio", "com.hammerandchisel.discord",
    "com.google.Gmail", "com.microsoft.Office.Outlook", "com.burbn.instagram", "com.atebits.Tweetie2",
    "com.linkedin.LinkedIn", "com.toyopagroup.picaboo", "jp.naver.line", "com.tencent.xin", "com.facebook.Facebook",
    "com.cardify.tinder", "com.hinge.app", "com.bumble.app", "com.microsoft.skype.teams", "com.google.chat",
    "com.apple.facetime"})
PAY_BUNDLES = frozenset({"com.apple.PassbookUIService", "com.apple.ios.StoreKitUIService"})
MAIL_APPS = frozenset({"com.apple.mobilemail", "com.google.Gmail", "com.microsoft.Office.Outlook"})
SHORTCUTS = "com.apple.shortcuts"
# Apps whose row swipe archives or deletes on its own, with no buttons to tap first.
SWIPE_ACT_APPS = frozenset({"com.google.Gmail", "com.microsoft.Office.Outlook", "com.readdle.smartemail",
                            "com.superhuman.Superhuman", "ch.protonmail.protonmail"})
# The card's line for a swipe that may itself be the act (no reveal step).
SWIPE_ACTS, SWIPE_MAY = "A swipe archives or deletes in this app", "A swipe may archive or delete here"

# (act, pattern) in order: the first that matches names the act. Words are matched on rules.norm's folded label.
_ACTS = (
    ("unsend", r"\b(?:unsend|undo send|delete for everyone|remove for everyone)\b"),
    ("erase", r"\b(?:erase|wipe)\b"),
    ("reset", r"\breset\b(?! (?:zoom|filters?|view|search)\b)"),
    ("delete", r"\b(?:delete|destroy|deactivate|close (?:my |your |the )?account|trash|move to (?:the )?(?:trash|bin)|"
               r"send to (?:the )?(?:trash|bin)|discard|don.?t save)\b"),
    ("clear", r"\bclear\b(?! (?:text|search|query|field|filters?|selection|input)\b)"),
    ("remove", r"\b(?:remove|forget (?:this )?device|unpair)\b(?! (?:filters?|tag from search)\b)"),
    ("sign out", r"\b(?:sign|log) ?out\b"),
    ("unmatch", r"\b(?:unmatch|unfriend)\b"),
    ("leave", r"\b(?:leave|exit) (?:the )?(?:group|chat|conversation|channel|server|community|team|call)\b|^leave$"),
    ("end call", r"\b(?:end call|hang up)\b"),
    ("block", r"^(?:block|unblock)\b|\bblock (?:this|contact|caller|user|account)\b"),
    ("report", r"^report\b|\breport (?:this|post|user|account|spam|abuse|message|comment)\b"),
    ("junk", r"\bmark as (?:junk|spam)\b|^(?:junk|spam)$"),
    ("archive", r"^archive\b"),
    ("unfollow", r"^(?:unfollow|unsubscribe)\b"),
    ("pay", r"\b(?:pay|send money|transfer|donate|apple pay)\b|^tip\b"),
    ("buy", r"\b(?:buy|purchase|checkout|check out|place (?:your |the |this |my )?order|order now|rent)\b"),
    ("subscribe", r"\b(?:subscribe|start (?:my |your )?(?:free )?trial|try (?:it )?free|upgrade now)\b"),
    ("book", r"^(?:book|reserve)\b"),
    ("request", r"^request\b(?! (?:desktop|mobile) )"),
    ("send", r"\b(?:send|submit)\b"),
    ("post", r"\b(?:post|publish|tweet|repost|retweet|reblog)\b(?! (?:st|street)\b)"),
    ("call", r"^(?:call|facetime|dial)\b"),
    ("follow", r"^follow\b(?! up)"),
    ("react", r"^(?:like|unlike|react|super ?like|love)(?: (?:heart|thumbs? ?up|button|icon))?$|"
              r"^(?:like|react to) (?:this|post|photo|video|comment)\b"),
)
ACTS = tuple((act, re.compile(pattern, re.I)) for act, pattern in _ACTS)
# The same acts in the largest iOS languages other than English: a phone set to Spanish says "Eliminar", not "Delete".
_LOCALIZED = {
    "unsend": ("anular envío", "deshacer envío", "cancelar envío", "senden widerrufen", "annuler l’envoi",
               "annuler l'envoi", "annulla invio", "送信を取り消す", "送信取消", "撤回", "보내기 취소"),
    "delete": ("eliminar", "borrar", "excluir", "apagar", "löschen", "supprimer", "effacer", "elimina", "cancella",
               "verwijderen", "radera", "ta bort", "usuń", "удалить", "削除", "删除", "刪除", "삭제"),
    "sign out": ("cerrar sesión", "terminar sessão", "sair da conta", "abmelden", "se déconnecter", "déconnexion",
                 "esci", "afmelden", "logga ut", "wyloguj", "çıkış yap", "выйти", "サインアウト", "ログアウト",
                 "退出登录", "登出", "로그아웃"),
    "block": ("bloquear", "blockieren", "bloquer", "blocca", "blokkeren", "blockera", "zablokuj", "engelle",
              "заблокировать", "ブロック", "屏蔽", "封鎖", "차단"),
    "report": ("denunciar", "reportar", "melden", "signaler", "segnala", "rapporteren", "anmäl", "zgłoś",
               "şikayet et", "пожаловаться", "報告", "举报", "檢舉", "신고"),
    "pay": ("pagar", "bezahlen", "payer", "paga", "betalen", "betala", "zapłać", "öde", "оплатить", "支払う",
            "支払い", "支付", "付款", "결제"),
    "buy": ("comprar", "kaufen", "acheter", "acquista", "kopen", "köp", "kup teraz", "satın al", "купить", "購入",
            "购买", "購買", "구매"),
    "subscribe": ("suscribirse", "assinar", "abonnieren", "s’abonner", "s'abonner", "iscriviti", "abonneren",
                  "prenumerera", "subskrybuj", "abone ol", "подписаться на", "登録する", "订阅", "訂閱", "구독"),
    "send": ("enviar", "senden", "envoyer", "invia", "verzenden", "versturen", "skicka", "wyślij", "gönder",
             "отправить", "送信", "发送", "傳送", "發送", "보내기", "전송"),
    "post": ("publicar", "veröffentlichen", "posten", "publier", "pubblica", "plaatsen", "publicera", "opublikuj",
             "paylaş", "опубликовать", "投稿", "发布", "發佈", "게시"),
    "follow": ("seguir", "folgen", "suivre", "segui", "volgen", "följ", "obserwuj", "takip et", "подписаться",
               "フォロー", "关注", "追蹤", "팔로우"),
    "react": ("me gusta", "curtir", "gefällt mir", "j’aime", "j'aime", "mi piace", "vind ik leuk", "gilla",
              "lubię to", "beğen", "нравится", "いいね", "赞", "讚", "좋아요"),
}
LOCALIZED = tuple((act, _phrases(words)) for act, words in _LOCALIZED.items())
# SF Symbols a control may be named by when it has no label of its own.
GLYPHS = (("send", re.compile(r"paperplane(?:\.fill)?|arrow\.up\.circle(?:\.fill)?|arrow\.up\.message(?:\.fill)?")),
          ("react", re.compile(r"(?:heart|hand\.thumbsup|hand\.thumbsdown|suit\.heart)(?:\.fill)?")),
          ("follow", re.compile(r"person\.(?:badge|crop\.circle\.badge)\.plus(?:\.fill)?")),
          ("delete", re.compile(r"(?:minus\.circle|xmark\.bin|trash\.circle|trash\.slash)(?:\.fill)?")))
# Bare labels that only open a composer, a sheet or a list: their own Send or Post asks in turn.
OPENERS = frozenset({"share", "share…", "share...", "comment", "comments", "reply", "reply all", "forward", "call",
                     "message", "messages", "new message", "compose", "post", "new post", "send"})
# ... except where the act happens on tap: a bare Send or Post is always the act (removed from OPENERS below).
OPENERS = OPENERS - {"send", "post", "call"}
# Rows named for a place, not an act: Mail's and Notes' folders.
FOLDERS = frozenset({"trash", "bin", "junk", "spam", "archive", "deleted", "recently deleted", "drafts", "sent",
                     "outbox", "blocked", "blocked contacts", "report", "reports"})
# Buttons that back out of a dialog: never the act.
SAFE = re.compile(r"^(?:cancel|close|not now|later|maybe later|no|no thanks|keep|keep editing|keep (?:it|note|"
                  r"photo|message|draft)|go back|back|dismiss|stay|don.?t (?:allow|delete|remove|leave|sign out|"
                  r"erase|reset|unsend|send|post)|skip|x|xmark|done)$", re.I)
# Words of a button that confirms whatever the dialog or screen says.
CONFIRM = re.compile(r"^(?:ok|okay|yes|continue|confirm|proceed|i understand|agree|accept|approve|"
                     r"erase now|delete now|erase iphone|confirm with side button|pay now|authori[sz]e|"
                     r"(?:\w+ )?anyway)$", re.I)
# What a dialog or a screen says it destroys or charges.
DESTROYS = re.compile(r"\b(?:delete[ds]?|deleting|erase[ds]?|erasing|remove[ds]?|removing|wipe[ds]?|"
                      r"(?:can.?t|cannot|can not) be (?:undone|recovered|reversed)|permanently|unsend|discard|"
                      r"sign(?:ing)? out|log(?:ging)? out|reset|leave (?:the )?(?:group|chat|conversation)|unmatch|"
                      r"block|report)\b", re.I)
CHARGES = re.compile(r"(?:[$€£¥₩₽₹]\s?\d|\d[\d.,]*\s?(?:€|zł|kr|usd|eur|gbp)\b|/\s?(?:mo|month|yr|year|wk|week)\b|"
                     r"\bper (?:month|year|week)\b|\bfree trial\b|\bsubscription\b|\bpurchase\b|\bapple pay\b)", re.I)
TAPBACKS = frozenset({"heart", "love", "thumbs up", "thumbs down", "like", "dislike", "haha", "ha ha", "emphasize",
                      "exclamation mark", "question mark", "!!", "?"})
# Controls beside a composer that never send: its own toolbar.
COMPOSER_TOOLS = re.compile(r"\b(?:camera|photos?|apps?|audio|record|emoji|gif|stickers?|attach\w*|add|plus|voice|"
                            r"microphone|mic|dictat\w*|expand|more|keyboard|gallery|location|files?|edit|cancel|"
                            r"close|back|search)\b", re.I)
COMPOSER = re.compile(r"\b(?:compose|message|imessage|text message|chat|comment|reply|write|tweet|caption|"
                      r"say something|on your mind|post)\b|\.\.\.$|…$", re.I)
NOT_MESSAGE_FIELD = re.compile(r"\b(?:search|find|query|filter|to|cc|bcc|subject|body|recipients?|address|url|website|"
                               r"name|email|phone|username|password|where to|destination|location|city|zip)\b", re.I)
PROTECTIONS = re.compile(r"\b(?:find my|stolen device protection|two.factor|2fa|passcode|face id|touch id|"
                         r"icloud backup|location services|erase data|lockdown mode|advanced data protection)\b", re.I)
# Shortcuts' own controls (tabs, folders, bars); every other tile there runs a shortcut.
SHORTCUTS_NAV = re.compile(r"^(?:shortcuts|automation|gallery|search|edit|select|done|back|cancel|add|new shortcut|"
                           r"\+|more|all shortcuts|share sheet|apple watch|my shortcuts|folders?|new folder|"
                           r"details|settings|library|sort|filter|close)$", re.I)
# A search field's words and a browser's address bar's names; a web page's own fields sit in a web view and never count.
ADDRESS_LABEL = re.compile(r"^(?:address(?: bar)?|address and search|web address|url)$", re.I)
BROWSERS = frozenset({"com.apple.mobilesafari", "com.google.chrome.ios", "org.mozilla.ios.Firefox",
                      "com.microsoft.msedge", "com.brave.ios.browser", "com.duckduckgo.mobile.ios",
                      "com.opera.OperaTouch"})
# A button that clears a field's text, as a search bar draws it: the whole label, nothing else on it.
CLEAR_BUTTON = re.compile(r"^clear$", re.I)
CLEAR_GAP = .12  # how far (screen width) a Clear button may sit past the end of the field it clears

SF_SYMBOL = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)+")
TRASH_GLYPH = re.compile(r"(?:trash|xmark\.bin|bin\.xmark)(?:\.[a-z0-9]+)*")


@dataclass(frozen=True)
class Reading:
    act: str      # the card's verb: "delete", "send", "sign out"...
    signal: str   # which signal found it
    reason: str   # the card's line, at most 12 words


def label_act(label, role=None, rect=None, pixels=False):
    """The act a control's own label names, or None (``label`` signal)."""
    text = " ".join(str(label or "").split())
    if not text or role in FIELD_ROLES:
        return None
    if TRASH_GLYPH.fullmatch(text):
        return "delete"
    glyph = next((act for act, glyph in GLYPHS if glyph.fullmatch(text)), None)
    if glyph is not None and role not in CONTENT_ROLES:
        return glyph
    if SF_SYMBOL.fullmatch(text):
        return None
    folded = norm(text)
    if folded in OPENERS:
        return None
    # A folder's row is full width (Mail's Trash); a swipe action or a toolbar button named Trash is narrow.
    folder = folded in FOLDERS and (not rect or rect[2] >= .6) and (role in CONTENT_ROLES or pixels)
    if folder or role in CONTENT_ROLES and ("," in text or len(words_of(text)) > (6 if pixels else ROW_WORDS)
                                            or re.search(r",\s*(?:incomplete|completed)$", text, re.I)):
        return None
    head = " ".join(words_of(text)[:6])
    for act, pattern in ACTS:
        found = pattern.search(head)
        if found is None:
            continue
        if role in CONTENT_ROLES and found.start() != 0:
            continue
        return act
    for act, pattern in LOCALIZED:
        found = pattern.search(folded)
        if found is not None and (role not in CONTENT_ROLES or found.start() == 0):
            return act
    return None


def _inside(outer, inner):
    x, y, w, h = outer
    cx, cy = inner[0] + inner[2] / 2, inner[1] + inner[3] / 2
    return x - 1e-6 <= cx <= x + w + 1e-6 and y - 1e-6 <= cy <= y + h + 1e-6


def _container(element, elements):
    """The smallest alert, sheet or menu that holds ``element``, or None."""
    rect = getattr(element, "rect", None)
    if rect is None:
        return None
    holders = [e for e in elements if e is not element and getattr(e, "role", None) in CONTAINERS
               and _inside(e.rect, rect)]
    return min(holders, key=lambda e: e.rect[2] * e.rect[3]) if holders else None


def _texts_in(container, elements):
    out = [container.label or ""]
    for e in elements:
        if e is container or getattr(e, "role", None) not in ("StaticText", "Other", "Text"):
            continue
        if _inside(container.rect, e.rect):
            out.append(e.label or "")
    return " ".join(out)


def _screen_texts(elements):
    return " ".join((e.label or "") for e in elements if getattr(e, "role", None) in ("StaticText", "NavigationBar",
                                                                                      "Other", "Text"))


def _emoji_only(label):
    text = "".join(str(label or "").split())
    if not text:
        return False
    return all(unicodedata.category(ch) in ("So", "Sk", "Mn", "Cf") or ch in "‍️" for ch in text) and any(
        unicodedata.category(ch) == "So" for ch in text)


def _composer(elements):
    """The message composer on the screen (an editable field named like one, not a search), or None."""
    for e in elements:
        if getattr(e, "role", None) in ("TextView", "TextField") and getattr(e, "editable", True):
            words = f"{e.label or ''} {getattr(e, 'placeholder', '') or ''}"
            if COMPOSER.search(words) and not NOT_MESSAGE_FIELD.search(words):
                return e
    return None


def _beside_written_composer(element, elements):
    """Whether ``element`` sits right of the message composer on its line while the composer holds text of its own."""
    composer = _composer(elements)
    if composer is None or element is composer:
        return False
    value = (getattr(composer, "value", "") or "").strip()
    if not value or value == (getattr(composer, "placeholder", "") or "").strip():
        return False
    line = composer.rect[1] + composer.rect[3] / 2
    return (abs(element.rect[1] + element.rect[3] / 2 - line) <= .04
            and element.rect[0] >= composer.rect[0] + composer.rect[2] - .01)


def _act_from_text(text):
    """The act a dialog's or a screen's words say it performs."""
    folded = norm(text)
    for act, word in (("erase", r"\berase|\bwipe"), ("unsend", r"\bunsend"), ("sign out", r"\bsign(?:ing)? out|\blog"),
                      ("leave", r"\bleave"), ("unmatch", r"\bunmatch"), ("block", r"\bblock"),
                      ("report", r"\breport"), ("reset", r"\breset"), ("remove", r"\bremov"),
                      ("delete", r"\bdelet|\bdiscard|permanently|undone|recovered|reversed")):
        if re.search(word, folded):
            return act
    return "delete"


def read(action, elements=()):
    """The Reading for ``action`` on a screen of ``elements``, or None when no signal says it acts past the phone."""
    op = (action.op or "").upper()
    role = action.role
    label = " ".join(str(action.label or "").split())
    elements = tuple(elements or getattr(action, "around", ()) or ())
    bundle = action.app or ""
    if op in RETURN_OPS:
        return _return(action, elements)
    pixels = bool(getattr(action, "pixels", False))
    rect = tuple(getattr(action, "rect", ()) or ()) or None
    if op in ("SWIPE_LEFT", "SWIPE_RIGHT"):
        # swipe: in apps where a row's swipe is the act itself (no reveal step).
        if (role == "Cell" or pixels) and bundle in SWIPE_ACT_APPS:
            return Reading("archive", "swipe", SWIPE_ACTS)
        if pixels and not bundle and rect:
            # Pixels in an app Mobster can't name: the swipe may be one that archives or deletes.
            return Reading("archive", "swipe", SWIPE_MAY)
        return None
    if op not in TAP_OPS or role in FIELD_ROLES:
        return None
    element = _element(action, elements)
    folded = norm(label)
    if pixels and not bundle and any(norm(e.label) in ("shortcuts", "all shortcuts") and e.rect[1] < .15
                                     for e in elements):
        bundle = SHORTCUTS  # pixels in an app Mobster can't name, whose title says it is Shortcuts
    if keypad_key(label, role, elements, pixels):
        return None  # a calculator's or a dial pad's key: Clear, Delete
    if clears_query_field(label, role, element, elements, bundle, pixels):
        return None  # a search bar's Clear: it empties a query, not a draft or a note
    # glyph: an icon whose shape commits under a name that doesn't (a paper plane named "Direct" by a message box).
    glyph = glyph_act(getattr(action, "glyph", ""), rect, elements, bundle)
    if glyph is not None and label_act(label, role, rect, pixels) is None:
        return Reading(glyph, "glyph", f"It looks like a {action.glyph}; it's called “{label[:24]}”")
    # App Store: Get, Install, a price (on pixels in an app Mobster can't name, a bare Get or price too).
    if ((bundle == APP_STORE or pixels and not bundle and re.fullmatch(r"get|[$€£¥]\s?[\d.,]+", folded))
            and re.match(r"^(?:get|install|redownload|re-download|buy|purchase|[$€£¥]\s?\d)", folded)):
        return Reading("buy", "label", "Gets an app from the App Store")
    # dialog: a button in an alert, a sheet or a menu that says it destroys or charges.
    container = _container(element, elements) if element is not None else None
    if container is not None and not SAFE.match(folded):
        said = _texts_in(container, elements)
        if DESTROYS.search(said) and (CONFIRM.match(folded) or label_act(label, role) or not folded
                                      or DESTROYS.search(label)):
            return Reading(label_act(label, role) or _act_from_text(said), "dialog", "The dialog says what this does")
        if CHARGES.search(said) and (CONFIRM.match(folded) or label_act(label, role) in ("pay", "buy", "subscribe")):
            return Reading("pay", "dialog", "Spends money")
    act = label_act(label, role, rect, pixels)
    if act == "send" and folded in ("send", "submit") and not elements:
        return Reading(act, "label", "Sends this")
    if act is not None:
        if act in ("book",) and bundle == APP_STORE:
            act = "buy"
        if act == "react" and container is None and role in CONTENT_ROLES and not pixels:
            act = None
        else:
            return Reading(act, "label", REASONS.get(act, "Has an effect beyond your iPhone"))
    # share: Share or Post with a caption the agent wrote.
    if folded == "share" and pixels:
        # On pixels a Share button can't be told from a share sheet's opener or a post's publish: unknown asks.
        return Reading("post", "share", "This may publish what is on screen")
    if folded in ("share", "post") and any(
            getattr(e, "editable", False) and (getattr(e, "value", "") or "").strip()
            and (getattr(e, "value", "") or "") != getattr(e, "placeholder", "")
            and getattr(e, "role", None) != "SearchField" for e in elements):
        return Reading("post", "share", "Publishes what you wrote")
    # screen: a confirming button on a screen that says it destroys or charges.
    if not SAFE.match(folded) and (CONFIRM.match(folded) or folded and bundle in PAY_BUNDLES):
        said = _screen_texts(elements) + " " + str(action.context or "")
        if bundle in PAY_BUNDLES or CHARGES.search(said) and re.search(r"\b(?:pay|buy|purchase|subscri|charge|"
                                                                        r"trial|order|total)", said, re.I):
            return Reading("pay", "screen", "Spends money")
        if re.search(r"(?:will be|are|is) (?:permanently )?(?:erased|deleted|removed|wiped)|(?:can.?t|cannot|can not)"
                     r" be (?:undone|recovered|reversed)|permanently", said, re.I):
            return Reading(_act_from_text(said), "screen", "The screen says this can't be undone")
    # setting: switching off a protection, or any switch the request doesn't ask to change (the scope lock).
    if role in ("Switch", "Toggle") and PROTECTIONS.search(label) and (action.value or "") in ("1", "on", "true"):
        return Reading("turn off", "setting", "Turns off a protection")
    if role in ("Switch", "Toggle") and not names_switch(action.goal, label):
        return Reading("setting", "scope", "Your request doesn't name this setting")
    if role not in ("Switch", "Toggle") and re.match(r"^turn off\b", folded) and PROTECTIONS.search(
            label + " " + _screen_texts(elements)):
        return Reading("turn off", "setting", "Turns off a protection")
    # iOS names an unlabelled SF Symbol by its shape ("Up"): a control right of a written message box asks.
    if element is not None and (role == "Button" or pixels) and _beside_written_composer(element, elements) \
            and not COMPOSER_TOOLS.search(folded) and not SAFE.match(folded):
        return Reading("send", "beside_composer", "It's beside your message; it may send it")
    if bundle in MESSAGE_APPS or pixels and not bundle:
        # reaction: an emoji button, or a Tapback (in a menu; on pixels,
        # any Tapback word in Messages or an unknown app).
        if _emoji_only(label) or (folded in TAPBACKS and (container is not None and bundle == "com.apple.MobileSMS"
                                                          or pixels and bundle in ("", "com.apple.MobileSMS"))):
            return Reading("react", "reaction", "Sends a reaction")
        # reply_chip: a sentence-like button just above the composer.
        composer = _composer(elements)
        if (composer is not None and element is not None and (role == "Button" or pixels) and len(words_of(label)) >= 2
                and not COMPOSER_TOOLS.search(folded) and not SAFE.match(folded)
                and 0 <= composer.rect[1] - (element.rect[1] + element.rect[3]) <= .15):
            return Reading("send", "reply_chip", "A quick reply sends on tap")
    elif _emoji_only(label) and _composer(elements) is not None:
        return Reading("react", "reaction", "Sends a reaction")
    if bundle == SHORTCUTS and (role in ("Button", "Cell") or pixels) and folded and not SHORTCUTS_NAV.match(folded) \
            and not re.search(r",\s*\d+(?: shortcuts?)?$", label) and element is not None and element.rect[3] >= .06:
        return Reading("run", "shortcut", "Runs this shortcut")
    return None


# What a glyph that commits does (perception.COMMIT_GLYPHS, kept here so this check stands alone).
GLYPH_ACTS = {"paper plane": "send", "arrow up": "send", "heart": "react", "thumbs up": "react", "trash": "delete",
              "person plus": "follow", "repost": "post"}
BAR_TOP, BAR_BOTTOM = .13, .9


def glyph_act(glyph, rect, elements, bundle=""):
    """The act an icon's glyph performs where it is drawn, or None."""
    act = GLYPH_ACTS.get(glyph or "")
    if act is None:
        return None
    if not rect:
        return act
    cy = rect[1] + rect[3] / 2
    bar = cy < BAR_TOP or cy > BAR_BOTTOM
    # A message box on the icon's line: a field, a composer's words, or on pixels any words left of it on that line.
    composer = any(abs(e.rect[1] + e.rect[3] / 2 - cy) <= .04 and e.rect[0] + e.rect[2] <= rect[0] + .01
                   and (getattr(e, "role", None) in ("TextField", "TextView") or COMPOSER.search(e.label or "")
                        or getattr(e, "role", None) == "StaticText" and len((e.label or "").strip()) >= 2)
                   for e in elements)
    if glyph == "paper plane":
        return act if composer or not bar else None
    if glyph == "arrow up":
        return act if composer or bundle in MESSAGE_APPS else None
    if glyph in ("heart", "thumbs up"):
        return None if bar else act
    return act


KEYPAD = re.compile(r"^(?:clear|all clear|ac|c|delete|backspace|⌫)$", re.I)


def keypad_key(label, role, elements, pixels=False):
    """A keypad's Clear or Delete key (Calculator, a dial pad): six or more digit keys on the screen."""
    keys = ("Button", "Key") + (("StaticText",) if pixels else ())
    if role not in keys or not KEYPAD.match(norm(label)):
        return False
    digits = {e.label.strip() for e in elements if getattr(e, "role", None) in keys
              and re.fullmatch(r"\d", (e.label or "").strip())}
    return len(digits) >= 6


def query_field(element, elements, bundle="", pixels=False):
    """Whether ``element`` is a search field or a browser's address bar, told with certainty."""
    if element is None or pixels or getattr(element, "role", None) not in ("SearchField", "TextField"):
        return False
    if _send_on_line(element, elements):
        return False
    words = f"{element.label or ''} {getattr(element, 'placeholder', '') or ''}"
    if element.role == "SearchField" or SEARCH_WORDS.search(words):
        return True
    return (bundle in BROWSERS and element.role == "TextField" and bool(ADDRESS_LABEL.match(norm(element.label or "")))
            and "WebView" not in (getattr(element, "locator", "") or ""))


def query_return(action, elements=()):
    """Return (SUBMIT, TYPE_SUBMIT) in a search field or an address bar: it runs the search or loads the page."""
    elements = tuple(elements or getattr(action, "around", ()) or ())
    return query_field(_element(action, elements), elements, action.app or "", bool(getattr(action, "pixels", False)))


def clears_query_field(label, role, element, elements, bundle="", pixels=False):
    """A bare Clear button at or just past the end of a search field or address bar: it empties the query."""
    if pixels or role != "Button" or element is None or not CLEAR_BUTTON.match(norm(label)):
        return False
    y = element.rect[1] + element.rect[3] / 2
    line = [e for e in elements if e is not element and getattr(e, "role", None) in FIELD_ROLES
            and abs(e.rect[1] + e.rect[3] / 2 - y) <= .05]
    if not line or not all(query_field(e, elements, bundle) for e in line):
        return False
    return any(e.rect[0] <= element.rect[0] <= e.rect[0] + e.rect[2] + CLEAR_GAP for e in line)


# A request that asks to change something (not only to look).
CHANGE_ASKED = re.compile(r"\b(?:turn|switch|set|change|enable|disable|toggle|allow|block|mute|unmute|activate|"
                          r"deactivate|start|stop|pause|resume|update|reset|clear|lock|unlock|hide)\b",
                          re.I)


def _beside_send(element, elements):
    """A field with a Send or Post control on its line: Return there sends what it holds."""
    return bool(getattr(element, "editable", False)) and _send_on_line(element, elements)


def _send_on_line(element, elements):
    """Whether a Send or Post control sits on ``element``'s line (whatever the element's role)."""
    y = element.rect[1] + element.rect[3] / 2
    for other in elements:
        if other is element or getattr(other, "editable", False):
            continue
        if label_act(other.label, other.role) in ("send", "post") and abs(other.rect[1] + other.rect[3] / 2 - y) <= .12:
            return True
    return False


_STOP = frozenset({"the", "and", "for", "with", "my", "your", "this", "that", "turn", "on", "off", "set", "allow",
                   "show", "open", "settings", "setting", "switch", "toggle", "enable", "disable", "please"})


def names_switch(goal, label):
    """Whether a request asks to change the switch ``label``."""
    if not goal or not CHANGE_ASKED.search(re.sub(r"[^.!?]*\b(?:do not|don't|never|without)\b[^.!?]*", " ", goal)):
        return False
    stems = lambda text: {w[:5] for w in re.findall(r"[^\W\d_]{2,}", norm(text)) if w not in _STOP}  # noqa: E731
    return bool(stems(goal) & stems(label))


def _return(action, elements):
    """Return in a message composer sends (the ``return`` signal)."""
    role = action.role
    words = f"{action.label or ''} {action.hint or ''}"
    if role == "SearchField" or SEARCH_WORDS.search(words):
        return None
    element = _element(action, elements)
    if re.match(r"^\s*(?:to|cc|bcc|recipients?)\b", action.label or "", re.I):
        return None
    if element is not None and elements and _beside_send(element, elements):
        return Reading("send", "return", "Return sends this message")
    if COMPOSER.search(words) and not NOT_MESSAGE_FIELD.search(words):
        return Reading("send", "return", "Return sends this message")
    if (action.app or "") in MESSAGE_APPS - MAIL_APPS and not NOT_MESSAGE_FIELD.search(words):
        return Reading("send", "return", "Return sends this message")
    rect = tuple(getattr(action, "rect", ()) or ())
    if (getattr(action, "pixels", False) and not action.app and not NOT_MESSAGE_FIELD.search(words)
            and (not rect or rect[1] + rect[3] / 2 > .45)):
        # Pixels in an app Mobster can't name: Return low on the screen, where a message box sits, may send.
        return Reading("send", "return", "Return may send this")
    return None


def _element(action, elements):
    """The screen element ``action`` names: the same frame, else the same label and role."""
    rect = getattr(action, "rect", None)
    if rect:
        for e in elements:
            if tuple(getattr(e, "rect", ()) or ()) == tuple(rect):
                return e
    for e in elements:
        if (e.label or "") == (action.label or "") and getattr(e, "role", None) == action.role:
            return e
    return None


REASONS = {"request": "Sends a request", "unsend": "Takes back a message others may see",
           "erase": "Erases data for good",
           "reset": "Resets settings or data", "delete": "Deletes something", "clear": "Clears data",
           "remove": "Removes something", "sign out": "Signs you out", "unmatch": "Ends a match for good",
           "leave": "Leaves a group others see", "end call": "Ends a call", "block": "Changes who can reach you",
           "report": "Sends a report about someone", "junk": "Marks mail as junk", "archive": "Archives this",
           "unfollow": "Others see this unfollow", "pay": "Spends money", "buy": "Spends money",
           "subscribe": "Starts a charge", "book": "Makes a booking", "send": "Sends this",
           "post": "Others see this post", "call": "Places a call", "follow": "Others see this follow",
           "react": "Others see this reaction", "run": "Runs this shortcut", "turn off": "Turns off a protection"}

# A swipe's card names both acts it may do: which one a full swipe does is the app's setting.
REASON_TITLES = {SWIPE_ACTS: "Archive or delete this?", SWIPE_MAY: "Archive or delete this?"}

# The card's title for each act this module names.
TITLES = {"request": "Send this request?", "unsend": "Unsend this message?", "erase": "Erase this?",
          "reset": "Reset this?", "delete": "Delete this?",
          "clear": "Clear this?", "remove": "Remove this?", "sign out": "Sign out?", "unmatch": "Unmatch?",
          "leave": "Leave this group?", "end call": "End this call?", "block": "Block this?",
          "report": "Report this?", "junk": "Mark this as junk?", "archive": "Archive this?",
          "unfollow": "Unfollow this account?", "pay": "Make this payment?", "buy": "Buy this?",
          "subscribe": "Subscribe?", "book": "Make this booking?", "send": "Send this message?",
          "post": "Post this?", "call": "Place this call?", "follow": "Follow this account?",
          "react": "Send this reaction?", "run": "Run this shortcut?", "turn off": "Turn this off?"}
