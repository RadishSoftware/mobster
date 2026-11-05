"""JUnit pieces beyond one case per check: the synthetic "Suite verdict" case a suite adds when it stopped or failed
with no case to show it, and a snapshot comparison's output lines."""
import xml.etree.ElementTree as ET

from .suite import SAFETY_EXIT_STOPS


def add_if_unrepresented(root, doc, totals, started):
    """Add one errored case unless a case already shows the stop or a failure; ``started`` is the ISO start time."""
    shown = any(r.get("safetyStop") in SAFETY_EXIT_STOPS
                for c in doc.get("checks") or () for r in c.get("results") or ())
    stopped = (doc.get("stopReason") is not None or doc.get("stopped")) and not shown
    if not (stopped or (doc.get("exitCode") and not (totals["failures"] or totals["errors"]))):
        return
    suite = ET.SubElement(root, "testsuite", name="Suite verdict", tests="1", failures="0", errors="1",
                          skipped="0", time="0.000", timestamp=started)
    case = ET.SubElement(suite, "testcase", classname="checks", name="Suite verdict", time="0.000")
    ET.SubElement(case, "error", type="suite_verdict", message="Suite did not pass.")
    totals["tests"] += 1
    totals["errors"] += 1


def snapshot_lines(snapshot, report_base, rebase):
    """The screenshot comparison as JUnit output: the status, and the baseline, current and diff images a CI summary can
    attach. ``rebase`` points an image path at the report folder."""
    if not isinstance(snapshot, dict) or not snapshot.get("status"):
        return []
    lines = [f"Snapshot {snapshot.get('name')}: {snapshot['status']}, {snapshot.get('changedPercent', 0):g} % changed "
             f"(limit {snapshot.get('thresholdPercent'):g} %)"]
    for key, label in (("diff", "Snapshot diff"), ("baseline", "Snapshot baseline"), ("current", "Snapshot current")):
        if snapshot.get(key):
            lines.append(f"{label}: {rebase(snapshot[key], report_base)}")
    return lines
