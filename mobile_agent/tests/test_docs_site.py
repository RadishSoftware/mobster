import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from mobile_agent import brand

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gen_docs_reference.py"
DOCS = ROOT / "docs"
REFERENCE = DOCS / "reference"
SITE = DOCS / "docs.json"
GUIDES = ("PRODUCT.md", "MESSAGING.md", "BRAND.md", "STYLE.md", "VISUAL-LANGUAGE.md")


def load_generator():
    spec = importlib.util.spec_from_file_location("gen_docs_reference", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SCRIPT.is_file() and REFERENCE.is_dir(), "the docs site is not part of this copy")
class GeneratedReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = load_generator()

    def test_exclusive_numeric_bounds_are_both_documented(self):
        self.assertEqual(self.generator.type_of({"type": "number", "exclusiveMinimum": 0,
                                                "exclusiveMaximum": 100}),
                         "number (more than 0, less than 100)")

    def test_the_committed_pages_match_the_code(self):
        stale = [path.relative_to(ROOT).as_posix() for path, _ in self.generator.stale()]
        self.assertEqual(stale, [], f"Out of date with the code: {', '.join(stale)}. Run "
                                   f"`{self.generator.COMMAND}` and commit the result.")

    def test_every_command_option_and_tool_is_on_its_page(self):
        cli_page, tools_page = (REFERENCE / "cli.mdx").read_text(), (REFERENCE / "mcp-tools.mdx").read_text()

        def fields(text):
            names = [json.loads(value) for value in re.findall(r'<ResponseField name=\{("(?:[^"\\]|\\.)*")\}', text)]
            return {part for name in names for part in name.split(", ")}
        documented = fields(cli_page)
        parser, _ = self.generator.build_parser()

        def walk(prog, parser):
            yield prog, parser
            for name, _help, sub in self.generator.commands_of(parser):
                yield from walk(f"{prog} {name}", sub)
        for prog, sub in walk("mobster", parser):
            with self.subTest(command=prog):
                named = f"## `{prog}`" in cli_page or f"**`{prog}`**" in cli_page
                self.assertTrue(named, f"{prog} has no heading or bold name")
                for action in self.generator.visible(sub):
                    for option in action.option_strings:
                        self.assertIn(option, documented)
        tools, _ = self.generator.tool_list()
        start = next(tool for tool in tools if tool["name"] == "verify_start")
        self.assertIn("smart", start["inputSchema"]["properties"], "the Smart arguments are documented too")
        for tool in tools:
            section = tools_page.split(f"## `{tool['name']}`\n", 1)
            with self.subTest(tool=tool["name"]):
                self.assertEqual(len(section), 2, "the tool has no section")
                arguments = fields(section[1].split("\n## ", 1)[0])
                self.assertEqual(arguments, set(tool["inputSchema"].get("properties", {})))

    def test_every_mobster_command_in_a_page_parses(self):
        """A `mobster …` line in a page's shell block is one the parser takes, so a reader who copies it runs it."""
        with mock.patch.dict(os.environ, {"MOBSTER_EXPERIMENTAL_ANDROID": "1"}):
            parser, _ = self.generator.build_parser()
        for page in sorted(DOCS.rglob("*.mdx")):
            if REFERENCE in page.parents:
                continue
            for block in re.findall(r"```(?:sh|bash|zsh|shell)\b[^\n]*\n(.*?)```", page.read_text(), re.S):
                for line in re.sub(r"\\\n\s*", " ", block).splitlines():
                    line = re.split(r"\s(?:\||&&|;|>|2>|#)", line.strip())[0]
                    if not line.startswith("mobster "):
                        continue
                    argv = shlex.split(re.sub(r"<[^>]+>", "X", line).replace("…", "x"))[1:]
                    if argv[:1] == ["import"]:
                        argv = ["test"] + argv  # __main__ runs `mobster import …` as `mobster test import …`
                    err = io.StringIO()
                    with self.subTest(page=str(page.relative_to(DOCS)), command=line):
                        try:
                            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                                parser.parse_args(argv)
                        except SystemExit as stop:
                            self.assertIn(stop.code, (0, None), err.getvalue().strip())

    def test_every_command_and_flag_named_in_a_pages_words_exists(self):
        """An inline `mobster …` may leave out what a command requires, or be a synopsis, but it names a real command
        and real flags. A code block's lines are held to more, above."""
        with mock.patch.dict(os.environ, {"MOBSTER_EXPERIMENTAL_ANDROID": "1"}):
            parser, _ = self.generator.build_parser()
        for page in sorted(DOCS.rglob("*.mdx")):
            if REFERENCE in page.parents:
                continue
            words = re.sub(r"```.*?```", "", page.read_text(), flags=re.S)
            # `mobster intents` is behind its own flag, which its page names. A command a page says doesn't exist
            # ("There is no separate `mobster mcp uninstall`") is left out.
            for span in re.findall(r"(?<!\bno separate )(?<!\bno )`mobster ((?!intents )[^`\n]+)`", words):
                line = re.sub(r"<[^>]+>", "X", re.split(r"\s(?:\||&&|;|>|<|2>|#)", span)[0]).replace("…", "x")
                try:
                    argv = shlex.split(line)
                except ValueError:
                    continue
                if argv[:1] == ["import"]:
                    argv = ["test"] + argv  # __main__ runs `mobster import …` as `mobster test import …`
                err = io.StringIO()
                with self.subTest(page=str(page.relative_to(DOCS)), command=span):
                    try:
                        with redirect_stderr(err), redirect_stdout(io.StringIO()):
                            parser.parse_args(argv)
                    except SystemExit:
                        self.assertNotRegex(err.getvalue(), r"unrecognized arguments|isn't a command")

    def test_the_pages_hold_nothing_from_this_machine(self):
        for page in REFERENCE.glob("*.mdx"):
            text = page.read_text()
            with self.subTest(page=page.name):
                self.assertNotIn(str(Path.home()), text)
                self.assertNotIn(self.generator.MODEL_SENTINEL, text)
                self.assertNotRegex(text, r"/Users/|/private/|/var/folders/")


def pages_in(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from pages_in(item)
    elif isinstance(node, dict):
        for key in ("pages", "groups", "tabs", "anchors", "dropdowns", "menu"):
            if key in node:
                yield from pages_in(node[key])


@unittest.skipUnless(SITE.is_file(), "the docs site is not part of this copy")
class SiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads(SITE.read_text())
        cls.pages = list(pages_in(cls.config["navigation"]))

    def page_file(self, page):
        return next((DOCS / f"{page}{suffix}" for suffix in (".mdx", ".md") if (DOCS / f"{page}{suffix}").is_file()),
                    None)

    def test_every_navigation_entry_is_a_page_and_is_listed_once(self):
        for page in self.pages:
            with self.subTest(page=page):
                self.assertIsNotNone(self.page_file(page), f"docs.json names {page}, but docs/ has no such page")
        self.assertEqual(len(self.pages), len(set(self.pages)), "a page is listed twice in docs.json")

    def test_every_public_page_is_in_the_navigation(self):
        public = {p.relative_to(DOCS).with_suffix("").as_posix() for p in DOCS.rglob("*.md*")
                  if p.suffix in (".md", ".mdx") and p.name != "README.md"
                  and "node_modules" not in p.parts and "snippets" not in p.parts}
        self.assertEqual(sorted(public - set(self.pages)), [], "add these pages to docs/docs.json's navigation")

    def test_no_published_page_names_an_internal_file(self):
        for page in self.pages:
            text = self.page_file(page).read_text()
            with self.subTest(page=page):
                self.assertNotRegex(text, r"\b(PRODUCT|MESSAGING|BRAND|STYLE)\.md\b",
                                    "a published page names an internal file")

    def test_every_public_page_has_a_title_and_description(self):
        for page in self.pages:
            text = self.page_file(page).read_text()
            with self.subTest(page=page):
                match = re.match(r"---\n(.*?)\n---\n", text, re.S)
                self.assertTrue(match, "the page needs YAML frontmatter")
                self.assertRegex(match[1], r"(?m)^title: \S")
                self.assertRegex(match[1], r"(?m)^description: \S")

    def test_markdown_pages_parse_as_mdx(self):
        for page in self.pages:
            path = self.page_file(page)
            if path.suffix != ".md":
                continue
            text = re.sub(r"^---\n.*?\n---\n", "", path.read_text(), flags=re.S)
            text = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1", lambda m: "\n" * m.group(0).count("\n"), text)
            for number, line in enumerate(text.split("\n"), 1):
                prose = re.sub(r"(`+)(?:(?!\1).)+\1", "", line)
                with self.subTest(page=path.name, line=number):
                    self.assertNotRegex(prose, r"(?<!\\)<(?:[A-Za-z]|!--)|(?<!\\)[{}]",
                                        f"{path.name}: this breaks the docs site's MDX parser: {line.strip()[:120]}")

    def test_no_two_sidebar_entries_share_a_title(self):
        names = {}
        for page in self.pages:
            front = re.match(r"---\n(.*?)\n---\n", self.page_file(page).read_text(), re.S)[1]
            if re.search(r"(?m)^hidden: true$", front):
                continue
            name = re.search(r'(?m)^sidebarTitle: "?(.*?)"?$', front) or re.search(r'(?m)^title: "?(.*?)"?$', front)
            names.setdefault(name[1], []).append(page)
        self.assertEqual({name: found for name, found in names.items() if len(found) > 1}, {})

    def test_changelog_entries_have_unique_labels(self):
        changelog = DOCS / "changelog.mdx"
        if not changelog.is_file():
            self.skipTest("no changelog")
        labels = re.findall(r'<Update label="([^"]+)"', changelog.read_text())
        self.assertEqual(sorted({label for label in labels if labels.count(label) > 1}), [])

    def test_the_site_holds_nothing_personal(self):
        files = [SITE] + [self.page_file(page) for page in self.pages
                                                if self.page_file(page).suffix == ".mdx"]
        for path in files:
            with self.subTest(file=path.name):
                self.assertNotRegex(path.read_text(), r"/Users/(?!you/)|/private/var/|[0-9A-F]{8}-[0-9A-F]{16}")

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists() and (ROOT / ".gitattributes").is_file(),
                         "not a git checkout, or the public tree, which has no .gitattributes and no internal guides")
    def test_the_public_tree_drops_the_internal_guides(self):
        guides = [f"product/{name}" for name in GUIDES]
        files = guides + ["docs/docs.json", "docs/index.mdx", "docs/reference/cli.mdx"]
        out = subprocess.run(["git", "-C", str(ROOT), "check-attr", "export-ignore", "--", *files],
                             capture_output=True, text=True, check=True).stdout
        state = dict(re.findall(r"^(\S+): export-ignore: (\S+)$", out, re.M))
        for name in guides:
            self.assertEqual(state[name], "set", f"{name} would reach the public tree")
        for public in files[len(guides):]:
            self.assertNotEqual(state[public], "set", f"{public} would be left out of the public tree")

@unittest.skipUnless(DOCS.is_dir(), "the docs site is not part of this copy")
class MoneyEscapingTests(unittest.TestCase):
    BARE_AMOUNT = re.compile(r"(?<![\\$\w])\$(?=\d)")
    LITERAL = re.compile(r"`[^`]*`|\{\"[^\"]*\"\}")  # inline code and a JSX string such as {"$12"}

    def test_no_money_amount_is_left_unescaped_outside_code(self):
        bare = []
        for path in sorted(DOCS.rglob("*.mdx")):
            fence, frontmatter = None, False
            for number, line in enumerate(path.read_text().split("\n"), 1):
                if number == 1 and line == "---":
                    frontmatter = True
                    continue
                if frontmatter:
                    frontmatter = line != "---"
                    if not re.match(r"(title|description):", line):
                        continue
                elif fence:
                    fence = None if line.lstrip().startswith(fence) else fence
                    continue
                elif line.lstrip().startswith(("```", "~~~")):
                    fence = line.lstrip()[:3]
                    continue
                if self.BARE_AMOUNT.search(self.LITERAL.sub("", line)):
                    bare.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
        self.assertEqual(bare, [], "A bare $<digit> can render as math on the docs site. Write \\$ before the amount "
                                   "(\\\\$ inside a double-quoted title or description): " + ", ".join(bare))


MOST_CODE_LINES = 1000


class PagesCompileTests(unittest.TestCase):
    """Mintlify shows "A parsing error occurred" for a page its MDX compiler can't build. Two causes have done it: a
    line outside code that starts with `import` or `export` (MDX reads it as ESM), and one code block so long that the
    compiler overflows its stack (an 18,000-line OpenAPI document did)."""

    def pages(self):
        return [p for p in DOCS.rglob("*.mdx") if "node_modules" not in p.parts]

    def test_no_line_outside_code_starts_an_esm_statement(self):
        bad = []
        for page in self.pages():
            fenced = False
            for number, line in enumerate(page.read_text().splitlines(), 1):
                if line.lstrip().startswith("```"):
                    fenced = not fenced
                elif (not fenced and re.match(r"(import|export)\b", line)
                      and not re.match(r"import .+ from ['\"]/", line)):
                    bad.append(f"{page.relative_to(ROOT).as_posix()}:{number}")
        self.assertEqual(bad, [], "write &#101;xport or &#105;mport, or put the line in code: " + ", ".join(bad))

    def test_no_code_block_is_long_enough_to_overflow_the_compiler(self):
        bad = []
        for page in self.pages():
            size, fenced = 0, False
            for line in page.read_text().splitlines():
                if line.lstrip().startswith("```"):
                    if fenced and size > MOST_CODE_LINES:
                        bad.append(f"{page.relative_to(ROOT).as_posix()} ({size} lines)")
                    fenced, size = not fenced, 0
                elif fenced:
                    size += 1
        self.assertEqual(bad, [],
                         f"a code block over {MOST_CODE_LINES} lines breaks Mintlify's compiler: link it instead")


STYLE = DOCS / "style.css"
TOKENS = DOCS / "brand" / "tokens.json"


@unittest.skipUnless(STYLE.is_file() and TOKENS.is_file(), "the docs site is not part of this copy")
class StylesheetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = json.loads(TOKENS.read_text())
        css = re.sub(r"/\* brand:tokens[^*]*\*/.*?/\* /brand:tokens \*/", "", STYLE.read_text(), flags=re.S)
        cls.by_hand = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    def declarations(self):
        for body in re.findall(r"\{([^{}]*)\}", self.by_hand):
            yield from re.findall(r"([\w-]+)\s*:\s*([^;]+)", body)

    def test_the_hand_kept_part_writes_no_colour_of_its_own(self):
        raw = [f"{prop}: {value}" for prop, value in self.declarations()
               if re.search(r"#[0-9A-Fa-f]{3,8}\b|\b(?:rgba?|hsla?|oklch|oklab)\(", value)]
        self.assertEqual(raw, [], "use a --mob-* colour from the brand:tokens region")

    def test_every_weight_is_one_of_the_three(self):
        allowed = {f"var(--mob-weight-{name})" for name in ("body", "medium", "semibold")}
        found = [f"{prop}: {value.strip()}" for prop, value in self.declarations()
                 if (prop == "font-weight" or prop.startswith("--font-weight-")) and value.strip() not in allowed]
        self.assertEqual(found, [], f"weights are {sorted(allowed)} (400, 500 and 600)")

    def test_the_faces_are_served_from_the_docs(self):
        config = json.loads(SITE.read_text())
        sans = self.tokens["font"]["sans"]
        self.assertEqual(config["fonts"],
                         {"family": sans["name"], "source": f"/fonts/{sans['file']}", "format": "woff2"})
        for font in (sans, self.tokens["font"]["mono"]):
            with self.subTest(face=font["name"]):
                self.assertTrue((DOCS / "fonts" / font["file"]).is_file())
        self.assertNotRegex(STYLE.read_text(), r"@import|https?://", "no stylesheet or face from another host")

    def test_mintlifys_greys_are_the_brands(self):
        css = STYLE.read_text()
        region = re.search(r"/\* brand:tokens as=mintlify \*/(.*?)/\* /brand:tokens \*/", css, re.S).group(1)
        for selector, palette in ((":root:not(.dark)", brand.LIGHT), (":root.dark", brand.DARK)):
            block = re.search(re.escape(selector) + r"\{([^}]*)\}", region).group(1)
            steps = dict(re.findall(r"--gray-(\d+):(\d+ \d+ \d+);", block))
            self.assertEqual(sorted(map(int, steps)), [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950], selector)
            for step, channels in steps.items():
                colour = "#" + "".join(f"{int(c):02X}" for c in channels.split())
                with self.subTest(appearance=selector, step=step):
                    self.assertIn(colour, palette.values())

    def test_code_takes_the_code_roles(self):
        self.assertEqual(json.loads(SITE.read_text())["styling"]["codeblocks"], {"theme": "css-variables"})
        region = re.search(r"/\* brand:tokens as=mintlify \*/(.*?)/\* /brand:tokens \*/", STYLE.read_text(),
                           re.S).group(1)
        code = self.tokens["code"]
        for selector, palette in ((":root:not(.dark)", brand.LIGHT), (":root.dark", brand.DARK)):
            block = re.search(re.escape(selector) + r"\{([^}]*)\}", region).group(1)
            found = dict(re.findall(r"--mint-([\w-]+):(#[0-9a-f]{6});", block))
            allowed = {palette[name].lower() for name in [*code["role"].values(), code["plate"]]}
            with self.subTest(appearance=selector):
                self.assertTrue({"foreground", "background", "token-keyword", "token-string",
                                 "token-comment"} <= set(found))
                self.assertLessEqual(set(found.values()), allowed)


ERRORS = DOCS / "troubleshooting"
STRUCTURED = DOCS / "structured-data.js"


def frontmatter(path):
    import yaml
    return yaml.safe_load(re.match(r"---\n(.*?)\n---\n", path.read_text(), re.S)[1])


@unittest.skipUnless(SITE.is_file() and ERRORS.is_dir(), "the docs site is not part of this copy")
class AnswerEngineTests(unittest.TestCase):
    """The pages answer engines quote: one page per printed error, the fact sheet and the structured data."""

    @classmethod
    def setUpClass(cls):
        cls.generator = load_generator()
        cls.errors = sorted(ERRORS.glob("*.mdx"))

    def printed_text(self):
        base = ROOT / "mobile_agent"
        sources = [*base.glob("*.py"), *base.glob("wda/*.py"), *(base / "tests" / "setup_errors").glob("*.txt")]
        text = "\n".join(path.read_text(errors="ignore") for path in sources)
        return re.sub(r'"\s*f?"', "", text)  # a message split over adjacent string literals reads as one

    def test_each_error_page_is_titled_with_a_string_mobster_or_xcode_prints(self):
        printed = self.printed_text()
        for path in self.errors:
            title = frontmatter(path)["title"]
            # The helper's address is the one part Mobster fills in at run time.
            wanted = title.split(" at http")[0].rstrip(".")
            with self.subTest(page=path.name):
                self.assertIn(wanted.lower(), printed.lower(), "no message Mobster or xcodebuild prints is this")

    def test_each_error_page_has_fix_steps_a_stamp_and_a_link_from_troubleshooting(self):
        index = (DOCS / "troubleshooting.mdx").read_text()
        for path in self.errors:
            text = path.read_text()
            meta = frontmatter(path)
            with self.subTest(page=path.name):
                self.assertRegex(text, r"(?m)^1\. ", "the fix is a numbered list, which is the page's HowTo")
                self.assertRegex(text, r"(?m)^Checked \d{1,2} \w{3} 2026 against Mobster \d+\.\d+\.\d+\.")
                self.assertRegex(str(meta["article:modified_time"]), r"^2026-\d\d-\d\d$")
                self.assertIn(f"(/troubleshooting/{path.stem})", index)

    def test_the_fact_sheet_figures_are_on_the_page_that_holds_them(self):
        for text, page, figures in self.generator.FACTS:
            source = next(p for p in (DOCS / f"{page}.mdx", DOCS / f"{page}.md") if p.is_file())
            body = source.read_text().replace("\\$", "$")
            for figure in figures:
                with self.subTest(page=page, figure=figure):
                    self.assertIn(figure, body)
                    self.assertIn(figure, text)

    def test_a_page_held_back_from_search_stays_out_of_llms_txt(self):
        text = (DOCS / "llms.txt").read_text()
        for path in sorted(p for p in DOCS.rglob("*.mdx") if "snippets" not in p.parts):
            page = path.relative_to(DOCS).with_suffix("").as_posix()
            with self.subTest(page=page):
                if frontmatter(path).get("noindex"):
                    self.assertNotIn(f"/{page}.md", text)
                    self.assertTrue(frontmatter(path).get("hidden"), "held back from search but still in the sidebar")

    def test_llms_txt_lists_only_links_verified_on_a_phone(self):
        from mobile_agent import deeplinks
        lines, unverified = self.generator.verified_links()
        self.assertEqual(len(lines), sum(1 for link in deeplinks.LINKS if link.verified))
        self.assertEqual(unverified, sum(1 for link in deeplinks.LINKS if not link.verified))
        text = (DOCS / "llms.txt").read_text()
        for line in lines:
            self.assertIn(line, text)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_structured_data_builds_the_right_types(self):
        script = (f"const {{build}}=require({json.dumps(str(STRUCTURED))});"
                  "const page=(path,extra)=>Object.assign({path,title:'T',description:'d',modified:'2026-10-26',"
                  "steps:[],gifs:[]},extra);"
                  "console.log(JSON.stringify([build(page('/')),"
                  "build(page('/troubleshooting/x',{steps:['a','b'],gifs:[{src:'/images/run.gif',name:'Run'}]})),"
                  "build(page('/troubleshooting/x',{steps:['a'],modified:'',gifs:[{src:'/images/run.gif'}]})),"
                  "build(page('/safety'))]))")
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
        home, fix, undated, plain = json.loads(out.stdout)
        self.assertEqual([n["@type"] for n in home["@graph"]], ["SoftwareApplication"])
        self.assertEqual([n["@type"] for n in fix["@graph"]], ["HowTo", "VideoObject"])
        self.assertEqual([s["position"] for s in fix["@graph"][0]["step"]], [1, 2])
        self.assertEqual([n["@type"] for n in undated["@graph"]], ["HowTo"], "a GIF without a date is no VideoObject")
        self.assertIsNone(plain)

    def test_structured_data_prices_are_the_published_plans(self):
        pricing = (DOCS / "pricing.mdx").read_text().replace("\\$", "$")
        for price in re.findall(r'offer\("(\d+)"', STRUCTURED.read_text()):
            self.assertIn(f"${price}", pricing)


if __name__ == "__main__":
    unittest.main()
