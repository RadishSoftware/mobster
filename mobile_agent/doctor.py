"""`mobster doctor`: check everything a live task needs, and say how to fix what is missing.

It changes nothing on the phone: it never builds, installs, starts or taps
anything. It asks the tools, the phone's lockdown service and WebDriverAgent
(GET requests only) what state they are in. Keys are reported as set or not
set, never shown.
"""

from dataclasses import asdict, dataclass
import os
import sys

from . import __version__

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
GLYPHS = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "·"}
from .links import TROUBLESHOOTING as DOCS  # noqa: E402


@dataclass
class Check:
    key: str
    label: str
    state: str
    detail: str
    fix: str = ""

    def public(self):
        return asdict(self)


def check_python():
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 12):
        return Check("python", "Python", FAIL, version, "Mobster needs Python 3.12 or later: brew install python@3.12")
    return Check("python", "Python", OK, version)


def check_install():
    from .paths import source_checkout
    kind = "frozen app build" if getattr(sys, "frozen", False) else "source checkout" if source_checkout() else "installed"
    # One line: where Mobster keeps its data is in `mobster help environment`.
    return Check("install", "Mobster", OK, f"{__version__} · {kind}")


def check_key(env_file=None):
    """The key Mobster's agent (Smart) runs on: Claude's or OpenAI's, from the shell, an env file or the settings
    Mobster for Mac and `mobster login` save; Quick mode's (Jev) when that is the only one. Shown masked
    (sk-ant-…a1F2), never whole."""
    from .engines import model_provider, smart_key, smart_model
    from .login import mask, source_words
    jev = os.environ.get("TYPESAFE_API_KEY")
    key = smart_key()
    if key:
        claude = model_provider(smart_model()) == "anthropic"
        name, variable = ("Claude", "ANTHROPIC_API_KEY") if claude else ("OpenAI", "OPENAI_API_KEY")
        detail = f"{name} ({mask(key)}) · {source_words(variable)}"
        if jev:
            detail += " · Quick mode too"
        return Check("key", "Model key", OK, detail)
    model = smart_model()
    if model_provider(model) == "anthropic" and os.environ.get("MOBSTER_SMART_MODEL"):
        return Check("key", "Model key", FAIL, f"MOBSTER_SMART_MODEL is {model}, and no Claude key is saved",
                     "mobster login")
    if jev:
        return Check("key", "Model key", OK, f"Jev ({mask(jev)}) for Quick mode · {source_words('TYPESAFE_API_KEY')}",
                     "")
    if env_file and not os.path.exists(os.path.expanduser(str(env_file))):
        return Check("key", "Model key", FAIL, f"none yet (the env file {_home(env_file)} doesn't exist)",
                     "mobster login")
    return Check("key", "Model key", FAIL, "none yet", "mobster login")


def check_helper():
    """The helper types text and answers for Fast (Jev); Smart needs none."""
    from .gemini import configured
    model = os.environ.get("TEXT_MODEL")
    if configured():
        provider = os.environ.get("TEXT_MODEL_PROVIDER") or "openai-compatible"
        return Check("helper", "Helper model", OK, f"{model} · {provider}")
    if not os.environ.get("TYPESAFE_API_KEY"):
        return Check("helper", "Helper model", SKIP, "not needed: only Quick mode uses one")
    return Check("helper", "Helper model", WARN, "not set: Quick mode stops when a step needs typed text",
                 "Set TEXT_MODEL and its provider in your env file, or use Mobster's agent (Claude or OpenAI)")


def check_xcode():
    from .device_manager import xcode_status
    try:
        status = xcode_status()
    except Exception as error:
        return Check("xcode", "Xcode", WARN, f"could not check ({type(error).__name__})")
    if status["state"] == "ok":
        return Check("xcode", "Xcode", OK, status.get("version") or "ready")
    detail = {"missing": "not installed (the Command Line Tools alone cannot build WebDriverAgent)",
              "not_selected": "installed but not selected", "license": "license not accepted",
              "first_launch": "first-launch components missing", "no_ios": "no iOS platform installed",
              "broken": "xcodebuild fails"}.get(status["state"], status["state"])
    fix = " && ".join(status.get("fixes") or ()) or "Install Xcode from the App Store and open it once"
    return Check("xcode", "Xcode", WARN, f"{detail}; needed only to build WebDriverAgent for a USB iPhone", fix)


def check_tools():
    from .device_manager import tool
    names = ("idevice_id", "ideviceinfo", "iproxy", "ideviceinstaller")
    missing = [name for name in names if not tool(name)]
    if not missing:
        return Check("tools", "iPhone tools", OK, "installed")
    return Check("tools", "iPhone tools", WARN, "missing " + ", ".join(missing) + " (for an iPhone on a cable)",
                 "brew install libimobiledevice ideviceinstaller")


def check_usb(manager):
    from .device_manager import tool
    if not tool("idevice_id"):
        return Check("usb", "iPhone", SKIP, "not checked: the iPhone tools are missing")
    try:
        devices = manager.devices()
    except Exception as error:
        return Check("usb", "iPhone", WARN, f"couldn't list devices ({type(error).__name__})")
    if not devices:
        return Check("usb", "iPhone", WARN, "not plugged in",
                     "Plug it in with a cable and unlock it, then run: mobster setup")
    chosen = manager.device(devices)
    device = chosen or devices[0]
    name = device.get("modelName") or device.get("model") or "iPhone"
    ios = f", iOS {device['ios']}" if device.get("ios") else ""
    more = f" (+{len(devices) - 1} more)" if len(devices) > 1 else ""
    if not device.get("trusted"):
        return Check("usb", "iPhone", WARN, f"{name} is plugged in, but doesn't trust this Mac yet{more}",
                     "Unlock it and tap Trust, then enter your passcode")
    return Check("usb", "iPhone", OK, f"{name}{ios}, trusts this Mac{more}")


def check_runner(manager):
    import time
    from .device_manager import renew_window
    try:
        settings = manager.settings()
        expires = manager.signature_expiry()
    except Exception:
        return Check("runner", "Mobster's helper", SKIP, "not checked")
    if not settings.get("built_for") and not settings.get("udid"):
        return Check("runner", "Mobster's helper", SKIP, "not installed yet",
                     "mobster setup installs it")
    if expires is not None and expires <= time.time():
        return Check("runner", "Mobster's helper", FAIL, "needs a refresh: its 7-day signature ran out",
                     "mobster setup refreshes it")
    if manager.built():
        days = f", refreshes in {int((expires - time.time()) // 86400)} days" if expires else ""
        if expires is not None and expires - time.time() <= renew_window():
            return Check("runner", "Mobster's helper", WARN, f"installed{days}",
                         "Mobster for Mac refreshes it while your iPhone is plugged in and unlocked, or run: "
                         "mobster setup")
        return Check("runner", "Mobster's helper", OK, f"installed{days}")
    return Check("runner", "Mobster's helper", WARN, "not installed on this iPhone yet", "mobster setup installs it")


def check_wda(wda_url):
    from .transport import HTTP
    client = None
    try:
        client = HTTP(wda_url)
        status = client.request("GET", "/status", timeout=2)
    except Exception:
        return Check("wda", "Phone connection", FAIL, f"nothing answers at {wda_url.replace('http://', '')}",
                     start_wda_hint())
    finally:
        if client is not None:
            client.close()
    value = status.get("value") if isinstance(status, dict) else None
    value = value if isinstance(value, dict) else {}
    ios = (value.get("os") or {}).get("version") if isinstance(value.get("os"), dict) else None
    device = value.get("device") if isinstance(value.get("device"), str) else None
    parts = [wda_url.replace("http://", "")]
    if device:
        parts.append(device)
    if ios:
        parts.append(f"iOS {ios}")
    if value.get("ready") is not True:
        return Check("wda", "Phone connection", WARN, " · ".join(parts) + " · not ready yet",
                     "Wait a few seconds; if it stays that way, run: mobster setup")
    return Check("wda", "Phone connection", OK, " · ".join(parts) + " · ready")


def start_wda_hint(one_line=False):
    """How to get the phone to answer, one route per line (``one_line``: joined, for an error message): the guided
    setup in the terminal or in Mobster for Mac, or a simulator."""
    routes = ("Set up your iPhone: mobster setup, or in Mobster for Mac",
              "For a simulator: mobster sim prepare, then --device NAME")
    return "; ".join(routes) if one_line else "\n".join(routes)


VERIFY_HINT = ("Building an iOS app? `mobster test`, `mobster verify` and `mobster mcp` run on a simulator Mobster "
               "manages and need none of the iPhone checks above: run `mobster sim doctor`.")


def check_locked(wda_url):
    from .transport import HTTP
    client = None
    try:
        client = HTTP(wda_url)
        locked = client.request("GET", "/wda/locked", timeout=3).get("value")
    except Exception:
        return Check("locked", "Screen", SKIP, "could not ask whether the phone is locked")
    finally:
        if client is not None:
            client.close()
    if locked is True:
        return Check("locked", "Screen", FAIL, "your iPhone is locked", "Unlock it; Mobster never enters a passcode")
    return Check("locked", "Screen", OK, "unlocked")


def check_lease(wda_url):
    from .journal import JournalError, Lease
    try:
        lease = Lease.device(wda_url.rstrip("/"))
    except JournalError:
        return Check("lease", "In use", WARN, "another Mobster is running a task on this phone",
                     "Finish or stop the task in Mobster for Mac, `mobster serve` or the other terminal")
    lease.close()
    return Check("lease", "In use", OK, "no other Mobster is using it")


def check_wifi(udid, data_dir, name=None, *, peers=None, nwi=None, gateway=None, local_network=None,
               network_result=None):
    """The Wi-Fi lines, for a phone with Wi-Fi turned on (`mobster wifi on`); [] for any other, so the cable-only
    report is unchanged. Reads only: devicectl's device list, `scutil --nwi`, the default route, one knock on the
    gateway for the Local Network permission, and a Bonjour browse of at most 2.5 s that tells whether this network
    lets devices see each other (guest, hotel and campus Wi-Fi often doesn't). ``peers``, ``nwi``, ``gateway``,
    ``local_network`` and ``network_result`` replace those reads in tests."""
    from .wireless import devicectl, network, store, transport_on, words
    try:
        setting = store.get(data_dir, udid) if udid else None
    except Exception:
        setting = None
    if not setting or not setting["enabled"]:
        return []
    who = name or "the iPhone"
    checks = []
    if not transport_on():
        checks.append(Check("wifi_gate", "Wi-Fi for iPhones", SKIP,
                            f"Wi-Fi is on for {who}, but off in this Mobster (MOBSTER_WIFI_TRANSPORT)"))
    peer, error = None, None
    if peers is None:
        try:
            peers = devicectl.list_devices()
        except devicectl.DevicectlError as failure:
            error = failure
        except RuntimeError:
            error = devicectl.DevicectlError("devices are off in this process", "off")
    if peers is not None:
        peer = peers.get(str(udid).upper())
    if error is not None and error.code == "hang":
        checks.append(Check("wifi", "Wi-Fi link", WARN, "Xcode's device service didn't answer in 8 s",
                            "Plug the iPhone in once, or restart this Mac if it keeps happening"))
    elif error is not None:
        checks.append(Check("wifi", "Wi-Fi link", SKIP, f"not checked: {error}"))
    elif peer is None:
        checks.append(Check("wifi", "Wi-Fi link", WARN, f"Xcode's device service doesn't list {who}",
                            "Plug it in once with the cable so this Mac pairs with it again"))
    elif peer.wired:
        checks.append(Check("wifi", "Wi-Fi link", OK, f"{who} is on the cable now; Wi-Fi takes over when it's "
                                                      "unplugged"))
    elif not peer.network:
        checks.append(Check("wifi", "Wi-Fi link", WARN, f"{who} isn't reachable over Wi-Fi",
                            "Keep it unlocked and on the same Wi-Fi as this Mac, or plug it in"))
    elif peer.tunnel == "up":
        checks.append(Check("wifi", "Wi-Fi link", OK, f"{who} is reachable over the encrypted Wi-Fi link"))
    else:
        checks.append(Check("wifi", "Wi-Fi link", WARN, f"{who} is on the network, but the encrypted link isn't up "
                                                        "yet", "Unlock it; Mobster brings the link up when it needs it"))
    mode = peer.developer_mode if peer is not None else None
    if mode == "enabled":
        checks.append(Check("devmode", "Developer Mode", OK, "on"))
    elif mode:
        checks.append(Check("devmode", "Developer Mode", FAIL, f"{mode} on {who}",
                            "On the iPhone: Settings › Privacy & Security › Developer Mode, then restart it"))
    on_cable = peer is not None and peer.wired
    gateway = network.default_gateway() if gateway is None else gateway
    allowed = network.local_network_allowed(gateway) if local_network is None else local_network
    if allowed is False:
        checks.append(Check("localnet", "Local Network", WARN if on_cable else FAIL,
                            "macOS doesn't let this app reach your network",
                            "System Settings › Privacy & Security › Local Network: turn on Mobster (or your "
                            "terminal app)"))
    elif allowed is True:
        checks.append(Check("localnet", "Local Network", OK, "allowed"))
    text = network.nwi() if nwi is None else nwi
    vpn = network.vpn_interface(text)
    if vpn:
        checks.append(Check("vpn", "VPN", WARN, f"a VPN is on ({vpn})",
                            "Turn it off, or plug the iPhone in: a VPN can keep this Mac and the iPhone apart"))
    if network_result is None:
        try:
            network_result = network.peer_check(gateway=gateway, allowed=allowed)
        except Exception:
            network_result = {"state": "unknown"}
    state = (network_result or {}).get("state")
    if state == "blocked":
        problem = words.problem("network_blocked")
        checks.append(Check("peers", "Network", WARN if on_cable else FAIL, problem["message"], problem["fix"]))
    elif state == "ok":
        checks.append(Check("peers", "Network", OK, "devices on it can see each other"))
    elif state == "offline":
        checks.append(Check("peers", "Network", WARN, "this Mac isn't on a network",
                            "Join the same Wi-Fi as the iPhone, or plug it in"))
    elif state == "unknown":
        checks.append(Check("peers", "Network", SKIP, "couldn't tell whether devices on it can see each other"))
    checks.append(Check("awake", "Over Wi-Fi", SKIP, f"keep {who} unlocked and on the charger for workflows"))
    return checks


def run_checks(wda_url=None, data_dir=None, device=True, env_file=None, udid=None):
    """Every check in order. ``device=False`` skips the USB and Xcode checks (a simulator's WDA). ``udid`` names
    a USB phone set up besides the primary one (`--device`): its own runner is checked. A phone with Wi-Fi on also
    gets the Wi-Fi lines (check_wifi)."""
    from .device_manager import DeviceManager
    from .devices import manager_for
    from .paths import user_data_dir
    wda_url = (wda_url or os.environ.get("MOBSTER_WDA_URL") or "http://127.0.0.1:8100").rstrip("/")
    checks = [check_python(), check_install(), check_key(env_file), check_helper()]
    if device:
        manager = (manager_for(udid, data_dir) if udid else None) or DeviceManager(data_dir or user_data_dir(), wda_url)
        checks += [check_xcode(), check_tools(), check_usb(manager), check_runner(manager)]
        try:
            phone = udid or manager.settings().get("udid")
            name = manager.settings().get("device_name")
        except Exception:
            phone, name = None, None
        checks += check_wifi(phone, data_dir or user_data_dir(), name if isinstance(name, str) else None)
    wda = check_wda(wda_url)
    checks.append(wda)
    if wda.state != FAIL:
        checks.append(check_locked(wda_url))
    checks.append(check_lease(wda_url))
    return checks


def problems(checks):
    return [check for check in checks if check.state == FAIL]


def plain_report(checks, color=False, verify_hint=None, width=100):
    """The report for a terminal (or a file): one line per check, the fix under each problem, wrapped at ``width``
    with a hanging indent under the value column."""
    from .style import Palette, wrap
    paint = Palette(color)
    roles = {OK: "green", WARN: "amber", FAIL: "coral", SKIP: "tertiary"}
    column = 4 + 18
    width = max(60, min(width, 110))
    lines = [paint("Mobster doctor", "bold"), ""]
    for check in checks:
        glyph = paint(GLYPHS[check.state], roles[check.state], "bold")
        detail = wrap(check.detail, width, indent=column, first=column)[column:]
        text = f"  {glyph} {check.label:<18}{detail}"
        lines.append(paint(text, "tertiary") if check.state == SKIP else text)
        if check.fix and check.state in {WARN, FAIL}:
            for route in check.fix.split("\n"):
                lines.append(paint(wrap("→ " + route, width, indent=column + 2, first=column), "secondary"))
    failed = problems(checks)
    warned = [c for c in checks if c.state == WARN]
    lines.append("")
    help_link = "Help: " + paint(DOCS, "accent")

    def ending(head, rest=""):
        # The help link joins the line when it fits, else gets its own: no line is wider than the terminal.
        if len(head) + len(rest) + 2 + len("Help: " + DOCS) <= width:
            return [paint(head, "bold") + rest + "  " + help_link]
        return [paint(head, "bold") + rest, help_link]
    if failed:
        count = len(failed)
        lines += ending(f"{count} thing{'s' if count != 1 else ''} to fix before your first task.")
    elif warned:
        lines += ending("Ready for tasks", f", with {len(warned)} thing{'s' if len(warned) != 1 else ''} to look at.")
    else:
        lines.append(paint("Ready for tasks.", "bold"))
    if verify_hint:
        lines += ["", wrap(verify_hint, width)]
    return "\n".join(lines)


def rich_report(checks):
    from rich.text import Text
    colors = {OK: "#48d597", WARN: "#f6bd4f", FAIL: "#ff806b", SKIP: "#8F8C96"}
    text = Text()
    for check in checks:
        text.append(f"{GLYPHS[check.state]} ", style=colors[check.state])
        text.append(f"{check.label:<18}", style="#F4F2EE")
        text.append(f"{check.detail}\n", style="#B4B1BA")
        if check.fix and check.state in {WARN, FAIL}:
            for route in check.fix.split("\n"):
                text.append(f"  {'':<18}→ {route}\n", style="#8F8C96")
    failed = problems(checks)
    text.append("\n")
    text.append("Ready for tasks." if not failed else
                f"{len(failed)} thing{'s' if len(failed) != 1 else ''} to fix before your first task.",
                style="#48d597" if not failed else "#ff806b")
    return text


def _home(path):
    home = os.path.expanduser("~")
    path = str(path)
    return "~" + path[len(home):] if path.startswith(home) else path
