"""`mobster chat "message"` and `mobster chat --workflow`: the scripted path."""

import json
import shlex
import time

from ..narrate import approval_title, completion_message, status_label
from .client import NoService, ServiceError
from .recent import remember_thread

EXIT_DONE, EXIT_NOT_DONE, EXIT_NEEDS_YOU, EXIT_COULDNT, EXIT_RUNNING = 0, 1, 2, 3, 4
DONE = frozenset({"completed", "completed_unverified", "user_condition_met"})


def outcome(run, thread_id):
    summary = run.get("summary") or {}
    data = summary.get("data")
    out = {"threadId": thread_id, "runId": run.get("id"), "status": run.get("status"),
           "outcome": summary.get("outcome"), "answer": summary.get("answer"),
           "costUsd": run.get("costUsd"), "elapsedMs": summary.get("elapsed_ms")}
    if data is not None:
        out["data"] = data
    if run.get("status") not in DONE and summary.get("reason"):
        out["reason"] = summary["reason"]
    return out


def waiting(run, thread_id):
    approval = run.get("approval") or {}
    if approval.get("kind") == "clarify":
        return {"threadId": thread_id, "runId": run.get("id"), "status": "waiting_for_answer",
                "question": approval.get("question") or approval.get("label"),
                "choices": [c.get("label") for c in approval.get("choices") or ()]}
    out = {"threadId": thread_id, "runId": run.get("id"), "status": "waiting_for_approval",
           "approval": {"title": approval_title(approval), "text": approval.get("text"), "app": approval.get("app")}}
    return out


def once(client, chat, text, *, as_json, wait, out, keep=False):
    """`mobster chat "message"`."""
    tty = chat.interactive
    if tty:
        response = chat.send(text)          # prints the task's first line and follows it
        if response is None:
            return EXIT_COULDNT
    else:
        body = {"text": text, **({"device": chat.device} if chat.device else {})}
        try:
            chat.thread_id, response = client.send(chat.thread_id, body)
        except ServiceError as error:
            if as_json:
                print(json.dumps({"status": "refused", "error": str(error), "code": error.code}), file=out,
                      flush=True)
            else:
                chat.say(str(error))
            return EXIT_COULDNT
        remember_thread(client.url, chat.thread_id)
    routed = routed_as(response)
    if routed == "remember":
        return offered(chat, response, tty=tty, as_json=as_json, wait=wait, out=out)
    run_id = (response.get("run") or {}).get("id") or (response.get("item") or {}).get("runId")
    if routed in ("steer", "answer"):
        if as_json:
            print(json.dumps({"threadId": chat.thread_id, "runId": run_id, "routed": routed}), file=out, flush=True)
        else:
            chat.say("  Mobster has your message; the task goes on. Follow it with: mobster chat --thread "
                     f"{chat.thread_id}", "faint")
        return EXIT_DONE
    if not tty:
        # --json, or no terminal: never answers an approval; waits until the task ends or needs someone (with
        # --keep-waiting, until it ends: someone answers in Mobster).
        return finish(client.wait_run(run_id, wait, through=keep), chat.thread_id, as_json, out, chat)
    return follow_here(chat, run_id, wait, out)


def follow_here(chat, run_id, wait, out):
    """On a terminal: the steps as they happen; an approval waits for a typed y, n or x. Returns the exit code."""
    deadline = time.monotonic() + wait
    term = chat.term
    try:
        while time.monotonic() < deadline:
            chat.pump(0.1)
            if bool(chat.pending) != term.prompting:
                term.clear()
                term.prompting = bool(chat.pending)
                term.show()
            done = chat.finished.get(run_id)
            if done is not None:
                return finish(done, chat.thread_id, False, out, chat, printed=True)
            if chat.pending and chat.pending.get("kind") == "clarify":
                chat.say(f"  Answer with: mobster chat --thread {chat.thread_id} \"your answer\"", "faint")
                return EXIT_NEEDS_YOU
            if chat.pending:
                chat.feed(chat.term.keys(0.1) or "")
    except KeyboardInterrupt:
        chat.stop()
        return 130
    chat.say(f"  Still working. Follow it with: mobster chat --thread {chat.thread_id}", "faint")
    return EXIT_RUNNING


def routed_as(response):
    """How Mobster took a message: new_run, steer, answer or remember."""
    return response.get("routed") or (response.get("item") or {}).get("routed") or "new_run"


def proposals_of(client, thread_id, response):
    """The suggestions to remember a message made, [{id, text, pin}]. Where the answer leaves them out (that older
    Mobster), the conversation's pending ones."""
    if "proposals" in response:
        return response.get("proposals") or []
    from urllib.parse import quote
    try:
        listed = client.call("GET", f"/api/memory/proposals?thread={quote(str(thread_id), safe='')}")["proposals"]
    except (ServiceError, NoService, KeyError, TypeError):
        return []
    return [{"id": p.get("id"), "text": p.get("text"), "scope": p.get("scope") or "global",
             "pin": bool(p.get("pin"))} for p in listed
            if isinstance(p, dict)]


def where_to_remember(proposal):
    scope = proposal.get("scope") or "global"
    command = ["mobster", "memory", "add", str(proposal.get("text") or "")]
    if scope != "global":
        command.extend(("--scope", scope))
    return "Answer it in Mobster for Mac, or save it with: " + shlex.join(command)


def offered(chat, response, *, tty, as_json, wait, out):
    """A message that only asked Mobster to remember something: no task started, so there's no run to follow."""
    if not tty:
        proposals = proposals_of(chat.client, chat.thread_id, response)
        if as_json:
            print(json.dumps({"threadId": chat.thread_id, "status": "remember", "proposals": proposals},
                             ensure_ascii=False), file=out, flush=True)
        else:
            chat.offer(proposals)
        return EXIT_DONE
    # chat.send showed the first suggestion already.
    term, deadline = chat.term, time.monotonic() + wait
    try:
        while chat.offers and not chat.quit and time.monotonic() < deadline:
            if not term.prompting:
                term.prompting = True
                term.show()
            data = term.keys(0.1)
            if data is None:
                break
            chat.feed(data)
    except KeyboardInterrupt:
        return 130
    for proposal in chat.offers:
        chat.say("  " + where_to_remember(proposal), "faint")
    chat.offers = []
    return EXIT_DONE


def finish(run, thread_id, as_json, out, chat, printed=False):
    if run.get("finishedAt") is None:
        if run.get("approval"):
            state = waiting(run, thread_id)
            if as_json:
                print(json.dumps(state, ensure_ascii=False), file=out, flush=True)
            else:
                if state["status"] == "waiting_for_answer":
                    chat.note(chat.paint(str(state["question"]), "bold"), glyph="?", role="accent", indent="")
                    chat.say(f"  Answer with: mobster chat --thread {thread_id} \"your answer\"", "faint")
                else:
                    chat.show_approval(run["approval"])
                    chat.note("Waiting for your approval in Mobster (or in `mobster chat` on a terminal).")
            return EXIT_NEEDS_YOU
        if as_json:
            print(json.dumps({"threadId": thread_id, "runId": run.get("id"), "status": "running"}), file=out,
                  flush=True)
        else:
            chat.say(f"Still working. Follow it with: mobster chat --thread {thread_id}", "faint")
        return EXIT_RUNNING
    result = outcome(run, thread_id)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), file=out, flush=True)
    elif not printed:
        chat.say(status_label(result["status"]), "bold")
        message = result.get("data") if isinstance(result.get("data"), str) else result.get("answer")
        if message:
            chat.say(str(message))
        elif result.get("reason"):
            chat.say(completion_message(result["status"], result["reason"], summary=result))
    return EXIT_DONE if result["status"] in DONE else EXIT_NOT_DONE


def blank_values(pairs):
    """{"topic": "basketball"} from ["topic=basketball", ...]. ValueError for a pair without "="."""
    values = {}
    for pair in pairs or ():
        name, sep, value = pair.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"Give each blank as NAME=VALUE, like --set topic=basketball (not {pair!r})")
        values[name.strip()] = value.strip()
    return values


def follow_graph(client, workflow, batch, thread_id, *, as_json, wait, out):
    """A workflow run as a graph: each step's state as it changes, then how the run ended (with --json, one object
    with every step). 0 when it completed, 1 when it didn't, 4 while it still runs."""
    def changed(node):
        if as_json or str(node.get("type", "")).startswith("trigger."):
            return
        line = f"{node.get('instance')}: {node.get('state')}"
        if node.get("reason") and node.get("state") in ("failed", "blocked", "skipped", "unknown", "cancelled"):
            line += f" ({node['reason']})"
        out.write(line + "\n")
    ended = client.wait_batch(workflow["id"], batch["id"], wait, on_node=changed) or batch
    status = ended.get("status")
    if as_json:
        nodes = {key: {k: node[k] for k in ("state", "reason", "output", "runId") if k in node}
                 for key, node in (ended.get("nodes") or {}).items()}
        out.write(json.dumps({"workflow": workflow["id"], "threadId": thread_id, "batch": ended.get("id"),
                              "status": status, "reason": ended.get("reason"), "result": ended.get("result"),
                              "nodes": nodes}) + "\n")
    else:
        out.write(f"“{workflow['name']}” {str(status).replace('_', ' ')}." +
                  (f" {ended['reason']}" if ended.get("reason") else "") + "\n")
    return 0 if status == "completed" else 4 if status == "running" else 1


def workflow_once(client, chat, args, *, as_json, wait, out, fail):
    """`mobster chat --workflow NAME [--set BLANK=VALUE]...`: start it, then follow it as one message."""
    try:
        values = blank_values(args.set)
        workflow = client.find_workflow(args.workflow)
    except ValueError as error:
        return fail(str(error), "usage")
    except LookupError as error:
        return fail(str(error), "not_found")
    try:
        thread_id, answer = client.run_workflow(workflow["id"], values=values, thread_id=chat.thread_id,
                                                new_conversation=bool(args.new))
    except ServiceError as error:
        return fail(str(error), error.code or "refused")
    run = answer.get("run") or {}
    chat.thread_id = thread_id
    if thread_id:
        remember_thread(client.url, thread_id)
    batch = answer.get("batch") or {}
    if batch.get("graph") == 2 and not chat.interactive:
        return follow_graph(client, workflow, batch, thread_id, as_json=as_json, wait=wait, out=out)
    if not run.get("id"):
        # A workflow of steps that waits before its first task: nothing to follow yet. Its conversation has each step.
        batch = answer.get("batch") or {}
        if as_json:
            out.write(json.dumps({"workflow": workflow["id"], "threadId": thread_id, "batch": batch.get("id"),
                                  "status": batch.get("status")}) + "\n")
        else:
            out.write(f"Started “{workflow['name']}”. It waits before its first task; its conversation shows each "
                      "step.\n")
        return 0
    if not chat.interactive:
        keep = bool(getattr(args, "keep_waiting", False))
        return finish(client.wait_run(run["id"], wait, through=keep), thread_id, as_json, out, chat)
    where = run.get("deviceName")
    chat.renderer(run["id"]).start(run.get("goal") or workflow["name"], f"on {where}" if where else "")
    chat.follow(run["id"])
    return follow_here(chat, run["id"], wait, out)
