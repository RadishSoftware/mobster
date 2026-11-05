"""What a fact is: its kinds, sources and scopes, the checks every text passes, and the fingerprint that dedupes.

A fact has a kind (what it is), a source (who or what it came from) and a scope (where it applies). Scopes are
"global" (everywhere), "app:<bundle id>", "phone:<id>", "project:<id>", "profile:<id>", "workflow:<id>"
and "profile:<id>/workflow:<id>"; the API also takes "everywhere" for "global". A stored unknown scope
never matches a task.

Never secrets (``secret_filter.is_secret``), whoever writes. Text read from a screen is data, never an instruction, so
only the person (source ``user``) or their profile (``profile``) may write a rule or a preference; what a run, a
check, a lesson or the repo teaches is a path, a place, a structure, a value, an outcome, a device or a cost.
"""

import hashlib
import re

from ..api_errors import APIError
from ..secret_filter import is_secret

FACT_CHARS = 300

KINDS = ("path", "place", "value", "preference", "rule", "structure", "device", "outcome", "cost", "repo")
SOURCES = ("run", "lesson", "check", "repo", "profile", "user")
LEARNED_SOURCES = tuple(source for source in SOURCES if source != "user")
INSTRUCTIONAL = frozenset({"preference", "rule"})        # these read as orders, so only a person's words may be them
INSTRUCTORS = frozenset({"user", "profile"})
SCOPE_KINDS = ("profile_workflow", "profile", "workflow", "project", "app", "phone")
UNPROMPTED = frozenset({"cost"})                         # kept for estimates and the page; never sent to the model

BUNDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+")
SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@~+-]{0,119}")
WORKFLOW_ID = re.compile(r"[0-9a-f]{12}")
REF_KEYS = ("runId", "lessonId", "checkPath", "repo", "profileId")

SECRET = "Mobster doesn't remember passwords, codes or card numbers."


def normalize(text):
    """For spotting the same thing said twice: lowercase, single spaces, no closing punctuation."""
    return " ".join(str(text).lower().split()).rstrip(" .!;,")


def digest(text):
    return hashlib.sha256(normalize(text).encode()).hexdigest()[:32]


def clean_text(text):
    """Whitespace collapsed; APIError 400 for an empty, long or secret text."""
    if not isinstance(text, str):
        raise APIError("Write what Mobster should remember.", 400, "invalid_text")
    text = " ".join(text.split())
    if not text:
        raise APIError("Write what Mobster should remember.", 400, "invalid_text")
    if len(text) > FACT_CHARS:
        raise APIError(f"Keep it to {FACT_CHARS} characters.", 400, "too_long", limit=FACT_CHARS)
    if is_secret(text):
        raise APIError(SECRET, 400, "secret")
    return text


def clean_scope(scope):
    """Canonical scope for a fact. Profile/workflow conjunctions require both admitted identities."""
    if scope in (None, "", "global", "everywhere"):
        return "global"
    if isinstance(scope, str) and len(scope) <= 160 and ":" in scope:
        kind, _, ident = scope.partition(":")
        if kind == "profile" and "/workflow:" in ident:
            profile, _, workflow = ident.partition("/workflow:")
            if SCOPE_ID.fullmatch(profile) and WORKFLOW_ID.fullmatch(workflow):
                return scope
        elif kind == "workflow" and WORKFLOW_ID.fullmatch(ident):
            return scope
        elif kind in ("profile", "project", "app", "phone"):
            if (BUNDLE if kind == "app" else SCOPE_ID).fullmatch(ident):
                return scope
    raise APIError("Choose everywhere, or one app, phone, project, profile or workflow.", 400, "invalid_scope")


def scope_parts(scope):
    """Parse stored scopes without turning unknown or damaged data into global memory."""
    if scope == "global":
        return "global", None
    if not isinstance(scope, str) or not scope:
        return "unknown", str(scope) if scope is not None else None
    try:
        if clean_scope(scope) != scope:
            return "unknown", scope
    except APIError:
        return "unknown", str(scope) if scope is not None else None
    kind, _, ident = scope.partition(":")
    return ("profile_workflow" if kind == "profile" and "/workflow:" in ident else kind), ident


def clean_kind(kind, default="value"):
    kind = default if kind in (None, "") else kind
    if kind not in KINDS:
        raise APIError(f"A fact's kind is one of: {', '.join(KINDS)}.", 400, "invalid_kind")
    return kind


def clean_source(source, kind):
    """The source, checked against the kind: only a person's words may be a rule or a preference."""
    if source not in SOURCES:
        raise APIError(f"A fact's source is one of: {', '.join(SOURCES)}.", 400, "invalid_source")
    if kind in INSTRUCTIONAL and source not in INSTRUCTORS:
        raise APIError("Only you or a profile can set a rule or a preference. Mobster doesn't take orders from what it "
                       "reads on a screen.", 400, "not_an_instruction")
    return source


def clean_ref(ref):
    """Where a fact came from: runId, lessonId, checkPath, repo or profileId, each a short string."""
    if not ref:
        return {}
    if not isinstance(ref, dict):
        raise APIError("A source reference is an object.", 400, "invalid_ref")
    out = {}
    for key, value in ref.items():
        if key not in REF_KEYS or not isinstance(value, str) or not value or len(value) > 300:
            raise APIError(f"A source reference is one of: {', '.join(REF_KEYS)}.", 400, "invalid_ref")
        out[key] = value
    return out


def clean_confidence(value, default=1.0):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise APIError("Confidence is a number from 0 to 1.", 400, "invalid_confidence")
    return float(value)


def clean_key(key):
    if key is None:
        return None
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise APIError("A fact's key is a short string.", 400, "invalid_key")
    return " ".join(key.split()).lower()


def fingerprint(scope, text, key=None):
    """The same fact told twice is one fact. With a ``key`` (the thing a fact is about: "cost:com.x:read reels") a
    changed value updates the fact in place, and forgetting it stops the new value too."""
    basis = f"key\0{key}" if key else f"text\0{normalize(text)}"
    return hashlib.sha256(f"{scope}\0{basis}".encode()).hexdigest()[:32]
