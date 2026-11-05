import os
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from mobile_agent.netxray import analyze, pcapng, tool
from mobile_agent.netxray import packets as wire
from mobile_agent.tests.netxray_synthetic import (
    PHONE,
    PHONE6,
    client_hello,
    dns_response,
    ethernet,
    ip,
    server,
    tcp,
    tls_record,
    udp,
)

SOURCE = tool.SOURCE
T0 = 1_792_000_000
SERVER6 = bytes.fromhex("20010db8000000000000000000000099")
RESOLVER6 = bytes.fromhex("20010db8000000000000000000000001")


def message(frame, *, process, pid, effective=None, epid=None, interface="en0", pre=14, out=True, svc=0, at=0.0,
            header_length=95):
    effective, epid = effective or process, epid if epid is not None else pid
    head = struct.pack(">IBIBHBIII", header_length, 2, len(frame), 6, 0, 1 if out else 0, 2, pre, 0)
    head += interface.encode().ljust(16, b"\0")
    head += struct.pack("<I", pid) + process.encode().ljust(17, b"\0")
    head += struct.pack(">I", svc)
    head += struct.pack("<I", epid) + effective.encode().ljust(17, b"\0")
    head += struct.pack(">II", T0 + int(at), int((at % 1) * 1_000_000))
    assert len(head) == 95
    return head + b"\0" * (header_length - 95) + frame


def stream(*messages):
    return b"".join(struct.pack(">I", len(item)) + item for item in messages)


class Fixture:
    def build(self):
        weather, safari = ("Weather", 412), ("Safari", 977)
        hello = tls_record(client_hello("api.weather.example"))
        big_hello = tls_record(client_hello("big-hello.example", padding=600))
        bulk = tcp(50001, 443, 0x18, b"\x17\x03\x03\x03\xc0" + b"A" * 1000, seq=2000)   # 1,005 payload bytes
        dns6 = ip(RESOLVER6, PHONE6, wire.UDP, udp(53, 51000, dns_response("cdn.weather.example", [server(30)])))
        no_name = tcp(50002, 443, 0x18, b"\x17\x03\x03\x00\x20" + b"B" * 32, seq=3000)
        items = [
            message(ethernet(ip(PHONE, server(10), wire.TCP, tcp(50000, 443, 0x02))), process=weather[0],
                    pid=weather[1], at=0.0),
            message(ethernet(ip(PHONE, server(10), wire.TCP, tcp(50000, 443, 0x18, hello, seq=1001))),
                    process=weather[0], pid=weather[1], at=0.1),
            message(ethernet(ip(PHONE, server(11), wire.TCP, tcp(50003, 443, 0x18, big_hello))),
                    process=weather[0], pid=weather[1], at=0.2),
            message(ethernet(ip(PHONE, server(10), wire.TCP, bulk)), process=weather[0], pid=weather[1], at=0.3),
            message(ethernet(dns6), process="mDNSResponder", pid=77, effective=weather[0], epid=weather[1], out=False,
                    at=0.4),
            message(ethernet(ip(PHONE, server(30), wire.TCP, tcp(50002, 443, 0x02))), process=weather[0],
                    pid=weather[1], at=0.5),
            message(ethernet(ip(PHONE, server(30), wire.TCP, no_name)), process=weather[0], pid=weather[1], at=0.6),
            message(ip(PHONE, server(40), wire.TCP, tcp(50010, 443, 0x18, tls_record(client_hello("www.example.org")))),
                    process=safari[0], pid=safari[1], interface="pdp_ip0", pre=0, at=1.0),
            message(b"\x02\x00\x00\x00" + ip(PHONE, server(41), wire.TCP,
                                             tcp(50011, 443, 0x18, tls_record(client_hello("m.example.org")))),
                    process=safari[0], pid=safari[1], interface="pdp_ip1", pre=4, at=1.1),
            b"too short to be a pcapd message",
        ]
        return stream(*items)


class MobsterPcapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp())
        cls.addClassCleanup(shutil.rmtree, cls.folder, True)
        clang, pkg = shutil.which("clang"), shutil.which("pkg-config") or next(
            (path for path in ("/opt/homebrew/bin/pkg-config", "/usr/local/bin/pkg-config")
             if os.access(path, os.X_OK)),
            None)
        if not clang or not pkg:
            raise unittest.SkipTest("clang or pkg-config isn't installed")
        try:
            flags = subprocess.run([pkg, "--cflags", "--libs", *tool.LIBRARIES], capture_output=True, text=True,
                                   timeout=30, check=True).stdout.split()
        except (OSError, subprocess.SubprocessError):
            raise unittest.SkipTest("libimobiledevice isn't installed (brew install libimobiledevice)") from None
        cls.binary = cls.folder / "mobster-pcap"
        built = subprocess.run([clang, "-Wall", "-O2", str(SOURCE), "-o", str(cls.binary), *flags],
                               capture_output=True, text=True, timeout=180)
        if built.returncode != 0:
            raise unittest.SkipTest("mobster-pcap didn't compile here: " + built.stderr[-300:])

    def run_tool(self, data, *options, name="capture.pcapng"):
        source = self.folder / "stream.bin"
        source.write_bytes(data)
        out = self.folder / name
        out.unlink(missing_ok=True)
        done = subprocess.run([str(self.binary), "--from-stream", str(source), *options, "--out", str(out)],
                              capture_output=True, text=True, timeout=60)
        return done, out

    def packets(self, path):
        with open(path, "rb") as stream_:
            reader = pcapng.Reader(stream_)
            return list(reader), reader

    def test_stream_becomes_a_pcapng_the_analyzer_reads_unchanged(self):
        done, out = self.run_tool(Fixture().build())
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        result = analyze.analyze_file(out)
        names = {item["name"] for item in result["apps"]}
        self.assertEqual(names, {"Weather", "Safari"})
        weather = next(item for item in result["apps"] if item["name"] == "Weather")
        safari = next(item for item in result["apps"] if item["name"] == "Safari")
        self.assertEqual({d["host"] for d in weather["domains"]},
                         {"api.weather.example", "big-hello.example", "cdn.weather.example"})
        self.assertEqual({d["host"] for d in safari["domains"]}, {"www.example.org", "m.example.org"})
        self.assertFalse(result["capture"]["truncated"])
        cdn = next(d for d in weather["domains"] if d["host"] == "cdn.weather.example")
        self.assertGreaterEqual(cdn["up"], 40 + 37)

    def test_link_types_follow_the_pre_length_and_the_process_comes_from_the_header(self):
        done, out = self.run_tool(Fixture().build())
        self.assertEqual(done.returncode, 0)
        found, reader = self.packets(out)
        self.assertEqual({(i.linktype, i.name) for i in reader.interfaces},
                         {(1, "en0"), (101, "pdp_ip0"), (0, "pdp_ip1")})
        self.assertEqual(len(found), 9)   # the short message was skipped, nothing else
        by_process = {(p.process, p.pid, p.eprocess, p.epid) for p in found}
        self.assertIn(("mDNSResponder", 77, "Weather", 412), by_process)
        self.assertIn(("Safari", 977, "Safari", 977), by_process)
        self.assertEqual([p.direction for p in found].count("in"), 1)
        self.assertEqual(found[0].time, T0 + 0.0)
        self.assertAlmostEqual(found[-1].time - T0, 1.1, places=6)
        self.assertEqual(sum(1 for p in found if p.interface == "pdp_ip0"), 1)

    def test_payload_beyond_snaplen_is_cut_and_a_hostname_is_not(self):
        done, out = self.run_tool(Fixture().build())
        found, _ = self.packets(out)
        bulk = next(p for p in found if p.length > 1000)
        self.assertEqual(len(bulk.data), 256)                  # snaplen, link header included
        self.assertEqual(bulk.length, 14 + 20 + 20 + 5 + 1000)  # the wire length is kept
        self.assertLess(bulk.data.count(b"A"), 256 - 14 - 20 - 20 - 5 + 1)
        big_hello = next(p for p in found if b"big-hello.example" in bytes(p.data))
        self.assertGreater(len(big_hello.data), 256)           # a ClientHello keeps more than snaplen
        dns = next(p for p in found if p.process == "mDNSResponder")
        self.assertEqual(len(dns.data), dns.length)            # a DNS message is whole

    def test_a_smaller_snaplen_cuts_more(self):
        done, out = self.run_tool(Fixture().build(), "--snaplen", "64", name="small.pcapng")
        self.assertEqual(done.returncode, 0)
        found, _ = self.packets(out)
        plain = [p for p in found if p.process == "Weather" and b"\x17\x03\x03" in bytes(p.data)[:80]]
        self.assertTrue(plain)
        self.assertTrue(all(len(p.data) <= 64 for p in plain))

    def test_names_from_the_phone_are_data(self):
        evil = message(ethernet(ip(PHONE, server(50), wire.TCP, tcp(50020, 443, 0x02))), process="a, EPID: 1\nx", pid=5,
                       at=2.0)
        done, out = self.run_tool(stream(evil))
        self.assertEqual(done.returncode, 0)
        found, _ = self.packets(out)
        self.assertEqual(len(found), 1)
        self.assertNotIn("\n", found[0].process or "")

    def test_never_overwrites_and_writes_privately(self):
        done, out = self.run_tool(Fixture().build())
        self.assertEqual(done.returncode, 0)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        before = out.read_bytes()
        again = subprocess.run([str(self.binary), "--from-stream", str(self.folder / "stream.bin"), "--out", str(out)],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(again.returncode, 6)
        self.assertIn("can't create", again.stderr)
        self.assertEqual(out.read_bytes(), before)

    def test_a_cut_stream_exits_7_and_leaves_no_file(self):
        done, out = self.run_tool(Fixture().build()[:-5])
        self.assertEqual(done.returncode, 7)
        self.assertIn("cut short", done.stderr)
        self.assertFalse(out.exists())

    def test_usage_errors_exit_2(self):
        for args in ([], ["--out", "x.pcapng"], ["--udid", "short", "--out", "x.pcapng"],
                     ["--from-stream", "a", "--udid", "00008130-000A1B2C3D4E5F60", "--out", "x.pcapng"],
                     ["--from-stream", "a", "--out", "x.pcapng", "--snaplen", "4"]):
            with self.subTest(args=args):
                done = subprocess.run([str(self.binary), *args], capture_output=True, text=True, timeout=30,
                                      cwd=self.folder)
                self.assertEqual(done.returncode, 2)
                self.assertIn("usage", done.stderr)

    def test_max_mb_stops_with_a_readable_file(self):
        frame = ethernet(ip(PHONE, server(60), wire.TCP, tcp(50030, 443, 0x18, b"\x17\x03\x03\x05\x00" + b"C" * 1400)))
        data = stream(*[message(frame, process="Weather", pid=412, at=i * 0.001) for i in range(1500)])
        done, out = self.run_tool(data, "--snaplen", "1600", "--max-mb", "1", name="limited.pcapng")
        self.assertEqual(done.returncode, 0)
        self.assertLessEqual(out.stat().st_size, 1024 * 1024)
        found, reader = self.packets(out)
        self.assertFalse(reader.truncated)
        self.assertTrue(0 < len(found) < 1500)

    def test_the_source_never_imports_pymobiledevice3(self):
        self.assertNotIn("pymobiledevice3", SOURCE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
