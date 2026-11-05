"""Render the standalone CLI Homebrew formula for a release.

usage: render.py --version 0.4.0 --sha256 <64 hex, or a SHA256SUMS-style file> [--url URL] [--out FILE]

Every @PLACEHOLDER@ must be filled before the formula is written. App casks and
legacy CLI cask migration are maintained in the private app repository.
"""

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "mobster.rb.in"
RELEASES = "https://github.com/RadishSoftware/mobster-releases/releases/download/v{version}/"
RELEASE_URL = RELEASES + "mobster-macos-arm64.tar.gz"
PLACEHOLDER = re.compile(r"@[A-Z0-9_]+@")
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
URL = re.compile(r"^(https://|file:///)[^\s\"'\\#]+$")


class RenderError(ValueError):
    pass


def render(version, sha256, url=None, template=None):
    """The formula text, or RenderError for malformed values or unfilled placeholders."""
    if not VERSION.fullmatch(version):
        raise RenderError(f"version {version!r} isn't a release version such as 0.2.0")
    sha256 = sha256.strip().lower()
    if not SHA256.fullmatch(sha256):
        raise RenderError("sha256 must be 64 hex characters, the first field of the .sha256 file")
    url = url or RELEASE_URL.format(version=version)
    if not URL.fullmatch(url):
        raise RenderError(f"url {url!r} must be https:// or file:/// with no spaces or quotes")
    text = template if template is not None else TEMPLATE.read_text()
    values = {"@VERSION@": version, "@SHA256@": sha256, "@URL@": url}
    for key, value in values.items():
        text = text.replace(key, value)
    left = sorted(set(PLACEHOLDER.findall(text)))
    if left:
        raise RenderError(f"the template has placeholders render.py doesn't fill: {', '.join(left)}")
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description="render the standalone CLI Homebrew formula for a release")
    parser.add_argument("--version", required=True, help="the release version, such as 0.2.0")
    parser.add_argument("--sha256", required=True,
                        help="the download's SHA-256, or a path to a file whose first field is it")
    parser.add_argument("--url",
                        help="the download's URL (default: the release asset on RadishSoftware/mobster-releases)")
    parser.add_argument("--out", help="write the result here instead of stdout")
    args = parser.parse_args(argv)
    sha256 = args.sha256
    if Path(sha256).is_file():
        fields = Path(sha256).read_text().split()
        sha256 = fields[0] if fields else ""
    try:
        text = render(args.version, sha256, args.url)
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
