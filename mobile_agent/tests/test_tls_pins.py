
import base64
import re
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from mobile_agent import pinning

FIXTURES = Path(__file__).parent / "fixtures" / "tls"
SHELL_PINS = Path(__file__).resolve().parents[2] / "desktop" / "src-tauri" / "src" / "pinning.rs"


def chain(name):
    text = (FIXTURES / f"{name}.pem").read_text()
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", text, re.S)
    return [base64.b64decode("".join(block.split())) for block in blocks]


class FakeTLS:

    def __init__(self, certificates, public=True):
        self.certificates, self.closed = certificates, False
        if public:
            self.get_verified_chain = lambda: list(self.certificates)

    def close(self):
        self.closed = True


class PinTests(unittest.TestCase):
    def test_the_spki_pins_match_openssls(self):
        api, remote = chain("api.mobster.dev"), chain("remote.mobster.dev")
        self.assertEqual(pinning.spki_pin(api[0]), "+ihSI4Febr9Oo/0GyEH2EL7Q3ulUJ3kjTrwkg9kQ5Nc=")
        self.assertEqual(pinning.spki_pin(api[1]), "LoMHBotttiDko50Gi13uXW71eIy7LAttI+rYT8wXF4w=")
        self.assertEqual(pinning.spki_pin(remote[1]), "kIdp6NNEd8wsugYyyIYFsi1ylMCED3hZbSR8ZFsa/A4=")
        for broken in (b"", b"\x30", b"\x30\x03\x02\x01", b"\x04\x00", api[1][:200]):
            with self.assertRaises(ValueError):
                pinning.spki_der(broken)

    def test_todays_chains_are_pinned_and_another_cas_is_not(self):
        self.assertTrue(pinning.chain_is_pinned(chain("api.mobster.dev")))
        self.assertTrue(pinning.chain_is_pinned(chain("remote.mobster.dev")))
        self.assertFalse(pinning.chain_is_pinned(chain("github.com")))
        self.assertFalse(pinning.chain_is_pinned(chain("api.mobster.dev")[:1]))
        self.assertFalse(pinning.chain_is_pinned([]))
        self.assertFalse(pinning.chain_is_pinned([b"not a certificate"]))

    def test_there_are_backup_pins_from_more_than_one_ca(self):
        names = " ".join(pinning.PINS.values())
        for ca in ("ISRG", "GTS", "SSL.com"):
            self.assertIn(ca, names)
        self.assertGreaterEqual(len(pinning.PINS), 2 + 5)
        for pin in pinning.PINS:
            self.assertEqual(len(base64.b64decode(pin)), 32, pin)

    def test_a_pinned_host_with_the_wrong_chain_is_refused(self):
        with self.assertRaises(pinning.PinMismatch) as caught:
            pinning.check_pins(FakeTLS(chain("github.com")), "remote.mobster.dev")
        self.assertTrue(str(caught.exception).startswith("Mobster couldn't verify its server (remote.mobster.dev)"))
        self.assertIsInstance(caught.exception, OSError)
        pinning.check_pins(FakeTLS(chain("remote.mobster.dev")), "remote.mobster.dev")
        pinning.check_pins(FakeTLS(chain("api.mobster.dev")), "API.mobster.dev.")

    def test_other_hosts_use_the_trust_store_alone(self):
        for host in ("github.com", "api.anthropic.com", "wwljtuapjuwfzzakujav.supabase.co", "docs.mobster.dev",
                     "mobster-remote-staging.example.workers.dev", "evilmobster.dev", "127.0.0.1"):
            self.assertFalse(pinning.is_pinned(host), host)
            pinning.check_pins(FakeTLS(chain("github.com")), host)

    def test_a_chain_it_cant_read_is_a_mismatch(self):
        with self.assertRaises(pinning.PinMismatch):
            pinning.check_pins(FakeTLS(chain("api.mobster.dev"), public=False), "api.mobster.dev")

    def test_python_312s_private_chain_is_read_too(self):
        class Certificate:
            def __init__(self, der):
                self.der = der

            def public_bytes(self, encoding):
                return self.der

        sock = FakeTLS([], public=False)
        sock._sslobj = type("SSLObject", (), {"get_verified_chain": lambda _: [
            Certificate(der) for der in chain("remote.mobster.dev")]})()
        pinning.check_pins(sock, "remote.mobster.dev")

    def test_wrap_closes_the_socket_on_a_mismatch(self):
        fake = FakeTLS(chain("github.com"))
        context = type("Context", (), {"wrap_socket": lambda self, sock, server_hostname: fake})()
        with patch.object(pinning, "pinned_context", return_value=context):
            with self.assertRaises(pinning.PinMismatch):
                pinning.wrap(object(), "remote.mobster.dev")
        self.assertTrue(fake.closed)

    def test_the_context_verifies_and_needs_tls_12(self):
        import ssl
        context = pinning.pinned_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_the_connection_checks_the_tunnelled_host(self):
        connection = pinning.PinnedHTTPSConnection("proxy.example", 8080)
        connection.set_tunnel("api.mobster.dev")
        seen = []

        def connect(self):
            self.sock = FakeTLS(chain("github.com"))

        with patch("http.client.HTTPSConnection.connect", connect), \
                patch.object(pinning, "check_pins", side_effect=lambda sock, host: seen.append(host)):
            connection.connect()
        self.assertEqual(seen, ["api.mobster.dev"])
        with patch("http.client.HTTPSConnection.connect", connect):
            with self.assertRaises(pinning.PinMismatch):
                connection.connect()

    def test_the_opener_pins_https(self):
        handlers = [type(handler) for handler in pinning.opener().handlers]
        self.assertIn(pinning.PinnedHTTPSHandler, handlers)
        self.assertNotIn(urllib.request.HTTPSHandler, handlers)

    @unittest.skipUnless(SHELL_PINS.is_file(), "the Mac app's shell isn't in this tree")
    def test_the_mac_apps_shell_pins_the_same_keys_and_hosts(self):
        source = SHELL_PINS.read_text()
        block = source.split("pub const PINS")[1].split("];")[0]
        self.assertEqual(set(re.findall(r'"([A-Za-z0-9+/]{43}=)"', block)), set(pinning.PINS))
        hosts = source.split("pub const PINNED_HOSTS")[1].split("];")[0]
        self.assertEqual(set(re.findall(r'"([a-z.]+)"', hosts)), set(pinning.PINNED_HOSTS))


if __name__ == "__main__":
    unittest.main()
