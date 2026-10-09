"""MCP tools that hand a task to Mobster's agent in a conversation (provider ``threads``, seam S10).

- ``phone_task {message, thread_id?, device?, wait_seconds<=45}``: a new task, or a follow-up in the same
  conversation, run by Mobster's agent on the user's iPhone. Returns {thread_id, run_id, status, answer?}.
- ``phone_task_status {thread_id | run_id}``: where that task is now.

Both go through the running Mobster app or `mobster serve` (client.py) with origin ``mcp``: approvals show in the
Mac app (or `mobster chat` on a terminal), where a person answers them; this server never answers one. A task
started here never starts in or opens a social or dating app (harness_api.UNATTENDED_DENY, enforced by the core),
takes messages only from MCP or the Mac app, and is listed in the user's conversations like any other.
"""

import re

from .client import Client, NoService, ServiceError

NO_SERVICE = "Open Mobster or run `mobster serve` so your agent can hand tasks to Mobster's agent."
WAIT_MAX = 45
HEADROOM = 7.0            # of the 44 s call budget: the HTTP round trips and the answer
ID = r"^[a-f0-9]{12}$"
DONE = frozenset({"completed"})


def schema_task():
    return {"type": "object", "properties": {
        "message": {"type": "string", "minLength": 1, "maxLength": 4000,
                    "description": "what Mobster's agent should do on the iPhone, in plain words; in a "
                                   "conversation, a follow-up such as \"now reply to her\" (it sees what the earlier "
                                   "tasks found)"},
        "thread_id": {"type": "string", "pattern": ID,
                      "description": "continue this conversation (from an earlier phone_task); leave it out to "
                                     "start a new one. While its task runs, the message steers it, or answers "
                                     "Mobster's question"},
        "device": {"type": "string", "maxLength": 128,
                   "description": "the iPhone or simulator by id, UDID or name (default: the conversation's, else "
                                  "the user's default device)"},
        "wait_seconds": {"type": "integer", "minimum": 0, "maximum": WAIT_MAX,
                         "description": "how long to wait for the task before returning (default 30); call "
                                        "phone_task_status for the rest"}},
        "required": ["message"], "additionalProperties": False}


def schema_status():
    return {"type": "object", "properties": {
        "thread_id": {"type": "string", "pattern": ID, "description": "the conversation's latest task"},
        "run_id": {"type": "string", "pattern": ID, "description": "one task"},
        "wait_seconds": {"type": "integer", "minimum": 0, "maximum": WAIT_MAX,
                         "description": "wait up to this long for it to finish or need the user (default 0)"}},
        "required": [], "additionalProperties": False}


def describe(run):
    """{status, answer?, waiting?} for a run: running, waiting_for_approval, waiting_for_answer, or how it ended."""
    approval = run.get("approval")
    if run.get("finishedAt") is None:
        if approval and approval.get("kind") == "clarify":
            return {"status": "waiting_for_answer", "question": approval.get("question") or approval.get("label"),
                    "choices": [c.get("label") for c in approval.get("choices") or () if isinstance(c, dict)]}
        if approval:
            return {"status": "waiting_for_approval", "approval": approval.get("title") or approval.get("label")}
        return {"status": "running"}
    summary = run.get("summary") or {}
    out = {"status": run.get("status") or summary.get("status") or "error"}
    data = summary.get("data")
    answer = data if isinstance(data, str) and data.strip() else summary.get("answer")
    if answer:
        out["answer"] = str(answer)[:3000]
    elif summary.get("reason") and out["status"] not in DONE:
        out["reason"] = str(summary["reason"])[:500]
    if isinstance(data, (dict, list)):
        out["data"] = data
    return out


def sentence(state, thread_id, answerable=True):
    """The tool's text. ``answerable``: the task was started over MCP, so phone_task may answer its question; any
    other task's question is the person's (the threads route refuses an answer from MCP)."""
    status = state["status"]
    if status == "running":
        return ("Mobster's agent is still working on it. Call phone_task_status with thread_id "
                f"{thread_id} to see how it ends.")
    if status == "waiting_for_approval":
        return (f"It's waiting for the user to approve this in the Mobster app: {state.get('approval') or 'an action'}"
                ". Tell the user; then call phone_task_status.")
    if status == "waiting_for_answer":
        choices = state.get("choices") or []
        asks = f"Mobster's agent asks: {state.get('question')}" + (f" (choices: {', '.join(choices)})" if choices
                                                                     else "")
        if not answerable:
            return asks + ". The user answers this in the Mobster app; then call phone_task_status."
        return asks + f". Answer with phone_task, thread_id {thread_id}, or let the user answer in the Mobster app."
    if status in DONE:
        return "Done." + (f" {state['answer']}" if state.get("answer") else "")
    return f"It ended {status.replace('_', ' ')}." + (f" {state.get('answer') or state.get('reason') or ''}".rstrip())


class ThreadsProvider:
    """mcp_server.registry.ToolProvider for conversations."""

    name = "threads"

    def __init__(self, connect=None):
        self._connect = connect or (lambda: Client.connect(origin="mcp"))

    def definitions(self, toolset):
        return [
            {"name": "phone_task",
             "description": "Hand a task to Mobster's agent on the user's iPhone. It asks the user in the Mobster app "
                            "before it sends, buys, posts or deletes. Pass thread_id for a follow-up that sees what "
                            "earlier tasks found.",
             "inputSchema": schema_task()},
            {"name": "phone_task_status",
             "description": "Where a phone_task task is: running, waiting for the user's approval or answer, or "
                            "finished with its answer.",
             "inputSchema": schema_status()}]

    def annotations(self):
        from ..mcp_server.tools import ACTS, READ_ONLY
        return {"phone_task": ACTS, "phone_task_status": READ_ONLY}

    def instructions(self):
        return ("phone_task hands a whole task to Mobster's agent in the user's Mobster app, in a conversation that "
                "remembers; the user approves sends, buys, posts and deletes there.")

    def client(self):
        from ..mcp_server.tools import ToolError
        try:
            return self._connect()
        except (NoService, ServiceError):
            raise ToolError(NO_SERVICE) from None

    def call(self, name, arguments, call, toolset):
        from ..mcp_server.protocol import ToolResult
        from ..mcp_server.tools import ToolError
        client = self.client()
        if not client.has_threads:
            raise ToolError("This Mobster doesn't have conversations yet. Ask the user to update Mobster.")
        try:
            if name == "phone_task":
                return self.task(client, arguments, toolset, ToolResult)
            return self.status(client, arguments, ToolResult)
        except ServiceError as error:
            if error.code == "approval_pending":
                raise ToolError("That conversation's task is waiting for the user's approval in the Mobster app. "
                                "Tell the user, then call phone_task_status.") from None
            if error.code == "steer_not_allowed":
                raise ToolError("That task was started somewhere else, so it takes messages only there. Start a "
                                "new conversation without thread_id.") from None
            raise ToolError(str(error)) from None
        except NoService:
            raise ToolError(NO_SERVICE) from None

    @staticmethod
    def allowed(toolset, *names):
        allow = getattr(toolset, "allow_devices", None)
        if allow is None:
            return True
        return any(isinstance(n, str) and n.strip().casefold() in allow for n in names)

    def task(self, client, arguments, toolset, ToolResult):
        from ..mcp_server.tools import ToolError
        thread_id, device = arguments.get("thread_id"), arguments.get("device")
        thread = client.thread(thread_id)["thread"] if thread_id else None
        if getattr(toolset, "allow_devices", None) is not None:
            names = (device,) if device else ((thread or {}).get("device"), (thread or {}).get("deviceName"))
            if not self.allowed(toolset, *names):
                raise ToolError("This server may use only the devices given with --allow-device: name one of them "
                                "with device.")
        body = {"text": arguments["message"]}
        if device:
            body["device"] = device
        thread_id, answer = client.send(thread_id, body)
        run = answer.get("run") or {}
        routed = answer.get("routed") or ("new_run" if run else None)
        run_id = run.get("id") or (answer.get("item") or {}).get("runId")
        wait = min(float(arguments.get("wait_seconds", 30)), WAIT_MAX - HEADROOM)
        if routed in ("steer", "answer"):
            text = ("Mobster's agent got your message while it works on the task." if routed == "steer"
                    else "Mobster's agent has your answer and goes on.")
            return ToolResult(text, structured={"thread_id": thread_id, "run_id": run_id, "status": "running",
                                                "routed": routed})
        state = describe(client.wait_run(run_id, wait)) if run_id else {"status": "running"}
        return ToolResult(sentence(state, thread_id), structured={"thread_id": thread_id, "run_id": run_id, **state})

    def status(self, client, arguments, ToolResult):
        from ..mcp_server.tools import ToolError
        run_id, thread_id = arguments.get("run_id"), arguments.get("thread_id")
        if not run_id and not thread_id:
            raise ToolError("Give the thread_id or the run_id that phone_task returned.")
        if not run_id:
            thread = client.thread(thread_id)["thread"]
            run_id = thread.get("lastRunId")
            if not run_id:
                return ToolResult("No task has started in this conversation yet.",
                                  structured={"thread_id": thread_id, "run_id": None, "status": "idle"})
        run = client.wait_run(run_id, min(float(arguments.get("wait_seconds", 0)), WAIT_MAX - HEADROOM))
        thread_id = thread_id or ((run.get("extras") or {}).get("threadId"))
        if not re.fullmatch(r"[a-f0-9]{12}", str(thread_id or "")):
            thread_id = None
        state = describe(run)
        return ToolResult(sentence(state, thread_id or "(none)", answerable=run.get("origin") in (None, "mcp")),
                          structured={"thread_id": thread_id, "run_id": run_id, **state})
