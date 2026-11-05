"""Published workflow tool names and schemas shared by REST, MCP and Shortcuts."""

import re

TOOL_PREFIX = "workflow_"
TOOL_WAIT_MAX = 45                 # seconds a tool call or a REST run waits for the run before it answers "running"
TOOL_WAIT_DEFAULT = 30
INPUT_TYPES = {"string": "string", "number": "number", "boolean": "boolean", "list": "array", "object": "object"}
# Names under /api/workflows/ that are routes of their own: a workflow named like one takes its id in its slug.
RESERVED_SLUGS = frozenset({"approvals", "draft", "evaluate", "node_types", "preview", "templates", "tools",
                            "validate"})


def tools(workflows):
    """Each published ``asTool`` workflow as a tool; slugs go oldest first, so a new one never renames an older one."""
    taken, out = set(), []
    for workflow, live in sorted(workflows, key=lambda pair: pair[0].get("createdAt") or 0):
        if not workflow.get("asTool") or not workflow.get("published"):
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", str(workflow.get("name") or "").casefold()).strip("_")[:40] or "run"
        if slug in taken or slug in RESERVED_SLUGS or re.fullmatch(r"[a-f0-9]{12}", slug):
            slug = f"{slug}_{str(workflow.get('id'))[:4]}"
        taken.add(slug)
        steps = " ".join(str(workflow.get("goal") or "").split())
        tool = {"name": TOOL_PREFIX + slug, "slug": slug, "path": f"/v1/workflows/{slug}",
                "workflowId": workflow["id"], "workflowName": workflow.get("name"),
                "description": (f"Run the user's workflow “{workflow.get('name')}” on their iPhone"
                                + (f": {steps}" if steps else "."))[:220],
                "inputSchema": tool_schema(workflow)}
        result = next((n for n in (live or {}).get("nodes") or ()
                       if n.get("type") == "output" and (n.get("config") or {}).get("schema")), None)
        if result is not None:
            tool["outputSchema"] = result["config"]["schema"]
        out.append(tool)
    return out


def tool_schema(workflow):
    """A workflow tool's input: its inputs and blanks (typed as declared), each by name, and wait_seconds."""
    properties, required = {}, []
    for blank in workflow.get("blanks") or []:
        kind = INPUT_TYPES.get(blank.get("type") or "string", "string")
        prop = {"type": kind, **({"maxLength": 300} if kind == "string" else {})}
        if blank.get("default") is not None:
            prop["description"] = f"default {blank['default']}"
        else:
            required.append(blank["name"])
        properties[blank["name"]] = prop
    properties["wait_seconds"] = {"type": "integer", "minimum": 0, "maximum": TOOL_WAIT_MAX,
                                  "description": f"how long to wait for it to finish before returning (default "
                                                 f"{TOOL_WAIT_DEFAULT})"}
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _as_tool(value):
    """``asTool``: the workflow is its own tool for AI clients (MCP), typed from its inputs. Off unless turned on."""
    if type(value) is not bool:
        raise ValueError("asTool is true or false")
    return value
