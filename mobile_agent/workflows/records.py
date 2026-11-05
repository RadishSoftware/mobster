"""A workflow's step cards: the format the card builder saves, its checks, and the journal table its runs live in."""

import copy
import json
import logging
import os
import re
import uuid
from pathlib import Path

from ..journal import encode
from . import starters

log = logging.getLogger("mobster.batch")

MAX_STEPS = 20
MAX_TIMES = 50
MAX_ROWS = 200
MAX_UNTIL = 50
TEXT_CHARS = 2000
CHECK_CHARS = 300
WAIT_RANGE = (5, 86_400)
SPEND_RANGE = (0.05, 100.0)
MINUTES_RANGE = (1, 1440)
DEFAULT_LIMITS = {"maxSpendUsd": 5.0, "maxMinutes": 240}
BUSY_WAIT_S = 20.0
BUSY_POLL_S = 1.0
SETTLE_WAIT_S = 10.0
FIRST_WAIT_S = 10.0
STOP_WAIT_S = 60.0
KEEP = 20
STEP_ID = re.compile(r"[A-Za-z0-9_-]{1,24}")
WORKFLOW_ID = re.compile(r"[0-9a-f]{12}")
KINDS = ("task", "taught", "wait", "send")
REPEATS = ("times", "each", "until")
TARGETS = ("notification", "clipboard", "file", "destination")
OBSERVED_SCHEMA = {"type": "object", "properties": {"observed": {"type": "boolean"}},
                   "required": ["observed"], "additionalProperties": False}
CHECK_GOAL = ("Look at the iPhone and answer one question with true or false: is this true right now? {check}\n"
              "Only look and scroll to answer. Don't send, post, buy, delete or change anything.")

# kind -> (validate(step, runtime) -> cleaned step, run(batch, step, values) -> {"answer", "line"})
_kinds = {}
# kind -> handler(batch, text, step) -> a sentence
_deliveries = {}


def register_step(kind, validate, run):
    if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,23}", kind) or kind in KINDS:
        raise ValueError("A step kind is a new short lowercase name")
    _kinds[kind] = (validate, run)


def register_delivery(kind, handler):
    if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,23}", kind) or kind in TARGETS:
        raise ValueError("A delivery is a new short lowercase name")
    _deliveries[kind] = handler


def step_kinds():
    return list(KINDS) + sorted(_kinds)


def delivery_kinds():
    return list(TARGETS) + sorted(_deliveries)


# ``unknown``: it may have acted and can't tell, so it is never retried; ``retryable``: nothing was done.
class Stop(Exception):
    def __init__(self, status, reason, run_id=None, *, unknown=False, retryable=False):
        super().__init__(reason)
        self.status, self.reason, self.run_id = status, reason, run_id
        self.unknown, self.retryable = unknown, retryable


def one_task(goal, app_id=None):
    return [{"id": "s1", "kind": "task", "text": goal, "appId": app_id}]


def is_plain(steps, repeat=None):
    if repeat:
        return False
    if not isinstance(steps, list):
        return True
    return len(steps) == 1 and steps[0].get("kind") == "task"


def _plain_record(record):
    from ..graph.chain import record_is_plain  # graph/chain.py reads summary_of from here
    return record_is_plain(record)


def summary_of(steps, repeat=None):
    lines = [step["text"] for step in steps if step.get("kind") == "task"]
    if repeat and repeat.get("kind") == "until":
        lines.append(repeat["check"])
    return "\n".join(line for line in lines if line.strip())


def _text(value, limit, what):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"{what} is 1 to {limit:,} characters")
    return value.strip()


def _int(value, low, high, what):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{what} is a whole number from {low:,} to {high:,}")
    return value


def validate_rows(rows, limit=MAX_ROWS):
    if not isinstance(rows, list) or not 1 <= len(rows) <= limit:
        raise ValueError(f"Give 1 to {limit} rows to repeat over")
    out = []
    for row in rows:
        try:
            cleaned = starters.clean_values(row)
        except ValueError as error:
            raise ValueError(f"Row {len(out) + 1}: {error}") from None
        if not cleaned:
            raise ValueError(f"Row {len(out) + 1} is empty")
        out.append({name: value for name, value in (row or {}).items() if starters._key(name) in cleaned})
    return out


def validate(steps, repeat=None, limits=None, *, workflow_id=None, workflows=None, destinations=None, runtime=None):
    if not isinstance(steps, list) or not steps:
        raise ValueError("A workflow has at least one step")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"A workflow has at most {MAX_STEPS} steps")
    seen, cleaned = set(), []
    for step in steps:
        if not isinstance(step, dict) or step.get("kind") not in step_kinds():
            raise ValueError("Each step's kind is " + ", ".join(step_kinds()))
        ident = step.get("id")
        if not isinstance(ident, str) or not STEP_ID.fullmatch(ident) or ident in seen:
            raise ValueError("Each step has its own id: 1 to 24 letters, digits, - or _")
        seen.add(ident)
        kind = step["kind"]
        if kind in _kinds:
            made = _kinds[kind][0](step, runtime)
            if not isinstance(made, dict):
                raise ValueError(f"A {kind} step couldn't be read")
            cleaned.append({**made, "id": ident, "kind": kind})
            continue
        fields = {"task": {"text", "appId"}, "taught": {"workflowId"}, "wait": {"seconds"},
                  "send": {"to", "destination", "what"}}[kind]
        if set(step) - fields - {"id", "kind"}:
            raise ValueError(f"A {kind} step has only {', '.join(sorted(fields))}")
        out = {"id": ident, "kind": kind}
        if kind == "task":
            out["text"] = starters.check_steps(_text(step.get("text"), TEXT_CHARS, "A task's words"))
            app = step.get("appId")
            if app is not None and (not isinstance(app, str) or not 1 <= len(app) <= 160):
                raise ValueError("A task's appId is an app's id, or null for the Home Screen")
            out["appId"] = app
        elif kind == "taught":
            target = step.get("workflowId")
            if not isinstance(target, str) or not WORKFLOW_ID.fullmatch(target):
                raise ValueError("A taught step names a saved workflow by its 12-character id")
            if target == workflow_id:
                raise ValueError("A workflow can't repeat itself")
            if workflows is not None:
                found = workflows.get(target)
                if found is None:
                    raise ValueError("The workflow a step runs was deleted. Choose another one.")
                if not _plain_record(found):
                    raise ValueError(f"“{found.get('name')}” has several steps. A taught step "
                                     "repeats a one-task workflow.")
            out["workflowId"] = target
        elif kind == "wait":
            out["seconds"] = _int(step.get("seconds"), *WAIT_RANGE, "A wait's seconds")
        else:
            target = step.get("to")
            if target not in delivery_kinds():
                raise ValueError("Send to a notification, the clipboard, a file or a saved destination")
            out["to"] = target
            what = step.get("what", "last")
            if what not in ("last", "all"):
                raise ValueError('A send step sends the last answer ("last") or every answer ("all")')
            out["what"] = what
            if target == "destination":
                dest = step.get("destination")
                if not isinstance(dest, str) or not WORKFLOW_ID.fullmatch(dest):
                    raise ValueError("Choose a saved destination")
                if destinations is None:
                    raise ValueError("Sending results isn't available in this Mobster. Choose another place.")
                if not destinations(dest):
                    raise ValueError("That destination was removed. Choose another one.")
                out["destination"] = dest
            elif step.get("destination") is not None:
                raise ValueError("Only a send to a destination names one")
        cleaned.append(out)
    if not any(step["kind"] in ("task", "taught") or step["kind"] in _kinds for step in cleaned):
        raise ValueError("Add a task for the phone")
    if repeat is not None:
        if not isinstance(repeat, dict) or repeat.get("kind") not in REPEATS:
            raise ValueError("Repeat a number of times (times), for each row (each) or until a check (until)")
        kind = repeat["kind"]
        allowed = {"times": {"times"}, "each": {"rows", "max"}, "until": {"check", "max"}}[kind]
        if set(repeat) - allowed - {"kind"}:
            raise ValueError(f"Repeat {kind} has only {', '.join(sorted(allowed))}")
        if kind == "times":
            repeat = {"kind": kind, "times": _int(repeat.get("times"), 2, MAX_TIMES, "Times")}
        elif kind == "each":
            rows = repeat.get("rows")
            rows = validate_rows(rows) if rows is not None else None
            repeat = {"kind": kind, "rows": rows,
                      "max": _int(repeat.get("max", MAX_ROWS if rows is None else len(rows)), 1, MAX_ROWS, "Rows")}
        else:
            repeat = {"kind": kind, "check": starters.check_steps(_text(repeat.get("check"), CHECK_CHARS, "The check")),
                      "max": _int(repeat.get("max", 10), 1, MAX_UNTIL, "The most rounds")}
    limits = limits if limits is not None else {}
    if not isinstance(limits, dict) or set(limits) - set(DEFAULT_LIMITS):
        raise ValueError("A workflow's limits are maxSpendUsd and maxMinutes")
    spend = limits.get("maxSpendUsd", DEFAULT_LIMITS["maxSpendUsd"])
    if isinstance(spend, bool) or not isinstance(spend, (int, float)) or not SPEND_RANGE[0] <= spend <= SPEND_RANGE[1]:
        raise ValueError(f"maxSpendUsd is from ${SPEND_RANGE[0]:.2f} to ${SPEND_RANGE[1]:.0f}")
    minutes = _int(limits.get("maxMinutes", DEFAULT_LIMITS["maxMinutes"]), *MINUTES_RANGE, "maxMinutes")
    summary = summary_of(cleaned, repeat)
    if len(summary) > starters.STEPS_CHARS:
        raise ValueError(f"Keep the steps' words under {starters.STEPS_CHARS:,} characters in all")
    starters.check_steps(summary)
    return cleaned, repeat, {"maxSpendUsd": round(float(spend), 2), "maxMinutes": minutes}


def answer_text(summary):
    summary = summary if isinstance(summary, dict) else {}
    data = summary.get("data")
    if isinstance(data, str) and data.strip():
        return data.strip()
    full = summary.get("full_answer") or summary.get("answer")
    if isinstance(full, str) and full.strip():
        return full.strip()
    if data is not None:
        return json.dumps(data, ensure_ascii=False, indent=2)[:8000]
    return None


class BatchStore:
    TOP = ("id", "workflowId", "key", "status", "createdAt", "finishedAt")

    def __init__(self, journal):
        self.journal = journal
        with journal.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS workflow_batches (
                id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, key TEXT NOT NULL, status TEXT NOT NULL,
                created_at INTEGER NOT NULL, finished_at INTEGER, data TEXT NOT NULL)""")
            connection.execute("CREATE INDEX IF NOT EXISTS workflow_batch_owner ON "
                               "workflow_batches(workflow_id, created_at)")
            connection.execute("""CREATE TABLE IF NOT EXISTS workflow_nodes (
                batch_id TEXT NOT NULL, instance TEXT NOT NULL, seq INTEGER NOT NULL, data TEXT NOT NULL, io TEXT,
                PRIMARY KEY (batch_id, instance))""")

    @classmethod
    def _row(cls, row):
        if row is None:
            return None
        return {"id": row[0], "workflowId": row[1], "key": row[2], "status": row[3], "createdAt": row[4],
                "finishedAt": row[5], **json.loads(row[6])}

    @classmethod
    def _data(cls, record):
        return encode({k: v for k, v in record.items() if k not in cls.TOP})

    def add(self, record):
        with self.journal.transaction() as connection:
            connection.execute("INSERT INTO workflow_batches VALUES (?,?,?,?,?,?,?)",
                               (record["id"], record["workflowId"], record["key"], record["status"],
                                record["createdAt"], record.get("finishedAt"), self._data(record)))
            stale = connection.execute("""SELECT id FROM workflow_batches WHERE workflow_id=? AND status!='running'
                ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?""", (record["workflowId"], KEEP)).fetchall()
            for (identifier,) in stale:
                connection.execute("DELETE FROM workflow_batches WHERE id=?", (identifier,))
                connection.execute("DELETE FROM workflow_nodes WHERE batch_id=?", (identifier,))

    def save(self, record):
        with self.journal.transaction() as connection:
            self.write(connection, record)

    def write(self, connection, record):
        connection.execute("UPDATE workflow_batches SET status=?, finished_at=?, data=? WHERE id=?",
                           (record["status"], record.get("finishedAt"), self._data(record), record["id"]))

    def get(self, identifier):
        with self.journal.lock:
            return self._row(self.journal.connection.execute(
                "SELECT * FROM workflow_batches WHERE id=?", (identifier,)).fetchone())

    def by_key(self, key):
        with self.journal.lock:
            return self._row(self.journal.connection.execute(
                "SELECT * FROM workflow_batches WHERE key=? ORDER BY created_at DESC LIMIT 1", (key,)).fetchone())

    def recent(self, workflow_id, limit=KEEP):
        with self.journal.lock:
            rows = self.journal.connection.execute("""SELECT * FROM workflow_batches WHERE workflow_id=?
                ORDER BY created_at DESC, rowid DESC LIMIT ?""", (workflow_id, limit)).fetchall()
        return [self.public(self._row(row)) for row in rows]

    def running(self):
        with self.journal.lock:
            rows = self.journal.connection.execute("SELECT * FROM workflow_batches WHERE status='running'").fetchall()
        return [self._row(row) for row in rows]

    def forget(self, workflow_id):
        with self.journal.transaction() as connection:
            connection.execute("""DELETE FROM workflow_nodes WHERE batch_id IN (SELECT id FROM workflow_batches
                WHERE workflow_id=? AND status!='running')""", (workflow_id,))
            connection.execute("DELETE FROM workflow_batches WHERE workflow_id=? AND status!='running'", (workflow_id,))

    def save_node(self, batch_id, entry, io=None):
        with self.journal.transaction() as connection:
            connection.execute("""INSERT INTO workflow_nodes VALUES (?,?,?,?,?) ON CONFLICT(batch_id, instance)
                DO UPDATE SET data=excluded.data, io=COALESCE(excluded.io, workflow_nodes.io)""",
                               (batch_id, entry["instance"], entry["seq"], encode(entry),
                                encode(io) if io is not None else None))

    def nodes(self, batch_id):
        with self.journal.lock:
            rows = self.journal.connection.execute(
                "SELECT data FROM workflow_nodes WHERE batch_id=? ORDER BY seq", (batch_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def nodes_with_io(self, batch_id):
        with self.journal.lock:
            rows = self.journal.connection.execute(
                "SELECT data, io FROM workflow_nodes WHERE batch_id=? ORDER BY seq", (batch_id,)).fetchall()
        return [(json.loads(data), json.loads(io) if io else None) for data, io in rows]

    def node(self, batch_id, instance):
        with self.journal.lock:
            row = self.journal.connection.execute(
                "SELECT data, io FROM workflow_nodes WHERE batch_id=? AND instance=?", (batch_id, instance)).fetchone()
        if row is None:
            return None
        return {**json.loads(row[0]),
                **(json.loads(row[1]) if row[1] else {"input": None, "output": None, "error": None})}

    def public(self, record):
        out = public(record)
        if out is None or record.get("graph") != 2:
            return out
        entries = self.nodes(record["id"])
        out.update(graph=2, trigger=record.get("trigger"), revision=record.get("revision"), result=record.get("result"),
                   test=bool(record.get("test")), nodes={entry["instance"]: entry for entry in entries},
                   log=[legacy_entry(entry) for entry in entries if entry.get("type") in LEGACY_KINDS],
                   rounds=legacy_rounds(entries, record))
        return out


LEGACY_KINDS = {"phone.task": "task", "phone.taught": "taught", "phone.check": "check", "wait": "wait", "send": "send",
                "connector.call": "connector", "brief": "brief"}
_RESULTS = ("task", "taught", "connector", "brief")


def legacy_entry(entry, record=None):
    kind = LEGACY_KINDS.get(entry.get("type"), entry.get("type"))
    state = entry.get("state")
    if kind in ("task", "taught", "check"):
        state = {"running": "started" if entry.get("runId") else "claimed", "unknown": "interrupted",
                 "done": "finished"}.get(state, state)
        if entry.get("refused"):
            state = "refused"
        elif entry.get("state") in ("failed", "cancelled") and entry.get("runId"):
            state = "finished"
    elif kind == "wait":
        state = {"running": "waiting"}.get(state, state)
    elif kind == "send":
        state = {"running": "sending"}.get(state, state)
    iteration = entry.get("iteration") or []
    out = {"at": entry.get("startedAt") or entry.get("finishedAt"), "round": iteration[0] + 1 if iteration else 1,
           "step": entry.get("node"), "kind": kind, "state": state, "node": entry.get("node"),
           "iteration": iteration}
    for key, name in (("key", "key"), ("runId", "runId"), ("label", "label"), ("status", "status"),
                      ("costUsd", "costUsd"), ("reason", "reason"), ("until", "until"), ("deviceId", "deviceId"),
                      ("answer", "answer")):
        if entry.get(key) is not None:
            out[name] = entry[key]
    if entry.get("line"):
        out["line"] = entry["line"]
    return out


def legacy_rounds(entries, record):
    loop = next((e for e in entries if e.get("type") == "loop" and not e.get("iteration")), None)
    steps = [e for e in entries if e.get("type") in LEGACY_KINDS]
    if loop is not None:
        count = 1 + max((e["iteration"][0] for e in entries if e.get("iteration")), default=-1)
        groups = [[e for e in steps if e.get("iteration") and e["iteration"][0] == n] for n in range(count)]
    else:
        groups = [steps]
    values = record.get("roundValues") or []
    rounds = []
    for n, members in enumerate(groups):
        tasks = [e for e in members if LEGACY_KINDS[e["type"]] in ("task", "taught", "check") and e.get("runId")]
        ordered = sorted(members, key=lambda e: (e.get("finishedAt") or 0, e["seq"]))
        answer = next((e.get("answer") for e in reversed(ordered)
                       if LEGACY_KINDS[e["type"]] in _RESULTS and e.get("answer")), None)
        if any(e.get("state") not in ("done", "skipped", "failed", "blocked", "cancelled", "unknown") for e in members):
            status, reason = "running", None
        else:
            failed = next((e for e in ordered if e.get("state") in ("unknown", "cancelled") or
                           e.get("state") == "failed" and not e.get("handled")), None)
            status = (failed.get("status") or ("error" if failed["state"] == "unknown" else "failed")) if failed \
                else "completed"
            reason = failed.get("reason") if failed else None
        rounds.append({"n": n + 1, "values": values[n] if n < len(values) else {}, "status": status,
                       "runIds": [e["runId"] for e in tasks], "answer": answer[:4000] if answer else None,
                       "costUsd": round(sum(e.get("costUsd") or 0.0 for e in tasks), 6), "reason": reason})
    return rounds


def _downloads():
    return Path(os.environ.get("MOBSTER_WORKFLOW_FILES") or Path.home() / "Downloads")


def save_text(name, text, folder=None):
    folder = Path(folder) if folder else _downloads()
    stem = re.sub(r"[^\w .,'’()&+–-]+", "-", name).strip(" .-")[:80] or "Mobster workflow"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(1000):
        target = folder / (f"{stem}.md" if index == 0 else f"{stem} ({index}).md")
        try:
            with open(target, "x", encoding="utf-8") as stream:
                stream.write(text)
            return target.name
        except FileExistsError:
            continue
    raise OSError("Too many files with that name in Downloads")


def public(record):
    if record is None:
        return None
    return {key: copy.deepcopy(record.get(key)) for key in
            ("id", "workflowId", "status", "reason", "createdAt", "finishedAt", "spentUsd", "modelCalls", "runs",
             "threadId", "repeat", "rounds", "log", "graph", "trigger", "revision", "result", "test", "roundValues",
             "from", "prove")
            if key in record or key in ("rounds", "log", "repeat")}


def new_record(workflow, key, thread_id, now_ms):
    return {"id": uuid.uuid4().hex[:12], "workflowId": workflow["id"], "key": key, "status": "running",
            "createdAt": now_ms, "finishedAt": None, "threadId": thread_id, "repeat": workflow.get("repeat"),
            "rounds": [], "log": [], "spentUsd": 0.0, "runs": 0, "reason": None}
