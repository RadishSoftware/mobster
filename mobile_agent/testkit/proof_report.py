"""The step proof in `mobster test`'s reports: the cause line and the failing step's requests and log for JUnit,
and each step's clip, log lines and requests for the HTML report (verify/proof.py builds the proof)."""


def cause_html(proof):
    """The cause line for the HTML report (verify/report.py draws it)."""
    from ..verify.report import cause_line
    return cause_line(proof)


def cause_lines(proof):
    cause = (proof or {}).get("cause")
    return [f"Cause ({cause.get('side')}): {cause.get('line')}"] if isinstance(cause, dict) else []


def step_lines(proof, evidence):
    """The failing step's span for <system-out>: its text, the requests the log shows and the lines the app logged."""
    from ..verify.evidence import log_text
    from ..verify.proof import failing_entry
    entry = failing_entry(proof, (evidence or {}).get("step")) if proof else None
    if entry is None:
        return []
    out = [f"Step {entry.get('index')}: {entry.get('text')} ({entry.get('fromMs', 0) / 1000:.3f} to "
           f"{entry.get('toMs', 0) / 1000:.3f} s)"]
    rows = (entry.get("network") or {}).get("rows") or []
    for row in rows:
        out.append("Request: " + " ".join(str(row[key]) for key in ("method", "host", "status", "durationMs")
                                          if row.get(key) is not None))
    lines = (entry.get("log") or {}).get("lines") or []
    if lines:
        out += [f"The app's log in this step ({len(lines)} of {(entry.get('log') or {}).get('count')} lines):",
                log_text(lines)]
    return out


def proof_steps(result):
    """Each step's clip, log lines and requests, the failing step open."""
    from ..verify.proof import clip, failing_entry
    from ..verify.report import proof_pane
    proof = result.get("stepProof") or {}
    entries = proof.get("steps") or []
    if not entries:
        return ""
    failing = failing_entry(proof, (result.get("evidence") or {}).get("step"))
    video, offset = result.get("video"), result.get("videoOffsetMs")
    panes = [proof_pane(entry, clip=clip(entry, offset), video=video,
                        title=f"Step {entry.get('index')}: {_short(entry.get('text'))}",
                        opened=entry is failing) for entry in entries]
    panes = [pane for pane in panes if pane]
    return '<div class="proof-steps">' + "".join(panes) + "</div>" if panes else ""


def _short(text, limit=80):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
