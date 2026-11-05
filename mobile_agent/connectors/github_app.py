"""Mobster Checks, Mobster's GitHub App for pull requests' checks (docs/ci/github-app.mdx): the agent's half."""

import re

from .. import plan_gate, share
from ..api_errors import APIError

PRESET = "github"
INSTALL_URL = "https://github.com/apps/mobster-checks/installations/new"
REPO = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
TOKEN = re.compile(r"[A-Za-z0-9_]{20,255}")
EXPIRES = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
NO_GITHUB = ("Connect GitHub in Mobster (Connectors), so Mobster Checks can tell you can push to this repository. "
             "Then run the checks again.")
SIGN_IN = "Sign in to Mobster, then run the checks again."
CODES = ("not_installed", "no_access", "not_allowed", "plan_required", "github_sign_in", "sign_in", "unconfigured")


def account_file(runtime):
    """The account.json this agent's plan check reads, else the Mac app's."""
    gate = getattr(runtime, "plan_gate", None)
    return gate.account_file if gate is not None else plan_gate.app_account_file()


def token(service, repo, account, confirm=None):
    """{token, expiresAt, repo} for ``repo`` (owner/name), or APIError with the reason and what to do. ``confirm(repo)``
    runs before Mobster's server is asked: it raises to refuse."""
    if not isinstance(repo, str) or not REPO.fullmatch(repo) or repo.split("/")[1] in (".", ".."):
        raise APIError("repo must be a GitHub repository as owner/name.", 400, "invalid_repo")
    stored = plan_gate.stored_account(account)
    if stored is None:
        raise APIError(SIGN_IN, 401, "sign_in")
    github = service.preset_bearer(PRESET)
    if not github:
        raise APIError(NO_GITHUB, 409, "no_github")
    if confirm is not None:
        confirm(repo)
    try:
        status, value = share.call("POST", "/api/github/token", stored["entitlement"]["token"],
                                   {"repo": repo, "github": github})
    except share.ShareError as error:
        raise APIError(str(error), error.status, error.code) from None
    finally:
        github = None  # noqa: F841 -- the sign-in's only other reference goes with this frame
    if status == 200:
        minted, expires, named = value.get("token"), value.get("expires_at"), value.get("repo")
        if not (isinstance(minted, str) and TOKEN.fullmatch(minted) and isinstance(expires, str)
                and EXPIRES.fullmatch(expires) and isinstance(named, str) and REPO.fullmatch(named)):
            raise APIError("Mobster's server answered without a token. Run the checks again.", 502, "error")
        return {"token": minted, "expiresAt": expires, "repo": named}
    code = value.get("error") if value.get("error") in CODES else "error"
    message = value.get("message") if isinstance(value.get("message"), str) else \
        "Mobster's server couldn't get a GitHub token. Run the checks again in a minute."
    extra = {"installUrl": INSTALL_URL} if code in ("not_installed", "not_allowed") else {}
    raise APIError(message[:400], status if 400 <= status < 600 else 502, code, **extra)
