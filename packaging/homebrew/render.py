"""Render a Homebrew cask (or the formula) for a release.

usage: render.py --app --version 0.4.0 --sha256 <64 hex, or a SHA256SUMS-style file> [--url URL] [--out FILE]
       render.py --cask --version 0.3.0 --sha256 ... [--url URL] [--out FILE]
       render.py --version 0.3.0 --sha256 ... [--url URL] [--out FILE]     # the formula

--app is Mobster for Mac: Casks/mobster-app.rb on RadishSoftware/homebrew-tap, which installs the signed, notarized
DMG from RadishSoftware/mobster-releases (`brew install --cask radishsoftware/tap/mobster-app`). Each release bumps it.

--cask is the Mobster CLI 0.3.0, Casks/mobster.rb. The CLI is part of Mobster for Mac since 13 Oct 2026: the cask
is deprecated in favor of mobster-app and stays at 0.3.0 so existing users see that message. The formula template
stays in this folder and out of the public tap. Every @PLACEHOLDER@ in a template must be filled, or nothing is
written.
"""

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = {"formula": HERE / "mobster.rb.in", "cask": HERE / "mobster-cask.rb.in", "app": HERE / "mobster-app-cask.rb.in"}
RELEASES = "https://github.com/RadishSoftware/mobster-releases/releases/download/v{version}/"
RELEASE_URL = RELEASES + "mobster-macos-arm64.tar.gz"
APP_URL = RELEASES + "Mobster_{version}_Apple-Silicon.dmg"
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
    if not VERSION.fullmatch(version):
        raise RenderError(f"version {version!r} isn't a release version such as 0.2.0")
    sha256 = sha256.strip().lower()
    if not SHA256.fullmatch(sha256):
        raise RenderError("sha256 must be 64 hex characters, the first field of the .sha256 file")
    url = url or (APP_URL if kind == "app" else RELEASE_URL).format(version=version)
    if not URL.fullmatch(url):
        raise RenderError(f"url {url!r} must be https:// or file:/// with no spaces or quotes")
    text = template if template is not None else TEMPLATES[kind].read_text()
    if kind in ("cask", "app"):
        # `brew audit --strict` calls a cask URL without #{version} unversioned and wants sha256 :no_check.
        url = url.replace(f"/v{version}/", "/v#{version}/").replace(f"_{version}_", "_#{version}_")
    values = {"@VERSION@": version, "@SHA256@": sha256, "@URL@": url}
    for key, value in values.items():
        text = text.replace(key, value)
    left = sorted(set(PLACEHOLDER.findall(text)))
    if left:
        raise RenderError(f"the template has placeholders render.py doesn't fill: {', '.join(left)}")
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description="render the Homebrew cask or formula for a release")
    kinds = parser.add_mutually_exclusive_group()
    kinds.add_argument("--app", action="store_true",
                       help="render Mobster for Mac's cask (Casks/mobster-app.rb), which each release bumps")
    kinds.add_argument("--cask", action="store_true",
                       help="render the deprecated Mobster CLI cask (Casks/mobster.rb); without either, the formula")
    parser.add_argument("--version", required=True, help="the release version, such as 0.2.0")
    parser.add_argument("--sha256", required=True,
                        help="the download's SHA-256, or a path to a file whose first field is it")
    parser.add_argument("--url", help="the download's URL (default: the release asset on RadishSoftware/mobster-releases)")
    parser.add_argument("--out", help="write the result here instead of stdout")
    args = parser.parse_args(argv)
    sha256 = args.sha256
    if Path(sha256).is_file():
        fields = Path(sha256).read_text().split()
        sha256 = fields[0] if fields else ""
    try:
        text = render(args.version, sha256, args.url, kind="app" if args.app else "cask" if args.cask else "formula")
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
