"""MCP tools that hand a task to Mobster's agent in a conversation (provider ``threads``, seam S10)."""

import json
import logging
import re
import time
import traceback
import uuid

from .. import engine_health
from .client import Client, NoService, ServiceError

log = logging.getLogger("mobster.threads")

NO_SERVICE = "Open Mobster or run `mobster serve` so your agent can hand tasks to Mobster's agent."
WAIT_MAX = 45
HEADROOM = 7.0            # of the 44 s call budget: the HTTP round trips and the answer
ID = r"^[a-f0-9]{12}$"
DONE = frozenset({"completed"})
BLOCKS_INLINE = 12000     # characters of result blocks a task's answer carries as they are


def accepted_unavailable(ToolResult, *, text, **handles):
    """A POST succeeded; losing its subsequent status read must not invite another POST."""
    return ToolResult(text, structured={**handles, "status": "status_unavailable"})


def known_refusal(error):
    """Whether a failed start POST certainly started nothing: Mobster refused it with a 4xx other than a timeout (408)
    or its own "outcome unknown", or said so with ``"started": false`` (api_errors.nothing_started), as it does for a
    503 such as the iPhone helper restarting. No reply, any other server fault (5xx) or a malformed answer leaves the
    outcome unknown."""
    if not isinstance(error, ServiceError) or error.code == "request_outcome_unknown":
        return False
    if error.data.get("started") is False:   # only the literal false: a missing or odd value stays unknown
        return True
    # A workflow's 409 "queued" (workflows/runs.py: the run starts when the one going ends) would count as a refusal
    # here, but only a scheduled key gets it: run_workflow and workflow_* send a fresh key on every call.
    return 400 <= error.status < 500 and error.status != 408


def well_formed(answer):
    """Whether a start's 2xx answer has the shapes the tools read: an object whose run, item, group and batch are absent
    or objects, with string ids where a tool reads one. Anything else names no task the tool could follow."""
    if not isinstance(answer, dict):
        return False
    run, item, group, batch = (answer.get(key) for key in ("run", "item", "group", "batch"))
    if any(part and not isinstance(part, dict) for part in (run, item, group, batch)):
        return False
    needed = [part.get("id") for part in (group, batch if batch and batch.get("graph") == 2 else None) if part]
    optional = [(run or {}).get("id"), (item or {}).get("runId")]
    return (all(isinstance(value, str) and value for value in needed)
            and all(value is None or isinstance(value, str) for value in optional))


def log_fault(what, error):
    """Log a fault a tool answered past: its type and stack, never its message, which can carry the phone's text."""
    stack = "".join(traceback.format_tb(error.__traceback__, limit=8))
    log.warning("%s (%s)\n%s", what, type(error).__name__, stack.rstrip())


def mask_handles(mask, name, arguments, result):
    """Keep only canonical continuation handles usable when a remembered code overlaps their bytes."""
    state = result.structured if isinstance(result.structured, dict) and not result.is_error else {}
    keys = {"thread_id", "run_id", "group_id"} if name in ("phone_task", "phone_task_status") else (
        {"workflow_id", "thread_id", "run_id", "batch_id"} if name == "run_workflow" or name == "graph_status"
        or name.startswith(TOOL_PREFIX) else set())
    selectors = ({"thread_id"} if name == "phone_task" else
                 {"thread_id", "run_id", "group_id"} if name == "phone_task_status" else
                 {"thread_id"} if name == "run_workflow" else
                 {"workflow_id", "batch_id"} if name == "graph_status" else set())
    handles = {key: value for key in keys if isinstance(value := state.get(key), str)
               and re.fullmatch(ID, value) and (key not in selectors or arguments.get(key) is None
                                                or arguments[key] == value)}
    if name == "run_workflow" and "workflow_id" in handles and re.fullmatch(ID, str(arguments.get("workflow") or "")):
        if handles["workflow_id"] != arguments["workflow"]:
            handles.pop("workflow_id")
    uri = state.get("status_uri")
    expected = ({f"/api/groups/{handles['group_id']}"} if "group_id" in handles else set()) | (
        {f"/api/runs/{handles['run_id']}"} if "run_id" in handles else set()) | (
        {f"/api/workflows/{handles['workflow_id']}/runs"} if "workflow_id" in handles
        and "batch_id" in handles else set())
    masked = mask(result)
    if isinstance(masked.structured, dict):
        masked.structured.update(handles)
        if isinstance(uri, str) and uri in expected:
            masked.structured["status_uri"] = uri
    return masked


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
        "profile": {"type": "string", "minLength": 1, "maxLength": 160,
                    "description": "act as this profile (from list_profiles, such as "
                                   "\"@getmobster on Instagram\"): the "
                                   "task runs on its iPhone, and Mobster confirms the profile on screen before it "
                                   "posts or sends"},
        "account": {"type": "string", "minLength": 1, "maxLength": 160,
                    "description": "deprecated: use profile, which takes the same value"},
        "phones": {"type": "array", "minItems": 2, "maxItems": 100,
                   "items": {"type": "string", "minLength": 1, "maxLength": 128},
                   "description": "run the same task on each of these devices at once (ids, UDIDs or names from "
                                  "list_devices), as one task: each phone's answer, then one summary that merges "
                                  "them. Leave device out"},
        "profiles": {"description": "run the same task as each of these profiles at once, each on its own iPhone, as "
                                    "one task: \"all\", or a list of profiles (from list_profiles). Mobster "
                                    "confirms each profile on screen before it posts or sends; a profile that "
                                    "fails doesn't stop the others. Leave device, phones and profile out",
                     "oneOf": [{"const": "all"},
                               {"type": "array", "minItems": 1, "maxItems": 100,
                                "items": {"type": "string", "minLength": 1, "maxLength": 160}}]},
        "queue": {"type": "boolean",
                  "description": "true (the default): when the phone is in use, wait in line "
                                 "and start when it is free; false: fail at once instead. "
                                 "The status says who holds the phone and your number"},
        "wait_seconds": {"type": "integer", "minimum": 0, "maximum": WAIT_MAX,
                         "description": "how long to wait for the task before returning (default 30); call "
                                        "phone_task_status for the rest"}},
        "required": ["message"], "additionalProperties": False}


def schema_status():
    return {"type": "object", "properties": {
        "thread_id": {"type": "string", "pattern": ID, "description": "the conversation's latest task"},
        "run_id": {"type": "string", "pattern": ID, "description": "one task"},
        "group_id": {"type": "string", "pattern": ID, "description": "a task run on several phones (from phone_task "
                                                                    "with phones)"},
        "wait_seconds": {"type": "integer", "minimum": 0, "maximum": WAIT_MAX,
                         "description": "wait up to this long for it to finish or need the user (default 0)"}},
        "required": [], "additionalProperties": False}


def describe(run):
    """{status, answer?, waiting?} for a run: running, waiting_for_approval, waiting_for_answer, waiting_for_phone, or
    how it ended."""
    approval = run.get("approval")
    if run.get("finishedAt") is None:
        line = run.get("queue")
        if isinstance(line, dict) and not approval:
            return {"status": "queued", "position": line.get("position"), "behind": line.get("behind"),
                    "queue_message": line.get("message"), "expires_at": line.get("expiresAt")}
        if approval and approval.get("kind") == "clarify":
            return {"status": "waiting_for_answer", "question": approval.get("question") or approval.get("label"),
                    "choices": [c.get("label") for c in approval.get("choices") or () if isinstance(c, dict)]}
        if approval:
            return {"status": "waiting_for_approval", "approval": approval.get("title") or approval.get("label"),
                    "reason": approval.get("reason"), "explain": approval.get("explain"),
                    "expires_at": approval.get("expiresAt")}
        wait = run.get("phoneWait")
        # What the task waits on, in the words every surface uses (states.ROWS).
        found = engine_health.task_wait(wait.get("kind"), run.get("deviceName")) if isinstance(wait, dict) else None
        if found:
            need, line = found
            since, seconds = wait.get("since"), wait.get("seconds")
            return {"status": "waiting_for_phone", "need": need, "message": line,
                    "expires_at": since + seconds * 1000 if isinstance(since, (int, float)) and isinstance(seconds, int)
                    else None}
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
    blocks = summary.get("blocks")
    if isinstance(blocks, dict) and blocks.get("blocks"):
        # The answer's result blocks (docs/results.mdx): as they are when small, else as Markdown, so a client's
        # context isn't flooded by a 200-row table.
        if len(json.dumps(blocks, ensure_ascii=False)) <= BLOCKS_INLINE:
            out["blocks"] = blocks
        else:
            from ..result_blocks import to_markdown
            out["blocks_markdown"] = to_markdown(blocks)[:BLOCKS_INLINE]
    return out


def sentence(state, thread_id, answerable=True, origin="mcp"):
    """The tool's text."""
    status = state["status"]
    remote = origin == "remote"
    where = "in Mobster on their Mac" if remote else "in the Mobster app"
    if status == "queued":
        return (f"{state.get('queue_message') or 'Waiting for the phone.'} Nothing has been done on the phone yet. It "
                f"starts when the phone is free; call phone_task_status with thread_id {thread_id} to see when.")
    if status == "running":
        return ("Mobster's agent is still working on it. Call phone_task_status with thread_id "
                f"{thread_id} to see how it ends.")
    if status == "waiting_for_approval":
        if remote:
            label = str(state.get("approval") or "an action").strip()
            end = "" if label.endswith((".", "?", "!")) else "."
            return (f"It's waiting for the user to approve this in Mobster on their Mac: {label}{end} You can't "
                    "approve it. Tell the user, then call phone_task_status.")
        left = ""
        if isinstance(state.get("expires_at"), (int, float)):
            minutes = max(0, int((state["expires_at"] / 1000 - time.time()) // 60))
            left = f" It waits about {minutes} more minutes, then ends without doing it."
        why = f" ({state['reason']})" if state.get("reason") else ""
        said = f" {state['explain']}" if state.get("explain") else ""
        return (f"It's waiting for the user's OK in the Mobster app, which notified them: "
                f"{state.get('approval') or 'an action'}{why}.{said} Nothing happens until they answer.{left} "
                "Tell the user; then call phone_task_status with wait_seconds to keep waiting.")
    if status == "waiting_for_phone":
        left = ""
        if isinstance(state.get("expires_at"), (int, float)):
            seconds = max(0, int(state["expires_at"] / 1000 - time.time()))
            left = f" It waits about {seconds} more seconds, then stops."
        return (f"It's waiting for the user on the iPhone itself. {state.get('message') or ''} Nothing is done on the "
                f"phone meanwhile.{left} Tell the user now; then call phone_task_status with thread_id {thread_id} and "
                "wait_seconds to keep waiting.")
    if status == "approval_timeout":
        return "Nobody answered the approval in the Mobster app in time, so Mobster didn't do it."
    if status == "waiting_for_answer":
        choices = state.get("choices") or []
        asks = f"Mobster's agent asks: {state.get('question')}" + (f" (choices: {', '.join(choices)})" if choices
                                                                     else "")
        if not answerable:
            return asks + f". The user answers this {where}; then call phone_task_status."
        return asks + f". Answer with phone_task, thread_id {thread_id}, or let the user answer {where}."
    if status in DONE:
        return "Done." + (f" {state['answer']}" if state.get("answer") else "")
    return f"It ended {status.replace('_', ' ')}." + (f" {state.get('answer') or state.get('reason') or ''}".rstrip())


class ThreadsProvider:
    """mcp_server.registry.ToolProvider for conversations."""

    name = "threads"

    def __init__(self, connect=None, *, origin="mcp", wait_max=None, extra_headers=None, default_wait=30):
        """``origin``: "mcp" (`mobster mcp`), or "remote" for Mobster Remote's tools, which word approvals for an
        assistant away from the Mac."""
        self.origin = origin
        self.wait_cap = WAIT_MAX - HEADROOM if wait_max is None else float(wait_max)
        self.default_wait = default_wait
        headers = dict(extra_headers or {})
        self._connect = connect or (lambda: self._connect_default(headers))

    def _connect_default(self, headers):
        client = Client.connect(origin=self.origin)
        client.extra_headers.update(headers)
        return client

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
        if self.origin == "mcp":
            # The holder label on the phone ("Claude Code is using this phone"): the client's own name for itself.
            from ..ownership import clean_client
            label = clean_client(toolset.client_name()) if callable(getattr(toolset, "client_name", None)) else None
            if label:
                client.extra_headers["X-Mobster-Client"] = label
        try:
            if name == "phone_task":
                return self.task(client, arguments, toolset, ToolResult, call=call)
            return self.status(client, arguments, ToolResult)
        except ServiceError as error:
            if error.code == "approval_pending":
                raise ToolError(self.approval_pending()) from None
            if error.code == "steer_not_allowed":
                raise ToolError("That task was started somewhere else, so it takes messages only there. Start a "
                                "new conversation without thread_id.") from None
            raise ToolError(str(error)) from None
        except NoService:
            raise ToolError(NO_SERVICE) from None

    def approval_pending(self):
        if self.origin == "remote":
            return ("That conversation's task is waiting for the user's approval in Mobster on their Mac. You can't "
                    "approve it. Tell the user, then call phone_task_status.")
        return ("That conversation's task is waiting for the user's approval in the Mobster app. Tell the user, then "
                "call phone_task_status.")

    @staticmethod
    def allowed(toolset, *names):
        allow = getattr(toolset, "allow_devices", None)
        if allow is None:
            return True
        return any(isinstance(n, str) and n.strip().casefold() in allow for n in names)

    @staticmethod
    def profile(arguments):
        """The profile a task acts as: ``profile``, or ``account``, its deprecated name. ToolError when they differ."""
        from ..mcp_server.tools import ToolError
        profile, account = arguments.get("profile"), arguments.get("account")
        if profile and account and profile.strip().casefold() != account.strip().casefold():
            raise ToolError("Name the profile once, with profile. account is its old name.")
        return profile or account

    def poll_seconds(self, arguments, call, toolset, client):
        """Leave one HTTP timeout for the last status read before the MCP call budget ends."""
        wait = min(float(arguments.get("wait_seconds", self.default_wait)), self.wait_cap)
        started = getattr(call, "started", None)
        budget = getattr(toolset, "call_budget", None)
        if isinstance(started, (int, float)) and isinstance(budget, (int, float)):
            reserve = max(0.0, float(getattr(client, "timeout", 10.0))) + 2.0
            remaining = budget - (time.monotonic() - started) - reserve
            if remaining <= 0:
                return None
            wait = min(wait, remaining)
        return wait

    def in_time(self, tool, kind, arguments, call, toolset, client):
        """Refuse before the start POST when the call's budget can't fit it and one status read: nothing is sent."""
        from ..mcp_server.tools import ToolError
        if self.poll_seconds(arguments, call, toolset, client) is None:
            raise ToolError(f"{tool} ran out of time before it sent the {kind}, so nothing started. Call {tool} again.")

    def submit(self, post, kind):
        """(thread id, answer) of ``post()``, the one POST that starts a ``kind``. Any failure but Mobster's own refusal
        leaves the outcome unknown, and the error says so instead of inviting another POST."""
        from ..mcp_server.tools import ToolError
        try:
            thread_id, answer = post()
            if not well_formed(answer):
                raise ValueError("the answer doesn't have the shape a start answers with")
            return thread_id, answer
        except Exception as error:  # noqa: BLE001 -- no reply, a 5xx or a malformed answer: the outcome is unknown
            if known_refusal(error):
                raise
            if not isinstance(error, (NoService, ServiceError)):
                log_fault(f"a {kind}'s start request failed, so its outcome is unknown", error)
            check = "Ask the user to check Mobster on their Mac" if self.origin == "remote" else "Check the Mobster app"
            raise ToolError(f"Mobster didn't confirm this {kind}, so whether it started is unknown. {check} before you "
                            "start it again; don't start another to find out.") from None

    @staticmethod
    def follow(wait, read, unavailable):
        """``read(wait)``, the accepted task's answer; ``unavailable``, its handles with status_unavailable, when the
        call has no time left or the read fails in any way. A lost status read never invites another POST."""
        if wait is None:
            return unavailable
        try:
            return read(wait)
        except Exception as error:  # noqa: BLE001 -- the task was accepted: any failed read keeps its handle
            if not isinstance(error, (NoService, ServiceError)):
                log_fault("a status read after an accepted task failed", error)
            return unavailable

    def task(self, client, arguments, toolset, ToolResult, *, call=None):
        from ..mcp_server.tools import ToolError
        thread_id, device, phones = arguments.get("thread_id"), arguments.get("device"), arguments.get("phones")
        if device and phones:
            raise ToolError("Name one phone with device, or several with phones, not both.")
        profile = self.profile(arguments)
        profiles = arguments.get("profiles")
        if profile and phones:
            raise ToolError("A profile runs on its own iPhone: leave phones out.")
        if profiles is not None and (profile or phones or device):
            raise ToolError("Run on several profiles with profiles alone: each runs on its own iPhone.")
        thread = client.thread(thread_id)["thread"] if thread_id else None
        if getattr(toolset, "allow_devices", None) is not None:
            if profiles is not None:
                raise ToolError("This server may use only the devices given with --allow-device: run profiles one "
                                "at a time, naming each profile's phone with device.")
            if profile and not device:
                # The profile's phone isn't known here: naming it lets the allowlist check it, and the task is
                # refused if it isn't the profile's phone (accounts/routing.py).
                raise ToolError("This server may use only the devices given with --allow-device: name the "
                                "profile's phone with device too.")
            if phones:
                if not all(self.allowed(toolset, name) for name in phones):
                    raise ToolError("This server may use only the devices given with --allow-device: name only "
                                    "those in phones.")
            else:
                names = (device,) if device else ((thread or {}).get("device"), (thread or {}).get("deviceName"))
                if not self.allowed(toolset, *names):
                    raise ToolError("This server may use only the devices given with --allow-device: name one of "
                                    "them with device.")
        body = {"text": arguments["message"]}
        if device:
            body["device"] = device
        if phones:
            body["phones"] = list(phones)
        if profile:
            body["account"] = profile
        if profiles is not None:
            body["profiles"] = profiles if profiles == "all" else list(profiles)
        if arguments.get("queue") is False:
            body["queue"] = False
        self.in_time("phone_task", "task", arguments, call, toolset, client)
        key = uuid.uuid4().hex  # with a key, Mobster answers "outcome unknown" for a start it can't confirm
        thread_id, answer = self.submit(lambda: client.send(thread_id, body, key=key), "task")
        wait = self.poll_seconds(arguments, call, toolset, client)
        if answer.get("group"):
            group_id = answer["group"]["id"]
            unavailable = accepted_unavailable(
                ToolResult, text=f"Mobster accepted the task, but its status is unavailable. Call "
                f"phone_task_status with group_id {group_id}; do not start it again.",
                thread_id=thread_id, group_id=group_id, status_uri=f"/api/groups/{group_id}")
            return self.follow(wait, lambda seconds: self.group_result(client.wait_group(group_id, seconds),
                                                                      thread_id, ToolResult), unavailable)
        run = answer.get("run") or {}
        routed = answer.get("routed") or ("new_run" if run else None)
        run_id = run.get("id") or (answer.get("item") or {}).get("runId")
        if routed in ("steer", "answer"):
            text = ("Mobster's agent got your message while it works on the task." if routed == "steer"
                    else "Mobster's agent has your answer and goes on.")
            return ToolResult(text, structured={"thread_id": thread_id, "run_id": run_id, "status": "running",
                                                "routed": routed})
        if not run_id:
            state = {"status": "running"}
            return ToolResult(sentence(state, thread_id, origin=self.origin),
                              structured={"thread_id": thread_id, "run_id": None, **state})

        def read(seconds):
            state = describe(client.wait_run(run_id, seconds, through_approval=True))
            return ToolResult(sentence(state, thread_id, origin=self.origin),
                              structured={"thread_id": thread_id, "run_id": run_id, **state})
        return self.follow(wait, read, accepted_unavailable(
            ToolResult, text=f"Mobster accepted the task, but its status is unavailable. Call "
            f"phone_task_status with run_id {run_id}; do not start it again.",
            thread_id=thread_id, run_id=run_id, status_uri=f"/api/runs/{run_id}"))

    def group_result(self, group, thread_id, ToolResult):
        """phone_task's answer for a task on several phones: each phone's state and answer, and when every phone is
        done, the merged summary (the lines several phones found, once)."""
        phones = []
        summary = group.get("summary") or {}
        answers = {item.get("profile") or item.get("device"): item for item in summary.get("answers") or ()}
        for member in group.get("members") or ():
            approval = member.get("approval") or {}
            if member.get("state") == "refused":
                state = {"status": "not_started", "reason": member.get("error")}
            elif member.get("state") == "held":
                state = {"status": "waiting_for_mac"}
            elif approval:
                state = {"status": "waiting_for_answer" if approval.get("kind") == "clarify" else
                         "waiting_for_approval", "approval": approval.get("title") or approval.get("label")}
            elif member.get("finishedAt") is None:
                state = {"status": "running"}
            else:
                found = answers.get(member.get("profile") or member.get("device")) or {}
                state = {"status": member.get("status") or found.get("status") or "error",
                         **({"answer": found["answer"]} if found.get("answer") else {})}
            phones.append({"device": member.get("device"), "name": member.get("deviceName"),
                           **({"profile": member.get("profileName") or member["profile"]}
                              if member.get("profile") else {}),
                           "run_id": member.get("runId"), **state})
        counts = group.get("counts") or {}
        total = counts.get("phones") or len(phones)
        if group.get("state") == "finished":
            lines = [line["text"] + (f" ({len(line['phones'])} phones)" if len(line.get("phones") or ()) > 1 else "")
                     for line in summary.get("lines") or ()][:40]
            text = (f"Done on {summary.get('completed', 0)} of {total} phones." +
                    (" " + " ".join(lines) if lines else ""))
        elif group.get("state") == "waiting":
            text = (f"{counts.get('waiting', 0)} of {total} phones wait for the user in the Mobster app. Tell the "
                    "user; "
                    f"then call phone_task_status with group_id {group.get('id')}.")
        else:
            text = (f"Mobster's agent is working on {total} phones. Call phone_task_status with group_id "
                    f"{group.get('id')} to see how it ends.")
        structured = {"thread_id": thread_id, "group_id": group.get("id"), "status": group.get("state"),
                      "phones": phones}
        if group.get("state") == "finished":
            structured["summary"] = {"lines": summary.get("lines") or [], "rows": summary.get("rows"),
                                     "completed": summary.get("completed"), "costUsd": summary.get("costUsd")}
        return ToolResult(text, structured=structured)

    def status(self, client, arguments, ToolResult):
        from ..mcp_server.tools import ToolError
        run_id, thread_id, group_id = arguments.get("run_id"), arguments.get("thread_id"), arguments.get("group_id")
        if not run_id and not thread_id and not group_id:
            raise ToolError("Give the thread_id, run_id or group_id that phone_task returned.")
        if group_id:
            group = client.wait_group(group_id, min(float(arguments.get("wait_seconds", 0)), self.wait_cap))
            return self.group_result(group, thread_id or group.get("threadId"), ToolResult)
        if not run_id:
            thread = client.thread(thread_id)["thread"]
            if thread.get("lastGroupId") and thread.get("group"):
                group = client.wait_group(thread["lastGroupId"],
                                          min(float(arguments.get("wait_seconds", 0)), self.wait_cap))
                return self.group_result(group, thread_id, ToolResult)
            run_id = thread.get("lastRunId")
            if not run_id:
                return ToolResult("No task has started in this conversation yet.",
                                  structured={"thread_id": thread_id, "run_id": None, "status": "idle"})
        run = client.wait_run(run_id, min(float(arguments.get("wait_seconds", 0)), self.wait_cap),
                              through_approval=True)
        thread_id = thread_id or ((run.get("extras") or {}).get("threadId"))
        if not re.fullmatch(r"[a-f0-9]{12}", str(thread_id or "")):
            thread_id = None
        state = describe(run)
        return ToolResult(sentence(state, thread_id or "(none)", answerable=run.get("origin") in (None, self.origin),
                                   origin=self.origin),
                          structured={"thread_id": thread_id, "run_id": run_id, **state})


TOOL_PREFIX = "workflow_"          # a workflow made a tool (workflows.tools names it)


def graph_state(batch, limit=60):
    """A graph run as a tool answers: its status, its result and each step's state, reason and output preview."""
    nodes = sorted((batch.get("nodes") or {}).values(), key=lambda node: node.get("seq", 0))
    steps = [{key: node[key] for key in ("instance", "type", "state", "reason", "output", "runId") if key in node}
             for node in nodes if not str(node.get("type")).startswith("trigger.")][:limit]
    return {"batch_id": batch.get("id"), "status": batch.get("status"), "reason": batch.get("reason"),
            "result": batch.get("result"), "steps": steps}


def schema_workflows():
    return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def schema_run_workflow():
    return {"type": "object", "properties": {
        "workflow": {"type": "string", "minLength": 1, "maxLength": 120,
                     "description": "the workflow's name or id, from list_workflows"},
        "values": {"type": "object", "additionalProperties": {"type": "string", "maxLength": 300},
                   "description": "a value for each blank, by name, like {\"topic\": \"basketball\"}; a blank left "
                                  "out takes its default"},
        "thread_id": {"type": "string", "pattern": ID,
                      "description": "run it in this conversation (from phone_task); leave it out for a new "
                                     "conversation, or the workflow's own one when it always continues one"},
        "new_conversation": {"type": "boolean",
                             "description": "start a new conversation even when the workflow always continues one"},
        "wait_seconds": {"type": "integer", "minimum": 0, "maximum": WAIT_MAX,
                         "description": "how long to wait for the task before returning (default 30); call "
                                        "phone_task_status for the rest"}},
        "required": ["workflow"], "additionalProperties": False}


def schema_graph_status():
    return {"type": "object", "properties": {
        "workflow_id": {"type": "string", "pattern": ID, "description": "the saved workflow's id from run_workflow"},
        "batch_id": {"type": "string", "pattern": ID, "description": "the graph batch's id from run_workflow"},
        "wait_seconds": {"type": "integer", "minimum": 0, "maximum": WAIT_MAX,
                         "description": "wait up to this long for the graph to finish (default 0)"}},
        "required": ["workflow_id", "batch_id"], "additionalProperties": False}


def shape(workflow):
    """{"nodes", "branches", "loops"}: how many steps its graph has, and how many branch or loop."""
    nodes = [n for n in ((workflow.get("graph") or {}).get("nodes") or [])
             if not str(n.get("type")).startswith("trigger.")]
    return {"nodes": len(nodes), "branches": sum(1 for n in nodes if n.get("type") == "branch"),
            "loops": sum(1 for n in nodes if n.get("type") == "loop")}


def describe_workflow(workflow):
    """One line: its name, id, steps, its graph's shape and its blanks."""
    blanks = workflow.get("blanks") or []
    words = ", ".join(f"{b['name']}" + (f" (default {b['default']})" if b.get("default") else " (no default)")
                      for b in blanks)
    steps = " ".join(str(workflow.get("goal") or "").split())
    line = f"“{workflow.get('name')}” ({workflow.get('id')}): {steps[:300]}"
    counts = shape(workflow)
    if counts["nodes"] > 1:
        singular = {"branches": "branch", "loops": "loop"}
        parts = [f"{counts['nodes']} steps"] + [f"{counts[key]} {key if counts[key] > 1 else singular[key]}"
                                                 for key in ("branches", "loops") if counts[key]]
        line += " Graph: " + ", ".join(parts) + "."
    if words:
        line += f" Blanks: {words}."
    if workflow.get("sameConversation"):
        line += " It always continues one conversation."
    return line


class WorkflowsProvider(ThreadsProvider):
    """mcp_server.registry.ToolProvider for saved workflows: list them, and run one with its blanks filled, in a new
    conversation or one you name."""

    name = "workflows"

    def definitions(self, toolset):
        return self.static_definitions() + [tool for tool, _workflow in self.workflow_tools()]

    def workflow_tools(self):
        """[(definition, tool)] for each published workflow the person made a tool of (``asTool``), as Mobster defines
        it (GET /api/workflows/tools; the same tool is its REST route), or none when Mobster isn't reachable: a tool
        list never fails for them."""
        try:
            listed = self.client().workflow_tools()
        except Exception:  # noqa: BLE001 -- no Mobster to ask, or one too old to say: the fixed tools only
            return []
        return [({"name": tool["name"], "description": tool["description"], "inputSchema": tool["inputSchema"]}, tool)
                for tool in listed if str(tool.get("name") or "").startswith(TOOL_PREFIX)]

    def static_definitions(self):
        return [
            {"name": "list_workflows",
             "description": "The user's saved workflows in Mobster: each one's name, steps and blanks.",
             "inputSchema": schema_workflows()},
            {"name": "run_workflow",
             "description": "Run one of the user's saved workflows on their iPhone, with a value for each blank, in "
                            "a new conversation or the one you name with thread_id. Mobster asks the user before it "
                            "sends, buys, posts or deletes.",
             "inputSchema": schema_run_workflow()},
            {"name": "graph_status",
             "description": "Read a graph workflow's accepted batch by workflow_id and batch_id, without "
                            "starting it again.",
             "inputSchema": schema_graph_status()}]

    def annotations(self):
        from ..mcp_server.tools import ACTS, READ_ONLY
        return {"list_workflows": READ_ONLY, "run_workflow": ACTS, "graph_status": READ_ONLY,
                **{tool["name"]: ACTS for tool, _workflow in self.workflow_tools()}}

    def instructions(self):
        return "run_workflow runs one of the user's saved workflows (list_workflows) with its blanks filled in."

    def call(self, name, arguments, call, toolset):
        from ..mcp_server.protocol import ToolResult
        from ..mcp_server.tools import ToolError
        client = self.client()
        try:
            if name == "list_workflows":
                listed = client.workflows()
                if not listed:
                    return ToolResult("The user has no saved workflows yet.", structured={"workflows": []})
                return ToolResult("\n".join(describe_workflow(w) for w in listed), structured={"workflows": [
                    {"id": w.get("id"), "name": w.get("name"), "steps": w.get("goal"), "blanks": w.get("blanks") or [],
                     "sameConversation": bool(w.get("sameConversation")), **shape(w),
                     "published": (w.get("published") or {}).get("revision")} for w in listed]})
            if name == "graph_status":
                workflow_id, batch_id = arguments["workflow_id"], arguments["batch_id"]
                wait = self.poll_seconds({"wait_seconds": arguments.get("wait_seconds", 0)}, call, toolset, client)
                if wait is None:
                    return ToolResult("There is no time to read this graph in the current call. Call graph_status "
                                      "again with the same workflow_id and batch_id; do not start the workflow again.",
                                      structured={"workflow_id": workflow_id, "batch_id": batch_id,
                                                  "status": "status_unavailable",
                                                  "status_uri": f"/api/workflows/{workflow_id}/runs"})
                batch = client.wait_batch(workflow_id, batch_id, wait)
                if batch is None:
                    raise ToolError("Mobster cannot find that graph batch in recent history. Inspect the Mobster "
                                    "app; do not restart it to check.")
                state = graph_state(batch)
                return ToolResult(f"Graph batch {batch_id} is {str(state['status']).replace('_', ' ')}.",
                                  structured={"workflow_id": workflow_id, **state,
                                              "status_uri": f"/api/workflows/{workflow_id}/runs"})
            if name.startswith(TOOL_PREFIX):
                found = next((tool for definition, tool in self.workflow_tools() if definition["name"] == name), None)
                if found is None:
                    raise ToolError(f"No workflow is the tool {name} any more. Call list_workflows.")
                values = {k: v for k, v in arguments.items() if k != "wait_seconds"}
                arguments = {"workflow": found["workflowId"], "values": values, **(
                    {"wait_seconds": arguments["wait_seconds"]} if "wait_seconds" in arguments else {})}
            if getattr(toolset, "allow_devices", None) is not None:
                raise ToolError("This server may use only the devices given with --allow-device. Saved workflows "
                                "can route to other phones, so this server cannot start them. Run the workflow in "
                                "the Mobster app.")
            try:
                workflow = client.find_workflow(arguments["workflow"])
            except LookupError as error:
                raise ToolError(str(error)) from None
            thread_id = arguments.get("thread_id")
            self.in_time(name, "workflow run", arguments, call, toolset, client)
            thread_id, answer = self.submit(lambda: client.run_workflow(
                workflow["id"], values=arguments.get("values"), thread_id=thread_id,
                new_conversation=bool(arguments.get("new_conversation"))), "workflow run")
            run_id = (answer.get("run") or {}).get("id")
            wait = self.poll_seconds(arguments, call, toolset, client)
            batch = answer.get("batch") or {}
            if batch.get("graph") == 2:
                # A graph run: follow it to its end, then say how each step ended.
                def graph(seconds):
                    state = graph_state(client.wait_batch(workflow["id"], batch["id"], seconds) or batch)
                    done = sum(1 for step in state["steps"] if step.get("state") == "done")
                    reason = f" {state['reason']}" if state.get("reason") else ""
                    waits = " Approvals wait for the user in the Mobster app." if state["status"] == "running" else ""
                    return ToolResult(f"“{workflow['name']}” {str(state['status']).replace('_', ' ')}: {done} of "
                                      f"{len(state['steps'])} steps done.{reason}{waits}", structured={
                                          "workflow_id": workflow["id"], "thread_id": thread_id, "run_id": run_id,
                                          "status_uri": f"/api/workflows/{workflow['id']}/runs", **state})
                return self.follow(wait, graph, accepted_unavailable(
                    ToolResult, text=f"Mobster accepted the graph workflow, but its status is unavailable. "
                    f"Call graph_status with workflow_id {workflow['id']} and batch_id {batch['id']}; "
                    "do not run it again.", workflow_id=workflow["id"], thread_id=thread_id, run_id=run_id,
                    batch_id=batch["id"], status_uri=f"/api/workflows/{workflow['id']}/runs"))
            if not run_id:
                state = {"status": "running"}
                return ToolResult(f"Started “{workflow['name']}”. " + sentence(state, thread_id or "(none)",
                                                                        origin=self.origin),
                                  structured={"workflow_id": workflow["id"], "thread_id": thread_id,
                                              "run_id": None, **state})

            def task(seconds):
                state = describe(client.wait_run(run_id, seconds))
                return ToolResult(f"Started “{workflow['name']}”. " + sentence(state, thread_id
                                                                               or "(none)", origin=self.origin),
                                  structured={"workflow_id": workflow["id"], "thread_id": thread_id, "run_id": run_id,
                                              **state})
            return self.follow(wait, task, accepted_unavailable(
                ToolResult, text=f"Mobster accepted the workflow, but its status is unavailable. Call "
                f"phone_task_status with run_id {run_id}; do not run it again.",
                workflow_id=workflow["id"], thread_id=thread_id, run_id=run_id,
                status_uri=f"/api/runs/{run_id}"))
        except ServiceError as error:
            raise ToolError(str(error)) from None
        except NoService:
            raise ToolError(NO_SERVICE) from None
