import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mobile_agent import artifacts, links
from mobile_agent import result_blocks as rb
from mobile_agent.tests.test_server_engine import MESSAGES, Base

JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")

ENVELOPE = rb.envelope([
    rb.table(["Creator", {"label": "Likes", "type": "number"}, {"label": "Link", "type": "link"}],
             [["<script>alert(1)</script>", 1200, "https://example.com/p/1"], ["@noodlelab", 30, None]],
             title="Saved posts",
             id="posts"),
    rb.review_queue([{"id": "c1", "to": "Sam Rivera", "handle": "@samrivera", "app": "Instagram",
                      "lastMessage": "Sam, are we still on?", "draft": "Yes, see you, Sam."}], title="Queue",
                    id="queue"),
    rb.evidence("The price on Plans is wrong.", frame_id="f0000000001", quote="Annual $4,999", verdict="fail", id="ev"),
    rb.checklist([{"label": "Home", "status": "pass", "frameId": "f0000000001"}], id="checks"),
])
RUN = {"id": "abcdef012345", "goal": "Check my build", "appName": "Firefox", "finishedAt": 1791950000000,
       "summary": {"answer": "One screen looks broken.", "data": "One screen looks broken.\n\nPlans shows $4,999.",
                   "blocks": ENVELOPE, "proof": [{"quote": "Annual $4,999", "app": "Firefox", "screen": "Plans"}]}}


def body_of(page):
    return page.split("</style>", 1)[1]


class DocumentTests(unittest.TestCase):
    def test_a_self_contained_escaped_page(self):
        page = artifacts.document(RUN, frames={"f0000000001": JPEG}.get)
        body = body_of(page)
        self.assertIn("default-src 'none'", page)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotRegex(page, r'(src|href)="http://|url\(http')
        self.assertNotIn('src="https', page)
        self.assertIn('href="https://example.com/p/1"', body)
        self.assertIn(f'<p class="made">Made with <a href="{links.MADE_WITH_SHARE}">Mobster</a> on an iPhone.', body)
        self.assertIn("One screen looks broken.", body)
        self.assertIn("Plans shows $4,999.", body)
        self.assertIn("data:image/jpeg;base64,", body)
        self.assertIn("Sources, as the screen showed them", body)
        self.assertIn("Nothing here was sent.", body)
        self.assertIn("Mobster Sans", page)
        self.assertIn("1,200", body)

    def test_people_masked_and_screens_left_out(self):
        page = body_of(artifacts.document(RUN, frames={"f0000000001": JPEG}.get, people="mask", screens=False))
        for name in ("Sam", "samrivera", "noodlelab"):
            self.assertNotIn(name, page)
        self.assertIn("Person 1", page)
        self.assertIn("Person 2, are we still on?", page)
        self.assertEqual(artifacts.masker(ENVELOPE)("ask @stranger and Sam"), "ask @someone and Person 2")
        self.assertNotIn("data:image/jpeg", page)
        self.assertNotIn("Sam", artifacts.markdown(RUN, people="mask"))

    def test_one_card(self):
        page = body_of(artifacts.document(RUN, blocks=["posts"], card=True))
        self.assertIn('data-block="table"', page)
        self.assertNotIn('data-block="review_queue"', page)
        self.assertNotIn("One screen looks broken.", page)
        self.assertEqual(page.count('<p class="credit">'), 1)
        self.assertTrue(page.rstrip().endswith('mobster.dev</p></body></html>'), page[-120:])
        self.assertNotIn('class="credit"', body_of(artifacts.document(RUN)))

    def test_markdown(self):
        text = artifacts.markdown(RUN)
        self.assertTrue(text.startswith("# Check my build\n\nOne screen looks broken.\n"))
        self.assertIn("| Creator | Likes | Link |", text)


def fake_helper(folder, output=b"%PDF-1.4 fake", code=0, said=""):
    path = Path(folder) / "pdf"
    path.write_text(f"#!/bin/sh\nprintf '%s' \"{said}\" >&2\n"
                    f"[ {code} -eq 0 ] && printf '%s' '{output.decode()}' > \"$2\"\nexit {code}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class RouteTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-000000000000"
        self.downloads = tempfile.TemporaryDirectory()
        self.addCleanup(self.downloads.cleanup)

    def finished(self, runtime):
        run = runtime.create(MESSAGES["id"], "Check my build", "live")
        run.finish({"event": "result", "status": "completed", "answer": "One screen looks broken.", "blocks": ENVELOPE})
        if run.lease:
            run.lease.close()
            run.lease = None
        runtime.active_runs.clear()
        return run

    def save(self, runtime, run, body):
        with mock.patch("mobile_agent.downloads._save_new",
                        side_effect=lambda _d, stem, ext, data: self._save(stem, ext, data)):
            return self.request(runtime, "POST", f"/api/runs/{run.id}/artifact", body)

    def _save(self, stem, ext, data):
        target = Path(self.downloads.name) / f"{stem}.{ext}"
        target.write_bytes(data)
        return {"name": target.name, "path": str(target)}

    def test_pdf_png_and_markdown(self):
        runtime = self.runtime()
        run = self.finished(runtime)
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch("mobile_agent.native_helpers.find", return_value=fake_helper(folder)):
            status, body, _ = self.save(runtime, run, {"format": "pdf"})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["name"], "Check my build.pdf")
            self.assertEqual(Path(body["path"]).read_bytes(), b"%PDF-1.4 fake")
            status, body, _ = self.save(runtime, run, {"format": "png", "blocks": ["posts"]})
            self.assertEqual((status, body["name"]), (200, "Saved posts.png"))
        status, body, _ = self.save(runtime, run, {"format": "md"})
        self.assertIn("| Creator |", Path(body["path"]).read_text())

    def test_refusals(self):
        runtime = self.runtime()
        run = self.finished(runtime)
        for request, code in (({"format": "png"}, 400), ({"format": "docx"}, 400),
                              ({"format": "pdf", "blocks": ["nope"]}, 404),
                              ({"format": "png", "blocks": ["a", "b"]}, 400)):
            with self.subTest(request=request):
                self.assertEqual(self.save(runtime, run, request)[0], code)
        with tempfile.TemporaryDirectory() as folder, mock.patch(
                "mobile_agent.native_helpers.find",
                return_value=fake_helper(folder, code=1, said="Mobster couldn't make the PDF.")):
            status, body, _ = self.save(runtime, run, {"format": "pdf"})
            self.assertEqual((status, body["code"], body["error"]),
                             (503, "artifact_unavailable", "Mobster couldn't make the PDF."))


@unittest.skipUnless(os.environ.get("MOBSTER_ARTIFACT_WEBKIT") == "1",
                     "builds the WebKit helper; MOBSTER_ARTIFACT_WEBKIT=1")
class WebKitTests(unittest.TestCase):
    def test_pdf_and_png(self):
        from mobile_agent import native_helpers
        helper = native_helpers.find("pdf") or native_helpers.build("pdf")
        long = {**RUN,
                "summary": {**RUN["summary"], "blocks": rb.envelope([rb.table(["n"], [[n] for n in range(150)])])}}
        pdf = artifacts.render(artifacts.document(long), "pdf", title="Check", helper=helper)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreaterEqual(pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page "), 1)
        png = artifacts.render(artifacts.document(RUN, blocks=["posts"], card=True), "png", helper=helper)
        self.assertTrue(png.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
