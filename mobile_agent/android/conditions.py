"""A check's conditions on Android: the ones with an Android equivalent are set, the rest make the check couldn't-run
(it never runs in the wrong state)."""

import re

from ..verify.conditions import ConditionError
from .adb import AdbError

# iOS privacy service -> Android runtime permissions.
PERMISSIONS = {
    "calendar": ("android.permission.READ_CALENDAR", "android.permission.WRITE_CALENDAR"),
    "contacts": ("android.permission.READ_CONTACTS", "android.permission.WRITE_CONTACTS"),
    "contacts-limited": ("android.permission.READ_CONTACTS", "android.permission.WRITE_CONTACTS"),
    "location": ("android.permission.ACCESS_FINE_LOCATION", "android.permission.ACCESS_COARSE_LOCATION"),
    "location-always": ("android.permission.ACCESS_FINE_LOCATION", "android.permission.ACCESS_COARSE_LOCATION",
                        "android.permission.ACCESS_BACKGROUND_LOCATION"),
    "microphone": ("android.permission.RECORD_AUDIO",),
    "photos": ("android.permission.READ_MEDIA_IMAGES", "android.permission.READ_MEDIA_VIDEO"),
}
NOT_ON_ANDROID = {
    "push": "conditions.push sends an APNs payload, which Android has no equivalent of here",
    "content_size": "conditions.content_size has no Android mapping yet",
    "increase_contrast": "conditions.increase_contrast has no Android mapping yet",
    "route": "conditions.route has no Android mapping yet",
    "media": "conditions.media has no Android mapping yet",
    "status_bar": "conditions.status_bar has no Android mapping yet",
    "network": "conditions.network shapes traffic through an iOS simulator's proxy, which Android has no equivalent of "
               "here",
}
SUPPORTED = ("appearance", "locale", "location", "permissions")   # what _apply sets; any other key is refused
FIX = "Remove that condition, or run the check on an iOS simulator with --sim."


class AndroidConditions:
    """Same shape as verify.conditions.Applier: before_launch, after_launch, launch_args, launch_env, restore, report,
    and ``net``, which is always None: network conditions and request stubs are an iOS simulator's (netrun.py)."""

    net = None

    def __init__(self, spec, adb, *, bundle, emulator, actions=None):
        self.spec, self.adb, self.bundle, self.emulator = dict(spec), adb, bundle, emulator
        self.actions = actions      # the run's action journal (verify/actions.py): every change is journaled first
        self.applied, self.skipped, self._undo = [], [], []

    def _change(self, operation, call, *args, **kwargs):
        """One device change, journaled before it is sent; an answer that never came stops the run, never retried."""
        if self.actions is None:
            raise ConditionError("Device condition changes need an action journal.", FIX)
        with self.actions.action(operation):
            return call(*args, **kwargs)

    def launch_args(self):
        return ()

    def launch_env(self):
        return {}

    def before_launch(self):
        for key in self.spec:   # a key added for iOS later is refused here too, never skipped unseen
            if key not in SUPPORTED:
                why = NOT_ON_ANDROID.get(key, f"conditions.{key} has no Android mapping yet")
                raise ConditionError(f"{why}, so this check can't run on Android.", FIX)
        try:
            self._apply()
        except AdbError as error:
            raise ConditionError(f"Mobster couldn't set a condition on the Android device: {error}",
                                 "Run `mobster sim doctor`, then try again. Remove the condition to run the check "
                                 "without it.") from None

    def _apply(self):
        spec = self.spec
        if "appearance" in spec:
            text = self.adb.shell("cmd", "uimode", "night", timeout=10, check=False).text
            old = re.search(r"Night mode: (yes|no)", text)
            self._change("CONDITION_SET", self.adb.shell, "cmd", "uimode", "night",
                         "yes" if spec["appearance"] == "dark" else "no", timeout=10)
            if old:
                self._undo.append(("cmd", "uimode", "night", old.group(1)))
            self.applied.append("appearance")
        if "locale" in spec:
            tag = spec["locale"].replace("_", "-")
            def set_locale():
                result = self.adb.shell("cmd", "locale", "set-app-locales", self.bundle, "--locales", tag, timeout=10,
                                        check=False)
                if result.code != 0 or b"xception" in result.out + result.err:
                    raise AdbError("per-app languages need Android 13 or later")
            self._change("CONDITION_SET", set_locale)
            self._undo.append(("cmd", "locale", "set-app-locales", self.bundle, "--locales", ""))
            self.applied.append("locale")
        if "location" in spec:
            if not self.emulator:
                raise ConditionError("conditions.location sets an emulator's GPS fix, and a USB Android phone has no "
                                     "such control, so this check can't run on it.", FIX)
            place = spec["location"]
            self._change("CONDITION_SET", self.adb.adb, "emu", "geo", "fix", f"{place['longitude']:g}",
                         f"{place['latitude']:g}", timeout=10)
            self.applied.append("location")
        if spec.get("permissions") and not self.emulator:
            # As on an iPhone: a phone's permissions are someone's own, so a check never changes them there.
            self.skipped.append({"key": "permissions", "why": "Mobster never changes an app's permissions on a phone."})
            return
        for service, action in (spec.get("permissions") or {}).items():
            names = PERMISSIONS.get(service)
            if names is None:
                raise ConditionError(f"conditions.permissions.{service} has no Android permission.", FIX)
            for name in names:
                def change(name=name, action=action):
                    try:
                        self.adb.permission(self.bundle, name, action == "grant")
                    except AdbError:
                        # A permission the app does not declare, or the OS version lacks, cannot be revoked.
                        if action == "grant":
                            raise
                self._change("CONDITION_SET", change)
            self.applied.append("permissions")

    def after_launch(self):
        """Nothing is sent after launch on Android."""

    def restore(self, actions=None):
        """Undo each change under the device lease, journaled; stop at the first that isn't confirmed, never retried."""
        if actions is not None:
            self.actions = actions
        while self._undo:
            self._change("CONDITION_RESTORE", self.adb.shell, *self._undo.pop(), timeout=10)

    def report(self):
        return {"applied": list(dict.fromkeys(self.applied)), "skipped": list(self.skipped)}
