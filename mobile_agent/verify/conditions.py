"""A check's device conditions. A real iPhone gets only appearance and locale; each other key is skipped and named."""

import difflib
import json
import math
import re
import subprocess
from pathlib import Path

from .assertions import CheckError

KEYS = ("appearance", "content_size", "increase_contrast", "locale", "location", "route", "permissions", "media",
        "status_bar", "push", "biometric", "network")
ON_A_PHONE = ("appearance", "locale")
MATRIX_KEYS = ("appearance", "content_size", "locale")   # a list here makes the check run once per combination
MATRIX_LIMIT = 24
APPEARANCES = ("light", "dark")
NETWORKS = ("offline", "slow-3g", "lte", "flaky")   # netproxy.PROFILES; a test keeps the two in step
CONTRASTS = ("enabled", "disabled")
BIOMETRICS = ("enrolled", "not_enrolled")
WIFI_MODES = ("searching", "failed", "active")
CELLULAR_MODES = ("notSupported", "searching", "failed", "active")
ROUTE_POINTS = (2, 50)   # simctl needs two waypoints at least
CONTENT_SIZES = ("extra-small", "small", "medium", "large", "extra-large", "extra-extra-large",
                 "extra-extra-extra-large", "accessibility-medium", "accessibility-large",
                 "accessibility-extra-large", "accessibility-extra-extra-large",
                 "accessibility-extra-extra-extra-large")
SERVICES = ("all", "calendar", "contacts-limited", "contacts", "location", "location-always", "photos-add",
            "photos", "media-library", "microphone", "motion", "reminders", "siri")
ACTIONS = ("grant", "revoke", "reset")
MEDIA_SUFFIXES = (".jpg", ".jpeg", ".png", ".heic", ".gif", ".mov", ".mp4", ".m4v", ".vcf")
MEDIA_LIMIT = 20
PAYLOAD_LIMIT = 4096   # APNs' limit
BATTERY_STATES = ("charging", "charged", "discharging")
DATA_NETWORKS = ("hide", "wifi", "3g", "4g", "lte", "lte-a", "lte+", "5g", "5g+", "5g-uwb", "5g-uc")
STATUS_BAR_KEYS = ("time", "battery_level", "battery_state", "wifi_mode", "wifi_bars", "cellular_mode", "cellular_bars",
                   "data_network", "operator_name")
LOCALE = re.compile(r"([a-z]{2,3})(?:[-_]([A-Za-z]{4}))?(?:[-_]([A-Z]{2}|[0-9]{3}))?")
NETWORK_ON_A_PHONE = ("{what} shapes the traffic of a simulator, and Mobster can't do that to a real iPhone. Run the "
                      "check on a simulator.")
WIRE_ON_A_PHONE = ("expect: request, no_request and storage read a simulator's proxy and app container, and Mobster "
                   "doesn't set up the proxy on a real iPhone.")
ON_A_PHONE_ONLY = ("{key} changes the state of a phone, so Mobster leaves it alone on a real iPhone. Run the check "
                   "on a simulator to use it.")
SIMCTL_TIMEOUT = 60


class ConditionError(Exception):
    """A condition that couldn't be set: what happened, then what to do."""

    def __init__(self, message, fix=""):
        super().__init__(message)
        self.message, self.fix = message, fix


def _choice(value, key, options):
    if value not in options:
        close = difflib.get_close_matches(str(value), options, n=1)
        raise CheckError(f"conditions.{key} {value!r} isn't one Mobster knows; "
                         + (f"did you mean {close[0]}? " if close else "") + f"Use one of {', '.join(options)}.")
    return value


def _integer(value, where, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CheckError(f"{where} must be a whole number from {low} to {high}.")
    return value


def _file(value, where, base_dir, suffixes=None):
    if isinstance(value, bool) or not isinstance(value, str) or not value.strip():
        raise CheckError(f"{where} must be a file path. Write it as text, relative to the project.")
    path = Path(value.strip()).expanduser()
    path = (path if path.is_absolute() else Path(base_dir or Path.cwd()) / path).resolve()
    if suffixes and path.suffix.lower() not in suffixes:
        raise CheckError(f"{where} {value!r} isn't a kind of file Mobster adds. Use {', '.join(suffixes)}.")
    if not path.is_file():
        raise CheckError(f"{where} {value!r} doesn't exist. Check the path, which is relative to the project.")
    return str(path)


def _location(value, where="conditions.location"):
    if isinstance(value, str):
        parts = value.split(",")
        try:
            value = {"latitude": float(parts[0]), "longitude": float(parts[1])} if len(parts) == 2 else None
        except ValueError:
            value = None
        if value is None:
            raise CheckError("conditions.location must be {latitude: 52.52, longitude: 13.405} or \"52.52,13.405\".")
    if not isinstance(value, dict) or set(value) != {"latitude", "longitude"}:
        raise CheckError(f"{where} needs a latitude and a longitude, such as {{latitude: 52.52, longitude: 13.405}}.")
    out = {}
    for key, limit in (("latitude", 90), ("longitude", 180)):
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) \
                or abs(number) > limit:
            raise CheckError(f"{where}.{key} must be a number from -{limit} to {limit}.")
        out[key] = float(number)
    return out


def _point(value, where):
    if isinstance(value, str):
        parts = value.split(",")
        try:
            value = {"latitude": float(parts[0]), "longitude": float(parts[1])} if len(parts) == 2 else None
        except ValueError:
            value = None
        if value is None:
            raise CheckError(f"{where} must be {{latitude: 52.52, longitude: 13.405}} or \"52.52,13.405\".")
    return _location(value, where)


def _route(value):
    if not isinstance(value, dict) or set(value) - {"waypoints", "speed"} or "waypoints" not in value:
        raise CheckError("conditions.route needs waypoints and may have a speed, such as "
                         "{waypoints: [\"52.52,13.40\", \"52.51,13.38\"], speed: 20}.")
    points = value["waypoints"]
    low, high = ROUTE_POINTS
    if not isinstance(points, list) or not low <= len(points) <= high:
        raise CheckError(f"conditions.route.waypoints must list {low} to {high} places.")
    speed = value.get("speed", 20)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0 < speed <= 1000:
        raise CheckError("conditions.route.speed must be a number of metres per second, above 0 and up to 1000.")
    return {"waypoints": [_point(point, f"conditions.route.waypoints[{index}]") for index, point in enumerate(points)],
            "speed": float(speed)}


def _locale(value):
    found = LOCALE.fullmatch(value) if isinstance(value, str) else None
    if not found:
        raise CheckError(f"conditions.locale {value!r} isn't a locale. Write a language and region, such as de_DE "
                         "or fr-CA.")
    language, script, region = found.groups()
    return "_".join(filter(None, [language, script, region]))


def _permissions(value):
    if not isinstance(value, dict) or not value:
        raise CheckError("conditions.permissions must list services, such as {photos: grant, location: revoke}.")
    out = {}
    for service, action in value.items():
        if service not in SERVICES:
            close = difflib.get_close_matches(str(service), SERVICES, n=1)
            raise CheckError(f"conditions.permissions.{service} isn't a service simctl knows; "
                             + (f"did you mean {close[0]}? " if close else "") + f"Use one of {', '.join(SERVICES)}.")
        out[service] = _choice(action, f"permissions.{service}", ACTIONS)
    return out


def _status_bar(value):
    if not isinstance(value, dict) or not value:
        raise CheckError("conditions.status_bar must set something, such as {time: \"9:41\", battery_level: 100}.")
    for key in value:
        if key not in STATUS_BAR_KEYS:
            close = difflib.get_close_matches(str(key), STATUS_BAR_KEYS, n=1)
            raise CheckError(f"conditions.status_bar.{key} isn't a setting; "
                             + (f"did you mean {close[0]}? " if close else "") + f"Use {', '.join(STATUS_BAR_KEYS)}.")
    out = {}
    for key, item in value.items():
        where = f"conditions.status_bar.{key}"
        if key == "battery_level":
            out[key] = _integer(item, where, 0, 100)
        elif key == "wifi_bars":
            out[key] = _integer(item, where, 0, 3)
        elif key == "cellular_bars":
            out[key] = _integer(item, where, 0, 4)
        elif key == "battery_state":
            out[key] = _choice(item, "status_bar.battery_state", BATTERY_STATES)
        elif key == "wifi_mode":
            out[key] = _choice(item, "status_bar.wifi_mode", WIFI_MODES)
        elif key == "cellular_mode":
            out[key] = _choice(item, "status_bar.cellular_mode", CELLULAR_MODES)
        elif key == "data_network":
            out[key] = _choice(item, "status_bar.data_network", DATA_NETWORKS)
        else:
            if isinstance(item, bool) or not isinstance(item, str) or not item.strip() or len(item) > 40:
                raise CheckError(f"{where} must be text of up to 40 characters; quote it, as in \"9:41\".")
            out[key] = item.strip()
    return out


def _payload(value, base_dir):
    path = _file(value, "conditions.push", base_dir, (".json", ".apns"))
    if Path(path).stat().st_size > PAYLOAD_LIMIT:
        raise CheckError(f"conditions.push {value!r} is larger than {PAYLOAD_LIMIT:,} bytes, the most a push may "
                         "carry. Trim the payload.")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CheckError(f"conditions.push {value!r} isn't valid JSON. Fix the file.") from None
    if not isinstance(data, dict) or not isinstance(data.get("aps"), dict):
        raise CheckError(f"conditions.push {value!r} has no \"aps\" object. A push payload looks like "
                         "{\"aps\": {\"alert\": \"Your order shipped\"}}.")
    return path


def _matrix(key, items):
    if not items:
        raise CheckError(f"conditions.{key} lists nothing. List at least one, or write one value.")
    values = []
    for item in items:
        one = parse({key: item})[key]
        if one not in values:
            values.append(one)
    return values


def combinations(conditions):
    """The scalar conditions of each run a check's matrix makes (appearance, then text size, then locale): one dict for
    a check with no lists."""
    conditions = conditions or {}
    out = [dict(conditions)]
    for key in MATRIX_KEYS:
        if isinstance(conditions.get(key), list):
            out = [{**one, key: value} for one in out for value in conditions[key]]
    return out


def is_matrix(conditions):
    return any(isinstance(value, list) for key, value in (conditions or {}).items() if key in MATRIX_KEYS)


def parse(value, base_dir=None):
    """The normalized ``conditions`` block of a check file: {} when absent. Raises CheckError."""
    if value is None:
        return {}
    if not isinstance(value, dict) or not value:
        raise CheckError("conditions must set something, such as {appearance: dark, locale: de_DE}.")
    for key in value:
        if key not in KEYS:
            close = difflib.get_close_matches(str(key), KEYS, n=1)
            raise CheckError(f"conditions.{key} isn't a condition; "
                             + (f"did you mean {close[0]}? " if close else "") + f"Use {', '.join(KEYS)}.")
    if "location" in value and "route" in value:
        raise CheckError("conditions.location and conditions.route both set where the device is. Keep one.")
    out = {}
    for key in KEYS:
        if key not in value:
            continue
        item = value[key]
        if key in MATRIX_KEYS and isinstance(item, list):
            out[key] = _matrix(key, item)
            continue
        if key == "appearance":
            out[key] = _choice(item, key, APPEARANCES)
        elif key == "content_size":
            out[key] = _choice(item, key, CONTENT_SIZES)
        elif key == "locale":
            out[key] = _locale(item)
        elif key == "increase_contrast":
            out[key] = _choice(item, key, CONTRASTS)
        elif key == "location":
            out[key] = _location(item)
        elif key == "route":
            out[key] = _route(item)
        elif key == "permissions":
            out[key] = _permissions(item)
        elif key == "status_bar":
            out[key] = _status_bar(item)
        elif key == "push":
            out[key] = _payload(item, base_dir)
        elif key == "biometric":
            out[key] = _choice(item, key, BIOMETRICS)
        elif key == "network":
            out[key] = _choice(item, key, NETWORKS)
        else:
            if not isinstance(item, list) or not item:
                raise CheckError("conditions.media must list files, such as [fixtures/receipt.jpg].")
            if len(item) > MEDIA_LIMIT:
                raise CheckError(f"conditions.media has more than {MEDIA_LIMIT} files. Add fewer.")
            out[key] = [_file(path, f"conditions.media[{index}]", base_dir, MEDIA_SUFFIXES)
                        for index, path in enumerate(item)]
    if len(combinations(out)) > MATRIX_LIMIT:
        raise CheckError(f"conditions lists {len(combinations(out))} combinations; the most is {MATRIX_LIMIT}. "
                         "Split the check.")
    return out


def to_dict(conditions, project_root=None):
    """The file shape of ``conditions``, with paths relative to ``project_root`` where they sit inside it."""
    def relative(path):
        try:
            return str(Path(path).relative_to(Path(project_root).resolve()))
        except (ValueError, TypeError):
            return path
    out = {}
    for key, value in conditions.items():
        if key == "push":
            out[key] = relative(value)
        elif key == "media":
            out[key] = [relative(path) for path in value]
        else:
            out[key] = json.loads(json.dumps(value))
    return out


def simctl(args):
    """(exit code, output) of `xcrun simctl ...`."""
    try:
        done = subprocess.run(["xcrun", "simctl", *map(str, args)], capture_output=True, text=True, errors="replace",
                              timeout=SIMCTL_TIMEOUT, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, "Xcode's command-line tools aren't installed."
    except subprocess.TimeoutExpired:
        return 124, f"simctl didn't answer in {SIMCTL_TIMEOUT} s."
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()


class _Notifier:
    """What sim.biometrics needs of a manager (``simctl``), over this module's ``run(args) -> (code, output)``."""

    def __init__(self, run):
        self._run = run

    def simctl(self, *args, timeout=60):
        from ..sim.simctl import Result
        code, output = self._run(list(args))
        return Result(code, output if code == 0 else "", "" if code == 0 else output)


class Applier:
    """Sets one run's conditions; restoration is a journaled device action, not best-effort cleanup."""

    def __init__(self, spec, *, udid, bundle, device=None, run=simctl, actions=None, stubs=(), net=None, watch=None):
        self.spec, self.udid, self.bundle, self.device, self.run = dict(spec), udid, bundle, device, run
        self.actions, self.stubs, self._net_factory, self.net = actions, tuple(stubs), net, None
        self.watch = watch      # host patterns of wire assertions (wire.py): None when the check has none
        self.real = device is not None
        self.applied, self.skipped, self._undo, self._env = [], [], [], {}
        for key in KEYS:
            if key in self.spec and self.real and key not in ON_A_PHONE and key != "network":
                self.skipped.append({"key": key, "why": ON_A_PHONE_ONLY.format(key=key)})

    def _wanted(self, key):
        return key in self.spec and not any(item["key"] == key for item in self.skipped)

    def launch_args(self):
        """The app's language and region, as the arguments iOS reads at launch."""
        if not self._wanted("locale"):
            return ()
        locale = self.spec["locale"]
        return ("-AppleLanguages", f"({locale.split('_')[0]})", "-AppleLocale", locale)

    def _simctl(self, args, what, operation="CONDITION_SET"):
        if self.actions is None:
            raise ConditionError("Device condition changes need an action journal.")
        with self.actions.action(operation):
            code, output = self.run(args)
            if code != 0:
                last = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "")
                raise ConditionError(f"Mobster couldn't set {what} on the simulator"
                                     + (f": {last[:200]}" if last else "") + ".",
                                     "Run `mobster sim doctor`, then try again. Remove the condition to run "
                                     "the check without it.")
        return output

    def launch_env(self):
        """The environment the app launches with: the shim that points it at the run's proxy, when there is one."""
        return dict(self._env)

    def _start_network(self):
        if not (self.spec.get("network") or self.stubs or self.watch is not None):
            return
        from . import netrun
        if self.actions is None:
            raise ConditionError("Device condition changes need an action journal.")
        try:
            make = self._net_factory or netrun.NetRun
            extra = {} if self.watch is None else {"watch": self.watch}
            net = make(self.spec.get("network"), self.stubs, udid=self.udid, run=self.run, **extra)
            self.net = net
            with self.actions.action("CONDITION_SET"):
                self._env = net.start()
        except netrun.NetError as error:
            raise ConditionError(error.message, error.fix) from None
        self.applied += [key for key, on in (("network", self.spec.get("network")), ("stubs", self.stubs)) if on]

    def before_launch(self):
        if self.real:
            if self.spec.get("network") or self.stubs:
                what = "conditions.network" if self.spec.get("network") else "stubs"
                raise ConditionError(NETWORK_ON_A_PHONE.format(what=what), "Run the check on a simulator.")
            if self.watch is not None:
                raise ConditionError(WIRE_ON_A_PHONE, "Run the check on a simulator, or see Wire assertions on a "
                                                      "real iPhone in docs/testing.mdx.")
            if self._wanted("appearance"):
                self._phone_appearance(self.spec["appearance"])
        else:
            for key in ("appearance", "content_size", "increase_contrast"):
                if key in self.spec:
                    word = "ui"
                    previous = self.run([word, self.udid, key])
                    old = previous[1].strip() if previous[0] == 0 else None
                    self._simctl([word, self.udid, key, self.spec[key]], f"the {key.replace('_', ' ')}")
                    if old and old != self.spec[key] and old.split()[-1] not in ("unsupported", "unknown"):
                        self._undo.append([word, self.udid, key, old])
                    self.applied.append(key)
            if "location" in self.spec:
                place = self.spec["location"]
                self._simctl(["location", self.udid, "set", f"{place['latitude']},{place['longitude']}"],
                             "the location")
                self._undo.append(["location", self.udid, "clear"])
                self.applied.append("location")
            if "route" in self.spec:
                route = self.spec["route"]
                self._simctl(["location", self.udid, "start", f"--speed={route['speed']:g}",
                              *(f"{p['latitude']},{p['longitude']}" for p in route["waypoints"])], "the route")
                self._undo.append(["location", self.udid, "clear"])
                self.applied.append("route")
            if "permissions" in self.spec:
                for service, action in self.spec["permissions"].items():
                    self._simctl(["privacy", self.udid, action, service, self.bundle], f"the {service} permission")
                    if action != "reset":
                        self._undo.append(["privacy", self.udid, "reset", service, self.bundle])
                self.applied.append("permissions")
            if "media" in self.spec:
                self._simctl(["addmedia", self.udid, *self.spec["media"]], "the media")
                self.applied.append("media")
            if "biometric" in self.spec:
                self._biometric(self.spec["biometric"])
            if "status_bar" in self.spec:
                flags = []
                for key, value in self.spec["status_bar"].items():
                    flags += ["--" + re.sub(r"_(.)", lambda m: m.group(1).upper(), key), str(value)]
                self._simctl(["status_bar", self.udid, "override", *flags], "the status bar")
                self._undo.append(["status_bar", self.udid, "clear"])
                self.applied.append("status_bar")
            self._start_network()
        if self._wanted("locale"):
            self.applied.append("locale")

    def _biometric(self, wanted):
        """Enroll or unenroll Face ID before the launch; the run puts the enrollment back."""
        from ..sim import biometrics
        from ..sim.models import SimError
        notifier = _Notifier(self.run)
        if self.actions is None:
            raise ConditionError("Device condition changes need an action journal.")
        try:
            before = biometrics.deliver(notifier, self.udid, "status")["enrolled"]
            if before != (wanted == "enrolled"):
                with self.actions.action("CONDITION_SET"):   # journaled before it is sent, as every condition change
                    biometrics.deliver(notifier, self.udid, "enroll" if wanted == "enrolled" else "unenroll")
                self._undo.append(("biometric", before))
        except SimError as error:
            raise ConditionError(f"Mobster couldn't set Face ID enrollment on the simulator: {error}",
                                 "Run `mobster sim doctor`, then try again. Remove the condition to run the check "
                                 "without it.") from None
        self.applied.append("biometric")

    def after_launch(self):
        if "push" in self.spec and not self.real:
            self._simctl(["push", self.udid, self.bundle, self.spec["push"]], "the push", "PUSH")
            self.applied.append("push")

    def _phone_appearance(self, name):
        if self.actions is None:
            raise ConditionError("Device condition changes need an action journal.")
        try:
            previous = self.device.appearance()
            with self.actions.action("CONDITION_SET"):
                self.device.appearance(name)
        except Exception as error:
            raise ConditionError(f"Mobster couldn't set the appearance on the iPhone ({type(error).__name__}).",
                                 "Keep the iPhone unlocked and connected, or remove the condition.") from None
        if previous and previous != name:
            self._undo.append(previous)
        self.applied.append("appearance")

    def restore(self, actions):
        """Restore under the device lease; stop at the first unconfirmed change and never retry it."""
        if self.net is not None:
            try:
                self.net.stop()
            except Exception:
                pass
        while self._undo:
            with actions.action("CONDITION_RESTORE"):
                step = self._undo.pop()
                if isinstance(step, str):
                    self.device.appearance(step)
                elif step and step[0] == "biometric":
                    from ..sim import biometrics
                    biometrics.deliver(_Notifier(self.run), self.udid, "enroll" if step[1] else "unenroll")
                else:
                    code, _ = self.run(step)
                    if code != 0:
                        raise ConditionError("Mobster couldn't confirm that a simulator condition was restored.")

    def report(self):
        return {"applied": list(dict.fromkeys(self.applied)), "skipped": list(self.skipped),
                **({"network": self.net.summary()} if self.net is not None else {})}
