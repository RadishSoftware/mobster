"""Synthetic pcapng captures for the network X-ray tests, on documentation and private addresses."""

import random
import struct

from mobile_agent.netxray import packets as wire
from mobile_agent.netxray.pcapng import (
    BYTE_ORDER_MAGIC,
    EPB,
    EPB_E_PIB_INDEX,
    EPB_FLAGS,
    EPB_PIB_INDEX,
    IDB,
    IF_NAME,
    IF_TSRESOL,
    OPT_COMMENT,
    PIB,
    PIB_NAME,
    SHB,
)


def _option(code, value):
    return struct.pack("<HH", code, len(value)) + value + b"\0" * (-len(value) % 4)


def _block(block_type, body):
    body += b"\0" * (-len(body) % 4)
    total = len(body) + 12
    return struct.pack("<II", block_type, total) + body + struct.pack("<I", total)


class Writer:
    """Write a little-endian pcapng section. For synthetic captures only: Mobster never writes a phone's traffic."""

    def __init__(self, stream, application="mobster netxray tests"):
        self.stream = stream
        self.interfaces = 0
        self.processes = {}
        body = struct.pack("<IHHq", BYTE_ORDER_MAGIC, 1, 0, -1) + _option(4, application.encode()) + _option(0, b"")
        stream.write(_block(SHB, body))

    def interface(self, linktype, name=None):
        options = (_option(IF_NAME, name.encode()) if name else b"") + _option(IF_TSRESOL, b"\x06") + _option(0, b"")
        self.stream.write(_block(IDB, struct.pack("<HHI", linktype, 0, 262144) + options))
        self.interfaces += 1
        return self.interfaces - 1

    def process(self, pid, name):
        key = (pid, name)
        if key not in self.processes:
            self.stream.write(_block(PIB, struct.pack("<I", pid) + _option(PIB_NAME, name.encode()) + _option(0, b"")))
            self.processes[key] = len(self.processes)
        return self.processes[key]

    def packet(self, interface, time, data, *, comment=None, pib=None, epib=None, direction=None):
        stamp = int(round(time * 1_000_000))
        options = b""
        if comment is not None:
            options += _option(OPT_COMMENT, comment.encode())
        if pib is not None:
            options += _option(EPB_PIB_INDEX, struct.pack("<I", pib))
        if epib is not None:
            options += _option(EPB_E_PIB_INDEX, struct.pack("<I", epib))
        if direction is not None:
            options += _option(EPB_FLAGS, struct.pack("<I", {"in": 1, "out": 2}[direction]))
        if options:
            options += _option(0, b"")
        body = struct.pack("<IIIII", interface, stamp >> 32, stamp & 0xFFFFFFFF, len(data), len(data))
        body += data + b"\0" * (-len(data) % 4) + options
        self.stream.write(_block(EPB, body))


def process_comment(pid, name, epid=None, ename=None, svc=0):
    epid = pid if epid is None else epid
    ename = name if ename is None else ename
    return f"PID: {pid}, ProcName: {name}, EPID: {epid}, EProcName: {ename}, SVC: {svc}"


# the stand-in Ethernet header mobster-pcap writes before every packet, IPv6 included
FAKE_ETHERNET = b"\xbe\xef" * 6 + b"\x08\x00"


def ethernet(packet, real=True):
    if not real:
        return FAKE_ETHERNET + packet
    ethertype = b"\x86\xdd" if packet[0] >> 4 == 6 else b"\x08\x00"
    return b"\x02\x00\x00\x00\x00\x01" + b"\x02\x00\x00\x00\x00\x02" + ethertype + packet


def ip(src, dst, proto, payload):
    if len(src) == 4:
        header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), 0, 0x4000, 64, proto, 0, src, dst)
        return header + payload
    return struct.pack("!IHBB16s16s", 0x60000000, len(payload), proto, 64, src, dst) + payload


def tcp(sport, dport, flags, payload=b"", seq=1000, ack=0):
    return struct.pack("!HHIIBBHHH", sport, dport, seq, ack, 5 << 4, flags, 65535, 0, 0) + payload


def udp(sport, dport, payload):
    return struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload


def _name(host):
    return b"".join(bytes([len(label)]) + label.encode() for label in host.split(".")) + b"\0"


def dns_query(host, ident=0x1234, kind=1):
    return struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0) + _name(host) + struct.pack("!HH", kind, 1)


def dns_response(host, addresses, ident=0x1234, cname=None):
    """A response for ``host``: an optional CNAME, then A or AAAA records (by address length), compressed names."""
    question = _name(host) + struct.pack("!HH", 1, 1)
    answers = b""
    count = 0
    owner = b"\xc0\x0c"
    if cname:
        target = _name(cname)
        answers += owner + struct.pack("!HHIH", 5, 1, 60, len(target)) + target
        count += 1
        owner = struct.pack("!H", 0xC000 | (12 + len(question) + 12))
    for address in addresses:
        kind = 1 if len(address) == 4 else 28
        answers += owner + struct.pack("!HHIH", kind, 1, 60, len(address)) + address
        count += 1
    return struct.pack("!HHHHHH", ident, 0x8180, 1, count, 0, 0) + question + answers


def client_hello(host, padding=0):
    """A TLS 1.3 ClientHello handshake message naming ``host`` (None: no server name); ``padding`` adds a padding
    extension's bytes (a post-quantum key share makes real ones about 1.8 KB)."""
    extensions = b""
    if host is not None:
        sni = host.encode()
        server_name = struct.pack("!HBH", len(sni) + 3, 0, len(sni)) + sni
        extensions += struct.pack("!HH", 0, len(server_name)) + server_name
    extensions += struct.pack("!HH", 43, 3) + b"\x02\x03\x04"          # supported_versions: TLS 1.3
    if padding:
        extensions += struct.pack("!HH", 21, padding) + b"\0" * padding
    body = b"\x03\x03" + bytes(range(32)) + b"\x20" + bytes(32) + struct.pack("!H", 6) + b"\x13\x01\x13\x02\x13\x03"
    body += b"\x01\x00" + struct.pack("!H", len(extensions)) + extensions
    return b"\x01" + len(body).to_bytes(3, "big") + body


def tls_record(handshake):
    return b"\x16\x03\x01" + struct.pack("!H", len(handshake)) + handshake


def http_request(host, path="/v1/locate?token=SECRET-TOKEN-123"):
    return (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: FlashlightPro/3.1\r\n"
            "Cookie: session=SECRET-COOKIE-456\r\n\r\n").encode()


def _varint(value):
    if value < 64:
        return bytes([value])
    if value < 16384:
        return struct.pack("!H", 0x4000 | value)
    return struct.pack("!I", 0x80000000 | value)


def quic_initial(host, dcid=bytes.fromhex("8394c8f03e515708"), scid=b"\x01\x02\x03\x04", padding=0):
    """A QUIC v1 client Initial carrying a ClientHello for ``host``, encrypted and header-protected as RFC 9001
    says. Without the ``cryptography`` package it's an Initial with a garbled payload (the name then comes from DNS)."""
    hello = client_hello(host, padding)
    frames = b"\x06" + _varint(0) + _varint(len(hello)) + hello
    frames += b"\0" * max(0, 1162 - len(frames))
    number = b"\x00\x00"
    header = (bytes([0xC0 | (len(number) - 1)]) + struct.pack("!I", wire.QUIC_V1) + bytes([len(dcid)]) + dcid
              + bytes([len(scid)]) + scid + _varint(0) + struct.pack("!H", 0x4000 | (len(number) + len(frames) + 16)))
    aes = wire._aes()
    if aes is None:
        return header + number + frames + bytes(16)
    Cipher, algorithms, modes, AESGCM = aes
    key, iv, hp = wire.quic_client_keys(dcid)
    nonce = bytes(a ^ b for a, b in zip(iv, int.from_bytes(number, "big").to_bytes(12, "big")))
    sealed = AESGCM(key).encrypt(nonce, frames, header + number)
    sample = sealed[4 - len(number):4 - len(number) + 16]
    mask = Cipher(algorithms.AES(hp), modes.ECB()).encryptor().update(sample)
    first = bytes([header[0] ^ (mask[0] & 0x0F)])
    protected = bytes(b ^ m for b, m in zip(number, mask[1:1 + len(number)]))
    return first + header[1:] + protected + sealed


PHONE = bytes([192, 168, 1, 23])
PHONE6 = bytes.fromhex("20010db8000000000000000000000017")
RESOLVER = bytes([192, 168, 1, 1])


class Capture:
    """Writes conversations as mobster-pcap does (Ethernet, a process comment on each packet), or as tcpdump
    on rvi0 would (``style="pib"``: raw IP, Apple's process blocks)."""

    def __init__(self, stream, style="comment", linktype=1):
        self.writer = Writer(stream)
        self.style = style
        self.linktype = linktype
        self.interface = self.writer.interface(linktype, "en0" if linktype == 1 else "pdp_ip0")
        self.ports = 49152
        self.packets = 0

    def port(self):
        self.ports += 1
        return self.ports

    def send(self, time, packet, process, pid, eprocess=None, epid=None, direction=None):
        frame = ethernet(packet, real=self.style != "comment") if self.linktype == 1 else packet
        if self.style == "comment":
            self.writer.packet(self.interface, time, frame, comment=process_comment(pid, process, epid, eprocess))
        else:
            pib = self.writer.process(pid, process)
            epib = self.writer.process(epid, eprocess) if eprocess else None
            self.writer.packet(self.interface, time, frame, pib=pib, epib=epib, direction=direction)
        self.packets += 1

    def lookup(self, time, app, pid, host, addresses, phone=PHONE, resolver=RESOLVER):
        """mDNSResponder asks on the app's behalf, as iOS does: the process is mDNSResponder,
        the effective one the app."""
        port = self.port()
        self.send(time, ip(phone, resolver, wire.UDP, udp(port, 53, dns_query(host))), "mDNSResponder", 90, app, pid)
        self.send(time + 0.012, ip(resolver, phone, wire.UDP, udp(53, port, dns_response(host, addresses))),
                  "mDNSResponder", 90, app, pid)

    def tls(self, time, app, pid, host, server, *, down=8000, up=1200, sni=True, split=False, via=None, phone=PHONE,
            port=443, chunks=4):
        sport = self.port()
        process, eprocess, epid = (via, app, pid) if via else (app, None, None)
        ppid = 120 if via else pid

        def out(t, payload=b"", flags=0x18):
            self.send(t, ip(phone, server, wire.TCP, tcp(sport, port, flags, payload)), process, ppid, eprocess, epid)

        def back(t, payload=b"", flags=0x18):
            self.send(t, ip(server, phone, wire.TCP, tcp(port, sport, flags, payload)), process, ppid, eprocess, epid)
        out(time, flags=0x02)
        back(time + 0.03, flags=0x12)
        out(time + 0.031, flags=0x10)
        record = tls_record(client_hello(host if sni else None, padding=1500 if split else 200))
        if split:
            out(time + 0.032, record[:1300])
            out(time + 0.033, record[1300:])
        else:
            out(time + 0.032, record)
        sent = len(record)
        t = time + 0.08
        for index in range(chunks):
            back(t, bytes(min(1400, max(1, down // chunks))))
            if sent < up:
                out(t + 0.002, bytes(min(1400, max(1, (up - sent) // (chunks - index)))))
                sent += min(1400, max(1, (up - sent) // (chunks - index)))
            t += 0.05
        out(t, flags=0x11)
        back(t + 0.02, flags=0x11)
        return t

    def quic(self, time, app, pid, host, server, *, down=20000, phone=PHONE, dcid=None):
        sport = self.port()
        dcid = dcid or bytes(random.Random(host).randrange(256) for _ in range(8))
        self.send(time, ip(phone, server, wire.UDP, udp(sport, 443, quic_initial(host, dcid))), app, pid)
        t = time + 0.03
        for _ in range(max(1, down // 1200)):
            self.send(t, ip(server, phone, wire.UDP, udp(443, sport, b"\x40" + bytes(1199))), app, pid)
            t += 0.004
        self.send(t, ip(phone, server, wire.UDP, udp(sport, 443, b"\x40" + bytes(40))), app, pid)

    def http(self, time, app, pid, host, server, phone=PHONE):
        sport = self.port()
        self.send(time, ip(phone, server, wire.TCP, tcp(sport, 80, 0x02)), app, pid)
        self.send(time + 0.02, ip(server, phone, wire.TCP, tcp(80, sport, 0x12)), app, pid)
        self.send(time + 0.021, ip(phone, server, wire.TCP, tcp(sport, 80, 0x18, http_request(host))), app, pid)
        self.send(time + 0.06,
                  ip(server, phone, wire.TCP, tcp(80, sport, 0x18, b"HTTP/1.1 200 OK\r\n\r\n" + bytes(600))),
                  app, pid)

    def mdns(self, time, app="sharingd", pid=70):
        payload = dns_response("Sams-MacBook.local", [bytes([192, 168, 1, 40])])
        self.send(time, ip(PHONE, bytes([224, 0, 0, 251]), wire.UDP, udp(5353, 5353, payload)), app, pid)


def server(n):
    return bytes([203, 0, 113, n])


APP, PID = "Flashlight Pro", 412

# (host, server address, how it's named, bytes down, bytes up)
FLASHLIGHT = [
    ("api.flashlightpro.example", server(10), "tls", 48000, 6000),
    ("cdn.flashlightpro.example", server(11), "tls", 310000, 2400),
    ("app-measurement.com", server(20), "tls", 4200, 9800),
    ("graph.facebook.com", server(21), "tls", 6100, 7400),
    ("googleads.g.doubleclick.net", server(22), "quic", 96000, 3000),
    ("api2.appsflyer.com", server(23), "tls", 2600, 5200),
    ("api2.amplitude.com", server(24), "tls", 1900, 11200),
    ("ms4.applovin.com", server(25), "tls", 182000, 4100),
    ("o4507.ingest.sentry.io", server(26), "tls", 900, 3900),
    ("sdk.iad-03.braze.com", server(27), "tls", 3100, 2700),
    ("d3k81ch9hvuctc.cloudfront.net", server(28), "tls", 54000, 1500),
    ("fonts.gstatic.com", server(29), "quic", 36000, 1200),
    ("www.googleapis.com", server(30), "dns", 7000, 1800),
    ("geo.locate-api.example", server(31), "http", 700, 500),
]

MOMENTS = (2.0, 9.5, 18.0, 29.0, 41.0)   # seconds into the capture when the made-up app does something new


def flashlight(stream, start=1_792_000_000.0):
    """A 60 s capture of the made-up Flashlight Pro: 14 domains, 6 of them ads, analytics or attribution, plus
    Apple's push service, local network chatter and mDNS."""
    capture = Capture(stream)
    plan = {
        0: ["api.flashlightpro.example", "app-measurement.com", "sdk.iad-03.braze.com", "o4507.ingest.sentry.io",
            "api2.appsflyer.com"],
        1: ["graph.facebook.com", "api2.amplitude.com", "geo.locate-api.example"],
        2: ["cdn.flashlightpro.example", "d3k81ch9hvuctc.cloudfront.net", "fonts.gstatic.com"],
        3: ["googleads.g.doubleclick.net", "ms4.applovin.com", "www.googleapis.com"],
        4: [],
    }
    table = {host: (address, kind, down, up) for host, address, kind, down, up in FLASHLIGHT}
    for minute in (0.4, 30.4):
        capture.mdns(start + minute)
    capture.lookup(start + 0.5, "apsd", 88, "courier.push.apple.com", [bytes([17, 0, 0, 5])])
    capture.tls(start + 0.6, "apsd", 88, "courier.push.apple.com", bytes([17, 0, 0, 5]), down=900, up=700, port=5223)
    for index, offset in enumerate(MOMENTS):
        t = start + offset + 0.4
        for host in plan[index]:
            address, kind, down, up = table[host]
            if kind != "http":
                capture.lookup(t, APP, PID, host, [address])
            t += 0.05
            if kind == "tls":
                capture.tls(t, APP, PID, host, address, down=down, up=up, split=host.startswith("api2.amp"),
                            via="nsurlsessiond" if host.startswith("cdn.") else None, chunks=max(2, down // 1400))
            elif kind == "quic":
                capture.quic(t, APP, PID, host, address, down=down)
            elif kind == "http":
                capture.http(t, APP, PID, host, address)
            else:   # a TLS connection whose hello names nothing: DNS names it
                capture.tls(t, APP, PID, host, address, down=down, up=up, sni=False)
            t += 0.6
    # The analytics SDK reports the paywall closing.
    capture.tls(start + 41.6, APP, PID, "app-measurement.com", server(20), down=800, up=2600)
    capture.tls(start + 59.5, "apsd", 88, "courier.push.apple.com", bytes([17, 0, 0, 5]), down=300, up=200,
                port=5223)


def bulk(stream, packets=200_000, seed=7):
    """About ``packets`` packets of mixed traffic (TLS, QUIC, DNS, HTTP) from 6 processes, for throughput."""
    rng = random.Random(seed)
    capture = Capture(stream)
    apps = [("Flashlight Pro", 412), ("TikTok", 500), ("Weather", 501), ("Mail", 502), ("Maps", 503), ("Safari", 504)]
    hosts = [host for host, *_ in FLASHLIGHT]
    t = 1_792_000_000.0
    while capture.packets < packets:
        app, pid = rng.choice(apps)
        host = rng.choice(hosts)
        address = server(rng.randrange(10, 250))
        capture.lookup(t, app, pid, host, [address])
        if rng.random() < 0.3:
            capture.quic(t + 0.01, app, pid, host, address, down=rng.randrange(1200, 60000))
        else:
            capture.tls(t + 0.01, app, pid, host, address, down=rng.randrange(1000, 80000),
                        up=rng.randrange(500, 8000), chunks=rng.randrange(2, 40))
        t += rng.random() * 0.05
    return capture.packets
