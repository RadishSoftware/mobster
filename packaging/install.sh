#!/bin/sh
# Install the mobster command on an Apple silicon Mac.
#
#   curl -fsSL https://mobster.dev/install.sh | sh
#
# It downloads the release from GitHub over HTTPS only, checks it against its SHA-256, checks
# the binary's code signature, unpacks it into ~/.mobster/versions/<version>, and links
# ~/.local/bin/mobster to it. Run it again to upgrade. It never uses sudo and never reads or
# writes keys.
#
# What the checks prove: the SHA-256 published beside the release catches a damaged download,
# not a release replaced on GitHub. The copy served at mobster.dev also carries the expected
# SHA-256 of the version it installs and the team that signs Mobster (EXPECTED_SHA256 and
# EXPECTED_TEAM_ID below), so a release changed on GitHub alone is refused. This copy, from
# the repository, has neither, and accepts any valid signature, an ad hoc one included.
#
# Environment:
#   MOBSTER_VERSION           install this release, such as 0.2.0 (default: DEFAULT_VERSION, else the latest)
#   MOBSTER_HOME              where versions live (default ~/.mobster)
#   MOBSTER_BIN_DIR           where the mobster link goes (default ~/.local/bin)
#   MOBSTER_INSTALL_BASE_URL  download from here instead of GitHub (an https:// mirror, or file:// for tests)
set -eu

# The release step sets these in the copy it deploys (the release runbook, step 6): the
# Developer ID team that signs Mobster, the version a plain install gets, and that version's
# tarball SHA-256. Empty in the repository: then any valid signature is accepted, and the
# latest release is checked only against the SHA-256 published beside it.
EXPECTED_TEAM_ID=""
DEFAULT_VERSION=""
EXPECTED_SHA256=""

ASSET="mobster-macos-arm64.tar.gz"
REPO_URL="https://github.com/RadishSoftware/mobster"

say() { printf '%s\n' "$*"; }
fail() { printf 'mobster install: %s\n' "$*" >&2; exit 1; }

# Everything that acts is inside main, which runs on the last line. sh has to read the whole
# function before it can call it, so a download cut short runs nothing.
main() {
  # 1. Apple silicon Macs only.
  os=$(uname -s)
  arch=$(uname -m)
  if [ "$os" != Darwin ]; then
    fail "Mobster runs on macOS only (this is $os): it reaches your iPhone over USB from a Mac, and simulators need Xcode."
  fi
  if [ "$arch" != arm64 ]; then
    # A Terminal running under Rosetta reports x86_64 on an Apple silicon Mac.
    if [ "$(sysctl -in hw.optional.arm64 2>/dev/null || true)" != 1 ]; then
      fail "Mobster needs an Apple silicon Mac (this one is $arch). Intel Macs aren't supported yet."
    fi
  fi

  # 2. Where to download from.
  if [ -n "${MOBSTER_INSTALL_BASE_URL:-}" ]; then
    base=${MOBSTER_INSTALL_BASE_URL%/}
  elif [ -n "${MOBSTER_VERSION:-$DEFAULT_VERSION}" ]; then
    wanted=${MOBSTER_VERSION:-$DEFAULT_VERSION}
    base="$REPO_URL/releases/download/v${wanted#v}"
  else
    base="$REPO_URL/releases/latest/download"
  fi

  home=${MOBSTER_HOME:-$HOME/.mobster}
  bin_dir=${MOBSTER_BIN_DIR:-$HOME/.local/bin}
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/mobster-install.XXXXXX")
  trap 'rm -rf "$tmp"' EXIT INT TERM

  # 3. Download, and check the SHA-256.
  # HTTPS (TLS 1.2 or later) only, redirects included; file:// is for testing a local build.
  say "Downloading $base/$ASSET"
  curl -fsSL --proto '=https,file' --proto-redir '=https' --tlsv1.2 "$base/$ASSET" -o "$tmp/$ASSET" \
    || fail "couldn't download $base/$ASSET"
  curl -fsSL --proto '=https,file' --proto-redir '=https' --tlsv1.2 "$base/$ASSET.sha256" -o "$tmp/$ASSET.sha256" \
    || fail "couldn't download $base/$ASSET.sha256"
  expected=$(awk 'NR == 1 { print tolower($1) }' "$tmp/$ASSET.sha256")
  case "$expected" in
    *[!0-9a-f]*|"") fail "$ASSET.sha256 doesn't hold a SHA-256" ;;
  esac
  [ ${#expected} -eq 64 ] || fail "$ASSET.sha256 doesn't hold a SHA-256"
  # A plain install of the deployed copy knows the SHA-256 it should find, from mobster.dev rather than GitHub.
  if [ -z "${MOBSTER_VERSION:-}" ] && [ -n "$EXPECTED_SHA256" ] && [ "$expected" != "$EXPECTED_SHA256" ]; then
    fail "the release's SHA-256 isn't the one this installer was published with. Nothing was installed. Report it."
  fi
  if ! (cd "$tmp" && printf '%s  %s\n' "$expected" "$ASSET" | shasum -a 256 -c - >/dev/null 2>&1); then
    fail "the download doesn't match its SHA-256. Nothing was installed. Try again, and report it if it happens twice."
  fi

  # 4. Unpack beside the other versions, then move into place in one rename.
  mkdir -p "$home/versions"
  stage=$(mktemp -d "$home/versions/.install.XXXXXX")
  if ! tar -xzf "$tmp/$ASSET" -C "$stage"; then
    rm -rf "$stage"
    fail "couldn't unpack $ASSET"
  fi
  unpacked="$stage/mobster-macos-arm64"
  if [ ! -x "$unpacked/mobster" ] || [ ! -f "$unpacked/VERSION" ]; then
    rm -rf "$stage"
    fail "$ASSET isn't a Mobster release: it has no mobster-macos-arm64/mobster"
  fi
  version=$(awk 'NR == 1 { print $1 }' "$unpacked/VERSION")
  case "$version" in
    ""|*[!0-9A-Za-z.+-]*|.*) rm -rf "$stage"; fail "$ASSET has an unreadable VERSION" ;;
  esac

  # 5. The binary's signature must verify, and name the release team when one is set.
  if ! codesign --verify --strict "$unpacked/mobster" 2>/dev/null; then
    rm -rf "$stage"
    fail "the mobster binary's signature doesn't verify. Nothing was installed."
  fi
  if [ -n "$EXPECTED_TEAM_ID" ]; then
    team=$(codesign -dv --verbose=2 "$unpacked/mobster" 2>&1 | awk -F= '$1 == "TeamIdentifier" { print $2 }')
    if [ "$team" != "$EXPECTED_TEAM_ID" ]; then
      rm -rf "$stage"
      fail "the mobster binary is signed by team ${team:-none}, not Mobster's ($EXPECTED_TEAM_ID). Nothing was installed."
    fi
  fi

  target="$home/versions/$version"
  if [ -e "$target" ]; then
    mv "$target" "$stage/previous"
  fi
  mv "$unpacked" "$target"
  rm -rf "$stage"
  rm -f "$home/current.new"
  ln -s "versions/$version" "$home/current.new"
  mv -fh "$home/current.new" "$home/current"

  # 6. Link the command. A mobster that another installer put there is left alone.
  mkdir -p "$bin_dir"
  link="$bin_dir/mobster"
  if [ -e "$link" ] || [ -L "$link" ]; then
    current=$(readlink "$link" 2>/dev/null || true)
    case "$current" in
      */current/mobster) ;;
      *)
        if [ -n "$current" ]; then what="a link to $current"; else what="a file"; fi
        case "$current" in
          *uv/tools/*) remove="uv tool uninstall mobster-cli" ;;
          *) remove="rm $link" ;;
        esac
        fail "$link is $what from another install. Remove it with \`$remove\`, then run this again. Mobster $version is unpacked in $target."
        ;;
    esac
  fi
  ln -sfn "$home/current/mobster" "$link"

  say ""
  "$link" version || fail "installed, but \`$link version\` failed"
  say "Installed in $target, linked from $link."

  # 7. PATH: is the link reachable, and does another mobster come first?
  case ":$PATH:" in
    *":$bin_dir:"*)
      first=$(command -v mobster 2>/dev/null || true)
      if [ -n "$first" ] && [ "$first" != "$link" ]; then
        real=$(readlink "$first" 2>/dev/null || true)
        case "$first $real" in
          *uv/tools/*|*"/.local/share/uv/"*) remove="uv tool uninstall mobster-cli" ;;
          */homebrew/*|*/Cellar/*) remove="brew uninstall mobster" ;;
          *) remove="remove it, or put $bin_dir earlier in PATH" ;;
        esac
        say ""
        say "Another mobster comes first on your PATH: $first"
        say "Remove it with: $remove"
      fi
      ;;
    *)
      say ""
      say "$bin_dir isn't on your PATH. Add this line to ~/.zshrc, then open a new terminal:"
      say "  export PATH=\"$bin_dir:\$PATH\""
      ;;
  esac

  # 8. What to run next.
  say ""
  say "Next:"
  say "  mobster login               # add the AI key Mobster's agent runs on"
  say "  mobster                     # tell Mobster what to do on your iPhone"
  say "  mobster mcp install --all   # give Claude Code and your other agents the phone"
  say "  mobster test                # run your app's checks on a simulator"
}

main "$@"
