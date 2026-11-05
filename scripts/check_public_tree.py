#!/usr/bin/env python3
"""Verify a cut of the public tree (scripts/cut-public-tree.sh --check runs this).

    python3 scripts/check_public_tree.py TREE --paths scripts/public-paths.txt [--overlay DIR]
                                         [--root PRIVATE_CHECKOUT] [--no-install] [--no-gitleaks]
    python3 scripts/check_public_tree.py --select --paths scripts/public-paths.txt < FILES

--select prints the files of FILES (one per line) that the allowlist ships: the most specific line that covers a file
decides, and `!path` beats `path` on the same path. cut-public-tree.sh cuts with it.

The checks, each printed as one line:

  paths      every file in the tree is one the allowlist ships or one the overlay (scripts/public-tree/) put there;
             every path the allowlist ships is in the tree; nothing that must never ship (NEVER, below) is
  scan       no file holds the owner's name, an email address that is not a placeholder, a path from someone's Mac,
             a device UDID, an Apple team ID, an API key or token, or a value from the maintainer's own env files; no
             key, certificate, journal or editor file is in the tree
  gitleaks   `gitleaks dir` over the tree with the tree's .gitleaks.toml (its allowlist names the synthetic test
             values and the publishable keys) finds nothing. Without gitleaks installed this check fails
  leaks      no file names an internal file by path (a path in product/, PRODUCT.md, PRIVACY.md ...). A few uses are
             deliberate; EXEMPT says which and why. Mentions of a source folder in a comment (desktop/, dashboard/,
             "SPEC §") are counted, not failed.
  imports    in a fresh venv holding only the dependencies pyproject.toml declares, every mobile_agent module outside
             tests imports, and the tree installs no command: pyproject.toml has no [project.scripts] and there is
             no mobile_agent/__main__.py. The tree is a reference and research, not a tool.
             Needs the network once to install; --no-install skips this step.

Exit status 0 when every check passes, 1 when any fails. Standard library only, and it runs on the macOS system Python
(3.9) too; the venv it makes uses a 3.12 or newer interpreter.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# What must never be in the public tree, whatever the allowlist says. The allowlist's `!` lines say
# the same and more; this list is the check's own copy, so deleting a `!` line cannot make a leak pass.
NEVER = (
    "product", "PRIVACY.md", "TERMS.md", "DPA.md", "MSA.md", "SECURITY-OVERVIEW.md", "packaging/README.md",
    "desktop", "dashboard", "services", "relay", "supabase", "site", "research_notes", "reports",
    "mobile_agent/private", ".gitattributes", ".env", ".git",
    # The harness: Mobster for Mac's since 13 Oct 2026. The engine reaches it only through harness_api.
    "docs", "plugins", ".claude-plugin", "skills", "actions", "packaging", "examples",
    "mobile_agent/tui", "mobile_agent/mcp_server", "mobile_agent/integrations", "mobile_agent/server.py",
    "mobile_agent/smart_run.py", "mobile_agent/tracks.py", "mobile_agent/engine_hooks.py", "mobile_agent/verify",
    "mobile_agent/testkit", "mobile_agent/replay_core", "mobile_agent/sim", "mobile_agent/workflows",
    "mobile_agent/threads", "mobile_agent/memory", "mobile_agent/longform", "mobile_agent/learned",
    "mobile_agent/outlets", "mobile_agent/remote", "mobile_agent/waiting",
    "mobile_agent/teams", "mobile_agent/wireless", "mobile_agent/attachments", "mobile_agent/ai_plans",
    "mobile_agent/accounts",
    "mobile_agent/phone_io", "mobile_agent/manual_control.py", "mobile_agent/permissions/outside.py",
    "mobile_agent/permissions/register.py", "mobile_agent/harness/routes.py", "mobile_agent/harness/prewarm.py",
    "mobile_agent/bench/real", "mobile_agent/docs/realbench.md",
    # No command line and no setup: the open-source tree is a reference loop and its research (13 Oct 2026).
    "mobile_agent/__main__.py", "mobile_agent/engine_cli.py", "mobile_agent/smart_run.py", "scripts/usb-wda.sh",
    "mobile_agent/docs/usb-wda.md", "mobile_agent/signing.py", "mobile_agent/wda/source.py", "mobile_agent/wda/gate.py",
    "mobile_agent/lockscreen.py", "mobile_agent/devices.py",
    "mobile_agent/device_manager.py", "mobile_agent/mac_tools.py", "mobile_agent/wda/patches.py",
    "mobile_agent/supervised.py", "mobile_agent/app_killer.py", "mobile_agent/native_helpers.py",
    # The quality and speed modules: Mobster for Mac's.
    "mobile_agent/skills", "mobile_agent/deeplinks.py", "mobile_agent/routes.py", "mobile_agent/app_choice.py",
    "mobile_agent/steering.py", "mobile_agent/hedge.py", "mobile_agent/decision_memo.py",
    "mobile_agent/helper_models.py", "mobile_agent/decision_provider.py", "mobile_agent/decider_scope.py",
    "mobile_agent/wda/frame_clock.py", "mobile_agent/wda/video.py", "mobile_agent/http_pool.py",
    "mobile_agent/latency_trace.py",
)
# The overlay (scripts/public-tree/) may put files under a NEVER folder: examples/reference_loop.py is the tree's
# one example. Anything else under that folder still fails.

# File names that hold credentials or a person's machine state, wherever they are in the tree.
FORBIDDEN_NAMES = re.compile(
    r"(^|/)(\.env(\..+)?|.*\.(p12|p8|pem|key|mobileprovision|sqlite3(-wal|-shm)?|har|pcap|xcuserstate)|"
    r"\.DS_Store|xcuserdata|\.state|\.ci-cache|\.venv|__pycache__|node_modules)(/|$)")
ALLOWED_NAMES = {".env.example"}

# The placeholders the tests and docs use. A value outside these sets is reported, so a new
# fixture has to be added here on purpose: that is the point of the check.
PLACEHOLDER_USERS = {"you", "test", "example", "someone", "user", "username", "yourname", "name", "me"}
SYNTHETIC_UDIDS = {
    # Distinct synthetic device identifiers used by the standalone source fixtures.
    '00000000-00000000ABCD0001',
    '00000000-00000000ABCD0002',
    '00008110-00000000ABCD0003',
    '00008110-00000000ABCD0004',
    '00008110-00000000ABCD0005',
    '00008120-00000000ABCD0006',
    '00008130-00000000ABCD0007',
    '00008130-00000000ABCD0008',
    '00008130-00000000ABCD0009',
    '00008130-00000000ABCD000A',
    '00008130-00000000ABCD000B',
    '00008130-00000000ABCD000C',
    '00008140-00000000ABCD000D',

    "00008130-001A2B3C4D5E6F70", "00008020-000A1B2C3D4E5F60", "00008030-000B2C3D4E5F6071",
    "0000FE01-AAAAAAAAAAAAAAAA", "00008130-0011223344556677", "00008130-000A1B2C3D4E5F60",
    "00008110-000000000000001E", "00008030-000000000000000E",
}
PLACEHOLDER_TEAMS = {"ABCDE12345", "ZYXWV98765", "QQQQQQQQQQ", "QQQQQ11111", "TEAM123456", "XXXXXXXXXX",
                     "0123456789", "1234567890"}
# Email domains that are not a person's: RFC 2606 reserved TLDs and examples,
# the product's own, and a fictional handle.
EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "mobster.dev", "radish.software",
                 "anthropic.com", "users.noreply.github.com", "coffee.studio",
                 "test", "example", "invalid", "localhost")
FILE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "json", "md", "py", "js", "ts", "txt",
                   "yml", "yaml", "toml", "sh", "swift", "plist", "xcassets", "mp4", "mov", "pdf"}
# Words in a key-shaped string that make it a fixture, not a key.
FAKE_KEY_WORDS = ("test", "eval", "fake", "example", "dummy", "placeholder", "xxxx", "0000", "never", "not-real",
                  "abcdefg", "1234567")  # the alphabet and counting runs that redaction tests use

OWNER = re.compile(r"(?<![A-Za-z0-9])(?:andy|guo)(?![a-z])", re.I)
EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.([A-Za-z]{2,}))")
HOME_PATH = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)")
MACHINE_PATH = re.compile(r"(?:/private)?/var/folders/[a-z0-9_]{2}/|/private/tmp/claude|/tmp/claude-\d")
UDID = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}(?![0-9A-Za-z])")
OLD_UDID = re.compile(r"udid\W{1,4}[0-9a-f]{40}(?![0-9a-z])", re.I)
TEAM = re.compile(
    r"(?<![A-Za-z])(?:DEVELOPMENT_TEAM|TeamIdentifier|teamID|team_id|TEAM_ID|OU|\"team\"|team)[\"' ]*[=:]"
    r"[\"' ]*([A-Z0-9]{10})(?![A-Za-z0-9])"
    r"|(?:Apple (?:Development|Distribution)|Developer ID (?:Application|Installer)|"
    r"iPhone Developer)[^()\n]{0,80}\(([A-Z0-9]{10})\)")
SECRETS = (
    ("an Anthropic or OpenAI key", re.compile(r"\bsk-(?:ant-|proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Stripe key", re.compile(r"\b(?:sk|rk|pk)_live_[A-Za-z0-9]{10,}|\bwhsec_[A-Za-z0-9]{16,}")),
    ("a Supabase secret key", re.compile(r"\bsb_secret_[A-Za-z0-9_-]{16,}")),
    ("a Remote credential", re.compile(r"\bmob_(?:at|rt|rk|dk)_[A-Za-z0-9_-]{16,}")),
    ("a private key",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?=(?:\\n|\s)*(?:Proc-Type|DEK-Info|[A-Za-z0-9+/]{16}))")),
    ("a JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("a bearer token", re.compile(r"\bBearer [A-Za-z0-9._~+/-]{30,}=*")),
    # In-app feedback's staff channel (DISCORD_FEEDBACK_WEBHOOK_URL) is the license API's server secret only.
    ("a Discord webhook URL", re.compile(r"\bdiscord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d{5,}/[A-Za-z0-9_-]{20,}")),
)

SECRET_ANY = re.compile("|".join(f"(?P<s{index}>{pattern.pattern})" for index, (_, pattern) in enumerate(SECRETS)))

# Internal files and folders. A name in `!` lines of the allowlist is added to HARD or SOFT:
# a path with a dot or a slash, or a compound name, is HARD (naming it is a pointer to a file the
# reader cannot open); a plain word (desktop, dashboard, ...) is SOFT, because source comments
# say "the dashboard" in prose and name a source folder, which tells a reader nothing.
SOFT_EXTRA = (re.compile(r"\bSPEC §"), "SPEC §")
# product/ is the positioning, messaging and launch plans, so a path in it is HARD though `!product` is a plain word,
# and so are the product brief and the messaging by the bare names the Mac app cites ("PRODUCT.md §9").
HARD_EXTRA = r"(?<!/)product/[\w/-]*(?:\.\w+)?|PRODUCT\.md|MESSAGING\.md"

# (file, what it says, why that is fine). `file` and `what` are regular expressions, on the
# tree-relative path and on the line.
EXEMPT = (
    (r"^mobile_agent/tests/", r"scripts/(?:sim_smoke|verify_smoke|ci-local)",
     "tests of maintainer scripts skip when the script is not in the tree, and say which script they need"),
)

def read_paths(path):
    """The allowlist: (paths that ship, paths carved out), from `path`, or `!path` lines."""
    ships, internal = [], []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        (internal if line.startswith("!") else ships).append(line.lstrip("!").strip())
    return ships, internal


def covers(entries, rel):
    """Whether `rel` is one of `entries` or inside one."""
    return any(rel == entry or rel.startswith(entry + "/") for entry in entries)


def ships_file(ships, internal, rel):
    """Whether the allowlist ships `rel`: the most specific line that covers it decides; `!path` beats `path`."""
    best = None
    for entry, ships_it in [(e, False) for e in internal] + [(e, True) for e in ships]:
        if rel == entry or rel.startswith(entry + "/"):
            if best is None or len(entry) > len(best[0]) or (len(entry) == len(best[0]) and not ships_it):
                best = (entry, ships_it)
    return bool(best and best[1])


def select(ships, internal, files):
    return [rel for rel in files if ships_file(ships, internal, rel)]


# The private repository's name. The public one (mobster-cli, to be renamed mobster) and the Mac app's
# releases repository (mobster-releases) may be named anywhere.
PRIVATE_REPO = "RadishSoftware/mobster-app"

_LEAK_PATTERNS = {}


def leak_entries(ships, internal, overlay=()):
    """The `!` lines a public file may not name: not a folder that ships some of its files (`!mobile_agent`), and not
    a file the overlay replaces (`!README.md`: the tree has its own)."""
    return [entry for entry in internal
            if not any(ship.startswith(entry + "/") for ship in ships) and entry not in overlay]


def leak_patterns(internal):
    """(hard, soft): one regex each for naming an internal path, built from the allowlist's `!` lines."""
    key = tuple(internal)
    if key not in _LEAK_PATTERNS:
        hard, soft = [re.escape(PRIVATE_REPO), HARD_EXTRA], []
        for entry in sorted(internal, key=len, reverse=True):
            if "/" in entry or re.search(r"\w\.\w+$", entry) or "-" in entry or "_" in entry:
                hard.append(re.escape(entry))
            elif re.fullmatch(r"[a-z]+", entry):
                soft.append(re.escape(entry) + "/")
        _LEAK_PATTERNS[key] = (re.compile(r"(?<![\w.-])(?:%s)(?![\w-])" % "|".join(hard)),
                               re.compile(r"(?<![\w/.-])(?:%s)" % "|".join(soft)) if soft else None)
    return _LEAK_PATTERNS[key]


def exempt(rel, line):
    return any(re.search(path, rel) and re.search(what, line) for path, what, _ in EXEMPT)


def line_of(text, index):
    return text.count("\n", 0, index) + 1


def scan_file(rel, data, internal):
    """What one file holds that a public tree must not: (found, leaks, notes), from its bytes.
    found: (kind, line, detail) for a personal detail or a secret. leaks: the same for an internal
    path. notes: each source folder it names in passing. A binary file is searched only for the
    patterns long enough not to turn up by chance in compressed data."""
    binary = b"\0" in data[:8192]
    text = data.decode("latin-1" if binary else "utf-8", errors="replace")
    where = (lambda match: None) if binary else (lambda match: line_of(text, match.start()))
    found = []

    if not binary:
        for match in OWNER.finditer(text):
            context = text[max(0, match.start() - 12):match.end() + 12]
            if rel == "mobile_agent/evals/vision/manifest.json" and "Andy Li" in context:
                continue  # the photographer's credit that a CC-licensed image requires
            found.append(("the owner's name", where(match), match.group(0)))
        for match in EMAIL.finditer(text):
            domain, tld = match.group(1).lower(), match.group(2).lower()
            if tld in FILE_EXTENSIONS or any(domain == d or domain.endswith("." + d) for d in EMAIL_DOMAINS):
                continue
            found.append(("an email address", where(match), match.group(0)))
        for match in OLD_UDID.finditer(text):
            found.append(("a device UDID", where(match), "40 hex digits after 'udid'"))
        for match in TEAM.finditer(text):
            team = match.group(1) or match.group(2)
            if team not in PLACEHOLDER_TEAMS:
                found.append(("an Apple team ID", where(match), team))
    for match in HOME_PATH.finditer(text):
        if match.group(1).lower() not in PLACEHOLDER_USERS:
            found.append(("a path from a Mac or Linux home folder", where(match), match.group(0)))
    for match in MACHINE_PATH.finditer(text):
        found.append(("a path from this machine's temporary folders", where(match), match.group(0)))
    for match in UDID.finditer(text):
        if match.group(0).upper() not in SYNTHETIC_UDIDS:
            found.append(("a device UDID", where(match), match.group(0)))
    for match in SECRET_ANY.finditer(text):
        if not any(word in match.group(0).lower() for word in FAKE_KEY_WORDS):
            found.append((SECRETS[int(match.lastgroup[1:])][0], where(match), fingerprint(match.group(0))))

    hard, soft = leak_patterns(internal)
    leaks, notes = [], []
    lines = None
    for match in hard.finditer(text):
        if not binary:
            lines = lines or text.splitlines()
        if not exempt(rel, "" if binary else lines[line_of(text, match.start()) - 1]):
            leaks.append(("an internal path", where(match), match.group(0)))
    if soft:
        notes += [match.group(0) for match in soft.finditer(text)]
    notes += [SOFT_EXTRA[1] for _ in SOFT_EXTRA[0].finditer(text)]
    return found, leaks, notes


def fingerprint(value):
    """How a report names a secret: its first 4 characters and a SHA-256 prefix, never the value."""
    raw = value.encode() if isinstance(value, str) else value
    return f"{raw[:4].decode('latin-1')}... sha256:{hashlib.sha256(raw).hexdigest()[:12]}"


def secret_values(root):
    """Values of the maintainer's own keys: from the process environment, the env files that hold
    them (mobile_agent/.env and the Vercel CLI's .vercel/.env* of the checkout and of the main
    checkout it belongs to), and every file MOBSTER_SECRET_FILES names (colon-separated: env files,
    key files, token files). They are only compared with the tree, never printed."""
    files, raw_files = [], []
    checkout = Path(root).resolve() if root else None
    if checkout:
        roots = [checkout]
        common = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True).stdout.strip()
        if common:
            roots.append(Path(common).parent)
        for base in roots:
            files.append(base / "mobile_agent" / ".env")
            files += sorted((base / ".vercel").glob(".env*"))
    files += [Path.home() / ".config" / "mobster" / "agent.env", Path.home() / ".mobster" / "agent.env"]
    # The maintainer's service secrets (the feedback webhook's discord.env among them), when this Mac has them.
    files += sorted((Path.home() / "mobster-work" / "secrets").glob("*.env"))
    raw_files += [Path(p).expanduser() for p in os.environ.get("MOBSTER_SECRET_FILES", "").split(":") if p]
    pairs = [(name, value, "the environment") for name, value in os.environ.items()]
    for file in dict.fromkeys(files + raw_files):
        try:
            text = file.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if re.match(r"(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=", line):
                name, value = line.split("=", 1)
                pairs.append((name.replace("export ", "").strip(), value.strip().strip("\"'"), file.name))
            elif file in raw_files and not line.startswith(("-----", "untrusted comment")):
                # A key or token file: each line is a secret (a PEM body line, a token).
                pairs.append(("a line of " + file.name, line, file.name))
    keyed = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|^a line of ", re.I)
    webhook = re.compile(r"WEBHOOK_URL$", re.I)
    seen, out = set(), []
    for name, value, source in pairs:
        # A webhook's URL is its secret (DISCORD_FEEDBACK_WEBHOOK_URL), so an https value counts for those names.
        url_secret = webhook.search(name) and value.startswith("https://")
        if (keyed.search(name) and len(value) >= 16 and not value.startswith(("http", "/", "~")) or url_secret) \
                and value not in seen:
            seen.add(value)
            out.append((name, value.encode(), source))
    return out


def files_of(tree):
    return sorted(p for p in Path(tree).rglob("*") if p.is_file() or p.is_symlink())


def inside(tree, path):
    return Path(path).resolve().is_relative_to(Path(tree).resolve())


def check_paths(tree, ships, internal, overlay=()):
    problems = []
    for path in files_of(tree):
        rel = path.relative_to(tree).as_posix()
        if rel != "SOURCE_REV" and rel not in overlay and not ships_file(ships, internal, rel):
            problems.append(f"{rel} is in the tree, but the allowlist doesn't ship it")
        if FORBIDDEN_NAMES.search(rel) and Path(rel).name not in ALLOWED_NAMES:
            problems.append(f"{rel} is a credential, cache or machine-state file")
        if path.is_symlink() and not inside(tree, path.parent / os.readlink(path)):
            problems.append(f"{rel} is a symbolic link that leaves the tree")
    for entry in ships:
        if not (tree / entry).exists():
            problems.append(f"the allowlist ships {entry}, but the tree has no such path")
    for entry in NEVER:
        path = tree / entry
        if path.is_dir():
            stray = [p.relative_to(tree).as_posix() for p in files_of(path)]
            stray = [rel for rel in stray if rel not in overlay]
            if stray:
                problems.append(f"{entry} must not ship, but it is in the tree ({stray[0]})")
        elif path.exists() or path.is_symlink():
            problems.append(f"{entry} must not ship, but it is in the tree")
    for entry in overlay:
        if not (tree / entry).is_file():
            problems.append(f"the overlay puts {entry} in the tree, but it isn't there")
    return problems


def check_scan(tree, internal, root):
    """(problems, leak problems, notes): the personal-detail and secret scan, then the internal-path scan."""
    problems, leaks, notes = [], [], {}
    secrets = secret_values(root)
    for path in files_of(tree):
        rel = path.relative_to(tree).as_posix()
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        found, hard, soft = scan_file(rel, data, internal)
        for kind, line, detail in found:
            problems.append(f"{rel}{':' + str(line) if line else ''}: {kind}: {detail}")
        for kind, line, detail in hard:
            leaks.append(f"{rel}{':' + str(line) if line else ''}: names {detail}")
        for what in soft:
            notes[what] = notes.get(what, 0) + 1
        for name, value, source in secrets:
            if value in data:
                problems.append(f"{rel}: holds the value of {name} from {source}")
    return problems, leaks, notes


def check_gitleaks(tree):
    """gitleaks over the tree with the tree's own .gitleaks.toml. Reports rule, file and line only
    (--redact keeps the value out of the report too). Refuses when gitleaks is missing or fails."""
    exe = shutil.which("gitleaks")
    if not exe:
        return ["gitleaks is not installed, so the tree was not scanned for secrets: brew install gitleaks"]
    config = tree / ".gitleaks.toml"
    with tempfile.TemporaryDirectory(prefix="mobster-cut-gitleaks.") as scratch:
        report = Path(scratch) / "report.json"
        command = [exe, "dir", str(tree), "--no-banner", "--redact", "--log-level", "error", "--exit-code", "1",
                   "--report-format", "json", "--report-path", str(report)]
        if config.is_file():
            command += ["--config", str(config)]
        done = run(command)
        try:
            findings = json.loads(report.read_text() or "[]")
        except (OSError, ValueError):
            findings = None
    if done.returncode == 0 and findings == []:
        return []
    if done.returncode != 1 or not findings:
        return [f"gitleaks did not finish (exit {done.returncode}): {(done.stderr or done.stdout).strip()[-300:]}"]
    problems = []
    for finding in findings:
        file = Path(finding.get("File", ""))
        rel = file.relative_to(tree).as_posix() if file.is_absolute() and tree in file.parents else file.as_posix()
        problems.append(f"{rel}:{finding.get('StartLine')}: {finding.get('RuleID')} ({finding.get('Description', '')})")
    return problems


def find_python():
    """An interpreter of Python 3.12 or newer for the fresh venv."""
    candidates = [sys.executable] + [shutil.which(f"python3.{minor}") for minor in (13, 12, 14)]
    for candidate in candidates:
        if not candidate:
            continue
        probe = subprocess.run([candidate, "-c", "import sys; print(int(sys.version_info >= (3, 12)))"],
                               capture_output=True, text=True)
        if probe.stdout.strip() == "1":
            return candidate
    return None


def run(command, **kwargs):
    return subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL, **kwargs)


def check_imports(tree):
    python = find_python()
    uv = shutil.which("uv")
    if not python and not uv:
        return ["no Python 3.12 or uv to make the fresh venv with"]
    scratch = Path(tempfile.mkdtemp(prefix="mobster-cut-venv."))
    try:
        venv = scratch / "venv"
        made = run([python, "-m", "venv", str(venv)]) if python else run([uv, "venv", "--python", "3.12", str(venv)])
        if made.returncode:
            return [f"could not make the venv: {made.stderr.strip()[-300:]}"]
        py = str(venv / "bin" / "python")
        declared = run([py, "-c", "import json, sys, tomllib; "
                        "print(json.dumps(tomllib.load(open(sys.argv[1], 'rb'))['project']['dependencies']))",
                        str(tree / "pyproject.toml")])
        if declared.returncode:
            return [f"could not read pyproject.toml's dependencies: {declared.stderr.strip()[-300:]}"]
        dependencies = json.loads(declared.stdout)
        install = (run([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *dependencies])
                   if python else run([uv, "pip", "install", "--quiet", "--python", py, *dependencies]))
        if install.returncode:
            return [f"could not install the engine's dependencies ({', '.join(dependencies)}): "
                    f"{install.stderr.strip()[-400:]}"]
        home = scratch / "home"
        home.mkdir()
        env = {"PATH": f"{venv / 'bin'}:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(scratch),
               "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1", "LANG": "en_US.UTF-8"}
        problems = []
        project = run([py, "-c", "import json, sys, tomllib; "
                       "print(json.dumps(tomllib.load(open(sys.argv[1], 'rb'))['project'].get('scripts') or {}))",
                       str(tree / "pyproject.toml")])
        if project.returncode or json.loads(project.stdout or "{}"):
            problems.append("pyproject.toml declares a command ([project.scripts]); the tree installs none")
        if (tree / "mobile_agent" / "__main__.py").exists():
            problems.append("mobile_agent/__main__.py makes `python -m mobile_agent` a command; the tree has none")
        modules = run([py, "-c", (
            "import importlib, pkgutil, sys\n"
            "bad = []\n"
            "for info in pkgutil.walk_packages(['mobile_agent'], 'mobile_agent.'):\n"
            "    if '.tests' in info.name or info.name.endswith('__main__'):\n"
            "        continue\n"
            "    try:\n"
            "        importlib.import_module(info.name)\n"
            "    except Exception as error:\n"
            "        bad.append(f'{info.name}: {type(error).__name__}: {error}')\n"
            "print('\\n'.join(bad)); sys.exit(1 if bad else 0)\n")], cwd=tree, env=env)
        if modules.returncode:
            problems += [f"does not import with the engine's dependencies only: {line}"
                         for line in modules.stdout.strip().splitlines()[:10]]
        return problems
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def size_lines(tree):
    total, count, by_top = 0, 0, {}
    for path in files_of(tree):
        size = path.lstat().st_size
        top = path.relative_to(tree).parts[0]
        total, count = total + size, count + 1
        by_top[top] = by_top.get(top, 0) + size
    parts = ", ".join(f"{name} {size / 1e6:.1f}" for name, size in sorted(by_top.items(), key=lambda kv: -kv[1])[:6])
    return f"{count} files, {total / 1e6:.1f} MB (MB by top-level path: {parts})"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tree", type=Path, nargs="?")
    parser.add_argument("--paths", required=True, type=Path, help="the allowlist the tree was cut with")
    parser.add_argument("--overlay", type=Path, help="the folder cut-public-tree.sh laid over the cut")
    parser.add_argument("--select", action="store_true", help="print the files on stdin that the allowlist ships")
    parser.add_argument("--root", help="the private checkout: its env files are searched for in the tree")
    parser.add_argument("--no-install", action="store_true", help="skip the fresh-venv step (it needs the network)")
    parser.add_argument("--no-gitleaks", action="store_true",
                        help="skip the gitleaks step (for tests on a machine without it; --check never passes it)")
    args = parser.parse_args(argv)
    ships, internal = read_paths(args.paths)
    if args.select:
        for rel in select(ships, internal, [line.strip() for line in sys.stdin if line.strip()]):
            print(rel)
        return 0
    if args.tree is None:
        parser.error("say which TREE to check, or --select")
    tree = args.tree.resolve()
    overlay = sorted(p.relative_to(args.overlay).as_posix() for p in files_of(args.overlay)) if args.overlay else []

    failed = 0

    def report(name, problems, extra=""):
        nonlocal failed
        failed += bool(problems)
        print(f"{'FAIL' if problems else 'ok  '} {name:<8}{extra}")
        for problem in problems[:40]:
            print(f"       {problem}")
        if len(problems) > 40:
            print(f"       ... and {len(problems) - 40} more")

    report("paths", check_paths(tree, ships, internal, overlay),
           f"{len(ships)} paths ship, {len(internal)} stay private, {len(overlay)} from the overlay")
    pii, leaks, notes = check_scan(tree, leak_entries(ships, internal, overlay), args.root)
    report("scan", pii)
    report("leaks", leaks)
    if notes:
        print("       " + "; ".join(f"{count}x {what}" for what, count in sorted(notes.items())) +
              " named in comments and docs (source folders, not links): counted, not failed")
    if args.no_gitleaks:
        print("skip gitleaks --no-gitleaks")
    else:
        report("gitleaks", check_gitleaks(tree))
    if args.no_install:
        print("skip imports --no-install")
    else:
        report("imports", check_imports(tree))
    print(f"tree: {size_lines(tree)}")
    print("PASS" if not failed else f"FAILED: {failed} check(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
