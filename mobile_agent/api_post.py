"""DELETE and POST routes of the local API."""

import re
from urllib.parse import urlsplit

from . import ai_plans, engines, env_secrets, harness_api, ownership
from .api_devices import runtime_teach
from .api_errors import APIError
from .api_http import (
    ALREADY_ANSWERED,
    APP_ONLY,
    APPROVE_IN_APP,
    APPROVE_IN_APP_ORIGINS,
    DEVICE_PATH,
    FILE_TROUBLE,
    TOOK_TOO_LONG,
    WORKFLOW_TOOL,
    api_path,
)
from .downloads import EXPORT_MAX_BYTES, save_export
from .graph.schema import GraphError
from .journal import JournalError
from .output_contract import FORMATS as OUTPUT_FORMATS
from .output_contract import validate_schema
from .transport import decode_json
from .workflows.cron import preview as schedule_preview
from .workflows.records import WORKFLOW_ID
from .workflows.runs import WorkflowDispatchUncertain


class WriteRoutesMixin:
    def do_DELETE(self):
        self.close_connection = True
        if self.mcp_run():
            return
        if self.refused():
            return
        path = api_path(urlsplit(self.path).path)
        if self.extension("DELETE", path):
            return
        link_match = re.fullmatch(r"/api/workflows/([a-f0-9]{12})/runs/([a-f0-9]{12})/approvals/"
                                  r"([A-Za-z0-9_@.-]{1,64})/link", path)
        if link_match:
            if not self.has_app_session():
                return self.send_json({"error": "Stop a link in the Mobster app.", "code": "app_only"}, 403)
            try:
                return self.send_json(self.runtime.workflows.approval_links.stop(*link_match.groups()))
            except APIError as exc:
                return self.send_api_error(exc)
        workflow_match = re.fullmatch(r"/api/workflows/([a-f0-9]{12})", path)
        if workflow_match:
            try:
                return self.send_json({"deleted": self.runtime.workflows.delete(workflow_match[1])})
            except APIError as exc:
                return self.send_api_error(exc)
            except JournalError:
                return self.send_json({"error": "Workflows could not be saved. Try again.",
                                       "code": "persistence_unavailable"}, 503)
        device_match = DEVICE_PATH.fullmatch(path)
        if device_match and device_match[2] is None and device_match[3] is None:
            phone = self.device_phone(device_match[1])
            if phone is None:
                return
            try:
                return self.send_json({"forgotten": self.runtime.fleet.forget(phone.id)})
            except LookupError as error:
                return self.send_json({"error": str(error), "code": "device_refused"}, 409)
        match = re.fullmatch(r"/api/runs/([a-f0-9]{12})", path)
        if not match:
            return self.send_json({"error": "Not found"}, 404)
        try:
            self.runtime.delete_run(match[1])
        except APIError as exc:
            return self.send_api_error(exc)
        except JournalError:
            return self.send_json({"error": "Task history could not be saved. Try again.",
                                   "code": "persistence_unavailable"}, 503)
        return self.send_json({"deleted": match[1]})

    def do_POST(self):
        # Close after POST so unread malformed bodies cannot become a second request.
        self.close_connection = True
        ownership.set_client(self.headers.get("X-Mobster-Client"))  # a label for who holds the phone, nothing more
        if self.mcp_run():
            return
        if self.refused():
            return
        if self.extension("POST", api_path(urlsplit(self.path).path)):
            return
        if self.headers.get_content_type() != "application/json":
            return self.send_json({"error": "Use application/json"}, 415)
        try:
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError("Supply one Content-Length and no Transfer-Encoding")
            size = int(self.headers.get("Content-Length", "0"))
            # Exports carry a whole result; every other body is a small command.
            limit = EXPORT_MAX_BYTES + 4096 if api_path(self.path) == "/api/export" else 64000
            if not 0 < size <= limit:
                return self.send_json({"error": "Invalid body length"}, 400)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("Incomplete request body")
            body = decode_json(raw)
            if not isinstance(body, dict):
                raise ValueError("JSON body must be an object")
            path = api_path(self.path)
            if path.startswith("/api/setup/"):
                return self.setup_action(path, body)
            if path == "/api/devices/chosen":
                return self.choose_devices(body)
            if path == "/api/fleet/health/acknowledge":
                # "Mark as handled" lifts a profile's pre-run refusal, so it needs the Mac app's own session.
                if self.needs_app():
                    return
                fleet = getattr(self.runtime, "fleet", None)
                profile = body.get("profile")
                if fleet is None or set(body) - {"profile", "code"} or not isinstance(profile, str) or not profile:
                    raise ValueError("Name the profile to mark as handled, and optionally the code")
                try:
                    cleared = fleet.health.acknowledge(profile, body.get("code"))
                except OSError:
                    raise APIError("Mobster could not save that: another Mobster process is holding the phone "
                                   "health file. Choose Mark as handled again in a moment.", 503,
                                   "health_busy") from None
                return self.send_json({"cleared": cleared})
            device_match = DEVICE_PATH.fullmatch(path)
            if device_match and device_match[2] in ("take-back", "resume-queue"):
                return self.owner_action(device_match[1], device_match[2], body)
            if device_match and (device_match[3] or device_match[2] in ("control", "teach")):
                return self.device_action(device_match[1], device_match[2], device_match[3], body)
            provider_match = re.fullmatch(r"/api/ai-plans/providers/([a-z_]{2,30})/([a-z-]{2,20})", path)
            if provider_match:
                if self.needs_app():
                    return
                from .ai_plans import routes as plan_routes
                try:
                    return self.send_json(plan_routes.handle(self.runtime.keys, provider_match[1], provider_match[2],
                                                             body))
                except plan_routes.RouteError as error:
                    return self.send_json({"error": str(error), "code": error.code}, error.status)
            if device_match and device_match[2] in ("disconnect", "connect", None) and not device_match[3]:
                return self.manage_device(device_match[1], device_match[2], body)
            if path.startswith("/api/groups/"):
                if self.group_request("POST", path, body) is False:
                    return self.send_json({"error": "Not found"}, 404)
                return
            if path == "/api/workflows/preview":
                return self.send_json(schedule_preview(body, self.runtime.workflows.now()))
            if path == "/api/workflows":
                return self.send_json({"workflow": self.runtime.workflows.create(body)}, 201)
            if path == "/api/workflows/draft":
                return self.send_json(self.runtime.workflows.draft(body))
            if path == "/api/workflows/validate":
                if not isinstance(body, dict) or set(body) - {"graph", "workflowId"}:
                    raise ValueError("Send the graph, and the workflowId when it is a saved workflow's")
                return self.send_json({"graph": self.runtime.workflows.check_graph(body.get("graph"),
                                                                              body.get("workflowId"))})
            if path == "/api/workflows/evaluate":
                from .graph import expr as graph_expr
                if not isinstance(body, dict) or set(body) - {"expression", "scope", "workflowId", "batchId", "node"} \
                        or not isinstance(body.get("scope", {}), dict):
                    raise ValueError("Send the expression, and a scope of values it reads or the run and step "
                                     "(workflowId, batchId, node) whose values it reads")
                scope = body.get("scope") or {}
                if body.get("batchId"):
                    try:
                        scope = {**self.runtime.workflows.recorded_scope(body.get("workflowId"), body["batchId"],
                                                                    body.get("node")), **scope}
                    except APIError as exc:
                        return self.send_api_error(exc)
                try:
                    value = graph_expr.render(body.get("expression"), scope)
                except graph_expr.ExprError as error:
                    return self.send_json({"error": str(error), "code": "invalid_expression", "at": error.at}, 400)
                return self.send_json({"value": value, "type": graph_expr.kind_of(value)})
            template = re.fullmatch(r"/api/workflows/templates/([a-z-]{1,40})", path)
            if template:
                from .workflows import brief
                return self.send_json({"workflow": brief.create_from_template(self.runtime.workflows, template[1],
                                                                              body)}, 201)
            link_match = re.fullmatch(r"/api/workflows/([a-f0-9]{12})/runs/([a-f0-9]{12})/approvals/"
                                      r"([A-Za-z0-9_@.-]{1,64})/link", path)
            if link_match:
                # Only the person asks for a link: MCP, Remote and hooks never make one.
                if not self.has_app_session():
                    return self.send_json({"error": "Ask by link in the Mobster app.", "code": "app_only"}, 403)
                if not isinstance(body, dict) or set(body) - {"label"}:
                    raise ValueError("A link takes a label, and nothing else")
                return self.send_json(self.runtime.workflows.approval_links.make(*link_match.groups(),
                                                                            label=body.get("label")))
            approval_match = re.fullmatch(
                r"/api/workflows/([a-f0-9]{12})/runs/([a-f0-9]{12})/approvals/([A-Za-z0-9_@.-]{1,64})", path)
            if approval_match:
                # Only the person answers: MCP, Remote, hooks and typed messages never approve a step.
                if not self.has_app_session():
                    return self.send_json({"error": "Answer this in the Mobster app.", "code": "app_only"}, 403)
                return self.send_json({"approval": self.runtime.workflows.answer_approval(*approval_match.groups(),
                                                                                     body)})
            workflow_match = re.fullmatch(
                r"/api/workflows/([a-f0-9]{12})(/run|/stop|/publish|/rollback|/prove|/resume)?", path)
            if workflow_match:
                if workflow_match[2] in ("/prove", "/resume"):
                    # A prove and a bulk resume start from Mobster's editor, like a test run and a Resume.
                    if not self.has_app_session():
                        return self.send_json({"error": "A test run or a re-run starts from Mobster's editor.",
                                               "code": "test_from_editor"}, 403)
                    if len(self.headers.get_all("Idempotency-Key", [])) != 1:
                        raise ValueError("Send one Idempotency-Key")
                    key = self.headers.get("Idempotency-Key")
                    if workflow_match[2] == "/prove":
                        return self.send_json(self.runtime.workflows.prove(workflow_match[1], body, key,
                                                                      origin=self.origin_label()), 201)
                    if not isinstance(body, dict) or set(body) != {"batchIds"}:
                        raise ValueError("Send batchIds: the runs to resume")
                    return self.send_json(self.runtime.workflows.resume_all(workflow_match[1], body["batchIds"], key,
                                                                       origin=self.origin_label()), 202)
                if not workflow_match[2]:
                    return self.send_json({"workflow": self.runtime.workflows.update(workflow_match[1], body)})
                if workflow_match[2] == "/publish":
                    return self.send_json({"workflow": self.runtime.workflows.history.publish(workflow_match[1], body)})
                if workflow_match[2] == "/rollback":
                    return self.send_json(
                        {"workflow": self.runtime.workflows.history.rollback(workflow_match[1], body)})
                if workflow_match[2] == "/stop":
                    return self.send_json({"batch": self.runtime.workflows.stop_batch(workflow_match[1])})
                if set(body) - {"values", "threadId", "newConversation", "rows", "test", "from", "only", "resume",
                                "trigger", "input"} \
                        or len(self.headers.get_all("Idempotency-Key", [])) != 1:
                    raise ValueError("A run takes values, threadId, newConversation, rows, test, from, only, "
                                     "resume, trigger or input, and one Idempotency-Key")
                rerun = {key: body[key] for key in ("from", "only", "resume") if key in body} or None
                if rerun is not None and len(rerun) > 1:
                    raise ValueError("A re-run is one of from, only or resume")
                if (body.get("test") or rerun) and not self.has_app_session():
                    return self.send_json({"error": "A test run or a re-run starts from Mobster's editor.",
                                           "code": "test_from_editor"}, 403)
                from .threads.routes import grant_header
                started = self.runtime.workflows.start(workflow_match[1], self.headers.get("Idempotency-Key"),
                                                  values=body.get("values"), thread_id=body.get("threadId"),
                                                  new_conversation=body.get("newConversation", False),
                                                  origin=self.origin_label(), rows=body.get("rows"),
                                                  remote_grant=grant_header(self.origin_label(), self.headers),
                                                  test=body.get("test", False), rerun=rerun,
                                                  trigger=body.get("trigger"), input=body.get("input"),
                                                  project_secrets=self.has_app_session())
                return self.send_json(started, 200 if started["replayed"] else 201)
            tool_match = WORKFLOW_TOOL.fullmatch(path)
            if tool_match and not WORKFLOW_ID.fullmatch(tool_match[1]):
                # A workflow made a tool, by its slug (POST /v1/workflows/<slug>): the same schema as its MCP tool.
                if len(self.headers.get_all("Idempotency-Key", [])) != 1:
                    raise ValueError("Send one Idempotency-Key with a workflow run")
                answer, ended = self.runtime.workflows.run_tool(tool_match[1], body,
                                                                self.headers.get("Idempotency-Key"),
                                                                origin=self.origin_label(),
                                                                project_secrets=self.has_app_session())
                return self.send_json(answer, 200 if ended else 202)
            if path == "/api/usage/estimate":
                engine = engines.read_engine(body.get("engine"))
                if (set(body) - {"goal", "outputSchema", "outputFormat", "engine"}
                        or not isinstance(body.get("goal"), str) or len(body["goal"].strip()) > 4000
                        or engine is not None and engine not in engines.ENGINES and not ai_plans.is_agent(engine)
                        or body.get("outputFormat") not in (None, "auto", *OUTPUT_FORMATS)):
                    raise ValueError("Supply a draft of at most 4000 characters and an optional output schema, "
                                     "output format and engine")
                schema = validate_schema(body["outputSchema"]) if body.get("outputSchema") is not None else None
                estimates = self.runtime.engine_estimates(body["goal"].strip(), schema,
                                                     output_format=body.get("outputFormat"))
                return self.send_json({"engine": engine or engines.SMART, "engines": estimates})
            if path == "/api/runs":
                if body.get("mode", "live") != "live":
                    raise ValueError("Dashboard tasks run on your iPhone. Synthetic replays are not supported here.")
                if len(self.headers.get_all("Idempotency-Key", [])) > 1:
                    raise ValueError("Supply only one Idempotency-Key")
                key = self.headers.get("Idempotency-Key")
                core = {"appId", "goal", "mode", "outputSchema", "outputFormat", "allowedBundles", "engine", "device",
                        "phones", "profiles", "pinnedApps", "queue", "teamGroup", "dryRun"}
                fields = harness_api.run_fields()
                if body.get("dryRun") is True:
                    return self.send_json({"dryRun": self.runtime.dry_run(body)})
                if "dryRun" in body:
                    raise ValueError("dryRun is true, or leave it out")
                if set(body) - core - set(fields):
                    raise ValueError("Unsupported task field")
                if body.get("device") is not None and not isinstance(body["device"], str):
                    raise ValueError("device must be a device's id, UDID or name")
                if "queue" in body and type(body["queue"]) is not bool:
                    raise ValueError("queue is true (wait for a held phone, the default) or false (refuse)")
                if set(body) & harness_api.claimed_fields():
                    raise ValueError("consoleMember and consoleSession are set by Mobster, never by a request")
                extras = {name: body[name] for name in body if name in fields and name not in core}
                if extras.get("projectEnv") is not None and not self.has_app_session():
                    # Resolving local secrets needs the private app session, never an origin label.
                    return self.send_json({"error": "Start a task with project secrets in the Mobster app on this Mac.",
                                           "code": "app_only"}, 403)
                longform = extras.get("longform")
                if isinstance(longform, dict) and longform.get("capUsd") is not None and not self.has_app_session():
                    # Raising a task's spending limit is the Mac app's: X-Mobster-Origin grants nothing.
                    return self.send_json({"error": APP_ONLY, "code": "app_only"}, 403)
                if self.loosening(body):
                    return
                origin = self.origin_label()
                extras.update(self.origin_claim())
                if body.get("teamGroup") is not None and body.get("phones") is None:
                    raise ValueError("teamGroup is set by the team's console, for phones only")
                if body.get("phones") is not None or body.get("profiles") is not None:
                    return self.start_group(body, key, extras, origin)
                created = self.runtime.create(body.get("appId"), body.get("goal"), "live", key,
                                         output_schema=body.get("outputSchema"),
                                         output_format=body.get("outputFormat", "auto"),
                                         allowed_bundles=body.get("allowedBundles"), engine=body.get("engine"),
                                         **({"device": body["device"]} if body.get("device") is not None else {}),
                                         **({"pinned_apps": body["pinnedApps"]}
                                            if body.get("pinnedApps") is not None else {}),
                                         **({"extras": extras} if extras else {}),
                                         **({"origin": origin} if origin != "api" else {}),
                                         queue=body.get("queue", True))
                run, replayed = created if key else (created, False)
                return self.send_json({"run": run.public(), "replayed": replayed}, 200 if replayed else 201)
            if path == "/api/settings":
                # Asking less and spending more are the person's, in the Mac app (S6.6), as a task's capUsd is.
                if {"askBeforeActing", "taskCostLimitUsd", "monthlyLimitUsd"} & set(body) and self.needs_app():
                    return
                return self.send_json(self.runtime.update_settings(body))
            if path == "/api/keys":
                return self.send_json(self.runtime.save_keys(body))
            if path == "/api/keys/test":
                if set(body) != {"target"}:
                    raise ValueError("Send the target to test: anthropic, openai or helper")
                return self.send_json(self.runtime.check_key(body["target"]))
            if path == "/api/export":
                return self.send_json(save_export(body))
            if path in ("/api/device/control", "/api/device/teach") and self.needs_app():
                return  # as /api/devices/{id}/control and teach (device_action)
            if path == "/api/device/control":
                return self.send_json({"ok": True, "action": self.runtime.control_device(body)})
            if path == "/api/device/teach":
                return self.send_json(runtime_teach(self.runtime, body, None))
            shared = re.fullmatch(r"/api/runs/([a-f0-9]{12})/share(?:/(preview)|/([A-Za-z0-9_-]{22})/revoke)?", path)
            if shared:
                token = body.pop("accessToken", None) if isinstance(body, dict) else None
                if shared[2] == "preview":
                    if set(body) - {"people", "screens", "title"}:
                        raise ValueError("A share preview takes people, screens and title")
                    return self.send_json(self.runtime.share_preview(shared[1], body.get("people", "mask"),
                                                                body.get("screens", False), body.get("title")))
                # Making or turning off a link is the Mac app's: it holds the account (as Remote's turn-on).
                if self.needs_app():
                    return
                if shared[3]:
                    if set(body):
                        raise ValueError("Turning a link off takes only the account's accessToken")
                    return self.send_json(self.runtime.unshare_run(shared[1], shared[3], token))
                if set(body) - {"people", "screens", "title", "expiresDays"}:
                    raise ValueError("A share takes people, screens, title, expiresDays and the accessToken")
                return self.send_json(self.runtime.share_run(shared[1], token, body.get("people", "mask"),
                                                        body.get("screens", False), body.get("title"),
                                                        body.get("expiresDays", 30)), 201)
            artifact = re.fullmatch(r"/api/runs/([a-f0-9]{12})/artifact", path)
            if artifact:
                if set(body) - {"blocks"} != {"format"} or body["format"] not in ("pdf", "png", "md"):
                    raise ValueError("An artifact takes format (pdf, png or md) and optionally blocks")
                return self.send_json(self.runtime.save_artifact(artifact[1], body["format"], body.get("blocks")))
            blocked = re.fullmatch(r"/api/runs/([a-f0-9]{12})/blocks/([A-Za-z0-9_.:-]{1,64})/items/"
                                   r"([A-Za-z0-9_.:-]{1,64})", path)
            if blocked:
                if (set(body) - {"text"} != {"decision"} or body["decision"] not in ("approve", "skip", "reopen")
                        or "text" in body and not isinstance(body["text"], str)):
                    raise ValueError("A decision on a result is approve, skip or reopen (and optionally the "
                                     "edited text with approve)")
                # Approving is the Mac app's, as an approval card's yes is (S6.6); skipping stays token-only.
                if body["decision"] == "approve" and self.needs_app(APPROVE_IN_APP):
                    return
                return self.send_json(self.runtime.decide_block_item(blocked[1], blocked[2], blocked[3],
                                                                body["decision"], body.get("text")))
            match = re.fullmatch(r"/api/runs/([a-f0-9]{12})/(stop|approval|review|gif|eval)", path)
            run = self.runtime.runs.get(match[1]) if match else None
            if run and match[2] == "eval":
                if body:
                    raise ValueError("Adding a task to your evals takes no body")
                return self.send_json(self.runtime.add_to_evals(match[1]))
            if run and match[2] == "gif":
                theme, redact, mp4 = body.get("theme", "paper"), body.get("redact", False), body.get("mp4", False)
                if (set(body) - {"theme", "redact", "mp4"} or theme not in ("paper", "night")
                        or type(redact) is not bool or type(mp4) is not bool):
                    raise ValueError("A GIF takes an optional theme (paper or night), redact and mp4 "
                                     "(true or false)")
                return self.send_json(self.runtime.save_run_gif(match[1], theme, redact, mp4))
            if run and match[2] == "approval":
                if (set(body) - {"choice", "instruction", "answer", "remember", "items", "text", "allowCost"}
                        != {"id", "approve"}
                        or "remember" in body and type(body["remember"]) is not bool
                        or "allowCost" in body and type(body["allowCost"]) is not bool
                        or not isinstance(body["id"], str)
                        or type(body["approve"]) is not bool
                        or "choice" in body and not isinstance(body["choice"], str)
                        or "instruction" in body and not isinstance(body["instruction"], str)
                        or "answer" in body and not isinstance(body["answer"], str)
                        or "items" in body and not isinstance(body["items"], list)
                        or "text" in body and not isinstance(body["text"], str)):
                    raise ValueError("An approval needs its id and approve: true or false (and optionally a "
                                     "choice, an instruction with approve false, an answer to a question, or "
                                     "the items it decides and an edited text)")
                pending = run.approval
                question = (pending is not None and pending.get("id") == body["id"]
                            and pending.get("kind") == "clarify")
                # Approving or choosing needs the Mac app; a decline, a redirect or a clarifying answer is token-only.
                if (body["approve"] or "choice" in body) and not question:
                    if run.origin in APPROVE_IN_APP_ORIGINS and not self.has_app_session():
                        return self.send_json({"error": APP_ONLY, "code": "app_only"}, 403)
                    if self.needs_app(APPROVE_IN_APP):
                        return
                if "items" in body or "text" in body:
                    answered = run.decide_proposals(body["id"], body["approve"], body.get("items"),
                                                    body.get("text"))
                else:
                    answered = run.answer_approval(body["id"], body["approve"], body.get("choice"),
                                                   body.get("instruction"),
                                                   **({"answer": body["answer"]} if "answer" in body else {}),
                                                   **({"remember": True} if body.get("remember") else {}),
                                                   **({"allow_cost": True} if body.get("allowCost") else {}))
                if not answered:
                    decision = run.approval_decision(body["id"])
                    raise APIError(ALREADY_ANSWERED.get(decision, "This approval request is no longer waiting"),
                                   409, "approval_not_pending", **({"decision": decision} if decision else {}))
                return self.send_json({"run": run.public()})
            if run and match[2] == "review":
                if set(body) != {"ok"} or type(body["ok"]) is not bool:
                    raise ValueError("A review needs ok: true or false")
                return self.send_json({"run": self.runtime.review_run(run.id, body["ok"]).public()})
            if run:
                self.runtime.stop_run(run.id)
                return self.send_json({"run": run.public()})
            return self.send_json({"error": "Not found"}, 404)
        except GraphError as exc:
            return self.send_json({"error": str(exc), "code": exc.code, "errors": exc.errors}, 400)
        except (ValueError, TypeError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except APIError as exc:
            return self.send_api_error(exc)
        except WorkflowDispatchUncertain as exc:
            return self.send_json({"error": str(exc), "code": "workflow_dispatch_uncertain", "activeRunId": exc.run_id},
                                  503)
        except JournalError:
            return self.send_json({"error": "Mobster couldn't save this to its history on this Mac, and a task may "
                                   "have started. Check its status before you try again.",
                                   "code": "persistence_unavailable"}, 503)
        except TimeoutError:  # the request's socket (5 s, ``setup``)
            return self.send_json({"error": TOOK_TOO_LONG, "code": "request_timeout"}, 408)
        except env_secrets.KeychainError:  # a key or the alert address, which never fall back to the file
            return self.send_json({"error": env_secrets.REFUSED, "code": "keychain"}, 503)
        except OSError:
            return self.send_json({"error": FILE_TROUBLE,
                                   "code": "io_error"}, 500)
