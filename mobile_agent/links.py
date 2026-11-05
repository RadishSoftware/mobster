"""Product links used by the command line and terminal UI.

Documentation uses stable docs.mobster.dev paths. Release metadata and downloads stay with the registry or
repository that owns each distribution.
"""

DOCS = "https://docs.mobster.dev"


def page(slug="", anchor=""):
    """``DOCS`` + ``/slug`` (+ ``#anchor``)."""
    return DOCS + (f"/{slug.strip('/')}" if slug else "") + (f"#{anchor}" if anchor else "")


TROUBLESHOOTING = page("troubleshooting")
SCRIPTING = page("scripting")
TUI = page("tui")

# Standalone Python installs (including the npm launcher) use PyPI. Frozen CLI builds use the signed archive
# published by packaging/cli/build.sh. App-owned agents defer to Mobster for Mac's own updater.
PYPI_API = "https://pypi.org/pypi/mobster-cli/json"
PYPI_PROJECT = "https://pypi.org/project/mobster-cli/"
CLI_RELEASES = "https://github.com/RadishSoftware/mobster-releases/releases"
CLI_RELEASES_API = "https://api.github.com/repos/RadishSoftware/mobster-releases/releases/latest"
CLI_ASSETS = frozenset({"mobster-macos-arm64.tar.gz", "mobster-macos-arm64.tar.gz.sha256"})
# The JSON Schema of a check file (verify/check_schema.py), served from mobster.dev. A check file Mobster saves names
# it on its first line, so an editor with a YAML language server completes and checks the file.
CHECK_SCHEMA = "https://mobster.dev/schema/check.json"
# "Made with Mobster": the credit a shared result's page and the reports end with. The PR comment ends with "Tested
# with Mobster" only when asked (`mobster test pr --tested-with`). Each surface has its own short path on mobster.dev,
# which redirects to a page with UTMs, so a visit is counted even from the share page, which sends no referrer.
# site/vercel.json holds the redirects; site/tests/config.test.mjs fails when a path written here has none. The paths
# are written out in full for that test to find.
MADE_WITH_PR = "https://mobster.dev/s/pr"
MADE_WITH_SHARE = "https://mobster.dev/s/share"
MADE_WITH_REPORT = "https://mobster.dev/s/report"
MADE_WITH_XRAY = "https://mobster.dev/s/xray"
