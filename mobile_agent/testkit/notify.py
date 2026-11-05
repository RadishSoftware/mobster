"""Tell a Slack channel how a `mobster test` suite ended, by the project's rules."""

import json
import os
from pathlib import Path

from ..paths import write_text_atomic
from .reports import counts_text, duration_text

RULES_FILE = "notifications.yaml"
MARKER = "slack-notify.json"
TOUCHED = frozenset({"fail", "always", "fix"})
ROUTE = "/api/tests/notify"
MAX_ROWS = 8
CELL = 160
PENDING = {"failed": "failed", "needs_review": "needs review", "couldnt_run": "couldn't run"}


class RulesError(Exception):
    """The rules file can't be used: one sentence that says which line and what to do."""


class Rules:
    def __init__(self, destination, on):
        self.destination, self.on = destination, on


def load_rules(root):
    """The project's Rules, or None when it has no notifications file. RulesError when the file is wrong."""
    path = Path(root) / ".mobster" / RULES_FILE
    if not path.is_file():
        return None
    name = f".mobster/{RULES_FILE}"
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise RulesError(f"{name} can't be read. Check that it is a text file.") from None
    except yaml.YAMLError:
        raise RulesError(f"{name} isn't valid YAML. Write `slack:` with `destination:` and `on:`.") from None
    slack = data.get("slack") if isinstance(data, dict) else None
    if not isinstance(data, dict) or set(data) - {"slack"} or not isinstance(slack, dict):
        raise RulesError(f"{name} holds a `slack:` section and nothing else.")
    slack = {"on" if key is True else key: value for key, value in slack.items()}   # YAML 1.1 reads `on` as true
    unknown = sorted(str(key) for key in set(slack) - {"destination", "on"})
    if unknown:
        raise RulesError(f"{name}: slack has no `{unknown[0][:40]}`. It takes destination and on.")
    destination = slack.get("destination")
    if not isinstance(destination, str) or not 1 <= len(destination.strip()) <= 120:
        raise RulesError(f"{name}: slack needs `destination:`, the name of a Slack destination from Settings › "
                         "Connectors.")
    on = slack.get("on", "fail")
    on = [on] if isinstance(on, str) else on
    if not isinstance(on, list) or not on or not all(isinstance(item, str) and item in TOUCHED for item in on):
        raise RulesError(f"{name}: `on:` is fail, always or fix, or a list of them.")
    return Rules(destination.strip(), tuple(item for item in ("fail", "always", "fix") if item in on))


def cells(doc):
    """(check, result) for every result that decides the suite: not quarantined."""
    return [(check, result) for check in doc.get("checks") or () for result in check.get("results") or ()
            if isinstance(result, dict) and not result.get("quarantined")]


def summarize(doc):
    """{"green", "unpassed": [...], "healed", "counts", "seconds"} from a suite's results.json document."""
    unpassed, healed = [], 0
    for check, result in cells(doc):
        healed += len(result.get("healed") or ())
        if result.get("status") in PENDING:
            reason = result.get("reason") if isinstance(result.get("reason"), dict) else {}
            unpassed.append({"check": str(check.get("name") or "check"), "status": result["status"],
                             "device": str(result.get("device") or ""),
                             "found": str(reason.get("message") or result.get("summary") or "")})
    tests = _xctest_rows(doc)
    unpassed += tests
    summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else {}
    return {"green": not unpassed, "unpassed": unpassed, "tests": len(tests), "healed": healed,
            "counts": counts_text(summary), "durationMs": doc.get("durationMs")}


def _xctest_rows(doc):
    """A row per failed XCTest case, or one row when the tests couldn't run."""
    from .xctest import failed_cases, failure_line
    found = doc.get("xctest") if isinstance(doc.get("xctest"), dict) else {}
    if found.get("status") == "couldnt_run":
        reason = found.get("reason") or {}
        return [{"check": "XCTest targets", "status": "couldnt_run", "device": "",
                 "found": str(reason.get("message") or "")}]
    return [{"check": f"{target}: {case['classname']}.{case['name']}", "status": "failed",
             "device": str(case.get("device") or ""), "found": failure_line(case)}
            for target, case in failed_cases(doc)]


def wanted(rules, summary, previous_green):
    """Whether the rules ask for a message about this suite."""
    if "always" in rules.on:
        return True
    if "fail" in rules.on and (not summary["green"] or summary["healed"]):
        return True
    return "fix" in rules.on and summary["green"] and previous_green is False


def _cell(text, limit=CELL):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def report(summary, *, project, link=None, folder=None, fixed=False):
    """The message as {"title", "lines", "link"}: plain text a Slack destination escapes."""
    unpassed = summary["unpassed"]
    if unpassed:
        one = len(unpassed) == 1
        noun = ("check or test" if one else "checks or tests") if summary.get("tests") else \
            "check" if one else "checks"
        title = f"{project}: {len(unpassed)} {noun} didn't pass"
    elif fixed:
        title = f"{project}: the checks pass again"
    else:
        title = f"{project}: the checks passed"
    lines = [summary["counts"] + (f" in {duration_text(summary['durationMs'])}" if summary.get("durationMs") else "")]
    for row in unpassed[:MAX_ROWS]:
        device = f" on {_cell(row['device'], 40)}" if row["device"] else ""
        found = f": {_cell(row['found'])}" if row["found"] else ""
        lines.append(f"{PENDING[row['status']].capitalize()}: {_cell(row['check'], 70)}{device}{found}")
    if len(unpassed) > MAX_ROWS:
        lines.append(f"{len(unpassed) - MAX_ROWS} more in the report.")
    if summary["healed"]:
        count = summary["healed"]
        lines.append(f"Needs review: {count} healed step{'' if count == 1 else 's'}. Run `mobster test heal` to "
                     "read the diffs, then keep or reject them.")
    if link:
        lines.append(f"Report: {link}")
    elif folder:
        lines.append(f"Report: {folder}")
    return {"title": title, "lines": lines, "link": link}


def previous_green(root, folder):
    """Whether the suite before ``folder`` in this project passed (from the Mac app's index), None without one."""
    from .recent import _index, _read
    known = _read(_index())
    root = str(Path(root).resolve())
    for entry in known if isinstance(known, list) else ():
        if isinstance(entry, dict) and entry.get("root") == root and entry.get("folder") != str(Path(folder).resolve()):
            return entry.get("exitCode") == 0
    return None


class NotSent(Exception):
    """The request surely never left this Mac: Mobster isn't running, or refused it before it posted."""


def ask_mobster(destination, message):
    from ..threads.client import Client, NoService, ServiceError
    try:
        client = Client.connect(origin="cli")
    except (NoService, ServiceError) as error:
        raise NotSent(str(error)) from None
    try:
        return client.call("POST", ROUTE, {"destination": destination, "report": message}, timeout=30)
    except ServiceError as error:
        if error.status < 500 or error.status == 503 and error.code == "unavailable":
            raise NotSent(str(error)) from None
        raise


def notify(root, folder, doc, *, link=None, ask=ask_mobster, previous=previous_green):
    """Post the suite's message when the project's rules ask for one; never raises or changes the suite's result."""
    try:
        rules = load_rules(root)
        if rules is None:
            return None
        summary = summarize(doc)
        before = previous(root, folder)
        if not wanted(rules, summary, before):
            return None
        message = report(summary, project=Path(root).resolve().name, link=link, folder=folder,
                         fixed=summary["green"] and before is False)
        # Written before the request leaves, so a post that may have arrived is never made again, by any command.
        marker = Path(folder) / MARKER
        try:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return "mobster test: Slack was already told about this suite, so Mobster doesn't post it again."
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"destination": rules.destination, "state": "sending"}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            answer = ask(rules.destination, message)
        except NotSent as error:
            marker.unlink(missing_ok=True)      # refused before anything left this Mac
            return f"mobster test: Slack wasn't told. {error}"
        except Exception:                       # noqa: BLE001 -- no answer: it may have posted, so it stays marked
            return ("mobster test: Slack may have been told, and Mobster doesn't post it again. Check "
                    f"{rules.destination} before you run the suite again.")
        ok = isinstance(answer, dict) and answer.get("ok") is True
        unsent = isinstance(answer, dict) and answer.get("unsent") is True
        text = answer.get("message") if isinstance(answer, dict) and isinstance(answer.get("message"), str) else ""
        if not ok and unsent:
            marker.unlink(missing_ok=True)
            return f"mobster test: Slack wasn't told. {text}".rstrip()
        write_text_atomic(marker, json.dumps({"destination": rules.destination, "state": "sent" if ok else "unknown"})
                          + "\n")
        if ok:
            return f"mobster test: told {rules.destination} on Slack."
        return (f"mobster test: Slack may not have the message, and Mobster doesn't post it again. {text}").rstrip()
    except RulesError as error:
        return f"mobster test: Slack wasn't told. {error}"
    except OSError:
        return "mobster test: Slack wasn't told: the suite folder can't be written."
