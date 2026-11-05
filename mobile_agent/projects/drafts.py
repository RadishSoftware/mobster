"""Reviewed local check files in one already-saved project. Saving never executes a check."""

import hashlib
import os
import stat
from pathlib import Path

import yaml

from ..api_errors import APIError
from ..testkit.record import RUN_ID, RecordError, check_data
from ..verify.checks import CheckError, check_from_dict

MAX_DRAFT_BYTES = 60_000


class _StrictLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        out = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in out:
                raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
            out[key] = self.construct_object(value_node, deep=deep)
        return out


def _child(parent, name):
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _root_fd(root):
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(root).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _save(root, name, raw):
    root_fd = _root_fd(root)
    try:
        mobster_fd = _child(root_fd, ".mobster")
        try:
            checks_fd = _child(mobster_fd, "checks")
            try:
                try:
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                                 dir_fd=checks_fd)
                except FileExistsError:
                    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=checks_fd)
                    try:
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != len(raw) or \
                                os.read(fd, len(raw) + 1) != raw:
                            raise APIError("That reviewed check has changed. Review and save it again.", 409,
                                           "check_changed")
                    finally:
                        os.close(fd)
                    return True
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.fsync(checks_fd)
                except BaseException:
                    os.unlink(name, dir_fd=checks_fd)
                    raise
                return False
            finally:
                os.close(checks_fd)
        finally:
            os.close(mobster_fd)
    finally:
        os.close(root_fd)


def _reviewed(root, check, raw, name):
    try:
        replayed = _save(root, name, raw)
    except OSError:
        raise APIError("Mobster cannot write that project's checks folder.", 403, "unwritable") from None
    return {"check": {"path": f".mobster/checks/{name}", "name": check.name,
                       "bundle": check.bundle_id, "steps": len(check.steps), "expect": len(check.expect)},
            "verified": False, "replayed": replayed}


def author(root, body, bundles):
    if not isinstance(body, dict) or set(body) - {"name", "app", "steps", "expect", "reviewed"} or \
            not {"app", "steps", "expect", "reviewed"} <= set(body) or body["reviewed"] is not True:
        raise ValueError("Review the app, steps and expected final text before saving the check")
    app, steps, expect = body["app"], body["steps"], body["expect"]
    if not isinstance(app, dict) or set(app) != {"bundle"} or \
            not isinstance(steps, list) or not steps or not isinstance(expect, list) or not expect or \
            any(not isinstance(item, dict) or set(item) != {"text"} or
                not isinstance(item["text"], str) or not item["text"].strip() for item in expect):
        raise ValueError("Name one app bundle, plain-English steps and expected final text")
    data = {"version": 1, "app": app, "reset": "none", "steps": steps, "expect": expect}
    if "name" in body:
        data["name"] = body["name"]
    try:
        check = check_from_dict(data, base_dir=root)
    except CheckError as error:
        raise ValueError(f"Review a valid check: {error}") from None
    if not check.bundle_id or check.app_path or not check.steps or not check.expect or check.bundle_id not in bundles:
        raise ValueError("Choose an app bundle this project builds, steps and expected final text")
    try:
        raw = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100).encode()
    except (yaml.YAMLError, UnicodeError):
        raise ValueError("The reviewed check contains invalid text") from None
    if len(raw) > MAX_DRAFT_BYTES:
        raise ValueError("The reviewed check is longer than 60 KB")
    return _reviewed(root, check, raw, f"authored-{hashlib.sha256(raw).hexdigest()}.yaml")


def save_finished(runtime, root, body, bundles):
    if not isinstance(body, dict) or set(body) != {"runId", "yaml", "reviewed"} or body["reviewed"] is not True:
        raise ValueError("Send a finished run, reviewed YAML and reviewed: true")
    run_id, text = body["runId"], body["yaml"]
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("runId must name one finished task")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("yaml must be a reviewed check of at most 60 KB")
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        raise ValueError("The reviewed check contains invalid text") from None
    if len(raw) > MAX_DRAFT_BYTES:
        raise ValueError("yaml must be a reviewed check of at most 60 KB")
    with runtime.lock:
        run = runtime.runs.get(run_id)
        source = {**run.public(), "app": dict(run.app or {})} if run is not None else None
    if source is None:
        raise APIError("That task is not in Mobster's history.", 404, "run_not_found")
    try:
        recorded = check_data(source)
        check = check_from_dict(yaml.load(text, Loader=_StrictLoader), base_dir=root)
    except RecordError as error:
        raise APIError(error.message, error.status, error.code) from None
    except (yaml.YAMLError, CheckError, RecursionError, TypeError) as error:
        raise ValueError(f"Review a valid check: {error}") from None
    if check.bundle_id != recorded["app"]["bundle"] or check.bundle_id not in bundles or check.app_path:
        raise ValueError("The reviewed check must keep this project's finished task app bundle")
    if not check.expect:
        raise ValueError("Add an expectation before saving the reviewed check")
    name = f"recorded-{run_id}-{hashlib.sha256(raw).hexdigest()}.yaml"
    return _reviewed(root, check, raw, name)
