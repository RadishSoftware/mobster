"""Import from GitHub: the repositories your GitHub account can open, through the GitHub connector's sign-in, and that
sign-in for cloning one of them.

``repositories(token)`` lists them, the one pushed to last first: owner/name, whether it's private, its language and
default branch and when it was last pushed. ``clone_env(token)`` is what git needs to clone a private repository from
github.com with that sign-in: an ``http.https://github.com/.extraheader`` set through git's environment, so the token
is never in a command line, a URL, a log or the clone's own .git/config, and is sent to github.com only.

Mobster never asks for more than the connector's sign-in already allows, and never keeps the list.
"""

import base64
import json
import os
import urllib.error
import urllib.request

from ..api_errors import APIError

API = "https://api.github.com"
PRESET = "github"
PAGES = 3
PER_PAGE = 100
TIMEOUT = 15
SIGN_IN_AGAIN = "GitHub didn't accept Mobster's sign-in. Sign in to GitHub again in Connectors, then try again."
OFFLINE = "Mobster couldn't reach GitHub. Check your connection, then try again."


def token_of(runtime):
    """The GitHub connector's sign-in, or None when there is none (or the Keychain won't give it)."""
    from ..connectors import service as connectors
    service = connectors.service_for(runtime)
    if service is None:
        return None
    try:
        return service.preset_bearer(PRESET)
    except APIError:
        return None


def repositories(token, *, opener=urllib.request.urlopen):
    """Every repository the account can open (owner, collaborator, organization member), newest push first, at most
    PAGES * PER_PAGE of them. APIError with what to do when GitHub refuses or can't be reached."""
    out = []
    for page in range(1, PAGES + 1):
        request = urllib.request.Request(
            f"{API}/user/repos?per_page={PER_PAGE}&page={page}&sort=pushed"
            "&affiliation=owner,collaborator,organization_member",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Mobster"})
        try:
            with opener(request, timeout=TIMEOUT) as response:
                rows = json.loads(response.read(8_000_000).decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise APIError(SIGN_IN_AGAIN, 401, "github_sign_in") from None
            raise APIError(f"GitHub answered {error.code}. Try again in a minute.", 502, "github_error") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise APIError(OFFLINE, 503, "offline") from None
        if not isinstance(rows, list):
            raise APIError("GitHub sent back something Mobster didn't expect. Try again.", 502, "github_error")
        out += [repo for repo in (_repo(row) for row in rows) if repo is not None]
        if len(rows) < PER_PAGE:
            break
    return out


def pulls(token, repo, *, opener=urllib.request.urlopen):
    """The repository's pull requests from its own branches, the one updated last first (one page of PER_PAGE), as
    {branch: {number, title, url, state (open, draft, merged or closed)}}: the newest for each branch. A pull request
    from a fork names a branch of another repository, so it is left out. APIError as ``repositories``."""
    request = urllib.request.Request(
        f"{API}/repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page={PER_PAGE}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Mobster"})
    try:
        with opener(request, timeout=TIMEOUT) as response:
            rows = json.loads(response.read(8_000_000).decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise APIError(SIGN_IN_AGAIN, 401, "github_sign_in") from None
        if error.code == 404:   # a repository the sign-in can't see
            return {}
        raise APIError(f"GitHub answered {error.code}. Try again in a minute.", 502, "github_error") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise APIError(OFFLINE, 503, "offline") from None
    out = {}
    for row in rows if isinstance(rows, list) else ():
        pull = _pull(row, repo)
        if pull is not None:
            out.setdefault(pull.pop("branch"), pull)
    return out


def _pull(row, repo):
    """A pull request as runs by branch show it, checked at the boundary; None for one that isn't, or a fork's."""
    head = row.get("head") if isinstance(row, dict) else None
    head_repo = head.get("repo") if isinstance(head, dict) else None
    if not (isinstance(head, dict) and isinstance(head.get("ref"), str) and isinstance(row.get("number"), int)
            and isinstance(head_repo, dict) and str(head_repo.get("full_name", "")).lower() == repo.lower()):
        return None
    state = "merged" if row.get("merged_at") else "draft" if row.get("draft") is True and row.get("state") == "open" \
        else "open" if row.get("state") == "open" else "closed"
    title = row["title"][:200] if isinstance(row.get("title"), str) else ""
    return {"branch": head["ref"], "number": row["number"], "title": title,
            "url": f"https://github.com/{repo}/pull/{row['number']}", "state": state}


def _repo(row):
    """A repository as the Add page shows it, checked at the boundary; None for one that isn't."""
    if not isinstance(row, dict) or not isinstance(row.get("full_name"), str) or "/" not in row["full_name"]:
        return None
    owner, name = row["full_name"].split("/", 1)
    text = lambda key, high=200: row[key][:high] if isinstance(row.get(key), str) else None  # noqa: E731
    return {"repo": row["full_name"], "owner": owner, "name": name, "private": row.get("private") is True,
            "description": text("description", 300), "language": text("language", 40),
            "defaultBranch": text("default_branch", 200), "pushedAt": text("pushed_at", 40),
            "archived": row.get("archived") is True, "url": f"https://github.com/{row['full_name']}.git"}


def clone_env(token, base=None):
    """git's environment for a clone from github.com with the connector's sign-in: http.extraheader for that host
    only, added after any GIT_CONFIG_* entries ``base`` (os.environ) already has, so none of them is lost."""
    base = os.environ if base is None else base
    try:
        count = max(0, int(base.get("GIT_CONFIG_COUNT", "0")))
    except ValueError:
        count = 0
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": str(count + 1), f"GIT_CONFIG_KEY_{count}": "http.https://github.com/.extraheader",
            f"GIT_CONFIG_VALUE_{count}": f"AUTHORIZATION: basic {basic}"}
