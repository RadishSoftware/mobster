import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from mobile_agent import __main__ as main_module
from mobile_agent import harness_api
from mobile_agent.wireless import (
    GATE,
    network,
    relay,
)
from mobile_agent.wireless.onboarding import Facts

TUNNEL = "fd7a:1c2b:3d4e::1"


class Runner:
    running = True

    def stop(self):
        self.running = False


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with (
        mock.patch.object(harness_api.hooks(), "cli", []),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        try:
            code = main_module.main(argv)
        except SystemExit as done:
            code = done.code
    return code, out.getvalue(), err.getvalue()


def device(
    udid,
    *,
    transport="localNetwork",
    tunnel="connected",
    address="fd7a:1c2b:3d4e::1",
    name="Studio iPhone",
    devmode="enabled",
    **extra,
):
    connection = {
        "authenticationType": "manualPairing",
        "isMobileDeviceOnly": False,
        "pairingState": "paired",
        "potentialHostnames": [f"{udid}.coredevice.local", "Sams-iPhone.local"],
        "localHostnames": ["Sams-iPhone.local"],
        "transportType": transport,
        "tunnelState": tunnel,
        "tunnelTransportProtocol": "tcp",
        **extra,
    }
    if address is not None:
        connection["tunnelIPAddress"] = address
    return {
        "capabilities": [],
        "connectionProperties": connection,
        "deviceProperties": {
            "bootState": "booted",
            "developerModeStatus": devmode,
            "name": name,
            "osVersionNumber": "26.0",
        },
        "hardwareProperties": {
            "deviceType": "iPhone",
            "marketingName": "iPhone 15 Pro",
            "platform": "iOS",
            "productType": "iPhone16,1",
            "reality": "physical",
            "udid": udid,
        },
        "identifier": "8E5B6D2A-0000-4000-8000-000000000001",
        "visibilityClass": "default",
    }


FIXTURES = Path(__file__).parent / "fixtures" / "devicectl"


ROUTE_ANSWER = """   route to: {target}
destination: {destination}
       mask: {mask}
  interface: {interface}
      flags: <UP,DONE,PRCLONING>
"""


# `ifconfig -v` of the CoreDevice tunnel as its protocol sets it up: this Mac's end on the phone's /64. Its shape is
# Apple's; the line with this Mac's end isn't captured from a real tunnel yet (no Wi-Fi probe has passed on a phone).
TUNNEL_IFCONFIG = """utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 16000 index 26
\teflags=2080<TXSTART,NOAUTOIPV6LL>
\txflags=4000004<NOAUTONX,INBAND_WAKE_PKT>
\tinet6 fe80::1%utun4 prefixlen 64 scopeid 0x1a
\tinet6 fd7a:1c2b:3d4e::2 prefixlen 64
\tnd6 options=201<PERFORMNUD,DAD>
"""


# A NetworkExtension VPN's utun, in the shape `ifconfig -v` printed for one on a Mac on 3 Nov 2026 (addresses made up).
# It even holds an address on the phone's /64: the VPN marks alone refuse it.
VPN_IFCONFIG = """utun5: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280 index 27
\teflags=4002080<TXSTART,NOAUTOIPV6LL,CHANNEL_DRV>
\txflags=4010004<NOAUTONX,IS_VPN,INBAND_WAKE_PKT>
\tinet6 fe80::5%utun5 prefixlen 64 scopeid 0x1b
\tinet6 fd7a:1c2b:3d4e::9 prefixlen 64
\tagent domain:Skywalk type:NetIf flags:0x8443 desc:"Userspace Networking"
\tagent domain:NetworkExtension type:VPN flags:0xf desc:"VPN: Example"
"""


def tunnel_reader(interface="utun4", ifconfig=TUNNEL_IFCONFIG, destination="fd7a:1c2b:3d4e::",
                  mask="ffff:ffff:ffff:ffff::", seen=None):
    """A ``reader`` for network.tunnel_route: `route -n get -inet6` and `ifconfig -v` as given (None: no answer)."""

    def read(argv):
        if seen is not None:
            seen.append(argv)
        if argv[0] == network.ROUTE:
            return None if interface is None else ROUTE_ANSWER.format(
                target=argv[-1], destination=destination, mask=mask, interface=interface)
        if argv[0] == network.IFCONFIG:
            return ifconfig
        raise AssertionError(f"read {argv}")

    return read


def recorded(name):
    return json.loads((FIXTURES / name).read_text())


ON = {GATE: "1"}


FRESH = Facts(attached=True, trusted=False, built=False, running=False, ios="26.0")


TRUSTED = Facts(attached=True, trusted=True, built=False, running=False, ios="26.0")


OFF_CABLE = Facts(attached=False, trusted=False, built=False, running=False)


HELPER_UP = Facts(attached=True, trusted=True, built=True, running=True, ios="26.0")


PACKAGE = Path(relay.__file__).resolve().parent


RUNNER_APP = "__TESTROOT__/Debug-iphoneos/WebDriverAgentRunner-Runner.app"


PLAN_V1 = {
    "WebDriverAgentRunner": {
        "BlueprintName": "WebDriverAgentRunner",
        "DependentProductPaths": [
            RUNNER_APP,
            RUNNER_APP + "/PlugIns/WebDriverAgentRunner.xctest",
        ],
        "EnvironmentVariables": {
            "USE_IP": "127.0.0.1",
            "USE_PORT": "",
            "MJPEG_SERVER_PORT": "",
        },
        "IsUITestBundle": True,
        "TestBundlePath": "__TESTHOST__/PlugIns/WebDriverAgentRunner.xctest",
        "TestHostPath": RUNNER_APP,
        "TestingEnvironmentVariables": {
            "DYLD_FRAMEWORK_PATH": "__TESTROOT__/Debug-iphoneos",
            "USE_IP": "127.0.0.1",
        },
        "UITargetAppPath": RUNNER_APP,
    },
    "__xctestrun_metadata__": {"FormatVersion": 1},
}


PLAN_V2 = {
    "TestConfigurations": [
        {
            "Name": "Test Scheme Action",
            "TestTargets": [
                {
                    "BlueprintName": "WebDriverAgentRunner",
                    "EnvironmentVariables": {"USE_IP": "127.0.0.1"},
                    "TestHostPath": RUNNER_APP,
                    "DependentProductPaths": [RUNNER_APP],
                }
            ],
        }
    ],
    "TestPlan": {"IsDefault": True, "Name": "WebDriverAgentRunner"},
    "__xctestrun_metadata__": {"FormatVersion": 2},
}
