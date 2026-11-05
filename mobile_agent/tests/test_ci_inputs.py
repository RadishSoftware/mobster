import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
INPUTS = SCRIPTS / "ci_inputs.py"

TEXT = """
a            one two
a/inner      two
a/*.md       one
b            -
c            one=exists two
"""


def load():
    spec = importlib.util.spec_from_file_location("ci_inputs", INPUTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(folder, *args, env=None):
    return subprocess.run(["git", "-C", str(folder), *args], check=True, text=True, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def commit(folder, message="fixture"):
    git(folder, "add", "-A")
    git(folder, "-c", "user.name=Test", "-c", "user.email=fixture@example.invalid", "commit", "-qm", message)
    return git(folder, "rev-parse", "HEAD^{tree}")


def write(folder, files):
    for name, text in files.items():
        path = Path(folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.ci = load()
        self.rules = self.ci.Rules(TEXT)

    def test_the_most_specific_line_wins_and_a_literal_beats_a_glob(self):
        readers = lambda path: self.rules.readers(tuple(path.split("/")))
        self.assertEqual(set(readers("a/file.py")), {"one", "two"})
        self.assertEqual(set(readers("a/inner/x.py")), {"two"})
        self.assertEqual(set(readers("a/notes.md")), {"one"})
        self.assertEqual(set(readers("b/x")), {"-"})
        self.assertIsNone(readers("nowhere"))

    def test_a_stage_written_as_exists_is_keyed_on_the_path_being_there_not_on_what_is_in_it(self):
        self.assertEqual(self.rules.readers(("c", "x"))["one"], "exists")
        self.assertEqual(self.rules.readers(("c", "x"))["two"], "content")
        with tempfile.TemporaryDirectory() as folder:
            write(folder, {"a/f": "1", "c/x": "1"})
            git(folder, "init", "-q")
            before = commit(folder)
            write(folder, {"c/x": "2"})
            changed = commit(folder)
            git(folder, "rm", "-rq", "c")
            removed = commit(folder)
            key = lambda tree, stage: self.ci.key(folder, tree, stage, [], self.rules, TEXT)
            self.assertEqual(key(before, "one"), key(changed, "one"))
            self.assertNotEqual(key(before, "two"), key(changed, "two"))
            self.assertNotEqual(key(changed, "one"), key(removed, "one"))

    def test_a_tool_version_or_a_changed_manifest_changes_the_key(self):
        with tempfile.TemporaryDirectory() as folder:
            write(folder, {"a/f": "1"})
            git(folder, "init", "-q")
            tree = commit(folder)
            base = self.ci.key(folder, tree, "one", ["node=v24"], self.rules, TEXT)
            self.assertNotEqual(base, self.ci.key(folder, tree, "one", ["node=v25"], self.rules, TEXT))
            self.assertNotEqual(base, self.ci.key(folder, tree, "one", ["node=v24"], self.rules, TEXT + "\nb two\n"))

    def test_a_line_that_names_no_stage_or_a_bad_suffix_is_an_error(self):
        with self.assertRaises(ValueError):
            self.ci.Rules("a\n")
        with self.assertRaises(ValueError):
            self.ci.Rules("a one=maybe\n")


class ManifestTests(unittest.TestCase):
    def test_the_manifest_covers_all_tracked_paths_and_new_paths(self):
        ci = load()
        rules = ci.Rules(ci.MANIFEST.read_text())
        self.assertEqual(rules.stages, ["backend", "lint"])
        try:
            files = git(REPO, "ls-files", "-z").split("\0")
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise unittest.SkipTest("not a git checkout")
        for name in filter(None, [*files, "new-folder/new-file"]):
            self.assertEqual(rules.readers(tuple(name.split("/"))),
                             {"backend": ci.CONTENT, "lint": ci.CONTENT}, name)


if __name__ == "__main__":
    unittest.main()
