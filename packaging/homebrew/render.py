"""Render the Homebrew cask (or the formula) for a release.

usage: render.py --cask --version 0.2.0 --sha256 <64 hex, or the .sha256 file> [--url URL] [--out FILE]
       render.py --version 0.2.0 --sha256 ... [--url URL] [--out FILE]     # the formula

The cask is what users install: it goes to RadishSoftware/homebrew-tap as Casks/mobster.rb, and
`brew install radishsoftware/tap/mobster` then needs no Command Line Tools. The formula template stays
in this folder and out of the public tap. --url defaults to the release asset on
RadishSoftware/mobster; --sha256 comes from build.sh's .sha256 file. Every @PLACEHOLDER@ in the
template must be filled, or nothing is written.
"""

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = {"formula": HERE / "mobster.rb.in", "cask": HERE / "mobster-cask.rb.in"}
TEMPLATE = TEMPLATES["formula"]
RELEASE_URL = "https://github.com/RadishSoftware/mobster/releases/download/v{version}/mobster-macos-arm64.tar.gz"
PLACEHOLDER = re.compile(r"@[A-Z0-9_]+@")
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
URL = re.compile(r"^(https://|file:///)[^\s\"'\\#]+$")


class RenderError(ValueError):
    pass


def render(version, sha256, url=None, template=None, kind="formula"):
    """The formula or cask text, or RenderError when a value is malformed or a placeholder is left."""
    if kind not in TEMPLATES:
        raise RenderError(f"kind {kind!r} isn't one of {', '.join(TEMPLATES)}")
    if not VERSION.match(version):
        raise RenderError(f"version {version!r} isn't a release version such as 0.2.0")
    sha256 = sha256.strip().lower()
    if not SHA256.match(sha256):
        raise RenderError("sha256 must be 64 hex characters, the first field of the .sha256 file")
    url = url or RELEASE_URL.format(version=version)
    if not URL.match(url):
        raise RenderError(f"url {url!r} must be https:// or file:/// with no spaces or quotes")
    text = template if template is not None else TEMPLATES[kind].read_text()
    values = {"@VERSION@": version, "@SHA256@": sha256, "@URL@": url}
    for key, value in values.items():
        text = text.replace(key, value)
    left = sorted(set(PLACEHOLDER.findall(text)))
    if left:
        raise RenderError(f"the template has placeholders render.py doesn't fill: {', '.join(left)}")
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description="render the Homebrew cask or formula for a release")
    parser.add_argument("--cask", action="store_true",
                        help="render the cask (Casks/mobster.rb), which users install; without it, the formula")
    parser.add_argument("--version", required=True, help="the release version, such as 0.2.0")
    parser.add_argument("--sha256", required=True, help="the tarball's SHA-256, or a path to its .sha256 file")
    parser.add_argument("--url", help="the tarball's URL (default: the release asset on RadishSoftware/mobster)")
    parser.add_argument("--out", help="write the result here instead of stdout")
    args = parser.parse_args(argv)
    sha256 = args.sha256
    if Path(sha256).is_file():
        fields = Path(sha256).read_text().split()
        sha256 = fields[0] if fields else ""
    try:
        text = render(args.version, sha256, args.url, kind="cask" if args.cask else "formula")
    except RenderError as error:
        print(f"render.py: {error}", file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
