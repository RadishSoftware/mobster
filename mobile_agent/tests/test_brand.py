import importlib.util
import io
import json
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from mobile_agent import brand, style

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "gen_brand_tokens.py"
SITE = ROOT / "site" / "app" / "styles" / "tokens.css"
APP = ROOT / "dashboard" / "src" / "index.css"


def generator():
    spec = importlib.util.spec_from_file_location("gen_brand_tokens", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def declarations(block):
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", block))


def block_after(css, opening):
    start = css.index(opening) + len(opening)
    return declarations(css[start:css.index("}", start)])


UI_REGION = re.compile(r"/\* brand:tokens as=ui scheme=(\w+)[^*]*\*/(.*?)/\* /brand:tokens \*/", re.S)


@unittest.skipUnless(APP.exists(), "the Mac app isn't in this tree")
class DataPaletteTests(unittest.TestCase):
    def test_the_apps_chart_colours_are_the_sources_in_both_appearances(self):
        css = APP.read_text()
        dark = block_after(css, ':root, [data-theme="dark"] {')
        light = block_after(css, ':root[data-theme="light"], [data-theme="light"] {')
        system_light = block_after(css, ':root:not([data-theme="dark"]) {')
        for scheme, found in (("dark", dark), ("light", light), ("light", system_light)):
            self.assertEqual([found.get(f"data-{n}", "").upper() for n in range(1, 6)], brand.DATA[scheme], scheme)


@unittest.skipUnless(GENERATOR.exists(), "the generator isn't in this tree")
class GeneratedCopiesTests(unittest.TestCase):
    def test_every_generated_copy_is_current(self):
        out = io.StringIO()
        with redirect_stdout(out):
            status = generator().main(["--check"])
        self.assertEqual(status, 0, "a surface's copy of the tokens is stale:\n" + out.getvalue())

    def test_a_deleted_source_of_a_copy_fails_instead_of_being_skipped(self):
        gen = generator()
        out = io.StringIO()
        with mock.patch.object(gen, "COPIES",
                               [("docs/brand/x.png", "docs/brand/deleted.avif")]), \
                redirect_stdout(out):
            status = gen.main(["--check"])
        self.assertEqual(status, 1)
        self.assertIn("docs/brand/deleted.avif is missing", out.getvalue())

    def test_a_source_in_a_folder_this_tree_lacks_is_still_skipped(self):
        gen = generator()
        with mock.patch.object(gen, "COPIES", [("docs/brand/x.png", "nowhere-here/x.png")]):
            gen.plan(gen.load())

    def test_an_unknown_flag_is_refused_instead_of_rewriting_the_copies(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as refused:
            generator().main(["--chek"])
        self.assertEqual(refused.exception.code, 2)
        self.assertIn("--chek", err.getvalue())

    def test_a_region_becomes_the_stylesheet(self):
        gen = generator()
        tokens = gen.load()
        text = gen.regions("a { }\n/* brand:tokens */ stale /* /brand:tokens */\nb { }", tokens)
        self.assertIn("--paper:#f8f8f8;", text)
        self.assertIn("@media (prefers-color-scheme: dark){:root{--paper:#121212;", text)
        self.assertTrue(text.startswith("a { }\n/* brand:tokens */\n:root{") and text.endswith("*/\nb { }"))

    def test_a_pruned_region_has_every_token_its_surface_reads(self):
        gen = generator()
        tokens = gen.load()
        known = {line.split(":", 1)[0][2:] for line in gen._vars(tokens, "light") + gen._vars(tokens, "dark")
                 + gen._common(tokens)}
        checked = 0
        for path in gen.tracked_files():
            if path.suffix != ".css" or not path.exists():
                continue
            text = path.read_text(errors="replace")
            for match in gen.REGION.finditer(text):
                options = dict(option.split("=", 1) for option in match.group("options").split())
                if options.get("as") == "ui":
                    continue
                prefix = options.get("prefix", "")
                body = match.group(0)
                files = [path] + [f for name in filter(None, options.get("scan", "").split(","))
                                  for f in ([ROOT / name] if (ROOT / name).is_file() else (ROOT / name).rglob("*"))
                                  if f.is_file()
                                  and f.suffix in {".css", ".ts", ".tsx", ".mjs", ".js", ".html", ".mdx"}]
                read = {name for f in files for name in re.findall(rf"var\(--{re.escape(prefix)}([\w-]+)",
                       gen.REGION.sub("", f.read_text(errors="replace")))} & known
                declared = {name for name in re.findall(rf"--{re.escape(prefix)}([\w-]+):", body)}
                self.assertEqual(sorted(read - declared), [], f"{path.relative_to(ROOT)} reads tokens its region lacks")
                checked += 1
        if gen.tracked_files() and (ROOT / "relay").exists():
            self.assertGreaterEqual(checked, 1)

    def test_a_surface_reading_a_renamed_token_is_refused(self):
        gen = generator()
        tokens = gen.load()
        used = {"duration-hover", "duration-short", "duration-local", "pane-gap", "focus-offset"}
        self.assertEqual(gen.unknown(tokens, used, "a { --duration-local: 1ms; }"), ["duration-hover"])
        stale = gen.unknown(tokens, {"duration-hover"}, "a { --duration-hover: 1ms; }", prefix="mob-")
        self.assertEqual(stale, ["duration-hover"])

    @unittest.skipUnless(SITE.exists(), "mobster.dev isn't in this tree")
    def test_the_sites_ui_folder_is_generated_or_listed(self):
        import json
        manifest = json.loads((ROOT / "scripts" / "ui-manifest.json").read_text())
        found = sorted(path.name for path in (ROOT / manifest["to"]).iterdir())
        self.assertEqual(found, sorted(manifest["files"] + manifest["site_only"]))

    def test_mixes_resolve_over_paper(self):
        self.assertEqual(brand.LIGHT["card"], "#F0F0F0")
        self.assertEqual(brand.DARK["violet-wash"], "#232038")
        self.assertEqual(brand.RAW["dark"]["scrim"], "#060606 62%")
        self.assertEqual(brand.mix("#FFFFFF", .5, "#000000"), "#808080")
        self.assertIn("--card: color-mix(in srgb, var(--ink) 3.5%, var(--paper));", brand.STYLESHEET)
        self.assertIn("--violet-wash: color-mix(in srgb, var(--violet) 16%, transparent);", brand.STYLESHEET)
        self.assertIn("--focus-ring: var(--violet);", brand.STYLESHEET)


def _contrast(one, two):
    def luminance(colour):
        channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4 for c in channels]
        return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]
    high, low = sorted((luminance(one), luminance(two)), reverse=True)
    return (high + .05) / (low + .05)


class FocusRingTests(unittest.TestCase):
    def test_the_focus_ring_is_three_to_one_on_every_surface_in_both_appearances(self):
        for name, palette in (("light", brand.LIGHT), ("dark", brand.DARK)):
            for surface in ("paper", "sunken", "surface", "card"):
                with self.subTest(appearance=name, surface=surface):
                    self.assertGreaterEqual(_contrast(palette["focus-ring"], palette[surface]), 3.0)


class SurfacesAgreeTests(unittest.TestCase):
    def assertGenerated(self, path, schemes):
        css = path.read_text()
        found = UI_REGION.findall(css)
        self.assertEqual([scheme for scheme, _ in found], schemes, f"{path.relative_to(ROOT)}'s as=ui regions")
        generated = {name for _, body in found for name in re.findall(r"--([\w-]+):", body)}
        rest = re.sub(r"/\*.*?\*/", "", UI_REGION.sub("", css), flags=re.S)
        roots = "".join(re.findall(r"(?m)^\s*(?::root|\[data-theme)[^{}]*\{([^{}]*)\}", rest))
        again = sorted(name for name in generated
                       if re.search(rf"--{re.escape(name)}\s*:", roots))
        self.assertEqual(again, [], f"{path.relative_to(ROOT)} declares what docs/brand/tokens.json generates")

    @unittest.skipUnless(SITE.exists(), "mobster.dev isn't in this tree")
    def test_the_sites_palette_and_names_are_generated(self):
        self.assertGenerated(SITE, ["light", "dark", "dark", "common", "common"])

    @unittest.skipUnless(APP.exists(), "the Mac app isn't in this tree")
    def test_the_apps_palette_and_names_are_generated(self):
        self.assertGenerated(APP, ["dark", "light", "light", "common"])

    @unittest.skipUnless(SITE.exists() and APP.exists(), "needs mobster.dev and the Mac app")
    def test_both_surfaces_get_the_same_declarations(self):
        def regions(path):
            return {scheme: [line.strip() for line in body.strip().splitlines()]
                    for scheme, body in UI_REGION.findall(path.read_text())}
        self.assertEqual(regions(SITE), regions(APP))

    @unittest.skipUnless(SITE.exists(), "mobster.dev isn't in this tree")
    def test_the_site_sets_only_the_sources_weights(self):
        allowed = set(brand.WEIGHT.values())
        found = []
        paths = [path for folder in ("app", "components") for path in (ROOT / "site" / folder).rglob("*")
                 if path.suffix in (".css", ".tsx")]
        for path in sorted(paths):
            text = path.read_text()
            if path.name == "landing-frames.css":
                text = re.sub(r"\.lf-du-[^{]*\{[^}]*\}", lambda rule: "\n" * rule.group().count("\n"), text)
            if path.name == "phone-screens.css":  # Amazon's, the lesson app's and Apple's Settings own type
                text = re.sub(r"\.(?:az-|ls[\s.{:,-]|st-)[^{]*\{[^}]*\}",
                              lambda rule: "\n" * rule.group().count("\n"), text)
            if path.name == "hero-phone.css":
                continue
            for match in re.finditer(r"font-weight:\s*(\d+)|font-\[(\d+)\]|fontWeight=\"(\d+)\"", text):
                value = int(next(group for group in match.groups() if group))
                if value not in allowed:
                    found.append(f"{path.relative_to(ROOT)}:{text.count(chr(10), 0, match.start()) + 1} {value}")
        self.assertEqual(found, [], f"weights off the scale {sorted(allowed)}")

    @unittest.skipUnless(APP.exists(), "the Mac app isn't in this tree")
    def test_the_apps_weights_are_the_source(self):
        found = {name: int(value) for name, value in re.findall(r"--weight-([\w-]+):\s*(\d+);", APP.read_text())}
        weight = brand.WEIGHT
        expected = {"regular": weight["body"], "medium": weight["medium"], "semibold": weight["semibold"]}
        self.assertEqual(found, expected)

    @unittest.skipUnless(APP.exists(), "the Mac app isn't in this tree")
    def test_the_apps_spaces_are_the_source(self):
        found = {int(step): int(value) for step, value in re.findall(r"--space-(\d+):\s*(\d+)px", APP.read_text())}
        self.assertEqual(sorted(set(found.values())), brand.SPACE)
        self.assertEqual({step: step * 4 for step in found}, found)

    def test_the_clis_colours_are_the_source(self):
        for palette, source in ((style.DARK, brand.DARK), (style.LIGHT, brand.LIGHT)):
            self.assertEqual(palette, {"accent": source["violet-text"], "secondary": source["ink-2"],
                                       "tertiary": source["ink-3"], "green": source["green"],
                                       "amber": source["amber"], "coral": source["coral"]})


@unittest.skipUnless((ROOT / "dashboard" / "src" / "components" / "ui" / "ui.css").exists(),
                     "the Mac app isn't in this tree")
class PrimitivesRegionTests(unittest.TestCase):
    OWN = generator().HOOKS

    def region(self):
        gen = generator()
        return gen, gen.primitives_css(gen.load())

    def test_a_region_becomes_the_primitives(self):
        gen = generator()
        text = gen.primitive_regions("a { }\n/* brand:primitives */ stale /* /brand:primitives */\nb { }", gen.load())
        self.assertTrue(text.startswith("a { }\n/* brand:primitives */\n:root{") and text.endswith("*/\nb { }"))
        self.assertIn('@layer components{[data-slot="button"]{', text)
        self.assertIn('[data-slot="button"][data-variant="default"]{background:var(--text-primary);'
                      'color:var(--on-primary)}', text)

    def test_every_name_the_sections_read_has_a_value(self):
        gen = generator()
        tokens = gen.load()
        css = gen.primitives_css(tokens)
        written = set(re.findall(r"--([\w-]+):", css)) | set(re.findall(r"--([\w-]+):", gen.stylesheet(tokens)))
        self.assertEqual(sorted(set(re.findall(r"var\(--([\w-]+)", css)) - written - self.OWN), [])

    def test_a_surface_gets_only_the_rules_it_can_reach(self):
        gen = generator()
        tokens = gen.load()
        css = gen.primitives_css(tokens, "relay/src/pages")
        for kept in ('[data-slot="button"][data-variant="default"]{', '[data-size="lg"]{',
                     '.ui-choice[data-mark="none"]{', ".ui-field-label{"):
            self.assertIn(kept, css)
        for dropped in (".ui-badge", ".ui-card", '[data-variant="destructive"]', '[data-size="xs"]',
                        'data-slot="dialog-content"',
                        '[type="checkbox"]', "--control-xs", "--status-green"):
            self.assertNotIn(dropped, css)
        self.assertLess(len(css), 0.7 * len(gen.primitives_css(tokens)))
        written = set(re.findall(r"--([\w-]+):", css)) | set(re.findall(r"--([\w-]+):", gen.stylesheet(tokens)))
        self.assertEqual(sorted(set(re.findall(r"var\(--([\w-]+)", css)) - written - self.OWN), [])

    def test_the_tokens_region_keeps_what_the_primitives_read(self):
        gen = generator()
        tokens = gen.load()
        text = ("/* brand:tokens scan=relay/src/pages */\nx\n/* /brand:tokens */\n"
                "/* brand:primitives scan=relay/src/pages */\ny\n/* /brand:primitives */\n")
        path = ROOT / "relay" / "public" / "assets" / "remote.css"
        once = gen.regions(gen.primitive_regions(text, tokens), tokens, path)
        self.assertIn("--focus-ring:", once)
        self.assertIn("--shadow-button-secondary:", once)
        self.assertEqual(gen.regions(gen.primitive_regions(once, tokens), tokens, path), once)

    def test_a_page_gets_the_apps_names_its_own_rules_read(self):
        gen = generator()
        tokens = gen.load()
        text = gen.primitive_regions("/* brand:primitives scan=relay/src/pages */\n/* /brand:primitives "
                                     "*/\nh1{font-size:var(--text-hero)}", tokens)
        self.assertIn("--text-hero:32px;", text)
        self.assertNotIn("--leading-hero:", text)


HERO_READERS = {
    "dashboard/src/features/first-run/first-run.css",
    "relay/public/assets/remote.css",
    "services/license-api/public/paid.html",
    "services/license-api/public/back.html",
}


@unittest.skipUnless((ROOT / "relay").exists() and APP.exists(), "Remote or the Mac app isn't in this tree")
class HeroSizeTests(unittest.TestCase):
    def test_only_whole_window_screens_read_the_hero_size(self):
        readers = set()
        for folder in ("dashboard/src", "site/app", "site/components", "relay/src", "relay/public",
                       "services/license-api"):
            for file in (ROOT / folder).rglob("*"):
                if file.suffix in {".css", ".ts", ".tsx", ".html", ".mjs", ".js"} and "node_modules" not in file.parts \
                        and file.is_file() and "var(--text-hero)" in file.read_text(errors="replace"):
                    readers.add(file.relative_to(ROOT).as_posix())
        self.assertEqual(sorted(readers - {"dashboard/src/design-tokens.test.ts"}), sorted(HERO_READERS))

    def test_remotes_title_is_the_token_not_a_copy_of_its_value(self):
        css = (ROOT / "relay" / "public" / "assets" / "remote.css").read_text()
        page = gen_free(css)
        self.assertIn("h1{font-size:var(--text-hero);line-height:var(--leading-hero);", page)
        self.assertNotRegex(page, r"font-size:\s*32px")
        self.assertRegex(css, r"--text-hero:32px;--leading-hero:40px;")


def gen_free(css):
    return re.sub(r"/\* brand:(tokens|primitives)[^*]*\*/.*?/\* /brand:(tokens|primitives) \*/", "", css, flags=re.S)


BOOK = ROOT / "product" / "BRAND.md"
KIT = ROOT / "docs" / "brand"


def indexed(book: str) -> set[str]:
    section = book.split("## 8. Assets", 1)[1].split("\n## ", 1)[0]
    names = set()
    for row in section.splitlines():
        if not row.startswith("| `"):
            continue
        folder = ""
        for token in re.findall(r"`([^`]+)`", row.split(" | ", 1)[0]):
            if "/" in token:
                folder = token if token.endswith("/") else token.rsplit("/", 1)[0] + "/"
                names.add(token)
            else:
                names.add(folder + token)
    return names


@unittest.skipUnless(BOOK.exists(), "the brand book isn't in this tree")
class BrandBookTest(unittest.TestCase):
    def setUp(self):
        self.book = BOOK.read_text()

    def test_every_picture_is_in_the_kit(self):
        shown = re.findall(r'(?:src|srcset)="(\.\./docs/brand/[^"]+)"', self.book)
        self.assertGreaterEqual(len(shown), 8)
        self.assertEqual([path for path in shown if not (BOOK.parent / path).is_file()], [])

    def test_the_index_names_every_file_and_only_real_ones(self):
        names = indexed(self.book)
        folders = tuple(name for name in names if name.endswith("/"))
        files = sorted(path.relative_to(KIT).as_posix() for path in KIT.rglob("*")
                       if path.is_file() and path.name != ".DS_Store")
        self.assertEqual([path for path in files if path not in names and not path.startswith(folders)], [])
        self.assertEqual(sorted(name for name in names if not name.endswith("/") and not (KIT / name).is_file()), [])


REGISTRY = ROOT / "docs" / "brand" / "art" / "variants.json"
README = ROOT / "docs" / "brand" / "art" / "README.md"
PLATES = ROOT / "docs" / "brand" / "art" / "plates.json"


@unittest.skipUnless(REGISTRY.exists(), "the art registry isn't in this tree")
class BrandArtVariantsTest(unittest.TestCase):
    def setUp(self):
        self.registry = json.loads(REGISTRY.read_text())
        self.variants = self.registry["variants"]

    def test_approval_is_all_or_nothing(self):
        for name, v in self.variants.items():
            with self.subTest(variant=name):
                self.assertIsInstance(v["approved"], bool)
                if v["approved"]:
                    self.assertEqual(v["approvedBy"], "owner")
                    date.fromisoformat(v["approvedOn"])
                    self.assertTrue(v["seenAs"])
                else:
                    self.assertIsNone(v["approvedOn"])
                    self.assertIsNone(v["approvedBy"])

    def test_every_variant_is_complete(self):
        for name, v in self.variants.items():
            with self.subTest(variant=name):
                self.assertRegex(name, r"^[a-z][a-z0-9-]*$")
                self.assertIn(v["ground"]["kind"], {"plate", "paper", "deep", "night"})
                self.assertIn(v["theme"], {"light", "dark"})
                self.assertIn("layout", v["lockup"])
                plates = re.findall(r"generated/[^\"]+", json.dumps(v))
                self.assertTrue(all("{category}" in p for p in plates), plates)

    def test_every_approved_variant_has_a_cover(self):
        for name, v in self.variants.items():
            if not v["approved"]:
                continue
            with self.subTest(variant=name):
                cover = v["cover"]
                self.assertEqual(cover["calm"], [], "a cover carries no type, so it has no calm zones")
                self.assertEqual(cover["cut"] is None, v["cut"] is None,
                                 "a cover keeps its variant's cut, or its lack of one")

    def test_every_plate_is_recorded(self):
        plates = json.loads(PLATES.read_text())["plates"]
        self.assertTrue(plates)
        for name, p in plates.items():
            with self.subTest(plate=name):
                self.assertRegex(name, r"^[a-z][a-z0-9-]*$")
                self.assertIn(p["category"], self.registry["categories"])
                self.assertIn(p["material"], {"glaze", "glass"})
                self.assertRegex(p["family"], r"^[a-z]+$")
                date.fromisoformat(p["made"])
                self.assertIn("Codex", p["generator"])
                self.assertEqual(len(p["native"]), 2)
                self.assertTrue(p["master"].endswith(".png") and p["prompt"].endswith(".md"))
                for k, x in p.get("tone", {}).items():
                    self.assertIn(k, {"saturation", "dim"})
                    self.assertTrue(0 < x <= 1)

    def test_readme_lists_every_variant(self):
        rows = re.findall(r"^\| `([a-z0-9-]+)` \|", README.read_text(), re.M)
        self.assertEqual(sorted(set(rows)), sorted(self.variants))


if __name__ == "__main__":
    unittest.main()
