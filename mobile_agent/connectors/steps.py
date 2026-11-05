"""The workflow builder's "Use a connector" step (batch.register_step, kind ``connector``): one tool of a connected
service, called by the workflow itself, with no model and without the phone."""

import json

from . import client as mcp
from . import service as connector_service
from . import store as connector_store

ARGS_CHARS = 16_000


def _service(runtime):
    service = connector_service.service_for(runtime)
    if service is None:
        raise ValueError("Connectors aren't available in this Mobster")
    return service


def validate(step, runtime):
    """The step as saved, or ValueError with a sentence."""
    if set(step) - {"id", "kind", "connector", "tool", "arguments"}:
        raise ValueError("A connector step has a connector, a tool and its arguments")
    connector, tool = step.get("connector"), step.get("tool")
    if not isinstance(connector, str) or not connector:
        raise ValueError("Choose a connector")
    if not isinstance(tool, str) or not 0 < len(tool) <= 128:
        raise ValueError("Choose one of the connector's tools")
    arguments = step.get("arguments") if step.get("arguments") is not None else {}
    if not isinstance(arguments, dict):
        raise ValueError("A connector step's arguments are one JSON object")
    if len(json.dumps(arguments)) > ARGS_CHARS:
        raise ValueError("Those arguments are too long")
    if runtime is not None:
        row = _service(runtime).store.get(connector)
        if row is None:
            raise ValueError("That connector was removed. Choose another one.")
        if row["tools"] and not any(t["name"] == tool for t in row["tools"]):
            raise ValueError(f"{row['name']} has no tool called {tool}. Test it in Settings › "
                             "Connectors, then choose again.")
    return {"connector": connector, "tool": tool, "arguments": arguments}


def _fill(value, values, fill):
    if isinstance(value, str):
        return fill(value, values)
    if isinstance(value, list):
        return [_fill(item, values, fill) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, values, fill) for key, item in value.items()}
    return value


def run(batch, step, values):
    """One call. {"answer", "line"}, or batch.Stop with a sentence (nothing sent unless the sentence says so)."""
    from ..secret_filter import redact
    from ..workflows.records import Stop
    from .skill import result_text
    service = connector_service.service_for(batch.runtime)
    if service is None:
        raise Stop("dispatch_failed", "Connectors aren't available in this Mobster.")
    row = service.store.get(step["connector"])
    if row is None:
        raise Stop("dispatch_failed", "The connector this step uses was removed. Choose another one.")
    tool, name = step["tool"], row["name"]
    if not row["enabled"]:
        raise Stop("dispatch_failed", f"{name} is turned off in Settings › Connectors, so this step didn't run.")
    if row["tools"] and not any(t["name"] == tool for t in row["tools"]):
        raise Stop("dispatch_failed", f"{name} no longer has a tool called {tool}.")
    mode = connector_store.mode_of(row, tool)
    if mode == "off":
        raise Stop("dispatch_failed", f"{name}: {tool} is blocked in Settings › Connectors, so this step didn't run.")
    if mode != "read":
        raise Stop("dispatch_failed", f"{name}: {tool} asks you first, and a workflow step has nobody to ask, so "
                   "nothing was sent. Allow it in Settings › Connectors, or use "
                   "a task step that names it.")
    arguments = _fill(step.get("arguments") or {}, values, batch._fill)
    try:
        result = service.call(row, tool, arguments)
    except mcp.OutcomeUnknown as error:
        raise Stop("dispatch_failed", f"{name}: {tool} didn't answer ({error.message}) It may or may not have "
                   f"happened, so the workflow stopped and won't send it again.",
                   unknown=True) from None
    except mcp.ConnectorError as error:
        raise Stop("dispatch_failed", f"{name}: {tool} wasn't called: {redact(error.message)}",
                   retryable=error.sent is False) from None
    text = result_text(result)
    if result.get("isError"):
        raise Stop("dispatch_failed", f"{name}: {tool} returned an error: {text[:200]}")
    structured = result.get("structuredContent")
    return {"answer": text, "line": f"Used {name}: {tool}",
            "structured": structured if isinstance(structured, dict) else None}
