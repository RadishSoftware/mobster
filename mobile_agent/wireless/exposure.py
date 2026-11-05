
import socket

from . import network

PORTS = (8100, 9100)
# What WebDriverAgent (and Mobster's patches to it) answer with; a router's own page says none of these.
SIGNS = (b"WDA MJPEG Server", b"sessionId", b"Local clients only", b"WebDriverAgent")


def speaks_wda(host, port, timeout=1.5, connect=None):
    try:
        with (connect or socket.create_connection)((host, port), timeout) as sock:
            sock.settimeout(timeout)
            # the local Host header: Mobster's patched helper refuses any other, and this only asks if it is there
            sock.sendall(f"GET /status HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            data = sock.recv(1024)
    except OSError:
        return False
    return any(sign in data for sign in SIGNS)


def exposed(hostnames, *, resolve=None, gateway=None, accepts=None, speaks=None):
    resolve, gateway = resolve or network.resolve, gateway or network.default_gateway
    accepts, speaks = accepts or network.port_open, speaks or speaks_wda
    found = resolve(list(hostnames)) if hostnames else {"ipv4": [], "ipv6": []}
    addresses = found["ipv4"] + found["ipv6"]
    hits = [f"{address}:{port}" for address in addresses for port in PORTS if accepts(address, port)]
    # a phone sharing its hotspot is the router and has no name: there only an answer from WebDriverAgent counts
    router = gateway()
    if router and router not in addresses:
        hits += [f"{router}:{port}" for port in PORTS if speaks(router, port)]
    return addresses, hits
