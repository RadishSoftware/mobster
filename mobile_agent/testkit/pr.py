import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..paths import write_text_atomic

UI_SUFFIXES = (".swift", ".storyboard", ".xib")
STRING_SUFFIXES = (".strings", ".xcstrings", ".stringsdict")
CHECK_SUFFIXES = (".yaml", ".yml", ".json")
GIT_TIMEOUT = 60
MAX_FILES = 400            # a bigger diff is read up to here, and the verdict says so
MAX_DIFF_BYTES = 8_000_000
MAX_DRAFTS = 3             # drafted checks per pull request; each may cost a model run
DRAFT_IDS, DRAFT_TEXTS = 4, 2
DRAFTS_DIR = "drafts/pr"   # under .mobster/: not .mobster/checks, so `mobster test` never runs a draft by itself
MIN_STRING = 4             # shorter strings ("OK", "Done") name no screen
SUFFIXES = ("ViewController", "Controller", "ViewModel", "Screen", "View", "Sheet", "Page", "Cell", "Row")
GENERIC = frozenset({"content", "main", "root", "app", "base", "common", "shared", "util", "utils", "helpers",
                     "extensions", "components", "preview", "previews", "button", "text", "label"})

_ID = re.compile(r"""(?:accessibilityIdentifier\s*\(\s*|accessibilityIdentifier\s*=\s*)"((?:[^"\\]|\\.){1,120})\"""")
_TEXT_CALLS = re.compile(
    r"""\b(?:Text|Button|Label|Toggle|Link|Section|Menu|Picker|TextField|SecureField|NavigationLink|Tab|"""
    r"""navigationTitle|alert|confirmationDialog|accessibilityLabel|accessibilityValue|accessibilityHint|"""
    r"""NSLocalizedString|LocalizedStringKey|LocalizedStringResource)\s*\(\s*(?:localized:\s*|verbatim:\s*)?"""
    r""""((?:[^"\\]|\\.){1,200})\"""")
_LOCALIZED = re.compile(r"""String\s*\(\s*localized:\s*"((?:[^"\\]|\\.){1,200})\"""")
_UIKIT_TEXT = re.compile(r"""\.(?:text|title|placeholder|accessibilityLabel)\s*=\s*"((?:[^"\\]|\\.){1,200})\"""")
_SET_TITLE = re.compile(r"""setTitle\s*\(\s*"((?:[^"\\]|\\.){1,200})\"""")
_LINK = re.compile(r""""([a-z][a-z0-9+.\-]{1,30}://[^"\s\\]{0,200})\"""")
_STRINGS_LINE = re.compile(r'^\s*"((?:[^"\\]|\\.){1,200})"\s*=\s*"((?:[^"\\]|\\.){0,400})"\s*;')
_VIEW_TYPE = re.compile(r"\b(?:struct|class|final\s+class)\s+([A-Z][A-Za-z0-9_]{1,80})\s*:\s*([^{]*)\{")
_INTERPOLATION = re.compile(r"\\\(.*?\)")
_WORD_BREAK = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


class PrError(Exception):
    """A usage problem: not a git repository, an unknown base. One plain sentence."""


def run_git(argv, cwd):
    """(exit code, stdout) of ``git argv`` in ``cwd``; never raises."""
    try:
        result = subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True, timeout=GIT_TIMEOUT,
                                stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    out = result.stdout[:MAX_DIFF_BYTES].decode("utf-8", errors="replace")
    return result.returncode, out


def default_base(git=run_git, cwd=None, env=None):
    """The branch a pull request merges into: GitHub's base ref in CI, else origin/main, main or master."""
    env = os.environ if env is None else env
    candidates = []
    if env.get("GITHUB_BASE_REF"):
        candidates += [f"origin/{env['GITHUB_BASE_REF']}", env["GITHUB_BASE_REF"]]
    candidates += ["origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        code, _ = git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
        if code == 0:
            return ref
    raise PrError("Mobster couldn't find the branch this PR merges into. Pass --base, such as --base origin/main.")


@dataclass
class Change:
    """One changed file: its path relative to the repository, how it changed, and its changed lines."""
    path: str
    status: str                    # A added, M modified, D deleted, R renamed
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)

    @property
    def suffix(self):
        return Path(self.path).suffix.lower()


@dataclass
class Changes:
    base: str                      # as given
    merge_base: str                # the commit the diff starts from
    head: str                      # HEAD's commit
    top: Path                      # the repository's top folder
    files: list                    # of Change
    truncated: bool = False
    uncommitted: bool = False      # the working tree has changes beyond HEAD (they're included)

    def public(self):
        return {"base": self.base, "mergeBase": self.merge_base, "head": self.head, "files": len(self.files),
                "truncated": self.truncated, "uncommitted": self.uncommitted}


def read_changes(root, base=None, *, git=run_git, env=None):
    """The changes of the branch checked out in ``root`` since it left ``base``, uncommitted work included (an agent
    tests what it just wrote)."""
    root = Path(root)
    code, top = git(["rev-parse", "--show-toplevel"], root)
    if code != 0 or not top.strip():
        raise PrError(f"{root} isn't in a git repository, so Mobster can't tell what the PR changed.")
    top = Path(top.strip())
    base = base or default_base(git, root, env)
    code, merge_base = git(["merge-base", base, "HEAD"], root)
    if code != 0 or not merge_base.strip():
        raise PrError(f"Mobster couldn't find where this branch left {base}. Fetch it (git fetch origin), or pass "
                      "another --base.")
    merge_base = merge_base.strip()
    _, head = git(["rev-parse", "HEAD"], root)
    code, diff = git(["diff", "--no-color", "--no-ext-diff", "--unified=0", "-M", merge_base, "--"], root)
    if code != 0:
        raise PrError(f"git diff from {base} failed. Run `git diff {base}` to see why.")
    files = parse_diff(diff)
    _, status = git(["status", "--porcelain", "--untracked-files=all"], root)
    # The drafts an earlier run wrote are Mobster's output, never the app's: they don't make HEAD's verdict another's.
    drafts = _relative_to((root / ".mobster" / DRAFTS_DIR).resolve(), top.resolve())
    uncommitted = any(line[3:].strip().strip('"') and not (drafts and line[3:].strip().strip('"').startswith(drafts))
                      for line in status.splitlines())
    for line in status.splitlines():
        if line.startswith("?? "):
            path = line[3:].strip().strip('"')
            if Path(path).suffix.lower() in UI_SUFFIXES + STRING_SUFFIXES + CHECK_SUFFIXES:
                files.append(_untracked(top, path))
    truncated = len(files) > MAX_FILES
    return Changes(base=base, merge_base=merge_base, head=head.strip(), top=top, files=files[:MAX_FILES],
                   truncated=truncated or len(diff) >= MAX_DIFF_BYTES, uncommitted=uncommitted)


def _relative_to(path, top):
    """``path`` relative to ``top`` with a trailing slash, as git status names what's under it; None outside it."""
    try:
        return path.relative_to(top).as_posix().rstrip("/") + "/"
    except ValueError:
        return None


def _untracked(top, path):
    try:
        text = (top / path).read_text(encoding="utf-8", errors="replace")[:200_000]
    except OSError:
        text = ""
    return Change(path, "A", added=text.splitlines())


def parse_diff(text):
    """``git diff --unified=0`` as Changes."""
    files, current = [], None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = Change(path="", status="M")
            files.append(current)
            match = re.match(r'diff --git "?a/(.+?)"? "?b/(.+?)"?$', line)
            if match:
                current.path = match.group(2)
        elif current is None:
            continue
        elif line.startswith("new file mode"):
            current.status = "A"
        elif line.startswith("deleted file mode"):
            current.status = "D"
        elif line.startswith("rename to "):
            current.status, current.path = "R", line[len("rename to "):]
        elif line.startswith("+++ "):
            if line[4:].startswith("b/"):
                current.path = line[6:]
        elif line.startswith("--- "):
            if current.status == "D" and line[4:].startswith("a/"):
                current.path = line[6:]
        elif line.startswith("+"):
            current.added.append(line[1:])
        elif line.startswith("-"):
            current.removed.append(line[1:])
    return [change for change in files if change.path]


@dataclass
class Screen:
    """A changed view (or a strings file) and what it shows that changed."""
    name: str                      # PaywallView; for a strings file, its file name
    file: str
    slug: str                      # paywall: what a tag, file name or URL would call it ("" when too generic)
    ids: list = field(default_factory=list)          # changed accessibility ids, added ones first
    texts: list = field(default_factory=list)        # changed user-facing strings, added ones first
    added_ids: list = field(default_factory=list)
    added_texts: list = field(default_factory=list)
    links: list = field(default_factory=list)        # deep links in the changed lines or the file
    deleted: bool = False
    strings_only: bool = False

    def public(self):
        return {"name": self.name, "file": self.file, "ids": self.ids[:20], "texts": self.texts[:20],
                "links": self.links[:5], "deleted": self.deleted}


def slug_of(name):
    """``PaywallView`` -> ``paywall``; ``PlanPickerSheet`` -> ``plan-picker``; generic names -> ""."""
    stem = name
    for suffix in SUFFIXES:
        if stem.endswith(suffix) and len(stem) > len(suffix):
            stem = stem[: -len(suffix)]
            break
    words = [word.lower() for word in _WORD_BREAK.split(stem.replace("_", " ").replace("-", " ")) for word in
             word.split() if word]
    slug = "-".join(words)
    return "" if not slug or slug in GENERIC or len(slug) < 3 else slug


def _flat(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _unescape(text):
    return _INTERPOLATION.sub("", text.replace('\\"', '"').replace("\\n", " ")).strip()


def ids_in(lines):
    return _dedupe(_unescape(match.group(1)) for line in lines for match in _ID.finditer(line))


def texts_in(lines):
    found = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        for pattern in (_TEXT_CALLS, _LOCALIZED, _UIKIT_TEXT, _SET_TITLE):
            found += [_unescape(match.group(1)) for match in pattern.finditer(line)]
    return _dedupe(text for text in found if len(text) >= MIN_STRING and "://" not in text)


def links_in(lines):
    return _dedupe(match.group(1) for line in lines for match in _LINK.finditer(line)
                   if not match.group(1).startswith(("http://", "https://")))


def strings_file_texts(lines):
    """The values in changed lines of a .strings file, or of an .xcstrings file's ``"value" : "…"``."""
    out = []
    for line in lines:
        match = _STRINGS_LINE.match(line)
        if match:
            out.append(_unescape(match.group(2)))
            continue
        match = re.search(r'"value"\s*:\s*"((?:[^"\\]|\\.){1,400})"', line)
        if match:
            out.append(_unescape(match.group(1)))
    return _dedupe(text for text in out if len(text) >= MIN_STRING)


def _dedupe(items):
    return list(dict.fromkeys(item for item in items if item))


def view_name(text, path):
    """The first SwiftUI view or UIKit view controller a file declares, else the file's name."""
    for match in _VIEW_TYPE.finditer(text or ""):
        name, conforms = match.group(1), match.group(2)
        if re.search(r"\bView\b|UIViewController|UITableViewController|UICollectionViewController|UIView\b",
                     conforms):
            return name
    return Path(path).stem


def screens_in(changes, root=None):
    """One Screen per changed Swift, storyboard or strings file."""
    top = Path(changes.top if root is None else root)
    screens = []
    for change in changes.files:
        if change.suffix not in UI_SUFFIXES + STRING_SUFFIXES:
            continue
        parts = {part.lower() for part in Path(change.path).parts}
        stem = Path(change.path).stem
        if parts & {"tests", "uitests", "test"} or stem.endswith(("Tests", "Test")) or "preview" in stem.lower():
            continue
        if change.suffix in STRING_SUFFIXES:
            texts = strings_file_texts(change.added + change.removed)
            if texts:
                screens.append(Screen(name=Path(change.path).name, file=change.path, slug="", texts=texts,
                                      added_texts=strings_file_texts(change.added), strings_only=True))
            continue
        try:
            text = (top / change.path).read_text(encoding="utf-8", errors="replace")[:500_000] \
                if change.status != "D" else ""
        except OSError:
            text = ""
        name = view_name(text, change.path) if change.suffix == ".swift" else stem
        lines = change.added + change.removed
        added_ids, added_texts = ids_in(change.added), texts_in(change.added)
        ids = _dedupe(added_ids + ids_in(change.removed))
        texts = _dedupe(added_texts + texts_in(change.removed))
        if change.suffix in (".storyboard", ".xib"):
            ids = _dedupe(ids + re.findall(r'identifier="([^"]{1,120})"', "\n".join(lines)))
            texts = _dedupe(texts + [t for t in re.findall(r'(?:title|text)="([^"]{4,200})"', "\n".join(lines))])
        links = _dedupe(links_in(lines) + links_in(text.splitlines()))
        screens.append(Screen(name=name, file=change.path, slug=slug_of(name), ids=ids, texts=texts,
                              added_ids=added_ids, added_texts=added_texts, links=links,
                              deleted=change.status == "D"))
    return screens


@dataclass
class Pick:
    entry: object                  # discover.Entry
    reasons: list                  # plain words: "changed in this PR", "expects id plan_annual", ...
    screens: list                  # names of the screens it covers

    def public(self):
        return {"file": self.entry.file, "name": self.entry.name, "reasons": self.reasons[:6],
                "screens": self.screens}


def _walk_strings(value, key=None):
    """(key, string) for every string in an assertion's dict form."""
    if isinstance(value, dict):
        for name, item in value.items():
            yield from _walk_strings(item, name)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item, key)
    elif isinstance(value, str):
        yield key, value


def _matches(pattern_or_text, candidate):
    """A selector string against a changed id or string: /regex/ by search, else equal (case-insensitive)."""
    raw = str(pattern_or_text)
    match = re.fullmatch(r"/(.*)/(i?)", raw, re.S)
    if match:
        try:
            return re.search(match.group(1), candidate, re.I if match.group(2) else 0) is not None
        except re.error:
            return False
    return raw.strip().casefold() == candidate.strip().casefold()


def _check_words(entry):
    check = entry.check
    pieces = [Path(entry.file).stem, check.name if check is not None else "", *(entry.tags or ())]
    if check is not None and check.open_url:
        pieces.append(check.open_url.split("://", 1)[-1])
    return {_flat(piece) for piece in pieces if piece}, " ".join(pieces).lower()


def reasons_for(entry, screen):
    """Why ``entry`` covers ``screen``, strongest first; [] when it doesn't."""
    check = entry.check
    if check is None:
        return []
    reasons = []
    expected = [(key, text) for assertion in check.expect for key, text in _walk_strings(assertion.to_dict())]
    for changed in screen.ids:
        if any(key == "id" and _matches(text, changed) for key, text in expected):
            reasons.append(f"expects id {changed}")
    for changed in screen.texts:
        if any(key in ("text", "no_text", "label", "value", "equals") and _matches(text, changed)
               for key, text in expected):
            reasons.append(f"expects “{changed}”")
        elif len(changed) >= 5 and any(changed.casefold() in step.casefold() for step in check.steps):
            reasons.append(f"a step mentions “{changed}”")
    if screen.slug:
        flat_words, _ = _check_words(entry)
        slug = _flat(screen.slug)
        if any(slug == word or (len(slug) >= 5 and slug in word) for word in flat_words):
            reasons.append(f"names {screen.name}")
    if check.open_url and any(check.open_url.rstrip("/") == link.rstrip("/") for link in screen.links):
        reasons.append(f"opens {check.open_url}")
    return _dedupe(reasons)


def pick(entries, screens, changes=None):
    """The checks the PR needs, in suite order, each with why. A check file the PR changed is always picked."""
    changed_files = set()
    if changes is not None:
        changed_files = {str((Path(changes.top) / change.path).resolve()) for change in changes.files
                         if change.status != "D"}
    picked = []
    for entry in entries:
        reasons, covers = [], []
        if str(Path(entry.path).resolve()) in changed_files:
            reasons.append("changed in this PR")
        for screen in screens:
            found = reasons_for(entry, screen)
            if found:
                reasons += found
                covers.append(screen.name)
        if reasons:
            picked.append(Pick(entry, _dedupe(reasons), _dedupe(covers)))
    return picked


def uncovered(screens, picked):
    """Changed screens no picked check covers and a check could see: not deleted, not a strings file, with a name."""
    covered = {name for item in picked for name in item.screens}
    return [screen for screen in screens if screen.name not in covered and not screen.deleted
            and not screen.strings_only and screen.slug and (screen.added_ids or screen.added_texts)]


def app_of(entries, built=None):
    """The app a draft names: the build Mobster just made, else the app most checks name."""
    if built is not None and built.get("bundle"):
        return {"bundle": built["bundle"], **({"path": built["path"]} if built.get("path") else {})}
    counts = {}
    for entry in entries:
        check = entry.check
        if check is None or not check.bundle_id:
            continue
        key = (check.bundle_id, check.app_path)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    bundle, path = max(counts, key=counts.get)
    return {"bundle": bundle, **({"path": path} if path else {})}


def _words(name):
    slug = slug_of(name) or _flat(name)
    return slug.replace("-", " ")


def draft_data(screen, app, root=None):
    expect = [{"visible": {"id": item}} for item in screen.added_ids[:DRAFT_IDS]]
    expect += [{"text": item} for item in screen.added_texts[:DRAFT_TEXTS]]
    words = _words(screen.name)
    data = {"version": 1, "name": f"The {words} screen shows what this PR changed"[:120]}
    path = app.get("path")
    if path and root is not None:
        try:
            path = str(Path(path).resolve().relative_to(Path(root).resolve()))
        except ValueError:
            pass
    data["app"] = {"bundle": app["bundle"], **({"path": path} if path else {})}
    data["tags"] = [tag for tag in (re.sub(r"[^a-z0-9_-]", "", screen.slug)[:40], "draft") if tag]
    link = next((link for link in screen.links if _flat(screen.slug) in _flat(link)), None)
    if link:
        data["launch"] = {"url": link}
    else:
        data["steps"] = [f"Open the {words} screen"]
    data["expect"] = expect
    return data


def draft_yaml(data, screen):
    import yaml
    head = (f"# Drafted by `mobster test pr` for {screen.file}, which no check covered.\n"
            "# Review it, then move it into .mobster/checks/ to keep it. Until then it runs as a draft and never "
            "fails the PR.\n")
    return head + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def draft_checks(screens, picked, *, app, root, limit=MAX_DRAFTS):
    """Write one draft per uncovered screen to .mobster/drafts/pr/<slug>.yaml. Returns [(Screen, path)]."""
    if app is None:
        return []
    folder = Path(root) / ".mobster" / DRAFTS_DIR
    out = []
    for screen in uncovered(screens, picked)[:limit]:
        data = draft_data(screen, app, root)
        if not data["expect"]:
            continue
        path = write_text_atomic(folder / f"{screen.slug}.yaml", draft_yaml(data, screen))
        out.append((screen, path))
    return out


@dataclass
class PrPlan:
    """What `mobster test pr` will run, before it builds or runs anything."""
    changes: Changes
    screens: list
    picked: list                   # of Pick
    fallback: Optional[str] = None  # "smoke" when no screen changed and the smoke checks run instead
    drafts: list = field(default_factory=list)   # [(Screen, Path)]

    def public(self, root=None):
        def rel(path):
            try:
                return str(Path(path).resolve().relative_to(Path(root).resolve())) if root else str(path)
            except ValueError:
                return str(path)
        return {"changes": self.changes.public(), "screens": [screen.public() for screen in self.screens],
                "checks": [item.public() for item in self.picked], "fallback": self.fallback,
                "drafts": [{"screen": screen.name, "file": rel(path)} for screen, path in self.drafts]}


def as_json(value):
    return json.dumps(value, ensure_ascii=False, indent=1)
