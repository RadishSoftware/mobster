import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "gen_icons.py"
SOURCE = ROOT / "docs" / "brand" / "icons"


def generator():
    spec = importlib.util.spec_from_file_location("gen_icons", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SOURCE.exists() and (ROOT / "dashboard").exists(),
                     "the icon source and the app aren't in this tree")
class IconCopiesTests(unittest.TestCase):
    def test_every_copy_is_current(self):
        result = subprocess.run([sys.executable, str(GENERATOR), "--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr or "a copy is stale: run python3 scripts/gen_icons.py")

    def test_the_line_is_the_one_icon_css_draws(self):
        manifest = json.loads((SOURCE / "icons.json").read_text())
        stroke = manifest["stroke"]
        css = (ROOT / "dashboard" / "src" / "components" / "ui" / "icon.css").read_text()
        self.assertEqual((stroke["base"], stroke["perPx"]), (0.5, 0.0625))
        self.assertIn("stroke-width: calc((12 / var(--icon-size, 16) + 1.5) * 1px);", css)


@unittest.skipUnless(SOURCE.exists(), "the icon source isn't in this tree")
class IconSourceTests(unittest.TestCase):
    def setUp(self):
        self.module = generator()
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        shutil.copytree(SOURCE, self.folder / "icons")
        self.module.SOURCE = self.folder / "icons"

    def load(self):
        with redirect_stderr(io.StringIO()):
            return self.module.load()

    def draw(self, name, body, motion=None):
        (self.module.SOURCE / "svg" / f"{name}.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">{body}</svg>')
        manifest = json.loads((self.module.SOURCE / "icons.json").read_text())
        manifest["icons"][name] = {"motion": motion} if motion else {}
        (self.module.SOURCE / "icons.json").write_text(json.dumps(manifest))

    def test_reads_every_icon_and_wraps_a_moving_one(self):
        _, icons = self.load()
        self.assertIn("bell", icons)
        self.assertEqual(icons["check"]["nodes"][0][1], {"data-part": "draw"})
        self.assertEqual(icons["check"]["nodes"][0][2][0][1]["pathLength"], "1")
        self.assertTrue(icons["arrow-right"]["whole"])
        self.assertFalse(icons["bell"]["whole"])

    def test_refuses_paint_and_markup_an_icon_doesnt_draw(self):
        for body in ('<path d="M0 0" stroke="#f00"/>', '<path d="M0 0" filter="url(#x)"/>', '<script>alert(1)</script>',
                     '<path d="M0 0" onclick="x"/>', '<image href="x.png"/>'):
            with self.subTest(body=body):
                self.draw("odd", body)
                with self.assertRaises(SystemExit):
                    self.load()

    def test_refuses_a_motion_without_its_parts(self):
        for body in ('<path d="M3 6h18"/>', '<g data-part="body"><path d="M3 6h18"/></g>'):
            with self.subTest(body=body):
                self.draw("odd", body, motion="lid")
                with self.assertRaises(SystemExit):
                    self.load()

    def test_refuses_a_drawing_icons_json_doesnt_name(self):
        (self.module.SOURCE / "svg" / "stray.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 '
                                                              '0 24 24"/>')
        with self.assertRaises(SystemExit):
            self.load()


if __name__ == "__main__":
    unittest.main()
