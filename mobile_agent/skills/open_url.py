"""OPEN_URL: jump straight to a screen with a deep link or a URL, instead of tapping through menus."""

import re
import time

from ..agent_hooks import Prepared, SkillResult

SPRINGBOARD = "com.apple.springboard"
SETTLE_SECONDS = .6


def prompt_line():
    return ("- OPEN_URL text: open a link or a deep link you know to jump straight to a screen in one action, "
            "instead of tapping there. For a web page, name the browser the task uses by its bundle id (the action's "
            "app; the target in the short reply); without one the browser on screen opens it, else the phone's "
            "default browser. If the screen it opens is wrong, navigate the usual way.")


def outside(ctx):
    """Whether an agent outside Mobster (MCP, Mobster Remote) started this run."""
    from ..permissions.policy import OUTSIDE_ORIGINS
    return getattr(getattr(ctx, "run", None), "origin", None) in OUTSIDE_ORIGINS


def remembered_for(ctx):
    """The person's "Don't ask again" list for this run (Runtime.permission_exceptions), or None."""
    if outside(ctx):
        return None
    runtime = getattr(getattr(ctx, "run", None), "runtime", None)
    store = getattr(runtime, "permission_exceptions", None)
    try:
        return store().contains if callable(store) else None
    except Exception:
        return None


WEB_FIELD = re.compile(r"\b(address|url|search or (type|enter) (web )?(address|url|website))\b", re.I)


def shows_web(snapshot):
    """The screen is a browser's: a web view, or an editable address field (any app, by what is on screen)."""
    for element in getattr(snapshot, "elements", ()) or ():
        if element.role == "WebView" or "XCUIElementTypeWebView" in (getattr(element, "locator", "") or ""):
            return True  # a web view, or content inside one (snapshots keep web content, not the view)
        if element.editable and WEB_FIELD.search(f"{element.label or ''} {element.value or ''}"):
            return True
    return False


def is_web(url):
    return str(url or "").strip().casefold().startswith(("http://", "https://"))


class OpenUrl:
    """agent_hooks.Skill: OPEN_URL."""

    op = "OPEN_URL"
    needs_target = False

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.prompt = prompt_line()

    def available(self, ctx):
        return callable(getattr(ctx.driver, "call", None))

    def prepare(self, ctx, act, snapshot):
        from ..agent_hooks import safe_allows
        from ..deeplinks import app_for, decide
        url = " ".join(str(act.get("text") or "").split())
        decision = decide(url, remembered_for(ctx))  # LinkError: frontier reports the type; perform never runs
        # The app the link opens in: its own (the map's), or for a web page the browser the model named.
        bundle = app_for(url) or (act.get("app") if is_web(url) else None)
        if bundle and bundle != getattr(snapshot, "bundle_id", None) and getattr(ctx, "guard", None) is not None \
                and not safe_allows(ctx.guard, bundle):
            # The app the link opens hasn't been allowed (its first-use card asked, or it is off): never opened.
            said = getattr(ctx.guard, "blocked_message", None)
            try:
                why = said(bundle) if callable(said) else None
            except Exception:
                why = None
            return Prepared(None, {"refused": why or "Mobster may not use the app this link opens."})
        if decision.action == "open":
            return Prepared(None, {"url": url})
        return Prepared(decision.title, {"url": url}, remember=None if outside(ctx) else decision.remember())

    def perform(self, ctx, prepared, act, snapshot):
        from ..deeplinks import LinkError, app_for, open_link
        if prepared.state.get("refused"):
            return SkillResult(feedback=f"Refused: {prepared.state['refused']} Do the task another way, or finish "
                                        "and say what is left for the user.")
        url = prepared.state["url"]
        driver = ctx.driver
        before = getattr(snapshot, "bundle_id", "") or ""
        bundle = app_for(url)
        if not bundle and is_web(url):
            # The browser the task uses: the app the model named, else the app in front when a web page can open
            # there (a Safari task's link opens in Safari, H3 10 Oct: unnamed, it opened in Chrome, the default).
            bundle = act.get("app") or (before if before and before != SPRINGBOARD and shows_web(snapshot) else None)
        try:
            # One open, never retried elsewhere: a failure's outcome is unknown (the page may have opened).
            opened = open_link(driver, url, bundle=bundle or None, clock=self.clock, sleep=self.sleep)
        except LinkError as error:
            return SkillResult(feedback=f"{error} Navigate there the usual way.")
        except Exception as error:
            ctx.emit({"event": "deep_link", "url": url[:200], "ok": False, "error": type(error).__name__})
            return SkillResult(feedback=f"The link didn't open ({type(error).__name__}). Navigate there the usual "
                                        "way.")
        landed = bundle
        active = getattr(driver, "active_app", None)
        if not landed and callable(active):
            try:
                landed = active(timeout=3)
            except Exception:
                landed = ""
            if landed and landed not in (before, SPRINGBOARD):
                # Later reads describe the app the link opened (the driver hints the app it activates).
                try:
                    driver.call("POST", "/wda/apps/activate", {"bundleId": landed}, timeout=15)
                except Exception:
                    pass
        ctx.emit({"event": "deep_link", "url": url[:200], "ok": True, "app": landed or None,
                  "prompt": opened.get("prompt")})
        where = f" in {landed}" if landed and landed != before else ""
        answered = f" (answered “{opened['prompt']}” to iOS's Open in prompt)" if opened.get("prompt") else ""
        return SkillResult(feedback=f"Opened the link{where}{answered}. Check the screen: if it isn't the one you "
                                    "wanted, navigate there the usual way.", changed=True)
