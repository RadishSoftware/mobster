"""The user's permission settings: the mode, the per-task override, and which apps Mobster may use."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ASK_ALL, ASK_IMPORTANT, DONT_ASK = "ask_all", "ask_important", "dont_ask"
MODES = (ASK_ALL, ASK_IMPORTANT, DONT_ASK)
DEFAULT_MODE = ASK_IMPORTANT


def mode_for_ask(ask, current):
    """The mode the older askBeforeActing switch sets: off is full access; on keeps a mode that asks,
    else the default."""
    if not ask:
        return DONT_ASK
    return current if current in (ASK_ALL, ASK_IMPORTANT) else DEFAULT_MODE


# Strictest first: an override may always move left; moving right needs the Mac app.
STRICTNESS = {ASK_ALL: 2, ASK_IMPORTANT: 1, DONT_ASK: 0}
MODE_LABELS = {ASK_ALL: "Ask before every change", ASK_IMPORTANT: "Ask before acting", DONT_ASK: "Full access"}
# Origins where an outside agent, a tool or a notification started the task: never below ask_important (Remote, rule 1).
OUTSIDE_ORIGINS = frozenset({"mcp", "remote", "hook", "trigger", "console"})

ENV_MODE = "MOBSTER_PERMISSION_MODE"
ENV_ACCESS = "MOBSTER_APP_ACCESS"
ENV_ALLOWED = "MOBSTER_APPS_ALLOWED"
# The blocked list is the one lockscreen.blocked_apps already reads (MOBSTER_BLOCKED_APPS): one list, two readers.
ENV_BLOCKED = "MOBSTER_BLOCKED_APPS"
ENV_LEGACY = "MOBSTER_ASK_BEFORE_ACTING"
ENV_OUTSIDE_BLOCKED = "MOBSTER_OUTSIDE_BLOCKED_APPS"
ENV_KEYS = ("MOBSTER_PERMISSION_MODE", "MOBSTER_APP_ACCESS", "MOBSTER_APPS_ALLOWED", "MOBSTER_BLOCKED_APPS",
            "MOBSTER_ASK_BEFORE_ACTING", "MOBSTER_OUTSIDE_BLOCKED_APPS")
ACCESS = ("ask", "all")
# MOBSTER_APP_ACCESS unset (the tests set "all": a test's task never stops at the first-use card unless it asks to).
DEFAULT_ACCESS = "ask"
# An app approval in the "Don't ask again" store: one entry per app (its bundle id, this label and operation).
APP_LABEL, APP_OP = "App", "APP"
SPRINGBOARD = "com.apple.springboard"
SETTINGS = "com.apple.Preferences"
MAX_LISTED = 300
_BUNDLE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*[A-Za-z0-9_]")

# Bank, payment, crypto, Wallet and password apps: "Don't ask again" is never offered in them; opening one asks alike.
SENSITIVE_BUNDLES = {
    "com.apple.Passbook": "wallet", "com.apple.Passwords": "passwords", "com.apple.PassKit": "wallet",
    "com.chase": "finance", "com.bankofamerica.BofA": "finance", "com.bofa.mobile": "finance",
    "com.wellsfargo.net.mobilebanking": "finance", "com.capitalone.enterprisemobilebanking": "finance",
    "com.citigroup.citimobile": "finance", "com.usbank.mobilebanking": "finance", "com.ally.MobileBanking": "finance",
    "com.discoverfinancial.mobile": "finance", "com.americanexpress.amelia": "finance",
    "com.americanexpress.amexservice": "finance", "com.schwab.mobile": "finance", "com.fidelity.iphone": "finance",
    "com.vanguard.ios": "finance", "com.etrade.mobilepro.iphone": "finance",
    "com.robinhood.release.Robinhood": "finance",
    "com.vilcsak.bitcoin2": "finance", "com.coinbase.wallet": "finance", "com.revolut.revolut": "finance",
    "co.uk.getmondo": "finance", "com.transferwise.Transferwise": "finance", "com.sofi.mobile": "finance",
    "com.1debit.ChimeProdApp": "finance", "com.zellepay.zelle": "finance", "net.kortina.labs.Venmo": "finance",
    "com.squareup.cash": "finance", "com.yourcompany.PPClient": "finance", "com.paypal.business": "finance",
    "com.creditkarma.mobile": "finance", "com.intuit.mint": "finance", "com.monarchmoney.app": "finance",
    "com.hsbc.hsbcnet": "finance", "com.barclays.ios.bmb": "finance", "uk.co.santander.santanderUK": "finance",
    "com.lloydsbank.businessmobile": "finance", "com.navyfederal.navyfederal.mydesk": "finance",
    "com.usaa.mobile.ios.usaa": "finance", "com.pnc.pncmobile": "finance", "com.td.tdbank": "finance",
    "com.kraken.invest.app": "finance", "com.binance.dev": "finance", "com.webull.trade": "finance",
    "de.number26.n26": "finance", "com.klarna.KlarnaApp": "finance",
    "com.affirm.Affirm": "finance", "com.afterpay.afterpay-consumer": "finance"}
# By name, for banks and credit unions the list above doesn't know.
SENSITIVE_NAME = re.compile(r"\b(bank|banking|banco|banque|credit union|fcu|federal credit|wallet|venmo|zelle|"
                            r"paypal|cash app|robinhood|coinbase|crypto|brokerage|invest(ing|ments?)?|trading|"
                            r"wise|revolut|monzo|n26|chime|klarna|affirm|afterpay|sofi|starling|nubank|"
                            r"1password|lastpass|bitwarden|dashlane|keeper|authenticator|passwords?)\b", re.I)


def sensitive_category(bundle, name=None):
    """"finance" | "wallet" | "passwords" | None for an app: what kind of screens it has, for the classifier."""
    if bundle in SENSITIVE_BUNDLES:
        return SENSITIVE_BUNDLES[bundle]
    if name and SENSITIVE_NAME.search(name):
        found = SENSITIVE_NAME.search(name).group(0).casefold()
        return "passwords" if found in ("1password", "lastpass", "bitwarden", "dashlane", "keeper", "authenticator",
                                        "password", "passwords") else "wallet" if found == "wallet" else "finance"
    return None


def _ids(raw):
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if item and len(item) <= 255 and _BUNDLE.fullmatch(item) and "." in item and item not in out:
            out.append(item)
    return tuple(out[:MAX_LISTED])


def validate_mode(value):
    if value not in MODES:
        raise ValueError("permissionMode must be ask_all, ask_important or dont_ask")
    return value


def validate_ids(values, name):
    if not isinstance(values, list) or len(values) > MAX_LISTED:
        raise ValueError(f"{name} must be a list of at most {MAX_LISTED} app bundle ids")
    out = []
    for value in values:
        if not isinstance(value, str) or not _BUNDLE.fullmatch(value) or "." not in value or len(value) > 255:
            raise ValueError(f"{name} must be app bundle ids")
        if value not in out:
            out.append(value)
    return out


@dataclass(frozen=True)
class AppPolicy:
    access: str = "ask"
    allowed: frozenset = field(default_factory=frozenset)
    blocked: frozenset = field(default_factory=frozenset)
    outside_blocked: frozenset = field(default_factory=frozenset)

    def allows(self, bundle, outside=False):
        """Whether Mobster may use ``bundle``: False only if never-open or, ``outside``, off for outside agents."""
        if not bundle or bundle == SPRINGBOARD:
            return True
        return not (bundle in self.blocked or outside and bundle in self.outside_blocked)

    def approved(self, bundle, grants=None):
        """Whether ``bundle`` opens without asking (Home Screen, access all, allow list, a grant), unless never-open."""
        if not bundle or bundle == SPRINGBOARD:
            return True
        if bundle in self.blocked:
            return False
        if self.access == "all" or bundle in self.allowed:
            return True
        try:
            return bool(grants is not None and grants.contains(bundle, APP_LABEL, APP_OP))
        except Exception:
            return False  # an unreadable store asks again

    def status(self, bundle, grants=None):
        """{allowed, why, outside} for one app, as Settings › Safety and the Apps view show it."""
        allowed = self.approved(bundle, grants)
        why = None if allowed else ("blocked" if bundle in self.blocked else "asks")
        return {"allowed": allowed, "why": why, "outside": bundle not in self.blocked
                and bundle not in self.outside_blocked}

    def blocked_message(self, name, outside=False):
        name = name or "This app"
        if outside:
            return (f"{name} is off for agents outside Mobster. You can turn it on for them in Settings › Safety, "
                    "or start the task in the Mobster app.")
        return f"{name} is on your list of apps Mobster never opens."

    @staticmethod
    def unapproved_message(name):
        name = name or "this app"
        return (f"Mobster didn't open {name}: it needs your OK to use an app. Choose Allow when it asks, or switch "
                f"{name} on in Apps, in Mobster's sidebar.")


ALLOW_ALL = AppPolicy(access="all")


def app_card_reason(count=1):
    """The line under "Allow Mobster to use Things?": what allowing an app means (at most 120 characters)."""
    if count == 1:
        return "Mobster then reads and moves around in it without asking. Turn it off anytime in Settings › Safety."
    return ("Mobster then reads and moves around in them without asking. Turn any of them off anytime in "
            "Settings › Safety.")


def task_card_reason(count=1, conversation=False):
    """The line under a task's app card, "Allow Mobster to use Things and Notes?"."""
    scope = "conversation" if conversation else "task"
    return (f"Allow is for this {scope}. Always allow keeps {'it' if count == 1 else 'them'} for later tasks, "
            f"until you turn {'it' if count == 1 else 'one'} off in Settings › Safety.")


ALWAYS_ALLOW = "Always allow"  # the task card's second button: the apps it lists are kept in the store


@dataclass(frozen=True)
class Settings:
    mode: str = DEFAULT_MODE
    apps: AppPolicy = field(default_factory=AppPolicy)

    def public(self):
        # envAllowed: apps MOBSTER_APPS_ALLOWED counts as allowed (scripts and tests); Settings › Safety says so.
        return {"permissionMode": self.mode, "appAccess": self.apps.access, "blockedApps": sorted(self.apps.blocked),
                "outsideBlockedApps": sorted(self.apps.outside_blocked), "envAllowed": sorted(self.apps.allowed)}


def from_env(env=None):
    """The settings in ``env`` (the process environment by default)."""
    env = os.environ if env is None else env
    mode = (env.get(ENV_MODE) or "").strip()
    if mode not in MODES:
        mode = DONT_ASK if (env.get(ENV_LEGACY) or "").strip() == "0" else DEFAULT_MODE
    access = (env.get(ENV_ACCESS) or "").strip()
    access = "ask" if access == "only" else access  # the older "only apps I choose": approved one by one now
    return Settings(mode=mode, apps=AppPolicy(access=access if access in ACCESS else DEFAULT_ACCESS,
                                              allowed=frozenset(_ids(env.get(ENV_ALLOWED))),
                                              blocked=frozenset(_ids(env.get(ENV_BLOCKED))),
                                              outside_blocked=frozenset(_ids(env.get(ENV_OUTSIDE_BLOCKED)))))


_file_cache = {}


def from_file(path=None):
    """The settings in the Mac app's agent.env, for a process that doesn't load it (``mobster mcp``)."""
    if path is None:
        from ..paths import user_data_dir
        path = user_data_dir() / "agent.env"
    path = Path(path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return from_env()
    cached = _file_cache.get(str(path))
    if cached and cached[0] == stamp:
        values = cached[1]
    else:
        values = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                key, sep, rest = line.strip().removeprefix("export ").partition("=")
                if sep and key.strip() in ENV_KEYS:
                    rest = rest.strip()
                    if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "'\"":
                        rest = rest[1:-1]
                    values[key.strip()] = rest
        except (OSError, UnicodeDecodeError):
            values = {}
        _file_cache[str(path)] = (stamp, values)
    merged = {**values, **{k: os.environ[k] for k in ENV_KEYS if k in os.environ}}
    return from_env(merged)


def changes_for(body, current):
    """The env lines a Settings › Safety change saves: {ENV: value}."""
    allowed = {"permissionMode", "appAccess", "blockedApps", "block", "unblock", "outsideOn", "outsideOff"}
    if not isinstance(body, dict) or not body or set(body) - allowed:
        raise ValueError("Send permissionMode, blockedApps, block/unblock for one app, allow/revoke for one app, or "
                         "outsideOn/outsideOff for one app")
    saved = {}
    blocked_apps = list(current.apps.blocked)
    if "permissionMode" in body:
        saved[ENV_MODE] = validate_mode(body["permissionMode"])
        # The older switch follows, so the terminal and an older app read the same choice.
        saved[ENV_LEGACY] = "0" if body["permissionMode"] == DONT_ASK else "1"
    if "appAccess" in body:
        # Every app allowed without asking (MOBSTER_APP_ACCESS=all) is for scripts and tests, never a setting.
        if body["appAccess"] != "ask":
            raise ValueError("appAccess can only be ask here; MOBSTER_APP_ACCESS=all is for scripts and tests")
        saved[ENV_ACCESS] = body["appAccess"]
    if "blockedApps" in body:
        blocked_apps = validate_ids(body["blockedApps"], "blockedApps")
    for key in ("block", "unblock"):
        if key in body:
            bundle = validate_ids([body[key]], key)[0]
            blocked_apps = [b for b in blocked_apps if b != bundle] + ([bundle] if key == "block" else [])
    if "outsideOn" in body or "outsideOff" in body:
        outside = list(current.apps.outside_blocked)
        for key in ("outsideOn", "outsideOff"):
            if key in body:
                bundle = validate_ids([body[key]], key)[0]
                outside = [b for b in outside if b != bundle] + ([bundle] if key == "outsideOff" else [])
        if len(outside) > MAX_LISTED:
            raise ValueError(f"outsideOff allows at most {MAX_LISTED} app bundle ids; remove one before adding another")
        saved[ENV_OUTSIDE_BLOCKED] = ",".join(outside)
    if "blockedApps" in body or "block" in body or "unblock" in body:
        if len(blocked_apps) > MAX_LISTED:
            raise ValueError(
                f"blockedApps allows at most {MAX_LISTED} app bundle ids; remove one before adding another")
        saved[ENV_BLOCKED] = ",".join(blocked_apps)
    return saved


def loosens(current, requested):
    """Whether a per-task ``requested`` mode asks less than the saved one (needs the Mac app's session)."""
    return requested in MODES and STRICTNESS[requested] < STRICTNESS[current]


def effective_mode(saved, override=None, origin=None):
    """The mode a run uses: the override when given, never below ask_important for an outside agent's task."""
    mode = override if override in MODES else saved if saved in MODES else DEFAULT_MODE
    if origin in OUTSIDE_ORIGINS and STRICTNESS[mode] < STRICTNESS[ASK_IMPORTANT]:
        mode = ASK_IMPORTANT
    return mode
