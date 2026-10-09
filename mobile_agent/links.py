"""Every link the command line and the terminal UI show, in one place.

They all point at the docs site, https://docs.mobster.dev, never at a repository path: someone who installed
Mobster with Homebrew or the install script has no `docs/` folder, and the public mirror moves. The docs keep
these paths working (they add a redirect when a page moves), and a test checks each one names a docs page.
"""

DOCS = "https://docs.mobster.dev"


def page(slug="", anchor=""):
    """``DOCS`` + ``/slug`` (+ ``#anchor``)."""
    return DOCS + (f"/{slug.strip('/')}" if slug else "") + (f"#{anchor}" if anchor else "")


HOME = page()
GETTING_STARTED = page("getting-started")
DEVICE_SETUP = page("device-setup")
CONFIGURATION = page("configuration")
TROUBLESHOOTING = page("troubleshooting")
TESTING = page("testing")
CHANGELOG = page("changelog")
SCRIPTING = page("scripting")
TUI = page("tui")
CODING_AGENTS = page("mcp-server")
CLI = page("cli")

# Where `mobster update` and the daily check read the latest release (the public repository's releases).
RELEASES_API = "https://api.github.com/repos/RadishSoftware/mobster/releases/latest"
INSTALL_SCRIPT = "https://mobster.dev/install.sh"
