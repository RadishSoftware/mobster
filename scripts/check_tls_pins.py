#!/usr/bin/env python3
"""Check the TLS pins against Mobster's live servers. Uses the network, so CI never runs it.

    mobile_agent/.venv/bin/python scripts/check_tls_pins.py           # check every pinned host
    mobile_agent/.venv/bin/python scripts/check_tls_pins.py --save    # also refresh mobile_agent/tests/fixtures/tls

For each host in pinning.PINNED_HOSTS it connects with the verifying context the agent uses, reads the chain
OpenSSL verified, and prints each certificate's pin-sha256, whether it's pinned, and when it expires. It exits 1
when a host's chain has no pinned key (the app would refuse it today) and warns when a pinned CA certificate a host
uses today expires within 180 days, so the next app update can add its successor first. The Mac app's shell has the
same check against macOS's chain: `cargo test --manifest-path desktop/src-tauri/Cargo.toml -- --ignored live`.
"""

import argparse
import datetime
import socket
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mobile_agent import pinning  # noqa: E402

FIXTURES = ROOT / "mobile_agent" / "tests" / "fixtures" / "tls"
WARN_DAYS = 180


def served_and_verified(host):
    with socket.create_connection((host, 443), timeout=15) as raw:
        with pinning.pinned_context().wrap_socket(raw, server_hostname=host) as sock:
            verified = pinning.verified_chain(sock)
            obj = getattr(sock, "_sslobj", None)
            unverified = getattr(sock, "get_unverified_chain", None) or getattr(obj, "get_unverified_chain", None)
            served = [c if isinstance(c, bytes) else c.public_bytes(ssl._ssl.ENCODING_DER) for c in unverified()]
            return served, verified, sock.version()


def describe(der):
    """(subject CN, notAfter) from DER, through ssl's own decoder."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as handle:
        handle.write(ssl.DER_cert_to_PEM_cert(der))
        handle.flush()
        info = ssl._ssl._test_decode_cert(handle.name)
    subject = dict(pair for rdn in info.get("subject", ()) for pair in rdn)
    expires = datetime.datetime.fromtimestamp(ssl.cert_time_to_seconds(info["notAfter"]), datetime.timezone.utc)
    return subject.get("commonName", "?"), expires


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--save", action="store_true", help="write each host's served chain to the test fixtures")
    args = parser.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)
    failed = False
    for host in sorted(pinning.PINNED_HOSTS):
        try:
            served, verified, version = served_and_verified(host)
        except OSError as error:
            print(f"{host}: couldn't connect ({error})")
            failed = True
            continue
        ok = pinning.chain_is_pinned(verified)
        print(f"{host}: {'pinned' if ok else 'NOT PINNED: the app refuses this server'} ({version})")
        for der in verified:
            pin = pinning.spki_pin(der)
            name, expires = describe(der)
            mark = pinning.PINS.get(pin)
            print(f"  {pin}  {name:<40} expires {expires:%Y-%m-%d}  {'pinned: ' + mark if mark else ''}")
            if mark and (expires - now).days < WARN_DAYS:
                print(f"  warning: {name} expires in {(expires - now).days} days; pin its successor in the next update")
        failed |= not ok
        if args.save and host in ("api.mobster.dev", "remote.mobster.dev"):
            path = FIXTURES / f"{host}.pem"
            path.write_text("".join(ssl.DER_cert_to_PEM_cert(der) for der in served))
            print(f"  saved {path.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
