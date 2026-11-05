"""Durable intent and outcome for direct MCP mutations, including key-less checks."""

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ..journal import append_jsonl


@contextmanager
def record_action(run_dir, operation):
    # Arguments can contain passwords, codes and private URLs. Record only the
    # operation here; the existing step records own their redacted descriptions.
    path = Path(run_dir) / "actions.jsonl"
    action = {"id": uuid4().hex, "operation": operation}
    append_jsonl(path, {"event": "action_started", **action})
    try:
        yield
    except BaseException:
        # A timeout or failed screen read cannot prove that nothing happened.
        append_jsonl(path, {"event": "action_finished", **action, "outcome": "unknown"})
        raise
    else:
        append_jsonl(path, {"event": "action_finished", **action, "outcome": "acknowledged"})
