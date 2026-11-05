"""Every link the command line and the terminal UI show, in one place.

They all point at the docs site, https://docs.mobster.dev, never at a repository path: someone who installed
Mobster with Homebrew or the install script has no `docs/` folder, and the public mirror moves. The docs keep
these paths working (they add a redirect when a page moves), and a test checks each one names a docs page.
"""

DOCS = "https://docs.mobster.dev"


def page(slug="", anchor=""):
    """``DOCS`` + ``/slug`` (+ ``#anchor``)."""
    return DOCS + (f"/{slug.strip('/')}" if slug else "") + (f"#{anchor}" if anchor else "")


TROUBLESHOOTING = page("troubleshooting")
SCRIPTING = page("scripting")
TUI = page("tui")

# Where `mobster update` and the daily check read the latest release: the releases repository that Mobster for Mac's
# feed (UPDATE_FEED) and mobster.dev/download read too. Never the open-source engine's repository, whose tags are the
# engine's.
RELEASES_API = "https://api.github.com/repos/RadishSoftware/mobster-releases/releases/latest"
# The asset that makes a release Mobster for Mac's (APP_DOWNLOAD redirects to it). A release without it isn't offered.
APP_ASSET = "Mobster.dmg"
# New versions of Mobster, the mobster command included, come in Mobster for Mac (13 Oct 2026). The update line and
# `mobster update` point here instead of running an installer.
APP_DOWNLOAD = "https://mobster.dev/download"
# The update feed Mobster for Mac reads. Its `minimum_version`, when set, is the oldest version still allowed to run,
# for the CLI too (they share one version).
UPDATE_FEED = "https://mobster.dev/updates/latest.json"
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
