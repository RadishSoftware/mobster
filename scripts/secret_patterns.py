"""Shared secret patterns and local secret values for release scans."""

import hashlib
import os
import re
import subprocess
from pathlib import Path

FAKE_KEY_WORDS = ("test", "eval", "fake", "example", "dummy", "placeholder", "xxxx", "0000", "never", "not-real",
                  "abcdefg", "1234567")


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
