"""Record-to-check: a finished Mobster task (from the Mac app or `mobster serve`) as a check file.

- steps: the task's steps in plain words (the run's ``step`` events), turned from what was done into what to do
  ("Tapped General" → "Tap General"); a task with more steps than a check holds uses its request as one step;
- expect: what the task proved (its proof quotes) as ``text`` assertions, plus the navigation bar it proved them on
  (Mobster's agent only: Quick mode's proof names a path of taps, not a screen's title);
- app: the app the task started in, else the first app it opened.

The check names no device, so it runs on a simulator by default, whichever device the task ran on. Every string goes
through secret_filter.redact, and typed text the agent masked stays masked. Social and dating apps
(harness_api.UNATTENDED_DENY) are refused: checks are for your own apps.

The journal is read without a lock or a write (SQLite read-only), so it works while the app is open.
"""

import json
from pathlib import Path
import re
import sqlite3
import time

RUN_ID = re.compile(r"[a-f0-9]{12}")
STEP_LIMIT, STEP_TEXT_LIMIT, EXPECT_LIMIT, QUOTE_LIMIT = 20, 500, 8, 120
ANY_APP = "any"
FAST = "fast"         # engines.FAST: Quick mode
BREADCRUMB = " › "    # fast_proof.path_at joins a Quick proof's screen path with it
HEADER = ("# A Mobster check, recorded from the task “{goal}” on {date}.\n"
          "# Run it with: mobster test {path}\n")
NO_EXPECT = ("# The task proved nothing Mobster can check, so add expect: what must be true at the end, such as\n"
             "# - text: Choose your plan\n")
MASKED = "# A step types text Mobster masked (••••••): replace it with the text to type, or a test account's.\n"
# What was done → what to do. Longest first where one prefix starts another.
IMPERATIVE = (("Pressed and held ", "Press and hold "), ("Pressed Return", "Press Return"),
              ("Opened ", "Open "), ("Tapped ", "Tap "), ("Typed ", "Type "), ("Entered ", "Enter "),
              ("Cleared ", "Clear "), ("Searched for ", "Search for "), ("Rewrote ", "Rewrite "),
              ("Wrote ", "Write "), ("Scrolled ", "Scroll "), ("Swiped ", "Swipe "),
              ("Closed a pop-up", "Close the pop-up"), ("Went to ", "Go to "), ("Sent ", "Send "),
              ("Chose ", "Choose "), ("Turned ", "Turn "), ("Selected ", "Select "))


class RecordError(Exception):
    """Why a task can't become a check: one sentence, an HTTP status and a code."""

    def __init__(self, message, status=400, code="cant_record"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def imperative(text):
    text = " ".join(str(text or "").split())
    for done, do in IMPERATIVE:
        if text.startswith(done):
            text = do + text[len(done):]
            break
    return text.replace(" and pressed Return", " and press Return")


def _redact(text):
    from ..secret_filter import redact
    return redact(str(text or ""))


def _app(record):
    """(bundle, name) the check runs: the task's start app, else the first app it opened. A run names its app by
    the app list's id ("settings") and keeps the bundle ID in ``app.bundleId``."""
    app = record.get("app") if isinstance(record.get("app"), dict) else {}
    start = record.get("appId") or app.get("id")
    bundle = app.get("bundleId") or (start if isinstance(start, str) and "." in start else None)
    if bundle and start != ANY_APP:
        return bundle, app.get("name") or record.get("appName")
    for event in record.get("events") or ():
        if event.get("event") == "frontier_action" and event.get("operation") == "LAUNCH_APP" \
                and isinstance(event.get("target_label"), str) and "." in event["target_label"]:
            return event["target_label"].strip(), None
    return None, None


def _steps(record, app_name):
    texts = []
    for event in record.get("events") or ():
        if event.get("event") != "step" or not isinstance(event.get("text"), str):
            continue
        text = _redact(imperative(event["text"]))[:STEP_TEXT_LIMIT]
        if text.startswith("Read ") and ":" in text:
            text = text.split(":", 1)[0]  # the value it read is an expectation, not a step
        if text and (not texts or texts[-1] != text):
            texts.append(text)
    if texts and app_name and texts[0].casefold() == f"open {app_name}".casefold():
        texts = texts[1:]  # the check launches its app itself
    goal = _redact(" ".join(str(record.get("goal") or "").split()))[:STEP_TEXT_LIMIT]
    if len(texts) > STEP_LIMIT or (not texts and goal):
        return [goal] if goal else []
    return texts


def _expect(record):
    from ..agent_hooks import MASK
    summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
    proof = [item for item in summary.get("proof") or () if isinstance(item, dict)]
    if not proof:
        proof = [{"quote": e.get("quote"), "screen": e.get("screen")} for e in record.get("events") or ()
                 if e.get("event") == "receipt"]
    out, seen, screen = [], set(), None
    for item in proof:
        quote = " ".join(str(item.get("quote") or "").split())
        if item.get("screen"):
            screen = " ".join(str(item["screen"]).split())
        if not quote or MASK in quote or quote.startswith("/"):
            continue
        quote = _redact(quote)
        if MASK in quote:
            continue
        if len(quote) > QUOTE_LIMIT:
            quote = quote[:QUOTE_LIMIT].rsplit(" ", 1)[0] or quote[:QUOTE_LIMIT]
        if quote.casefold() in seen:
            continue
        seen.add(quote.casefold())
        out.append({"text": quote})
        if len(out) >= EXPECT_LIMIT:
            break
    # Smart proves on a screen's title (its navigation bar); Quick's proof names a breadcrumb of the taps that led
    # there ("General › About"), which no navigation bar shows, so a Quick task's check ends on no bar.
    if screen and record.get("engine") != FAST and BREADCRUMB not in screen and MASK not in screen \
            and not screen.startswith("/"):
        out.append({"visible": {"role": "navbar", "id": _redact(screen)[:200]}, "name": f"Ends on {screen}"[:120]})
    return out


def check_data(record, *, name=None):
    """The check file's shape (verify.checks) for a run ``record`` (Run.public() or a journal row with its events).
    Raises RecordError."""
    from ..harness_api import unattended_allowed
    if record.get("finishedAt") is None or record.get("status") in ("queued", "running"):
        raise RecordError("The task is still running. Record it once it ends.", 409, "run_active")
    bundle, app_name = _app(record)
    if not bundle:
        raise RecordError("The task didn't open an app, so there's nothing to check.", 400, "no_app")
    if not unattended_allowed(bundle):
        raise RecordError("Checks are for your own apps, so Mobster doesn't make them from tasks in social or "
                          "dating apps.", 400, "not_unattended")
    steps = _steps(record, app_name)
    title = " ".join(str(name or "").split()) or " ".join(str(record.get("goal") or "").split()) or "Recorded check"
    title = _redact(title)
    data = {"version": 1, "name": title[:119] + "…" if len(title) > 120 else title, "app": {"bundle": bundle}}
    if steps:
        data["steps"] = steps
    data["expect"] = _expect(record)
    return data


def check_yaml(record, *, name=None, path=".mobster/checks/<name>.yaml"):
    """The check file's text for ``record``: a header, then YAML that verify.checks.load_check reads back."""
    import yaml
    from ..agent_hooks import MASK
    from ..verify.checks import check_from_dict
    data = check_data(record, name=name)
    check_from_dict(data)  # the same validation a check file gets: never hand out one that can't load
    created = record.get("finishedAt") or record.get("createdAt")
    date = time.strftime("%-d %b %Y", time.localtime(created / 1000)) if isinstance(created, (int, float)) \
        else time.strftime("%-d %b %Y")
    goal = " ".join(_redact(record.get("goal") or "the task").split())
    text = HEADER.format(goal=goal[:80] + ("…" if len(goal) > 80 else ""), date=date, path=path)
    if any(MASK in step for step in data.get("steps") or ()):
        text += MASKED
    if not data["expect"]:
        text += NO_EXPECT
    return text + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100)


# -- the journal ---------------------------------------------------------------------------------------------------

def journal_paths():
    """Where the Mac app and `mobster serve` keep their task journals, in the order they are tried."""
    from ..paths import PACKAGE, source_checkout, user_data_dir
    paths = [user_data_dir() / "state" / "mobster.sqlite3"]
    if source_checkout():
        paths.append(PACKAGE / ".state" / "mobster.sqlite3")
    return paths


def find_run(run_id, paths=None):
    """The run ``run_id`` from the first journal that has it (metadata plus ``events``). Raises RecordError."""
    run_id = str(run_id or "").strip().lower()
    if not RUN_ID.fullmatch(run_id):
        raise RecordError("A task's id is 12 characters of 0-9 and a-f, as the Mobster app and "
                          "`mobster history` show it.", 400, "bad_run_id")
    searched = []
    for path in paths if paths is not None else journal_paths():
        path = Path(path)
        if not path.is_file():
            continue
        searched.append(path)
        record = _read(path, run_id)
        if record is not None:
            return record
    if not searched:
        raise RecordError("Mobster found no task history on this Mac. Run a task in the Mobster app or with "
                          "`mobster serve` first.", 404, "no_history")
    raise RecordError(f"No task {run_id} in Mobster's history. The Mobster app keeps the last 100 tasks.", 404,
                      "run_not_found")


def _read(path, run_id):
    for uri in (f"file:{path}?mode=ro", f"file:{path}?immutable=1"):
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=3)
        except sqlite3.Error:
            continue
        try:
            row = connection.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None
            record = json.loads(row[0])
            record["events"] = [json.loads(item[0]) for item in connection.execute(
                "SELECT data FROM events WHERE run_id=? ORDER BY seq", (run_id,))]
            return record
        except (sqlite3.Error, ValueError, TypeError):
            continue
        finally:
            connection.close()
    raise RecordError(f"Mobster couldn't read its task history at {path}.", 503, "journal_unreadable")
