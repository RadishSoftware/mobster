"""`mobster doctor`: check everything a live task needs, and say how to fix what is missing.

The checks change nothing on the phone: they never build, install, start or tap anything. They ask the tools, the
phone's lockdown service and WebDriverAgent (GET requests only) what state they are in. Keys are reported as set or not
set, never shown. A problem is a row of ``states.ROWS``, so it says what the Mac app and ``mobster mcp`` say, in the
same words; ``doctor_fix`` (``mobster doctor --fix``) acts on the row's fix.
"""

import os
import sys
from dataclasses import asdict, dataclass

from . import __version__, states

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
GLYPHS = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "–"}
from .links import TROUBLESHOOTING as DOCS  # noqa: E402


@dataclass
class Check:
    key: str
    label: str
    state: str
    detail: str
    fix: str = ""
    # The row of states.ROWS this finding is, when it is one; its fix is what `mobster doctor --fix` runs.
    cause: str = ""

    def public(self):
        value = asdict(self)
        if self.cause:
            value["action"] = states.fix_public(states.row(self.cause).fix)
        else:
            del value["cause"]
        return value


def finding(key, label, level, cause, *, phone=None, extra=""):
    """A problem as the table says it: the row's message (then ``extra``, what only this check knows) and its next
    step."""
    found = states.row(cause)
    message, step, _need = states.words(found.state, cause, phone)
    # A fix Mobster can run, or a command to type, gets a second line saying which; the person's own taps need none.
    named = isinstance(found.fix, states.Run) and found.fix.command in step
    route = "" if named or isinstance(found.fix, (states.Yours, states.Wait)) \
        else "\n" + states.how(found.fix).replace("`", "")
    return Check(key, label, level, f"{message} {extra}".strip(), step + route, cause)


def check_python():
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 12):
        return Check("python", "Python", FAIL, version, "Mobster needs Python 3.12 or later: brew install python@3.12")
    return Check("python", "Python", OK, version)


def check_install():
    from .paths import source_checkout
    kind = ("frozen app build" if getattr(sys, "frozen", False) else "source checkout" if source_checkout()
            else "installed")
    # One line: where Mobster keeps its data is in `mobster help environment`.
    return Check("install", "Mobster", OK, f"{__version__}, {kind}")


def check_key(env_file=None):
    """The key Mobster's agent runs on."""
    from .engines import model_provider, smart_key, smart_model
    from .login import mask, source_words
    key = smart_key()
    from .ai_plans import providers
    routed = providers.get(model_provider(smart_model()))
    if routed is not None:
        # A plan or provider from Settings › Models or `mobster login <id>` (its key never shown, even masked).
        if key:
            return Check("key", "Model key", OK, f"{routed.name}: {providers.chosen_model(routed)}")
        return finding("key", "Model key", FAIL, "no_key", extra=f"You chose {routed.name}, and its key or sign-in is "
                                                                  "missing.")
    if key:
        claude = model_provider(smart_model()) == "anthropic"
        name, variable = ("Claude", "ANTHROPIC_API_KEY") if claude else ("OpenAI", "OPENAI_API_KEY")
        return Check("key", "Model key", OK, f"{name} ({mask(key)}), {source_words(variable)}")
    model = smart_model()
    if model_provider(model) == "anthropic" and os.environ.get("MOBSTER_SMART_MODEL"):
        return finding("key", "Model key", FAIL, "no_key",
                       extra=f"MOBSTER_SMART_MODEL is {model}, and no Claude key is saved.")
    if env_file and not os.path.exists(os.path.expanduser(str(env_file))):
        return finding("key", "Model key", FAIL, "no_key", extra=f"The env file {_home(env_file)} doesn't exist.")
    return finding("key", "Model key", FAIL, "no_key")


def check_xcode():
    from .mac_tools import xcode_status
    try:
        status = xcode_status()
    except Exception as error:
        return Check("xcode", "Xcode", WARN, f"could not check ({type(error).__name__})")
    if status["state"] == "ok":
        return Check("xcode", "Xcode", OK, status.get("version") or "ready")
    detail = {"missing": "not installed (the Command Line Tools alone cannot build WebDriverAgent)",
              "license": "license not accepted",
              "first_launch": "first-launch components missing", "no_ios": "no iOS platform installed",
              "broken": "xcodebuild fails"}.get(status["state"], status["state"])
    fix = " && ".join(status.get("fixes") or ()) or "Install Xcode from the App Store and open it once"
    return Check("xcode", "Xcode", WARN, f"{detail}; needed only to build WebDriverAgent for a USB iPhone", fix)


def check_tools():
    from .mac_tools import tool
    names = ("idevice_id", "ideviceinfo", "iproxy", "ideviceinstaller")
    missing = [name for name in names if not tool(name)]
    if not missing:
        return Check("tools", "iPhone tools", OK, "installed")
    return Check("tools", "iPhone tools", WARN, "missing " + ", ".join(missing) + " (for an iPhone on a cable)",
                 "brew install libimobiledevice ideviceinstaller")


def check_usb(manager):
    from .mac_tools import tool
    if not tool("idevice_id"):
        return Check("usb", "iPhone", SKIP, "not checked: the iPhone tools are missing")
    try:
        devices = manager.devices()
    except Exception as error:
        return Check("usb", "iPhone", WARN, f"couldn't list devices ({type(error).__name__})")
    if not devices:
        saved = _saved(manager)
        return finding("usb", "iPhone", WARN, "unplugged" if saved.get("udid") else "none_chosen",
                       phone=_name(saved.get("device_name")))
    chosen = manager.device(devices)
    device = chosen or devices[0]
    name = device.get("modelName") or device.get("model") or "iPhone"
    ios = f", iOS {device['ios']}" if device.get("ios") else ""
    more = f" (+{len(devices) - 1} more)" if len(devices) > 1 else ""
    if not device.get("trusted"):
        return finding("usb", "iPhone", WARN, "computer_untrusted", phone=_name(device.get("name")),
                       extra=f"It is plugged in{more}." if more else "")
    return Check("usb", "iPhone", OK, f"{name}{ios}, trusts this Mac{more}")


def check_runner(manager):
    import time

    from .signing import renew_window
    try:
        settings = manager.settings()
        expires = manager.signature_expiry()
    except Exception:
        return Check("runner", "Mobster's helper", SKIP, "not checked")
    if not settings.get("built_for") and not settings.get("udid"):
        return Check("runner", "Mobster's helper", SKIP, "not installed yet",
                     "mobster setup installs it")
    phone = _name(settings.get("device_name"))
    if expires is not None and expires <= time.time():
        return finding("runner", "Mobster's helper", FAIL, "signature_expired", phone=phone,
                       extra="Its 7-day signature ran out.")
    if manager.built():
        days = f", refreshes in {int((expires - time.time()) // 86400)} days" if expires else ""
        if expires is not None and expires - time.time() <= renew_window():
            return Check("runner", "Mobster's helper", WARN, f"installed{days}",
                         "Mobster for Mac refreshes it while your iPhone is plugged in and unlocked, or run: "
                         "mobster setup")
        return Check("runner", "Mobster's helper", OK, f"installed{days}")
    return finding("runner", "Mobster's helper", WARN, "not_built", phone=phone)


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
        return Check("wda", "Phone connection", WARN, ", ".join(parts) + ", not ready yet",
                     "Wait a few seconds; if it stays that way, run: mobster setup")
    return Check("wda", "Phone connection", OK, ", ".join(parts) + ", ready")


def start_wda_hint(one_line=False):
    """How to get the phone to answer, one route per line (``one_line``: joined, for an error message)."""
    routes = ("Set up your iPhone: mobster setup, or in Mobster for Mac",
              "For a simulator: mobster sim prepare, then --device NAME")
    return "; ".join(routes) if one_line else "\n".join(routes)


VERIFY_HINT = ("Building an iOS app? `mobster test`, `mobster verify` and `mobster mcp` run on a simulator Mobster "
               "manages and need none of the iPhone checks above: run `mobster sim doctor`.")


def check_locked(wda_url, phone=None):
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
        return finding("locked", "Screen", FAIL, "locked", phone=phone, extra="Mobster never enters a passcode.")
    if locked is not False:
        return Check("locked", "Screen", SKIP, "could not ask whether the phone is locked")
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


# The helper's log names a cause for one start; older than this, it is a different day's start.
LOG_FRESH_S = 600


# The checks that say a phone's own problem: when one has, the connection that doesn't answer is its consequence.
PHONE_KEYS = frozenset({"usb", "runner", "wifi", "devmode"})


def refine_wda(wda_url, manager, now=None):
    """The phone connection that doesn't answer, said as the row it is: the cause the helper's last start logged
    (Developer Mode, trust, UI Automation) while that log is fresh, else a helper that isn't running. Called only when
    the checks before it found no cause: a phone that isn't plugged in is already said, and says it once."""
    import time

    from .engine_health import PHONE_CAUSES
    cause = "stopped"
    try:
        age = (time.time() if now is None else now) - (manager.logs / "wda-runner.log").stat().st_mtime
        if age < LOG_FRESH_S:
            manager.runner_hint()
            if manager.runner_cause in PHONE_CAUSES:
                cause = manager.runner_cause
    except OSError:
        pass
    saved = _saved(manager)
    return finding("wda", "Phone connection", FAIL, cause, phone=_name(saved.get("device_name")),
                   extra=f"Nothing answers at {wda_url.replace('http://', '')}.")


def check_port(manager, netstat=None, listening=None):
    """A program on the port Mobster's helper needs, while no helper answers there. Named, never stopped: stopping it is
    ``doctor_fix``'s ``free_port``, which asks. ``netstat`` replaces the socket list and ``listening`` the connect, in
    tests."""
    from .doctor_fix import holders, taken
    ports = (manager.wda_port, manager.video_port)
    held = taken(ports, listening)
    if not held:
        return Check("port", "Ports", OK, f"{manager.wda_port} and {manager.video_port} are free")
    found = holders(held, netstat)
    if not found:
        return finding("port", "Ports", WARN, "port_taken",
                       extra=f"Something holds {', '.join(map(str, held))}, and Mobster couldn't read which program.")
    who = ", ".join(f"{os.path.basename(holder.command.split()[0])} (pid {holder.pid})" for holder in found)
    return finding("port", "Ports", WARN, "port_taken", extra=f"{who} holds {', '.join(str(h.port) for h in found)}.")


def check_network(interface=None):
    """Whether this Mac has a network: a default route. Reads `route`, sends nothing to anyone."""
    from . import native_helpers
    from .wireless import network
    if interface is None:
        if not native_helpers.devices_allowed():
            return Check("network", "Network", SKIP, "not checked")
        interface = network.primary_interface()
    if not interface:
        return finding("network", "Network", WARN, "network_gone")
    return Check("network", "Network", OK, f"online ({interface})")


def check_service(read=None, urls=None):
    """Mobster for Mac's service, when one was started on this Mac and left its address behind: frozen (it accepted the
    connection and never answered) or down (the address is stale: it crashed). Nothing started, nothing to say. A
    warning, not a failure: `mobster run` and the terminal UI don't use it."""
    from .engine_health import read_from_app
    from .threads.client import service_urls
    urls = service_urls() if urls is None else urls
    if not urls:
        return Check("service", "Mobster for Mac", SKIP, "not running")
    engine = (read or read_from_app)(urls[0])
    if engine and engine.get("cause") in ("no_answer", "not_running"):
        return finding("service", "Mobster for Mac", WARN, engine["cause"])
    return Check("service", "Mobster for Mac", OK, "answering")


def _saved(manager):
    try:
        return manager.settings()
    except Exception:
        return {}


def _name(value):
    return value if isinstance(value, str) and value.strip() else None


def check_wifi(udid, data_dir, name=None, *, peers=None, nwi=None, gateway=None, local_network=None,
               network_result=None):
    """The Wi-Fi lines, for a phone with Wi-Fi turned on (`mobster wifi on`)."""
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
        checks.append(finding("wifi", "Wi-Fi link", WARN, "wifi_lost", phone=name))
    elif peer.tunnel == "up":
        checks.append(Check("wifi", "Wi-Fi link", OK, f"{who} is reachable over the encrypted Wi-Fi link"))
    else:
        checks.append(Check("wifi", "Wi-Fi link", WARN, f"{who} is on the network, but the encrypted link isn't up "
                            "yet", "Unlock it; Mobster brings "
                            "the link up when it needs it"))
    mode = peer.developer_mode if peer is not None else None
    if mode == "enabled":
        checks.append(Check("devmode", "Developer Mode", OK, "on"))
    elif mode == "disabled":
        checks.append(finding("devmode", "Developer Mode", FAIL, "developer_mode_off", phone=name))
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
    """Every check in order. The ones that read only this Mac run beside the ones that ask the phone, so the report
    waits for the slowest, not for their sum (the 10 s bar, S4)."""
    from concurrent.futures import ThreadPoolExecutor

    from .cli_constants import DEFAULT_WDA_URL
    from .device_manager import DeviceManager
    from .devices import manager_for
    from .paths import user_data_dir
    wda_url = (wda_url or os.environ.get("MOBSTER_WDA_URL") or DEFAULT_WDA_URL).rstrip("/")
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="mobster-doctor") as pool:
        service, network = pool.submit(check_service), pool.submit(check_network)
        xcode = pool.submit(check_xcode) if device else None
        checks = [check_python(), check_install(), check_key(env_file), network.result()]
        manager = name = None
        if device:
            manager = (manager_for(udid, data_dir) if udid else None) or DeviceManager(data_dir or user_data_dir(),
                                                                                       wda_url)
            checks += [xcode.result(), check_tools(), check_usb(manager), check_runner(manager)]
            saved = _saved(manager)
            phone, name = udid or saved.get("udid"), _name(saved.get("device_name"))
            checks += check_wifi(phone, data_dir or user_data_dir(), name)
        wda = check_wda(wda_url)
        phone_causes = any(c.key in PHONE_KEYS and c.cause for c in checks)
        if wda.state == FAIL and manager is not None and not phone_causes:
            wda = refine_wda(wda_url, manager)
        checks.append(wda)
        service = service.result()
        if wda.state != FAIL:
            checks.append(check_locked(wda_url, name))
        elif manager is not None and not phone_causes and service.state != OK:
            # Plugged in, built, and nothing answers: a program on the helper's port is the other thing that stops it.
            # (A running Mobster service holds the port for its own helper.)
            checks.append(check_port(manager))
        checks.append(check_lease(wda_url))
        checks.append(service)
    return checks


def problems(checks):
    return [check for check in checks if check.state == FAIL]


def fix_report(steps, paint, width=100):
    """What `--fix` did and could not do, one line each. The person's own taps, a command Mobster doesn't run and a
    state that clears by itself are not here: the check that found them says them under its own line."""
    from .style import wrap
    glyphs = {"fixed": ("✓", "green"), "failed": ("✗", "coral"), "refused": ("–", "amber"), "declined": ("–", "amber"),
              "needs_terminal": ("–", "amber")}
    lines = [paint("What --fix did", "bold"), ""]
    shown = [step for step in steps if step.outcome in glyphs]
    for step in shown:
        glyph, role = glyphs[step.outcome]
        lines.append(f"  {paint(glyph, role, 'bold')} " + wrap(step.detail, width, indent=4, first=0))
    if not shown:
        lines.append("  Nothing Mobster can do for you.")
    return lines


def plain_report(checks, color=False, verify_hint=None, width=100, steps=None):
    """The report for a terminal (or a file): one line per check, the fix under each problem, wrapped at ``width``
    with a hanging indent under the value column. After `--fix`, ``steps`` (doctor_fix.Step) adds what it did."""
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
    if steps is not None:
        lines += ["", *fix_report(steps, paint, width)]
    if verify_hint:
        lines += ["", wrap(verify_hint, width)]
    return "\n".join(lines)


def _home(path):
    home = os.path.expanduser("~")
    path = str(path)
    return "~" + path[len(home):] if path.startswith(home) else path
