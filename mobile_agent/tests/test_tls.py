import os
import tempfile
import unittest
from unittest.mock import patch

from mobile_agent import tls


class CaBundleTests(unittest.TestCase):
    def setUp(self):
        handle, self.bundle = tempfile.mkstemp(suffix=".pem")
        os.close(handle)
        self.addCleanup(os.remove, self.bundle)

    def test_the_frozen_app_always_uses_the_system_bundle(self):
        # The build machine's CA file may exist where the app is built; it never exists on a customer's Mac.
        environ = {}
        with patch.object(tls, "_own_paths_exist", return_value=True):
            self.assertEqual(tls.ensure_ca_bundle(environ, frozen=True, bundle=self.bundle), self.bundle)
        self.assertEqual(environ["SSL_CERT_FILE"], self.bundle)

    def test_a_python_install_keeps_its_own_file_when_it_exists(self):
        environ = {}
        with patch.object(tls, "_own_paths_exist", return_value=True):
            self.assertIsNone(tls.ensure_ca_bundle(environ, frozen=False, bundle=self.bundle))
        self.assertEqual(environ, {})

    def test_a_python_install_without_its_file_uses_the_system_bundle(self):
        # python.org's Python before "Install Certificates", or any Python frozen elsewhere.
        environ = {}
        with patch.object(tls, "_own_paths_exist", return_value=False):
            self.assertEqual(tls.ensure_ca_bundle(environ, frozen=False, bundle=self.bundle), self.bundle)

    def test_the_users_own_setting_wins(self):
        for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
            environ = {name: "/corp/roots"}
            self.assertIsNone(tls.ensure_ca_bundle(environ, frozen=True, bundle=self.bundle))
            self.assertEqual(environ, {name: "/corp/roots"})

    def test_no_system_bundle_changes_nothing(self):
        environ = {}
        self.assertIsNone(tls.ensure_ca_bundle(environ, frozen=True, bundle=self.bundle + ".missing"))
        self.assertEqual(environ, {})

    def test_the_report_counts_the_cas_that_load(self):
        with patch.dict(os.environ, {"SSL_CERT_FILE": tls.SYSTEM_BUNDLE}):
            report = tls.report()
        self.assertEqual(report["caFile"], tls.SYSTEM_BUNDLE)
        if os.path.isfile(tls.SYSTEM_BUNDLE):
            self.assertGreater(report["caCerts"], 50)


if __name__ == "__main__":
    unittest.main()
