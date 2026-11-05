import io
import json
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from mobile_agent import links, result_blocks
from mobile_agent.agent_hooks import MASK
from mobile_agent.netxray import analyze, attribution, pcapng, report, store, summary, trackers
from mobile_agent.netxray import packets as wire
from mobile_agent.tests import netxray_synthetic as synthetic
from mobile_agent.tests.netxray_synthetic import (
    PHONE,
    Capture,
    Writer,
    client_hello,
    dns_response,
    ethernet,
    http_request,
    ip,
    process_comment,
    server,
    tcp,
    tls_record,
    udp,
)

T0 = 1_792_000_000.0


def read_all(data):
    reader = pcapng.Reader(io.BytesIO(data))
    return list(reader), reader


def run(build, **options):
    stream = io.BytesIO()
    build(stream)
    stream.seek(0)
    analysis = analyze.Analysis(**options)
    reader = pcapng.Reader(stream)
    for packet in reader:
        analysis.add(packet)
    return analysis, analysis.result(truncated=reader.truncated)


def app(result, name):
    return next(item for item in result["apps"] if item["name"] == name)


def hosts(result, name):
    return {domain["host"]: domain for domain in app(result, name)["domains"]}


class PcapngReaderTest(unittest.TestCase):
    def test_process_comments_name_the_process_on_ethernet_with_ipv4_and_ipv6(self):
        stream = io.BytesIO()
        writer = Writer(stream)
        interface = writer.interface(1)
        v4 = ip(PHONE, server(1), wire.UDP, udp(5000, 443, b"x"))
        v6 = ip(synthetic.PHONE6, bytes.fromhex("20010db8000000000000000000000099"), wire.UDP, udp(5000, 443, b"y"))
        writer.packet(interface, T0, ethernet(v4, real=False), comment=process_comment(412, "Flashlight Pro"))
        writer.packet(interface, T0 + 1, ethernet(v6, real=False),
                      comment=process_comment(90, "mDNSResponder", 500, "TikTok"))
        found, reader = read_all(stream.getvalue())
        self.assertFalse(reader.truncated)
        self.assertEqual([(p.process, p.pid, p.eprocess, p.epid) for p in found],
                         [("Flashlight Pro", 412, "Flashlight Pro", 412), ("mDNSResponder", 90, "TikTok", 500)])
        self.assertAlmostEqual(found[1].time, T0 + 1, places=5)
        self.assertEqual([wire.decode(p.linktype, p.data).version for p in found], [4, 6])

    def test_apple_process_blocks_name_the_process_on_raw_ip(self):
        stream = io.BytesIO()
        writer = Writer(stream)
        interface = writer.interface(101, "pdp_ip0")
        packet = ip(PHONE, server(2), wire.TCP, tcp(5000, 443, 0x02))
        writer.packet(interface, T0, packet, pib=writer.process(312, "TikTok"),
                      epib=writer.process(312, "TikTok"), direction="out")
        writer.packet(interface, T0, packet, pib=writer.process(120, "nsurlsessiond"),
                      epib=writer.process(412, "Flashlight Pro"), direction="in")
        found, _ = read_all(stream.getvalue())
        self.assertEqual([(p.process, p.eprocess, p.direction, p.interface) for p in found],
                         [("TikTok", "TikTok", "out", "pdp_ip0"), ("nsurlsessiond", "Flashlight Pro", "in", "pdp_ip0")])
        self.assertEqual(wire.decode(found[0].linktype, found[0].data).dport, 443)

    @unittest.skipUnless(Path("/usr/sbin/tcpdump").exists(), "macOS tcpdump reads Apple's process blocks")
    def test_macos_tcpdump_reads_the_process_blocks_as_this_reader_does(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pib.pcapng"
            with open(path, "wb") as stream:
                writer = Writer(stream)
                interface = writer.interface(101, "rvi0")
                writer.packet(interface, T0, ip(PHONE, server(3), wire.UDP, udp(5000, 443, b"z")),
                              pib=writer.process(312, "TikTok"), epib=writer.process(90, "mDNSResponder"),
                              direction="out")
            printed = subprocess.run(["/usr/sbin/tcpdump", "-r", str(path), "-n", "-k", "NPD"], capture_output=True,
                                     text=True, timeout=20).stdout
        self.assertIn("proc TikTok:312", printed)
        self.assertIn("eproc mDNSResponder:90", printed)
        self.assertIn("out", printed)

    def test_a_capture_cut_mid_block_keeps_the_packets_before_the_cut(self):
        stream = io.BytesIO()
        with_steps = Capture(stream)
        with_steps.tls(T0, "Flashlight Pro", 412, "api.flashlightpro.example", server(10))
        data = stream.getvalue()
        whole, _ = read_all(data)
        found, reader = read_all(data[:-7])
        self.assertTrue(reader.truncated)
        self.assertEqual(len(found), len(whole) - 1)

    def test_it_refuses_anything_but_pcapng(self):
        classic = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
        for data, words in ((classic, "classic pcap"), (b"GIF89a" + bytes(40), "isn't a pcapng"), (b"", "empty")):
            with self.subTest(words=words), self.assertRaises(pcapng.PcapngError) as caught:
                pcapng.Reader(io.BytesIO(data))
            self.assertIn(words, str(caught.exception))


class DecoderTest(unittest.TestCase):
    def test_dns_answers_follow_compression_and_cnames(self):
        message = dns_response("api.example.com", [server(5), bytes(range(16))], cname="edge.cdn.example.net")
        response, asked, records = wire.dns(message)
        self.assertTrue(response)
        self.assertEqual(asked, "api.example.com")
        self.assertEqual([(name, kind) for name, kind, _ in records],
                         [("api.example.com", "CNAME"), ("edge.cdn.example.net", "A"),
                          ("edge.cdn.example.net", "AAAA")])
        self.assertEqual(records[0][2], "edge.cdn.example.net")

    def test_the_server_name_comes_from_a_client_hello_whole_or_split(self):
        record = tls_record(client_hello("graph.facebook.com", padding=1500))
        self.assertEqual(wire.tls_sni(record), "graph.facebook.com")
        self.assertIs(wire.tls_sni(record[:1300]), wire.NEED_MORE)
        self.assertIsNone(wire.tls_sni(tls_record(client_hello(None))))
        self.assertIsNone(wire.tls_sni(b"\x17\x03\x03\x00\x10" + bytes(16)))

    def test_http_gives_the_host_and_never_the_path_or_headers(self):
        self.assertEqual(wire.http_host(http_request("geo.locate-api.example")), "geo.locate-api.example")
        self.assertIs(wire.http_host(b"GET /a HTTP/1.1\r\nUser-Agent: x\r\n"), wire.NEED_MORE)
        self.assertIsNone(wire.http_host(b"SSH-2.0-OpenSSH_9.0\r\n"))

    def test_quic_initial_keys_match_rfc_9001(self):
        key, iv, hp = wire.quic_client_keys(bytes.fromhex("8394c8f03e515708"))
        self.assertEqual(key.hex(), "1f369613dd76d5467730efcbe3b1a22d")
        self.assertEqual(iv.hex(), "fa044b2f42a3fd3b46fb255c")
        self.assertEqual(hp.hex(), "9f50449e04a0e810283a1e9933adedd2")

    def test_a_quic_initial_gives_its_server_name(self):
        packet = synthetic.quic_initial("googleads.g.doubleclick.net")
        self.assertEqual(wire.quic_initial(packet)[0], wire.QUIC_V1)
        frames = wire.quic_crypto_frames(packet)
        self.assertEqual(wire.sni_from_handshake(frames[0][1]), "googleads.g.doubleclick.net")
        damaged = bytearray(packet)
        damaged[-1] ^= 1
        self.assertIsNone(wire.quic_crypto_frames(bytes(damaged)))

    def test_a_hostname_is_letters_digits_dots_and_hyphens_or_nothing(self):
        self.assertEqual(wire.clean_host(b"API.Example.COM."), "api.example.com")
        for junk in (b"evil.com<script>", "ex ample.com", b"\xff\xfe.com", b"localhost", b"203.0.113.9",
                     b"a..b.com", b"x" * 300 + b".com", b"-bad.com"):
            with self.subTest(junk=junk):
                self.assertIsNone(wire.clean_host(junk))

    def test_local_addresses(self):
        for raw in ([192, 168, 1, 1], [10, 0, 0, 2], [172, 20, 0, 1], [169, 254, 3, 4], [224, 0, 0, 251],
                    [100, 64, 1, 1], [255, 255, 255, 255]):
            self.assertTrue(wire.is_local(bytes(raw)), raw)
        for raw in ([17, 0, 0, 5], [203, 0, 113, 9], [8, 8, 8, 8], [172, 32, 0, 1]):
            self.assertFalse(wire.is_local(bytes(raw)), raw)
        self.assertTrue(wire.is_local(bytes.fromhex("fe80" + "00" * 14)))
        self.assertTrue(wire.is_local(bytes.fromhex("fd00" + "00" * 14)))
        self.assertTrue(wire.is_local(bytes.fromhex("ff02" + "00" * 13 + "fb")))
        self.assertFalse(wire.is_local(synthetic.PHONE6))

    def test_apple_pktap_frames_name_the_process(self):
        header = bytearray(108)
        struct.pack_into("<III", header, 0, 108, 1, 12)
        struct.pack_into("<i", header, 52, 412)
        header[56:56 + 14] = b"Flashlight Pro"
        struct.pack_into("<i", header, 84, 412)
        header[88:88 + 14] = b"Flashlight Pro"
        segment = wire.decode(258, bytes(header) + ip(PHONE, server(1), wire.TCP, tcp(5000, 443, 0x02)))
        self.assertEqual(segment.process, ("Flashlight Pro", 412, "Flashlight Pro", 412))
        self.assertEqual(segment.dport, 443)


class ClassifyTest(unittest.TestCase):
    def test_the_longest_suffix_decides(self):
        self.assertEqual(trackers.classify("firebaselogging-pa.googleapis.com")[:2], ("Google", "analytics"))
        self.assertEqual(trackers.classify("firebaseinstallations.googleapis.com")[:2], ("Google", "platform"))
        self.assertEqual(trackers.classify("www.googleapis.com")[:2], ("Google", "platform"))
        self.assertEqual(trackers.classify("graph.facebook.com")[:2], ("Meta", "analytics"))
        self.assertEqual(trackers.classify("www.facebook.com")[:2], ("Meta", "social"))
        self.assertEqual(trackers.classify("api2.appsflyer.com")[:2], ("AppsFlyer", "attribution"))
        self.assertEqual(trackers.classify("cdn.flashlightpro.example"), ("flashlightpro.example", None, None))

    def test_the_trackers_missed_on_a_real_phone_are_named(self):
        for host, expected in {"secure-asts.imrworldwide.com": ("Nielsen", "analytics"),
                               "engine.adzerk.net": ("Kevel", "advertising"),
                               "vast.zefrdata.com": ("Zefr", "advertising"),
                               "tags.tiqcdn.com": ("Tealium", "analytics"),
                               "events.mapbox.com": ("Mapbox", "analytics"),
                               "config.mapbox.com": ("Mapbox", "platform"),
                               "ca.iadsdk.apple.com": ("Apple", "attribution"),
                               "rr1---sn-x.googlevideo.com": ("Google", "cdn"),
                               "api16-normal-useast5.tiktokv.us": ("ByteDance", "social")}.items():
            with self.subTest(host):
                self.assertEqual(trackers.classify(host)[:2], expected)

    def test_registrable_domains(self):
        self.assertEqual(trackers.registrable("graph.facebook.com"), "facebook.com")
        self.assertEqual(trackers.registrable("news.bbc.co.uk"), "bbc.co.uk")
        self.assertEqual(trackers.registrable("my-app.web.app"), "my-app.web.app")


class AnalyzeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analysis, cls.result = run(synthetic.flashlight)

    def test_the_flashlight_capture_adds_up(self):
        flashlight = app(self.result, "Flashlight Pro")
        self.assertEqual(flashlight["domain_count"], 14)
        self.assertEqual(len(flashlight["tracking"]), 6)
        self.assertEqual(len(flashlight["companies"]), 10)
        self.assertEqual(analyze.headline(flashlight, 60), "In 60 s Flashlight Pro contacted 14 domains at 10 "
                                                           "companies; 6 of them are ads, analytics or attribution.")
        named = {host: domain["named_by"] for host, domain in hosts(self.result, "Flashlight Pro").items()}
        self.assertEqual(named["graph.facebook.com"], ["sni"])
        self.assertEqual(named["www.googleapis.com"], ["dns"])
        self.assertEqual(named["geo.locate-api.example"], ["http"])
        self.assertEqual(named["googleads.g.doubleclick.net"], ["quic"])
        self.assertEqual(hosts(self.result, "Flashlight Pro")["googleads.g.doubleclick.net"]["protocols"], ["quic"])

    def test_two_processes_stay_apart_and_dns_counts_for_the_app_that_asked(self):
        self.assertEqual(set(hosts(self.result, "iOS")), {"courier.push.apple.com"})
        self.assertEqual(app(self.result, "iOS")["processes"], ["apsd"])
        self.assertNotIn("courier.push.apple.com", hosts(self.result, "Flashlight Pro"))
        self.assertIn("api2.appsflyer.com", app(self.result, "Flashlight Pro")["lookups"])
        self.assertNotIn("mDNSResponder", [item["name"] for item in self.result["apps"]])
        self.assertEqual(app(self.result, "Flashlight Pro")["via"], ["nsurlsessiond"])

    def test_a_connection_with_no_name_counts_without_its_address(self):
        def build(stream):
            Capture(stream).tls(T0, "Flashlight Pro", 412, "x.example", server(77), sni=False, down=3000)
        _, result = run(build)
        flashlight = app(result, "Flashlight Pro")
        self.assertEqual(flashlight["domains"], [])
        self.assertEqual(flashlight["unnamed"]["connections"], 1)
        self.assertNotIn("203.0.113.77", json.dumps(result))

    def test_client_bytes_are_dropped_once_the_name_is_read_or_never_coming(self):
        def build(stream):
            capture = Capture(stream)
            capture.tls(T0, "Flashlight Pro", 412, "api.flashlightpro.example", server(10))
            sport = capture.port()
            for index in range(20):
                capture.send(T0 + index, ip(PHONE, server(12), wire.TCP, tcp(sport, 9000, 0x18, bytes(1400))),
                             "Flashlight Pro", 412)
        analysis, _ = run(build)
        flows = analysis.finished + list(analysis.flows.values())
        self.assertTrue(all(flow.hello is None for flow in flows if not flow.local and flow.kind != "dns"))


class PrivacyTest(unittest.TestCase):
    def test_reports_hold_no_payload_path_address_or_local_name(self):
        _, result = run(synthetic.flashlight)
        with tempfile.TemporaryDirectory() as folder:
            paths = report.write(result, folder, synthetic=True)
            texts = {kind: Path(path).read_text(encoding="utf-8") for kind, path in paths.items()}
        for kind, text in texts.items():
            for secret in ("SECRET-TOKEN", "SECRET-COOKIE", "/v1/locate", "FlashlightPro/3.1", "Sams-MacBook",
                           "192.168.", "203.0.113.", "17.0.0.5", "2001:db8"):
                self.assertNotIn(secret, text, f"{kind} says {secret}")
        self.assertIn("graph.facebook.com", texts["html"])
        self.assertIn("Synthetic capture", texts["html"])
        self.assertEqual(json.loads(texts["json"])["schema"], "mobster.netxray/1")

    def test_a_process_name_from_the_phone_is_text(self):
        evil = "<img src=x onerror=alert(1)>|`"

        def build(stream):
            Capture(stream).tls(T0, evil, 666, "api.flashlightpro.example", server(10))
        _, result = run(build)
        page = report.render_html(result)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertNotIn("<b>it</b>", page)
        markdown = report.render_markdown(result)
        self.assertNotRegex(markdown, r"(?<!\\)<img")
        self.assertIn("\\|\\`", markdown)

    def test_the_page_loads_nothing(self):
        _, result = run(synthetic.flashlight)
        page = report.render_html(result)
        self.assertIn("default-src 'none'", page)
        self.assertNotIn("<script", page)
        self.assertNotRegex(page, r"src=\"?https?:|url\(\s*['\"]?https?:|@import|<link\b")
        self.assertEqual(re.findall(r"href=\"?(https?:[^\"\s>]*)", page), [links.MADE_WITH_XRAY])


TOKEN_HOST = "sk-ant-api03-abcdefghijklmnop12345.tracker.example"
GITHUB_HOST = "ghp_abcdefghijklmnopqrstuvwx0123.hooks.example"
UDID = "00008130-000A1B2C3D4E5F60"
OTHER_UDID = "00008130-00000000ABCD000A"
SESSION = "s" * 40
APPS = {"Weather": "Weather", "MobileSafari": "Safari", "DuolingoLearning": "Duolingo"}


def weather(stream):
    capture_ = Capture(stream)
    capture_.lookup(T0, "Weather", 412, "api.weather.example", [server(10)])
    capture_.tls(T0 + 0.1, "Weather", 412, "api.weather.example", server(10), down=5000, up=900)
    capture_.tls(T0 + 1, "Weather", 412, "app-measurement.com", server(20), down=1000, up=3000)
    capture_.tls(T0 + 2, "MobileSafari", 500, "news.example", server(30), down=9000)
    capture_.tls(T0 + 3, "apsd", 88, "courier.push.apple.com", server(40), down=300, up=200, port=5223)
    capture_.tls(T0 + 4, "mysteryd", 99, "who-knows.example", server(50), down=700, up=100)


class Blind(Capture):
    def send(self, time_, packet, process, pid, eprocess=None, epid=None, direction=None):
        self.writer.packet(self.interface, time_, ethernet(packet, real=False))
        self.packets += 1


class AttributionTest(unittest.TestCase):
    def test_a_process_becomes_the_app_that_has_that_executable(self):
        names = attribution.Names(APPS)
        self.assertEqual(names.label("Weather"), ("Weather", "app"))
        self.assertEqual(names.label("MobileSafari"), ("Safari", "app"))

    def test_a_process_name_cut_to_sixteen_characters_matches_only_when_one_app_fits(self):
        self.assertEqual(attribution.Names(APPS).label("DuolingoLearning"), ("Duolingo", "app"))
        cut = "DuolingoLearning"[:16]
        names = attribution.Names({"DuolingoLearningA": "One", "DuolingoLearningB": "Two"})
        self.assertEqual(names.label(cut), (cut, "process"))

    def test_an_executable_two_apps_share_names_neither(self):
        rows = [{"executable": "Runner", "name": "A"}, {"executable": "Runner", "name": "B"},
                {"executable": "Solo", "name": "C"}, {"name": "No executable"}]
        self.assertEqual(attribution.executables_from(rows), {"Solo": "C"})

    def test_the_installed_app_list_carries_each_executable(self):
        from mobile_agent import installed_apps
        data = plistlib.dumps([{"CFBundleIdentifier": "com.apple.weather", "CFBundleDisplayName": "Weather",
                                "CFBundleExecutable": "Weather"},
                               {"CFBundleIdentifier": "com.example.none", "CFBundleName": "None"}])
        listed = installed_apps.parse_listing(data)
        self.assertEqual({app["name"]: app.get("executable") for app in listed}, {"Weather": "Weather", "None": None})
        self.assertIn("CFBundleExecutable", installed_apps.ATTRIBUTES)


class NamedAppsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.result = run(weather, names=attribution.Names(APPS))

    def test_each_process_is_shown_as_the_phones_app_and_the_effective_process_wins(self):
        self.assertEqual({item["name"]: item["kind"] for item in self.result["apps"]},
                         {"Weather": "app", "Safari": "app", "iOS": "ios", "mysteryd": "process"})
        self.assertEqual(set(hosts(self.result, "Weather")), {"api.weather.example", "app-measurement.com"})
        self.assertEqual(app(self.result, "Weather")["processes"], ["Weather"])
        self.assertNotIn("mDNSResponder", [item["name"] for item in self.result["apps"]])

    def test_a_daemon_is_ios_with_its_process_name_in_the_details(self):
        ios = app(self.result, "iOS")
        self.assertEqual((ios["processes"], list(hosts(self.result, "iOS"))), (["apsd"], ["courier.push.apple.com"]))
        page, markdown = report.render_html(self.result), report.render_markdown(self.result)
        self.assertIn("parts of iOS itself: apsd", page)
        self.assertIn("Parts of iOS: apsd", markdown)


class UnattributedTest(unittest.TestCase):
    def build(self, stream):
        weather(stream)
        blind = Blind(stream)
        blind.tls(T0 + 5, "", 0, "mystery-sdk.example", server(60), down=4000, up=500)
        blind.tls(T0 + 6, "", 0, "api.weather.example", server(10), sni=False, down=2000, up=100)

    def test_traffic_with_no_process_name_goes_to_unattributed_and_is_never_guessed(self):
        stream = io.BytesIO()
        blind = Blind(stream)
        blind.tls(T0, "", 0, "mystery-sdk.example", server(60), down=4000, up=500)
        blind.tls(T0 + 1, "", 0, "x.example", server(61), sni=False, down=2000, up=100)
        stream.seek(0)
        analysis = analyze.Analysis(attribution.Names(APPS))
        for packet in pcapng.Reader(stream):
            analysis.add(packet)
        result = analysis.result()
        self.assertEqual([item["name"] for item in result["apps"]], ["Unattributed"])
        lost = result["apps"][0]
        self.assertEqual((lost["kind"], lost["note"]), ("unattributed", attribution.UNATTRIBUTED_NOTE))
        self.assertGreater(lost["up"] + lost["down"], 6000)
        self.assertEqual(lost["connections"], 2)
        self.assertEqual(lost["unnamed"]["connections"], 1)
        self.assertEqual(result["capture"]["with_process"], 0)

    def test_the_sentence_and_the_bytes_reach_every_output(self):
        stream = io.BytesIO()
        Blind(stream).tls(T0, "", 0, "mystery-sdk.example", server(60), down=4000, up=500)
        stream.seek(0)
        analysis = analyze.Analysis()
        for packet in pcapng.Reader(stream):
            analysis.add(packet)
        result = analysis.result()
        self.assertEqual(summary.lines(result),
                         ["In 1 s 1 connection had no process name, so Mobster couldn't tell which app sent it."])
        self.assertIn("couldn't tell which app", summary.answer(result))
        unattributed = summary.live(result)["totals"]["unattributed"]
        self.assertGreater(unattributed["bytes"], 4000)
        self.assertEqual(unattributed["connections"], 1)
        text = report.render_html(result) + report.render_markdown(result)
        self.assertIn(attribution.UNATTRIBUTED_NOTE, text)
        blocks = summary.blocks(result)
        self.assertIn("Unattributed", json.dumps(blocks))
        self.assertEqual(result_blocks.validate({"v": 1, "blocks": blocks})[1], [])

    def test_a_connection_later_given_a_process_name_is_that_apps(self):
        stream = io.BytesIO()
        sender = Capture(stream)
        sport = sender.port()
        packet = ip(PHONE, server(70), wire.TCP, tcp(sport, 443, 0x02))
        sender.writer.packet(sender.interface, T0, ethernet(packet, real=False))
        sender.send(T0 + 0.1, ip(PHONE, server(70), wire.TCP, tcp(sport, 443, 0x10)), "Weather", 412)
        stream.seek(0)
        analysis = analyze.Analysis(attribution.Names(APPS))
        for item in pcapng.Reader(stream):
            analysis.add(item)
        self.assertEqual([item["name"] for item in analysis.result()["apps"]], ["Weather"])


class MaskingTest(unittest.TestCase):
    def build(self, stream):
        capture_ = Capture(stream)
        capture_.lookup(T0, "Weather", 412, TOKEN_HOST, [server(10)])
        capture_.tls(T0 + 0.1, "Weather", 412, TOKEN_HOST, server(10), down=2000)
        capture_.http(T0 + 1, "Weather", 412, GITHUB_HOST, server(11))
        capture_.tls(T0 + 2, "sk-ant-api03-abcdefghijklmnop12345", 77, "ok.example", server(12))

    def test_a_token_in_a_hostname_or_a_host_header_never_reaches_a_report_a_block_or_an_event(self):
        _, result = run(self.build, names=attribution.Names(APPS))
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        paths = report.write(result, folder)
        texts = [Path(path).read_text(encoding="utf-8") for path in paths.values()]
        texts += [json.dumps(summary.live(result)), json.dumps(summary.blocks(result)), summary.answer(result),
                  "\n".join(summary.lines(result))]
        envelope = result_blocks.envelope(summary.blocks(result))
        texts.append(json.dumps(envelope))
        for text in texts:
            for secret in ("abcdefghijklmnop", "ghp_abcdef", "sk-ant-api03"):
                self.assertNotIn(secret, text)
        self.assertIn(MASK, texts[0])
        self.assertEqual(sorted(hosts(result, "Weather")),
                         sorted({MASK + ".tracker.example", MASK + ".hooks.example"}))

    def test_a_masked_host_still_gets_its_company_from_the_real_one(self):
        _, result = run(self.build)
        domain = hosts(result, "Weather")[MASK + ".tracker.example"]
        self.assertEqual(domain["domain"], "tracker.example")
        self.assertEqual(domain["company"], "tracker.example")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)

    def put_capture(self, folder):
        with open(store.capture_path(folder), "wb") as stream:
            synthetic.flashlight(stream)
        store.capture_path(folder).chmod(0o600)

    def test_the_folder_is_private_named_for_the_time_and_never_reused(self):
        first = store.new_folder(T0, self.base)
        second = store.new_folder(T0, self.base)
        self.assertNotEqual(first, second)
        self.assertRegex(first.name, r"^\d{8}-\d{6}$")
        self.assertEqual(second.name, first.name + "-2")
        for folder in (first, second):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)

    def test_the_three_reports_are_written_and_the_capture_is_deleted(self):
        folder = store.new_folder(T0, self.base)
        self.put_capture(folder)
        _, paths = store.conclude(folder, None)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["netxray.json", "report.html", "report.md"])
        self.assertEqual({kind: path.name for kind, path in paths.items()},
                         {"json": "netxray.json", "html": "report.html", "markdown": "report.md"})
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(json.loads((folder / "netxray.json").read_text())["schema"], "mobster.netxray/1")

    def test_keep_capture_keeps_it_private_beside_the_reports(self):
        folder = store.new_folder(T0, self.base)
        self.put_capture(folder)
        store.conclude(folder, None, keep=True)
        self.assertEqual(stat.S_IMODE(store.capture_path(folder).stat().st_mode), 0o600)

    def test_a_capture_that_cant_be_read_is_deleted_all_the_same(self):
        folder = store.new_folder(T0, self.base)
        store.capture_path(folder).write_bytes(b"not a capture at all")
        with self.assertRaises(pcapng.PcapngError):
            store.conclude(folder, None)
        self.assertFalse(store.capture_path(folder).exists())
        self.assertEqual(list(folder.iterdir()), [])

    def test_a_capture_you_name_is_read_and_never_deleted(self):
        mine = self.base / "mine.pcapng"
        with open(mine, "wb") as stream:
            synthetic.flashlight(stream)
        folder = store.new_folder(T0, self.base)
        store.conclude(folder, None, source=mine)
        self.assertTrue(mine.exists())


if __name__ == "__main__":
    unittest.main()
