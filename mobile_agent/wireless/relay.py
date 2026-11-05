"""The local relay for a phone on Wi-Fi (SPEC §3.7 item 4): 127.0.0.1-only forwarders to the phone's tunnel."""

import ipaddress
import socket
import threading

from ..devices import LOOPBACK

TUNNEL_NETWORK = ipaddress.IPv6Network("fd00::/8")
NOT_AVAILABLE = "Wi-Fi isn't available for this iPhone yet."
CONNECT_TIMEOUT = 5.0
MAX_CONNECTIONS = 64
BUFFER = 65536


# Only the phone's end of the CoreDevice tunnel: the relay never carries WebDriverAgent over the plain LAN.
def check_target(address):
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
    target = check_target(address)
    if through is None:
        from .network import through_tunnel as through
    if not through(target):
        raise ValueError(NOT_AVAILABLE)
    return target


class Relay:
    """Forwards 127.0.0.1:<local> to [target]:<remote> for each ``(local, remote)`` pair, until ``stop``."""

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
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.lock = threading.Lock()
        self.stopping = threading.Event()

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
            if not self.slots.acquire(blocking=False):
                _close(client)
                continue
            with self.lock:
                if self.stopping.is_set():
                    self.slots.release()
                    _close(client)
                    continue
                # Count and track clients before upstream connects: a missing
                # tunnel must not leave unbounded pending threads or sockets.
                self.connections.add(client)
            threading.Thread(target=self._serve, args=(client, remote), daemon=True,
                             name=f"mobster-{self.name}-conn").start()

    def _serve(self, client, remote):
        upstream = None
        try:
            try:
                upstream = socket.create_connection((self.target, remote), timeout=self.connect_timeout)
            except OSError:
                return
            with self.lock:
                if self.stopping.is_set():
                    return
                upstream.settimeout(None)
                client.settimeout(None)
                self.connections.add(upstream)
            other = threading.Thread(target=self._pump, args=(upstream, client), daemon=True,
                                     name=f"mobster-{self.name}-pump")
            other.start()
            self._pump(client, upstream)
            other.join()
        finally:
            with self.lock:
                self.connections.discard(client)
                self.connections.discard(upstream)
            _close(client)
            if upstream is not None:
                _close(upstream)
            self.slots.release()

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
