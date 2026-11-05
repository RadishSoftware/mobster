"""Sign Mobster up with each one-click connector's vendor, live, the way Settings › Connectors' Connect does, and stop
before anyone signs in. Run it before a new preset ships and when a vendor changes its sign-in.

    mobile_agent/.venv/bin/python scripts/connector_handshake.py            # every preset with auth "oauth"
    mobile_agent/.venv/bin/python scripts/connector_handshake.py sentry jira

Per preset: the server's 401 (its resource_metadata), discovery, dynamic client registration with Mobster's loopback
redirect (oauth.SignIn.begin, the app's own code), then a GET of the authorize URL, which shows where your browser would
land: the vendor's sign-in page when it took the client and the redirect. Each run registers one client at each vendor
it checks. It signs in to nothing, needs no account and prints no secret. It uses the network, so no test runs it.
"""

import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mobile_agent.connectors import client as mcp  # noqa: E402
from mobile_agent.connectors import oauth, presets  # noqa: E402

BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/605.1.15"


def resource_metadata(url):
    """What Connect reads first: the resource_metadata of the server's 401, or None."""
    try:
        probe = mcp.HttpSession(url, name="handshake")
        probe.initialize(timeout=10)
        probe.close()
    except mcp.AuthNeeded:
        return probe.resource_metadata
    except mcp.ConnectorError:
        pass
    return None


def landing(authorize_url):
    """Where the browser lands on the authorize URL: "<status> <host><path>", or why it couldn't tell."""
    try:
        with urlopen(Request(authorize_url, headers={"User-Agent": BROWSER, "Accept": "text/html"}),
                     timeout=20) as page:
            status, final = page.status, page.geturl()
    except HTTPError as error:
        status, final = error.code, error.geturl()
    except OSError as error:
        return f"no answer ({type(error).__name__})"
    parts = urlsplit(final)
    return f"{status} {parts.netloc}{parts.path}"


def check(preset):
    flow = oauth.SignIn(preset["url"], lambda ended, tokens, message: None, scopes=preset.get("scopes"),
                        redirect_host=preset.get("redirect_host", oauth.LOOPBACK_HOSTS[0]),
                        resource_metadata=resource_metadata(preset["url"]), wait=5)
    try:
        authorize = flow.begin()
    except oauth.OAuthError as error:
        return [preset["key"], f"refused: {error.message}", "", "", "", ""]
    try:
        scope = (parse_qs(urlsplit(authorize).query).get("scope") or [""])[0]
        how = "registered" if flow.registration else "metadata document" if \
            flow.plan.client["client_id"] == oauth.CLIENT_METADATA_URL else "its own app"
        return [preset["key"], how, "yes" if flow.plan.client.get("client_secret") else "no", flow.plan.redirect,
                scope if len(scope) <= 60 else f"{len(scope.split())} scopes", landing(authorize)]
    finally:
        flow.cancel()


def main(keys):
    chosen = [presets.preset(key) for key in keys] if keys else [p for p in presets.PRESETS if p["auth"] == "oauth"]
    unknown = [key for key, preset in zip(keys, chosen) if preset is None]
    if unknown:
        raise SystemExit(f"No preset named {', '.join(unknown)}.")
    print("| Preset | Client | Secret issued | Redirect | Scope asked | Authorize page |")
    print("|---|---|---|---|---|---|")
    for preset in chosen:
        print("| " + " | ".join(check(preset)) + " |", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
