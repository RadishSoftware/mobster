"""Vague tasks on the phone: a long task (mobile_agent/longform) on Smart, measured.

  python -m mobile_agent.bench.vague_run --task feed.ai --out DIR [--wda-url URL]

Each attempt starts a LongSession the way the Runtime does (longform.session.start), runs Smart with it, writes the
report, and records what the vague-task benchmark scores: items a minute of phone time, $ for 10 minutes of phone
time, the notebook's counts, and the report. Safety, as for every bench run on the owner's phone:

- every action passes the bench safety monitor (SmartGuardedDriver) for a read-only task in the task's app, which
  refuses a send, post, like, follow, delete and the like before it reaches the phone;
- approvals are forced on and declined: a long run asks before every act that asks, and the answer here is no.

The report, the notebook and the frames hold what the phone showed (personal content): they stay in ``--out`` (under
frontier/runs), never in git.
"""

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

# Vague tasks: (start app bundle, the person's request). Read-only: nothing here asks to act.
TASKS = {
    "feed.ai": ("com.burbn.instagram", "Watch 40 Instagram Reels about AI and tell me what you learned"),
    "feed.basketball": ("com.burbn.instagram", "Watch 40 Instagram Reels about basketball "
                        "and tell me what you learned"),
    "feed.cooking.tiktok": ("com.zhiliaoapp.musically", "Watch 40 TikToks about cooking and tell me what you learned"),
    "feed.shorts.tech": ("com.google.ios.youtube", "Watch 40 YouTube Shorts about tech and tell me what you learned"),
    # An inbox sweep (longform/inbox.py): reads conversations and drafts replies for review; nothing is sent.
    "inbox.messages": ("com.apple.MobileSMS", "Go through my messages and draft replies to anything that needs one"),
}
# The held-out part (vague-task milestone, 10 Oct): frozen in vague-heldout-manifest.json before any of them runs, and
# never tuned on. Other topics and other inboxes than the tasks above.
HELDOUT = {
    "h.feed.travel": ("com.burbn.instagram", "Watch 30 Instagram Reels about travel and tell me what you learned"),
    "h.feed.fitness.tiktok": ("com.zhiliaoapp.musically", "Watch 30 TikToks about fitness "
                              "and tell me what you learned"),
    "h.shorts.science": ("com.google.ios.youtube", "Watch 30 YouTube Shorts about science "
                         "and tell me what you learned"),
    "h.inbox.instagram": ("com.burbn.instagram", "Go through my Instagram DMs and draft replies "
                          "to anything that needs one"),
    "h.inbox.gmail": ("com.google.Gmail", "Go through my unread emails and draft replies to "
                      "the ones that need an answer"),
}
HELDOUT_MANIFEST = Path(__file__).with_name("vague-heldout-manifest.json")


def heldout_hash(tasks=None):
    """sha256 of the held-out tasks as frozen (sorted JSON)."""
    import hashlib
    tasks = HELDOUT if tasks is None else tasks
    return hashlib.sha256(json.dumps({k: list(v) for k, v in sorted(tasks.items())}, sort_keys=True)
                          .encode()).hexdigest()


def task_for(task_id):
    """(bundle, request) of a tuning or held-out task; a held-out one only while it matches its frozen manifest."""
    if task_id in TASKS:
        return TASKS[task_id]
    frozen = json.loads(HELDOUT_MANIFEST.read_text())
    if frozen.get("sha256") != heldout_hash():
        raise RuntimeError("the held-out vague tasks changed after they were frozen")
    return HELDOUT[task_id]


# Haiku plans and extracts (the engine with a Claude key); Sonnet writes the report (longform/models.py).
RUN_ENV = {"MOBSTER_SMART_MODEL": "claude-haiku-5-5"}
CAP_USD = 1.50


class Ledger:
    """The run's spend from its inference events, as the Runtime's ledger keeps it."""

    def __init__(self):
        self.cost_nanodollars = 0
        self.by_purpose = {}
        self.calls = {}

    def add(self, event):
        if event.get("event") != "inference_finished":
            return
        nano = event.get("cost_nanodollars")
        if nano is None:
            nano = round((event.get("estimated_usd") or 0) * 1e9)
        purpose = event.get("purpose") or "other"
        self.cost_nanodollars += int(nano or 0)
        self.by_purpose[purpose] = self.by_purpose.get(purpose, 0) + int(nano or 0)
        self.calls[purpose] = self.calls.get(purpose, 0) + 1


def decline(request):
    """Every approval is answered no: nothing that asks happens in a bench run."""
    return "denied"


def wda_session(wda_url):
    import urllib.request
    with urllib.request.urlopen(wda_url.rstrip("/") + "/status", timeout=5) as reply:
        return (json.loads(reply.read() or b"{}") or {}).get("sessionId") or ""


def run_one(task_id, out, wda_url, model=None):
    from ..catalog import app_label
    from ..compose import build_target_driver, close_all
    from ..costs import meter
    from ..engines import build_client, build_frontier, smart_key, smart_model
    from ..journal import Lease
    from ..longform import plan as long_plan
    from ..longform import session as long_session
    from ..longform.synthesis import synthesize
    from ..smart_result import summarize
    from ..wda.frame_clock import attach_frame_clock
    from .agents.smart import SmartGuardedDriver
    from .safety import Monitor
    from .suite import READ_ONLY

    bundle, goal = task_for(task_id)
    out.mkdir(parents=True, exist_ok=True)
    saved = {key: os.environ.get(key) for key in RUN_ENV}
    os.environ.update(RUN_ENV)
    ledger, events = Ledger(), []
    started = time.monotonic()

    def emit(event):
        ledger.add(event)
        if event.get("event") not in ("frontier_prompt",):
            events.append({**{k: v for k, v in event.items() if k != "snapshot"},
                           "t_ms": round((time.monotonic() - started) * 1000, 1)})

    task = SimpleNamespace(bundles=(bundle,), bundle=bundle, safety=READ_ONLY, allow=(), dry_run=False)
    monitor = Monitor(task)
    ctx = SimpleNamespace(started=started, deadline=started + 1200,
                          elapsed_ms=lambda: (time.monotonic() - started) * 1000)
    from .agents.base import Recorder
    recorder = Recorder(ctx)
    lease = driver = None
    result = {"task": task_id, "goal": goal}
    try:
        lease = Lease.device(wda_url)
        session_id = wda_session(wda_url)
        driver = build_target_driver(wda_url=wda_url, session=session_id)
        attach_frame_clock(driver, wda_url=wda_url, session=session_id)
        driver.call("POST", "/wda/apps/activate", {"bundleId": bundle})
        guarded = SmartGuardedDriver(driver, monitor, recorder)
        model = model or smart_model()
        client = meter(build_client(key=smart_key()), emit)
        name = app_label(bundle).split(" (")[0]
        card = long_plan.make_plan(goal, apps=({"bundleId": bundle, "name": name},), limit_usd=CAP_USD,
                                   nav_model=getattr(client, "model", model))
        if card is None:
            raise RuntimeError("the request was not detected as a long task")
        run_id = "bench-" + uuid.uuid4().hex[:10]
        session = long_session.start(run_id=run_id, goal=goal, plan=card, budget=long_plan.budget_of(card),
                                     origin="bench", nav_client=client, key=smart_key(), inference=emit,
                                     ledger=ledger, cap_usd=CAP_USD, emit=emit)
        # The long run's tools, as the Runtime adds them (longform/register.py: WATCH_FEED, READ_EACH).
        from ..frontier import default_skills
        from ..longform import feed, items
        tools = tuple(t for t in (feed.factory(SimpleNamespace(run_id=run_id)),
                                  items.factory(SimpleNamespace(run_id=run_id))) if t is not None)
        agent = build_frontier(guarded, client, apps={bundle: name}, emit=emit, max_cost_usd=CAP_USD,
                               approve=decline, cancelled=lambda: monitor.stopped, longform=session,
                               forced_approve=decline, skills=tuple(default_skills()) + tools)
        outcome = agent.run(f"In {name}: {goal}")
        session.release_phone()
        status = outcome.get("status") or "error"
        report, markdown = synthesize(session, status, answer=outcome.get("answer"))
        from ..longform import inbox
        if inbox.wants_drafts(goal):
            markdown = (markdown or "").rstrip() + "\n\n" + inbox.drafts_markdown(session, app_names={bundle: name})
        summary = summarize(outcome, agent, None, request=goal, report=report, markdown=markdown)
        counters = dict(session.notebook.counters)
        phone_s = float(session.phone_seconds() or 0.0)
        watched = counters.get("kept", 0) + counters.get("off_topic", 0)
        spent = session.spent_usd()
        result.update({
            "status": status, "model": getattr(client, "model", model), "phone_seconds": round(phone_s, 1),
            "wall_seconds": round(time.monotonic() - started, 1), "counters": counters,
            "items_per_minute": round(watched / (phone_s / 60), 1) if phone_s > 0 else None,
            "usd": spent, "usd_per_10_min": round(spent / phone_s * 600, 4) if phone_s > 0 else None,
            "usd_by_purpose": {k: round(v / 1e9, 5) for k, v in ledger.by_purpose.items()},
            "calls_by_purpose": dict(ledger.calls),
            "unsafe": monitor.unsafe if hasattr(monitor, "unsafe") else None,
            "blocked": monitor.stopped, "reason": str(summary.get("reason") or "")[:300],
            "inbox": inbox_counts(session)})
        (out / "report.md").write_text(markdown or "")
        (out / "report.json").write_text(json.dumps(report, default=str, indent=1))
        (out / "notebook.json").write_text(json.dumps(session.notebook.to_dict() if hasattr(
            session.notebook, "to_dict") else {}, default=str))
    except Exception as error:  # noqa: BLE001 -- recorded as an infra failure
        result.update({"status": "error", "reason": f"{type(error).__name__}: {error}"[:300]})
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        close_all(driver)
        if lease is not None:
            lease.close()
    result["monitor"] = monitor.events[-50:]
    (out / "events.jsonl").write_text("".join(json.dumps(e, default=str) + "\n" for e in events))
    (out / "result.json").write_text(json.dumps(result, default=str, indent=1))
    return result


def inbox_counts(session):
    """An inbox sweep's numbers: conversations read, how many need a reply, drafts written (review only: none sent)."""
    from ..longform.inbox import drafts, wants_drafts
    if not wants_drafts(getattr(session, "request", "")):
        return None
    read = [item for item in getattr(session.notebook, "items", ()) or () if item.kind in ("chat", "row")]
    return {"read": len(read), "needs_reply": sum(bool((c.extract or {}).get("needs_reply")) for c in read),
            "drafts": len(drafts(session)), "sent": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m mobile_agent.bench.vague_run")
    parser.add_argument("--task", required=True, choices=sorted(TASKS) + sorted(HELDOUT))
    parser.add_argument("--out", required=True)
    parser.add_argument("--wda-url", default="http://127.0.0.1:8100")
    parser.add_argument("--secrets-env")
    parser.add_argument("--env-file")
    args = parser.parse_args(argv)
    from ..config import load_env_file
    if args.env_file:
        load_env_file(args.env_file)
    if args.secrets_env:
        for line in Path(args.secrets_env).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip().strip('"').strip("'")
    result = run_one(args.task, Path(args.out), args.wda_url)
    shown = {k: v for k, v in result.items() if k not in ("monitor", "goal")}
    print(json.dumps(shown, default=str, indent=1))
    return 0 if result.get("status") not in ("error",) else 1


if __name__ == "__main__":
    raise SystemExit(main())
