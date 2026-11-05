import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mobile_agent.tests.timing import bound

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
INPUTS = SCRIPTS / "ci_inputs.py"
LOCAL = SCRIPTS / "ci-local.sh"
HAVE_CI = INPUTS.is_file() and (SCRIPTS / "ci-inputs.txt").is_file() and LOCAL.is_file()

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


@unittest.skipUnless(HAVE_CI, "local CI isn't in this checkout")
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


@unittest.skipUnless(HAVE_CI and (REPO / ".git").exists(), "local CI or the git checkout isn't here")
class ManifestTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ci = load()
        cls.rules = cls.ci.Rules(cls.ci.MANIFEST.read_text())
        try:
            listing = git(REPO, "ls-files", "-z")
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise unittest.SkipTest("not a git checkout")
        cls.files = [name for name in listing.split("\0") if name]

    def test_every_top_level_path_has_a_line_of_its_own(self):
        named = {rule[0] for rule, _ in self.rules.rules if len(rule) == 1}
        top = {name.split("/")[0] for name in self.files}
        self.assertEqual(sorted(top - named), [],
                         "add a line to scripts/ci-inputs.txt for each: the stages that read it, or - for none")

    def test_a_line_without_a_glob_names_a_path_that_exists(self):
        top = set(self.files)
        for rule, _ in self.rules.rules:
            if any(c in part for part in rule for c in "*?["):
                continue
            path = "/".join(rule)
            self.assertTrue(path in top or any(name.startswith(path + "/") for name in self.files),
                            f"scripts/ci-inputs.txt names {path}, which is not in the repository")

    def test_the_stages_the_manifest_knows_are_the_stages_the_script_runs(self):
        script = LOCAL.read_text()
        run = set(re.findall(r"^if will_run (\w+); then job \1 \1; fi$", script, re.M))
        self.assertEqual(run, set(self.rules.stages))

    def readers(self, path):
        parts = tuple(path.split("/"))
        found = self.rules.readers(parts)
        return None if found is None else set(found) - {self.ci.NONE}

    def assertReads(self, stage, path, source):
        readers = self.readers(path)
        if readers is None or self.ci.EVERY in readers:
            return
        self.assertIn(stage, readers, f"{source} reads {path} but scripts/ci-inputs.txt does not list {stage} for it")

    @staticmethod
    def repo_relative(base, literal):
        target = Path(os.path.normpath(base / literal))
        try:
            return target.relative_to(REPO)
        except ValueError:
            return None

    def test_every_path_a_stage_reaches_outside_its_folder_is_listed_for_that_stage(self):
        escapes = re.compile(r"""['"`]((?:\.\./)+[^'"`\s$]*)['"`]""")
        folders = {"dashboard": "dashboard", "site": "site", "relay": "relay", "license": "services/license-api"}
        suffixes = {".ts", ".tsx", ".js", ".mjs", ".cjs", ".json"}
        skip = ("node_modules/", "/dist/", "/out/", "/.next/", "package-lock.json")
        for stage, folder in folders.items():
            for name in self.files:
                path = REPO / name
                if (not name.startswith(folder + "/") or path.suffix not in suffixes
                        or any(s in "/" + name for s in skip)):
                    continue
                for literal in escapes.findall(path.read_text(errors="replace")):
                    target = self.repo_relative(path.parent, literal)
                    if target is None or str(target).startswith(folder + "/") or not (REPO / target).exists():
                        continue
                    self.assertReads(stage, str(target), name)

    def test_every_path_the_desktop_crate_includes_or_joins_from_outside_it_is_listed_for_desktop(self):
        include = re.compile(r'include_(?:str|bytes)!\(\s*"((?:\.\./)+[^"]*)"')
        manifest_join = re.compile(r'CARGO_MANIFEST_DIR"\)\)\s*\.join\("([^"]*)"\)')
        crate = REPO / "desktop/src-tauri"
        found = 0
        for name in self.files:
            if not (name.startswith("desktop/src-tauri/") and name.endswith(".rs")):
                continue
            path = REPO / name
            source = path.read_text(errors="replace")
            for base, literals in ((path.parent, include.findall(source)), (crate, manifest_join.findall(source))):
                for literal in literals:
                    target = self.repo_relative(base, literal)
                    if (target is None or str(target) == "." or str(target).startswith("desktop/")
                            or not (REPO / target).exists()):
                        continue
                    found += 1
                    self.assertReads("desktop", str(target), name)
        self.assertGreater(found, 5, "the scan found no include_str! that leaves desktop/: has the pattern changed?")

    def test_the_cross_folder_reads_no_scan_finds_are_listed(self):
        self.assertReads("desktop", "mobile_agent/server.py", "the desktop crate")
        for path in ("scripts/perf_budget.py", "scripts/perf-budgets.json"):
            self.assertReads("dashboard", path, "the dashboard job's bundle budget")


@unittest.skipUnless(HAVE_CI and shutil.which("python3"), "local CI or python3 isn't here")
class PlanTests(unittest.TestCase):

    FILES = {
        "mobile_agent/requirements.txt": "",
        "dashboard/package.json": "{}", "dashboard/package-lock.json": "{}", "dashboard/src/a.ts": "1",
        "site/package-lock.json": "{}", "site/page.tsx": "1",
        "docs/note.md": "1",
    }

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name) / "repo"
        self.tmp = Path(self.folder.name) / "tmp"
        self.scripts = Path(self.folder.name) / "scripts"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("MOBSTER_CI_")}
        self.root.mkdir()
        self.tmp.mkdir()
        git(self.root, "init", "-q")
        git(self.root, "remote", "add", "origin", "https://example.invalid/mobster.git")
        write(self.root, self.FILES)
        commit(self.root)
        shutil.copytree(SCRIPTS, self.scripts, ignore=shutil.ignore_patterns("__pycache__"))

    def run_script(self, *args):
        return subprocess.run(["bash", str(self.scripts / "ci-local.sh"), *args], cwd=self.root,
                              env={**self.env, "TMPDIR": str(self.tmp)}, text=True, timeout=bound(60),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def plan(self, *args):
        run = self.run_script("--plan", "--full", *args)
        self.assertEqual(run.returncode, 0, run.stdout)
        return run.stdout

    def stages(self, output):
        runs, cached = {}, {}
        for line in output.splitlines():
            found = re.match(r"\s+(\w+) runs \(inputs (\w+)\)", line)
            if found:
                runs[found.group(1)] = found.group(2)
            found = re.match(r"\s+(\w+) cached \(inputs (\w+) green at (\w+)\)", line)
            if found:
                cached[found.group(1)] = found.group(2)
        return runs, cached

    def pass_stages(self, runs, *names):
        for name in names:
            record = self.root / ".git/ci-cache/stage" / name / runs[name]
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text("abc1234 2026-10-14 12:00\n")

    def test_a_stage_whose_inputs_passed_before_is_skipped_and_says_which_run_it_trusts(self):
        runs, _ = self.stages(self.plan())
        self.pass_stages(runs, "dashboard", "site")
        output = self.plan()
        runs_now, cached = self.stages(output)
        self.assertEqual(set(cached), {"dashboard", "site"})
        self.assertEqual(set(runs_now), {"backend", "desktop", "versions"})
        self.assertIn(f"dashboard cached (inputs {runs['dashboard']} green at abc1234)", output)

    def test_an_edit_outside_a_stage_leaves_it_skipped_and_an_edit_inside_runs_it_again(self):
        runs, _ = self.stages(self.plan())
        self.pass_stages(runs, "dashboard", "site", "desktop")
        write(self.root, {"site/page.tsx": "2"})
        commit(self.root)
        runs_now, cached = self.stages(self.plan())
        self.assertEqual(set(cached), {"dashboard", "desktop"})
        self.assertIn("site", runs_now)
        self.assertIn("backend", runs_now)
        write(self.root, {"dashboard/src/a.ts": "2"})
        commit(self.root)
        runs_now, cached = self.stages(self.plan())
        self.assertIn("dashboard", runs_now)
        self.assertEqual(set(cached), {"desktop"})

    def test_a_path_no_line_in_the_manifest_names_runs_every_stage(self):
        runs, _ = self.stages(self.plan())
        self.pass_stages(runs, *runs)
        self.assertEqual(self.stages(self.plan())[0], {})
        write(self.root, {"brand-new-folder/x": "1"})
        commit(self.root)
        runs_now, cached = self.stages(self.plan())
        self.assertEqual(cached, {})
        self.assertEqual(set(runs_now), set(runs))

    def test_no_cache_runs_every_stage_even_when_all_of_them_passed(self):
        runs, _ = self.stages(self.plan())
        self.pass_stages(runs, *runs)
        runs_now, cached = self.stages(self.plan("--no-cache"))
        self.assertEqual((set(runs_now), cached), (set(runs), {}))

    def test_a_change_to_the_ci_scripts_themselves_runs_every_stage(self):
        runs, _ = self.stages(self.plan())
        self.pass_stages(runs, *runs)
        with open(self.scripts / "ci-local.sh", "a") as script:
            script.write("\n# a change to how the stages run\n")
        runs_now, cached = self.stages(self.plan())
        self.assertEqual((set(runs_now), cached), (set(runs), {}))

    def test_without_the_hashing_script_every_stage_runs_and_the_run_says_the_cache_is_off(self):
        runs, _ = self.stages(self.plan())
        self.pass_stages(runs, *runs)
        (self.scripts / "ci_inputs.py").unlink()
        output = self.plan()
        self.assertIn("the stage cache is off", output)
        self.assertEqual(self.stages(output)[1], {})

    def test_the_cache_stays_in_the_repository_whatever_the_backends_cache_variable_says(self):
        elsewhere = Path(self.folder.name) / "elsewhere"
        self.env["MOBSTER_CI_CACHE"] = str(elsewhere)
        self.plan()
        self.assertFalse(elsewhere.exists())
        self.assertTrue((self.root / ".git" / "ci-cache" / "stage").is_dir())

    def test_the_whole_tree_marker_is_unchanged(self):
        tree = git(self.root, "rev-parse", "HEAD^{tree}")
        passed = self.root / ".git/ci-cache/passed"
        passed.mkdir(parents=True)
        (passed / f"{tree}-site0-desktop0").write_text("on 2026-10-14 12:00")
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn("this exact tree and check set passed on 2026-10-14", run.stdout)


@unittest.skipUnless(LOCAL.is_file() and shutil.which("bash"), "local CI isn't in this checkout")
class CloneModulesTests(unittest.TestCase):
    """The dashboard and perf jobs share dashboard/node_modules, and a cached dashboard stage clones nothing."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        base = Path(self.folder.name)
        self.cache, self.tree = base / "cache", base / "tree"
        (self.cache / "node_modules/vite").mkdir(parents=True)
        (self.cache / "node_modules/vite/index.js").write_text("1")
        (self.tree / "dashboard").mkdir(parents=True)
        source = LOCAL.read_text()
        self.function = re.search(r"^clone_modules\(\) \{.*?^\}\n", source, re.S | re.M).group(0)

    def clone(self):
        return subprocess.Popen(["bash", "-c", f'{self.function}\ncd "$1" && clone_modules dashboard "$2"', "x",
                                 str(self.tree), str(self.cache)])

    def test_the_perf_job_clones_the_cached_modules_before_it_builds_the_canvas_page(self):
        perf = re.search(r"^perf\(\) \{.*?^\}\n", LOCAL.read_text(), re.S | re.M).group(0)
        self.assertIn('clone_modules dashboard "$NODE"', perf)

    def test_two_jobs_cloning_at_once_leave_one_complete_copy(self):
        jobs = [self.clone() for _ in range(4)]
        self.assertEqual([job.wait(timeout=bound(30)) for job in jobs], [0, 0, 0, 0])
        modules = self.tree / "dashboard/node_modules"
        self.assertEqual((modules / "vite/index.js").read_text(), "1")
        self.assertFalse((modules / "node_modules").exists())
        self.assertEqual(sorted(p.name for p in (self.tree / "dashboard").iterdir()), ["node_modules"])

    def test_a_clone_that_finds_the_modules_already_there_changes_nothing(self):
        self.assertEqual(self.clone().wait(timeout=bound(30)), 0)
        (self.tree / "dashboard/node_modules/marker").write_text("kept")
        self.assertEqual(self.clone().wait(timeout=bound(30)), 0)
        self.assertEqual((self.tree / "dashboard/node_modules/marker").read_text(), "kept")
        self.assertFalse((self.tree / "dashboard/node_modules/node_modules").exists())

    def test_a_cache_without_modules_fails_the_job(self):
        shutil.rmtree(self.cache / "node_modules")
        self.assertNotEqual(self.clone().wait(timeout=bound(30)), 0)


@unittest.skipUnless(LOCAL.is_file() and shutil.which("bash") and shutil.which("git"),
                     "local CI isn't in this checkout")
class DesktopCheckoutTests(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        base = Path(self.folder.name)
        self.repo, self.cache, self.xdg, self.record = base / "repo", base / "cache", base / "xdg", base / "record"
        self.repo.mkdir()
        self.cache.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "ci@example.com")
        self.git("config", "user.name", "ci")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, text=True,
                              stdout=subprocess.PIPE).stdout.strip()

    def commit(self, lock):
        (self.repo / "desktop" / "src-tauri").mkdir(parents=True, exist_ok=True)
        (self.repo / "desktop" / "src-tauri" / "Cargo.lock").write_text(lock)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", lock)
        return self.git("rev-parse", "HEAD")

    def run_stage(self, commit, rust="rustc 1.0", prepare=""):
        function = re.search(r"^desktop\(\) \{.*?^\}\n", LOCAL.read_text(), re.M | re.S).group(0)
        script = (f'set -euo pipefail\nROOT="{self.repo}" CACHE="{self.cache}" COMMIT={commit} RUST_V="{rust}"\n'
                  f'TREE=$(git -C "$ROOT" rev-parse {commit}^{{tree}})\n{prepare}\n'
                  'prune_cargo_target() { :; }\n'
                  f'desktop_build() {{ echo "$1 $(git -C "$1" rev-parse HEAD) $CARGO_TARGET_DIR" '
                  f'>> "{self.record}"; }}\n'
                  f'{function}\ndesktop\n')
        env = {**os.environ, "XDG_CACHE_HOME": str(self.xdg)}
        env.pop("MOBSTER_CI_DESKTOP_SRC", None)
        run = subprocess.run(["bash", "-c", script], env=env, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        self.assertEqual(run.returncode, 0, run.stdout)
        return self.record.read_text().splitlines()[-1].split(" ")

    def test_every_run_builds_in_the_same_checkout_outside_git_at_the_commit_it_checks(self):
        first, second = self.commit("one"), self.commit("two")
        a = self.run_stage(first)
        b = self.run_stage(second)
        self.assertEqual(a[0], b[0])
        self.assertEqual((a[1], b[1]), (first, second))
        self.assertNotIn(".git", Path(a[0]).parts)
        self.assertTrue(a[0].startswith(str(self.xdg)))
        self.assertFalse((self.cache / "desktop.lock").exists())

    def test_the_target_follows_the_lockfile_and_toolchain_and_nothing_else(self):
        one = self.commit("one")
        same_lock = self.commit("one")
        other = self.commit("two")
        targets = {
            "one": self.run_stage(one)[2],
            "same lock, new commit": self.run_stage(same_lock)[2],
            "other lock": self.run_stage(other)[2],
            "other toolchain": self.run_stage(one, rust="rustc 2.0")[2],
        }
        self.assertEqual(targets["one"], targets["same lock, new commit"])
        self.assertEqual(len(set(targets.values())), 3, targets)
        for path in targets.values():
            self.assertRegex(path, r"/cargo-target/[0-9a-f]{16}$")

    def test_a_lock_left_by_a_process_that_is_gone_is_taken_over(self):
        commit = self.commit("one")
        dead = subprocess.Popen(["true"])
        dead.wait()
        stale = self.cache / "desktop.lock"
        stale.mkdir()
        (stale / "pid").write_text(str(dead.pid))
        self.run_stage(commit)
        self.assertTrue(self.record.is_file())
        self.assertFalse(stale.exists())


@unittest.skipUnless(LOCAL.is_file() and shutil.which("bash"), "local CI isn't in this checkout")
class CargoPruneTests(unittest.TestCase):

    def prune(self, target):
        source = LOCAL.read_text()
        function = re.search(r"^prune_cargo_target\(\) \{.*?^\}\n", source, re.M | re.S).group(0)
        keep = re.search(r"^KEEP_BUILDS=\d+", source, re.M).group(0)
        subprocess.run(["bash", "-c", f'set -u\n{keep}\n{function}\nprune_cargo_target "{target}"\n'], check=True)

    def build(self, debug, number, age):
        digest = f"{number:016x}"
        for name in (f"mobster_desktop-{digest}", f"mobster_desktop-{digest}.d", f"libmobster_desktop-{digest}.rmeta"):
            path = debug / "deps" / name
            path.write_text("x" * 100)
            os.utime(path, (1_700_000_000 + age, 1_700_000_000 + age))
        incremental = debug / "incremental" / f"mobster_desktop-{digest}"
        incremental.mkdir(parents=True)
        (incremental / "s-1.lock").write_text("x" * 100)
        os.utime(incremental, (1_700_000_000 + age, 1_700_000_000 + age))

    def test_the_newest_three_builds_stay_in_deps_and_incremental_and_older_ones_go(self):
        with tempfile.TemporaryDirectory() as folder:
            debug = Path(folder) / "debug"
            (debug / "deps").mkdir(parents=True)
            for number in range(1, 8):
                self.build(debug, number, age=number * 10)
            (debug / "deps" / "libserde-0123456789abcdef.rlib").write_text("dependency")
            self.prune(folder)
            left = sorted({re.search(r"mobster_desktop-[0-9a-f]{16}", n).group(0) for n in os.listdir(debug / "deps")
                           if re.search(r"mobster_desktop-[0-9a-f]{16}", n)})
            self.assertEqual(left, [f"mobster_desktop-{n:016x}" for n in (5, 6, 7)])
            self.assertEqual(sorted(os.listdir(debug / "incremental")),
                             [f"mobster_desktop-{n:016x}" for n in (5, 6, 7)])
            self.assertTrue((debug / "deps" / "libserde-0123456789abcdef.rlib").exists())

    def test_a_target_with_fewer_builds_than_the_limit_or_none_loses_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            self.prune(folder)
            debug = Path(folder) / "debug"
            (debug / "deps").mkdir(parents=True)
            self.prune(folder)
            for number in (1, 2):
                self.build(debug, number, age=number)
            before = sorted(os.listdir(debug / "deps"))
            self.prune(folder)
            self.assertEqual(sorted(os.listdir(debug / "deps")), before)


@unittest.skipUnless(LOCAL.is_file(), "the app local-CI script is not part of this repository")
class NextCacheTests(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.cache = Path(self.folder.name) / "ci-cache"
        self.site = Path(self.folder.name) / "wt" / "site"
        self.site.mkdir(parents=True)
        source = LOCAL.read_text()
        self.function = re.search(r"^next_cache\(\) \{.*?^\}\n", source, re.M | re.S).group(0)

    def next_cache(self, what, lock="aaaa"):
        script = (f'{self.function}\nCACHE="{self.cache}" SITE_NODE="{self.cache}/node/{lock}" NODE_V=v1\n'
                  f'next_cache {what}\n')
        subprocess.run(["bash", "-c", script], cwd=self.site, check=True)

    def test_a_saved_cache_comes_back_in_a_new_worktree_and_a_missing_one_changes_nothing(self):
        self.next_cache("restore")
        self.assertFalse((self.site / ".next").exists())
        (self.site / ".next" / "cache" / "turbopack").mkdir(parents=True)
        (self.site / ".next" / "cache" / "turbopack" / "data").write_text("compiled")
        self.next_cache("save")
        shutil.rmtree(self.site / ".next")
        self.next_cache("restore")
        self.assertEqual((self.site / ".next" / "cache" / "turbopack" / "data").read_text(), "compiled")

    def test_a_run_that_built_nothing_saves_nothing(self):
        self.next_cache("save")
        self.assertFalse((self.cache / "next-cache").exists())

    def test_a_new_lockfile_starts_cold_and_only_the_two_newest_caches_stay(self):
        for age, lock in enumerate(("aaaa", "bbbb", "cccc")):
            (self.site / ".next" / "cache").mkdir(parents=True, exist_ok=True)
            (self.site / ".next" / "cache" / "x").write_text(lock)
            self.next_cache("save", lock)
            for path in (self.cache / "next-cache").iterdir():
                if path.name.startswith(lock):
                    os.utime(path, (1_700_000_000 + age, 1_700_000_000 + age))
        self.assertEqual(len(list((self.cache / "next-cache").iterdir())), 2)
        shutil.rmtree(self.site / ".next")
        self.next_cache("restore", "dddd")
        self.assertFalse((self.site / ".next").exists())

    def test_the_script_saves_the_cache_only_after_the_build_passed(self):
        site = re.search(r"^site\(\) \{.*?^\}\n", LOCAL.read_text(), re.M | re.S).group(0)
        self.assertRegex(site, r"npm run build && next_cache save")


@unittest.skipUnless(LOCAL.is_file(), "the app local-CI script is not part of this repository")
class DashboardJobTests(unittest.TestCase):

    def run_job(self, tests, build, budget=False):
        with tempfile.TemporaryDirectory() as folder:
            bin_dir, logs = Path(folder) / "bin", Path(folder) / "logs"
            bin_dir.mkdir()
            logs.mkdir()
            (Path(folder) / "dashboard").mkdir()
            tools = [("npx", tests), ("npm", build)]
            if budget:
                (Path(folder) / "scripts").mkdir()
                (Path(folder) / "scripts/perf_budget.py").write_text("")
                (Path(folder) / "venv/bin").mkdir(parents=True)
                tools.append(("venv/bin/python", 0))
            for name, code in tools:
                tool = (Path(folder) / name) if "/" in name else bin_dir / name
                tool.write_text(f"#!/bin/sh\necho {Path(name).name} \"$@\"\nexit {code}\n")
                tool.chmod(0o755)
            function = re.search(r"^dashboard\(\) \{.*?^\}\n", LOCAL.read_text(), re.M | re.S).group(0)
            script = (f'set -euo pipefail\nLOGS="{logs}" VITEST_WORKERS=2 NODE=n WT="{folder}" VENV="{folder}/venv"\n'
                      f'clone_modules() {{ :; }}\n'
                      f'scan_bundle() {{ echo scanned $1; }}\n'
                      f'no_webhook() {{ echo checked webhooks in $1; }}\n{function}\n( dashboard )\n')
            return subprocess.run(["bash", "-c", script], cwd=folder,
                                  env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_a_job_whose_tests_and_build_pass_scans_the_bundle(self):
        run = self.run_job(tests=0, build=0)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn("npx vitest run --maxWorkers=2", run.stdout)
        self.assertIn("npm run build", run.stdout)
        self.assertIn("scanned dist", run.stdout)
        self.assertIn("checked webhooks in dist", run.stdout)
        self.assertNotIn("bundle dist", run.stdout)

    def test_a_tree_with_a_perf_budget_checks_the_bundle_after_the_build(self):
        run = self.run_job(tests=0, build=0, budget=True)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn("perf_budget.py bundle dist", run.stdout)
        self.assertNotIn("perf_budget.py", self.run_job(tests=0, build=1, budget=True).stdout)

    def test_a_failing_build_or_failing_tests_fail_the_job_and_nothing_is_scanned(self):
        for tests, build in ((0, 1), (1, 0)):
            with self.subTest(tests=tests, build=build):
                run = self.run_job(tests=tests, build=build)
                self.assertNotEqual(run.returncode, 0, run.stdout)
                self.assertNotIn("scanned", run.stdout)
                self.assertNotIn("checked webhooks", run.stdout)


@unittest.skipUnless(LOCAL.is_file(), "the app local-CI script is not part of this repository")
class DashboardFastJobTests(unittest.TestCase):
    """--fast's narrowed dashboard job: the related tests and tsc, then the startup bundle budget on a vite build."""

    def run_job(self, tests=0, build=0, budget=0, has_budget=True):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("bin", "logs", "sel", "cache", "dashboard", "venv/bin"):
                (root / name).mkdir(parents=True)
            (root / "sel/dashboard.txt").write_text("src/a.test.ts\n")
            if has_budget:
                (root / "scripts").mkdir()
                (root / "scripts/perf_budget.py").write_text("")
            npx = (f'#!/bin/sh\necho npx "$@"\ncase "$1" in vitest) exit {tests};; vite) echo vite says hi; '
                   f'exit {build};; *) exit 0;; esac\n')
            for path, body in ((root / "bin/npx", npx),
                               (root / "venv/bin/python", f'#!/bin/sh\necho python "$@"\nexit {budget}\n')):
                path.write_text(body)
                path.chmod(0o755)
            function = re.search(r"^dashboard_fast\(\) \{.*?^\}\n", LOCAL.read_text(), re.M | re.S).group(0)
            script = (f'set -euo pipefail\nLOGS="{root}/logs" SEL="{root}/sel" CACHE="{root}/cache" VITEST_WORKERS=2 '
                      f'NODE=n WT="{root}" VENV="{root}/venv"\nclone_modules() {{ :; }}\n{function}\n'
                      f'( dashboard_fast )\n')
            return subprocess.run(["bash", "-c", script], cwd=folder,
                                  env={**os.environ, "PATH": f"{root}/bin:{os.environ['PATH']}"},
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_a_passing_job_checks_the_startup_bundle_budget_on_a_vite_build(self):
        run = self.run_job()
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertIn("npx vitest related", run.stdout)
        self.assertIn("perf_budget.py bundle dist", run.stdout)
        self.assertNotIn("vite says hi", run.stdout)  # a passing build's log stays in its file

    def test_a_bundle_over_its_budget_fails_the_job(self):
        run = self.run_job(budget=1)
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertIn("perf_budget.py bundle dist", run.stdout)

    def test_a_failed_build_fails_the_job_prints_its_log_and_skips_the_budget(self):
        run = self.run_job(build=1)
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertIn("-- vite build", run.stdout)
        self.assertIn("vite says hi", run.stdout)
        self.assertNotIn("perf_budget.py", run.stdout)

    def test_failing_tests_still_fail_the_job_and_the_budget_is_still_reported(self):
        run = self.run_job(tests=1)
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertIn("perf_budget.py bundle dist", run.stdout)

    def test_a_tree_without_a_perf_budget_builds_nothing(self):
        run = self.run_job(has_budget=False)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertNotIn("perf_budget.py", run.stdout)
        self.assertNotIn("vite", run.stdout.replace("vitest", ""))


if __name__ == "__main__":
    unittest.main()
