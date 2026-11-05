"""Phone I/O outside tasks: the clipboard (clipboard.py), installing a build (install.py), files to and from an app's
Documents folder (files.py), Photos and files both ways by every route (transfer.py with photos.py, tunnel.py,
icloud.py and handoff.py), and the phone's buttons and lock (controls.py). `mobster phone` (cli.py), the MCP tools
set_clipboard, install_app, send_media, pull_media and press_button, and the files track (attachments/: PUT_FILE,
the app's "Put on iPhone…", MCP's put_file, get_file and list_files) use them.

``wda(record)`` holds the device's lease (one Mobster process at a time, as a task or an MCP session does) and
yields a request function in WebDriverAgent's session. These modules run outside a task, so none of them is
journaled; the person's own command, or the tool's own approval, is the go-ahead. None of them reads the screen to
decide what to do or taps an app. What acts on the phone or the Mac, and nothing more:
- network_capture.py (only `mobster phone network capture`) runs rvictl and tcpdump on this Mac to save a USB packet
  trace; no task and no MCP tool reaches it. network_profiles.py only writes files.
- controls.py presses Home and the volume buttons, and locks the screen (it never unlocks).
- transfer._accept_airdrop reads an alert's text (as a match condition only) and taps Accept on an alert that says
  AirDrop, only while Mobster's own AirDrop send runs.
- transfer.open_on_mirroring and handoff.mirroring_drop act through the iPhone Mirroring window: open an app with
  Spotlight, drop files on the window.
"""

import contextlib
import signal
import threading
import urllib.error
import urllib.request


class PhoneIOError(Exception):
    def __init__(self, message, code="failed", fix=""):
        super().__init__(message)
        self.code, self.fix = code, fix

    def public(self):
        return {"code": self.code, "message": str(self), "fix": self.fix or None}


# `mobster phone` exit codes per error code.
EXIT_CODES = {"usage": 2, "no_device": 3, "tools": 3, "needs_cable": 3}


def exit_code(error):
    return EXIT_CODES.get(getattr(error, "code", ""), 1)


@contextlib.contextmanager
def capture_cancellation(*, defer=False):
    """Route termination through capture cleanup; defer a nested spawn until its handle is owned."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    pending = None

    def cancel(number, frame):
        nonlocal pending
        if defer:
            pending = number
            return
        # A repeated termination request must not interrupt the bounded stop already in progress.
        for number in previous:
            signal.signal(number, signal.SIG_IGN)
        raise KeyboardInterrupt

    try:
        for number in previous:
            signal.signal(number, cancel)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if pending is not None:
            signal.raise_signal(pending)


def choose_device(name=None, records=None):
    """The device ``name`` names (an id, UDID or name), else the primary iPhone, else the only device there is."""
    from .. import devices
    if records is None:
        records = devices.discover(probe=False)
    if name:
        try:
            return devices.resolve(records, name)
        except LookupError as error:
            raise PhoneIOError(str(error), "no_device") from None
    for pick in ([r for r in records if r.get("primary")], [r for r in records if r.get("kind") == "usb"
                                                              and r.get("state") == "ready"], records):
        if len(pick) == 1:
            return pick[0]
    if not records:
        raise PhoneIOError("Mobster sees no device.", "no_device",
                           "Plug in an iPhone set up in the Mobster app, or prepare a simulator.")
    raise PhoneIOError("Several devices are here: " + ", ".join(f"{r['name']} ({r['id']})" for r in records[:6])
                       + ".", "usage", "Name one with --device.")


class Calls:
    def __init__(self, request):
        self.request = request

    def call(self, method, path, body=None, timeout=15):
        return self.request(method, path, body, timeout)


def checked_request(send):
    """``request`` over ``send``: the answer's value, else PhoneIOError (``unknown_route`` on a 404).
    A value with ``error`` among other keys is the runner's own answer, not a refusal."""
    def request(method, path, body=None, timeout=15):
        try:
            answer = send(method, path, body, timeout)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise PhoneIOError("WebDriverAgent doesn't know that command.", "unknown_route") from None
            raise PhoneIOError(f"WebDriverAgent didn't answer (HTTP {error.code}).", "failed",
                               "Keep the iPhone unlocked and connected, then try again.") from None
        except Exception as error:
            raise PhoneIOError(f"WebDriverAgent didn't answer ({type(error).__name__}).", "failed",
                               "Keep the iPhone unlocked and connected, then try again.") from None
        value = answer.get("value") if isinstance(answer, dict) else None
        envelope = (isinstance(value, dict) and bool(value.get("error"))
                    and set(value) <= {"error", "message", "traceback"})
        if envelope or (isinstance(answer, dict) and answer.get("status", 0) not in (0, None)):
            raise PhoneIOError("WebDriverAgent refused the request.", "failed",
                               "Keep the iPhone unlocked, then try again.")
        return value
    return request


@contextlib.contextmanager
def wda(record, *, opener=None, lease=None):
    """Yield ``request(method, path, body=None, timeout=15)`` in the device's WDA session, holding its lease."""
    from ..device_targets import WdaDevice
    if not record.get("wdaUrl"):
        raise PhoneIOError(f"“{record.get('name') or record.get('id')}” isn't set up yet.", "no_device",
                           record.get("reason") or "Set it up in the Mobster app.")
    device = WdaDevice(record, lease=lease, opener=opener or urllib.request.urlopen)
    try:
        held = device.acquire()
    except Exception as error:
        from ..verify.actions import ActionNotDispatched
        if type(error) is ActionNotDispatched and error.kind in ("busy", "environment"):
            raise PhoneIOError(str(error), "device_busy", getattr(error, "fix", "")) from None
        raise PhoneIOError(str(error), "no_device", getattr(error, "fix", "")) from None
    try:
        request = checked_request(device._in_session)
        yield request
    finally:
        held.release()
