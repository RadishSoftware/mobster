import io
import json
import unittest

from mobile_agent import links, update_check


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


APP_RELEASE = ("Mobster_0.4.0_Apple-Silicon.dmg", "Mobster.dmg", "Mobster.app.tar.gz", "Mobster.app.tar.gz.sig",
               "SHA256SUMS", "latest.json")


def release(tag="v0.5.0", assets=APP_RELEASE, **extra):
    body = {"tag_name": tag, "published_at": "2026-10-20T00:00:00Z", "draft": False, "prerelease": False,
            "assets": [{"name": name} for name in assets], **extra}
    return body


NOW = 1792670400


class ReleasesFeedTests(unittest.TestCase):
    def fetch(self, body):
        asked = []

        def opener(request, timeout):
            asked.append(request.full_url)
            return Response(json.dumps(body).encode())
        return update_check.latest_release(opener=opener), asked

    def test_it_asks_the_releases_repository_not_the_engine_repository(self):
        found, asked = self.fetch(release())
        self.assertEqual(asked, ["https://api.github.com/repos/RadishSoftware/mobster-releases/releases/latest"])
        self.assertNotIn("/repos/RadishSoftware/mobster/", links.RELEASES_API)
        self.assertEqual(found, {"version": "0.5.0", "publishedAt": "2026-10-20T00:00:00Z"})

    def test_a_release_without_the_app_is_never_offered(self):
        self.assertIsNone(self.fetch(release(assets=()))[0])
        self.assertIsNone(self.fetch(release(assets=("mobster-macos-arm64.tar.gz",
                                                     "mobster-macos-arm64.tar.gz.sha256")))[0])
        self.assertIsNone(self.fetch({k: v for k, v in release().items() if k != "assets"})[0])
        self.assertIsNone(self.fetch(release() | {"assets": "not a list"})[0])
        self.assertIsNone(self.fetch(release(draft=True))[0])
        self.assertIsNone(self.fetch(release(prerelease=True))[0])

    def test_the_040_app_release_points_a_cli_user_at_the_app_never_at_a_cli_update(self):
        found = self.fetch(release(tag="v0.4.0"))[0]
        self.assertEqual(found["version"], "0.4.0")
        line = update_check.notice("0.3.0", {"latest": found}, now=NOW)
        self.assertEqual(line, "Mobster 0.4.0 is out, released 2 days ago, in Mobster for Mac (you have 0.3.0). "
                               "Download it at https://mobster.dev/download")
        self.assertNotIn("mobster update", line)
        self.assertNotIn("install.sh", line)


if __name__ == "__main__":
    unittest.main()
