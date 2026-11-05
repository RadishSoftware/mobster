import importlib.util
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "gen_agent_marks.py"


def generator():
    spec = importlib.util.spec_from_file_location("gen_agent_marks", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless((ROOT / "docs" / "brand" / "marks" / "marks.json").exists(),
                     "the marks' source isn't in this tree")
class PartnerMarksTest(unittest.TestCase):
    def setUp(self):
        self.gen = generator()

    def test_every_copy_is_current(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.gen.main(["--check"])
        self.assertEqual(code, 0, f"stale marks:\n{out.getvalue()}")

    def test_every_mark_has_its_source_and_a_measured_box(self):
        data = self.gen.load()
        for mark in data["marks"]:
            with self.subTest(mark=mark["id"]):
                self.assertTrue((self.gen.SOURCE / "svg" / mark["file"]).exists())
                self.assertTrue(mark["source"].startswith("https://"))
                self.assertRegex(mark["taken"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertEqual(len(mark["box"]), 4)
                self.assertGreater(mark["scale"], 0.5)
                self.assertLessEqual(mark["scale"], 4 / 3)

    def test_a_copy_is_the_tile_square_centred_on_the_mark(self):
        mark = {"id": "t", "file": "t.svg", "box": [10, 20, 40, 20], "scale": 0.8}
        source = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><path d="M10 20h40v20H10z"/></svg>'
        copy = self._render(mark, source)
        self.assertEqual(ET.fromstring(copy).attrib["viewBox"], "-14.444 -14.444 88.889 88.889")

    def test_a_one_colour_copy_takes_the_colour_it_is_given(self):
        mark = {"id": "t", "file": "t.svg", "box": [0, 0, 10, 10], "scale": 1}
        source = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path fill="#000" d="M0 0h10v10H0z"/>'
                  '<path style="fill:#111;stroke:none" d="M0 0h1v1H0z"/></svg>')
        copy = self._render(mark, source, "#F4F2EE")
        self.assertNotIn("#000", copy)
        self.assertNotIn("#111", copy)
        self.assertIn('fill="#F4F2EE"', copy)
        self.assertIn("stroke:none", copy)

    def test_it_refuses_anything_but_shapes(self):
        mark = {"id": "t", "file": "t.svg", "box": [0, 0, 10, 10], "scale": 1}
        refused = [
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><script>alert(1)</script></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><style>path{fill:red}</style></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><image href="https://x.test/a.png"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><use href="https://x.test/a.svg#m"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path onload="x()" d="M0 0h1v1H0z"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path '
            'fill="url(https://x.test/g)" d="M0 0h1v1H0z"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path '
            'style="background:url(#a)" d="M0 0h1v1H0z"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><foreignObject/></svg>',
        ]
        for source in refused:
            with self.subTest(source=source), self.assertRaises(self.gen.MarkError):
                self._render(mark, source)

    def test_a_misspelled_or_shortened_option_writes_nothing(self):
        for option in ("--chekc", "--ch"):
            with self.subTest(option=option), mock.patch.object(self.gen, "plan") as plan, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as refused:
                self.gen.main([option])
            self.assertEqual(refused.exception.code, 2)
            plan.assert_not_called()

    def test_a_glyph_is_its_owners_icon_as_paths(self):
        mark = next(m for m in self.gen.load()["marks"] if m["id"] == "github")
        view, paths = self.gen.glyph(mark)
        self.assertEqual(view, "0 0 24 24")
        self.assertEqual(len(paths), 1)
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "svg").mkdir()
            (Path(folder) / "svg" / "g.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8">'
                                                       '<rect width="8" height="8"/></svg>')
            with mock.patch.object(self.gen, "SOURCE", Path(folder)), self.assertRaises(self.gen.MarkError):
                self.gen.glyph({"id": "g", "glyph": "g.svg"})

    def test_an_icon_fills_the_tile_as_it_is(self):
        mark = {"id": "t", "file": "t.svg", "box": [0, 0, 48, 48], "scale": 1, "fill": True}
        source = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">'
                  '<rect width="48" height="48" rx="12"/></svg>')
        self.assertEqual(ET.fromstring(self._render(mark, source)).attrib["viewBox"], "0 0 48 48")

    def test_an_icon_keeps_its_own_colours(self):
        data = {"marks": [{"id": "t", "name": "T", "products": ["T"], "owner": "T", "file": "t.svg", "ink": True,
                           "fill": True, "surfaces": ["app"], "source": "https://t.test", "taken": "2026-10-17"}]}
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "marks.json"
            manifest.write_text(json.dumps(data))
            with mock.patch.object(self.gen, "MANIFEST", manifest), self.assertRaises(self.gen.MarkError):
                self.gen.load()

    def test_the_sign_in_page_knows_a_mark_by_every_name_it_goes_by(self):
        data = self.gen.load()
        light, dark = self.gen.ink()
        module = {}
        exec(self.gen._py_module(data, light, dark), module)
        for name, mark in (("monday.com", "monday"), ("Google Sheets", "google-sheets"), ("sheets", "google-sheets"),
                           ("Help Scout", "helpscout"), ("GitHub checks", "github")):
            with self.subTest(name=name):
                self.assertEqual(module["NAMES"][self.gen.slug(name)], mark)
        self.assertIn(light, module["LIGHT"]["github"])
        self.assertIn(dark, module["DARK"]["github"])
        self.assertNotIn("slack", module["DARK"])

    def test_an_alias_names_a_mark_that_exists(self):
        data = self.gen.load()
        ids = {mark["id"] for mark in data["marks"]}
        for alias, target in data["aliases"].items():
            self.assertIn(target, ids, alias)

    def test_an_alias_is_never_also_a_marks_id(self):
        data = self.gen.load()
        data["aliases"] = {**data["aliases"], "google-g": "gemini"}
        with self.assertRaises(self.gen.MarkError):
            self._validate(data)

    def test_each_company_network_xray_names_has_one_mark(self):
        from mobile_agent.netxray import trackers
        data = self.gen.load()
        companies = {company for _, (company, _) in trackers.DOMAINS.items()}
        named = [company for mark in data["marks"] for company in mark.get("companies", ())]
        self.assertTrue(named)
        self.assertEqual(sorted(set(named) - companies), [], "a mark names a company Network X-ray never shows")
        listed = self.gen._ts_list(data, "app")
        self.assertIn('"google": "google-g"', listed)
        self.assertIn('"twilio segment": "twilio"', listed)
        self.assertNotIn("COMPANY_MARKS", self.gen._ts_list(data, "remote"))  # company marks are the app's only
        twice = {**data, "marks": [*data["marks"], {**data["marks"][0], "id": "again", "companies": ["Google"]}]}
        with self.assertRaises(self.gen.MarkError):
            self._validate(twice)

    def _validate(self, data):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "marks.json"
            manifest.write_text(json.dumps(data))
            saved, self.gen.MANIFEST = self.gen.MANIFEST, manifest
            try:
                return self.gen.load()
            finally:
                self.gen.MANIFEST = saved

    def _render(self, mark, source, colour=None):
        return self.gen.render(mark, colour, source)


if __name__ == "__main__":
    unittest.main()
