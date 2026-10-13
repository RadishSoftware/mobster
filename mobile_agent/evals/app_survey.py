"""How much of each app's screen the accessibility tree actually exposes.

An accessibility-first agent is only as good as the tree an app publishes, and
that depends on how the app is built: UIKit/SwiftUI publish nearly everything,
React Native maps to native views, Flutter publishes a separate semantics tree,
web views go through WebKit, and games or canvases often publish nothing.

For each app this launches it (no taps, typing or scrolling), reads the tree the
agent would read, takes one screenshot, recognizes its text with on-device
Apple Vision, and reports how much of the visible text the tree also carries.

Privacy: everything runs locally. Screenshots and recognized text stay in
memory; no model is called; the report holds counts and ratios only.

    python -m mobile_agent.evals.app_survey --wda-url http://127.0.0.1:8100 \
        --bundles com.apple.Preferences com.spotify.client --out survey.json
"""

import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET

from ..compose import build_target_driver
from ..drivers import WDA_SOURCE_PATH
from ..paths import build_dir

OCR = build_dir() / "ocr"
ACTIONABLE = {"Button", "Cell", "Link", "TextField", "SearchField", "TextView", "Switch", "Slider",
              "Tab", "Key", "SegmentedControl", "PickerWheel", "Stepper", "Toggle"}
# OCR lines worth scoring: confident and long enough to be real text.
MIN_CONFIDENCE, MIN_CHARS = .5, 3
SETTLE_SECONDS = 3.0


def normalize(text):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.casefold())).strip()


def text_recall(lines, corpus):
    """Share of visible text lines the tree also carries (substring or 80% of words)."""
    corpus_text = normalize(" ".join(corpus))
    words = set(corpus_text.split())
    scored = covered = 0
    for line in lines:
        norm = normalize(line)
        if len(norm) < MIN_CHARS:
            continue
        scored += 1
        tokens = norm.split()
        if norm in corpus_text or (tokens and sum(t in words for t in tokens) / len(tokens) >= .8):
            covered += 1
    return (covered / scored if scored else None), scored


def raw_metrics(xml):
    """Counts from the raw tree, including what the agent's parser drops."""
    root = ET.fromstring(xml)
    nodes = list(root.iter())
    roles = [node.tag.removeprefix("XCUIElementType") for node in nodes]
    unlabeled = sum(1 for node, role in zip(nodes, roles)
                    if role in ACTIONABLE and not (node.attrib.get("label") or node.attrib.get("value")))
    return {"nodes": len(nodes), "webviews": roles.count("WebView"),
            "actionable_raw": sum(role in ACTIONABLE for role in roles), "unlabeled_actionable": unlabeled}


# The status bar (clock, signal, battery) belongs to SpringBoard, not the app.
STATUS_BAR_BAND = .06


def ocr_lines(png):
    completed = subprocess.run([str(OCR)], input=png, capture_output=True, timeout=30)
    if completed.returncode:
        return []
    return [row["text"] for row in json.loads(completed.stdout)
            if row.get("confidence", 0) >= MIN_CONFIDENCE and isinstance(row.get("text"), str)
            and row.get("rect", [0, 1])[1] >= STATUS_BAR_BAND]


def classify(metrics):
    recall, elements, lines = metrics.get("text_recall"), metrics.get("elements", 0), metrics.get("ocr_lines", 0)
    if metrics.get("foreground") != metrics["bundle_id"]:
        return "not_foreground"
    if elements <= 3 and lines >= 5:
        return "opaque"          # drawn surface: games, canvases, video
    if recall is None:
        return "no_text"
    if recall >= .85:
        return "good"
    if recall >= .6:
        return "partial"
    return "poor"


def survey_app(driver, bundle_id):
    metrics = {"bundle_id": bundle_id}
    try:
        driver.call("POST", "/wda/apps/activate", {"bundleId": bundle_id}, timeout=20)
        time.sleep(SETTLE_SECONDS)
        driver._stable = None
        started = time.monotonic()
        snapshot = driver.observe_ready(timeout=20)
        metrics["read_ms"] = round((time.monotonic() - started) * 1000)
        metrics["foreground"] = snapshot.bundle_id
        xml = driver.call("GET", WDA_SOURCE_PATH, timeout=30)
        metrics.update(raw_metrics(xml))
        corpus = [f"{e.label} {e.value}" for e in snapshot.elements] + snapshot.text.split("\n")
        metrics["elements"] = len(snapshot.elements)
        metrics["actionable"] = sum(e.role in ACTIONABLE for e in snapshot.elements)
        image = driver.capture_preview(timeout=10)
        lines = ocr_lines(base64.b64decode(image.split(",", 1)[1]))
        metrics["ocr_lines"] = len(lines)
        metrics["text_recall"], metrics["scored_lines"] = text_recall(lines, corpus)
    except Exception as error:
        metrics["error"] = type(error).__name__
    metrics["class"] = classify(metrics) if "error" not in metrics else "error"
    return metrics


def main():
    parser = argparse.ArgumentParser(description="Accessibility coverage across installed apps (local only)")
    parser.add_argument("--wda-url", default="http://127.0.0.1:8100")
    parser.add_argument("--bundles", nargs="+", required=True)
    parser.add_argument("--out", help="Write per-app metrics (numbers only) as JSON")
    args = parser.parse_args()
    with urllib.request.urlopen(args.wda_url + "/status") as response:
        session = json.load(response)["sessionId"]
    driver = build_target_driver(wda_url=args.wda_url, session=session)
    rows = []
    for bundle in args.bundles:
        row = survey_app(driver, bundle)
        rows.append(row)
        recall = row.get("text_recall")
        print(f"{bundle:45} {row['class']:14} elements={row.get('elements', '-'):>4} "
              f"recall={'-' if recall is None else f'{recall:.2f}':>5} lines={row.get('ocr_lines', '-'):>3} "
              f"unlabeled={row.get('unlabeled_actionable', '-'):>3} webviews={row.get('webviews', '-')} "
              f"read={row.get('read_ms', '-')}ms", flush=True)
    driver.call("POST", "/wda/pressButton", {"name": "home"}, timeout=10)
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
