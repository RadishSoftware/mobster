"""One table of how long each kind of WebDriverAgent request may take, and which requests are actions.

``transport.HTTP.request`` always has an absolute deadline, but its own default is 20 s and each call site
used to pick its own number. This module is the one place that names a bound per class of call, so a read
of ``/wda/locked`` can never wait as long as a screenshot, and so a test can walk the code and fail a
WebDriverAgent request that has no bound (tests/test_wda_timeouts.py).

``is_action`` is the fail-closed side: a POST that is not known to be a read changes the phone, so the
watchdog (wda_watchdog.py) never restarts a runner while one is in flight, and nothing resends one.
"""

# Seconds per class of call. Edit here, not at call sites.
WDA_CALL_TIMEOUTS = {
    "probe": 3.0,      # sessionless GET /status, /wda/locked, /wda/activeAppInfo, /wda/screen: milliseconds when healthy
    "settings": 5.0,   # POST /appium/settings
    "read": 10.0,      # /source, /screenshot, /window/size, element reads
    "element": 10.0,   # POST /elements, element find
    "action": 10.0,    # taps, swipes, keys, button presses, element value and clear, app activate
    "session": 30.0,   # POST /session: a cold runner takes seconds to create one
}
# No WDA call may ask for more than this, whatever the caller computed.
MAX_WDA_TIMEOUT = 60.0

PROBE_PATHS = ("/status", "/wda/locked", "/wda/activeAppInfo", "/wda/screen", "/wda/device/info")
# POSTs that read or configure and never change what is on the phone: a find (at the top or under an element) and
# these prefixes. Every other POST under /element/<id> is an action.
READ_POST_PREFIXES = ("/appium/settings", "/wda/getPasteboard", "/wda/apps/state")
FIND_SUFFIXES = ("/element", "/elements")


def _path(path):
    path = path.split("?", 1)[0]
    if path.startswith("/session/"):
        path = "/" + path.split("/", 3)[3] if path.count("/") >= 3 else "/"
    return path


def classify(method, path):
    """The call class of a WDA request: probe, settings, read, element, action or session."""
    method, path = method.upper(), _path(path)
    if method == "POST" and path == "/session" or path == "/":
        return "session"
    if method == "GET":
        return "probe" if path in PROBE_PATHS else "read"
    if method == "POST" and path.startswith("/appium/settings"):
        return "settings"
    if method == "POST" and path.startswith(("/elements", "/element")) and path.rstrip("/").endswith(("/elements", "/element")):
        return "element"
    if is_action(method, path):
        return "action"
    return "read"


def is_action(method, path):
    """Whether this request can change the phone. Fail closed: any POST or DELETE not known to be a read is one."""
    method, path = method.upper(), _path(path)
    if method == "GET":
        return False
    if method == "POST" and path == "/session":
        return False
    if method == "POST" and (path.rstrip("/").endswith(FIND_SUFFIXES) or path.startswith(READ_POST_PREFIXES)):
        return False
    return True


def timeout_for(method, path):
    return WDA_CALL_TIMEOUTS[classify(method, path)]


def bounded(timeout, method="GET", path="/status"):
    """``timeout`` clamped to MAX_WDA_TIMEOUT, or the table's value for the call when it is None."""
    if timeout is None:
        return timeout_for(method, path)
    return max(0.001, min(float(timeout), MAX_WDA_TIMEOUT))
