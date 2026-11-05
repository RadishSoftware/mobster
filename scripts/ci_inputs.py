#!/usr/bin/env python3
"""Hashes what one local CI stage reads from a git tree, so scripts/ci-local.sh can skip a stage whose inputs
already passed.

    ci_inputs.py keys TREE [extra:WORD...] [stage:NAME [extra:WORD...]]...
        print "NAME KEY" for each stage, KEY being 16 characters; extra: words before the first stage: apply to
        every stage, the ones after a stage: to that stage
    ci_inputs.py list STAGE TREE      print the tree entries the key covers, one per line
    ci_inputs.py stages               print the stage names scripts/ci-inputs.txt knows

The key covers the whole tree except what scripts/ci-inputs.txt says the stage never reads. A path no line
names counts as read by every stage, so a new file or folder runs every stage until someone classifies it.
Extra words (tool versions, the date, the CI scripts' own hashes) go into the key as they are. Only the standard
library, so a macOS system python3 runs it.
"""

from __future__ import annotations

import fnmatch
import functools
import hashlib
import subprocess
import sys
from pathlib import Path

MANIFEST = Path(__file__).with_name("ci-inputs.txt")
EVERY = "*"
NONE = "-"
CONTENT = "content"
EXISTS = "exists"


class Rules:
    """The lines of ci-inputs.txt: a path (or a glob per path segment) and the stages that read it. A stage written
    as NAME=exists reads only whether the path is there, not what is in it."""

    def __init__(self, text):
        self.rules = []  # (segments, {stage: CONTENT or EXISTS})
        for number, line in enumerate(text.splitlines(), 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            path, *words = line.split()
            if not words:
                raise ValueError(f"ci-inputs.txt line {number}: {path} names no stage")
            readers = {}
            for word in words:
                name, _, how = word.partition("=")
                if how not in ("", EXISTS):
                    raise ValueError(f"ci-inputs.txt line {number}: {word} ends in neither nothing nor =exists")
                readers[name] = how or CONTENT
            self.rules.append((tuple(path.strip("/").split("/")), readers))
        self.stages = sorted({stage for _, readers in self.rules for stage in readers} - {EVERY, NONE})

    @staticmethod
    def _covers(rule, path):
        return len(rule) <= len(path) and all(fnmatch.fnmatchcase(p, r) for p, r in zip(path, rule))

    def readers(self, path):
        """{stage: how} for the stages that read ``path`` (a tuple of segments): the most specific line covering it,
        or None when no line does."""
        best = None
        for rule, readers in self.rules:
            if self._covers(rule, path):
                score = (len(rule), -sum(any(c in part for c in "*?[") for part in rule))
                if best is None or score > best[0]:
                    best = (score, readers)
        return None if best is None else best[1]

    def below(self, path):
        """True when a line names something under ``path``, so a folder there has to be opened."""
        return any(len(rule) > len(path) and self._covers(rule[: len(path)], path) for rule, _ in self.rules)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE).stdout


@functools.lru_cache(maxsize=None)
def entries(repo, tree):
    """(mode, type, oid, name) of the entries directly in a tree object."""
    found = []
    for record in git(repo, "ls-tree", "-z", tree).split(b"\0"):
        if record:
            head, name = record.split(b"\t", 1)
            mode, kind, oid = head.decode().split()
            found.append((mode, kind, oid, name.decode("utf-8", "surrogateescape")))
    return found


def covered(repo, tree, stage, rules, prefix=()):
    """(path, what) of everything under ``tree`` that ``stage`` reads: files, and folders no line looks inside. What
    is the mode and object id, or "present" for a path the stage reads only to see that it is there."""
    found = []
    for mode, kind, oid, name in entries(repo, tree):
        path = prefix + (name,)
        if kind == "tree" and rules.below(path):
            found += covered(repo, oid, stage, rules, path)
            continue
        # The deepest line that covers this path decides; a folder with a line inside it was opened above, and a line
        # for the folder itself applies to what it holds that no deeper line names.
        readers = rules.readers(path)
        if readers is None or EVERY in readers:
            how = CONTENT
        elif stage in readers:
            how = readers[stage]
        else:
            continue
        found.append(("/".join(path), "present" if how == EXISTS else mode + ":" + oid))
    return found


def key(repo, tree, stage, extra, rules, manifest_text):
    if stage not in rules.stages:
        raise SystemExit(f"ci_inputs.py: no stage named {stage} in ci-inputs.txt ({', '.join(rules.stages)})")
    digest = hashlib.sha256()
    digest.update(f"stage={stage}\0manifest={hashlib.sha256(manifest_text.encode()).hexdigest()}\0".encode())
    for word in extra:
        digest.update(b"extra=" + word.encode("utf-8", "surrogateescape") + b"\0")
    for path, oid in sorted(covered(repo, tree, stage, rules)):
        digest.update(path.encode("utf-8", "surrogateescape") + b"\0" + oid.encode() + b"\0")
    return digest.hexdigest()[:16]


def main(argv):
    if not argv or argv[0] not in {"keys", "list", "stages"}:
        print(__doc__, file=sys.stderr)
        return 2
    manifest_text = MANIFEST.read_text()
    rules = Rules(manifest_text)
    repo = str(Path.cwd())
    if argv[0] == "stages":
        print("\n".join(rules.stages))
    elif argv[0] == "list" and len(argv) == 3:
        for path, oid in sorted(covered(repo, argv[2], argv[1], rules)):
            print(f"{oid}\t{path}")
    elif argv[0] == "keys" and len(argv) >= 2:
        common, per_stage = [], []  # per_stage: [name, [words]]
        for word in argv[2:]:
            kind, _, value = word.partition(":")
            if kind == "stage":
                per_stage.append([value, []])
            elif kind == "extra":
                (per_stage[-1][1] if per_stage else common).append(value)
            else:
                print(f"ci_inputs.py: expected stage:NAME or extra:WORD, got {word}", file=sys.stderr)
                return 2
        for name, words in per_stage:
            print(name, key(repo, argv[1], name, common + words, rules, manifest_text))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
