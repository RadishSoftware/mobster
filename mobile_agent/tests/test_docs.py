import py_compile
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "https://github.com/RadishSoftware/mobster"
DOCS = ROOT / "docs"
PAGES = [ROOT / name for name in ("README.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md")] + \
    sorted((ROOT / "examples").glob("*.md"))


@unittest.skipUnless((DOCS / "docs.json").is_file(), "docs are not part of this copy")
class DocsTests(unittest.TestCase):
    def test_links_are_absolute_and_resolve(self):
        for page in [page for page in PAGES if page.is_file()]:
            text = re.sub(r"```.*?```", "", page.read_text(), flags=re.S)
            for target in re.findall(r"\]\(([^)\s]+)\)", text):
                with self.subTest(page=page.name, link=target):
                    self.assertRegex(target, r"^(https?:|mailto:|#)", "use an absolute link to the repository")
                    match = re.match(re.escape(REPOSITORY) + r"/(?:blob|tree|raw)/main/([^#?]+)", target)
                    if match:
                        self.assertTrue((ROOT / match[1]).exists(),
                                        f"{page.relative_to(ROOT)} links to a missing {match[1]}")

    def test_verify_states_the_simulator_managers_start_limits(self):
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
