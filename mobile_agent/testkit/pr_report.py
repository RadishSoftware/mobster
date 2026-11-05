"""What `mobster test pr` hands back. A draft runs as quarantined: it is reported and never fails the PR."""

import json
import re
import shlex
import shutil
from pathlib import Path

from .. import failure_line, links, traces
from ..paths import write_text_atomic
from ..verify import perf
from ..verify.report import dots
from .xctest import failed_cases, words
from .xctest import failure_line as case_failure  # not main's failure_line module above

SCHEMA = "mobster.test-pr/1"
MARKER = "<!-- mobster:test-pr -->"
DRAFT_REASON = "drafted for this PR"
MAX_GIFS = 6
MAX_ROWS = 40
MAX_REPROS = 5
MAX_REPRO_STEPS = 20
TAIL_LINES = 6                  # the failing step's last log lines the comment quotes
TAIL_REQUESTS = 5
COMMENT_LIMIT = 60_000          # GitHub refuses a comment over 65,536 characters
WORDS = {"passed": "Passed", "failed": "Failed", "needs_review": "Needs review", "couldnt_run": "Couldn't run"}


def slug(text, limit=48):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")[:limit] or "check"


def is_draft(check):
    return check.get("quarantineReason") == DRAFT_REASON


def counted(doc):
    """(check, result) for every result that decides the PR: not a draft, not quarantined."""
    return [(check, result) for check in doc.get("checks") or () for result in check.get("results") or ()
            if not result.get("quarantined")]


def counts(doc):
    cells = counted(doc)
    out = {key: sum(result.get("status") == key for _, result in cells)
           for key in ("passed", "failed", "needs_review", "couldnt_run")}
    out["flaky"] = sum(bool(result.get("flaky")) for _, result in cells)
    out["drafts"] = sum(1 for check in doc.get("checks") or () if is_draft(check))
    return out


def headline(doc):
    """"Mobster checks: 11 passed, 1 failed, XCTest 3 of 4 passed"."""
    found = doc.get("xctest")
    unit = [words(found)] if found else []
    if found and not counted(doc):
        return "Mobster checks: " + ", ".join(["no saved check covers this PR"] + unit)
    tally = counts(doc)
    parts = [f"{tally['passed']} passed"]
    for key, word in (("failed", "failed"), ("needs_review", "need review"), ("couldnt_run", "couldn't run")):
        if tally[key]:
            parts.append(f"{tally[key]} {'needs review' if key == 'needs_review' and tally[key] == 1 else word}")
    if tally["flaky"]:
        parts.append(f"{tally['flaky']} flaky")
    if not counted(doc):
        return "Mobster checks: no saved check covers this PR"
    return "Mobster checks: " + ", ".join(parts + unit)


def _run_result(result):
    """verify's result.json for the run that decided this cell, or {}."""
    run_dir = result.get("runDir")
    run_id = result.get("runId")
    if not run_dir and run_id:
        run_dir = next((run.get("runDir") for run in result.get("runs") or ()
                        if run.get("runId") == run_id), None)
    if not run_dir and not run_id and "runDir" not in result:
        run_dir = next((run.get("runDir") for run in reversed(result.get("runs") or ())
                        if run.get("runDir")), None)
    if not run_dir:
        return {}, None
    try:
        return json.loads((Path(run_dir) / "result.json").read_text(encoding="utf-8")), Path(run_dir)
    except (OSError, ValueError):
        return {}, Path(run_dir)


def copy_frames(doc, folder):
    """Copy each result's verdict frame into pr/frames/. Returns {(check index, result index): relative path}."""
    out = {}
    for c, check in enumerate(doc.get("checks") or ()):
        for r, result in enumerate(check.get("results") or ()):
            source = result.get("frame")
            if not source or not Path(source).is_file():
                continue
            name = f"{c + 1:03d}-{r + 1:02d}-{slug(check.get('name'))}-{slug(result.get('device'), 24)}.jpg"
            target = Path(folder) / "pr" / "frames" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(source, target)
            except OSError:
                continue
            out[(c, r)] = target.relative_to(folder).as_posix()
    return out


def gif_order(doc):
    """Which results get a GIF, most useful first: failures, then drafts, then the rest."""
    def rank(item):
        _, check, result = item
        status = result.get("status")
        return (0 if status in ("failed", "needs_review") and not is_draft(check) else 1 if is_draft(check) else
                2 if status == "couldnt_run" else 3)
    items = [((c, r), check, result) for c, check in enumerate(doc.get("checks") or ())
             for r, result in enumerate(check.get("results") or ())]
    return sorted(items, key=rank)


def failing_step(result, run):
    """The index of the step a failure belongs to, from the run's proof, else None."""
    from ..verify.proof import failing_entry
    entry = failing_entry(run.get("step_proof") or result.get("stepProof"), (run.get("evidence") or {}).get("step"))
    return entry.get("index") if entry else None


def failing_proof(result):
    """What the PR comment says of the step a run failed on: its words, the requests and the app's last log lines."""
    from ..verify.proof import failing_entry
    proof = result.get("stepProof") or {}
    entry = failing_entry(proof, (result.get("evidence") or {}).get("step"))
    if entry is None:
        return None
    text = entry.get("text")
    if text:
        # A typed step's words are masked as the repro's steps are (_steps_taken); when the run's own steps can't be
        # read, they are masked anyway.
        from ..run_gif import mask_quotes
        run, _ = _run_result(result)
        steps = [step for step in run.get("steps") or () if isinstance(step, dict)]
        typed = next((step.get("typed") for step in steps if step.get("index") == entry.get("index")), None)
        if typed or not steps:
            text = mask_quotes(text)
    return {"index": entry.get("index"), "text": text,
            "requests": ((entry.get("network") or {}).get("rows") or [])[-TAIL_REQUESTS:],
            "log": ((entry.get("log") or {}).get("lines") or [])[-TAIL_LINES:]}


def beats_for(check, result, run, run_dir):
    """The clip of one check run: its name, each step's screen with the step's words, then the verdict on the frame that
    outlines the expectations."""
    from ..run_gif import Beat, mask_quotes
    beats = [Beat("title", check.get("name") or "Check")]
    steps = [step for step in run.get("steps") or () if isinstance(step, dict) and step.get("frame")]
    failing = failing_step(result, run) if result.get("status") != "passed" else None
    if failing is not None and len(steps) > 2:
        # A failed run's clip is the step it failed on and the one before it, not the whole check.
        steps = [step for step in steps if step.get("index") in (failing - 1, failing)]
    frames = [frame for frame in run.get("frames") or () if isinstance(frame, str)]
    if steps:
        for step in steps:
            text = " ".join(str(step.get("text") or step.get("op") or "Step").split())
            beats.append(Beat("step", mask_quotes(text) if step.get("typed") else text, str(run_dir / step["frame"])))
    else:
        launch = next((frame for frame in frames if "verdict" not in frame), None)
        if launch:
            beats.append(Beat("step", "Opened the app", str(run_dir / launch)))
    shown = [beat for beat in beats if beat.kind == "step"]
    for number, beat in enumerate(shown, 1):
        beat.n, beat.total = number, len(shown)
    verdict_frame = next((frame for frame in reversed(frames) if frame.endswith("-verdict-ax.jpg")), None) or \
        next((frame for frame in reversed(frames) if frame.endswith("-verdict.jpg")), None)
    status = result.get("status") or "couldnt_run"
    word = WORDS.get(status, status)
    reason = (result.get("reason") or {}).get("message") or run.get("summary") or ""
    caption = word if status == "passed" else f"{word}: {reason}" if reason else word
    held = sum(1 for item in result.get("assertions") or () if item.get("ok"))
    total = len(result.get("assertions") or ())
    note = f"{held} of {total} expectations held" if total else word
    end = Beat("end", " ".join(caption.split())[:200], str(run_dir / verdict_frame) if verdict_frame else
               (shown[-1].frame_id if shown else None), note=note)
    end.card = {"done": status == "passed"}
    return beats + [end]


def check_gif(check, result, *, theme="paper"):
    """GIF bytes for one result, or None when its run left no screens or no clip fits."""
    from ..run_gif import render_clip
    run, run_dir = _run_result(result)
    if not run or run_dir is None:
        return None

    def read(frame_id):
        try:
            return Path(frame_id).read_bytes()
        except (OSError, TypeError):
            return None
    try:
        return render_clip(beats_for(check, result, run, run_dir), read, theme=theme, fonts=None,
                           attempts=((0.75, 128, False), (0.6, 64, False)), max_bytes=9_500_000).gif
    except ValueError:
        return None


def make_gifs(doc, folder, *, limit=MAX_GIFS, build=check_gif):
    """Write pr/gifs/*.gif. Returns {(check index, result index): relative path}."""
    out = {}
    for key, check, result in gif_order(doc):
        if len(out) >= limit:
            break
        try:
            data = build(check, result)
        except Exception:  # a clip is a nicety: a run's verdict never depends on it
            data = None
        if not data:
            continue
        c, r = key
        name = f"{c + 1:03d}-{r + 1:02d}-{slug(check.get('name'))}-{slug(result.get('device'), 24)}.gif"
        target = Path(folder) / "pr" / "gifs" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        out[key] = target.relative_to(folder).as_posix()
    return out


def _expected_found(result):
    """(what it expected, what it found) in a few words."""
    assertions = [item for item in result.get("assertions") or () if isinstance(item, dict)]
    failed = next((item for item in assertions if not item.get("ok")), None)
    status = result.get("status")
    if failed is not None and status != "passed":
        return failed.get("text") or "an expectation", failed.get("observed") or "something else"
    reason = (result.get("reason") or {}).get("message")
    if status == "failed" and reason:
        return "", reason
    if status in ("couldnt_run", "needs_review") and reason:
        return (f"{len(assertions)} expectations" if assertions else "a verdict"), reason
    if assertions:
        return (assertions[0].get("text") if len(assertions) == 1 else f"{len(assertions)} expectations"), \
            ("as expected" if status == "passed" else "not confirmed")
    return "the app to launch", "as expected" if status == "passed" else (reason or WORDS.get(status, status))


def _steps_taken(result):
    from ..run_gif import mask_quotes
    run, run_dir = _run_result(result)
    check = run.get("check") if isinstance(run.get("check"), dict) else {}
    out, times = [], []
    for step in run.get("steps") or ():
        if not isinstance(step, dict):
            continue
        text = " ".join(str(step.get("text") or step.get("op") or "").split())
        if text:
            out.append(mask_quotes(text) if step.get("typed") else text)
            times.append(step.get("at_ms") if isinstance(step.get("at_ms"), (int, float)) else None)
    if not out:
        out = [" ".join(str(step).split()) for step in check.get("steps") or () if str(step).strip()]
        times = [None] * len(out)
    return out[:MAX_REPRO_STEPS], times[:MAX_REPRO_STEPS], _launch_url(run_dir)


def _clock(ms):
    seconds = max(0, int(ms // 1000))
    return f"{seconds // 60}:{seconds % 60:02d}"


def why(row):
    """The failure in one plain sentence: after which step, what was expected, and what the screen showed."""
    status = row.get("status")
    steps = row.get("steps") or []
    after = f"After its last step, {steps[-1]}" if steps else "Right after launch"
    if status == "couldnt_run":
        return f"The check couldn't run: {row.get('found')}"
    if status == "needs_review":
        return f"{after}, Mobster couldn't decide: {row.get('found')}"
    return f"{after}, {row.get('expected')} was {row.get('found')}" if row.get("expected") else \
        f"{after}, the check failed: {row.get('found')}"


def _launch_url(run_dir):
    """The deep link the run's check opened (launch.url in the run's check.yaml), or None."""
    if run_dir is None:
        return None
    try:
        import yaml
        data = yaml.safe_load((Path(run_dir) / "check.yaml").read_text(encoding="utf-8"))
    except Exception:
        return None
    launch = data.get("launch") if isinstance(data, dict) else None
    url = launch.get("url") if isinstance(launch, dict) else None
    return url if isinstance(url, str) and url else None


def _video(result, folder):
    """The result's screen recording, relative to the suite folder, when it's there."""
    video = result.get("video")
    if not video:
        return None
    path = Path(video) if Path(video).is_absolute() else Path(folder) / video
    if not path.is_file():
        return None
    try:
        return path.resolve().relative_to(Path(folder).resolve()).as_posix()
    except ValueError:
        return None


def repro_command(row):
    """The command that runs the row's check again on the same device: `mobster test FILE --sim TYPE@RUNTIME`, or
    `--device NAME` on an iPhone."""
    file = row.get("file")
    if not file:
        return None
    argv = ["mobster", "test", str(file)]
    key = str(row.get("deviceKey") or "")
    if key.startswith("sim:"):
        argv += ["--sim", key[4:]]
    elif key.startswith("device:"):
        argv += ["--device", str(row.get("device") or key[7:]).split(" · ")[0]]
    return " ".join(shlex.quote(item) for item in argv)


def document(plan_public, doc, *, folder, frames, gifs, built=None, head=None, tested_with=False, notes=()):
    """pr.json's content."""
    rows = []
    for c, check in enumerate(doc.get("checks") or ()):
        for r, result in enumerate(check.get("results") or ()):
            expected, found = _expected_found(result)
            row = {"check": check.get("name"), "file": check.get("file"), "device": result.get("device"),
                   "deviceKey": result.get("deviceKey"), "status": result.get("status"),
                   "flaky": bool(result.get("flaky")), "draft": is_draft(check), "expected": expected,
                   "found": found, "frame": frames.get((c, r)), "gif": gifs.get((c, r)),
                   "video": _video(result, folder), "report": result.get("report"),
                   "healed": [item for item in (result.get("healed") if isinstance(result.get("healed"), list)
                                                else ()) if isinstance(item, dict)],
                   "replayed": bool(result.get("replayed")),
                   "perf": perf.line(perf.numbers(result.get("perf")), (result.get("history") or {}).get("perf"))}
            if result.get("status") != "passed":
                row["cause"] = (result.get("stepProof") or {}).get("cause")
                row["failingStep"] = failing_proof(result)
                row["steps"], times, row["openUrl"] = _steps_taken(result)
                offset = result.get("videoOffsetMs")
                if row["video"] and isinstance(offset, (int, float)):
                    # Where each step starts in the video: the run's clock less the moment recording began.
                    row["videoAt"] = [_clock(at - offset) if isinstance(at, (int, float)) else None for at in times]
                row["why"] = why(row)
                if traces.enabled():
                    failure = failure_line.of_check(_run_result(result)[0])
                    if failure is not None:
                        row["failure"] = failure["line"]
                row["repro"] = repro_command(row)
            rows.append(row)
    out = {"schema": SCHEMA, "suite": doc.get("suite"), "headline": headline(doc), "exitCode": doc.get("exitCode"),
           "counts": counts(doc), "rows": rows, "plan": plan_public, "build": built, "head": head,
            "testedWith": bool(tested_with), "notes": list(notes), "costUsd": (doc.get("summary") or {}).get("costUsd")}
    if doc.get("xctest"):
        out["xctest"] = xctest_public(doc["xctest"], failed_cases(doc))
    return out


def xctest_public(found, failed):
    """pr.json's XCTest part: the targets that ran and why, and one entry per failed test."""
    return {"status": found.get("status"), "scheme": found.get("scheme"), "headline": words(found),
            "reason": found.get("reason"),
            "targets": [{"name": target["name"], "tests": target["tests"], "failed": target["failed"],
                         "why": (found.get("why") or {}).get(target["name"])}
                        for target in found.get("targets") or ()],
            "failures": [{"target": target, "test": f"{case['classname']}.{case['name']}",
                          "message": case_failure(case), "file": (case["failures"][0] or {}).get("file"),
                          "line": (case["failures"][0] or {}).get("line")} for target, case in failed]}


def _cell(text, limit=90):
    text = " ".join(str(text if text is not None else "").split())
    text = text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    return text.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;") or " "


def _link(path, artifact, file_url=None):
    if not path:
        return "–"
    name = _cell(Path(path).name, 60)
    url = file_url(path) if file_url else artifact
    return f"[{name}]({url})" if url else f"`{name}`"


def _repro(row, artifact_url, file_url):
    """One failed check's repro, folded: device, deep link, steps, expected and found, video, the command."""
    status = WORDS.get(row.get("status"), row.get("status"))
    cause = (row.get("cause") or {}).get("line")
    lines = [f"<details><summary>Repro: {_cell(row.get('check'), 70)} ({status.lower()}"
             + (f", {_cell(cause, 70)}" if cause else "") + ")</summary>", ""]
    numbered = [f"Use {_cell(dots(row.get('device') or 'the simulator'), 60)}."]
    numbered.append(f"Open `{_cell(row['openUrl'], 120)}`." if row.get("openUrl") else "Launch the app.")
    at = row.get("videoAt") or []
    numbered += [_cell(step, 160) + (f" (video {at[index]})" if index < len(at) and at[index] else "")
                 for index, step in enumerate(row.get("steps") or ())]
    lines += [f"{number}. {text}" for number, text in enumerate(numbered, 1)]
    lines += ["", f"**Why:** {_cell(row.get('why') or why(row), 400).rstrip('.')}."]
    if row.get("failure"):
        lines += ["", f"**Failure:** {_cell(row['failure'], 300)}"]
    lines += _step_proof(row)
    media = [f"{label}: {_link(row[key], artifact_url, file_url)}" for key, label in
             (("video", "Video"), ("gif", "GIF"), ("frame", "Frame")) if row.get(key)]
    if media:
        lines += ["", ", ".join(media)]
    if row.get("repro"):
        lines += ["", "Run it again:", "", "```sh", row["repro"], "```"]
    lines += ["", "</details>", ""]
    return lines


def _step_proof(row):
    """The cause line and the failing step's requests and last log lines, for the repro."""
    cause, step = row.get("cause") or {}, row.get("failingStep") or {}
    lines = []
    if cause.get("line"):
        side = {"app": "App problem", "check": "Check problem", "environment": "Environment problem"}.get(
            cause.get("side"), "Cause")
        lines += ["", f"**{side}:** {_cell(cause['line'], 200)}."]
    if step.get("requests"):
        lines += ["", f"Requests in step {step.get('index')}:", ""] + [
            "- " + _cell(" ".join(str(item[key]) for key in ("method", "host", "status", "durationMs") if item.get(key)
                                  is not None), 100) for item in step["requests"]]
    if step.get("log"):
        lines += ["", f"The app's log in step {step.get('index')} ({_cell(step.get('text'), 80)}):", "", "```"]
        lines += [f"{(item.get('atMs') or 0) / 1000:9.3f}  {item.get('level') or '':<6} "
                  f"{' '.join(str(item.get('text') or '').split())[:160].replace('```', chr(39) * 3)}"
                  for item in step["log"]]
        lines += ["```"]
    return lines


def _xctest_lines(found):
    """The PR comment's XCTest part: the targets that ran and why, then one line per failed test with its file and
    line."""
    if not found:
        return []
    lines = [f"**{_cell(found.get('headline'), 80)}**", ""]
    if found.get("status") == "couldnt_run":
        reason = found.get("reason") or {}
        return lines + [f"> {_cell(reason.get('message'), 300)} {_cell(reason.get('fix'), 200)}".rstrip(), ""]
    for target in found.get("targets") or ():
        count = f"{target['tests']} test{'' if target['tests'] == 1 else 's'}"
        why = f": {_cell(target['why'], 140)}" if target.get("why") else ""
        lines.append(f"- Ran `{_cell(target['name'], 40)}` ({count}){why}")
    failures = found.get("failures") or ()
    for failure in failures[:MAX_ROWS]:
        lines.append(f"- `{_cell(failure['test'], 90)}` failed: {_cell(failure['message'], 200)}")
    if len(failures) > MAX_ROWS:
        lines.append(f"- {len(failures) - MAX_ROWS} more in the report.")
    return lines + [""]


def comment(pr, *, artifact_url=None, tested_with=None, file_url=None):
    """The PR comment's Markdown."""
    tested_with = pr.get("testedWith", False) if tested_with is None else tested_with
    plan = pr.get("plan") or {}
    lines = [MARKER, f"### {_cell(pr.get('headline'), 120)}", ""]
    screens = [screen.get("name") for screen in plan.get("screens") or ()]
    facts = []
    if screens:
        shown = ", ".join(f"`{_cell(name, 40)}`" for name in screens[:6])
        facts.append(f"Changed screens: {shown}" + (f" and {len(screens) - 6} more" if len(screens) > 6 else ""))
    elif plan.get("fallback") == "smoke":
        facts.append("No screen changed, so this ran the checks tagged `smoke`")
    if pr.get("head"):
        facts.append(f"at `{str(pr['head'])[:7]}`")
    if facts:
        lines += [", ".join(facts), ""]
    rows = [row for row in pr.get("rows") or () if not row.get("draft")]
    order = {"failed": 0, "needs_review": 1, "couldnt_run": 2, "passed": 3}
    rows.sort(key=lambda row: (order.get(row.get("status"), 4), not row.get("flaky")))
    if rows:
        lines += ["| Check | Device | Expected | Found | Frame |", "| --- | --- | --- | --- | --- |"]
        for row in rows[:MAX_ROWS]:
            word = WORDS.get(row.get("status"), row.get("status"))
            if row.get("flaky") and row.get("status") == "passed":
                word = "Flaky"
            device = _cell(dots(row.get("device") or ""), 40) + (", replayed" if row.get("replayed")
                                                                                      else "")
            lines.append(f"| **{word}** {_cell(row.get('check'), 70)} | {device} | {_cell(row.get('expected'))} "
                         f"| {_cell(row.get('found'))} | {_link(row.get('frame'), artifact_url, file_url)} |")
        if len(rows) > MAX_ROWS:
            lines.append(f"\n{len(rows) - MAX_ROWS} more in the report.")
        lines.append("")
    measured = [row for row in rows if row.get("perf")]
    if measured:
        lines.append("Measured (↑ is 20% over the last 10 passes):")
        lines += [f"- {_cell(row.get('check'), 60)} on {_cell(dots(row.get('device') or ''), 40)}: "
                  f"{_cell(row['perf'], 120)}" for row in measured[:MAX_ROWS]]
        lines.append("")
    lines += _xctest_lines(pr.get("xctest"))
    failing = next((row for row in rows if row.get("status") != "passed" and row.get("gif")), None)
    if failing:
        if file_url:
            lines += [f"GIF of the failure: {_link(failing['gif'], None, file_url)}.", ""]
        else:
            where = f"[in the report]({artifact_url})" if artifact_url else "in the suite folder"
            lines += [f"GIF of the failure: `{_cell(Path(failing['gif']).name, 60)}`, {where}.", ""]
    for row in [row for row in rows if row.get("status") != "passed"][:MAX_REPROS]:
        lines += _repro(row, artifact_url, file_url)
    healed = [(row, item) for row in rows for item in row.get("healed") or ()]
    if healed:
        lines.append(f"**{len(healed)} step{'' if len(healed) == 1 else 's'} healed.** Review the diff before you "
                     "accept it:")
        for row, item in healed[:10]:
            lines.append(f"- {_cell(row.get('check'), 60)}, step {_cell(item.get('step'), 4)}: "
                         f"{_cell(item.get('from'), 50)} → {_cell(item.get('to'), 50)}")
        lines.append("")
    drafts = [row for row in pr.get("rows") or () if row.get("draft")]
    if drafts:
        lines.append(f"**No check covered {len(drafts)} changed screen{'' if len(drafts) == 1 else 's'}.** Mobster "
                     "drafted one for each. Drafts never fail the PR: move one into `.mobster/checks/` to keep it.")
        for row in drafts[:MAX_ROWS]:
            word = WORDS.get(row.get("status"), row.get("status")).lower()
            lines.append(f"- `{_cell(row.get('file'), 80)}`: {word} on {_cell(row.get('device'), 40)}"
                         + (f" ({_cell(row.get('found'), 80)})" if row.get("status") != "passed" else ""))
        lines.append("")
    elif plan.get("drafts"):
        written = plan["drafts"]
        lines.append(f"**No check covered {len(written)} changed screen{'' if len(written) == 1 else 's'}.** Mobster "
                     "drafted one for each, without running it: move one into `.mobster/checks/` to keep it.")
        lines += [f"- `{_cell(item.get('file'), 80)}`" for item in written[:MAX_ROWS]]
        lines.append("")
    for note in pr.get("notes") or ():
        lines.append(f"> {_cell(note, 300)}")
    if pr.get("notes"):
        lines.append("")
    if artifact_url:
        lines.append(f"[Report, frames and GIFs]({artifact_url})")
    if tested_with:
        lines.append(("" if not artifact_url else "\n") + f"<sub>Tested with [Mobster]({links.MADE_WITH_PR})</sub>")
    text = "\n".join(lines).rstrip() + "\n"
    if len(text) > COMMENT_LIMIT:
        text = text[:COMMENT_LIMIT - 40].rsplit("\n", 1)[0] + "\n\n(Cut short: the report has the rest.)\n"
    return text


def hosted_pages(link):
    try:
        from ..teams import report_seal
    except ModuleNotFoundError as error:
        if error.name != "mobile_agent.teams":
            raise
        raise ValueError("Hosted report links need the Mobster app's team extension") from None
    report_id, key, _ = report_seal.parse_report_link(link)

    def page(path):
        try:
            return report_seal.report_link(report_id, key, path)
        except ValueError:
            return link
    return page


def write(pr, folder):
    """pr.json and pr-comment.md (without an artifact link). Returns their paths."""
    folder = Path(folder)
    json_path, md_path = folder / "pr.json", folder / "pr-comment.md"
    for path, text in ((json_path, json.dumps(pr, indent=1, ensure_ascii=False) + "\n"), (md_path, comment(pr))):
        write_text_atomic(path, text)
    return {"prJson": json_path, "comment": md_path}
