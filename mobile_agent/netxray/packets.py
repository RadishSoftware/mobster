"""What Network X-ray decodes from a packet: headers and hostnames. Payloads are read here and never kept."""

import ipaddress
import struct

ETHERNET, RAW, NULL, LOOP = 1, 101, 0, 108
RAW_ALIASES = {101, 12, 14}
IPV4_ONLY, IPV6_ONLY = 228, 229
PKTAP = {149, 258}
TCP, UDP = 6, 17
IPV6_EXTENSIONS = {0, 43, 60}  # hop-by-hop, routing, destination options; 44 (fragment) is handled on its own

SYN, ACK, FIN, RST = 0x02, 0x10, 0x01, 0x04


class Segment:
    """One IP packet's transport view."""
    __slots__ = ("version", "src", "dst", "proto", "sport", "dport", "flags", "payload", "size", "process")

    def __init__(self, version, src, dst, proto, sport, dport, flags, payload, size, process=None):
        self.version, self.src, self.dst, self.proto = version, src, dst, proto
        self.sport, self.dport, self.flags, self.payload, self.size = sport, dport, flags, payload, size
        self.process = process


def _pktap(data):
    # Apple's PKTAP header (xnu bsd/net/pktap.h), in host byte order: little-endian on Apple silicon and Intel.
    if len(data) < 108:
        return None
    length, _next, dlt = struct.unpack_from("<III", data, 0)
    if length < 108 or length > len(data):
        return None
    pid = struct.unpack_from("<i", data, 52)[0]
    comm = bytes(data[56:73]).split(b"\0", 1)[0].decode("utf-8", "replace") or None
    epid = struct.unpack_from("<i", data, 84)[0]
    ecomm = bytes(data[88:105]).split(b"\0", 1)[0].decode("utf-8", "replace") or None
    return dlt, length, (comm, pid, ecomm, epid)


def ip_offset(linktype, data):
    """Where the IP header starts in a frame of ``linktype``, or None."""
    # mobster-pcap's made-up Ethernet header says IPv4 even for IPv6, so the IP version comes from the packet.
    if linktype == ETHERNET:
        if len(data) < 14:
            return None
        ethertype = (data[12] << 8) | data[13]
        offset = 14
        while ethertype in (0x8100, 0x88A8) and len(data) >= offset + 4:
            ethertype = (data[offset + 2] << 8) | data[offset + 3]
            offset += 4
        return offset if ethertype in (0x0800, 0x86DD) else None
    if linktype in RAW_ALIASES or linktype in (IPV4_ONLY, IPV6_ONLY):
        return 0
    if linktype in (NULL, LOOP):
        return 4
    return None


def decode(linktype, data):
    """A Segment for a TCP or UDP packet, else None. ``data`` is the captured frame."""
    view = memoryview(data)
    process = None
    if linktype in PKTAP:
        found = _pktap(view)
        if found is None:
            return None
        linktype, offset, process = found
        view = view[offset:]
    offset = ip_offset(linktype, view)
    if offset is None or len(view) < offset + 20:
        return None
    first = view[offset] >> 4
    if first == 4:
        header = (view[offset] & 0x0F) * 4
        total = (view[offset + 2] << 8) | view[offset + 3]
        proto = view[offset + 9]
        fragment = ((view[offset + 6] & 0x1F) << 8) | view[offset + 7]
        if fragment or header < 20:
            return None  # a later fragment has no ports; the first fragment carries the ports and the hello
        src = bytes(view[offset + 12:offset + 16])
        dst = bytes(view[offset + 16:offset + 20])
        start = offset + header
        end = min(len(view), offset + total) if total >= header else len(view)
        size = total or (len(view) - offset)
        version = 4
    elif first == 6:
        if len(view) < offset + 40:
            return None
        payload_length = (view[offset + 4] << 8) | view[offset + 5]
        proto = view[offset + 6]
        src = bytes(view[offset + 8:offset + 24])
        dst = bytes(view[offset + 24:offset + 40])
        start = offset + 40
        end = min(len(view), start + payload_length) if payload_length else len(view)
        size = 40 + payload_length
        while proto in IPV6_EXTENSIONS or proto == 44:
            if start + 8 > end:
                return None
            if proto == 44:
                if ((view[start + 2] << 8) | view[start + 3]) & 0xFFF8:
                    return None
                proto, start = view[start], start + 8
            else:
                proto, start = view[start], start + (view[start + 1] + 1) * 8
        version = 6
    else:
        return None
    if proto == TCP:
        if end - start < 20:
            return None
        sport, dport = struct.unpack_from("!HH", view, start)
        data_offset = (view[start + 12] >> 4) * 4
        flags = view[start + 13]
        return Segment(version, src, dst, TCP, sport, dport, flags, view[start + data_offset:end], size, process)
    if proto == UDP:
        if end - start < 8:
            return None
        sport, dport = struct.unpack_from("!HH", view, start)
        return Segment(version, src, dst, UDP, sport, dport, 0, view[start + 8:end], size, process)
    return None


def is_local(raw):
    # Not ipaddress.is_private, which also covers the documentation ranges. These names never reach a report.
    if len(raw) == 4:
        a, b = raw[0], raw[1]
        return (a in (0, 10, 127) or a >= 224 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)
                or (a == 169 and b == 254) or (a == 100 and 64 <= b <= 127))
    a, b = raw[0], raw[1]
    return (a == 0xFF or (a & 0xFE) == 0xFC or (a == 0xFE and (b & 0xC0) == 0x80)
            or bytes(raw) in (bytes(16), bytes(15) + b"\x01"))


def clean_host(raw):
    """A hostname as letters, digits, dots, hyphens and underscores, lowercase, at most 253 characters; None when the
    bytes aren't one."""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            raw = bytes(raw).decode("ascii")
        except UnicodeDecodeError:
            return None
    host = raw.strip().rstrip(".").lower()
    if not host or len(host) > 253 or ".." in host or host.startswith((".", "-")):
        return None
    for char in host:
        if not (char.isascii() and (char.isalnum() or char in ".-_")):
            return None
    if "." not in host:
        return None
    try:
        ipaddress.ip_address(host)
        return None  # an address in the name field names nothing
    except ValueError:
        return host


def _dns_name(message, offset, depth=0):
    """(name, offset after it in the original position) with compression pointers followed."""
    labels = []
    end = None
    jumps = 0
    while True:
        if offset >= len(message):
            return None, None
        length = message[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(message) or jumps > 20:
                return None, None
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | message[offset + 1]
            jumps += 1
            continue
        if length == 0:
            if end is None:
                end = offset + 1
            break
        label = bytes(message[offset + 1:offset + 1 + length])
        labels.append(label)
        offset += 1 + length
    try:
        return ".".join(label.decode("ascii") for label in labels), end
    except UnicodeDecodeError:
        return None, end


def dns(payload):
    message = bytes(payload)
    if len(message) < 12:
        return None
    flags, questions, answers = struct.unpack_from("!HHH", message, 2)
    if questions > 8 or answers > 64:
        return None
    response = bool(flags & 0x8000)
    offset = 12
    asked = None
    for _ in range(questions):
        name, offset = _dns_name(message, offset)
        if offset is None:
            return None
        asked = asked or name
        offset += 4
    records = []
    for _ in range(answers if response else 0):
        name, offset = _dns_name(message, offset)
        if offset is None or offset + 10 > len(message):
            break
        kind, _class, _ttl, length = struct.unpack_from("!HHIH", message, offset)
        offset += 10
        value = message[offset:offset + length]
        if kind == 1 and length == 4:
            records.append((name, "A", value))
        elif kind == 28 and length == 16:
            records.append((name, "AAAA", value))
        elif kind == 5:
            target, _ = _dns_name(message, offset)
            if target:
                records.append((name, "CNAME", target))
        offset += length
    return response, asked, records


NEED_MORE = object()


def sni_from_handshake(message):
    if len(message) < 4 or message[0] != 1:
        return None
    length = int.from_bytes(message[1:4], "big")
    if len(message) < 4 + length:
        return NEED_MORE
    body = message[4:4 + length]
    offset = 2 + 32                                        # version, random
    if offset >= len(body):
        return None
    offset += 1 + body[offset]                             # session id
    if offset + 2 > len(body):
        return None
    offset += 2 + int.from_bytes(body[offset:offset + 2], "big")   # cipher suites
    if offset >= len(body):
        return None
    offset += 1 + body[offset]                             # compression methods
    if offset + 2 > len(body):
        return None
    end = min(len(body), offset + 2 + int.from_bytes(body[offset:offset + 2], "big"))
    offset += 2
    while offset + 4 <= end:
        kind = int.from_bytes(body[offset:offset + 2], "big")
        size = int.from_bytes(body[offset + 2:offset + 4], "big")
        data = body[offset + 4:offset + 4 + size]
        if kind == 0 and len(data) >= 5:                   # server_name: list length, type 0, name length, name
            name_length = int.from_bytes(data[3:5], "big")
            if data[2] == 0:
                return clean_host(data[5:5 + name_length])
            return None
        offset += 4 + size
    return None


def tls_sni(stream):
    """The server name from the first bytes a client sent on a TLS connection (records joined across segments)."""
    if len(stream) < 5:
        return NEED_MORE if stream[:1] in (b"", b"\x16") else None
    if stream[0] != 0x16 or stream[1] != 3:
        return None
    handshake = bytearray()
    offset = 0
    while offset + 5 <= len(stream) and stream[offset] == 0x16:
        length = int.from_bytes(stream[offset + 3:offset + 5], "big")
        handshake += stream[offset + 5:offset + 5 + length]
        if offset + 5 + length > len(stream):
            break
        offset += 5 + length
        if len(handshake) >= 4 and len(handshake) >= 4 + int.from_bytes(handshake[1:4], "big"):
            break
    return sni_from_handshake(bytes(handshake))


METHODS = (b"GET ", b"POST ", b"HEAD ", b"PUT ", b"DELETE ", b"OPTIONS ", b"PATCH ", b"CONNECT ")


def http_host(stream):
    """The Host header of a plain HTTP/1 request; the path and every other header are never returned."""
    if not stream.startswith(METHODS):
        return None if len(stream) >= 8 or not any(m.startswith(bytes(stream[:8])) for m in METHODS) else NEED_MORE
    end = stream.find(b"\r\n\r\n")
    head = stream if end < 0 else stream[:end]
    for line in bytes(head).split(b"\r\n")[1:]:
        if line[:5].lower() == b"host:":
            host = line[5:].strip()
            if host.startswith(b"["):
                return None
            return clean_host(host.split(b":", 1)[0])
    return None if end >= 0 else NEED_MORE


QUIC_V1 = 0x00000001
QUIC_V1_SALT = bytes.fromhex("38762cf7f55934b34d179ae6a4c80cadccbb7f0a")


def quic_initial(payload):
    """(version, destination connection id) for a QUIC long-header Initial packet, else None."""
    if len(payload) < 7 or payload[0] & 0xC0 != 0xC0:
        return None
    version = int.from_bytes(payload[1:5], "big")
    if version == QUIC_V1 and (payload[0] >> 4) & 3 != 0:
        return None
    dcid_length = payload[5]
    if dcid_length > 20 or len(payload) < 6 + dcid_length:
        return None
    return version, bytes(payload[6:6 + dcid_length])


def _varint(data, offset):
    first = data[offset]
    size = 1 << (first >> 6)
    if offset + size > len(data):
        raise ValueError("varint")
    value = first & 0x3F
    for byte in data[offset + 1:offset + size]:
        value = (value << 8) | byte
    return value, offset + size


def _hkdf_label(secret, label, length):
    import hmac
    full = b"tls13 " + label
    info = struct.pack("!H", length) + bytes([len(full)]) + full + b"\0"
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(secret, block + info + bytes([counter]), "sha256").digest()
        out += block
        counter += 1
    return out[:length]


def quic_client_keys(dcid):
    """(key, iv, header protection key) for a QUIC v1 client Initial (RFC 9001 section 5.2)."""
    import hmac
    initial = hmac.new(QUIC_V1_SALT, dcid, "sha256").digest()
    client = _hkdf_label(initial, b"client in", 32)
    return (_hkdf_label(client, b"quic key", 16), _hkdf_label(client, b"quic iv", 12),
            _hkdf_label(client, b"quic hp", 16))


def _aes():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return None
    return Cipher, algorithms, modes, AESGCM


def quic_crypto_frames(payload):
    # Decrypted with the keys anyone can derive from the packet (RFC 9001 section 5); needs ``cryptography``.
    found = quic_initial(payload)
    aes = _aes()
    if found is None or found[0] != QUIC_V1 or aes is None:
        return None
    Cipher, algorithms, modes, AESGCM = aes
    data = bytes(payload)
    try:
        offset = 6 + len(found[1])
        offset += 1 + data[offset]                          # source connection id
        token_length, offset = _varint(data, offset)
        offset += token_length
        length, offset = _varint(data, offset)
        if offset + length > len(data) or offset + 20 > len(data):
            return None
        key, iv, hp = quic_client_keys(found[1])
        sample = data[offset + 4:offset + 20]
        mask = Cipher(algorithms.AES(hp), modes.ECB()).encryptor().update(sample)
        first = data[0] ^ (mask[0] & 0x0F)
        number_length = (first & 3) + 1
        number = bytes(b ^ m for b, m in zip(data[offset:offset + number_length], mask[1:1 + number_length]))
        header = bytearray(data[:offset + number_length])
        header[0] = first
        header[offset:offset + number_length] = number
        nonce = bytes(a ^ b for a, b in zip(iv, int.from_bytes(number, "big").to_bytes(12, "big")))
        plain = AESGCM(key).decrypt(nonce, data[offset + number_length:offset + length], bytes(header))
    except Exception:  # noqa: BLE001 - a packet that isn't what it claims is skipped, never fatal
        return None
    frames = []
    position = 0
    try:
        while position < len(plain):
            kind = plain[position]
            if kind == 0x00 or kind == 0x01:               # padding, ping
                position += 1
            elif kind == 0x06:                             # CRYPTO
                start, position = _varint(plain, position + 1)
                size, position = _varint(plain, position)
                frames.append((start, plain[position:position + size]))
                position += size
            elif kind in (0x02, 0x03):                     # ACK
                _, position = _varint(plain, position + 1)
                _, position = _varint(plain, position)
                ranges, position = _varint(plain, position)
                _, position = _varint(plain, position)
                for _ in range(ranges * 2):
                    _, position = _varint(plain, position)
                if kind == 0x03:
                    for _ in range(3):
                        _, position = _varint(plain, position)
            else:
                break
    except ValueError:
        pass
    return frames
