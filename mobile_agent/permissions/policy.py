"""The user's permission settings: the mode, the per-task override, and which apps Mobster may use.

Saved in the agent's env file beside the other settings (Settings › Safety writes them through POST
/api/permissions):

- ``MOBSTER_PERMISSION_MODE``: ask_all | ask_important (default) | dont_ask. Unset, the older
  ``MOBSTER_ASK_BEFORE_ACTING=0`` reads as dont_ask, so nobody's choice changes under them.
- ``MOBSTER_APP_ACCESS``: ask (default) | all (scripts and tests only: POST /api/permissions never sets it). With ask, apps are approved one by one, as the person decides (the
  owner, 10 Oct): the first time a run needs an app it asks "Allow Mobster to use Things?", Allow is remembered in
  the "Don't ask again" store (``permission-exceptions.json``, ``APP_OP`` entries), and Settings › Safety › Apps
  lists the approved apps, each switched off anytime (then it asks again). Every app is treated the same: there is no
  list of apps that start off. ``all`` counts every app as approved (scripts, and the tests).
- ``MOBSTER_APPS_ALLOWED``: apps approved in the env file, as if allowed on the card (scripts and tests only).
- ``MOBSTER_BLOCKED_APPS`` (the existing never-open list): apps never opened, without asking.
- ``MOBSTER_OUTSIDE_BLOCKED_APPS``: apps the user turned off for agents outside Mobster (an MCP client, Mobster
  Remote). Otherwise those agents use the same apps as everyone, always asking before acting (``effective_mode``):
  nothing is refused for being a kind of app; what an outside agent may open is the user's choice.

The modes work like a coding harness's. An app the person allowed is the boundary, as a granted folder is: in it,
reading and moving around never ask. ask_important (the default) asks before anything with an effect outside the
phone; ask_all also before a change that stays on it; dont_ask is full access and doesn't ask. Typing a one-time code
asks in every mode. The Home Screen is always allowed.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re

ASK_ALL, ASK_IMPORTANT, DONT_ASK = "ask_all", "ask_important", "dont_ask"
MODES = (ASK_ALL, ASK_IMPORTANT, DONT_ASK)
DEFAULT_MODE = ASK_IMPORTANT
# Strictest first: an override may always move left; moving right needs the Mac app.
STRICTNESS = {ASK_ALL: 2, ASK_IMPORTANT: 1, DONT_ASK: 0}
MODE_LABELS = {ASK_ALL: "Ask before every change", ASK_IMPORTANT: "Ask before acting", DONT_ASK: "Full access"}
# Origins where an outside agent started the task: never below ask_important (Remote's design, rule 1).
OUTSIDE_ORIGINS = frozenset({"mcp", "remote"})

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

# Bank, payment, brokerage, crypto, Wallet and password apps by bundle id: "Don't ask again" is never offered or used
# in them (rules.money_context). Never whether Mobster may open them: every app asks the first time alike.
SENSITIVE_BUNDLES = {
    "com.apple.Passbook": "wallet", "com.apple.Passwords": "passwords", "com.apple.PassKit": "wallet",
    "com.chase": "finance", "com.bankofamerica.BofA": "finance", "com.bofa.mobile": "finance",
    "com.wellsfargo.net.mobilebanking": "finance", "com.capitalone.enterprisemobilebanking": "finance",
    "com.citigroup.citimobile": "finance", "com.usbank.mobilebanking": "finance", "com.ally.MobileBanking": "finance",
    "com.discoverfinancial.mobile": "finance", "com.americanexpress.amelia": "finance",
    "com.americanexpress.amexservice": "finance", "com.schwab.mobile": "finance", "com.fidelity.iphone": "finance",
    "com.vanguard.ios": "finance", "com.etrade.mobilepro.iphone": "finance", "com.robinhood.release.Robinhood": "finance",
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

    def allows(self, bundle, name=None, outside=False):
        """Whether Mobster may use ``bundle`` at all (``outside``: for an agent outside Mobster): False only for the
        never-open list and, for outside agents, the apps turned off for them. An app not approved yet is allowed
        here: it asks the first time (``approved``). None or the Home Screen: always."""
        if not bundle or bundle == SPRINGBOARD:
            return True
        return not (bundle in self.blocked or outside and bundle in self.outside_blocked)

    def approved(self, bundle, grants=None):
        """Whether ``bundle`` opens without asking: the Home Screen, ``access`` all, the env's allow list, or an
        approval in ``grants`` (the "Don't ask again" store). Never an app on the never-open list."""
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

    def status(self, bundle, name=None, grants=None):
        """{allowed, why, outside} for one app, as Settings › Safety and the Apps view show it: allowed is approved;
        why says what happens otherwise ("asks" the first time, or "blocked")."""
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
        return (f"Mobster didn't open {name}: it needs your OK the first time it uses an app. Choose Allow when it "
                f"asks, or switch {name} on in Apps, in Mobster's sidebar.")


ALLOW_ALL = AppPolicy(access="all")


def app_card_reason(count=1):
    """The line under "Allow Mobster to use Things?": what allowing an app means (at most 120 characters)."""
    if count == 1:
        return "Mobster then reads and moves around in it without asking. Turn it off anytime in Settings › Safety."
    return ("Mobster then reads and moves around in them without asking. Turn any of them off anytime in "
            "Settings › Safety.")


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
    """The settings in the Mac app's agent.env, for a process that doesn't load it (``mobster mcp``): read again
    whenever the file changes. The process environment wins for a key it sets, as everywhere else."""
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
    """The env lines a Settings › Safety change saves: {ENV: value}. ValueError for anything else. (Approving an app
    or taking it back is the store's: register.post_permissions.)"""
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
        # Every app allowed without asking is for scripts and tests (MOBSTER_APP_ACCESS=all in the env file), never a
        # choice the app's settings make.
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
        saved[ENV_OUTSIDE_BLOCKED] = ",".join(outside[:MAX_LISTED])
    if "blockedApps" in body or "block" in body or "unblock" in body:
        saved[ENV_BLOCKED] = ",".join(blocked_apps[:MAX_LISTED])
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
