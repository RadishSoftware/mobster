"""verify/report.py: the self-contained report (escaping, CSP, no external URLs, the numbered expectations beside
the frame) and the overlay frame (thin outlines, one numbered pill per asserted element, nothing over the screen's
bottom). Offline."""

from pathlib import Path
import re
import tempfile
import unittest

from PIL import Image

from mobile_agent.tests.test_verify_fixtures import app, node, paywall
from mobile_agent.verify.assertions import evaluate_on, parse_assertion
from mobile_agent.verify.report import CSP, MAX_PILLS, draw_overlay, pill_numbers, render_html, write_report
from mobile_agent.verify.tree import parse_tree

EVIL = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def result_with(run_dir, **changes):
    frames = run_dir / "frames"
    frames.mkdir(exist_ok=True)
    for name in ("01-launch.jpg", "02-step.jpg", "03-verdict.jpg", "04-verdict-ax.jpg"):
        Image.new("RGB", (402, 874), (200, 200, 210)).save(frames / name, "JPEG")
    result = {
        "schema": "mobster.verify/1", "run_id": "20261001-101500-3f9a", "verdict": "failed", "exit_code": 1,
        "summary": f"text \"{EVIL}\" failed: not on screen", "reason": {"class": "assertion",
                                                                       "message": "1 of 2 expectations failed",
                                                                       "fix": None},
        "mode": "smart",
        "check": {"name": f"Paywall {EVIL}", "steps": ["Open it"], "expect": [{"text": EVIL}], "source": None},
        "app": {"bundle_id": "dev.mobster.daybreak", "name": f"Day{EVIL}", "version": "1.0 (1)", "path": None},
        "device": {"name": "Mobster · iPhone 17 Pro · iOS 26.4", "udid": "U", "type": "iPhone 17 Pro",
                   "runtime": "iOS 26.4"},
        "assertions": [{"index": 0, "assertion": {"text": EVIL}, "text": f'text "{EVIL}"', "ok": False,
                        "observed": f'not on screen; closest: "{EVIL}"', "matches": [], "frame": "frames/03-verdict.jpg"},
                       {"index": 1, "assertion": {"text": "Plans"}, "text": 'text "Plans"', "ok": True,
                        "observed": 'found in text "Plans"', "matches": [], "frame": "frames/03-verdict.jpg"}],
        "baseline": {"taken": True, "held": [False, False]}, "stable": True,
        "steps": [{"index": 1, "op": "TAP", "text": f"Tapped {EVIL}", "target": {"id": None, "label": EVIL,
                                                                                 "role": None},
                   "typed": None, "changed": True, "frame": "frames/02-step.jpg", "at_ms": 3120}],
        "agent": {"status": "completed", "note": f"I tapped {EVIL}", "model": "gpt-5.6-sol", "turns": 4},
        "frames": ["frames/01-launch.jpg", "frames/02-step.jpg", "frames/03-verdict.jpg", "frames/04-verdict-ax.jpg"],
        "proof": [], "alert": None, "report": str(run_dir / "report.html"), "draft_check": str(run_dir / "check.yaml"),
        "run_dir": str(run_dir), "repro": f"mobster verify --check {run_dir / 'check.yaml'}", "seconds": 12.4,
        "timing": {"simulator": 0.4, "install": 2.1, "launch": 1.3, "flow": 6.8, "assert": 1.2}, "cost_usd": 0.031,
        "mobster": {"version": "0.2.0"}}
    result.update(changes)
    return result


class ReportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.run_dir = Path(folder.name)

    def test_every_app_and_model_string_is_escaped(self):
        html = render_html(result_with(self.run_dir), self.run_dir)
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("onerror=alert", html.replace("onerror=alert(1)&gt;", ""))
        self.assertNotIn(EVIL, html)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", html)

    def test_the_csp_and_no_external_urls(self):
        html = render_html(result_with(self.run_dir), self.run_dir)
        self.assertIn(f'<meta http-equiv="Content-Security-Policy" content="{CSP}">', html)
        self.assertEqual(CSP, "default-src 'none'; img-src data:; style-src 'unsafe-inline'")
        for attribute in re.findall(r'(?:src|href)="([^"]*)"', html):
            self.assertTrue(attribute.startswith("data:image/jpeg;base64,"), attribute[:40])
        self.assertIsNone(re.search(r"<(link|script|iframe|object|a)\b", html))
        self.assertNotRegex(html, r"url\(")

    def test_the_contents_in_order(self):
        html = render_html(result_with(self.run_dir), self.run_dir)
        order = ['class="verdict fail"', ">Expectations<", ">The verdict frame<", ">Steps<", ">Run<",
                 ">Run it again<", ">The model's note (not the verdict)<"]
        positions = [html.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("prefers-color-scheme: dark", html)
        self.assertIn("@media print", html)
        self.assertIn("$0.0310", html)
        self.assertIn("1 of 2 expectations failed", html)
        self.assertEqual(html.count("<img"), 4)  # overlay, plain verdict, launch and one step thumbnail
        self.assertIn('alt="The screen at the verdict"', html)

    def test_the_numbers_beside_the_frame_match_its_pills(self):
        result = result_with(self.run_dir)
        result["assertions"][1]["matches"] = [{"id": "plans", "label": "Plans", "value": None, "role": "StaticText",
                                               "rect": [24, 118, 354, 34]}]
        html = render_html(result, self.run_dir)
        # The failed text has no element on screen, so the frame has no pill 1; the held one is pill 2.
        self.assertIn('class="pill fail" role="img" aria-label="1, failed, not outlined on the frame"', html)
        self.assertIn('class="pill pass" role="img" aria-label="2, held"', html)
        self.assertLess(html.index('aria-label="1, failed'), html.index('aria-label="2, held'))
        self.assertIn("found in text &quot;Plans&quot;", html)

    def test_pills_go_to_failures_first_and_at_most_a_few(self):
        items = [(True, True)] * 8 + [(False, True), (False, False)]
        chosen = pill_numbers(items)
        self.assertEqual(len(chosen), MAX_PILLS)
        self.assertIn(8, chosen)                      # the failure with an element
        self.assertNotIn(9, chosen)                   # nothing on screen to point at
        self.assertEqual(chosen - {8}, set(range(MAX_PILLS - 1)))

    def test_verdict_chips(self):
        for verdict, tone, word in (("passed", "pass", "Passed"), ("failed", "fail", "Failed"),
                                    ("needs_review", "review", "Needs review"),
                                    ("couldnt_run", "none", "Couldn&#x27;t run")):
            with self.subTest(verdict=verdict):
                html = render_html(result_with(self.run_dir, verdict=verdict), self.run_dir)
                self.assertRegex(html, f'<span class="verdict {tone}"><svg[^>]*>.*?</svg>{word}</span>')
        self.assertIn("<title>Couldn&#x27;t run: Paywall", render_html(result_with(self.run_dir, verdict="couldnt_run"),
                                                                       self.run_dir))

    def test_a_run_that_couldnt_start(self):
        result = result_with(self.run_dir, verdict="couldnt_run", assertions=[], steps=[], frames=[], agent=None,
                             reason={"class": "busy", "message": "Every simulator is in use.", "fix": "Wait."})
        path = write_report(self.run_dir, result)
        html = path.read_text()
        self.assertIn("Every simulator is in use.", html)
        self.assertIn("Not evaluated", html)
        self.assertNotIn("<img", html)
        self.assertNotIn(">Steps<", html)  # no steps taken: no Steps section

    def test_paths_under_home_read_as_tilde(self):
        home = str(Path.home())
        result = result_with(self.run_dir, repro=f"mobster verify --check {home}/app/.mobster/runs/x/check.yaml",
                             app={"bundle_id": "dev.mobster.daybreak", "name": "Daybreak", "version": "1.0 (1)",
                                  "path": f"{home}/app/Daybreak.app"})
        html = render_html(result, self.run_dir)
        self.assertNotIn(home + "/", html)
        self.assertIn("~/app/Daybreak.app", html)
        self.assertIn("~/app/.mobster/runs/x/check.yaml", html)


class OverlayTests(unittest.TestCase):
    def test_shown_nodes_thin_and_asserted_nodes_green_or_red(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            frame = folder / "verdict.jpg"
            Image.new("RGB", (603, 1311), (255, 255, 255)).save(frame, "JPEG", quality=95)
            tree = parse_tree(paywall(plans=("Monthly", "Annual")))
            assertions = [parse_assertion({"text": "Choose your plan"}),
                          parse_assertion({"count": {"id": "/^plan_/"}, "equals": 3}),
                          parse_assertion({"text": "Missing"})]
            results = evaluate_on(assertions, tree)
            out = draw_overlay(frame, tree, [(r, list(r.paths)) for r in results], folder / "ax.jpg")
            with Image.open(out) as image:
                self.assertEqual(image.size, (603, 1311))
                scale = 603 / 402

                def edge(y):  # the strongest colour just around an element's left edge (x = 24 pt)
                    return [image.getpixel((x, y)) for x in range(round(24 * scale) - 12, round(24 * scale) + 4)]

                # The title (held) is outlined green on its left edge, each plan (failed count) red.
                self.assertTrue(any(g > r + 40 for r, g, b in edge(round((118 + 17) * scale))))
                for top in (180, 270):
                    self.assertTrue(any(r > g + 40 for r, g, b in edge(round((top + 40) * scale))))
                # Nothing is laid over the screen's bottom: the missing text is listed in the report, not here.
                self.assertGreater(sum(image.getpixel((301, 1290))), 740)
                # The asserted rows are tinted, not covered: no pill over the middle of a plan's row.
                self.assertGreater(min(image.getpixel((301, round(225 * scale)))), 200)

    def test_every_shown_node_is_outlined_faintly(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            frame = folder / "verdict.jpg"
            Image.new("RGB", (402, 874), (255, 255, 255)).save(frame, "JPEG", quality=95)
            tree = parse_tree(app(node("Button", "Alone", rect=(100, 300, 200, 60))))
            out = draw_overlay(frame, tree, [], folder / "ax.jpg")
            with Image.open(out) as image:
                red, green, blue = image.getpixel((200, 300))
                self.assertLess(red, 245)            # a line is drawn
                self.assertGreater(blue, red)         # in violet
                self.assertGreater(red, 120)          # at 35% opacity, not solid


if __name__ == "__main__":
    unittest.main()
