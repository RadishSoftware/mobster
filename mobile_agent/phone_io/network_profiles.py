"""Removable Apple network profiles. Generating one never changes a device's routing.

Keys follow apple/device-management's WiFi, VPN.IKEv2 and Relay payload schemas.
Credentials come from files, never command-line arguments or result messages.
"""

import ipaddress
import os
import plistlib
import re
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import PhoneIOError


def host(value):
    value = str(value or "").strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if (len(value) > 253 or not value or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", p)
                                               for p in value.rstrip(".").split("."))):
            raise PhoneIOError("Use a hostname or IP address without a scheme, path or credentials.", "usage") from None
    return value


def url(value, *, https=False):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in (("https",) if https else ("http", "https")) and parsed.hostname and
                 parsed.username is None and parsed.password is None and not parsed.fragment and
                 not any(c.isspace() or ord(c) < 32 for c in value))
        host(parsed.hostname)
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            valid = False
    except (ValueError, TypeError, PhoneIOError):
        valid = False
    if not valid:
        raise PhoneIOError("Use an HTTPS URL without embedded credentials." if https else
                           "Use an HTTP or HTTPS URL without embedded credentials.", "usage")
    return value


def secret(path):
    if not path:
        return None
    try:
        with Path(path).expanduser().open("rb") as stream:
            # Include the optional CRLF plus one extra byte so oversized files cannot be truncated into valid secrets.
            value = stream.read(4099).decode("utf-8").removesuffix("\n").removesuffix("\r")
    except (OSError, UnicodeError):
        raise PhoneIOError("Mobster couldn't read the credential file as UTF-8.", "usage") from None
    if not value or len(value.encode()) > 4096 or any(ord(c) < 32 for c in value):
        raise PhoneIOError("The credential file must contain one nonempty line of at most 4 KB.", "usage")
    return value


def proxy(*, ssid, server=None, port=8080, pac=None, username=None, password=None,
          wifi_password=None, encryption="Any"):
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32 or any(ord(c) < 32 for c in ssid):
        raise PhoneIOError("The Wi-Fi name must contain 1 to 32 UTF-8 bytes.", "usage")
    if bool(server) == bool(pac):
        raise PhoneIOError("Choose a proxy server or a PAC URL.", "usage")
    if encryption not in ("Any", "WPA2", "WPA3", "None") or encryption == "None" and wifi_password:
        raise PhoneIOError("Choose Any, WPA2, WPA3 or None; an open network takes no Wi-Fi password.", "usage")
    payload = {"PayloadType": "com.apple.wifi.managed", "SSID_STR": ssid, "EncryptionType": encryption,
               "AutoJoin": True, "ProxyType": "Auto" if pac else "Manual"}
    if wifi_password:
        payload["Password"] = wifi_password
    if pac:
        if username or password:
            raise PhoneIOError("PAC profiles do not take proxy credentials.", "usage")
        payload.update(ProxyPACURL=url(pac), ProxyPACFallbackAllowed=False)
    else:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise PhoneIOError("The proxy port must be between 1 and 65535.", "usage")
        payload.update(ProxyServer=host(server), ProxyServerPort=port)
        if password and not username:
            raise PhoneIOError("A proxy password needs --username.", "usage")
        if username:
            payload["ProxyUsername"] = username
        if password:
            payload["ProxyPassword"] = password
    return payload


def vpn(*, server, remote_id, local_id, username, password=None):
    if not all(isinstance(v, str) and v.strip() for v in (remote_id, local_id, username)):
        raise PhoneIOError("IKEv2 needs a remote identifier, local identifier and username from your VPN provider.",
                           "usage")
    settings = {"RemoteAddress": host(server), "RemoteIdentifier": remote_id, "LocalIdentifier": local_id,
                "AuthenticationMethod": "None", "ExtendedAuthEnabled": 1, "AuthName": username,
                "IncludeAllNetworks": 1, "OnDemandEnabled": 0}
    if password:
        settings["AuthPassword"] = password
    return {"PayloadType": "com.apple.vpn.managed", "VPNType": "IKEv2", "UserDefinedName": "Mobster VPN",
            "IKEv2": settings}


def relay(*, endpoint, token=None):
    server = {"HTTP2RelayURL": url(endpoint, https=True)}
    if token:
        if any(ord(c) < 32 for c in token):
            raise PhoneIOError("The relay token must be one line.", "usage")
        server["AdditionalHTTPHeaderFields"] = {"Authorization": "Bearer " + token}
    return {"PayloadType": "com.apple.relay.managed", "Relays": [server]}


def write_profile(payload, output, *, name="Mobster network", identifier="app.mobster.network"):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{1,199}", identifier):
        raise PhoneIOError("Use a profile identifier of 2 to 200 letters, numbers, dots or hyphens.", "usage")
    def envelope(kind, identity):
        return {"PayloadType": kind, "PayloadVersion": 1, "PayloadIdentifier": identity,
                "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)), "PayloadDisplayName": name}
    content = {**payload, **envelope(payload["PayloadType"], identifier + ".settings")}
    profile = {**envelope("Configuration", identifier), "PayloadContent": [content], "PayloadRemovalDisallowed": False}
    try:
        data = plistlib.dumps(profile)
    except (TypeError, ValueError):
        raise PhoneIOError("The profile contains text Apple can't encode. Remove control characters.",
                           "usage") from None
    target = Path(output).expanduser().absolute()
    try:
        # Publish only the complete private plist; link keeps a concurrent writer's output intact.
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".mobster-profile-") as stream:
            stream.write(data)
            stream.flush()
            os.link(stream.name, target)
    except FileExistsError:
        raise PhoneIOError("The output file already exists. Choose another --output path.", "usage") from None
    except OSError:
        raise PhoneIOError("Mobster couldn't write the profile. Check the output folder.") from None
    return {"ok": True, "path": str(target), "identifier": identifier, "installed": False,
            "next": "Transfer this profile to your iPhone, then review and install it in Settings > General > "
                    "VPN & Device Management. Remove that profile there to remove this configuration."}
