"""Runs by branch on a project's page: the project's suites carry the branch and commit they tested (testkit/gitref.py
through results.json), and GET /api/projects/{id}/pulls names each branch's pull request through the GitHub
connector's sign-in. A fake GitHub answers; real `git init` repositories give the remote. Offline."""

import json
import urllib.error
from unittest import mock

from mobile_agent.api_errors import APIError
from mobile_agent.projects import routes
from mobile_agent.projects import service as project_service
from mobile_agent.tests.test_projects import Base, FakeResponse, git


def pull(number, branch, *, repo="getmobster/firefox", state="open", merged=False, draft=False, title=None):
    return {"number": number, "title": title or f"Change {number}", "state": state, "draft": draft,
            "merged_at": "2026-10-17T10:00:00Z" if merged else None,
            "head": {"ref": branch, "repo": {"full_name": repo}}}


class PullsTests(Base):
    def setUp(self):
        super().setUp()
        self.requests = []
        self.rows = [pull(41, "streak-fix", draft=True), pull(40, "onboarding-v2", state="closed", merged=True),
                     pull(39, "streak-fix", state="closed"), pull(38, "main", repo="someone/firefox"),
                     {"number": "x"}, "junk"]
        self.answer = None

        def opener(request, timeout):
            self.requests.append(request.full_url)
            if self.answer is not None:
                raise self.answer
            return FakeResponse(self.rows)
        self.service._opener = opener
        self.path = self.app_folder()
        git("init", "-q", cwd=self.path)
        git("remote", "add", "origin", "git@github.com:getmobster/Firefox.git", cwd=self.path)
        self.project, _ = self.service.add({"path": str(self.path)})

    def signed_in(self, token="gho_signedin"):
        patcher = mock.patch("mobile_agent.projects.github.token_of", return_value=token)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_branch_gets_its_newest_pull_request_and_forks_are_left_out(self):
        self.signed_in()
        found = self.service.pulls(self.project["id"])
        self.assertEqual((found["signedIn"], found["repo"]), (True, "getmobster/Firefox"))
        self.assertEqual(found["pulls"], {
            "streak-fix": {"number": 41, "title": "Change 41", "url": "https://github.com/getmobster/Firefox/pull/41",
                           "state": "draft"},
            "onboarding-v2": {"number": 40, "title": "Change 40", "url": "https://github.com/getmobster/Firefox/pull/40",
                              "state": "merged"}})
        self.assertIn("/repos/getmobster/Firefox/pulls?state=all", self.requests[0])
        self.service.pulls(self.project["id"])
        self.assertEqual(len(self.requests), 1)        # kept a minute

    def test_without_a_sign_in_or_a_github_remote_nothing_is_asked(self):
        self.signed_in(None)
        self.assertEqual(self.service.pulls(self.project["id"]), {"signedIn": False, "repo": "getmobster/Firefox",
                                                                   "pulls": {}})
        other = self.app_folder("Ledger")
        git("init", "-q", cwd=other)
        git("remote", "add", "origin", "https://gitlab.com/getmobster/ledger.git", cwd=other)
        project, _ = self.service.add({"path": str(other)})
        self.assertEqual(self.service.pulls(project["id"])["pulls"], {})
        self.assertEqual(self.requests, [])

    def test_a_refused_sign_in_says_what_to_do_and_a_hidden_repository_has_none(self):
        self.signed_in()
        self.answer = urllib.error.HTTPError("u", 404, "no", {}, None)
        self.assertEqual(self.service.pulls(self.project["id"])["pulls"], {})
        self.service._pulls.clear()
        self.answer = urllib.error.HTTPError("u", 401, "no", {}, None)
        with self.assertRaises(APIError) as caught:
            self.service.pulls(self.project["id"])
        self.assertEqual(caught.exception.code, "github_sign_in")

    def test_the_route_needs_the_mac_apps_session(self):
        from mobile_agent import api_routes
        self.signed_in()
        project_service._services[id(self.runtime)] = self.service
        self.addCleanup(project_service._services.pop, id(self.runtime), None)
        self.runtime.local_api = {"app_session": "s"}
        path = f"/api/projects/{self.project['id']}/pulls"
        request = api_routes.Request(runtime=self.runtime, method="GET", path=path,
                                     params={"id": self.project["id"]}, query={}, headers={}, body=None,
                                     content_type=None, origin="app", app_session=False)
        with self.assertRaises(APIError) as caught:
            routes.pulls(request)
        self.assertEqual(caught.exception.code, "app_only")
        self.assertEqual(self.requests, [])


class SuitesTests(Base):
    def test_the_projects_suites_carry_their_branch(self):
        from mobile_agent.testkit import recent
        path = self.app_folder()
        project, _ = self.service.add({"path": str(path)})
        for index, branch in enumerate(("main", "streak-fix", None)):
            suite = f"20261018-18000{index}-000{index}"
            folder = path / ".mobster" / "test-results" / suite
            folder.mkdir(parents=True)
            doc = {"schema": "mobster.test/1", "suite": suite, "startedAt": index, "exitCode": 0,
                   "summary": {"passed": 1, "total": 1}, "checks": []}
            if branch:
                doc["git"] = {"branch": branch, "head": f"{index}" * 40}
            (folder / "results.json").write_text(json.dumps(doc))
            recent.remember(doc, folder, path)
        self.assertEqual([(suite["suite"][-4:], suite["git"]) for suite in self.service.tests(project["id"])["suites"]],
                         [("0002", None), ("0001", {"branch": "streak-fix", "head": "1" * 40}),
                          ("0000", {"branch": "main", "head": "0" * 40})])
