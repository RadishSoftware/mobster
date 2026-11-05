"""A small pcapng reader. A classic pcap has no place for the process, so it is refused."""

import re
import struct
from dataclasses import dataclass

SHB = 0x0A0D0D0A
IDB = 0x00000001
SPB = 0x00000003
EPB = 0x00000006
PIB = 0x80000001          # Apple: process information block
BYTE_ORDER_MAGIC = 0x1A2B3C4D
CLASSIC_MAGICS = {b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"}

OPT_END, OPT_COMMENT = 0, 1
IF_NAME, IF_TSRESOL = 2, 9
EPB_FLAGS = 2
# Apple's libpcap numbers (pcap-ng.h), as macOS `tcpdump -P` writes them.
EPB_PIB_INDEX, EPB_SVC, EPB_E_PIB_INDEX = 0x8001, 0x8002, 0x8003
PIB_NAME = 2

MAX_BLOCK = 1 << 20       # a block longer than 1 MB is damage, not a packet

# The per-packet comment mobster-pcap writes (pymobiledevice3's pcapd client wrote the same text).
COMMENT = re.compile(r"PID: (-?\d+), ProcName: (.*?), EPID: (-?\d+), EProcName: (.*?), SVC: (-?\d+)\s*$")


class PcapngError(ValueError):
    pass


@dataclass(slots=True)
class Packet:
    time: float               # seconds since the epoch, as the capturing Mac stamped it
    linktype: int
    data: bytes               # the captured bytes, link layer first; parsed and dropped, never kept
    length: int               # the packet's length on the wire
    process: str | None = None
    pid: int | None = None
    eprocess: str | None = None
    epid: int | None = None
    direction: str | None = None   # "in" or "out" when the file says (epb_flags)
    interface: str | None = None


@dataclass(slots=True)
class Interface:
    linktype: int
    name: str | None
    tsresol: float


def _options(body, offset, endian):
    """(code, value) pairs from an options list starting at ``offset``."""
    end = len(body)
    while offset + 4 <= end:
        code, length = struct.unpack_from(endian + "HH", body, offset)
        offset += 4
        if code == OPT_END:
            return
        value = body[offset:offset + length]
        offset += (length + 3) & ~3
        yield code, value


def _text(value):
    return value.split(b"\0", 1)[0].decode("utf-8", "replace").strip()


def _resolution(value):
    if not value:
        return 1e-6
    byte = value[0]
    return 2.0 ** -(byte & 0x7F) if byte & 0x80 else 10.0 ** -byte


def parse_comment(text):
    """(process, pid, effective process, effective pid) from a packet's process comment, or None."""
    match = COMMENT.search(text or "")
    if not match:
        return None
    return match.group(2).strip() or None, int(match.group(1)), match.group(4).strip() or None, int(match.group(3))


class Reader:
    """Iterate ``Packet``s from a binary file object. PcapngError when the file isn't pcapng."""

    def __init__(self, stream):
        self.stream = stream
        self.truncated = False
        self.interfaces = []
        self.processes = []    # (pid, name), in block order, per section
        self.endian = "<"
        head = stream.read(12)
        if len(head) < 12:
            raise PcapngError("The file is empty or too short to be a capture.")
        if head[:4] in CLASSIC_MAGICS:
            raise PcapngError("This is a classic pcap file. Network X-ray reads pcapng, which records the process "
                              "behind each packet: record again with `mobster xray network`.")
        if struct.unpack_from("<I", head)[0] != SHB:
            raise PcapngError("This isn't a pcapng capture. Give it the .pcapng file `mobster xray network` recorded.")
        self._pending = head

    def __iter__(self):
        return self._blocks()

    def _read_block(self):
        head = self._pending or self.stream.read(8)
        self._pending = b""
        if not head:
            return None
        if len(head) < 8:
            self.truncated = True
            return None
        block_type = struct.unpack_from(self.endian + "I", head)[0]
        if block_type == SHB:
            magic = head[8:12] if len(head) >= 12 else self.stream.read(4)
            if len(head) < 12:
                head += magic
            if len(magic) < 4:
                self.truncated = True
                return None
            if struct.unpack("<I", magic)[0] == BYTE_ORDER_MAGIC:
                self.endian = "<"
            elif struct.unpack(">I", magic)[0] == BYTE_ORDER_MAGIC:
                self.endian = ">"
            else:
                raise PcapngError("The pcapng section header has an unknown byte order mark.")
        total = struct.unpack_from(self.endian + "I", head, 4)[0]
        if total < 12 or total % 4 or total > MAX_BLOCK:
            if block_type == SHB and not self.interfaces and not self.processes:
                raise PcapngError("The pcapng section header is damaged.")
            self.truncated = True
            return None
        rest = self.stream.read(total - len(head))
        if len(rest) < total - len(head):
            self.truncated = True
            return None
        block = head + rest
        if struct.unpack_from(self.endian + "I", block, total - 4)[0] != total:
            self.truncated = True
            return None
        return block_type, block[8:total - 4]

    def _blocks(self):
        while True:
            item = self._read_block()
            if item is None:
                return
            block_type, body = item
            endian = self.endian
            if block_type == SHB:
                self.interfaces, self.processes = [], []
            elif block_type == IDB:
                linktype = struct.unpack_from(endian + "H", body, 0)[0]
                name, resolution = None, 1e-6
                for code, value in _options(body, 8, endian):
                    if code == IF_NAME:
                        name = _text(value)
                    elif code == IF_TSRESOL:
                        resolution = _resolution(value)
                self.interfaces.append(Interface(linktype, name, resolution))
            elif block_type == PIB:
                if len(body) < 4:
                    continue
                pid = struct.unpack_from(endian + "I", body, 0)[0]
                name = None
                for code, value in _options(body, 4, endian):
                    if code == PIB_NAME:
                        name = _text(value) or None
                self.processes.append((pid, name))
            elif block_type == EPB:
                packet = self._packet(body, endian)
                if packet is not None:
                    yield packet
            elif block_type == SPB and self.interfaces and len(body) >= 4:
                length = struct.unpack_from(endian + "I", body, 0)[0]
                interface = self.interfaces[0]
                yield Packet(0.0, interface.linktype, body[4:4 + length], length, interface=interface.name)

    def _packet(self, body, endian):
        if len(body) < 20:
            return None
        interface_id, high, low, captured, length = struct.unpack_from(endian + "IIIII", body, 0)
        if interface_id >= len(self.interfaces) or 20 + captured > len(body):
            return None
        interface = self.interfaces[interface_id]
        packet = Packet(((high << 32) | low) * interface.tsresol, interface.linktype, body[20:20 + captured],
                        length, interface=interface.name)
        for code, value in _options(body, 20 + ((captured + 3) & ~3), endian):
            if code == OPT_COMMENT:
                found = parse_comment(value.decode("utf-8", "replace"))
                if found:
                    packet.process, packet.pid, packet.eprocess, packet.epid = found
            elif code in (EPB_PIB_INDEX, EPB_E_PIB_INDEX) and len(value) >= 4:
                index = struct.unpack_from(endian + "I", value)[0]
                if index < len(self.processes):
                    pid, name = self.processes[index]
                    if code == EPB_PIB_INDEX:
                        packet.pid, packet.process = pid, name
                    else:
                        packet.epid, packet.eprocess = pid, name
            elif code == EPB_FLAGS and len(value) >= 4:
                inbound = struct.unpack_from(endian + "I", value)[0] & 3
                packet.direction = {1: "in", 2: "out"}.get(inbound)
        return packet


def read(path):
    """(packets iterator, reader) for the file at ``path``; the caller closes nothing: the file closes at the end."""
    stream = open(path, "rb")
    try:
        reader = Reader(stream)
    except BaseException:
        stream.close()
        raise

    def packets():
        try:
            yield from reader
        finally:
            stream.close()
    return packets(), reader
