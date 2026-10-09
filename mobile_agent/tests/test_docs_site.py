"""The docs site (docs/docs.json, Mintlify): its generated reference pages match the code (docs/reference/cli.mdx from
the argparse parsers, docs/reference/mcp-tools.mdx from the MCP tool list), its navigation names every public page
and only those, and the internal guides stay off both the site and the public tree. Offline: no device, no server."""

import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gen_docs_reference.py"
DOCS = ROOT / "docs"
REFERENCE = DOCS / "reference"
SITE = DOCS / "docs.json"
# Internal guides that live in docs/ but are never published: not on the site, not in the public tree.
INTERNAL = ("BRAND.md", "STYLE.md")


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

    def test_the_committed_pages_match_the_code(self):
        stale = [path.relative_to(ROOT).as_posix() for path, _ in self.generator.stale()]
        self.assertEqual(stale, [], f"Out of date with the code: {', '.join(stale)}. Run "
                                   f"`{self.generator.COMMAND}` and commit the result.")

    def test_every_command_option_and_tool_is_on_its_page(self):
        cli_page, tools_page = (REFERENCE / "cli.mdx").read_text(), (REFERENCE / "mcp-tools.mdx").read_text()

        def fields(text):
            """The names of the page's ResponseFields: `-n, --limit` gives both options."""
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
                self.assertIn(f"## `{prog}`", cli_page)
                for action in self.generator.visible(sub):
                    for option in action.option_strings:
                        self.assertIn(option, documented)
        tools, _ = self.generator.tool_list()
        self.assertIn("verify", [tool["name"] for tool in tools], "the Smart tool is documented too")
        for tool in tools:
            section = tools_page.split(f"## `{tool['name']}`\n", 1)
            with self.subTest(tool=tool["name"]):
                self.assertEqual(len(section), 2, "the tool has no section")
                arguments = fields(section[1].split("\n## ", 1)[0])
                self.assertEqual(arguments, set(tool["inputSchema"].get("properties", {})))

    def test_the_pages_hold_nothing_from_this_machine(self):
        for page in REFERENCE.glob("*.mdx"):
            text = page.read_text()
            with self.subTest(page=page.name):
                self.assertNotIn(str(Path.home()), text)
                self.assertNotIn(self.generator.MODEL_SENTINEL, text)
                self.assertNotRegex(text, r"/Users/|/private/|/var/folders/")


def pages_in(node):
    """Every page path a docs.json navigation node names, at any depth."""
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
        """A new page in docs/ needs a place in docs.json, or the site hides it."""
        public = {p.relative_to(DOCS).with_suffix("").as_posix() for p in DOCS.rglob("*.md*")
                  if p.suffix in (".md", ".mdx") and p.name not in INTERNAL + ("README.md",)
                  and "node_modules" not in p.parts and "snippets" not in p.parts}
        self.assertEqual(sorted(public - set(self.pages)), [], "add these pages to docs/docs.json's navigation")

    def test_internal_guides_stay_off_the_site(self):
        ignored = (DOCS / ".mintignore").read_text().split()
        for name in INTERNAL:
            with self.subTest(name=name):
                self.assertIn(name, ignored)
                self.assertNotIn(Path(name).stem, self.pages)
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
        """Mintlify reads .md pages as MDX: outside code, `<Name` opens a JSX tag, `{` an expression and `<!--` is
        an error, so one stray `<App>` breaks the page's build. Escape it as `\\<App>` or put it in backticks."""
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
        """The sidebar shows sidebarTitle, else title: two pages with one name can't be told apart."""
        names = {}
        for page in self.pages:
            front = re.match(r"---\n(.*?)\n---\n", self.page_file(page).read_text(), re.S)[1]
            if re.search(r"(?m)^hidden: true$", front):
                continue
            name = re.search(r'(?m)^sidebarTitle: "?(.*?)"?$', front) or re.search(r'(?m)^title: "?(.*?)"?$', front)
            names.setdefault(name[1], []).append(page)
        self.assertEqual({name: found for name, found in names.items() if len(found) > 1}, {})

    def test_changelog_entries_have_unique_labels(self):
        """Each Update's label is its anchor and its RSS entry: two with one date collide. Merge them instead."""
        changelog = DOCS / "changelog.mdx"
        if not changelog.is_file():
            self.skipTest("no changelog")
        labels = re.findall(r'<Update label="([^"]+)"', changelog.read_text())
        self.assertEqual(sorted({label for label in labels if labels.count(label) > 1}), [])

    def test_the_site_holds_nothing_personal(self):
        files = [SITE, DOCS / ".mintignore"] + [self.page_file(page) for page in self.pages
                                                if self.page_file(page).suffix == ".mdx"]
        for path in files:
            with self.subTest(file=path.name):
                self.assertNotRegex(path.read_text(), r"/Users/(?!you/)|/private/var/|[0-9A-F]{8}-[0-9A-F]{16}")

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists() and (ROOT / ".gitattributes").is_file(),
                         "not a git checkout, or the public tree, which has no .gitattributes and no internal guides")
    def test_the_public_tree_drops_the_internal_guides(self):
        """The public tree is cut with `git archive`, which honours export-ignore in .gitattributes."""
        files = [f"docs/{name}" for name in INTERNAL] + ["docs/docs.json", "docs/.mintignore", "docs/index.mdx",
                                                         "docs/reference/cli.mdx"]
        out = subprocess.run(["git", "-C", str(ROOT), "check-attr", "export-ignore", "--", *files],
                             capture_output=True, text=True, check=True).stdout
        state = dict(re.findall(r"^(\S+): export-ignore: (\S+)$", out, re.M))
        for name in INTERNAL:
            self.assertEqual(state[f"docs/{name}"], "set", f"docs/{name} would reach the public tree")
        for public in files[len(INTERNAL):]:
            self.assertNotEqual(state[public], "set", f"{public} would be left out of the public tree")


if __name__ == "__main__":
    unittest.main()
