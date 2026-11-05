"""One task on several phones at once: one run per phone through Runtime.create, shown and summarized as one task."""

import contextlib
import ctypes
import ctypes.util
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .api_errors import APIError

log = logging.getLogger("mobster.fanout")

MAX_PHONES = 100
SOFT_LIMIT = 10
CPU_HOLD = 85.0
HOLD_MAX_S = 300.0
HOLD_POLL_S = 1.0
HOLD_STAGGER_S = 1.0
KEEP = 200
GROUP_ID = re.compile(r"[a-f0-9]{12}")
STORE = "groups.json"
DONE = frozenset({"completed"})

HELD = "Your Mac is busy (CPU {cpu:.0f}%). Mobster starts the other phones when it drops below {limit:.0f}%."
GAVE_UP = "Your Mac stayed busy, so this phone didn't start. Nothing ran on it. Try again when the Mac is less busy."
SOFT = ("{count} tasks at once is more than {limit}. Each phone streams its screen and sends screenshots, so a busy "
        "Mac or USB hub can slow every task down.")


class MacLoad:
    MIN_GAP = 0.5

    def __init__(self):
        self.lock = threading.Lock()
        self.last = None
        self.value = None
        self._host = self._libc = None
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("System") or "/usr/lib/libSystem.B.dylib")
            libc.mach_host_self.restype = ctypes.c_uint
            self._host, self._libc = libc.mach_host_self(), libc
        except (OSError, AttributeError):
            pass

    def ticks(self):
        if self._libc is not None:
            info = (ctypes.c_uint * 4)()     # user, system, idle, nice (HOST_CPU_LOAD_INFO)
            count = ctypes.c_uint(4)
            if self._libc.host_statistics(self._host, 3, ctypes.byref(info), ctypes.byref(count)) == 0:
                user, system, idle, nice = list(info)
                return user + system + nice, user + system + idle + nice
        try:
            with open("/proc/stat") as stream:
                values = [int(part) for part in stream.readline().split()[1:8]]
            idle = values[3] + values[4]
            return sum(values) - idle, sum(values)
        except (OSError, ValueError, IndexError):
            return None

    def percent(self):
        with self.lock:
            now = time.monotonic()
            if self.last is not None and now - self.last[0] < self.MIN_GAP and self.value is not None:
                return self.value
            ticks = self.ticks()
            if ticks is None:
                try:
                    self.value = min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100)
                except OSError:
                    self.value = 0.0
                return self.value
            if self.last is None:
                # The first read has nothing to compare with: the load average stands in.
                self.last = (now, ticks)
                try:
                    self.value = round(min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100), 1)
                except OSError:
                    self.value = 0.0
                return self.value
            first = self.last[1]
            busy = (ticks[0] - first[0]) % 2 ** 32 if ticks[0] < first[0] else ticks[0] - first[0]
            total = (ticks[1] - first[1]) % 2 ** 32 if ticks[1] < first[1] else ticks[1] - first[1]
            if total > 0:
                self.value = round(100.0 * busy / total, 1)
            elif self.value is None:
                self.value = 0.0
            self.last = (time.monotonic(), ticks)
            return self.value


BULLET = re.compile(r"^\s*(?:[-*•·–]|\d{1,3}[.)])\s+")
TRIM = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)


def _key(line):
    return TRIM.sub("", " ".join(BULLET.sub("", line).split()).casefold())


def answer_of(summary):
    summary = summary if isinstance(summary, dict) else {}
    data = summary.get("data")
    if isinstance(data, str) and data.strip():
        return data.strip()
    full = summary.get("full_answer") or summary.get("answer")
    return full.strip() if isinstance(full, str) and full.strip() else None


def merge_results(results):
    answers, lines, order, rows, seen_rows, tables = [], {}, [], [], set(), True
    completed, cost, priced = 0, 0.0, False
    for item in results:
        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
        status = item.get("status") or summary.get("status") or "error"
        text = answer_of(summary)
        answers.append({"device": item.get("device"), "deviceName": item.get("deviceName"),
                        **({"profile": item["profile"], "profileName": item.get("profileName")}
                           if item.get("profile") else {}), "status": status,
                        "answer": text[:3000] if text else None,
                        "reason": None if text or status in DONE else (str(summary.get("reason") or "")[:500] or None)})
        if isinstance(item.get("costUsd"), (int, float)):
            cost, priced = cost + float(item["costUsd"]), True
        if status not in DONE:
            continue
        completed += 1
        data = summary.get("data")
        if isinstance(data, list) and all(isinstance(row, dict) for row in data):
            for row in data:
                key = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
                if key not in seen_rows:
                    seen_rows.add(key)
                    rows.append(row)
        else:
            tables = False
        for line in (text or "").splitlines():
            key = _key(line)
            # A heading ("Your notes:") introduces each phone's list; the lines under it are what they found.
            if not key or line.rstrip().endswith(":"):
                continue
            if key not in lines:
                lines[key] = {"text": BULLET.sub("", line).strip(), "phones": []}
                order.append(key)
            if item.get("device") not in lines[key]["phones"]:
                lines[key]["phones"].append(item.get("device"))
    return {"phones": len(results), "completed": completed, "answers": answers,
            "lines": [lines[key] for key in order][:400], "rows": rows[:1000] if tables and completed else None,
            "costUsd": round(cost, 6) if priced else None}


PART_STATUS = re.compile(r"[a-z_]{1,32}")
MAX_PART_ROWS = 1000


def merge_parts(parts):
    """One summary for a task run as one group on each of several Macs (the console's fan-out across Macs): ``parts``
    are the groups' summaries as the console shows them, phones by team id ({answers: [{device, deviceName, status,
    answer, reason}], rows, costUsd}). The answers are merged as ``merge_results`` merges one Mac's phones; the rows
    are every part's once, when every part has rows; the cost is the sum of the parts' known costs."""
    results, rows, seen, tables, cost, priced = [], [], set(), True, 0.0, False
    for part in parts:
        for item in part["answers"]:
            summary = {"status": item["status"], "answer": item.get("answer"), "reason": item.get("reason")}
            results.append({"device": item["device"], "deviceName": item.get("deviceName"), "status": item["status"],
                            "summary": summary})
        if isinstance(part.get("rows"), list):
            for row in part["rows"]:
                key = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
        else:
            tables = False
        if isinstance(part.get("costUsd"), (int, float)):
            cost, priced = cost + float(part["costUsd"]), True
    merged = merge_results(results)
    merged["rows"] = rows[:MAX_PART_ROWS] if tables and merged["completed"] else None
    merged["costUsd"] = round(cost, 6) if priced else None
    return merged


def new_id():
    return uuid.uuid4().hex[:12]


def retained_groups(groups):
    finished = [group for group in groups if group.get("finishedAt") is not None]
    kept = {group["id"] for group in finished[-KEEP:]}
    return [group for group in groups if group.get("finishedAt") is None or group["id"] in kept]


class GroupStore:
    def __init__(self, folder):
        self.path = Path(folder) / STORE if folder else None

    def read(self):
        if self.path is None:
            return []
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        groups = data.get("groups") if isinstance(data, dict) else None
        return [group for group in groups or [] if isinstance(group, dict)
                and isinstance(group.get("id"), str) and GROUP_ID.fullmatch(group["id"])
                and isinstance(group.get("members"), list)]

    def write(self, groups):
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".groups.", suffix=".json", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps({"version": 1, "groups": retained_groups(groups)}, ensure_ascii=False))
            os.replace(temporary, self.path)
        except OSError:
            log.warning("mobster: could not save the task groups")
            with contextlib.suppress(OSError, UnboundLocalError):
                os.unlink(temporary)


class Fanout:
    def __init__(self, runtime, load=None):
        self.runtime = runtime
        state_db = getattr(runtime.config, "state_db", None)
        self.store = GroupStore(Path(state_db).parent if isinstance(state_db, str) and state_db else None)
        self.lock = threading.RLock()
        self.groups = {group["id"]: group for group in self.store.read()}
        self.keys = {group["key"]: group["id"] for group in self.groups.values() if group.get("key")}
        self.load = load or MacLoad()
        self.wake = threading.Event()
        self.dispatcher = None
        self.closed = False
        # Phones held by a group that was interrupted by a restart never start later: say so.
        for group in list(self.groups.values()):
            for member in group["members"]:
                if member.get("state") in ("held", "pending"):
                    member.update(state="refused", code="interrupted",
                                  error="Mobster restarted before this phone's task was confirmed. "
                                        "Inspect the phone before starting another task; it was not retried.")
            self.finish_if_done(group, notify=False)

    def running(self):
        with self.runtime.lock:
            return len(self.runtime.active_runs)

    def limits(self):
        cpu = self.load.percent()
        return {"running": self.running(), "softLimit": SOFT_LIMIT, "cpuPercent": cpu, "holdAbovePercent": CPU_HOLD,
                "holding": cpu > CPU_HOLD, "maxPhones": MAX_PHONES}

    def get(self, group_id):
        with self.lock:
            group = self.groups.get(group_id)
        if group is None:
            raise APIError("That task group doesn't exist here.", 404, "group_not_found")
        return group

    def has_request(self, key):
        with self.lock:
            return key in self.keys

    def public(self, group):
        runs = self.runtime.runs
        members, counts = [], {"phones": 0, "running": 0, "waiting": 0, "completed": 0, "ended": 0, "refused": 0,
                               "held": 0}
        for member in group["members"]:
            run = runs.get(member.get("runId")) if member.get("runId") else None
            entry = {key: member.get(key) for key in ("device", "deviceName", "runId", "state", "error", "code")}
            if member.get("profile"):
                entry.update(profile=member["profile"], profileName=member.get("profileName"))
            approval = None
            if run is not None:
                with run.condition:
                    approval = dict(run.approval) if run.approval else None
                entry.update(status=run.status, finishedAt=run.finished_at * 1000 if run.finished_at else None,
                             costUsd=run.cost_usd, approval=approval, estimateUsd=run.estimate_usd)
            else:
                entry.update(status=None, finishedAt=None, costUsd=member.get("costUsd"), approval=None,
                             estimateUsd=None)
            counts["phones"] += 1
            if member["state"] == "refused":
                counts["refused"] += 1
            elif member["state"] == "held":
                counts["held"] += 1
            elif run is not None and run.finished_at is None:
                counts["waiting" if approval else "running"] += 1
            elif run is not None and run.status in DONE or member.get("status") in DONE:
                counts["completed"] += 1
            else:
                counts["ended"] += 1
            members.append(entry)
        state = ("waiting" if counts["waiting"] else "running" if counts["running"] else "held" if counts["held"]
                 else "finished" if group.get("finishedAt") is not None else "running")
        estimates = [item["estimateUsd"] for item in members if isinstance(item.get("estimateUsd"), (int, float))]
        estimate = group.get("estimateUsd") if group.get("estimateUsd") is not None else (
            max(estimates) if estimates else None)
        started = sum(1 for item in members if item["state"] != "refused")
        return {"id": group["id"], "goal": group["goal"], "appId": group.get("appId"), "engine": group.get("engine"),
                "origin": group.get("origin"), "threadId": group.get("threadId"), "createdAt": group["createdAt"],
                "finishedAt": group.get("finishedAt"), "state": state, "counts": counts, "members": members,
                "warning": group.get("warning"), "held": group.get("held"),
                "estimateUsd": estimate,
                "estimateTotalUsd": round(estimate * started, 6) if estimate is not None else None,
                "summary": group.get("summary"),
                **({"teamGroup": group["teamGroup"]} if group.get("teamGroup") else {})}

    def recent(self, limit=20):
        with self.lock:
            groups = sorted(self.groups.values(), key=lambda group: group["createdAt"], reverse=True)[:limit]
        return [self.public(group) for group in groups]

    def resolve(self, phones):
        if not isinstance(phones, list) or not 1 <= len(phones) <= MAX_PHONES:
            raise ValueError(f"phones is a list of 1 to {MAX_PHONES} devices (their ids, UDIDs or names)")
        if not all(isinstance(item, str) and 1 <= len(item.strip()) <= 128 for item in phones):
            raise ValueError("Name each phone by its id, UDID or name")
        fleet = getattr(self.runtime, "fleet", None)
        if fleet is None or getattr(self.runtime, "pool", None) is not None:
            raise ValueError("This Mobster runs every task on its own device; leave phones out")
        resolved, seen = [], set()
        for name in phones:
            phone = self.runtime.resolve_device(name)
            if phone.id in seen:
                continue
            seen.add(phone.id)
            resolved.append(phone)
        return resolved

    def start(self, phones, app_id, goal, *, idempotency_key=None, fingerprint=None, thread_id=None, team_group=None,
              **create):
        """Start ``goal`` on every phone of ``phones``: (group, replayed). ``team_group``: the console's id for the
        same task on its other Macs' phones (12 hex), kept on the group."""
        if team_group is not None and (not isinstance(team_group, str) or not GROUP_ID.fullmatch(team_group)):
            raise ValueError("teamGroup is 12 lowercase hex characters")
        resolved = self.resolve(phones)
        members = [{"device": phone.id, "deviceName": phone.name, "runId": None, "state": "pending",
                    "error": None, "code": None} for phone in resolved]
        return self._open(members, app_id, goal, idempotency_key, fingerprint, thread_id, create, team_group=team_group)

    def start_profiles(self, profiles, app_id, goal, *, idempotency_key=None, fingerprint=None, thread_id=None,
                       **create):
        # Profiles that share a phone run in turn. Nothing is retried: an unknown outcome is that profile's result.
        resolved = self.resolve_profiles(profiles)
        members, first_on = [], {}
        for account in resolved:
            device = account.get("device")
            member = {"device": device, "deviceName": account.get("deviceName"), "profile": account["id"],
                      "profileName": account.get("handle"), "runId": None, "state": "pending", "error": None,
                      "code": None}
            if not device:
                member.update(state="refused", code="no_phone",
                              error=f"@{str(account.get('handle') or '').lstrip('@')} "
                              "has no phone yet. Choose its phone on the Profiles page, then run it again.")
            elif device in first_on:
                member["state"] = "queued"  # starts when the profile ahead of it on this phone has finished
            else:
                first_on[device] = member
            members.append(member)
        return self._open(members, app_id, goal, idempotency_key, fingerprint, thread_id, create)

    def resolve_profiles(self, profiles):
        from .accounts.store import AccountNotFound, AmbiguousAccount, resolve, store_for
        stored = store_for(self.runtime).all()
        if profiles == "all":
            chosen = list(stored)
        else:
            if not isinstance(profiles, list) or not 1 <= len(profiles) <= MAX_PHONES or not all(
                    isinstance(item, str) and 1 <= len(item.strip()) <= 128 for item in profiles):
                raise ValueError(f"profiles is \"all\", or a list of 1 to {MAX_PHONES} profiles (their ids or handles)")
            chosen = []
            for name in profiles:
                try:
                    chosen.append(resolve(stored, name))
                except (AccountNotFound, AmbiguousAccount) as error:
                    raise ValueError(str(error)) from None
        seen, out = set(), []
        for account in chosen:
            if account["id"] not in seen:
                seen.add(account["id"])
                out.append(account)
        if not out:
            raise ValueError("There are no profiles yet. Add one on the Profiles page, then run it on every profile.")
        return out[:MAX_PHONES]

    def _open(self, members, app_id, goal, idempotency_key, fingerprint, thread_id, create, *, team_group=None):
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 4000:
            raise ValueError("Choose an app, a task (1–4000 characters), and demo/live mode")
        resolved = members
        with self.lock:
            if idempotency_key:
                prior = self.keys.get(idempotency_key)
                if prior is not None and prior in self.groups:
                    if self.groups[prior].get("fingerprint") != fingerprint:
                        raise APIError("This request key belongs to a different task", code="idempotency_conflict")
                    return self.groups[prior], True
            running = self.running()
            group = {"id": new_id(), "key": idempotency_key, "fingerprint": fingerprint, "goal": goal.strip(),
                     "appId": app_id, "engine": create.get("engine"), "origin": create.get("origin", "api"),
                     "threadId": thread_id, "createdAt": time.time() * 1000, "finishedAt": None, "summary": None,
                     **({"teamGroup": team_group} if team_group else {}),
                     "warning": SOFT.format(count=running + len(resolved), limit=SOFT_LIMIT)
                     if running + len(resolved) > SOFT_LIMIT else None, "held": None, "estimateUsd": None,
                     "members": members,
                     "create": {"app_id": app_id, "goal": goal.strip(), **{key: value for key, value in create.items()
                                                                           if key in RUN_KEYS}}}
            self.groups[group["id"]] = group
            if idempotency_key:
                self.keys[idempotency_key] = group["id"]
        cpu = self.load.percent()
        if cpu > CPU_HOLD:
            with self.lock:
                for member in group["members"]:
                    if member["state"] == "pending":
                        member["state"] = "held"
                if any(member["state"] == "held" for member in group["members"]):
                    group["held"] = {"cpuPercent": cpu, "since": time.time() * 1000,
                                     "reason": HELD.format(cpu=cpu, limit=CPU_HOLD)}
        else:
            for member in group["members"]:
                self._admit(group, member)
        with self.lock:
            self.save()
        if not any(member["state"] in ("started", "held", "queued") for member in group["members"]):
            # Nothing started: fail as a one-phone start would, with the first refusal.
            with self.lock:
                self.groups.pop(group["id"], None)
                if idempotency_key:
                    self.keys.pop(idempotency_key, None)
                self.save()
            first = group["members"][0]
            raise APIError(first.get("error") or "None of those phones could start this task.", 409,
                           first.get("code") or "group_refused",
                           refused=[{key: member.get(key) for key in ("device", "deviceName", "error", "code")}
                                    for member in group["members"]])
        self.notify(group, "created")
        if group["held"] is not None:
            self._dispatch()
        return group, False

    def _admit_queued(self, group, member):
        with self.lock:
            if member["state"] != "queued":
                return
            member["state"] = "pending"
        self._admit(group, member)
        with self.lock:
            self.save()
        self.notify(group, "updated")

    def _admit(self, group, member):
        # Stop uses this same lock: it either refuses this member before admission or sees its new run id.
        with self.lock:
            if self.closed or member["state"] not in ("pending", "held"):
                return None
            create = dict(group["create"])
            if member.get("profile"):
                # A task as this profile: its own phone, the profile confirmed on screen before it commits.
                create["extras"] = {**(create.get("extras") or {}), "account": member["profile"]}
                create["engine"] = create.get("engine") or "smart"
            try:
                run = self.runtime.create(create.pop("app_id"), create.pop("goal"), "live", device=member["device"],
                                          group_id=group["id"], **create)
            except APIError as error:
                member.update(state="refused", error=str(error), code=error.code)
                return None
            except ValueError as error:
                member.update(state="refused", error=str(error), code="invalid")
                return None
            run = run[0] if isinstance(run, tuple) else run
            member.update(state="started", runId=run.id, deviceName=run.device_name or member.get("deviceName"),
                          error=None, code=None)
            if group.get("estimateUsd") is None and run.estimate_usd is not None:
                group["estimateUsd"] = run.estimate_usd
            return run

    def _dispatch(self):
        self.wake.set()
        with self.lock:
            if self.dispatcher is not None and self.dispatcher.is_alive():
                return
            self.dispatcher = threading.Thread(target=self._held_loop, name="mobster-fanout-held", daemon=True)
            self.dispatcher.start()

    def _held_loop(self):
        while not self.closed:
            with self.lock:
                waiting = [(group, member) for group in self.groups.values() for member in group["members"]
                           if member["state"] == "held"]
                if not waiting:
                    self.dispatcher = None
                    return
            group, member = waiting[0]
            held = group.get("held") or {}
            since = held.get("since") or time.time() * 1000
            if time.time() * 1000 - since > HOLD_MAX_S * 1000:
                with self.lock:
                    if member["state"] == "held":
                        member.update(state="refused", code="mac_busy", error=GAVE_UP)
                self._settle(group)
                continue
            cpu = self.load.percent()
            if cpu <= CPU_HOLD and not getattr(self.runtime, "closing", False):
                self._admit(group, member)
                self._settle(group)
                # The next phone waits a moment, so the CPU read before it sees this one's start.
                self.wake.clear()
                self.wake.wait(HOLD_STAGGER_S)
                continue
            with self.lock:
                if group.get("held") is not None:
                    group["held"].update(cpuPercent=cpu, reason=HELD.format(cpu=cpu, limit=CPU_HOLD))
            self.wake.clear()
            self.wake.wait(HOLD_POLL_S)

    def _settle(self, group):
        with self.lock:
            if not any(member["state"] == "held" for member in group["members"]):
                group["held"] = None
            self.save()
        self.notify(group, "updated")
        self.finish_if_done(group)

    def stop(self, group_id):
        group = self.get(group_id)
        with self.lock:
            for member in group["members"]:
                if member["state"] in ("held", "pending", "queued"):
                    member.update(state="refused", code="stopped", error="Stopped before it started on this phone.")
        for member in group["members"]:
            if member.get("runId") and member["runId"] in self.runtime.runs:
                with contextlib.suppress(APIError):
                    self.runtime.stop_run(member["runId"])
        with self.lock:
            group["held"] = None
            self.save()
        self.notify(group, "updated")
        self.finish_if_done(group)
        return group

    def answer_approvals(self, group_id, approve, approvals):
        # Only an approval still waiting with exactly the id the person saw is answered.
        group = self.get(group_id)
        if type(approve) is not bool:
            raise ValueError("approve is true or false")
        if not isinstance(approvals, dict) or not 1 <= len(approvals) <= MAX_PHONES:
            raise ValueError("approvals maps each phone's run id to the approval id that was shown")
        members = {member.get("runId"): member for member in group["members"] if member.get("runId")}
        requests = []
        for run_id, approval_id in approvals.items():
            if run_id not in members or not isinstance(approval_id, str):
                raise ValueError("Each approval belongs to one of this group's runs")
            run = self.runtime.runs.get(run_id)
            if run is not None:
                with run.condition:
                    pending = dict(run.approval) if run.approval else None
                if pending is not None and pending.get("id") == approval_id and pending.get("kind") == "clarify":
                    raise ValueError("Mobster's questions are answered one at a time, on their phone's tab")
            requests.append((run_id, approval_id, run))
        # Validate the whole request before answering any card: a later invalid entry must not approve an earlier one.
        out = []
        for run_id, approval_id, run in requests:
            answered = bool(run is not None and run.answer_approval(approval_id, approve))
            out.append({"runId": run_id, "device": members[run_id].get("device"), "answered": answered,
                        "decision": None if answered or run is None else run.approval_decision(approval_id)})
        return out

    def member_finished(self, run):
        group_id = getattr(run, "group_id", None)
        if not group_id:
            return
        with self.lock:
            group = self.groups.get(group_id)
            if group is None:
                return
            following = None
            for member in group["members"]:
                if member.get("runId") == run.id:
                    member.update(status=run.status, costUsd=run.cost_usd)
                    following = next((other for other in group["members"] if other["state"] == "queued"
                                      and other.get("device") == member.get("device")), None)
            self.save()
        if following is not None:
            self._admit_queued(group, following)
        if not self.finish_if_done(group):
            self.notify(group, "updated")

    def finish_if_done(self, group, *, notify=True):
        runs = self.runtime.runs
        with self.lock:
            if group.get("finishedAt") is not None:
                return False
            for member in group["members"]:
                if member["state"] in ("held", "pending", "queued"):
                    return False
                run = runs.get(member.get("runId")) if member.get("runId") else None
                if run is not None and run.finished_at is None:
                    return False
            results = []
            for member in group["members"]:
                run = runs.get(member.get("runId")) if member.get("runId") else None
                results.append({"device": member["device"], "deviceName": member.get("deviceName"),
                                "profile": member.get("profile"), "profileName": member.get("profileName"),
                                "status": run.status if run is not None else ("refused" if member["state"] == "refused"
                                                                              else member.get("status")),
                                "summary": (run.summary if run is not None else None) or (
                                    {"status": "refused", "reason": member.get("error")}
                                    if member["state"] == "refused" else {}),
                                "costUsd": run.cost_usd if run is not None else member.get("costUsd")})
            group["summary"] = merge_results(results)
            group["finishedAt"] = time.time() * 1000
            self.save()
        if notify:
            self.notify(group, "finished")
        return True

    def notify(self, group, event):
        self.runtime.publish("groups", {"event": f"group_{event}", "groupId": group["id"],
                                        **({"threadId": group["threadId"]} if group.get("threadId") else {})})
        public = self.public(group)
        for listener in list(LISTENERS):
            try:
                listener(self.runtime, public, event)
            except Exception:  # noqa: BLE001 -- a listener never changes the group
                log.exception("a group listener failed")

    def save(self):
        # Call with ``lock`` held. A held run's ``create`` options stay in memory only.
        ordered = sorted(self.groups.values(), key=lambda group: group["createdAt"])
        retained = retained_groups(ordered)
        if len(retained) < len(ordered):
            kept = {group["id"] for group in retained}
            for old in ordered:
                if old["id"] in kept:
                    continue
                self.groups.pop(old["id"], None)
                if old.get("key"):
                    self.keys.pop(old["key"], None)
        self.store.write([{key: value for key, value in group.items() if key != "create"} for group in retained])

    def close(self):
        self.closed = True
        self.wake.set()


RUN_KEYS = frozenset({"output_schema", "output_format", "allowed_bundles", "engine", "extras", "origin"})

# Called with (runtime, public group, event) on "created", "updated" and "finished".
LISTENERS = []


def add_listener(fn):
    if not callable(fn):
        raise ValueError("A group listener is a function")
    if fn not in LISTENERS:
        LISTENERS.append(fn)
