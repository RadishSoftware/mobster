"""The docs stay true to the code: every command and flag is in the CLI reference, the repository's own files link
absolutely to files that exist, and examples compile. Links between docs pages are test_docs_links.py's."""

from pathlib import Path
import py_compile
import re
import unittest
from unittest import mock

from mobile_agent import __main__ as cli
from mobile_agent.extensions import Hooks

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "https://github.com/RadishSoftware/mobster"
DOCS = ROOT / "docs"
# Files read on GitHub and PyPI, where a link must be absolute. Pages in docs/ are the site's and link /slug instead.
PAGES = [ROOT / name for name in ("README.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md")] + \
    [DOCS / "README.md"] + sorted((ROOT / "examples").glob("*.md"))


# Paths that moved, until the pages that link them catch up (polish/cli/DOCS-ASKS.md asks the docs workflow).
RENAMED = {"skills/mobster": "skills/ios-testing"}


@unittest.skipUnless((DOCS / "cli.md").is_file(), "docs are not part of this copy")
class DocsTests(unittest.TestCase):
    def test_the_cli_reference_names_every_command_and_flag(self):
        # The hand-written page and the reference generated from the parser (scripts/gen_docs_reference.py).
        generated = DOCS / "reference" / "cli.mdx"
        reference = (DOCS / "cli.md").read_text() + (generated.read_text() if generated.is_file() else "")
        with mock.patch.object(cli, "load_extensions", return_value=Hooks()):
            parser = cli.build_parser()
        commands = next(a for a in parser._actions if a.dest == "command").choices
        for name, sub in [("mobster", parser), *commands.items()]:
            self.assertIn(f"mobster {name}" if name != "mobster" else "`mobster`", reference)
            for action in sub._actions:
                if action.help == "==SUPPRESS==":
                    continue  # hidden on purpose (completion's --values is for the scripts it prints)
                for option in action.option_strings:
                    if option in {"-h", "--help"}:
                        continue
                    with self.subTest(command=name, option=option):
                        # In backticks in the prose, or as a field of the generated reference (name={"-c, --continue"}).
                        named = f"`{option}" in reference or re.search(
                            r'name=\{"(?:[^"]*, )?' + re.escape(option) + r'(?:, [^"]*)?"\}', reference)
                        self.assertTrue(named, f"docs/cli.md does not mention {option} of {name}")

    def test_links_are_absolute_and_resolve(self):
        """Links point at the public repository or the docs site (GitHub and PyPI render them the same) and at files
        that exist."""
        for page in [page for page in PAGES if page.is_file()]:
            text = re.sub(r"```.*?```", "", page.read_text(), flags=re.S)
            for target in re.findall(r"\]\(([^)\s]+)\)", text):
                with self.subTest(page=page.name, link=target):
                    self.assertRegex(target, r"^(https?:|mailto:|#)", "use an absolute link to the repository")
                    match = re.match(re.escape(REPOSITORY) + r"/(?:blob|tree|raw)/main/([^#?]+)", target)
                    if match:
                        path = RENAMED.get(match[1], match[1])
                        self.assertTrue((ROOT / path).exists(), f"{page.relative_to(ROOT)} links to a missing {match[1]}")

    def test_verify_states_the_simulator_managers_start_limits(self):
        """The test quickstart says how long a first run waits to boot and to start WebDriverAgent: sim's two
        constants."""
        api, wda = ROOT / "mobile_agent" / "sim" / "api.py", ROOT / "mobile_agent" / "sim" / "wda.py"
        if not (api.is_file() and wda.is_file()):
            self.skipTest("the simulator manager is not in this tree")
        boot = re.search(r"^BOOT_TIMEOUT = (\d+)\b", api.read_text(), re.M)
        ready = re.search(r"^READY_TIMEOUT = (\d+)\b", wda.read_text(), re.M)
        self.assertTrue(boot and ready, "sim/api.py BOOT_TIMEOUT or sim/wda.py READY_TIMEOUT moved")
        self.assertEqual(boot[1], ready[1], "quickstart/test.mdx gives one limit for both; say which is which")
        text = (DOCS / "quickstart" / "test.mdx").read_text()
        self.assertEqual(re.findall(r"waits up to (\d+) s for each", text), [boot[1]])
        self.assertNotRegex(text, r"\b(\d+)-second limit")

    def test_examples_compile(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            for example in (ROOT / "examples").glob("*.py"):
                with self.subTest(example=example.name):
                    py_compile.compile(str(example), cfile=str(Path(folder) / "example.pyc"), doraise=True)


if __name__ == "__main__":
    unittest.main()
