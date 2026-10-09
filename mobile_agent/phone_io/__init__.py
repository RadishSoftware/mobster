"""Phone I/O outside tasks: the clipboard between the Mac and the phone (clipboard.py), installing a build
(install.py), and files to and from an app's Documents folder (files.py). `mobster phone` (cli.py), the MCP tools
set_clipboard and install_app, and the files track (attachments/: PUT_FILE, the app's "Put on iPhone…", MCP's
put_file, get_file and list_files) use them.

``wda(record)`` holds the device's lease (one Mobster process at a time, as a task or an MCP session does) and
yields a request function in WebDriverAgent's session. Nothing here reads the screen or acts in an app.
"""

import contextlib
import urllib.request


class PhoneIOError(Exception):
    """One plain sentence, a code (usage, no_device, not_signed, developer_mode, locked, tools, failed; for files
    also needs_cable, no_file_sharing, not_found) and what to do (may be "")."""

    def __init__(self, message, code="failed", fix=""):
        super().__init__(message)
        self.code, self.fix = code, fix

    def public(self):
        return {"code": self.code, "message": str(self), "fix": self.fix or None}


# `mobster phone` exit codes per error code.
EXIT_CODES = {"usage": 2, "no_device": 3, "tools": 3, "needs_cable": 3}


def exit_code(error):
    return EXIT_CODES.get(getattr(error, "code", ""), 1)


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


@contextlib.contextmanager
def wda(record, *, opener=None, lease=None):
    """Yield ``request(method, path, body=None, timeout=15)`` in the device's WebDriverAgent session, holding the
    device's lease. The value WDA answers, or PhoneIOError."""
    from ..device_targets import WdaDevice
    if not record.get("wdaUrl"):
        raise PhoneIOError(f"“{record.get('name') or record.get('id')}” isn't set up yet.", "no_device",
                           record.get("reason") or "Set it up in the Mobster app.")
    device = WdaDevice(record, lease=lease, opener=opener or urllib.request.urlopen)
    try:
        held = device.acquire()
    except Exception as error:
        raise PhoneIOError(str(error), "no_device", getattr(error, "fix", "")) from None
    try:
        def request(method, path, body=None, timeout=15):
            try:
                answer = device._in_session(method, path, body, timeout)
            except Exception as error:
                raise PhoneIOError(f"WebDriverAgent didn't answer ({type(error).__name__}).", "failed",
                                   "Keep the iPhone unlocked and connected, then try again.") from None
            value = answer.get("value") if isinstance(answer, dict) else None
            if (isinstance(value, dict) and value.get("error")) or (isinstance(answer, dict)
                                                                    and answer.get("status", 0) not in (0, None)):
                raise PhoneIOError("WebDriverAgent refused the request.", "failed",
                                   "Keep the iPhone unlocked, then try again.")
            return value
        yield request
    finally:
        held.release()
