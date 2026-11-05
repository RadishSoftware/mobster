import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DMG = ROOT / "desktop" / "dmg"


def load_background():
    spec = importlib.util.spec_from_file_location("dmg_background_under_test", DMG / "background.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless((DMG / "settings.py").is_file(), "the desktop app is not part of this copy")
class DmgLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PIL import Image

        cls.bg = load_background()
        cls.settings = cls.bg.load_settings("/tmp/build/Mobster.app")
        cls.frames = []
        with Image.open(cls.settings["background"]) as tiff:
            for index in range(tiff.n_frames):
                tiff.seek(index)
                cls.frames.append((tiff.convert("RGB"), tiff.info.get("dpi"), tiff.info.get("icc_profile")))

    def content(self, tab_bar=False):
        width, height = self.settings["window_rect"][1]
        return width, height - self.settings["_title_bar"] - (self.settings["_tab_bar"] if tab_bar else 0)

    def test_the_settings_load_as_dmgbuild_runs_them(self):
        s = self.settings
        self.assertEqual(s["files"], ["/tmp/build/Mobster.app"])
        self.assertEqual(s["symlinks"], {"Applications": "/Applications"})
        self.assertEqual(s["icon"], "/tmp/build/Mobster.app/Contents/Resources/icon.icns")
        self.assertEqual(Path(s["background"]), DMG / "background.tiff")
        self.assertEqual(set(s["icon_locations"]), {"Mobster.app", "Applications"})
        self.assertEqual(s["default_view"], "icon-view")
        for bar in ("show_toolbar", "show_sidebar", "show_status_bar", "show_pathbar", "show_tab_view"):
            self.assertIs(s[bar], False, bar)
        self.assertNotIn("hide_extensions", s)

    def test_icons_labels_and_the_line_fit_the_window_even_under_a_tab_bar(self):
        width, height = self.content(tab_bar=True)
        half = self.settings["icon_size"] / 2
        _, band_bottom = self.bg.label_band(self.settings)
        (ax, ay), (bx, by) = self.settings["icon_locations"].values()
        for x, y in ((ax, ay), (bx, by)):
            self.assertGreaterEqual(x - half, 0)
            self.assertLessEqual(x + half, width)
            self.assertGreaterEqual(y - half, 0)
        self.assertLess(band_bottom, self.bg.CAPTION_Y - self.bg.CAPTION_SIZE)
        self.assertLess(self.bg.CAPTION_Y + self.bg.CAPTION_SIZE, height)
        self.assertEqual(ay, by)
        self.assertLess(ax + half, bx - half)
        x0, x1, y = self.bg.arrow_geometry(self.settings)
        self.assertEqual(y, ay)
        self.assertGreater(x0, ax + half)
        self.assertLess(x1, bx - half)

    def test_the_arrow_is_centred_in_the_window_whatever_the_art_s_size(self):
        # background.tiff is 960 x 600, larger than the 660-point window on purpose: Finder shows its top left. The
        # arrow's centre (330) is the window's, not the art's.
        x0, x1, _ = self.bg.arrow_geometry(self.settings)
        self.assertEqual((x0 + x1) / 2, self.settings["window_rect"][1][0] / 2)

    def test_the_background_holds_1x_and_2x_in_srgb_and_covers_the_window(self):
        self.assertEqual(len(self.frames), 2)
        width, height = self.content()
        for (image, dpi, icc), scale in zip(self.frames, (1, 2)):
            self.assertEqual(image.size, (self.bg.CANVAS[0] * scale, self.bg.CANVAS[1] * scale))
            self.assertGreaterEqual(image.width, width * scale)
            self.assertGreaterEqual(image.height, (height + 4) * scale)
            self.assertEqual(round(dpi[0]), 72 * scale)
            self.assertIn(b"sRGB", icc or b"")

    def test_labels_read_black_or_white_on_every_pixel_under_them(self):
        top, bottom = self.bg.label_band(self.settings)
        width, _ = self.content()
        for image, _, _ in self.frames:
            scale = image.width // self.bg.CANVAS[0]
            band = image.crop((0, int(top * scale), width * scale, int(bottom * scale) + 1))
            colours = {colour for _, colour in band.getcolors(maxcolors=band.width * band.height)}
            for colour in colours:
                self.assertGreaterEqual(self.bg.contrast((0, 0, 0), colour), 4.5, colour)
                self.assertGreaterEqual(self.bg.contrast((255, 255, 255), colour), 4.5, colour)

    def test_the_arrow_and_the_line_read_on_the_ground(self):
        bg = self.bg
        self.assertGreaterEqual(bg.contrast(bg.over(bg.WHITE, bg.ARROW_OPACITY), bg.GROUND), 3.0)
        self.assertGreaterEqual(bg.contrast(bg.WHITE, bg.GROUND), 4.5)

    def test_the_committed_background_is_what_background_py_draws_for_these_settings(self):
        from PIL import Image, ImageChops

        top, bottom = self.bg.label_band(self.settings)
        for (committed, _, _), scale in zip(self.frames, (1, 2)):
            drawn = self.bg.render(self.settings, scale)
            ground = Image.new("RGB", drawn.size, self.bg.GROUND)
            for part, rows in (("arrow", (0, int(top * scale))), ("text", (int(bottom * scale), drawn.height))):
                box = (0, rows[0], drawn.width, rows[1])
                boxes = [ImageChops.difference(image.crop(box), ground.crop(box)).getbbox()
                         for image in (committed, drawn)]
                self.assertIsNotNone(boxes[1], part)
                for a, b in zip(*boxes):
                    self.assertLessEqual(abs(a - b),
                                         scale, f"{part} at {scale}x: committed {boxes[0]}, drawn {boxes[1]}")


if __name__ == "__main__":
    unittest.main()
