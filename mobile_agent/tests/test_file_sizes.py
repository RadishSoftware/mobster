"""Files stay under 1,000 lines (AGENTS.md, Code quality). The files already past it are listed with their size and
may only shrink: an entry is a ceiling, and it goes once the file is under the limit or gone."""

import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIMIT = 1000
# Existing debt measured on 19 Oct 2026 after the Runtime split. This initial list is exact; later changes may
# only lower it. kb_data is generated lookup data; the others need separate ownership-aware decomposition rather
# than mechanical file slicing (testkit command dispatch).
OVER = {
    "mobile_agent/ios/kb_data.py": 1588,
}


def lines(path):
    with path.open("rb") as source:
        return sum(1 for _line in source)


def sources(root=ROOT):
    for directory, folders, files in os.walk(root / "mobile_agent"):
        folders[:] = sorted(name for name in folders
                            if not name.startswith(".") and name not in {"tests", "__pycache__", "node_modules"})
        for name in sorted(files):
            if name.endswith(".py") and not name.startswith("."):
                path = Path(directory) / name
                yield path.relative_to(root).as_posix(), path


class FileSizes(unittest.TestCase):
    def test_every_source_file_is_under_the_limit_or_within_its_ceiling(self):
        over = {relative: (count, OVER.get(relative, LIMIT)) for relative, path in sources()
                if (count := lines(path)) > OVER.get(relative, LIMIT)}
        self.assertEqual(over, {}, "over 1,000 lines (or past its ceiling in OVER): split the file first")

    def test_every_ceiling_still_names_a_file_over_the_limit(self):
        stale = [relative for relative in OVER
                 if not (ROOT / relative).exists() or lines(ROOT / relative) <= LIMIT]
        self.assertEqual(stale, [], "under 1,000 lines now, or gone: drop its entry from OVER")


class SizeGuardInputs(unittest.TestCase):
    def test_counts_the_final_line_without_a_newline(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "source.py"
            for content, expected in ((b"", 0), (b"one", 1), (b"one\n", 1), (b"one\ntwo", 2)):
                path.write_bytes(content)
                self.assertEqual(lines(path), expected)

    def test_scans_nested_sources_without_dependency_or_test_trees(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            kept = ["mobile_agent/agent.py", "mobile_agent/pkg/flow.py"]
            ignored = ["mobile_agent/.venv/lib/dependency.py", "mobile_agent/pkg/tests/test_flow.py",
                       "mobile_agent/node_modules/dependency.py", "mobile_agent/__pycache__/generated.py",
                       "mobile_agent/.scratch.py"]
            for name in kept + ignored:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# source\n")
            self.assertEqual([relative for relative, _path in sources(root)], kept)
