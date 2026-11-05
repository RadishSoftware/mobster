"""Close an app on the device without WebDriverAgent, for an app WDA itself can't close.

An app whose accessibility server stopped answering snapshots also blocks WDA's own ``/wda/apps/terminate``: in
the 11 Oct iOSWorld batch (ab1, scorezone-003) the stuck-app relaunch fired and WDA's terminate timed out, and the
run ended in an error. ``xcrun simctl terminate`` closed the same kind of stuck app in about 2 s. ``WDA.killer``
(drivers.py) is one of these, set where the device is known; ``WDA.relaunch_app`` uses it only when WDA's terminate
fails.

- ``simulator(udid)``: ``xcrun simctl terminate``.
- ``device(udid)``: CoreDevice (``xcrun devicectl``). It finds the app's process by the app's install path, then
  terminates that one process with SIGKILL. A process of any other app is never touched.

Each returns a ``killer(bundle_id, timeout) -> bool``: True when the app was closed.
"""

import subprocess


def simulator(udid, runner=subprocess.run):
    """A killer for the simulator ``udid``."""
    def kill(bundle_id, timeout=15):
        try:
            done = runner(["xcrun", "simctl", "terminate", udid, bundle_id], capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0
    return kill


def _app_url(document, bundle_id):
    for app in ((document or {}).get("result") or {}).get("apps") or ():
        if app.get("bundleIdentifier") == bundle_id and app.get("url"):
            return app["url"].rstrip("/") + "/"
    return None


def _pids(document, app_url):
    """Processes whose executable lies inside the app's install folder."""
    out = []
    for process in ((document or {}).get("result") or {}).get("runningProcesses") or ():
        executable, pid = process.get("executable") or "", process.get("processIdentifier")
        if app_url and executable.startswith(app_url) and isinstance(pid, int) and pid > 0:
            out.append(pid)
    return out


def device(udid, run_json=None):
    """A killer for the USB or network device ``udid``, through CoreDevice."""
    def kill(bundle_id, timeout=15):
        from .harness_api import hooks
        run = run_json or hooks().devicectl_json
        try:
            # --include-all-apps: without it CoreDevice lists only apps installed for development, never an App Store
            # app (TikTok on a phone, 11 Oct: no app found, so a stuck TikTok was never closed).
            apps = run(["device", "info", "apps", "--device", udid, "--bundle-id", bundle_id, "--include-all-apps"],
                       timeout)
            app_url = _app_url(apps, bundle_id)
            if app_url is None:
                return False
            processes = run(["device", "info", "processes", "--device", udid], timeout)
            pids = _pids(processes, app_url)
            if not pids:
                return False
            for pid in pids:
                run(["device", "process", "terminate", "--device", udid, "--pid", str(pid), "--kill"], timeout)
            return True
        except Exception:
            return False
    return kill
