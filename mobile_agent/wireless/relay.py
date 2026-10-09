"""The local relay for a phone on Wi-Fi (SPEC §3.7 item 4): TCP forwarders bound to 127.0.0.1 only, on the phone's
own slot ports, to WebDriverAgent on the phone's tunnel address.

    127.0.0.1:8100+N  ->  [tunnel]:8100   (WebDriverAgent's API)
    127.0.0.1:9100+N  ->  [tunnel]:9100   (its MJPEG stream)

The tunnel address is the phone's end of Apple's encrypted CoreDevice tunnel: an IPv6 address in fd00::/8, reachable
only from this Mac through that tunnel. Anything else (an IPv4 or IPv6 Wi-Fi address, 0.0.0.0, ::, loopback) is
refused, so the relay can never carry WebDriverAgent over the plain LAN. Every listening socket here binds LOOPBACK;
tests check that no other bind exists in this package.

A home network may use fd00::/8 addresses too, so before WebDriverAgent is told to listen on an address
(xctestrun.write) ``require_tunnel`` also checks that this Mac routes it through a tunnel interface (utun), as it
routes a phone's end of the CoreDevice tunnel, and never through Wi-Fi or Ethernet.
"""

import ipaddress
import socket
import threading

LOOPBACK = "127.0.0.1"
TUNNEL_NETWORK = ipaddress.IPv6Network("fd00::/8")
NOT_AVAILABLE = "Wi-Fi isn't available for this iPhone yet."
CONNECT_TIMEOUT = 5.0
MAX_CONNECTIONS = 64
BUFFER = 65536


def check_target(address):
    """``address`` in its canonical form when it is a tunnel address (IPv6, fd00::/8, no zone); ValueError
    (NOT_AVAILABLE) for anything else."""
    if not isinstance(address, str) or not address.strip() or "%" in address:
        raise ValueError(NOT_AVAILABLE)
    try:
        parsed = ipaddress.ip_address(address.strip().strip("[]"))
    except ValueError:
        raise ValueError(NOT_AVAILABLE) from None
    if not isinstance(parsed, ipaddress.IPv6Address) or parsed.ipv4_mapped is not None or parsed not in TUNNEL_NETWORK:
        raise ValueError(NOT_AVAILABLE)
    return parsed.compressed


def require_tunnel(address, through=None):
    """``check_target(address)``, and this Mac must reach it through a tunnel interface (network.through_tunnel);
    ValueError (NOT_AVAILABLE) otherwise. ``through`` replaces the route lookup in tests."""
    target = check_target(address)
    if through is None:
        from .network import through_tunnel as through
    if not through(target):
        raise ValueError(NOT_AVAILABLE)
    return target


class Relay:
    """Forwards 127.0.0.1:<local> to [target]:<remote> for each ``(local, remote)`` pair, until ``stop``.

    It has the ``running`` and ``stop()`` of device_manager.Supervised, so a DeviceManager keeps it as its relay.
    A local port of 0 takes any free port (``ports`` says which)."""

    def __init__(self, target, pairs, *, connect_timeout=CONNECT_TIMEOUT, name="wifi-relay"):
        self.target = check_target(target)
        self.pairs = []
        for local, remote in pairs:
            if not all(isinstance(port, int) and not isinstance(port, bool) for port in (local, remote)) \
                    or not 0 <= local <= 65535 or not 1 <= remote <= 65535:
                raise ValueError("A relay forwards a port number to a port number")
            self.pairs.append((local, remote))
        if not self.pairs:
            raise ValueError("A relay needs at least one port")
        self.connect_timeout = connect_timeout
        self.name = name
        self.listeners = []
        self.connections = set()
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.failures = 0           # upstream connects that failed (the tunnel or WDA isn't there)

    @property
    def running(self):
        return bool(self.listeners) and not self.stopping.is_set()

    @property
    def ports(self):
        """The local ports, in the order of ``pairs`` (after ``start``)."""
        return [listener.getsockname()[1] for listener, _ in self.listeners]

    def start(self):
        """Listen on every local port, or none: OSError (a port in use) leaves nothing open."""
        if self.running:
            return self
        self.stopping.clear()
        opened = []
        try:
            for local, remote in self.pairs:
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                opened.append((listener, remote))
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((LOOPBACK, local))
                listener.listen(16)
        except OSError:
            for listener, _ in opened:
                listener.close()
            raise
        self.listeners = opened
        for listener, remote in opened:
            threading.Thread(target=self._accept, args=(listener, remote), daemon=True,
                             name=f"mobster-{self.name}-{listener.getsockname()[1]}").start()
        return self

    def stop(self):
        self.stopping.set()
        for listener, _ in self.listeners:
            try:
                listener.close()
            except OSError:
                pass
        with self.lock:
            live = list(self.connections)
            self.connections.clear()
        for sock in live:
            _close(sock)

    # -- forwarding ------------------------------------------------------------------------------------------------

    def _accept(self, listener, remote):
        while not self.stopping.is_set():
            try:
                client, _ = listener.accept()
            except OSError:
                return
            with self.lock:
                full = len(self.connections) >= MAX_CONNECTIONS * 2
            if full or self.stopping.is_set():
                _close(client)
                continue
            threading.Thread(target=self._serve, args=(client, remote), daemon=True,
                             name=f"mobster-{self.name}-conn").start()

    def _serve(self, client, remote):
        try:
            upstream = socket.create_connection((self.target, remote), timeout=self.connect_timeout)
        except OSError:
            self.failures += 1
            _close(client)
            return
        upstream.settimeout(None)
        client.settimeout(None)
        with self.lock:
            if self.stopping.is_set():
                _close(client)
                _close(upstream)
                return
            self.connections.update((client, upstream))
        other = threading.Thread(target=self._pump, args=(upstream, client), daemon=True,
                                 name=f"mobster-{self.name}-pump")
        other.start()
        self._pump(client, upstream)
        other.join()
        with self.lock:
            self.connections.discard(client)
            self.connections.discard(upstream)
        _close(client)
        _close(upstream)

    def _pump(self, source, target):
        """Copy ``source`` to ``target`` until ``source`` ends; then end ``target``'s sending side."""
        try:
            while True:
                data = source.recv(BUFFER)
                if not data:
                    break
                target.sendall(data)
        except OSError:
            _close(source)
            _close(target)
            return
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _close(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
