# Network X-ray (shipped 15 Oct 2026)

Network X-ray shows who every app on your iPhone talks to. Mobster records one phone's network metadata per process and writes a report like "In 60 s Weather contacted 14 domains at 9 companies; 3 of them are ads, analytics or attribution", with bytes sent and received per hostname. The user page is `docs/xray.mdx`; this note is the design.

It reads hostnames (DNS answers, TLS and QUIC server names, HTTP/1 Host headers), sizes and times. It never decrypts a payload, never keeps one in a report, and never shows an address. Every hostname, process name and company passes `secret_filter.redact` before it reaches a result (a token-looking label becomes the mask and still groups with itself).

History: a spike (14 Oct, PR #448) ran behind `MOBSTER_EXPERIMENTAL_NET_XRAY=1` with `python -m mobile_agent.netxray` and three capture sources. The ship (15 Oct) removed the flag, that entry, the pymobiledevice3 and rvictl sources (QA had used them), and the step alignment (`--run`, `--steps`: nothing called it). The port onto main (16 Oct) made the Mac app's recorder a section on the Devices page instead of a dialog, drew the company logos from `docs/brand/marks` on `LogoTile` (the agents' logos' one source), and kept the report's task on this Mac (below).

## Where it lives

| Module | What it is |
|---|---|
| `tool.py`, `capture.py` | find or build `mobster-pcap`; `Recording` runs it for one UDID (`--network` for Wi-Fi), stops it with SIGTERM then kill, and turns its exit code into a sentence |
| `target.py` | the phone: `pick(--device)` resolves with `devices.resolve`, refuses simulators and more than one phone with no name, follows the cable (`on_cable`) unless `--wifi`; `phone_names` reads the app list |
| `attribution.py` | process name -> app name (below) |
| `pcapng.py`, `packets.py`, `trackers.py`, `analyze.py` | the analyzer |
| `report.py`, `store.py`, `summary.py` | the three files, the folder and the words and tables |
| `cli.py` | `mobster xray network` (`devtools.py` creates the parser) |
| `job.py`, `register.py` | the track `netxray`: the job board and its routes |

## Process to app

pcapd names the executable (`Weather`, `nsurlsessiond`) and the effective executable it acted for. The analyzer attributes a connection to the effective process. `attribution.Names` maps an executable to the app's name on the phone from `ideviceinstaller list --user` and `--system` with `CFBundleExecutable` (`installed_apps.list_apps(..., network=True)` adds `-n` for a phone on Wi-Fi). pcapd's process name is 16 characters, so a longer executable arrives cut: it matches when exactly one executable starts with it. An executable two apps share names neither.

- A match is the app's name (`kind: app`).
- A name on the short list of iOS daemons (`SYSTEM_PROCESSES`) is **iOS**, its process name in `processes` (`kind: ios`). All daemons share one `iOS` row.
- Any other name stays the process name with the note "Mobster couldn't match this process to an app on the phone." (`kind: process`).
- No process name on the packet, or a connection nothing named: **Unattributed** (`kind: unattributed`) with its bytes, its connections and the sentence "Mobster couldn't tell which app sent this traffic, so it isn't counted for any app." A connection first seen without a name and later given one by a packet of its own is that app's. Never guessed.

Without the app list (no `ideviceinstaller`, a locked phone) every process keeps its name.

## Storage

`<user_data_dir>/xray/network/<YYYYmmdd-HHMMSS>/` (0700): `netxray.json`, `report.html`, `report.md`. `mobster-pcap` writes `capture.pcapng` (0600) in the same folder; `store.conclude` reads it, writes the reports, and deletes it in a `finally` unless `--keep-capture`. `--from FILE` reads a file of the person's own and never deletes it.

## The job

`POST /api/xray/network {device, seconds?, wifi?, key?}` -> 202 `{jobId}`; Mac app session only; plan checked at the start; 409 `xray_busy` for a phone that is recording; `key` makes a retry return the same job. `GET /api/xray/network/{id}` gives `state`, `elapsed`, `seconds`, `device`, `live` and, when done, `runId` and `early`. `POST .../stop` ends the recording; the analysis and the task still happen.

`job.Board` (one per Runtime, a `register_service`) keeps the last 20 jobs and one thread each. While recording it re-reads the growing file about every 2 s (more slowly when a pass takes longer) and publishes `summary.live`: `apps` (`name, kind, process, domains, companies, tracking, bytes`), `companies` (`name, category, categoryLabel, apps, domains, bytes`) and `totals` (`apps, domains, companies, tracking, bytes, packets, unattributed {bytes, connections}`). The app list is read on a second thread, so live rows show process names until it lands. When the time is up, or on Stop, it asks the tool to flush, reads the file once more and calls `Runtime.compose_result` (no phone lease, no model): app `any` (as a Home Screen run has), the answer line, the tables "Who your apps talked to" and "Every domain" (company columns are the new `brand` type) and a note with what was read and what wasn't seen. States: `recording`, `analyzing`, `done`, `failed` (with `error {message, code}`) and `stopped` (Stop before the phone sent anything, or shutdown). The tool is stopped on Stop, on shutdown and on any exception, and the capture is deleted in all of them.

## Wi-Fi

`mobster-pcap --network` uses only the Wi-Fi connection, even with a cable in. Over Wi-Fi it exits 5 with one sentence when the pcapd service descriptor is not `ssl_enabled`, before reading a byte, so captured metadata never crosses the network in clear. Unverified on a phone: whether iOS 26's pcapd offers an encrypted service over Wi-Fi at all. If it doesn't, Wi-Fi capture is refused for every phone.

## mobster-pcap

`mobile_agent/netxray/mobster-pcap.c` (MIT) is Mobster's pcapd client over libimobiledevice (LGPL-2.1). The CLI ships its source beside `tool.py`, which compiles it into the data folder's `helpers/` using Xcode command-line tools, `pkg-config` and Homebrew's libimobiledevice. `mobile_agent/wireless/pair.py` builds its adjacent `mobster-wifi-pair.c` the same way. Both prefer an existing Mac app tool when available. They never import or copy pymobiledevice3 (GPL-3.0-or-later).

```sh
mobster-pcap --udid <UDID> [--network] [--seconds N] [--max-mb N] [--snaplen N] --out FILE.pcapng
mobster-pcap --from-stream FILE [--snaplen N] --out FILE.pcapng     # tests: length-prefixed pcapd messages, not a phone
```

- It connects with `idevice_new_with_options` (USB or network), `lockdownd_client_new_with_handshake`, `lockdownd_start_service("com.apple.pcapd")` and `property_list_service_receive_plist_with_timeout`. Each message is a plist whose root is a DATA blob: a header, then the link-layer frame.
- The header, as read from the device and checked against pymobiledevice3's published field table (read only, no code copied): header length (u32), version (u8), frame length (u32), interface type (u8), unit (u16), io (u8, 0 in and 1 out), family (u32), link pre-length (u32), post-length (u32), interface name (16 bytes), pid (u32 little-endian), process (17 bytes), service class (u32), effective pid (u32 little-endian), effective process (17 bytes), seconds (u32), microseconds (u32). All big-endian except the two pids. That is 95 bytes; the frame starts at the header length. A message with a shorter header, a frame that runs past the message, or no frame is skipped.
- Link types: pre-length 14 is Ethernet (1), 0 is raw IP (101), 4 is BSD loopback (0, the cellular interface's 4-byte family). Any other pre-length drops the link header and writes raw IP. One interface block per distinct link type and interface name, at most 32.
- Each packet gets the comment above (the format the analyzer already reads), `epb_flags` with the direction, and the time the Mac received it. `--from-stream` uses the time in the header instead.
- Names from the phone are data: control characters become `?`, so a name can't break the comment.
- It stops, flushes and exits 0 on `SIGINT`, `SIGTERM`, `--seconds` or `--max-mb`. Exit codes: 2 usage, 3 no phone with that UDID, 4 not trusted or locked, 5 the phone refused the service (or, over `--network`, offered it unencrypted), 6 the file couldn't be written, 7 the stream file couldn't be read, 8 the connection broke. Each failure prints one line on stderr and removes the file it made. It creates the file exclusively with mode 0600 and never overwrites.
- Snap length: each packet keeps its first 256 bytes by default (`--snaplen`, 64 to 65,535), link header included, so a data segment is headers plus about 190 bytes of payload. The frame's real length is kept in the block, and the analyzer counts bytes from the IP header, so cutting doesn't change byte totals. Three kinds of packet keep more because the hostname is in them: a DNS message over UDP or TCP port 53 (up to 1,500 bytes, so the A and AAAA answers survive), a TCP segment that starts a TLS handshake record (up to 1,024 bytes, enough for the server name that sits near the start of a ClientHello even with a post-quantum key share), and a UDP datagram to port 443 that starts with a QUIC long header, type Initial (up to 1,500 bytes, because the analyzer decrypts the Initial to read the name). The tradeoff: without those allowances 256 bytes cuts an IPv6 ClientHello through the cipher list on a real iPhone, and cuts the QUIC Initial's ciphertext. A cleartext HTTP request keeps its first 256 bytes, which includes the Host header and the start of the path; the analyzer never returns the path, and the pcapng is private to the file's owner.

Notes:

- `-k` only prints metadata. `-P` is the flag that writes pcapng.
- macOS tcpdump on this Mac reads a file built with those block and option numbers and prints `(proc TikTok:312, eproc mDNSResponder:77, out)`. `test_netxray` checks this whenever `/usr/sbin/tcpdump` is present. Whether `tcpdump -i rvi0 -P` writes those blocks for iPhone traffic is unverified until the QA run.
- mobster-pcap stamps each packet with the Mac's clock as it arrives.
- pcapd's Ethernet frames carry a made-up header that says IPv4 even for IPv6. The decoder reads the version from the IP header.

## What the analyzer does

- `pcapng.py` reads the section header, interface descriptions (link type, name, timestamp resolution), enhanced packets with their comments, flags (direction) and Apple process options, and Apple's process blocks. It reads either byte order. It refuses classic pcap and anything else. It keeps the packets before a cut at the end of the file and sets `truncated`.
- `packets.py` decodes Ethernet (with VLAN tags), raw IP, BSD loopback and Apple PKTAP; IPv4 (first fragments only) and IPv6 (with extension headers); TCP and UDP. On top of that it reads:
  - DNS responses: A, AAAA and CNAME, with compression. Every address in an answer maps to the question's name, which is the name the app asked for.
  - TLS ClientHello SNI, with records joined across segments.
  - HTTP/1 Host. The path and every other header are never returned.
  - QUIC v1 Initials. It decrypts the client Initial with the keys anyone can derive from the packet (RFC 9001 section 5, checked against the RFC's test vector) and reads the ClientHello's server name, joining CRYPTO frames across up to 4 Initials. This uses the `cryptography` package when it is installed. Without it, QUIC is labelled QUIC and the name comes from DNS.
- `analyze.py` tracks connections (TCP by SYN, FIN, RST and idle time; UDP by idle time). It decides which side is the phone from the capture's direction flag, the SYN, service ports, then local addresses. It attributes each connection to the effective process, so DNS from mDNSResponder and downloads by nsurlsessiond count for the app they were for. The `via` field says when another process carried the traffic. `result()` groups processes by the label `attribution.Names` gives them. A connection's hostname is its SNI, Host or QUIC name, else the DNS answer for its server address.
- `trackers.py` is a list of 150 domain suffixes with company and category, written for Mobster from the SDK vendors’ own documentation of their hosts and, on 15 Oct 2026, from the hostnames two real captures on the test device left unnamed. It has 91 ad, analytics, attribution, messaging and crash entries, plus Apple, Google, Meta, Amazon, ByteDance and hosting providers. The longest suffix wins. It doesn't copy DuckDuckGo Tracker Radar (CC BY-NC-SA) or Disconnect (GPL). Registrable domains use a short list of two-label suffixes, not the Public Suffix List.

## Privacy defaults (fail closed)

- The capture file is the one place bytes of a packet exist. `mobster-pcap` writes each packet's headers and the first 256 bytes only (see Snap length), to the file you named, readable by you alone. The analyzer reads the file and keeps no payload. Delete the capture when you have the report.
- Client bytes on a connection are held only until its hostname is read, at most 16 KB and 8 segments. Then they are dropped. A test checks every connection's buffer is gone after analysis.
- The report names no address: not the phone's, not the router's, not a server's. A connection with no hostname counts as "unnamed" with its bytes.
- Traffic to the local network (RFC 1918, CGNAT, link-local, multicast, unique-local) is one local total with no names. mDNS names like "Sam's MacBook" never reach a report. DNS answers from a local resolver are still read to name connections.
- Hostnames are data. Only letters, digits, dots, hyphens and underscores pass, and a name that is an address is dropped. Process names are escaped in HTML and Markdown.
- `Recording` refuses a file that exists, a name that isn't `.pcapng`, a UDID that isn't one, a length outside 5 s to 1 h and a missing tool before anything runs. The routes are the Mac app's only; Network X-ray is not an agent or MCP tool, so no hostname reaches a model.
- The report's task has the run field `stayOnMac` (`harness_api.stays_on_mac`, which reads the run's own field, so a run from before a restart still counts). The share link refuses it (403 `stays_on_mac`), destinations never get it (`outlets.service.subscribers`), a feedback report leaves it out (`feedback.bundle.last_run`), and `GET /api/runs` and `/api/runs/{id}` answer "Not found" to a caller without the Mac app's session (an agent over MCP, Mobster Remote, a script). `test_netxray_egress` holds each refusal and the ordinary result that still goes.

## Measured

- Analyzer throughput, 14 Oct 2026, Apple M5 Max, macOS 26.5.1, Python 3.12.2, under `nice -n 10` with load average 18 to 26: a synthetic 200,002-packet, 162 MB pcapng (`tests/netxray_synthetic.py`, `bulk`: TLS, QUIC, DNS from 6 processes) analyzed in 1.58 s, 1.65 s and 1.70 s. That is about 121,000 packets/s at the median and 127,000 at best. At that rate a 100,000-packet capture takes under a second.

## Not verified

- Measured on the test device (iPhone 15 Pro, iOS 26.0.1) over the cable on 15 Oct 2026, from #448: the pcapd layout holds (98.3% and 98.7% of packets carry a process name; 1 and 2 connections unattributed), the service connection over USB is SSL, and over Wi-Fi iOS refuses `com.apple.pcapd` (lockdown error -27). `mobster-pcap` took 0.58 to 0.63 s of CPU per 90 s and 6.5 to 8.8 MB. Not measured since: a capture with the list as it now stands (its matching numbers in `docs/xray.mdx` are in sample), and the Devices section with a phone attached.
- Capture over Wi-Fi through the CoreDevice tunnel's `com.apple.pcapd.shim.remote`: a follow-up.
- Encrypted DNS and iCloud Private Relay hide DNS answers. TLS and QUIC server names still name most connections. Encrypted Client Hello would show only the outer name.

## Owner decisions

1. **Capture path.** Decided 14 Oct (D3): our own pcapd client over libimobiledevice (LGPL-2.1), `mobster-pcap`, is the only source. Mobster code never imports pymobiledevice3 (`tests/test_no_pymobiledevice3.py`) and the Mac app never bundles it (`scripts/check_no_pymobiledevice3.py` stops the build).
2. **Privacy copy.** "Metadata only: hostnames, sizes and times. Nothing is decrypted and no payload is kept. Addresses and local-network names never appear in a report."
3. **Server addresses.** They stay out of reports, including for unnamed connections.
