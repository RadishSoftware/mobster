"""Independent oracles for the benchmark, shared by every agent under test.

Same rule as ``evals/oracles.py``: a check never trusts the agent's own claim
of success, evidence or citations. It compares (a) the agent's final answer
with ground truth fixed before the run, and/or (b) the device state read by
the harness's own probe after the run.

Checks work on the agent-neutral run record (``agents.base.AgentRun`` as a
dict), so Mobster and the screenshot baselines are graded by the same code.
Citation checks are deliberately absent: only Mobster produces citations, and
grading a property one agent cannot have would bias the comparison.
"""

from dataclasses import dataclass, field
import re
import unicodedata

from ..evals.oracles import ProbeUnavailable, navigation_title, screen_text


class MissingTruth(Exception):
    """Ground truth for a check was not recorded, so the attempt is ungraded (never passed)."""


# ---------------------------------------------------------------- references

@dataclass(frozen=True)
class Literal:
    """Ground truth written into the task itself (stable public facts)."""
    value: object


@dataclass(frozen=True)
class Truth:
    """A per-device value recorded by ``bench capture-truth`` before any run."""
    key: str


@dataclass(frozen=True)
class Captured:
    """A value the probe reads from the start screen during this attempt's reset."""
    key: str


@dataclass(frozen=True)
class Fixture:
    """A value derived from the frozen fixture manifest (``fixtures.py``)."""
    key: str
    part: str


def resolve(ref, ctx):
    if isinstance(ref, Literal):
        return ref.value
    if isinstance(ref, Truth):
        value = ctx.truth.get(ref.key)
        if value is None:
            raise MissingTruth(f"truth {ref.key!r} not recorded; run capture-truth")
        return value
    if isinstance(ref, Captured):
        value = (ctx.captured or {}).get(ref.key)
        if value is None:
            raise MissingTruth(f"start-screen value {ref.key!r} was not captured at reset")
        return value
    if isinstance(ref, Fixture):
        return ctx.fixtures.value(ref.key, ref.part)
    return ref


# ---------------------------------------------------------------- normalizers

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                         "–": "-", "—": "-", " ": " ", " ": " "})


def norm_text(value):
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).translate(_QUOTES)
    text = re.sub(r"\s+", " ", text).strip().strip(".;,").strip()
    return text.casefold()


ON = {"on", "true", "yes", "enabled", "1", "active", "connected"}
OFF = {"off", "false", "no", "disabled", "0", "inactive", "not connected"}


def norm_bool(value):
    if isinstance(value, bool):
        return "on" if value else "off"
    text = norm_text(value)
    if text in ON:
        return "on"
    if text in OFF:
        return "off"
    # "Bluetooth is on", "It is turned off": one polarity word and not the other.
    words = set(re.findall(r"[a-z]+", text))
    has_on = bool(words & {"on", "enabled", "yes"})
    has_off = bool(words & {"off", "disabled", "no", "not"})
    if has_on != has_off:
        return "on" if has_on else "off"
    return None


def norm_number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = re.search(r"-?\d[\d,   ]*(\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return float(re.sub(r"[,   ]", "", match.group(0)))
    except ValueError:
        return None


def _same_number(got, expected):
    """Equal at the expected value's own precision: 8,848.86 answers "8849" (the page's
    exact figure for a rounded truth); 8,848 does not. Same rule for every agent."""
    want = norm_number(expected)
    if want is None:
        return False
    match = re.search(r"\.(\d+)", str(expected))
    places = len(match.group(1)) if match and not isinstance(expected, (int, float)) else 0
    if isinstance(expected, float):
        places = len(repr(expected).split(".")[1].rstrip("0")) if "." in repr(expected) else 0
    return got == want or round(got, places) == want


def norm_digits(value):
    return re.sub(r"\D", "", str(value or ""))


def norm_filename(value):
    """Files may hide extensions; a user may quote the name. Compare stems."""
    text = norm_text(value).strip("'\"")
    return re.sub(r"\.(jpe?g|png|heic)$", "", text)


def matches(actual, expected, mode="text", alternatives=()):
    """True when ``actual`` equals ``expected`` under ``mode``. Never partial credit."""
    candidates = (*(expected if isinstance(expected, (list, tuple)) else (expected,)), *alternatives)
    if actual is None:
        return False
    if mode == "text":
        return norm_text(actual) in {norm_text(c) for c in candidates}
    if mode == "regex":
        return any(re.fullmatch(c, norm_text(actual), re.I) for c in candidates)
    if mode == "bool":
        got = norm_bool(actual)
        return got is not None and got in {norm_bool(c) for c in candidates}
    if mode == "number":
        got = norm_number(actual)
        return got is not None and any(_same_number(got, c) for c in candidates)
    if mode == "digits":
        got = norm_digits(actual)
        return bool(got) and got in {norm_digits(c) for c in candidates}
    raise ValueError(f"unknown match mode {mode!r}")


def answer_field(run, path):
    value = run.get("answer")
    for key in [part for part in path.split("/") if part]:
        if isinstance(value, dict):
            # Field names are fixed by the schema; tolerate case only.
            hit = next((k for k in value if isinstance(k, str) and k.casefold() == key.casefold()), None)
            value = value.get(hit) if hit is not None else None
        else:
            return None
    return value


# ---------------------------------------------------------------- checks

@dataclass(frozen=True)
class Check:
    """``evaluate`` returns (ok, detail, extra). ok=None means ungraded."""

    def evaluate(self, run, ctx):
        raise NotImplementedError

    @property
    def name(self):
        return type(self).__name__


@dataclass(frozen=True)
class Claimed(Check):
    """The agent itself reported success (a navigation reached but disowned is not done)."""

    def evaluate(self, run, ctx):
        ok = run.get("status") == "completed"
        return ok, f"status={run.get('status')!r}", {}


@dataclass(frozen=True)
class AnswerIs(Check):
    """The answer field equals ground truth fixed before the run."""
    field: str
    expect: object
    mode: str = "text"
    alternatives: tuple = ()

    def evaluate(self, run, ctx):
        expected = resolve(self.expect, ctx)
        actual = answer_field(run, self.field)
        ok = matches(actual, expected, self.mode, self.alternatives)
        return ok, f"{self.field}={actual!r} expected={expected!r} ({self.mode})", {}


@dataclass(frozen=True)
class Abstains(Check):
    """The fact is not on the device/page: the only correct output is an explicit no-answer."""

    def evaluate(self, run, ctx):
        ok = bool(run.get("abstained")) and not _has_value(run.get("answer"))
        return ok, f"abstained={run.get('abstained')!r} answer={run.get('answer')!r}", {"abstention_correct": ok}


def _has_value(answer):
    if answer is None:
        return False
    if isinstance(answer, dict):
        return any(_has_value(v) for v in answer.values())
    if isinstance(answer, (list, tuple)):
        return any(_has_value(v) for v in answer)
    return str(answer).strip() != ""


@dataclass(frozen=True)
class InApp(Check):
    """The expected app is frontmost, read by the probe."""
    bundle_id: str

    def evaluate(self, run, ctx):
        snapshot = ctx.probe.observe(self.bundle_id)
        ok = snapshot.bundle_id == self.bundle_id
        return ok, f"foreground={snapshot.bundle_id!r}", {}


@dataclass(frozen=True)
class OnScreen(Check):
    """The final screen's navigation title, read by the probe."""
    bundle_id: str
    expect: object

    def evaluate(self, run, ctx):
        expected = resolve(self.expect, ctx)
        title = navigation_title(ctx.probe.observe(self.bundle_id))
        ok = norm_text(title) == norm_text(expected)
        return ok, f"title={title!r} expected={expected!r}", {}


@dataclass(frozen=True)
class Shows(Check):
    """The final screen contains text (used where no title identifies the screen)."""
    bundle_id: str
    text: str

    def evaluate(self, run, ctx):
        observed = norm_text(screen_text(ctx.probe.observe(self.bundle_id)))
        ok = norm_text(self.text) in observed
        return ok, f"{'found' if ok else 'missing'} {self.text!r}", {}


@dataclass(frozen=True)
class ActedAtLeast(Check):
    """Answering from the start screen when the task requires reaching another is not the task."""
    count: int

    def evaluate(self, run, ctx):
        actions = run.get("action_count")
        ok = isinstance(actions, int) and actions >= self.count
        return ok, f"actions={actions} required>={self.count}", {}


@dataclass(frozen=True)
class ItemSet(Check):
    """Exact set of items meeting a visual predicate (dry run: nothing is acted on).

    Pass requires the exact set. Precision/recall are recorded per attempt so
    the report can show item-level quality, not only task pass/fail.
    """
    field: str
    fixture: str

    def evaluate(self, run, ctx):
        expected = {norm_filename(x) for x in ctx.fixtures.value(self.fixture, "positives")}
        universe = {norm_filename(x) for x in ctx.fixtures.value(self.fixture, "files")}
        raw = answer_field(run, self.field)
        if raw is None or not isinstance(raw, (list, tuple)):
            return False, f"{self.field}={raw!r} is not a list", {"items": {"tp": 0, "fp": 0, "fn": len(expected)}}
        got = {norm_filename(x) for x in raw if isinstance(x, str)}
        unknown = got - universe
        tp, fp, fn = len(got & expected), len(got - expected), len(expected - got)
        ok = got == expected
        return ok, (f"tp={tp} fp={fp} fn={fn}" + (f" unknown={sorted(unknown)[:3]}" if unknown else "")), {
            "items": {"tp": tp, "fp": fp, "fn": fn, "unknown": len(unknown)}}


@dataclass(frozen=True)
class ItemLabels(Check):
    """Per-item label (e.g. eye colour) with 'unsure' as the correct answer where it is.

    Every fixture item carries an accept set; items whose true label is
    'unsure' measure abstention: answering a colour there is a wrong answer.
    """
    field: str
    fixture: str
    item_key: str = "file"
    label_key: str = "label"

    def evaluate(self, run, ctx):
        accept = {norm_filename(k): {norm_text(a) for a in v}
                  for k, v in ctx.fixtures.value(self.fixture, "accept").items()}
        truth = {norm_filename(k): norm_text(v) for k, v in ctx.fixtures.value(self.fixture, "labels").items()}
        raw = answer_field(run, self.field)
        answers = {}
        if isinstance(raw, list):
            for row in raw:
                if isinstance(row, dict):
                    answers[norm_filename(row.get(self.item_key))] = norm_text(row.get(self.label_key))
        correct = sum(1 for item, ok in accept.items() if answers.get(item) in ok)
        should_abstain = [item for item, label in truth.items() if label == "unsure"]
        abstained_right = sum(1 for item in should_abstain if answers.get(item) == "unsure")
        wrongly_abstained = sum(1 for item, label in truth.items()
                                if label != "unsure" and answers.get(item) == "unsure"
                                and "unsure" not in accept[item])
        ok = correct == len(accept)
        return ok, f"{correct}/{len(accept)} items acceptable", {
            "labels": {"correct": correct, "total": len(accept),
                       "abstain_expected": len(should_abstain), "abstain_correct": abstained_right,
                       "abstain_wrong": wrongly_abstained}}


@dataclass(frozen=True)
class FixtureIntact(Check):
    """After the run, the harness re-reads the fixture: a read-only task left it unchanged."""
    fixture: str

    def evaluate(self, run, ctx):
        if ctx.fixture_verifier is None:
            return None, "no fixture verifier (offline)", {}
        ok, detail = ctx.fixture_verifier(self.fixture)
        return ok, detail, {}


@dataclass
class CheckContext:
    probe: object = None
    truth: dict = field(default_factory=dict)
    captured: dict = field(default_factory=dict)
    fixtures: object = None
    fixture_verifier: object = None


def grade(checks, run, ctx):
    """Every check runs so one failure does not hide another. Unsafe actions always fail."""
    results = []
    for check in checks:
        try:
            ok, detail, extra = check.evaluate(run, ctx)
        except MissingTruth as error:
            ok, detail, extra = None, f"ungraded: {error}", {}
        except ProbeUnavailable as error:
            ok, detail, extra = None, f"ungraded: probe unavailable: {error}", {}
        except Exception as error:  # a broken check must not pass anything
            ok, detail, extra = False, f"{type(error).__name__}: {error}", {}
        results.append({"check": check.name, "ok": ok, "detail": detail, **({"extra": extra} if extra else {})})
    unsafe = run.get("unsafe") or []
    results.append({"check": "NoUnsafeAction", "ok": not unsafe,
                    "detail": f"{len(unsafe)} unsafe attempt(s)" if unsafe else "none"})
    graded = [r["ok"] for r in results]
    if any(ok is False for ok in graded):
        verdict = "fail"
    elif any(ok is None for ok in graded):
        verdict = "ungraded"
    else:
        verdict = "pass"
    return verdict, results
