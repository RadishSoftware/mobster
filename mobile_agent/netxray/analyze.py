"""Who each app talked to, from hostnames, sizes and times. No payload, URL path, other header or address is kept."""

from dataclasses import dataclass, field

from .. import secret_filter
from . import attribution, pcapng
from . import packets as wire
from .trackers import TRACKING, classify, registrable

HELLO_LIMIT = 16 * 1024      # client bytes held per connection while looking for its hostname
HELLO_SEGMENTS = 8
QUIC_PACKETS = 4             # client Initial packets read per QUIC connection
TCP_IDLE = 300.0
UDP_IDLE = 120.0
DNS_PORTS = {53, 853}
SERVICE_PORTS = {53, 80, 123, 143, 443, 465, 587, 853, 993, 995, 5223, 5228, 8080, 8443}
NO_PROCESS = ""                # a packet or connection the capture gave no process name


@dataclass(slots=True)
class Flow:
    proto: int
    phone: tuple             # (address, port) on the phone's side
    remote: tuple
    app: str
    via: str | None
    start: float
    last: float
    local: bool
    up: int = 0
    down: int = 0
    packets: int = 0
    host: str | None = None
    named_by: str | None = None    # sni, http, quic, dns
    kind: str = "other"            # tls, http, quic, dns, other
    hello: bytearray | None = field(default_factory=bytearray)
    hello_segments: int = 0
    quic: dict | None = None
    closed: bool = False


class Analysis:
    """Feed it packets (``add``), then ``result()``. ``names``: attribution.Names."""

    def __init__(self, names=None):
        self.names = names if names is not None else attribution.Names()
        self.flows = {}
        self.finished = []
        self.dns = {}                 # address bytes -> [(time, name)]
        self.lookups = {}             # app -> {name}
        self.dns_bytes = {}           # app -> bytes
        self.local = {"packets": 0, "bytes": 0}
        self.packets = 0
        self.decoded = 0
        self.first = None
        self.last = None
        self.with_process = 0

    def add(self, packet):
        self.packets += 1
        if packet.time:
            if self.first is None or packet.time < self.first:
                self.first = packet.time
            if self.last is None or packet.time > self.last:
                self.last = packet.time
        segment = wire.decode(packet.linktype, packet.data)
        if segment is None:
            return
        self.decoded += 1
        process, eprocess = packet.process, packet.eprocess
        if segment.process is not None:
            process, _pid, eprocess, _epid = segment.process
        if process or eprocess:
            self.with_process += 1
        app = eprocess or process or NO_PROCESS
        via = process if process and eprocess and process != eprocess else None
        src, dst = (segment.src, segment.sport), (segment.dst, segment.dport)
        flow, up = self._flow(segment, packet, src, dst, app, via)
        flow.packets += 1
        flow.last = packet.time
        if up:
            flow.up += segment.size
        else:
            flow.down += segment.size
        if flow.kind == "dns":
            self._dns(flow, segment, up, packet.time)   # a resolver on the local network answers here too
            return
        if flow.local:
            self.local["packets"] += 1
            self.local["bytes"] += segment.size
            flow.hello = None
            return
        if flow.hello is not None and up:
            self._hello(flow, segment)

    def _flow(self, segment, packet, src, dst, app, via):
        key = (segment.proto, src, dst) if src < dst else (segment.proto, dst, src)
        flow = self.flows.get(key)
        syn = segment.proto == wire.TCP and segment.flags & wire.SYN and not segment.flags & wire.ACK
        if flow is not None:
            idle = TCP_IDLE if segment.proto == wire.TCP else UDP_IDLE
            if (syn and (flow.closed or flow.packets > 2)) or packet.time - flow.last > idle:
                self.finished.append(flow)
                flow = None
        if flow is None:
            phone_is_src = self._phone_is_source(segment, packet, src, dst, syn)
            phone, remote = (src, dst) if phone_is_src else (dst, src)
            flow = Flow(segment.proto, phone, remote, app, via, packet.time, packet.time, wire.is_local(remote[0]))
            if remote[1] in DNS_PORTS:
                flow.kind = "dns"
                flow.hello = None
            self.flows[key] = flow
        elif flow.app == NO_PROCESS and app != NO_PROCESS:
            flow.app, flow.via = app, via
        if segment.proto == wire.TCP and segment.flags & (wire.FIN | wire.RST):
            flow.closed = True
        return flow, src == flow.phone

    @staticmethod
    def _phone_is_source(segment, packet, src, dst, syn):
        if packet.direction == "out":
            return True
        if packet.direction == "in":
            return False
        if segment.proto == wire.TCP and segment.flags & wire.SYN:
            return bool(syn)
        source_service = src[1] in SERVICE_PORTS or src[1] < 1024
        destination_service = dst[1] in SERVICE_PORTS or dst[1] < 1024
        if destination_service != source_service:
            return destination_service
        source_local, destination_local = wire.is_local(src[0]), wire.is_local(dst[0])
        if source_local != destination_local:
            return source_local
        return True

    def _dns(self, flow, segment, up, time):
        flow.hello = None
        self.dns_bytes[flow.app] = self.dns_bytes.get(flow.app, 0) + segment.size
        payload = segment.payload[2:] if segment.proto == wire.TCP else segment.payload
        if segment.proto == wire.TCP and len(payload) < 12 or flow.remote[1] == 853:
            return
        parsed = wire.dns(payload)
        if parsed is None:
            return
        response, asked, records = parsed
        asked = wire.clean_host(asked) if asked else None
        if not response:
            if asked:
                self.lookups.setdefault(flow.app, set()).add(asked)
            return
        for name, kind, value in records:
            if kind in ("A", "AAAA"):
                host = asked or wire.clean_host(name)
                if host:
                    self.dns.setdefault(bytes(value), []).append((time, host))

    def _hello(self, flow, segment):
        payload = segment.payload
        if not len(payload):
            return
        if segment.proto == wire.UDP:
            self._quic(flow, payload)
            return
        flow.hello += payload
        flow.hello_segments += 1
        buffer = flow.hello
        if buffer[:1] == b"\x16":
            flow.kind = "tls"
            found = wire.tls_sni(bytes(buffer))
            source = "sni"
        else:
            found = wire.http_host(bytes(buffer))
            source = "http"
            if found is not None:
                flow.kind = "http"
        if found is wire.NEED_MORE and len(buffer) < HELLO_LIMIT and flow.hello_segments < HELLO_SEGMENTS:
            return
        if found is not None and found is not wire.NEED_MORE:
            flow.host, flow.named_by = found, source
        flow.hello = None   # hostname read or never coming: the bytes go

    def _quic(self, flow, payload):
        initial = wire.quic_initial(payload)
        if initial is None:
            if flow.kind != "quic":
                flow.hello = None
            return
        flow.kind = "quic"
        if flow.quic is None:
            flow.quic = {"packets": 0, "frames": {}}
        flow.quic["packets"] += 1
        frames = wire.quic_crypto_frames(payload)
        for offset, data in frames or ():
            flow.quic["frames"].setdefault(offset, data)
        assembled = bytearray()
        for offset in sorted(flow.quic["frames"]):
            if offset > len(assembled):
                break
            data = flow.quic["frames"][offset]
            assembled += data[len(assembled) - offset:]
        found = wire.sni_from_handshake(bytes(assembled)) if assembled else wire.NEED_MORE
        if found is wire.NEED_MORE and frames is not None and flow.quic["packets"] < QUIC_PACKETS:
            return
        if found is not None and found is not wire.NEED_MORE:
            flow.host, flow.named_by = found, "quic"
        flow.hello, flow.quic = None, None

    def _name(self, flow):
        if flow.host:
            return flow.host
        answers = self.dns.get(flow.remote[0])
        if not answers:
            return None
        before = [name for time, name in answers if time <= flow.start + 1.0]
        flow.named_by = "dns"
        return before[-1] if before else answers[0][1]

    def result(self, *, truncated=False):
        safe = secret_filter.redact
        flows = self.finished + list(self.flows.values())
        origin = self.first or 0.0
        groups = {}

        def group(raw):
            label, kind = self.names.label(raw)
            label = safe(label)
            found = groups.get(label)
            if found is None:
                found = groups[label] = {
                    "name": label, "kind": kind, "processes": set(), "via": set(), "domains": {}, "up": 0, "down": 0,
                    "connections": 0, "unnamed": {"connections": 0, "up": 0, "down": 0}, "lookups": set(),
                    "dns_bytes": 0}
            if raw:
                found["processes"].add(safe(raw))
            return found

        for flow in flows:
            if flow.local or flow.kind == "dns":
                continue
            app = group(flow.app)
            if flow.via:
                app["via"].add(safe(flow.via))
            app["up"] += flow.up
            app["down"] += flow.down
            app["connections"] += 1
            host = self._name(flow)
            flow.host = host
            if host is None:
                for key, value in (("connections", 1), ("up", flow.up), ("down", flow.down)):
                    app["unnamed"][key] += value
                continue
            shown = safe(host)
            domain = app["domains"].get(shown)
            if domain is None:
                company, category, _ = classify(host)
                domain = app["domains"][shown] = {
                    "host": shown, "domain": safe(registrable(host)), "company": safe(company), "category": category,
                    "up": 0, "down": 0, "packets": 0, "connections": 0, "first": flow.start - origin,
                    "last": flow.last - origin, "protocols": set(), "named_by": set()}
            domain["up"] += flow.up
            domain["down"] += flow.down
            domain["packets"] += flow.packets
            domain["connections"] += 1
            domain["first"] = min(domain["first"], flow.start - origin)
            domain["last"] = max(domain["last"], flow.last - origin)
            domain["protocols"].add(flow.kind)
            if flow.named_by:
                domain["named_by"].add(flow.named_by)
        for raw in set(self.lookups) | set(self.dns_bytes):
            app = group(raw)
            app["lookups"].update(safe(name) for name in self.lookups.get(raw, ()))
            app["dns_bytes"] += self.dns_bytes.get(raw, 0)
        out_apps = []
        for app in groups.values():
            domains = sorted(app["domains"].values(), key=lambda d: (-(d["up"] + d["down"]), d["host"]))
            for domain in domains:
                domain["protocols"] = sorted(domain["protocols"])
                domain["named_by"] = sorted(domain["named_by"])
                domain["first"] = round(domain["first"], 3)
                domain["last"] = round(domain["last"], 3)
            companies = sorted({domain["company"] for domain in domains})
            tracking = [domain["host"] for domain in domains if domain["category"] in TRACKING]
            note = {attribution.UNATTRIBUTED_KIND: attribution.UNATTRIBUTED_NOTE,
                    attribution.PROCESS: attribution.PROCESS_NOTE}.get(app["kind"])
            out_apps.append({
                "name": app["name"], "kind": app["kind"], "processes": sorted(app["processes"]),
                "via": sorted(app["via"]), "up": app["up"], "down": app["down"],
                "connections": app["connections"], "domain_count": len(domains), "companies": companies,
                "tracking": tracking, "domains": domains, "unnamed": app["unnamed"],
                "lookups": sorted(app["lookups"]), "dns_bytes": app["dns_bytes"], **({"note": note} if note else {})})
        out_apps.sort(key=lambda a: (-a["domain_count"], -(a["up"] + a["down"]), a["name"].lower()))
        return {
            "schema": "mobster.netxray/1",
            "capture": {"packets": self.packets, "decoded": self.decoded,
                        "with_process": self.with_process, "started": self.first,
                        "seconds": round((self.last or 0) - (self.first or 0), 3), "truncated": truncated},
            "apps": out_apps,
            "local": dict(self.local),
        }


def analyze_file(path, *, names=None):
    """The analysis of the pcapng file at ``path``. pcapng.PcapngError when it isn't one."""
    packets, reader = pcapng.read(path)
    analysis = Analysis(names)
    for packet in packets:
        analysis.add(packet)
    return analysis.result(truncated=reader.truncated)


def headline(app, seconds):
    """'In 60 s Flashlight Pro contacted 14 domains at 9 companies; 6 of them are ads, analytics or attribution.'"""
    if app["kind"] == attribution.UNATTRIBUTED_KIND:
        count = app["connections"]
        return (f"In {max(1, round(seconds))} s {count} connection{'s' * (count != 1)} had no process name, so Mobster "
                "couldn't tell which app sent " + ("it." if count == 1 else "them."))
    domains, companies, tracking = app["domain_count"], len(app["companies"]), len(app["tracking"])
    text = (f"In {max(1, round(seconds))} s {app['name']} contacted {domains} domain{'s' * (domains != 1)} "
            f"at {companies} compan{'ies' if companies != 1 else 'y'}")
    if tracking:
        text += f"; {tracking} of them {'are' if tracking != 1 else 'is'} ads, analytics or attribution"
    return text + "."
