import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from mobile_agent.testkit import junit_verdict
from mobile_agent.testkit import reports as R
from mobile_agent.testkit.discover import Entry, in_matrix, with_names
from mobile_agent.testkit.snapshots_cli import snapshots as snapshots_command
from mobile_agent.tests.test_verify_fixtures import app, node
from mobile_agent.tests.test_verify_runner import BUNDLE, Driver, Manager, RunnerCase
from mobile_agent.verify import conditions, runner, snapshots
from mobile_agent.verify.checks import CheckError, check_from_dict, dump_check, load_check
from mobile_agent.verify.report import render_html
from mobile_agent.verify.runner import VerifyRun

SIZE = (402, 874)


def screen(patch=None, tint=0):
    """A flat screen with a title bar; ``patch`` is a box painted a different colour; ``tint`` moves every pixel."""
    image = Image.new("RGB", SIZE, (240 + tint, 240, 245))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 100, 401, 160], fill=(30, 60, 120))
    if patch:
        draw.rectangle(patch, fill=(200, 40, 40))
    return image


HOME = app(node("Other", children=[node("StaticText", "Home", id="title", rect=(24, 118, 354, 34)),
                                   node("StaticText", "9:41 AM", id="clock", rect=(24, 300, 100, 20))],
                rect=(0, 0, 402, 874)), bundle=BUNDLE)


class ShotManager(Manager):
    """Takes whatever image ``self.image`` holds, as the PNG or JPEG the path asks for."""

    def __init__(self, image):
        super().__init__()
        self.image = image

    def screenshot(self, target, path, *, max_width=None, quality=75):
        self._step("screenshot", Path(path).name, max_width)
        if str(path).endswith(".png"):
            self.image.save(path, "PNG")
        else:
            self.image.resize((804, 1748)).save(path, "JPEG", quality=quality)
        return Path(path)


class CompareTests(unittest.TestCase):
    def test_identical_screens_match(self):
        found = snapshots.compare(screen(), screen())
        self.assertEqual((found.status, found.changed_pixels, found.regions), ("matched", 0, []))

    def test_antialiasing_noise_never_fails(self):
        noisy = screen()
        pixels = noisy.load()
        for x in range(0, 402, 3):                # a column of edge pixels a shade off, as a font's subpixels are
            for y in range(100, 160):
                r, g, b = pixels[x, y]
                pixels[x, y] = (r + 9, g + 9, b + 9)
        self.assertEqual(snapshots.compare(screen(), noisy).status, "matched")
        self.assertEqual(snapshots.compare(screen(), screen(tint=10)).status, "matched")

    def test_a_real_change_fails_with_a_box_around_it_and_a_diff_image(self):
        found = snapshots.compare(screen(), screen(patch=[100, 400, 299, 499]))
        self.assertEqual(found.status, "changed")
        # the blur widens the edge
        self.assertAlmostEqual(found.changed_percent, 100 * 200 * 100 / (402 * 874), delta=1)
        (x, y, w, h), = found.regions
        self.assertTrue(x <= 100 and y <= 400 and x + w >= 300 and y + h >= 500)
        self.assertLess(w, 260)
        self.assertEqual(found.diff.size, SIZE)

    def test_a_change_under_the_threshold_matches(self):
        small = screen(patch=[10, 400, 29, 409])
        self.assertEqual(snapshots.compare(screen(), small, threshold=0.1).status, "matched")
        self.assertEqual(snapshots.compare(screen(), small, threshold=0.01).status, "changed")

    def test_a_masked_change_is_ignored_and_leaves_the_total_smaller(self):
        changed = screen(patch=[100, 400, 299, 499])
        found = snapshots.compare(screen(), changed, masks=[[90, 390, 220, 120]])
        self.assertEqual(found.status, "matched")
        self.assertEqual(found.compared_pixels, 402 * 874 - 220 * 120)

    def test_two_changes_far_apart_are_two_regions(self):
        found = snapshots.compare(screen(), screen(patch=[10, 300, 90, 340]))
        two = snapshots.compare(screen(), _both())
        self.assertEqual(len(found.regions), 1)
        self.assertEqual(len(two.regions), 2)

    def test_another_size_is_its_own_status(self):
        found = snapshots.compare(screen(), Image.new("RGB", (390, 844)))
        self.assertEqual(found.status, "size_changed")


def _both():
    image = screen(patch=[10, 300, 90, 340])
    ImageDraw.Draw(image).rectangle([300, 700, 390, 760], fill=(0, 200, 0))
    return image


class ParseTests(unittest.TestCase):
    def check(self, expect, **more):
        return check_from_dict({"version": 1, "app": {"bundle": BUNDLE}, "expect": expect, **more})

    def test_a_snapshot_with_masks_and_a_threshold_round_trips_through_a_saved_check(self):
        check = self.check([{"text": "Home"}, {"snapshot": "home", "threshold": 0.5,
                                                "mask": [{"id": "clock"}, {"label": "Last sync"},
                                                         {"rect": [0, 100, 390, 40]}]}])
        self.assertEqual(check.snapshot.name, "home")
        self.assertEqual(check.snapshot.threshold, .5)
        self.assertEqual(len(check.snapshot.masks), 3)
        self.assertEqual(len(check.expect), 1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "home.yaml"
            path.write_text(dump_check(check), encoding="utf-8")
            again = load_check(path)
        self.assertEqual(again.snapshot, check.snapshot)

    def test_a_snapshot_alone_is_a_check(self):
        self.assertEqual(self.check([{"snapshot": "home"}]).snapshot.threshold, snapshots.DEFAULT_THRESHOLD)

    def refused(self, expect, message, **more):
        with self.assertRaisesRegex(CheckError, message):
            self.check(expect, **more)

    def test_mistakes_are_refused_in_a_sentence(self):
        self.refused([{"snapshot": "Home Screen"}], "names the baseline file")
        self.refused([{"snapshot": "home"}, {"snapshot": "other"}], "one snapshot")
        self.refused([{"snapshot": "home", "treshold": 1}], "treshold")
        self.refused([{"snapshot": "home", "threshold": 101}], "from 0 to 100")
        self.refused([{"snapshot": "home", "mask": [{"rect": [0, 0, 0, 10]}]}], "four numbers")
        self.refused([{"snapshot": "home", "mask": [{"id": "x", "rect": [0, 0, 1, 1]}]}], "rect goes alone")
        self.refused([{"snapshot": "home", "mask": [{"color": "red"}]}], "color")
        self.refused([{"snapshot": "home", "mask": "clock"}], "must be a list")
        with self.assertRaisesRegex(CheckError, "goes in expect"):
            self.check([{"text": "Home"}], somewhere=[{"snapshot": "home"}])


class MatrixTests(unittest.TestCase):
    def test_lists_expand_in_a_fixed_order(self):
        parsed = conditions.parse({"appearance": ["light", "dark"], "content_size": ["large", "accessibility-large"],
                                   "locale": "de_DE"})
        self.assertTrue(conditions.is_matrix(parsed))
        combos = conditions.combinations(parsed)
        self.assertEqual(len(combos), 4)
        self.assertEqual(combos[0], {"appearance": "light", "content_size": "large", "locale": "de_DE"})
        self.assertEqual(combos[-1]["appearance"], "dark")
        self.assertFalse(conditions.is_matrix(conditions.parse({"appearance": "dark"})))
        self.assertEqual(conditions.combinations(None), [{}])

    def test_bad_lists_are_refused(self):
        with self.assertRaisesRegex(CheckError, "lists nothing"):
            conditions.parse({"appearance": []})
        with self.assertRaisesRegex(CheckError, "isn't one Mobster knows"):
            conditions.parse({"appearance": ["light", "sepia"]})
        with self.assertRaisesRegex(CheckError, "the most is 24"):
            conditions.parse({"appearance": ["light", "dark"], "locale": ["de", "fr", "ja", "ar", "es", "it", "pt"],
                              "content_size": ["large", "small"]})

    def test_a_check_with_lists_becomes_one_entry_per_combination(self):
        check = check_from_dict({"version": 1, "app": {"bundle": BUNDLE}, "name": "Home", "expect": [{"text": "x"}],
                                 "conditions": {"appearance": ["light", "dark"], "content_size": "large"}})
        entry = Entry(path=Path("/p/home.yaml"), file="home.yaml", check=check, classname="checks.home")
        plain = Entry(path=Path("/p/b.yaml"), file="b.yaml", check=check_from_dict(
            {"version": 1, "app": {"bundle": BUNDLE}, "expect": [{"text": "x"}]}), classname="checks.b")
        out = in_matrix([entry, plain])
        self.assertEqual([item.name for item in out], ["Home [light]", "Home [dark]", plain.name])
        self.assertEqual(out[1].check.conditions, {"appearance": "dark", "content_size": "large"})
        self.assertEqual(out[1].classname, "checks.home.dark")
        self.assertEqual([item.name for item in with_names(out, ["b"])], [plain.name])

    def test_a_single_run_refuses_a_matrix_before_touching_a_simulator(self):
        check = check_from_dict({"version": 1, "app": {"bundle": BUNDLE}, "expect": [{"text": "x"}],
                                 "conditions": {"appearance": ["light", "dark"]}})
        with tempfile.TemporaryDirectory() as folder:
            manager = Manager()
            result = runner.verify(check, mode="launch", runs_dir=Path(folder) / ".mobster" / "runs", manager=manager)
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("couldnt_run", "usage"))
        self.assertIn("mobster test", result["reason"]["fix"])
        self.assertNotIn("acquire", manager.names())


class Run(RunnerCase):
    def run_check(self, image, *, update=False, record=True, conditions=None, device="iPhone 17 Pro", name="home",
                  **snapshot):
        data = {"version": 1, "app": {"bundle": BUNDLE}, "name": "Home", "expect": [{"text": "Home"},
                                                                                {"snapshot": name, **snapshot}]}
        if conditions:
            data["conditions"] = conditions
        check = check_from_dict(data)
        if update or not record:
            from dataclasses import replace
            check = replace(check, snapshot_update=update, snapshot_record=record)
        manager = ShotManager(image)
        driver = Driver([HOME])
        self.watch(driver, manager)
        run = VerifyRun(check, mode="launch", runs_dir=self.runs, manager=manager,
                        simctl=lambda args: (0, ""))
        with mock.patch.object(runner, "build_driver", lambda *a: driver):
            run.prepare()
        return run.finish(), run

    baseline = property(lambda self: self.root / ".mobster" / "snapshots" / "home"
                        / "home@iphone-17-pro-default-default-default.png")

    def test_no_baseline_records_one_and_asks_for_review(self):
        result, _ = self.run_check(screen())
        self.assertEqual((result["verdict"], result["snapshot"]["status"]), ("needs_review", "recorded"))
        self.assertIn("baseline recorded", result["reason"]["message"])
        self.assertTrue(self.baseline.is_file())
        self.assertEqual(result["snapshot"]["key"],
                         ".mobster/snapshots/home/home@iphone-17-pro-default-default-default.png")
        self.assertEqual(result["check"]["expect"][-1], {"snapshot": "home"})
        self.assertEqual(result["snapshot"]["size"], list(SIZE))

    def test_an_approved_ci_suite_fails_without_a_baseline_and_records_none(self):
        result, _ = self.run_check(screen(), record=False)
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "finding"))
        self.assertIn("never records one", result["findings"][0]["message"])
        self.assertFalse(self.baseline.exists())
        self.run_check(screen())                       # recorded locally, it compares as any run does
        self.assertEqual(self.run_check(screen(tint=6), record=False)[0]["snapshot"]["status"], "matched")

    def test_the_next_run_matches_it(self):
        self.run_check(screen())
        result, run = self.run_check(screen(tint=6))
        self.assertEqual((result["verdict"], result["snapshot"]["status"]), ("passed", "matched"))
        self.assertEqual(result["snapshot"]["changedPixels"], 0)
        for key in ("baseline", "current"):
            self.assertTrue((run.run_dir / result["snapshot"][key]).is_file())
        self.assertIsNone(result["snapshot"]["diff"])

    def test_a_changed_screen_fails_with_the_diff_and_the_baseline_stays(self):
        self.run_check(screen())
        before = self.baseline.read_bytes()
        result, run = self.run_check(screen(patch=[100, 400, 299, 499]))
        self.assertEqual((result["verdict"], result["reason"]["class"]), ("failed", "finding"))
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["status"], "changed")
        self.assertGreater(snapshot["changedPercent"], 5)
        self.assertTrue((run.run_dir / snapshot["diff"]).is_file())
        self.assertTrue(snapshot["regions"])
        self.assertIn("mobster test --update-snapshots", result["findings"][0]["message"])
        self.assertEqual(self.baseline.read_bytes(), before)
        self.assertIn(snapshot["diff"], render_html(result, run.run_dir))

    def test_update_writes_the_baseline_and_passes(self):
        self.run_check(screen())
        result, _ = self.run_check(screen(patch=[100, 400, 299, 499]), update=True)
        self.assertEqual((result["verdict"], result["snapshot"]["status"]), ("passed", "updated"))
        self.assertEqual(Image.open(self.baseline).getpixel((150, 450)), (200, 40, 40))
        self.assertEqual(self.run_check(screen(patch=[100, 400, 299, 499]))[0]["snapshot"]["status"], "matched")

    def test_a_mask_by_id_hides_a_changing_clock(self):
        self.run_check(screen(), mask=[{"id": "clock"}])
        result, _ = self.run_check(screen(patch=[24, 300, 123, 319]), mask=[{"id": "clock"}])
        self.assertEqual(result["snapshot"]["status"], "matched")
        self.assertTrue(any(rect[1] == 300 for rect in result["snapshot"]["masks"]))
        result, _ = self.run_check(screen(patch=[24, 300, 123, 319]))
        self.assertEqual(result["snapshot"]["status"], "changed")

    def test_masks_scale_from_points_to_pixels(self):
        self.run_check(screen(), mask=[{"id": "clock"}])
        image = Image.new("RGB", (804, 1748), (240, 240, 245))
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 200, 803, 320], fill=(30, 60, 120))
        snapshots.save_png(image, self.baseline)
        result, _ = self.run_check(image, mask=[{"id": "clock"}])
        self.assertEqual(result["snapshot"]["status"], "matched")
        self.assertEqual(result["snapshot"]["masks"][0][:2], [48, 600])    # 24, 300 pt at 2x

    def test_each_device_and_condition_has_its_own_baseline(self):
        self.run_check(screen())
        result, _ = self.run_check(screen(), conditions={"appearance": "dark"})
        self.assertEqual(result["snapshot"]["status"], "recorded")
        self.assertTrue((self.baseline.parent / "home@iphone-17-pro-dark-default-default.png").is_file())

    def test_a_baseline_of_another_size_fails_and_says_so(self):
        snapshots.save_png(Image.new("RGB", (390, 844), "white"), self.baseline)
        result, _ = self.run_check(screen())
        self.assertEqual((result["verdict"], result["snapshot"]["status"]), ("failed", "size_changed"))
        self.assertIn("390x844", result["findings"][0]["message"])

    def test_the_simulator_status_bar_is_set_not_masked(self):
        result, run = self.run_check(screen())
        self.assertTrue(result["snapshot"]["statusBarSet"])
        self.assertIn("status_bar", result["conditions"]["applied"])
        self.assertEqual(result["snapshot"]["masks"], [])

    def test_an_unreadable_baseline_is_a_finding_not_a_crash(self):
        self.baseline.parent.mkdir(parents=True)
        self.baseline.write_bytes(b"not a png")
        result, _ = self.run_check(screen())
        self.assertEqual(result["verdict"], "needs_review")
        self.assertIn("can't be read", result["reason"]["message"])


class MaskTests(unittest.TestCase):
    def tree(self, *nodes):
        from mobile_agent.verify.tree import AXTree
        return AXTree(BUNDLE, (402, 874), nodes)

    def node(self, role, rect, ident=""):
        from mobile_agent.verify.tree import AXNode
        return AXNode("/p/" + ident + role, role, ident, "", "", "", rect, True, False, True)

    def test_the_keyboard_and_the_fields_above_it_are_masked(self):
        snap = snapshots.parse_snapshot({"snapshot": "form"})
        tree = self.tree(self.node("TextField", (10, 100, 300, 40)), self.node("Keyboard", (0, 600, 402, 274)),
                         self.node("Button", (10, 400, 100, 40)))
        rects = snapshots.mask_rects(snap, tree, 804, status_bar_set=True)
        self.assertEqual(sorted(rect[1] for rect in rects), [200, 1200])
        none = snapshots.mask_rects(snap, self.tree(self.node("TextField", (10, 100, 300, 40))), 804,
                                    status_bar_set=True)
        self.assertEqual(none, [])

    def test_a_phone_masks_the_status_bar(self):
        snap = snapshots.parse_snapshot({"snapshot": "home"})
        rects = snapshots.mask_rects(snap, self.tree(), 402, status_bar_set=False)
        self.assertEqual(rects, [[0, 0, 402, snapshots.STATUS_BAR_POINTS]])


class FilesTests(unittest.TestCase):
    def test_names_and_audit(self):
        self.assertEqual(snapshots.variant("iPhone 17 Pro", {"appearance": "dark", "locale": "de_DE"}),
                         "iphone-17-pro-dark-default-de_DE")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for key, name in (("home", "home@iphone-17-pro-dark-default-default"),
                              ("home", "home@iphone-17-pro-light-default-default"),
                              ("gone", "home@iphone-17-pro-default-default-default"),
                              ("home", "old@iphone-17-pro-default-default-default")):
                snapshots.save_png(Image.new("RGB", (4, 4)), root / ".mobster" / "snapshots" / key / f"{name}.png")
            check = check_from_dict({"version": 1, "app": {"bundle": BUNDLE}, "expect": [{"snapshot": "home"}],
                                     "conditions": {"appearance": ["dark"]}})
            states = {(item["check"], item["name"], item["variant"]): item["state"]
                      for item in snapshots.audit(root, [("home", check)])}
        self.assertEqual(states[("home", "home", "iphone-17-pro-dark-default-default")], "current")
        self.assertEqual(states[("home", "home", "iphone-17-pro-light-default-default")], "stale")
        self.assertEqual(states[("gone", "home", "iphone-17-pro-default-default-default")], "orphaned")
        self.assertEqual(states[("home", "old", "iphone-17-pro-default-default-default")], "orphaned")

    def test_the_snapshots_command_lists_and_filters(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / ".mobster" / "checks").mkdir(parents=True)
            (root / ".mobster" / "checks" / "home.yaml").write_text(
                "version: 1\napp: {bundle: dev.mobster.daybreak}\nexpect:\n  - snapshot: home\n", encoding="utf-8")
            snapshots.save_png(Image.new("RGB", (4, 4)), root / ".mobster" / "snapshots" / "home"
                               / "home@iphone-17-pro-default-default-default.png")
            snapshots.save_png(Image.new("RGB", (4, 4)), root / ".mobster" / "snapshots" / "old"
                               / "x@iphone-17-pro-default-default-default.png")
            out = io.StringIO()
            code = snapshots_command(mock.Mock(json=False), ["orphaned"], out=out, cwd=root)
            as_json = io.StringIO()
            snapshots_command(mock.Mock(json=True), [], out=as_json, cwd=root)
        self.assertEqual(code, 0)
        self.assertIn("orphaned .mobster/snapshots/old/x@", out.getvalue())
        self.assertNotIn("home@", out.getvalue())
        self.assertEqual(len(json.loads(as_json.getvalue())["snapshots"]), 2)


class JunitTests(unittest.TestCase):
    def test_the_diff_image_path_is_in_the_case_output(self):
        snapshot = {"name": "home", "status": "changed", "changedPercent": 2.5, "thresholdPercent": .1,
                    "diff": "/runs/a/snapshots/home-diff.png", "baseline": "/runs/a/b.png"}
        lines = junit_verdict.snapshot_lines(snapshot, None, R._rebase)
        self.assertIn("Snapshot diff: /runs/a/snapshots/home-diff.png", lines)
        self.assertEqual(junit_verdict.snapshot_lines(None, None, R._rebase), [])


if __name__ == "__main__":
    unittest.main()
