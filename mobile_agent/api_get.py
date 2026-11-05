"""GET routes of the local API."""

import json
import re
import time
from urllib.parse import parse_qs, urlsplit

from .api_errors import APIError
from .api_http import DEVICE_PATH, WORKFLOW_TOOL, api_path
from .journal import JournalError
from .workflows.records import WORKFLOW_ID


class GetRoutesMixin:
    def do_GET(self):
        if self.mcp_run():
            return
        if self.refused():
            return
        path = api_path(urlsplit(self.path).path)
        if self.extension("GET", path):
            return
        if path == "/api/events":
            return self.send_events()
        if path == "/api/status":
            return self.send_json({**self.runtime.status(), "app_session": bool(self.app_session)})
        if path == "/api/engine":
            return self.send_json(self.runtime.engine())
        if path == "/api/ai-plans":
            # Settings › Models: the plans and providers, and the coding agents, never a token.
            fresh = (parse_qs(urlsplit(self.path).query).get("refresh") or ["0"])[0] == "1"
            from .ai_plans import routes as plan_routes
            return self.send_json({"providers": plan_routes.public(),
                                   "inUse": (self.runtime.keys.state().get("smart") or {}).get("preference"),
                                   "agents": self.runtime.agents_status(max_age=0 if fresh else 60, wait=True)})
        if path == "/api/agents":
            # Settings and Setup's "Check again": read now (the status's copy may be a minute old).
            fresh = (parse_qs(urlsplit(self.path).query).get("refresh") or ["0"])[0] == "1"
            return self.send_json({"agents": self.runtime.agents_status(max_age=0 if fresh else 60, wait=True)})
        if path == "/api/apps":
            device = (parse_qs(urlsplit(self.path).query).get("device") or [None])[0]
            if device is None:
                return self.send_json(self.runtime.apps_payload())
            phone = self.device_phone(device)
            return phone and self.send_json(self.runtime.apps_payload(phone))
        if path == "/api/devices":
            return self.send_devices()
        if path == "/api/fleet/health":
            fleet = getattr(self.runtime, "fleet", None)
            if fleet is None or self.runtime.pool is not None:
                return self.send_json({"devices": [], "checkedAt": round(time.time() * 1000)})
            return self.send_json(fleet.health.report())
        device_match = DEVICE_PATH.fullmatch(path)
        if device_match and device_match[2] == "teach" and not device_match[3]:
            phone = self.device_phone(device_match[1])
            return phone and self.send_json(self.runtime.teach_state(phone))
        if path == "/api/device/teach":
            return self.send_json(self.runtime.teach_state(None))
        if device_match and device_match[2] in ("take-back", "resume-queue"):
            return self.send_json({"error": "Not found"}, 404)
        if device_match and not device_match[3] and device_match[2] not in ("control", "disconnect", "connect"):
            return self.send_device(device_match[1], device_match[2])
        if path == "/api/apps/icon":
            return self.send_icon()
        if path == "/api/workflows":
            try:
                from .graph import schema as graph_schema
                from .workflows import records as workflow_batch
                return self.send_json({"workflows": self.runtime.workflows.list_with_health(),
                                       "schedulerError": self.runtime.workflows.scheduler_error,
                                       "running": self.runtime.workflows.running(),
                                       "stepKinds": workflow_batch.step_kinds(),
                                       "deliveryKinds": workflow_batch.delivery_kinds(),
                                       "nodeTypes": list(graph_schema.TYPES)})
            except JournalError:
                return self.send_json({"error": "Mobster couldn't read your workflows from its history on this Mac. "
                                                "Quit and reopen Mobster, then try again.",
                                       "code": "persistence_unavailable"}, 503)
        if path == "/api/workflows/approvals":
            return self.send_json({"approvals": self.runtime.workflows.pending_approvals()})
        if path == "/api/workflows/node-types":
            from .graph import schema as graph_schema
            return self.send_json({"nodeTypes": graph_schema.catalog(), "limits": graph_schema.public_limits()})
        versions_match = re.fullmatch(r"/api/workflows/([a-f0-9]{12})/versions(?:/(\d{1,9})(?:/diff/(\d{1,9}))?)?",
                                      path)
        if versions_match:
            try:
                identifier, revision, other = versions_match[1], versions_match[2], versions_match[3]
                if other:
                    return self.send_json(
                        {"diff": self.runtime.workflows.history.diff(identifier, int(revision), int(other))})
                if revision:
                    return self.send_json({"workflow": self.runtime.workflows.history.version(identifier,
                                                                                              int(revision))})
                return self.send_json({"versions": self.runtime.workflows.history.versions(identifier)})
            except APIError as exc:
                return self.send_api_error(exc)
        estimate_match = re.fullmatch(r"/api/workflows/([a-f0-9]{12})/estimate", path)
        if estimate_match:
            draft = (parse_qs(urlsplit(self.path).query).get("draft") or ["0"])[0] == "1"
            try:
                return self.send_json({"estimate": self.runtime.workflows.estimate(estimate_match[1], draft=draft)})
            except APIError as exc:
                return self.send_api_error(exc)
        timeline_match = re.fullmatch(r"/api/workflows/([a-f0-9]{12})/runs/([a-f0-9]{12})/timeline", path)
        if timeline_match:
            try:
                return self.send_json({"timeline": self.runtime.workflows.timeline(*timeline_match.groups())})
            except APIError as exc:
                return self.send_api_error(exc)
        resume_match = re.fullmatch(r"/api/workflows/([a-f0-9]{12})/runs/([a-f0-9]{12})/resume", path)
        if resume_match:
            try:
                return self.send_json({"resume": self.runtime.workflows.resume_preview(*resume_match.groups())})
            except APIError as exc:
                return self.send_api_error(exc)
        node_match = re.fullmatch(
            r"/api/workflows/([a-f0-9]{12})/runs/([a-f0-9]{12})/nodes/([A-Za-z0-9_@.-]{1,64})(/frames)?", path)
        if node_match:
            try:
                if node_match[4]:
                    return self.send_json(self.runtime.workflows.node_frames(*node_match.groups()[:3]))
                return self.send_json({"node": self.runtime.workflows.node_history(*node_match.groups()[:3])})
            except APIError as exc:
                return self.send_api_error(exc)
        batches_match = re.fullmatch(r"/api/workflows/([a-f0-9]{12})/runs", path)
        if batches_match:
            try:
                return self.send_json({"batches": self.runtime.workflows.batch_runs(batches_match[1])})
            except APIError as exc:
                return self.send_api_error(exc)
        if path == "/api/workflows/tools":
            return self.send_json({"tools": self.runtime.workflows.tools()})
        tool_match = WORKFLOW_TOOL.fullmatch(path)
        if tool_match and not WORKFLOW_ID.fullmatch(tool_match[1]):
            try:
                return self.send_json({"tool": self.runtime.workflows.tool(tool_match[1])})
            except APIError as exc:
                return self.send_api_error(exc)
        if path == "/api/inbox":
            return self.send_json(self.runtime.inbox())
        if path == "/api/home":
            since = (parse_qs(urlsplit(self.path).query).get("since") or [None])[0]
            try:
                since = int(since) if since is not None else None
            except ValueError:
                return self.send_json({"error": "since is a time in milliseconds", "code": "invalid_since"}, 400)
            from . import home
            return self.send_json(home.summary(self.runtime, since))
        if path == "/api/usage":
            return self.send_json({**self.runtime.journal.usage(), "monthUsd": self.runtime.month_usd(),
                                   "monthlyLimitUsd": self.runtime.monthly_limit()})
        if path == "/api/usage/estimate":
            return self.send_json({"engines": self.runtime.engine_estimates()})
        if path == "/api/settings":
            return self.send_json(self.runtime.settings())
        if path == "/api/keys":
            return self.send_json(self.runtime.keys.state())
        if path == "/api/setup":
            if self.runtime.setup is None:
                return self.send_json({"error": "Setup is managed outside this app", "code": "setup_unmanaged"}, 404)
            return self.send_json(self.runtime.setup.state())
        if path == "/api/device/video/status":
            return self.send_json({**self.runtime.video.status(), "awakeLease": self.runtime.awake.status()})
        if path == "/api/device/info":
            return self.send_json(self.runtime.device_info.snapshot())
        if path == "/api/device/status":
            return self.send_json(self.runtime.device_status())
        if path == "/api/device/stream":
            return self.send_stream()
        if path == "/api/device/frame":
            return self.send_frame()
        if path == "/api/groups" or path.startswith("/api/groups/"):
            try:
                if self.group_request("GET", path, None) is False:
                    return self.send_json({"error": "Not found"}, 404)
            except APIError as exc:
                return self.send_api_error(exc)
            return
        if path == "/api/runs":
            with self.runtime.lock:
                runs = [r for r in self.runtime.runs.values() if self.may_read(r)]
            device = (parse_qs(urlsplit(self.path).query).get("device") or [None])[0]
            if device is not None:
                phone = self.device_phone(device)
                if phone is None:
                    return
                # Runs saved before devices had one ran on the primary phone.
                runs = [r for r in runs if r.device_id == phone.id or (phone.primary and r.device_id is None)]
            return self.send_json({"runs": [r.row() for r in reversed(runs)]})
        frame = re.fullmatch(r"/api/runs/([a-f0-9]{12})/frames/(f[0-9a-f]{10})", path)
        if frame:
            return self.send_run_frame(frame[1], frame[2])
        drawn = re.fullmatch(r"/api/runs/([a-f0-9]{12})/blocks/([A-Za-z0-9_.:-]{1,64})/map", path)
        if drawn:
            query = parse_qs(urlsplit(self.path).query)
            try:
                size = [int((query.get(name) or [default])[0]) for name, default in (("w", "720"), ("h", "360"))]
                return self.send_json(self.runtime.block_map(drawn[1], drawn[2], dark=(query.get("theme") or [""])[0]
                                                        == "dark", width=size[0], height=size[1]))
            except ValueError:
                return self.send_json({"error": "w and h are whole numbers"}, 400)
            except APIError as exc:
                return self.send_api_error(exc)
        match = re.fullmatch(r"/api/runs/([a-f0-9]{12})(?:/(events|screenshot|usage))?", path)
        run = self.runtime.runs.get(match[1]) if match else None
        if not run or not self.may_read(run):
            return self.send_json({"error": "Not found"}, 404)
        if match[2] == "usage":
            return self.send_json(self.runtime.journal.usage(run.id))
        if match[2] == "screenshot":
            return self.send_json(self.runtime.preview(run))
        if match[2] != "events":
            return self.send_json({"run": run.public()})
        if not self.runtime.streams.acquire(blocking=False):
            return self.send_json({"error": "Too many event streams", "code": "stream_limit"}, 429)
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                index = max(0, int(self.headers.get("Last-Event-ID", "-1")) + 1)
            except ValueError:
                index = 0
            with run.condition:
                if index > len(run.events):
                    index = 0
            while True:
                with run.condition:
                    events = list(run.events[index:])
                    finished = run.finished_at is not None
                    if not events and not finished:
                        run.condition.wait(timeout=5)
                if events:
                    for event in events:
                        self.wfile.write(f"id: {event['seq']}\ndata: {json.dumps(event)}\n\n".encode())
                    index += len(events)
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                if finished:
                    break
        except (OSError, TimeoutError):
            pass
        finally:
            self.runtime.streams.release()
