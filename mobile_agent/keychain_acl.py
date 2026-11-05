"""Login Keychain items only Mobster's own programs can read; a write never falls back to `security` or a file."""

import ctypes
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

MARKER = "Mobster: only Mobster can read this (access list v1)"
SECURITY = "/usr/bin/security"
APP = Path("/Applications/Mobster.app")
APP_PROGRAMS = ("mobster-desktop", "mobster-agent")
NOT_FOUND = -25300  # errSecItemNotFound
DUPLICATE = -25299  # errSecDuplicateItem
REFUSED = {-25308, -25293, -128, -25315}  # interaction not allowed, auth failed, user canceled, interaction required
CLI_NOT_FOUND = 44  # security(1)'s exit status for errSecItemNotFound
# What a legacy secret may be to go back through `security -i` as one quoted word (base64, base64url or a mob_dk_ key).
CLI_WORD = re.compile(r"[A-Za-z0-9_.:+/=-]{1,16000}")
SAFE = re.compile(r"[A-Za-z0-9_.:@ -]{1,200}")


class KeychainError(Exception):
    """The Keychain refused, or couldn't be reached. Never carries a secret."""


class BridgeError(Exception):
    """A Security framework call failed with ``status`` (an OSStatus)."""

    def __init__(self, status):
        super().__init__(f"OSStatus {status}")
        self.status = status


class SecurityBridge:
    """The Security framework through ctypes: generic passwords in the default (login) keychain."""

    def __init__(self):
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        cf, sec = self.cf, self.sec
        vp = ctypes.c_void_p
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFDataCreate.restype = vp
        cf.CFDataCreate.argtypes = [vp, ctypes.c_char_p, ctypes.c_long]
        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetLength.argtypes = [vp]
        cf.CFDataGetBytePtr.restype = vp
        cf.CFDataGetBytePtr.argtypes = [vp]
        cf.CFDictionaryCreate.restype = vp
        cf.CFDictionaryCreate.argtypes = [vp, ctypes.POINTER(vp), ctypes.POINTER(vp), ctypes.c_long, vp, vp]
        cf.CFArrayCreate.restype = vp
        cf.CFArrayCreate.argtypes = [vp, ctypes.POINTER(vp), ctypes.c_long, vp]
        cf.CFRelease.argtypes = [vp]
        for name in ("SecItemCopyMatching", "SecItemAdd"):
            getattr(sec, name).restype = ctypes.c_int32
            getattr(sec, name).argtypes = [vp, ctypes.POINTER(vp)]
        sec.SecItemUpdate.restype = ctypes.c_int32
        sec.SecItemUpdate.argtypes = [vp, vp]
        sec.SecItemDelete.restype = ctypes.c_int32
        sec.SecItemDelete.argtypes = [vp]
        sec.SecTrustedApplicationCreateFromPath.restype = ctypes.c_int32
        sec.SecTrustedApplicationCreateFromPath.argtypes = [ctypes.c_char_p, ctypes.POINTER(vp)]
        sec.SecAccessCreate.restype = ctypes.c_int32
        sec.SecAccessCreate.argtypes = [vp, vp, ctypes.POINTER(vp)]
        sec.SecKeychainSetUserInteractionAllowed.restype = ctypes.c_int32
        sec.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_ubyte]
        const = lambda lib, name: vp.in_dll(lib, name).value  # noqa: E731
        self.k = {name: const(sec, name) for name in (
            "kSecClass", "kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount", "kSecAttrComment",
            "kSecAttrLabel", "kSecAttrAccess", "kSecValueData", "kSecReturnData", "kSecMatchLimit",
            "kSecMatchLimitOne")}
        self.true = const(cf, "kCFBooleanTrue")
        self.dict_keys = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        self.dict_values = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
        self.array_callbacks = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeArrayCallBacks"))

    # CoreFoundation values. Every object made here is released by the caller's `owned` list.
    def _str(self, text, owned):
        ref = self.cf.CFStringCreateWithCString(None, text.encode(), 0x08000100)  # kCFStringEncodingUTF8
        if not ref:
            raise BridgeError(-50)
        owned.append(ref)
        return ref

    def _data(self, raw, owned):
        ref = self.cf.CFDataCreate(None, raw, len(raw))
        if not ref:
            raise BridgeError(-50)
        owned.append(ref)
        return ref

    def _dict(self, pairs, owned):
        n = len(pairs)
        keys = (ctypes.c_void_p * n)(*[k for k, _ in pairs])
        values = (ctypes.c_void_p * n)(*[v for _, v in pairs])
        ref = self.cf.CFDictionaryCreate(None, keys, values, n, self.dict_keys, self.dict_values)
        if not ref:
            raise BridgeError(-50)
        owned.append(ref)
        return ref

    def _release(self, owned):
        for ref in reversed(owned):
            self.cf.CFRelease(ref)

    def _query(self, service, account, owned, *, marked):
        k = self.k
        pairs = [(k["kSecClass"], k["kSecClassGenericPassword"]),
                 (k["kSecAttrService"], self._str(service, owned)),
                 (k["kSecAttrAccount"], self._str(account, owned))]
        if marked:
            pairs.append((k["kSecAttrComment"], self._str(MARKER, owned)))
        return pairs

    def find(self, service, account, *, marked):
        """The item's data (bytes) when ``marked``; otherwise only whether any item exists (b"" or None)."""
        owned = []
        try:
            pairs = self._query(service, account, owned, marked=marked)
            pairs.append((self.k["kSecMatchLimit"], self.k["kSecMatchLimitOne"]))
            if marked:
                pairs.append((self.k["kSecReturnData"], self.true))
            result = ctypes.c_void_p()
            status = self.sec.SecItemCopyMatching(self._dict(pairs, owned), ctypes.byref(result))
            if status == NOT_FOUND:
                return None
            if status != 0:
                raise BridgeError(status)
            if not marked:
                if result.value:
                    self.cf.CFRelease(result.value)
                return b""
            owned.append(result.value)
            size = self.cf.CFDataGetLength(result.value)
            return ctypes.string_at(self.cf.CFDataGetBytePtr(result.value), size)
        finally:
            self._release(owned)

    def update(self, service, account, data):
        """Replaces a marked item's data; the item keeps its access list. False when there is no marked item."""
        owned = []
        try:
            query = self._dict(self._query(service, account, owned, marked=True), owned)
            change = self._dict([(self.k["kSecValueData"], self._data(data, owned))], owned)
            status = self.sec.SecItemUpdate(query, change)
            if status == NOT_FOUND:
                return False
            if status != 0:
                raise BridgeError(status)
            return True
        finally:
            self._release(owned)

    def add(self, service, account, data, trusted):
        """A new marked item whose access list trusts this program and the paths in ``trusted``."""
        owned = []
        try:
            apps = []
            for path in [None, *trusted]:
                app = ctypes.c_void_p()
                status = self.sec.SecTrustedApplicationCreateFromPath(
                    None if path is None else os.fsencode(path), ctypes.byref(app))
                if status != 0:
                    if path is None:
                        raise BridgeError(status)  # this program must be on the list
                    continue  # an unsigned or missing app: left off, never "any application"
                owned.append(app.value)
                apps.append(app.value)
            array = self.cf.CFArrayCreate(None, (ctypes.c_void_p * len(apps))(*apps), len(apps),
                                          self.array_callbacks)
            if not array:
                raise BridgeError(-50)
            owned.append(array)
            access = ctypes.c_void_p()
            status = self.sec.SecAccessCreate(self._str(service, owned), array, ctypes.byref(access))
            if status != 0:
                raise BridgeError(status)
            owned.append(access.value)
            pairs = self._query(service, account, owned, marked=True)
            pairs += [(self.k["kSecAttrLabel"], self._str(service, owned)),
                      (self.k["kSecAttrAccess"], access.value),
                      (self.k["kSecValueData"], self._data(data, owned))]
            status = self.sec.SecItemAdd(self._dict(pairs, owned), None)
            if status != 0:
                raise BridgeError(status)
        finally:
            self._release(owned)

    def delete(self, service, account):
        """Deletes the marked item. False when there was none."""
        owned = []
        try:
            status = self.sec.SecItemDelete(self._dict(self._query(service, account, owned, marked=True), owned))
            if status == NOT_FOUND:
                return False
            if status != 0:
                raise BridgeError(status)
            return True
        finally:
            self._release(owned)

    def no_prompts(self):
        self.sec.SecKeychainSetUserInteractionAllowed(0)


_bridge = None


def system_bridge():
    """The process's one SecurityBridge (tests/__init__.py replaces this so no test reaches the Keychain)."""
    global _bridge
    if _bridge is None:
        if sys.platform != "darwin":
            raise KeychainError("The Keychain is only on a Mac")
        try:
            _bridge = SecurityBridge()
        except (OSError, AttributeError, ValueError):
            raise KeychainError("Mobster couldn't load the Security framework") from None
    return _bridge


def no_prompts():
    """Mobster for Mac's agent runs with no window: a Keychain read it isn't trusted for fails instead of asking."""
    try:
        system_bridge().no_prompts()
    except KeychainError:
        pass


def trusted_programs(executable=None, app=APP):
    """Mobster for Mac's programs the access list trusts besides this one."""
    executable = Path(executable or sys.executable).resolve()
    bundle = next((p for p in executable.parents if p.suffix == ".app"), None)
    root = bundle if bundle is not None and (bundle / "Contents" / "MacOS").is_dir() else Path(app)
    out = []
    for name in APP_PROGRAMS:
        path = root / "Contents" / "MacOS" / name
        if path.is_file() and path.resolve() != executable:
            out.append(str(path))
    return out


def _cli(argv, data=None):
    return subprocess.run(argv, input=data, capture_output=True, timeout=10)


class Store:
    """String secrets under one service."""

    def __init__(self, service, *, bridge=None, runner=None, trusted=None):
        if not SAFE.fullmatch(service):
            raise ValueError("A Keychain service name is a plain word")
        from .paths import keychain_service
        self.service = keychain_service(service)
        self._bridge = bridge
        self.run = runner or _cli
        self._trusted = trusted

    @property
    def bridge(self):
        return self._bridge if self._bridge is not None else system_bridge()

    def trusted(self):
        return list(self._trusted) if self._trusted is not None else trusted_programs()

    def _check(self, account):
        if not SAFE.fullmatch(account):
            raise ValueError("A Keychain account name is a plain word")

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BridgeError as error:
            if error.status in REFUSED:
                raise KeychainError("The Keychain refused Mobster (it's locked, or the item isn't Mobster's)") \
                    from None
            raise KeychainError(f"The Keychain didn't answer (OSStatus {error.status})") from None

    def get(self, account):
        """The secret, or None when there is none. Moves an older item to the access list on the way."""
        self._check(account)
        data = self._call(self.bridge.find, self.service, account, marked=True)
        if data is not None:
            return data.decode(errors="replace") or None
        if self._call(self.bridge.find, self.service, account, marked=False) is None:
            return None
        secret = self._legacy_read(account)
        if secret is None:
            return None
        self._migrate(account, secret)
        return secret

    def set(self, account, secret):
        """Saves ``secret``. Raises KeychainError, and stores nothing new, when the Keychain refuses."""
        self._check(account)
        if not isinstance(secret, str) or not secret:
            raise ValueError("A Keychain secret is a non-empty string")
        data = secret.encode()
        if self._call(self.bridge.update, self.service, account, data):
            return
        old = None
        if self._call(self.bridge.find, self.service, account, marked=False) is not None:
            old = self._legacy_read(account)
            self._legacy_delete(account)
        try:
            self._call(self.bridge.add, self.service, account, data, self.trusted())
        except KeychainError:
            if old is not None:
                self._legacy_restore(account, old)
            raise

    def delete(self, account):
        self._check(account)
        self._call(self.bridge.delete, self.service, account)
        if self._call(self.bridge.find, self.service, account, marked=False) is not None:
            self._legacy_delete(account)

    # Items an older Mobster wrote with /usr/bin/security, which they trust: read and removed with it, no prompt.
    def _legacy_read(self, account):
        try:
            done = self.run([SECURITY, "find-generic-password", "-s", self.service, "-a", account, "-w"], None)
        except (OSError, subprocess.SubprocessError):
            raise KeychainError("Mobster couldn't reach the Keychain") from None
        if done.returncode == CLI_NOT_FOUND:
            return None
        if done.returncode != 0:
            raise KeychainError("The Keychain didn't give Mobster its older item")
        out = done.stdout.decode(errors="replace") if isinstance(done.stdout, bytes) else (done.stdout or "")
        return out.strip() or None

    def _legacy_delete(self, account):
        try:
            done = self.run([SECURITY, "delete-generic-password", "-s", self.service, "-a", account], None)
        except (OSError, subprocess.SubprocessError):
            raise KeychainError("Mobster couldn't reach the Keychain") from None
        if done.returncode not in (0, CLI_NOT_FOUND):
            raise KeychainError("The Keychain didn't remove Mobster's older item")

    def _legacy_restore(self, account, secret):
        """Puts an older item back as it was, so a failed move loses nothing. Logs, never raises."""
        if not CLI_WORD.fullmatch(secret):
            log.warning("Mobster couldn't put back a Keychain item of %s it failed to move", self.service)
            return
        command = f'add-generic-password -U -s "{self.service}" -a "{account}" -w "{secret}"\n'.encode()
        try:
            done = self.run([SECURITY, "-i"], command)
            ok = done.returncode == 0 and not (done.stderr or b"").strip()
        except (OSError, subprocess.SubprocessError):
            ok = False
        finally:
            command = secret = None  # noqa: F841 -- the secret's only other copy goes with this frame
        if not ok:
            log.warning("Mobster couldn't put back a Keychain item of %s it failed to move", self.service)

    def _migrate(self, account, secret):
        try:
            self._legacy_delete(account)
        except KeychainError:
            log.warning("Mobster couldn't move a Keychain item of %s to its access list; it tries again next time",
                        self.service)
            return
        try:
            self._call(self.bridge.add, self.service, account, secret.encode(), self.trusted())
        except KeychainError:
            log.warning("Mobster couldn't move a Keychain item of %s to its access list; it tries again next time",
                        self.service)
            self._legacy_restore(account, secret)
            return
        log.info("Mobster moved a Keychain item of %s to its access list", self.service)
