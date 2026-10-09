"""GET /api/apps/icon: App Store icons for apps found on the phone. The network is always mocked."""

import http.client
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from mobile_agent.app_icons import IMAGE_LIMIT, AppIcons, IconUnavailable
from mobile_agent.server import BoundedServer, make_handler

TOKEN = "t" * 43
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 64
ARTWORK = "https://is1-ssl.mzstatic.com/image/thumb/Purple/v4/icon.png/100x100bb.jpg"


class Response(io.BytesIO):
    def __init__(self, body, content_type):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Store:
    """The App Store lookup and its image CDN, as a urllib opener."""

    def __init__(self, results=None, image=JPEG, image_type="image/jpeg", artwork=ARTWORK):
        self.results = {"com.burbn.instagram": [{"bundleId": "com.burbn.instagram", "artworkUrl100": artwork,
                                                 "artworkUrl512": artwork.replace("100x100", "512x512")}]}
        if results is not None:
            self.results = results
        self.image, self.image_type = image, image_type
        self.urls = []
        self.failure = None

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.urls.append(url)
        if self.failure:
            raise self.failure
        if url.startswith("https://itunes.apple.com/lookup?bundleId="):
            bundle = url.rsplit("=", 1)[1]
            found = self.results.get(bundle, [])
            return Response(json.dumps({"resultCount": len(found), "results": found}).encode(), "text/javascript")
        return Response(self.image, self.image_type)


class IconTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mobster-icons-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "app-icons"
        self.store = Store()
        self.now = 1_000_000.0
        self.icons = AppIcons(self.root, opener=self.store, clock=lambda: self.now)

    def test_looks_up_only_the_bundle_id_then_caches_the_image(self):
        self.assertEqual(self.icons.get("com.burbn.instagram"), (JPEG, "image/jpeg"))
        self.assertEqual(self.store.urls, ["https://itunes.apple.com/lookup?bundleId=com.burbn.instagram", ARTWORK])
        self.assertEqual(self.icons.get("com.burbn.instagram"), (JPEG, "image/jpeg"))
        self.assertEqual(len(self.store.urls), 2)
        self.assertEqual(len(list(self.root.glob("*.jpg"))), 1)
        # A fresh process reads the same cache.
        self.assertEqual(AppIcons(self.root, opener=Mock(side_effect=AssertionError)).get("com.burbn.instagram")[1],
                         "image/jpeg")

    def test_an_app_the_store_does_not_know_is_remembered_for_a_week(self):
        self.assertIsNone(self.icons.get("com.example.inhouse"))
        self.assertIsNone(self.icons.get("com.example.inhouse"))
        self.assertEqual(len(self.store.urls), 1)
        self.now += 8 * 86_400
        self.assertIsNone(self.icons.get("com.example.inhouse"))
        self.assertEqual(len(self.store.urls), 2)

    def test_invalid_bundle_ids_never_reach_the_network(self):
        for bundle in ("", "noperiod", "../../etc/passwd", "com.a/b", "com.a b", "com.a&country=x", "a" * 300 + ".b", None):
            with self.subTest(bundle=bundle), self.assertRaises(ValueError):
                self.icons.get(bundle)
        self.assertEqual(self.store.urls, [])

    def test_only_listed_apps_are_looked_up(self):
        icons = AppIcons(self.root, opener=self.store, allowed=lambda: {"com.burbn.instagram"})
        self.assertIsNone(icons.get("com.example.private"))
        self.assertEqual(self.store.urls, [])
        self.assertIsNotNone(icons.get("com.burbn.instagram"))

    def test_rejects_wrong_types_oversized_images_and_foreign_artwork_hosts(self):
        cases = {
            "html": Store(image=b"<html>not an icon</html>", image_type="text/html"),
            "svg": Store(image=b"<svg xmlns='http://www.w3.org/2000/svg'/>", image_type="image/svg+xml"),
            "mismatch": Store(image=PNG, image_type="text/html"),
            "huge": Store(image=JPEG + b"\0" * IMAGE_LIMIT, image_type="image/jpeg"),
            "host": Store(artwork="https://example.com/icon.jpg"),
            "http": Store(artwork=ARTWORK.replace("https:", "http:")),
        }
        for name, store in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as root:
                icons = AppIcons(root, opener=store)
                self.assertIsNone(icons.get("com.burbn.instagram"))
                self.assertFalse(any(Path(root).glob("*.jpg")) or any(Path(root).glob("*.png")))
                self.assertFalse(any("example.com" in url or url.startswith("http:") for url in store.urls))
        png = AppIcons(self.root, opener=Store(image=PNG, image_type="image/png"))
        self.assertEqual(png.get("com.burbn.instagram"), (PNG, "image/png"))

    def test_temporary_failures_are_not_remembered_and_rate_limits_back_off(self):
        self.store.failure = urllib.error.URLError("offline")
        with self.assertRaises(IconUnavailable):
            self.icons.get("com.burbn.instagram")
        self.store.failure = urllib.error.HTTPError("u", 429, "Too many", {}, None)
        with self.assertRaises(IconUnavailable):
            self.icons.get("com.burbn.instagram")
        self.store.failure = None
        calls = len(self.store.urls)
        with self.assertRaises(IconUnavailable):
            self.icons.get("com.burbn.instagram")  # backing off: no request at all
        self.assertEqual(len(self.store.urls), calls)
        self.now += 61
        self.assertIsNotNone(self.icons.get("com.burbn.instagram"))

    def test_concurrent_requests_share_one_lookup(self):
        gate = threading.Event()
        original = self.store.__call__

        def slow(request, timeout=None):
            gate.wait(5)
            return original(request, timeout)

        self.icons.open = slow
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.icons.get("com.burbn.instagram"))) for _ in range(4)]
        for thread in threads:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(results, [(JPEG, "image/jpeg")] * 4)
        self.assertEqual(len(self.store.urls), 2)


class IconRouteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mobster-icons-")
        self.addCleanup(directory.cleanup)
        self.store = Store()
        runtime = Mock()
        runtime.config = SimpleNamespace(port=8765)
        runtime.icons = AppIcons(directory.name, opener=self.store, allowed=lambda: {"com.burbn.instagram", "com.example.inhouse"})
        self.server = BoundedServer(("127.0.0.1", 0), make_handler(runtime, TOKEN))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, query, token=TOKEN):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        suffix = f"&token={token}" if token else ""
        connection.request("GET", f"/api/apps/icon?{query}{suffix}", headers={"Host": "127.0.0.1:8765"})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def test_serves_the_icon_with_the_token_in_the_query_like_other_images(self):
        response, body = self.get("bundle=com.burbn.instagram")
        self.assertEqual((response.status, body), (200, JPEG))
        self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(self.get("bundle=com.burbn.instagram", token=None)[0].status, 401)

    def test_validation_and_missing_icons(self):
        self.assertEqual(self.get("bundle=..%2F..%2Fsecret")[0].status, 400)
        self.assertEqual(self.get("bundle=")[0].status, 400)
        self.assertEqual(self.get("other=com.burbn.instagram")[0].status, 400)
        self.assertEqual(self.get("bundle=com.example.inhouse")[0].status, 404)
        self.assertEqual(self.get("bundle=com.example.notlisted")[0].status, 404)
        self.store.failure = urllib.error.URLError("offline")
        response, body = self.get("bundle=com.burbn.instagram")
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(body)["code"], "icon_unavailable")
        self.assertNotIn("com.example.notlisted", " ".join(self.store.urls))


if __name__ == "__main__":
    unittest.main()
