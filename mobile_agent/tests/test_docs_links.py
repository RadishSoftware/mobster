import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
SITE = DOCS / "docs.json"
BASE = "https://docs.mobster.dev"
SKIP = {"README.md"}
GENERATED = {"reference/cli", "reference/mcp-tools"}
ASSETS = ("/images/", "/brand/", "/snippets/", "/changelog/rss.xml", "/llms.txt", "/llms-full.txt", "/mcp")

CONTRACT = {
    "": [], "quickstart/mac": [], "quickstart/cli": [], "quickstart/test": [],
    "agents": [], "mcp-server": [], "agents/setup": [],
    "testing": [], "test/steps": [], "ci": [], "checks": [], "simulators": [],
    "conversations": [], "memory": [], "files": ["photos-and-videos"], "schedules": [], "mac/tour": [],
    "mac/settings": [], "mac/shortcuts": ["voice"], "tui": [], "scripting": [],
    "device-setup": [], "devices": [], "capabilities": [], "wifi": [],
    "how-it-works": [], "safety": [], "models-and-keys": [], "privacy": [], "requirements": [],
    "pricing": [], "faq": [],
    "reference/cli": [], "reference/mcp-tools": [], "configuration": ["environment-variables"],
    "reference/errors": ["exit-codes", "statuses", "http-codes"],
    "changelog": [],
    "troubleshooting": ["install", "iphone", "tasks", "mac", "checks", "command-not-found", "old-version",
                        "needs-terminal", "no-tui", "wda-not-answering", "no-iphone", "not-trusted", "runner-expired",
                        "locked", "device-busy", "not-responding", "locked-mid-task", "unplugged", "face-id",
                        "app-locked", "app-store-confirm", "not-registered", "iproxy-old", "no-key", "env-file-missing",
                        "needs-text-helper", "blocked", "needs-review", "declined", "spend-cap", "step-limit",
                        "wrong-app", "missing-controls", "wda-building", "couldnt-run", "out-not-writable",
                        "smart-off", "license", "setup", "helper-refresh", "voice-unavailable", "ai-account"],
    "account": ["sign-in", "plans", "one-mac-at-a-time", "manage-your-plan", "sign-out",
                "delete-your-account", "troubleshooting"],
    "remote": ["turn-it-on", "connect-an-app", "remote-keys", "remove-access", "limits", "privacy", "troubleshooting"],
    "remote/assistants": [],
}


def pages():
    out = {}
    for path in DOCS.rglob("*"):
        if path.suffix not in (".md", ".mdx") or path.name in SKIP or "snippets" in path.parts \
                or "node_modules" in path.parts:
            continue
        slug = path.relative_to(DOCS).with_suffix("").as_posix()
        out["" if slug == "index" else slug] = path
    return out


def slugify(heading):
    text = re.sub(r"<[^>]+>", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return re.sub(r" ", "-", text)


def strip_code(text):
    text = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1", "", text)
    return re.sub(r"`[^`\n]*`", "", text)


def anchors(path):
    text = path.read_text()
    found = set(re.findall(r"""\bid=["']([\w-]+)["']""", text))
    for line in re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1", "", text).splitlines():
        heading = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", line)
        if heading:
            found.add(slugify(heading[1]))
    found.update(slugify(label) for label in re.findall(r'<Update label="([^"]+)"', text))
    return found


def site_links(text):
    prose = strip_code(text)
    targets = re.findall(r"\]\((/[^)\s]*|#[^)\s]*)\)", prose)
    targets += re.findall(r"""\bhref=["'](/[^"']*|#[^"']*)["']""", prose)
    return targets


class DocsLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pages = pages()
        cls.anchors = {}
        cls.config = json.loads(SITE.read_text())
        cls.redirects = {r["source"].rstrip("/"): r["destination"] for r in cls.config.get("redirects", [])}

    def anchors_of(self, slug):
        if slug not in self.anchors:
            self.anchors[slug] = anchors(self.pages[slug])
        return self.anchors[slug]

    def resolve(self, target, here):
        path, _, anchor = target.partition("#")
        if path.startswith(ASSETS) or path == "/mcp":
            return None
        slug = path.strip("/").removesuffix(".md") if path else here
        if f"/{slug}" in self.redirects:
            return f"/{slug} is redirected to {self.redirects['/' + slug]}: link there"
        if slug not in self.pages:
            return f"no page {slug or 'index'}"
        if anchor and anchor not in self.anchors_of(slug):
            return f"{slug or 'index'} has no heading or anchor #{anchor}"
        return None

    def test_links_between_pages_resolve(self):
        for slug, path in sorted(self.pages.items()):
            for target in site_links(path.read_text()):
                with self.subTest(page=path.relative_to(DOCS).as_posix(), link=target):
                    self.assertIsNone(self.resolve(target, slug))

    def test_pages_link_to_the_site_not_to_github_copies(self):
        for slug, path in sorted(self.pages.items()):
            if slug in GENERATED:
                continue
            found = re.findall(r"github\.com/RadishSoftware/[\w-]+/blob/main/docs/\S*", path.read_text())
            with self.subTest(page=path.relative_to(DOCS).as_posix()):
                self.assertEqual(found, [], "link the page on the site instead")

    def test_docs_urls_in_the_code_and_readmes_resolve(self):
        sources = [ROOT / name for name in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md")] + [DOCS / "README.md"]
        for folder in ("mobile_agent", "dashboard/src", "desktop/src", "desktop/src-tauri/src", "skills", "plugins",
                       "examples"):
            base = ROOT / folder
            if base.is_dir():
                sources += [p for p in base.rglob("*") if p.suffix in (".py", ".ts", ".tsx", ".rs", ".md")
                            and "node_modules" not in p.parts and p.name != Path(__file__).name]
        for source in sources:
            if not source.is_file():
                continue
            for url in re.findall(re.escape(BASE) + r"(/[\w/.#-]*)?", source.read_text(errors="ignore")):
                url = (url or "/").rstrip(".")
                with self.subTest(file=source.relative_to(ROOT).as_posix(), url=BASE + url):
                    self.assertIsNone(self.resolve(url, ""))

    def test_the_link_contract_holds(self):
        for slug, wanted in CONTRACT.items():
            with self.subTest(page=slug or "index"):
                self.assertIn(slug, self.pages, f"/{slug} is in the link contract: keep it, or redirect it")
                missing = sorted(set(wanted) - self.anchors_of(slug))
                self.assertEqual(missing, [], f"/{slug} lost anchors the CLI or the app may link to")

    def test_every_redirect_leaves_a_moved_page_for_one_that_exists(self):
        for source, destination in self.redirects.items():
            with self.subTest(source=source):
                self.assertNotIn(source.strip("/"), self.pages, "a redirect hides a page that still exists")
                self.assertIsNone(self.resolve(destination, ""))


if __name__ == "__main__":
    unittest.main()
