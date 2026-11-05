"""A workflow's history (docs/workflows.mdx › Drafts, publishing and versions)"""

import json

from ..api_errors import APIError
from ..graph import edit
from ..journal import encode

KEPT = 100
# What a rollback brings back from an old version; whether the schedule is on, the home conversation and the webhook
# stay as they are now.
RESTORED = ("graph", "name", "appId", "appName", "engine", "device", "account", "sendTo", "sameConversation",
            "outputSchema", "outputFormat", "produces")


class WorkflowHistory:
    def __init__(self, owner):
        self.owner = owner
        with owner.journal.transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS workflow_versions (
                workflow_id TEXT NOT NULL, revision INTEGER NOT NULL, saved_at INTEGER NOT NULL, data TEXT NOT NULL,
                published_at INTEGER, PRIMARY KEY (workflow_id, revision))""")

    def forget(self, connection, identifier):
        connection.execute("DELETE FROM workflow_versions WHERE workflow_id=?", (identifier,))

    def publish(self, identifier, body):
        """POST /api/workflows/{id}/publish {revision}"""
        if not isinstance(body, dict) or set(body) != {"revision"} or type(body["revision"]) is not int:
            raise ValueError("Give the revision to publish")
        with self.owner.lock, self.owner.journal.lock:
            workflow = self.owner._get(identifier)
            row = self.owner.journal.connection.execute(
                "SELECT data FROM workflow_versions WHERE workflow_id=? AND revision=?",
                (identifier, body["revision"])).fetchone()
            if row is None:
                raise APIError("That version isn't kept any more.", 404, "version_not_found")
            try:
                graph = self.owner.check_graph(json.loads(row[0]).get("graph"), identifier)
            except ValueError as error:
                raise APIError(f"That version can't run now: {error}", 409, "version_invalid") from None
            updated = {**workflow, "published": {"revision": body["revision"], "graph": graph}}
            self.owner.derive(updated)
            next_at = self.owner._schedule(updated)
            if any(updated.get(key) != workflow.get(key) for key in ("cron", "timezone")):
                updated["nextAt"] = next_at if updated["enabled"] else None
            with self.owner.journal.transaction() as connection:
                self.owner._save(connection, updated)
                connection.execute("UPDATE workflow_versions SET published_at=? WHERE workflow_id=? AND revision=?",
                                   (self.owner.now(), identifier, body["revision"]))
            self.owner.triggers.changed()
            return self.owner.public(updated)

    def keep(self, connection, workflow):
        published = (workflow.get("published") or {}).get("revision") == workflow["revision"]
        connection.execute("INSERT OR REPLACE INTO workflow_versions VALUES (?,?,?,?,?)",
                           (workflow["id"], workflow["revision"], workflow["updatedAt"], encode(workflow),
                            workflow["updatedAt"] if published else None))
        connection.execute("""DELETE FROM workflow_versions WHERE workflow_id=? AND revision NOT IN (
            SELECT revision FROM workflow_versions WHERE workflow_id=? ORDER BY revision DESC LIMIT ?)""",
                           (workflow["id"], workflow["id"], KEPT))

    def versions(self, identifier):
        """GET /api/workflows/{id}/versions"""
        with self.owner.journal.lock:
            live = (self.owner._get(identifier).get("published") or {}).get("revision")
            rows = self.owner.journal.connection.execute("""SELECT revision, saved_at, data, published_at FROM workflow_versions
                WHERE workflow_id=? ORDER BY revision""", (identifier,)).fetchall()  # noqa: E501
        out, before = [], None
        for revision, saved_at, data, published_at in rows:
            graph = (json.loads(data).get("graph") or {})
            out.append({"revision": revision, "savedAt": saved_at, "nodes": len(graph.get("nodes") or []),
                        "edges": len(graph.get("edges") or []), "change": edit.change_line(before, graph),
                        "published": published_at is not None, "live": revision == live})
            before = graph
        return out[::-1]

    def diff(self, identifier, a, b):
        """GET /api/workflows/{id}/versions/{a}/diff/{b}: the nodes and edges added, removed and changed from a to b."""
        return edit.diff(self.version(identifier, a)["graph"], self.version(identifier, b)["graph"])

    def version(self, identifier, revision):
        """GET /api/workflows/{id}/versions/{revision}: that version as the API shows a workflow."""
        with self.owner.journal.lock:
            self.owner._get(identifier)
            row = self.owner.journal.connection.execute(
                "SELECT data FROM workflow_versions WHERE workflow_id=? AND revision=?",
                (identifier, revision)).fetchone()
        if row is None:
            raise APIError("That version isn't kept any more.", 404, "version_not_found")
        return self.owner.public(json.loads(row[0]))

    def call_graph(self, identifier, revision, *, allow_draft=False):
        """The saved graph a child call may run, with publication provenance checked under the same lock."""
        with self.owner.journal.lock:
            self.owner._get(identifier)
            row = self.owner.journal.connection.execute(
                "SELECT data, published_at FROM workflow_versions WHERE workflow_id=? AND revision=?",
                (identifier, revision)).fetchone()
            if row is None:
                raise APIError("That version isn't kept any more.", 404, "version_not_found")
            if row[1] is None and not allow_draft:
                raise APIError("Publish that saved version before another workflow runs it.", 409, "not_published")
            return json.loads(row[0])["graph"]

    def rollback(self, identifier, body):
        """POST /api/workflows/{id}/rollback {revision, current}: save version ``revision`` as the newest."""
        if not isinstance(body, dict) or set(body) != {"revision", "current"} or any(
                type(body[key]) is not int for key in body):
            raise ValueError("Give the revision to bring back and the current revision")
        with self.owner.lock, self.owner.journal.lock:
            workflow = self.owner._get(identifier)
            if body["current"] != workflow["revision"]:
                raise APIError("The workflow changed. Reload before rolling it back.", 409, "revision_conflict")
            row = self.owner.journal.connection.execute(
                "SELECT data FROM workflow_versions WHERE workflow_id=? AND revision=?",
                (identifier, body["revision"])).fetchone()
            if row is None:
                raise APIError("That version isn't kept any more.", 404, "version_not_found")
            old = json.loads(row[0])
            updated = dict(workflow)
            for key in RESTORED:
                if key in old:
                    updated[key] = old[key]
                else:
                    updated.pop(key, None)
            try:
                updated["graph"] = self.owner.check_graph(old.get("graph"), identifier)
            except ValueError as error:
                raise APIError(f"That version can't be saved now: {error}", 409, "version_invalid") from None
            updated["published"] = {"revision": workflow["revision"] + 1, "graph": updated["graph"]}
            self.owner.derive(updated)
            return self.owner._commit(workflow, updated)
