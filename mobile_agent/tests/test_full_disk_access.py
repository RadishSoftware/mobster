import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import api_routes
from mobile_agent import full_disk_access as fda


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [os.chmod(p, 0o700) for p in self.dir.rglob("*")])

    def test_granted_missing_and_needed(self):
        readable = self.dir / "a.plist"
        readable.write_bytes(b"secret content")
        self.assertEqual(fda.check(readable), "granted")
        self.assertEqual(fda.check(self.dir / "absent"), "missing")
        refused = self.dir / "b.plist"
        refused.write_bytes(b"x")
        with mock.patch("builtins.open", side_effect=PermissionError(1, "Operation not permitted")):
            self.assertEqual(fda.check(refused), "needed")

    def test_a_folder_lists_names_only(self):
        (self.dir / "f").write_text("x")
        self.assertEqual(fda.check(self.dir), "granted")

    def test_never_reads_content(self):
        target = self.dir / "a.db"
        target.write_bytes(b"data")
        reads = []
        real = open

        def spy(path, mode="r", *a, **k):
            handle = real(path, mode, *a, **k)
            original = handle.read
            handle.read = lambda *x: reads.append(1) or original(*x)
            return handle
        with mock.patch("builtins.open", spy):
            fda.check(target)
        self.assertEqual(reads, [])

    def test_unknown_errors_fail_closed(self):
        (self.dir / "y").write_text("x")
        with mock.patch("builtins.open", side_effect=OSError(5, "Input/output error")):
            self.assertEqual(fda.check(self.dir / "y"), "needed")

    def test_overall_state_prefers_needed_then_granted(self):
        ok = self.dir / "ok"
        ok.write_text("x")
        gone = self.dir / "gone"
        self.assertEqual(fda.status([gone, ok])["state"], "granted")
        self.assertEqual(fda.status([gone])["state"], "missing")
        with mock.patch.object(fda, "check", side_effect=lambda p: "needed" if p == ok else "granted"):
            self.assertEqual(fda.status([gone, ok, self.dir])["state"], "needed")
        self.assertEqual(fda.status([])["state"], "missing")

    def test_route_answers_for_the_features_own_paths(self):
        api_routes.reset_for_tests()
        fda.register_route()
        fda.register_route()
        self.assertIn(("access", "GET", "/api/access/full-disk"),
                      [(r.owner, r.method, r.pattern) for r in api_routes._routes])
        with mock.patch.object(fda, "FEATURES", {"triggers": (self.dir / "gone",)}):
            request = mock.Mock(query={"feature": ["triggers"]})
            body = fda.route(request).value
        self.assertEqual((body["feature"], body["state"]), ("triggers", "missing"))
        with self.assertRaises(Exception) as caught:
            fda.route(mock.Mock(query={"feature": ["x"]}))
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(ValueError):
            fda.feature_status("nope")

    def tearDown(self):
        api_routes.reset_for_tests()


if __name__ == "__main__":
    unittest.main()
