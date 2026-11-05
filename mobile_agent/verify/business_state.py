"""Run-owned app-state fixture in one enrolled simulator container.

Only the fixed Documents/mobster-business.json file is touched. Descriptor-
relative, no-follow access closes the gap between container validation and I/O.
"""

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from ..sim.models import SimError
from .assertions import CheckError
from .business import CONTRACT, FILENAME, strict_json, valid_scalar

MAX_SNAPSHOT = 16_384


class FixtureError(RuntimeError):
    pass


@contextmanager
def _documents(container):
    path = Path(container)
    if not path.is_absolute():
        raise FixtureError("The simulator app container was not absolute")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*path.parts[1:], "Documents"):
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _read(fd):
    source = os.open(FILENAME, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
    with os.fdopen(source, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SNAPSHOT:
            raise FixtureError("The app-state snapshot is not a bounded regular file")
        raw = handle.read(MAX_SNAPSHOT + 1)
    if len(raw) > MAX_SNAPSHOT:
        raise FixtureError("The app-state snapshot is too large")
    return raw, (info.st_dev, info.st_ino)


def _decode(raw, token, binding, *, produced):
    try:
        data = strict_json(raw)
    except (CheckError, ValueError, UnicodeError, RecursionError):
        raise FixtureError("The app-state snapshot is not valid JSON") from None
    if (not isinstance(data, dict) or set(data) != {"schema", "runToken", "generation", "state"}
            or data["schema"] != CONTRACT or data["runToken"] != token
            or type(data["generation"]) is not int or data["generation"] < (1 if produced else 0)
            or not isinstance(data["state"], dict) or set(data["state"]) != set(binding.fields)
            or any(not valid_scalar(value) for value in data["state"].values())):
        raise FixtureError("The app-state snapshot is stale or has the wrong schema")
    return data


class AppContainerFixture:
    def __init__(self, binding, manager, target, actions, run_id, token):
        self.binding, self.manager, self.target, self.actions = binding, manager, target, actions
        self.run_id, self.token = run_id, token
        self.container = None
        self.documents_identity = None
        self.seeded = False
        self.cleanup = "not_needed"

    def _container(self):
        if getattr(self.manager, "real_device", False):
            raise FixtureError("App-state fixtures require a managed simulator")
        try:
            result = self.manager.simctl("get_app_container", self.target.udid, self.binding.bundle, "data")
            if not result.ok:
                raise FixtureError("The simulator app data container is unavailable")
            from ..sim.apps import guard_container
            return guard_container(result.out.strip(), self.target.udid)
        except SimError:
            raise FixtureError("The simulator app data container is not safely accessible") from None

    def quiesce(self):
        """A failed termination is safe only when simctl explicitly says the app is not running."""
        result = self.manager.simctl("terminate", self.target.udid, self.binding.bundle, timeout=30)
        if result.ok:
            return
        detail = " ".join((result.err or "", result.out or "")).lower()
        if result.code is not None and "not running" in detail:
            return
        raise FixtureError("The simulator could not confirm the app was stopped")

    @contextmanager
    def _open_checked(self):
        container = self._container()
        if self.container is not None and container != self.container:
            raise FixtureError("The simulator app container changed during the run")
        with _documents(container) as fd:
            identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            if self.documents_identity is not None and identity != self.documents_identity:
                raise FixtureError("The app Documents folder changed during the run")
            yield fd, container, identity

    def setup(self):
        with self._open_checked() as (fd, container, identity):
            try:
                _read(fd)
            except FileNotFoundError:
                pass
            else:
                raise FixtureError("The fixture file already exists; inspect and reconcile it before a new run")
            payload = json.dumps({"schema": CONTRACT, "runToken": self.token, "generation": 0,
                                  "state": self.binding.seed_state()}, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8")
            temporary = f".{FILENAME}.{self.token}.tmp"
            with self.actions.action("BUSINESS_SETUP", operation_id=f"{self.run_id}:business:setup"):
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=fd)
                try:
                    with os.fdopen(descriptor, "wb") as destination:
                        destination.write(payload)
                        destination.flush()
                        os.fsync(destination.fileno())
                    os.link(temporary, FILENAME, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                    self.container = container
                    self.documents_identity = identity
                    self.seeded = True
                    self.cleanup = "pending"
                    os.fsync(fd)
                finally:
                    os.unlink(temporary, dir_fd=fd)

    def observe(self):
        if not self.seeded:
            raise FixtureError("The fixture was not seeded")
        with self._open_checked() as (fd, _, _):
            raw, _ = _read(fd)
        data = _decode(raw, self.token, self.binding, produced=True)
        return data["state"], data["generation"], hashlib.sha256(raw).hexdigest()

    def remove(self):
        if not self.seeded:
            return self.cleanup
        if self.actions.blocked:
            self.cleanup = "needs_reconciliation"
            return self.cleanup
        try:
            with self.actions.action("BUSINESS_QUIESCE", operation_id=f"{self.run_id}:business:quiesce"):
                self.quiesce()
            with self._open_checked() as (fd, _, _):
                raw, identity = _read(fd)
                _decode(raw, self.token, self.binding, produced=False)
                current = os.stat(FILENAME, dir_fd=fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != identity:
                    raise FixtureError("The app-state file changed before cleanup")
                with self.actions.action("BUSINESS_CLEANUP", operation_id=f"{self.run_id}:business:cleanup"):
                    os.unlink(FILENAME, dir_fd=fd)
                    os.fsync(fd)
            self.cleanup = "removed"
        except BaseException:
            self.cleanup = "needs_reconciliation"
            raise
        return self.cleanup
