"""The ios-testing Agent Skill: copied into each client's skills folder by `mobster mcp install --with-skill`.

The skill's source of truth is ``skills/ios-testing/`` at the repository's root, where `npx skills add` finds it.
It was called ``mobster`` before 0.3: installing or removing it also removes an old ``mobster`` folder that is
Mobster's (its SKILL.md says ``source: mobster-cli``), so nobody ends up with both. Two copies are generated from
the source, and a test fails when either drifts:

- ``plugins/mobster/skills/ios-testing/``, inside the Claude Code plugin (``mobster:ios-testing``), which is
  installed by copying its folder;
- ``skill_files.py`` beside this module, so a pip install and the frozen binary carry the files without data
  files in their build specs.

``python -m mobile_agent.integrations.skill`` regenerates both after an edit to the source.
"""

import os
from pathlib import Path
import re
import shutil
import sys

NAME = "ios-testing"
OLD_NAMES = ("mobster",)  # the skill's folder before the rename: removed when it is Mobster's
MARKER = re.compile(r"^\s*source:\s*mobster-cli\s*$", re.M)
ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "skills" / NAME
PLUGIN_COPY = ROOT / "plugins" / "mobster" / "skills" / NAME
BUNDLE_MODULE = Path(__file__).with_name("skill_files.py")


def files():
    """{relative path: text} of the skill this build carries."""
    from .skill_files import FILES
    return dict(FILES)


def read_tree(folder):
    """{relative path: text} of every file under ``folder``."""
    folder = Path(folder)
    return {path.relative_to(folder).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(folder.rglob("*")) if path.is_file() and path.name != ".DS_Store"}


def ours(folder):
    """Whether the skill folder holds Mobster's skill (its SKILL.md says ``source: mobster-cli``)."""
    try:
        return bool(MARKER.search((Path(folder) / "SKILL.md").read_text(encoding="utf-8")))
    except OSError:
        return False


def migrate(skills_dir, dry_run=False):
    """Remove the skill's old folder (``skills_dir``/mobster) when it is Mobster's. Returns the paths removed."""
    removed = []
    for old in OLD_NAMES:
        folder = Path(skills_dir) / old
        if folder.is_symlink() or not folder.is_dir() or not ours(folder):
            continue
        if not dry_run:
            for path in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if path.is_file() and (path.name == "SKILL.md" or path.parent.name == "references"):
                    path.unlink()
                elif path.is_dir():
                    _rmdir(path)
            _rmdir(folder)
        removed.append(str(folder))
    return removed


def install(skills_dir, dry_run=False):
    """Copy the skill to ``skills_dir``/ios-testing, and remove its old ``mobster`` folder. Returns (action, path,
    message)."""
    from .clients import write_atomic
    migrate(skills_dir, dry_run)
    target = Path(skills_dir) / NAME
    if target.is_symlink():
        return "skipped", str(target), "it is a link another installer made (such as `npx skills add`); left as is"
    bundle = files()
    if target.exists():
        if not ours(target):
            return "error", str(target), f"a different skill named {NAME} is there; left as is"
        if all(_same(target / name, text) for name, text in bundle.items()):
            return "unchanged", str(target), ""
        action = "updated"
    else:
        action = "added"
    if not dry_run:
        for name, text in bundle.items():
            write_atomic(target / name, text, keep_backup=False)
    return action, str(target), ""


def remove(skills_dir, dry_run=False):
    """Delete Mobster's files from ``skills_dir``/ios-testing (and the old ``mobster`` folder), and the folder when
    nothing else is left in it."""
    migrate(skills_dir, dry_run)
    target = Path(skills_dir) / NAME
    if target.is_symlink():
        return "skipped", str(target), "it is a link another installer made; remove it with that installer"
    if not target.exists():
        return "absent", str(target), ""
    if not ours(target):
        return "error", str(target), "it isn't Mobster's skill; left as is"
    if dry_run:
        return "removed", str(target), ""
    for name in files():
        path = target / name
        if path.is_file():
            path.unlink()
    for folder in sorted((p for p in target.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        _rmdir(folder)
    _rmdir(target)
    if target.exists():
        return "removed", str(target), "files that aren't Mobster's are still in the folder"
    return "removed", str(target), ""


def _rmdir(folder):
    try:
        folder.rmdir()
    except OSError:
        pass


def _same(path, text):
    try:
        return path.read_text(encoding="utf-8") == text
    except OSError:
        return False


# ------------------------------------------------------------------------------------------- generation

def render_module(tree):
    lines = [f'"""Generated by `python -m mobile_agent.integrations.skill` from skills/{NAME}/. Don\'t edit."""',
             "", "FILES = {"]
    for name, text in sorted(tree.items()):
        lines.append(f"    {name!r}: {text!r},")
    lines.append("}")
    return "\n".join(lines) + "\n"


def sync(root=ROOT):
    """Regenerate the plugin's copy and skill_files.py from skills/ios-testing. Returns the paths it changed."""
    source = Path(root) / "skills" / NAME
    tree = read_tree(source)
    changed = []
    module = Path(root) / "mobile_agent" / "integrations" / "skill_files.py"
    text = render_module(tree)
    if not module.is_file() or module.read_text(encoding="utf-8") != text:
        module.write_text(text, encoding="utf-8")
        changed.append(module)
    copy = Path(root) / "plugins" / "mobster" / "skills" / NAME
    if copy.exists() and read_tree(copy) == tree:
        return changed
    if copy.exists():
        shutil.rmtree(copy)
    for name, body in tree.items():
        path = copy / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    changed.append(copy)
    return changed


if __name__ == "__main__":
    for path in sync():
        print(f"wrote {os.path.relpath(path)}")
    sys.exit(0)
