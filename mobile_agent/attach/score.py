"""Rate an app for automation from the accessibility tree it publishes: good, partial or poor, with reasons."""

import json
import time
from dataclasses import asdict, dataclass

from ..paths import user_data_dir, write_text_atomic
from . import flutter, web

ACTIONABLE = frozenset({"Button", "Cell", "Link", "TextField", "SearchField", "TextView", "Switch", "Slider", "Tab",
                        "Key", "SegmentedControl", "PickerWheel", "Stepper", "Toggle"})
OPAQUE_NODES = 3
GOOD_LABELLED = .85
PARTIAL_LABELLED = .6
MIN_DENSITY = 8        # shown nodes below this on a full screen leave the agent little to act on
RATINGS = ("good", "partial", "poor")


@dataclass(frozen=True)
class AppScore:
    bundle_id: str
    rating: str
    reasons: tuple
    nodes: int
    actionable: int
    labelled_share: float | None
    webview: bool
    flutter: bool
    scored_at: float

    def public(self):
        return {**asdict(self), "reasons": list(self.reasons)}


def score_tree(tree, bundle_id=None, now=None):
    """Score one screen. The caller decides which screen speaks for the app (its first screen after launch)."""
    shown = tree.shown()
    actionable = [node for node in shown if node.role in ACTIONABLE]
    labelled = [node for node in actionable if (node.label or node.identifier or node.value).strip()]
    share = len(labelled) / len(actionable) if actionable else None
    view = web.web_view(tree)
    has_web = view is not None
    has_flutter = flutter.is_flutter(tree)
    reasons, rating = [], "good"

    def lower(to, why):
        nonlocal rating
        reasons.append(why)
        if RATINGS.index(to) > RATINGS.index(rating):
            rating = to

    if len(shown) <= OPAQUE_NODES:
        lower("poor", f"the tree has {len(shown)} elements: the screen is drawn, not published")
    elif len(shown) < MIN_DENSITY:
        lower("partial", f"the tree has {len(shown)} elements, too few to act on with confidence")
    if share is None:
        if len(shown) > OPAQUE_NODES:
            lower("partial", "no buttons, fields or links are exposed")
    elif share < PARTIAL_LABELLED:
        lower("poor", f"{len(labelled)} of {len(actionable)} controls have a label ({share:.0%})")
    elif share < GOOD_LABELLED:
        lower("partial", f"{len(labelled)} of {len(actionable)} controls have a label ({share:.0%})")
    if has_web:
        inside = sum(1 for node in shown if web.inside(node, view)) - 1
        lower("partial", f"a WebView holds the content ({inside} accessibility elements inside it); "
                         "only its published accessibility elements are scored")
    if has_flutter:
        lower("partial", "Flutter draws its own widgets; its semantics tree is the only thing the agent can read")
    if not reasons:
        reasons.append(f"{len(labelled)} of {len(actionable)} controls are labelled across {len(shown)} elements")
    return AppScore(bundle_id or tree.bundle_id, rating, tuple(reasons), len(shown), len(actionable),
                    None if share is None else round(share, 3), has_web, has_flutter,
                    time.time() if now is None else now)


def score_apps(read_tree, launch, bundles, settle=lambda: None):
    """Launch each bundle, read its tree and score it. ``launch(bundle)`` and ``read_tree()`` are the driver's.

    An app that will not open or read is recorded as poor with the reason, so one bad app never stops the survey.
    """
    scores = []
    for bundle in bundles:
        try:
            launch(bundle)
            settle()
            tree = read_tree()
        except Exception as error:
            scores.append(AppScore(bundle, "poor", (f"could not be read ({type(error).__name__})",), 0, 0, None,
                                   False, False, time.time()))
            continue
        scores.append(score_tree(tree, bundle))
    return scores


def store_path():
    return user_data_dir() / "attach" / "app-scores.json"


def save(scores, path=None):
    """Merge ``scores`` into the stored file by bundle ID (newest wins) and return the path."""
    path = path or store_path()
    merged = {row["bundle_id"]: row for row in load(path)}
    merged.update({score.bundle_id: score.public() for score in scores})
    return write_text_atomic(path, json.dumps(sorted(merged.values(), key=lambda row: row["bundle_id"]), indent=1))


def load(path=None):
    try:
        rows = json.loads((path or store_path()).read_text())
    except (OSError, ValueError):
        return []
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("bundle_id"), str)] \
        if isinstance(rows, list) else []


def report(scores):
    """Plain lines: bundle, rating and the first reason."""
    width = max((len(score.bundle_id) for score in scores), default=0)
    return "\n".join(f"{score.bundle_id:{width}}  {score.rating:7}  {score.reasons[0]}" for score in scores)
