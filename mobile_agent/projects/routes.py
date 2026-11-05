"""The local API for projects, under /api/projects (api_routes, owner "projects").

| Route | Auth | Body | Answer |
|---|---|---|---|
| GET /api/projects | token | | {projects, suggestions, clones, cloneFolder, cloneFolderShown} |
| POST /api/projects | app | {path, name?} | 201 {project}, or 200 {project} when the folder is a project already |
| GET /api/projects/{id} | token | | {project}, with checkFiles, checkTags and the branch's commit, upstream, changes |
| GET /api/projects/{id}/icon | token | | the app's own icon (image/png), or 404 no_icon |
| GET /api/projects/{id}/frame | token | ?run=&path= | a frame from one of its runs (JPEG or PNG), .mobster/runs |
| GET /api/projects/{id}/tests | token | | {checks, ok, suites}: its own, the last 20 of each, a suite with its git |
| GET /api/projects/{id}/pulls | app | | {signedIn, repo, pulls}: the GitHub repository's pull requests by branch |
| POST /api/projects/{id}/open | token | | {project}: the project opened last, which the app shows first |
| POST /api/projects/{id}/setup | app | {app?} | 201 {project, created}: `.mobster/` and a starter check |
| POST /api/projects/{id}/checks/author | app session | {name?, app, steps, expect, reviewed:true} | reviewed draft |
| POST /api/projects/{id}/checks/save | app session | {runId, yaml, reviewed:true} | reviewed finished-task draft |
| POST /api/projects/{id}/test-revisions | app session | {checks, appPath, simulatorId, reviewed} | approved revision |
| GET /api/projects/{id}/test-revisions/options | app session | | saved checks, apps and simulators |
| GET /api/projects/ci-candidates | app session | | ready approved revision summaries for local grant issuance |
| POST /api/projects/{id}/runs | app | {requestId?, mode?, sims? or device?, tags? or links?} | 202 {run}, once |
| GET /api/projects/{id}/runs?requestId= | token | | {run}: reconcile an unknown submit |
| GET /api/projects/runs/{id} | token | | {run} |
| DELETE /api/projects/runs/{id} | app | | {run}: stopped |
| DELETE /api/projects/{id} | app | | {id}: Mobster forgets the project; the folder stays |
| GET /api/projects/github | app | ?refresh=1 | {signedIn, repos}: what the GitHub connector's sign-in can open |
| POST /api/projects/clones | app | {url, folder?, name?} | 202 {clone}; a github.com URL clones with that sign-in |
| POST /api/projects/{id}/workflows | app | {workflowId} | {workflow}: it gets a file in .mobster/workflows |
| POST /api/projects/workflows/{id}/resolve | app | {keep: file or mobster} | {workflow}: a file's conflict settled |
| GET /api/projects/clones/{id} | token | | {clone} |
| DELETE /api/projects/clones/{id} | app | | {clone}: stopped, and the folder it made removed |

A project is {id, name, path, shownPath, source, addedAt, openedAt, state, problem, mobster, apps, icon (a word that
changes with the icon, for its address; null without one), checks, workflows, simulators, matrixProblem, git, lastRun}
(docs/projects.mdx). A clone is {id, url, path, shownPath, name, state (cloning, done, failed, canceled), phase,
percent, code, error, projectId, startedAt, endedAt}. A run is {id, projectId, state (building, running, done, failed,
stopped), line, done, total, targets, suite, summary, exitCode, code, error, fix, log, choice, startedAt, endedAt}; a
project carries its newest as ``run``. With no options a run builds the app and runs every check on the simulators. A
run's body takes options only while MOBSTER_EXPERIMENTAL_TESTS_PAGE=1: an iPhone needs a plan and no task on it (403
plan_required, 404 device_not_found, 409 device_busy). Errors are {"error": <a sentence>, "code"}; a folder or a clone
that won't do answers 400, 403, 404 or 409 with its own code.

"app" routes need the Mac app's session (X-Mobster-App-Session) when the agent has one: 403 app_only without it.
"""

from ..api_errors import APIError
from ..api_routes import Response, Route, register
from . import service as project_service

APP_ONLY = "Do this in the Mobster app."


def service_of(request):
    service = project_service.service_for(request.runtime)
    if service is None:
        raise APIError(project_service.GONE, 503, "unavailable")
    return service


def needs_app(request):
    local = getattr(request.runtime, "local_api", None) or {}
    if local.get("app_session") and not request.app_session:
        raise APIError(APP_ONLY, 403, "app_only")


def listed(request):
    return Response.json(service_of(request).list())


def add(request):
    service = service_of(request)
    needs_app(request)
    project, created = service.add(request.body)
    return Response.json({"project": project}, 201 if created else 200)


def get(request):
    return Response.json({"project": service_of(request).get(request.params["id"])})


def frame(request):
    data, kind = service_of(request).frame(request.params["id"], (request.query.get("run") or [None])[0],
                                           (request.query.get("path") or [None])[0])
    return Response.bytes(data, kind)


def icon(request):
    return Response.bytes(service_of(request).icon(request.params["id"]), "image/png")


def tests(request):
    return Response.json(service_of(request).tests(request.params["id"]))


def opened(request):
    return Response.json({"project": service_of(request).open(request.params["id"])})


def forget(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"id": service.forget(request.params["id"])})


def set_up(request):
    service = service_of(request)
    needs_app(request)
    project, created = service.setup(request.params["id"], request.body)
    return Response.json({"project": project, "created": created}, 201)


def _review_session(request):
    if not request.app_session:
        raise APIError(APP_ONLY, 403, "app_only")


def author_check(request):
    _review_session(request)
    saved = service_of(request).author_check(request.params["id"], request.body)
    return Response.json(saved, 200 if saved["replayed"] else 201)


def save_check(request):
    _review_session(request)
    saved = service_of(request).save_finished_check(request.params["id"], request.body)
    return Response.json(saved, 200 if saved["replayed"] else 201)


def approve_test_revision(request):
    _review_session(request)
    approved = service_of(request).approve_test_revision(request.params["id"], request.body)
    return Response.json({"revision": approved}, 201 if approved["created"] else 200)


def test_revision_options(request):
    _review_session(request)
    return Response.json(service_of(request).ci_revision_options(request.params["id"]))


def ci_candidates(request):
    _review_session(request)
    return Response.json(service_of(request).ci_candidates())


def run_checks(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"run": service.run_checks(request.params["id"], request.body)}, 202)


def run_status(request):
    return Response.json({"run": service_of(request).run_status(request.params["id"])})


def requested_run(request):
    values = request.query.get("requestId") or []
    if len(values) != 1:
        raise ValueError("Give one requestId to find a check run")
    return Response.json({"run": service_of(request).requested_run(request.params["id"], values[0])})


def stop_run(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"run": service.stop_run(request.params["id"])})


def pulls(request):
    service = service_of(request)
    needs_app(request)
    return Response.json(service.pulls(request.params["id"]))


def github_repos(request):
    service = service_of(request)
    needs_app(request)
    refresh = (request.query.get("refresh") or ["0"])[0] in ("1", "true")
    return Response.json(service.github(refresh=refresh))


def add_workflow(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"workflow": service.add_workflow(request.params["id"], request.body)})


def resolve(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"workflow": service.resolve(request.params["id"], request.body)})


def clone(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"clone": service.clone(request.body)}, 202)


def clone_status(request):
    return Response.json({"clone": service_of(request).clone_status(request.params["id"])})


def cancel_clone(request):
    service = service_of(request)
    needs_app(request)
    return Response.json({"clone": service.cancel_clone(request.params["id"])})


def register_routes():
    for route in (Route("projects", "GET", "/api/projects", listed, body="none"),
                  Route("projects", "POST", "/api/projects", add, max_bytes=4096),
                  Route("projects", "GET", "/api/projects/{id}", get, body="none"),
                  Route("projects", "GET", "/api/projects/{id}/icon", icon, body="none"),
                  Route("projects", "GET", "/api/projects/{id}/frame", frame, body="none"),
                  Route("projects", "GET", "/api/projects/{id}/tests", tests, body="none"),
                  Route("projects", "GET", "/api/projects/{id}/pulls", pulls, body="none"),
                  Route("projects", "POST", "/api/projects/{id}/open", opened, body="none"),
                  Route("projects", "DELETE", "/api/projects/{id}", forget, body="none"),
                  Route("projects", "POST", "/api/projects/{id}/setup", set_up, max_bytes=4096),
                  Route("projects", "POST", "/api/projects/{id}/checks/author", author_check),
                  Route("projects", "POST", "/api/projects/{id}/checks/save", save_check),
                  Route("projects", "POST", "/api/projects/{id}/test-revisions", approve_test_revision),
                  Route("projects", "GET", "/api/projects/{id}/test-revisions/options", test_revision_options,
                        body="none"),
                  Route("projects", "GET", "/api/projects/ci-candidates", ci_candidates, body="none"),
                  Route("projects", "POST", "/api/projects/{id}/runs", run_checks, max_bytes=1024),
                  Route("projects", "GET", "/api/projects/{id}/runs", requested_run, body="none"),
                  Route("projects", "GET", "/api/projects/runs/{id}", run_status, body="none"),
                  Route("projects", "DELETE", "/api/projects/runs/{id}", stop_run, body="none"),
                  Route("projects", "GET", "/api/projects/github", github_repos, body="none"),
                  Route("projects", "POST", "/api/projects/{id}/workflows", add_workflow, max_bytes=1024),
                  Route("projects", "POST", "/api/projects/workflows/{id}/resolve", resolve, max_bytes=1024),
                  Route("projects", "POST", "/api/projects/clones", clone, max_bytes=4096),
                  Route("projects", "GET", "/api/projects/clones/{id}", clone_status, body="none"),
                  Route("projects", "DELETE", "/api/projects/clones/{id}", cancel_clone, body="none")):
        register(route)
