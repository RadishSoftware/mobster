"""A turn's section of Mobster's iOS guide (kb.py): on iOS's own screens (the Home Screen, the App Library,
Spotlight, Control Center, Settings, a keyboard), the few entries about that screen and the request, for the phone's
iOS version. The engine gets it through harness_api's "ios_slice" hook (engine_hooks.py); elsewhere it is "".

Measured 14 Oct 2026 (paired A/B, 12 Settings tasks on an iOS 26.4 simulator, Sonnet 5.5 low, one run per arm): with
the section, 12/12 passed in 71 model calls, 43 actions and $0.328; without it, 12/12 in 79 calls, 55 actions and
$0.338. An IOS_DOCS lookup tool offered instead was never called (0 of 12 runs) and was removed. The ``ios_docs`` MCP
tool (mcp.py) answers the same guide for other agents."""

from . import kb, system


def turn_slice(snapshot, request, driver):
    """The section for this turn's screen, or ""."""
    return kb.slice_text(getattr(snapshot, "bundle_id", ""), [e.label for e in snapshot.elements],
                         system.ios_version(driver), getattr(snapshot, "keyboard", ""), request)
