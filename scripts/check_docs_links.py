"""Check every external URL and install command the docs site gives readers, over the network.

    python scripts/check_docs_links.py            # every URL in docs/ answers, apart from the launch-gated ones
    python scripts/check_docs_links.py --launch   # the launch-gated ones too: run it on launch day

It reads docs/**/*.md(x) (not the internal guides), finds every http(s) URL outside example output, and asks each
one with a HEAD request (then a GET, for servers that refuse HEAD), following redirects. A URL fails when it doesn't
answer 2xx or 3xx. With --launch it also checks that the install script starts with a shebang and that the
Homebrew tap answers. Links between docs pages are checked offline by mobile_agent/tests/test_docs_links.py.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SKIP = {"BRAND.md", "STYLE.md"}
# Allowed to fail until their launch gate passes, and never after: --launch checks them too.
GATED = {
    "https://mobster.dev/install.sh": "S5",
    "https://mobster.dev/download": "S6",
    "https://mobster.dev/privacy": "launch",
    "https://mobster.dev/terms": "launch",
    "https://docs.mobster.dev/llms.txt": "indexing",
}
GATED_PREFIXES = {
    "https://github.com/RadishSoftware/mobster-cli": "S5",
    "https://github.com/RadishSoftware/mobster": "S5",
}
# Placeholders, API base addresses and addresses on the reader's own machine, never fetched. Links to the docs site's
# own pages are resolved offline by the test instead, so a new page needn't be deployed before this passes.
IGNORED = re.compile(r"^https?://(127\.0\.0\.1|localhost|0\.0\.0\.0|example\.|hooks\.slack\.com/services/T|"
                     r"[^/]*\.example\b|docs\.mobster\.dev/(?!llms)|[^/]+/api/v\d+/?$)|[<>{}]|\.\.\.|…")


def urls():
    """{url: [page, ...]} for every external URL in the docs' prose, links and attributes."""
    found = {}
    for path in sorted(DOCS.rglob("*.md*")):
        if path.name in SKIP or "node_modules" in path.parts or path.suffix not in (".md", ".mdx"):
            continue
        text = path.read_text()
        for url in re.findall(r"https?://[^\s)\"'`<>\]]+", text):
            url = url.rstrip(".,;:")
            if not IGNORED.search(url):
                found.setdefault(url, []).append(path.relative_to(DOCS).as_posix())
    return found


def gate(url):
    if url.split("#")[0] in GATED:
        return GATED[url.split("#")[0]]
    return next((name for prefix, name in GATED_PREFIXES.items() if url.startswith(prefix)), None)


def answers(url):
    """None when the URL answers, else why not."""
    target = url.split("#")[0]
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh) mobster-docs-link-check"}
    for method in ("HEAD", "GET"):
        try:
            request = urllib.request.Request(target, method=method, headers=headers)
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status < 400:
                    return None
        except urllib.error.HTTPError as error:
            if method == "GET" or error.code not in (403, 405, 501):
                last = f"HTTP {error.code}"
                if method == "GET":
                    return last
                continue
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return f"no answer ({getattr(error, 'reason', error)})"
    return "no answer"


def launch_checks():
    problems = []
    try:
        with urllib.request.urlopen("https://mobster.dev/install.sh", timeout=20) as response:
            if not response.read(64).startswith(b"#!"):
                problems.append("https://mobster.dev/install.sh doesn't start with a shebang")
    except (urllib.error.URLError, OSError) as error:
        problems.append(f"https://mobster.dev/install.sh: {error}")
    if shutil.which("brew"):
        done = subprocess.run(["brew", "info", "radishsoftware/tap/mobster"], capture_output=True, text=True)
        if done.returncode:
            problems.append("brew info radishsoftware/tap/mobster: " + (done.stderr.strip().splitlines() or ["failed"])[-1])
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--launch", action="store_true", help="check the launch-gated URLs and commands too")
    args = parser.parse_args(argv)
    found = urls()
    checked = {url: pages for url, pages in found.items() if args.launch or not gate(url)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip(checked, pool.map(answers, checked)))
    failures = [(url, why) for url, why in results.items() if why]
    for url, why in sorted(failures):
        print(f"✗ {url}: {why} (in {', '.join(sorted(set(found[url])))})")
    if args.launch:
        for problem in launch_checks():
            failures.append((problem, ""))
            print(f"✗ {problem}")
    skipped = len(found) - len(checked)
    print(f"{len(checked) - len([f for f in failures if f[0] in checked])} of {len(checked)} URLs answer"
          + (f"; {skipped} wait for a launch gate (run with --launch on launch day)" if skipped else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
