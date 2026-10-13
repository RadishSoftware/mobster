"""The benchmark's own reads of the phone, independent of the run under test.

The agent decides whether it is finished by comparing its own evidence against
its own goal. That is model agreement, not verification. The bench grades a run
by reading the phone itself: ``WDAProbe`` NEVER reads the agent's evidence,
citations, screenshots or completion claim. It opens its own accessibility
connection to the phone, reads the tree itself, and the bench's checks
(``bench/checks.py``, ``bench/truth.py``) compare what it finds against ground
truth declared before the run started. A grader that consulted the agent's own
record would only re-certify the agent's opinion.
"""

import re
import time

from ..drivers import WDA, WDA_SOURCE_PATH
from ..errors import MobsterError
from ..state import from_wda
from ..transport import HTTP


class ProbeUnavailable(MobsterError):
    """The harness could not read the device itself, so nothing is graded."""


# Measured on iOS 26.0.1 (2026-09-23): WDA reported this invisible Siri/Apple
# Intelligence edge-light window as the active app while Safari was in front.
SYSTEM_OVERLAYS = frozenset({"com.apple.siri.IntelligentLight"})
SPRINGBOARD = "com.apple.springboard"


class WDAProbe:
    """The harness's own eyes on a USB iPhone, separate from the run under test.

    Reads go through WDA's sessionless routes on a dedicated HTTP connection:
    the probe never shares the agent's driver object, its evidence, or its
    cached observations. WDA serves one session per device, so resets (which
    must act) use that session through a WDA driver owned by the probe.
    """

    # Apps whose foreground check needed the hinted source read, per WDA URL (process-wide).
    _overlay_apps = {}

    def __init__(self, url, session=None):
        self.url = url.rstrip("/")
        self.http = HTTP(self.url)
        self._overlay_seen = False
        self.session = session or self._current_session()

    def _current_session(self):
        try:
            session = self.http.request("GET", "/status", timeout=5).get("sessionId")
        except Exception as error:
            raise ProbeUnavailable(f"WDA status unavailable: {error}") from None
        if not isinstance(session, str) or not session:
            raise ProbeUnavailable("WDA has no active session")
        return session

    def _get(self, path, timeout):
        value = WDA.response_value(self.http.request("GET", path, timeout=timeout))
        return value

    def foreground(self, timeout=5, expected=None):
        """The foreground app. WDA's activeAppInfo is the fast route, except while
        a see-through system layer is up: then it took ~8 s and named the layer
        (measured 2026-09-23), and the session's own tree names the app instead."""
        if not self._overlay_seen and expected not in WDAProbe._overlay_apps.get(self.url, ()):
            # One short ask. With Safari in front activeAppInfo hangs on the Siri overlay
            # (measured 24 Sep: 11 s, then the fallback's settings call queued behind it
            # and timed out); WDA serves one request at a time, so waiting longer only
            # delays the path that works.
            try:
                value = self._get("/wda/activeAppInfo", min(timeout, 3))
            except Exception:
                value = None
            bundle = value.get("bundleId") if isinstance(value, dict) else None
            # On a simulator activeAppInfo names SpringBoard whatever is in front (iOS 26.4, 10 Oct: every
            # reset failed "not in the foreground"): the session's tree, hinted with the app, names it instead.
            stale = bundle == SPRINGBOARD and expected not in (None, SPRINGBOARD) and self._simulator()
            if bundle == SPRINGBOARD and expected not in (None, SPRINGBOARD) and not stale:
                # A phone on iOS 26.4 does the same once an app it was asked to open isn't there (the bench phone, 11 Oct:
                # after activating a deleted Mail, every later read named SpringBoard with Settings in front). The
                # session's own activeAppInfo answers in one short read. A hinted tree read is not asked here: it
                # names the hinted app even when SpringBoard is in front.
                bundle = self._session_app(min(timeout, 3)) or bundle
            if isinstance(bundle, str) and bundle and bundle not in SYSTEM_OVERLAYS and not stale:
                return bundle
            self._overlay_seen = True
            if expected:
                WDAProbe._overlay_apps.setdefault(self.url, set()).add(expected)
        try:
            if expected:
                # Name the app we expect as WDA's default active application,
                # as the agent's driver does (WDA.observe).
                # Patient: the request it follows may still hold WDA for several seconds.
                self._get_session_settings(expected, max(timeout, 15))
            snapshot = from_wda(self._get(f"/session/{self.session}{WDA_SOURCE_PATH}", max(timeout, 25)))
        except Exception as error:
            raise ProbeUnavailable(f"foreground unavailable: {error}") from None
        if not snapshot.bundle_id:
            raise ProbeUnavailable("WDA did not report a foreground app")
        return snapshot.bundle_id

    def _session_app(self, timeout):
        """The foreground app by the session's own activeAppInfo, or None."""
        try:
            value = self._get(f"/session/{self.session}/wda/activeAppInfo", timeout)
        except Exception:  # noqa: BLE001 -- unknown: the caller's other paths decide
            return None
        bundle = value.get("bundleId") if isinstance(value, dict) else None
        return bundle if isinstance(bundle, str) and bundle and bundle not in SYSTEM_OVERLAYS else None

    def observe(self, bundle_id, timeout=10):
        """A fresh read, stamped with the foreground app read before AND after it,
        so a read that straddled an app switch is refused rather than graded."""
        try:
            before = self.foreground(timeout=min(5, timeout), expected=bundle_id)
            # Through the session, so the foreground hint WDA needs on iOS 26
            # (see WDA.observe) applies to the probe's reads as well.
            hinted = WDA._overlay_apps.get(self.url, ())
            if bundle_id in hinted:
                self._get_session_settings(bundle_id, timeout)
            snapshot = from_wda(self._get(f"/session/{self.session}{WDA_SOURCE_PATH}", timeout))
            after = self.foreground(timeout=min(5, timeout), expected=bundle_id)
        except ProbeUnavailable:
            raise
        except Exception as error:
            raise ProbeUnavailable(f"observation failed: {error}") from None
        if before != after:
            raise ProbeUnavailable(f"foreground changed during the read ({before} -> {after})")
        snapshot.bundle_id = after
        return snapshot

    def _simulator(self):
        """True when this WDA runs on a simulator (its /status names a simulator version); asked once."""
        if getattr(self, "_is_simulator", None) is None:
            try:
                status = self._get("/status", 3)
                ios = status.get("ios") if isinstance(status, dict) else None
                self._is_simulator = bool(isinstance(ios, dict) and ios.get("simulatorVersion"))
            except Exception:  # noqa: BLE001 -- unknown: a phone, asked again next time
                return False
        return self._is_simulator

    def _get_session_settings(self, bundle_id, timeout):
        WDA.response_value(self.http.request(
            "POST", f"/session/{self.session}/appium/settings",
            {"settings": {"defaultActiveApplication": bundle_id}}, timeout=timeout))

    def driver(self):
        driver = WDA(self.url, self.session)
        driver.configure()
        return driver

    def reset(self, bundle_id, url=None, max_back=12):
        """Relaunch the app and pop it to its root screen, verified.

        iOS restores an app's navigation stack across relaunches, so a task could
        start on the screen its answer is on. The probe pops back through the
        navigation bar until no back button remains, and confirms the app.
        """
        try:
            # WDA issues a new session whenever its runner restarts.
            self.session = self._current_session()
        except ProbeUnavailable:
            pass
        driver = None
        try:
            driver = self.driver()
            for path in ("/wda/apps/terminate", "/wda/apps/activate"):
                driver.call("POST", path, {"bundleId": bundle_id}, timeout=15)
            if url:
                # Bound to the task's app: a bare /url opens the default browser
                # (Chrome on the measured phone), not the app under test.
                driver.call("POST", "/url", {"url": url, "bundleId": bundle_id}, timeout=15)
            # activate can return while SpringBoard still owns the foreground.
            # The first read after a launch can be slow once (a system overlay); keep
            # asking within the window instead of failing the task's setup on it.
            launched = time.monotonic() + 20
            while time.monotonic() < launched:
                try:
                    if self.foreground(expected=bundle_id) == bundle_id:
                        break
                except ProbeUnavailable:
                    pass
                time.sleep(.1)
            popped = False
            for _ in range(max_back):
                # Room for one failed read on a system overlay plus its hinted retry.
                snapshot = driver.observe_ready(timeout=20)
                if self.foreground(expected=bundle_id) != bundle_id:
                    raise ProbeUnavailable(f"{bundle_id} is not in the foreground after launch")
                if url:
                    # The URL is the start state; Back would walk browser history.
                    snapshot.bundle_id = bundle_id
                    return snapshot
                viewer = next((e for e in snapshot.elements if e.role == "Button" and e.rect[1] < .12
                               and (e.label or "").strip().casefold() == "close"), None)
                if viewer is not None:
                    # A document viewer left open (Quick Look in Files): close it first.
                    driver.execute("TAP", viewer, snapshot, timeout=10)
                    driver.wait_for_change(snapshot, timeout=3)
                    continue
                search = next((e for e in snapshot.elements if e.role == "SearchField"
                               and (e.value or "").strip() not in ("", (e.label or "").strip())), None)
                dismiss = search and next((e for e in snapshot.elements if e.role == "Button"
                                           and (e.label or "").strip().casefold() in ("close", "cancel")
                                           and abs(e.center[1] - search.center[1]) < .04), None)
                if dismiss:
                    # A search left open (Notes keeps its query and results): close it, so the
                    # task starts on the app's own screen (pass 6: three Notes tasks began there).
                    driver.execute("TAP", dismiss, snapshot, timeout=10)
                    driver.wait_for_change(snapshot, timeout=3)
                    continue
                back = navigation_back(snapshot)
                pop = RESET_POP_TABS.get(bundle_id)
                if back is None and pop and not popped:
                    # Files' folder views publish no labelled Back button; re-tapping the
                    # selected tab pops its stack to the root (Browse), as a user would.
                    popped = True
                    tab = next((e for e in snapshot.elements if e.label == pop and e.role == "Button"), None)
                    if tab is not None:
                        for press in range(2 if (tab.value or "").strip() == "1" else 1):
                            if press:
                                snapshot = driver.observe_ready(timeout=10)
                                tab = next((e for e in snapshot.elements if e.label == pop and e.role == "Button"),
                                           None)
                                if tab is None:
                                    break
                            driver.execute("TAP", tab, snapshot, timeout=10)
                            time.sleep(.6)
                        continue
                if back is None:
                    tab = RESET_TABS.get(bundle_id)
                    selected = next((e for e in snapshot.elements if e.label == tab and e.role == "Button"), None)
                    if tab and selected is not None and (selected.value or "").strip() != "1":
                        # A tabbed app reopens on its last tab; the task's start is its first one
                        # (nav.clock_stopwatch started on Stopwatch and needed no action).
                        driver.execute("TAP", selected, snapshot, timeout=10)
                        driver.wait_for_change(snapshot, timeout=3)
                        continue
                    snapshot.bundle_id = bundle_id
                    return snapshot
                driver.execute("TAP", back, snapshot, timeout=10)
                driver.wait_for_change(snapshot, timeout=3)
            raise ProbeUnavailable(f"{bundle_id} did not reach its root screen")
        except ProbeUnavailable:
            raise
        except Exception as error:
            raise ProbeUnavailable(f"reset failed: {error}") from None
        finally:
            if driver is not None:
                driver.close()

    def close(self):
        self.http.close()


# Leading navigation-bar buttons that are not Back: Clock's root has "Edit" there, and
# tapping it twelve times left the reset toggling edit mode ("did not reach its root screen").
# The tab a tabbed app's root must show at the start of a task.
RESET_TABS = {"com.apple.mobiletimer": "World Clock"}
# Apps whose root is reached by re-tapping a tab rather than by Back buttons.
RESET_POP_TABS = {"com.apple.DocumentsApp": "Browse"}
NOT_BACK = re.compile(r"^(edit|done|cancel|close|select|add|new|filter|sort|menu|more|history|list|sidebar|"
                      r"show sidebar|hide sidebar|[a-z0-9]+(\.[a-z0-9]+)+)$", re.I)


def navigation_back(snapshot):
    """The navigation bar's leading button, which on iOS is the back button."""
    return next((element for element in snapshot.elements
                 if element.role == "Button" and "XCUIElementTypeNavigationBar" in element.locator
                 and element.rect[0] < .25 and not NOT_BACK.match((element.label or "").strip())), None)


def navigation_title(snapshot):
    """The nav-bar title: an inert label in the top band of the screen.

    Needed because a row label is not a location. Settings' General screen lists
    a row called About, so asserting that the text 'About' appears anywhere on
    screen passes while the agent is still one screen away from it.
    """
    bars = [element for element in snapshot.elements if element.role == "NavigationBar"]
    if bars:
        # WDA names the bar after its title; every WDA element advertises TAP,
        # so the inert-label heuristic below cannot apply to it. Some bars carry a
        # class name instead ("FullDocumentManagerViewControllerNavigationBar" in
        # Files): then the title is the text drawn inside the bar.
        bar = min(bars, key=lambda element: element.rect[1])
        label = bar.label.strip()
        if re.fullmatch(r"[A-Za-z_][\w.]*", label) and len(label) > 20:
            x, y, w, h = bar.rect
            inside = [e for e in snapshot.elements if e.role == "StaticText" and e.label.strip()
                      and y <= e.rect[1] + e.rect[3] / 2 <= y + h and x <= e.rect[0] + e.rect[2] / 2 <= x + w]
            title = max(inside, key=lambda e: e.rect[2]).label.strip() if inside else label
            return re.sub(r",\s*Actions Menu$", "", title)  # iOS 26 menu titles
        return re.sub(r",\s*Actions Menu$", "", label)
    candidates = [element for element in snapshot.elements
                  if not (element.actions or ()) and (element.label or "").strip()
                  and element.rect[1] < .12 and element.rect[3] < .06]
    if not candidates:
        return ""
    return min(candidates, key=lambda element: element.rect[1]).label.strip()


def screen_text(snapshot):
    parts = [snapshot.text or ""]
    for element in snapshot.elements:
        parts.append(f"{element.label or ''} {element.value or ''}")
    return "\n".join(parts)
