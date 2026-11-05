"""A bounded, locally enrolled app-state oracle for simulator checks.

The check names an adapter and fixture; only a project-owned manifest can define
their fields and seed values. This is persisted app state, not a backend oracle.
"""

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from ..state import validate_bundle_id
from .assertions import CheckError

CONTRACT = "mobster.business/1"
FILENAME = "mobster-business.json"
MANIFEST = ".mobster/fixtures.json"
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_FIELD = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z")
_MAX_BYTES = 64_000


@dataclass(frozen=True)
class BusinessExpectation:
    field: str
    equals: object

    def to_dict(self):
        return {"field": self.field, "equals": self.equals}


@dataclass(frozen=True)
class BusinessSpec:
    adapter: str
    fixture: str
    expect: tuple[BusinessExpectation, ...]

    def to_dict(self):
        return {"adapter": self.adapter, "fixture": self.fixture,
                "expect": [item.to_dict() for item in self.expect]}


@dataclass(frozen=True)
class BusinessBinding:
    spec: BusinessSpec
    bundle: str
    fields: tuple[str, ...]
    seed: tuple[tuple[str, object], ...]
    digest: str

    def seed_state(self):
        return dict(self.seed)

    @property
    def action_digest(self):
        """Replay starting state; expected assertions do not change the action path."""
        encoded = json.dumps({"contract": CONTRACT, "adapter": self.spec.adapter, "fixture": self.spec.fixture,
                              "bundle": self.bundle, "fields": self.fields, "seed": self.seed_state()},
                             sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _id(value, where, pattern=_ID):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CheckError(f"{where} must be a lowercase symbolic ID")
    return value


def _scalar(value, where):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str) and len(value) <= 256 and all(
            ord(c) >= 32 and not 127 <= ord(c) <= 159 and not 0xD800 <= ord(c) <= 0xDFFF
            and not 0xFDD0 <= ord(c) <= 0xFDEF and ord(c) & 0xFFFF not in (0xFFFE, 0xFFFF)
            for c in value):
        return value
    if isinstance(value, int) and abs(value) <= 1_000_000_000:
        return value
    if isinstance(value, float) and math.isfinite(value) and abs(value) <= 1_000_000_000:
        return value
    raise CheckError(f"{where} must be a bounded JSON scalar")


def valid_scalar(value):
    try:
        _scalar(value, "app-state value")
    except CheckError:
        return False
    return True


def parse_business(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"adapter", "fixture", "expect"}:
        raise CheckError("business needs adapter, fixture and expect; no other keys")
    adapter = _id(value["adapter"], "business.adapter")
    fixture = _id(value["fixture"], "business.fixture")
    items = value["expect"]
    if not isinstance(items, list) or len(items) > 20:
        raise CheckError("business.expect must be a list of at most 20 field comparisons")
    seen = set()
    parsed = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"field", "equals"}:
            raise CheckError(f"business.expect[{index}] needs field and equals")
        field = _id(item["field"], f"business.expect[{index}].field", _FIELD)
        if field in seen:
            raise CheckError(f"business.expect repeats field {field!r}")
        seen.add(field)
        parsed.append(BusinessExpectation(field, _scalar(item["equals"], f"business.expect[{index}].equals")))
    return BusinessSpec(adapter, fixture, tuple(parsed))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CheckError(f"duplicate registration key {key!r}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise CheckError(f"non-finite JSON value {value!r}")


def strict_json(raw):
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)


def _manifest_bytes(project_root):
    root = os.open(Path(project_root).resolve(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        folder = os.open(".mobster", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            descriptor = os.open("fixtures.json", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=folder)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
                    raise CheckError(f"{MANIFEST} must be a regular file of at most {_MAX_BYTES:,} bytes")
                raw = source.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise CheckError(f"{MANIFEST} is larger than {_MAX_BYTES:,} bytes")
            return raw
        finally:
            os.close(folder)
    finally:
        os.close(root)


def load_business_binding(check, project_root):
    """Resolve a check against the project-owned, versioned local registration."""
    spec = check.business
    if spec is None:
        raise CheckError("the check has no business binding")
    try:
        data = strict_json(_manifest_bytes(project_root))
    except CheckError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise CheckError(f"{MANIFEST} is missing or invalid ({type(error).__name__})") from None
    if not isinstance(data, dict) or set(data) != {"version", "adapters"} or type(data["version"]) is not int \
            or data["version"] != 1 or not isinstance(data["adapters"], dict) \
            or len(data["adapters"]) > 16:
        raise CheckError(f"{MANIFEST} needs version 1 and at most 16 adapters")
    for adapter_id in data["adapters"]:
        _id(adapter_id, "adapter registration")
    registration = data["adapters"].get(spec.adapter)
    if not isinstance(registration, dict) or set(registration) != {"kind", "bundle", "fields", "fixtures"} \
            or registration["kind"] != "app_container_json":
        raise CheckError(f"business adapter {spec.adapter!r} is not locally registered for app_container_json")
    bundle = registration["bundle"]
    try:
        bundle = validate_bundle_id(bundle)
    except (TypeError, ValueError):
        raise CheckError("the registered app-state bundle is not a bundle ID") from None
    if check.bundle_id and bundle != check.bundle_id:
        raise CheckError("the registered app-state bundle does not match the check")
    fields = registration["fields"]
    if not isinstance(fields, list) or not 1 <= len(fields) <= 20:
        raise CheckError("business adapter fields must be 1–20 distinct keys")
    for field in fields:
        _id(field, "business adapter field", _FIELD)
    if len(set(fields)) != len(fields):
        raise CheckError("business adapter fields must be 1–20 distinct keys")
    fixtures = registration["fixtures"]
    if not isinstance(fixtures, dict) or not 1 <= len(fixtures) <= 20:
        raise CheckError("business adapter needs 1–20 named fixtures")
    for fixture_id in fixtures:
        _id(fixture_id, "business fixture")
    seed = fixtures.get(spec.fixture)
    if not isinstance(seed, dict) or set(seed) != set(fields):
        raise CheckError(f"business fixture {spec.fixture!r} is missing or has unregistered fields")
    seed = tuple((field, _scalar(seed[field], f"business fixture {spec.fixture}.{field}")) for field in fields)
    if any(item.field not in fields for item in spec.expect):
        raise CheckError("business.expect names a field this adapter did not register")
    canonical = json.dumps({"contract": CONTRACT, "adapter": spec.adapter, "fixture": spec.fixture,
                            "registration": registration, "expect": [item.to_dict() for item in spec.expect]},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return BusinessBinding(spec, bundle, tuple(fields), seed, hashlib.sha256(canonical).hexdigest())


def evaluate_business(binding, state):
    """All comparisons use one app-produced snapshot; JSON types must match exactly."""
    def equal(actual, expected):
        if type(actual) is bool or type(expected) is bool:
            return type(actual) is type(expected) and actual == expected
        if type(actual) in (int, float) and type(expected) in (int, float):
            return actual == expected
        return type(actual) is type(expected) and actual == expected

    return [{"field": item.field, "ok": item.field in state and equal(state[item.field], item.equals),
             "observed": (state[item.field] if type(state[item.field]) is bool or state[item.field] is None
                          else f"[{type(state[item.field]).__name__} redacted]") if item.field in state else None,
             "present": item.field in state}
            for item in binding.spec.expect]
