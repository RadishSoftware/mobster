"""Durable, run-owned intent for physical verification actions.

Only the operation category, sequence ID and outcome enter actions.jsonl. The
caller keeps acquired resources reachable inside the context so cleanup still
works if acknowledging the action fails.
"""

import os
import re
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError

from ..errors import SimError
from ..journal import append_jsonl
from ..transport import TransportError, decode_json

_CATEGORY = re.compile(r"[A-Z][A-Z0-9_]{0,39}\Z")
_OPERATION_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9:_-]{0,119}\Z")


class ActionError(RuntimeError):
    """The run can no longer authorize actions: klass is journal, unknown_outcome or stopped."""

    def __init__(self, klass):
        self.klass = klass
        message = ("The action journal could not save intent; no new action was sent."
                   if klass == "journal" else "The run stopped before action dispatch."
                   if klass == "stopped" else "An action may have reached the device; its outcome is unknown.")
        super().__init__(message)


class ActionNotDispatched(SimError):
    """Trusted device-acquisition preflight rejected before any physical effect."""

    def __init__(self, error):
        if not isinstance(error, SimError):
            raise TypeError("ActionNotDispatched requires a simulator error")
        self.error = error
        super().__init__(error.kind, str(error), error.fix)


class ActionJournal:
    def __init__(self, run_dir):
        self.path = Path(run_dir) / "actions.jsonl"
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._next_id = 0
        self._klass = None
        self._dispatching = 0
        self._stopped = False
        self._recover()

    def _recover(self):
        """A resumed run cannot dispatch past an intent left open by process death."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return
        except OSError:
            self._klass = "journal"
            return
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_size > 16_000_000:
                self._klass = "journal"
                return
            with os.fdopen(fd, "rb", closefd=False) as handle:
                rows = handle.read(16_000_001)
            if len(rows) != info.st_size or (rows and not rows.endswith(b"\n")):
                self._klass = "unknown_outcome"
                return
            pending, seen = {}, set()
            for line in rows.splitlines():
                row = decode_json(line)
                if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
                    raise ValueError("invalid action row")
                self._next_id = max(self._next_id, row["id"])
                if row.get("event") == "action_started":
                    if row["id"] in seen or not isinstance(row.get("operation"), str):
                        raise ValueError("duplicate action")
                    pending[row["id"]] = (row["operation"], row.get("operation_id"))
                    seen.add(row["id"])
                elif row.get("event") == "action_finished":
                    if (row["id"] not in pending
                            or pending[row["id"]] != (row.get("operation"), row.get("operation_id"))
                            or row.get("outcome") not in ("acknowledged", "rejected", "unknown")):
                        raise ValueError("invalid finish")
                    del pending[row["id"]]
                    if row["outcome"] == "unknown":
                        self._klass = "unknown_outcome"
                else:
                    raise ValueError("invalid action event")
            if pending:
                self._klass = "unknown_outcome"
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            self._klass = "unknown_outcome"
        finally:
            os.close(fd)

    @property
    def klass(self):
        with self._state_lock:
            return self._klass

    @property
    def blocked(self):
        with self._state_lock:
            return self._klass is not None or self._stopped

    def request_stop(self):
        """Seal admission during abort; report any body that may have begun without waiting."""
        with self._state_lock:
            self._stopped = True
            return bool(self._dispatching)

    def _latch(self, klass):
        with self._state_lock:
            if self._klass != "unknown_outcome":
                self._klass = klass

    @contextmanager
    def action(self, operation, *, operation_id=None):
        """Save intent before dispatch, then its outcome; serialize concurrent physical effects."""
        with self._action(operation, operation_id=operation_id):
            yield

    @contextmanager
    def _action(self, operation, *, operation_id=None, url_404_is_rejected=False):
        if not isinstance(operation, str) or not _CATEGORY.fullmatch(operation):
            raise ValueError("An action needs a stable uppercase category")
        if operation_id is not None and (not isinstance(operation_id, str)
                                         or not _OPERATION_ID.fullmatch(operation_id)):
            raise ValueError("An action operation ID must be a bounded opaque identifier")
        with self._lock:
            with self._state_lock:
                if self._klass is not None:
                    raise ActionError(self._klass)
                if self._stopped:
                    raise ActionError("stopped")
            dispatching = False
            try:
                self._next_id += 1
                identity = {"id": self._next_id, "operation": operation}
                if operation_id is not None:
                    identity["operation_id"] = operation_id
                try:
                    append_jsonl(self.path, {"event": "action_started", **identity})
                except BaseException:
                    self._latch("journal")
                    raise ActionError("journal") from None
                with self._state_lock:
                    stopped = self._stopped
                    if not stopped:
                        self._dispatching += 1
                        dispatching = True
                if stopped:
                    try:
                        append_jsonl(self.path, {"event": "action_finished", **identity,
                                                 "outcome": "rejected"})
                    except BaseException:
                        self._latch("journal")
                        raise ActionError("journal") from None
                    raise ActionError("stopped")
                try:
                    yield
                except BaseException as error:
                    known_rejection = (url_404_is_rejected and _url_404(error))
                    if (operation == "ACQUIRE" and type(error) is ActionNotDispatched and
                            self._dispatching == 1 and self.klass is None):
                        known_rejection = True
                    if known_rejection:
                        try:
                            append_jsonl(self.path, {"event": "action_finished", **identity,
                                                     "outcome": "rejected"})
                        except BaseException:
                            self._latch("journal")
                            raise ActionError("journal") from None
                    else:
                        self._latch("unknown_outcome")
                        try:
                            append_jsonl(self.path, {"event": "action_finished", **identity,
                                                     "outcome": "unknown"})
                        except BaseException:
                            pass  # The in-memory refusal remains sticky; nothing may retry.
                    raise
                if self.klass is not None:
                    self._latch("unknown_outcome")
                    raise ActionError("unknown_outcome")
                try:
                    append_jsonl(self.path, {"event": "action_finished", **identity,
                                             "outcome": "acknowledged"})
                except BaseException:
                    self._latch("unknown_outcome")
                    raise ActionError("unknown_outcome") from None
            finally:
                with self._state_lock:
                    if dispatching:
                        self._dispatching -= 1

    def wrap_http(self, driver):
        """Journal non-GET/HEAD requests on the transport used by WDA; return the same driver.

        Small fake drivers expose only call(), so wrap that boundary for offline tests.
        """
        client = getattr(driver, "http", None) or driver
        attribute = "request" if client is not driver else "call"
        original = getattr(client, attribute)
        if getattr(client, "_verify_action_journal", None) is self:
            return driver
        if getattr(client, "_verify_action_journal", None) is not None:
            self._latch("unknown_outcome")
            raise ActionError("unknown_outcome")

        def request(method, path, *args, **kwargs):
            verb = method.upper()
            if verb in ("GET", "HEAD"):
                return original(method, path, *args, **kwargs)
            with self._action(f"HTTP_{verb}", url_404_is_rejected=verb == "POST" and path.endswith("/url")):
                return original(method, path, *args, **kwargs)

        setattr(client, attribute, request)
        client._verify_action_original = original
        client._verify_action_journal = self
        return driver

    def guard_driver(self, driver):
        """Cover WDA transport and the recovery callbacks that use separate connections. A driver with no WDA transport
        (Android's adb) names its device-changing methods in ``effects``, and each call of one is journaled; a driver
        with neither can't be guarded, so it is refused before it acts."""
        owner = getattr(driver, "_verify_action_driver_journal", None)
        if owner is self:
            return driver
        if owner is not None:
            self._latch("unknown_outcome")
            raise ActionError("unknown_outcome")
        effects = tuple(getattr(driver, "effects", None) or ())
        if effects:
            if not all(callable(getattr(driver, name, None)) for name in effects):
                raise TypeError("A driver's effects must name its own methods")
        elif not (getattr(driver, "http", None) or callable(getattr(driver, "call", None))):
            raise TypeError("This driver has no journaled transport, so a run can't use it")
        else:
            self.wrap_http(driver)
        originals = {}
        try:
            for attribute, operation in (("new_session", "NEW_SESSION"),
                                         ("restarter", "WDA_RESTART"),
                                         ("runner_restart", "WDA_RESTART"),
                                         *((name, "DRIVER_" + name.upper()) for name in effects)):
                callback = getattr(driver, attribute, None)
                if not callable(callback):
                    continue
                originals[attribute] = callback

                def guarded(*args, _callback=callback, _operation=operation, **kwargs):
                    with self.action(_operation):
                        return _callback(*args, **kwargs)

                setattr(driver, attribute, guarded)
            driver._verify_action_driver_originals = originals
            driver._verify_action_driver_journal = self
        except BaseException:
            for attribute, callback in originals.items():
                setattr(driver, attribute, callback)
            self.unwrap_http(driver)
            raise
        return driver

    def unwrap_http(self, driver):
        """Restore a driver's transport after its run closes (offline fakes may be reused)."""
        if getattr(driver, "_verify_action_driver_journal", None) is self:
            for attribute, callback in driver._verify_action_driver_originals.items():
                setattr(driver, attribute, callback)
            delattr(driver, "_verify_action_driver_originals")
            delattr(driver, "_verify_action_driver_journal")
        client = getattr(driver, "http", None) or driver
        if getattr(client, "_verify_action_journal", None) is not self:
            return
        attribute = "request" if client is not driver else "call"
        setattr(client, attribute, client._verify_action_original)
        delattr(client, "_verify_action_original")
        delattr(client, "_verify_action_journal")


def _url_404(error):
    return ((isinstance(error, TransportError) and str(error) == "HTTP 404; request not retried")
            or (isinstance(error, HTTPError) and error.code == 404))
