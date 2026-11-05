import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci_backend.py"


@unittest.skipUnless(SCRIPT.is_file(), "scripts/ci_backend.py isn't in this checkout")
class TimeScaleTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("ci_backend", SCRIPT)
        self.ci = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ci)

    def test_a_module_that_starts_on_a_busy_mac_gets_the_slack_and_one_on_an_idle_mac_does_not(self):
        ci = {"MOBSTER_CI_BUSY_SCALE": "2.5"}
        self.assertIsNone(self.ci.time_scale(ci, load=4.0, cpus=18))
        self.assertEqual(self.ci.time_scale(ci, load=33.0, cpus=18), "2.5")

    def test_a_scale_you_set_is_left_alone_and_nothing_changes_outside_local_ci(self):
        mine = {"MOBSTER_TEST_TIME_SCALE": "3", "MOBSTER_CI_BUSY_SCALE": "2.5"}
        self.assertIsNone(self.ci.time_scale(mine, load=33.0, cpus=18))
        self.assertIsNone(self.ci.time_scale({}, load=33.0, cpus=18))


@unittest.skipUnless(SCRIPT.is_file(), "scripts/ci_backend.py isn't in this checkout")
class TimeoutTests(unittest.TestCase):
    def test_a_module_that_exits_as_its_limit_expires_is_reported_not_raised(self):
        spec = importlib.util.spec_from_file_location("ci_backend", SCRIPT)
        ci = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ci)
        with tempfile.TemporaryDirectory() as folder:
            python = Path(folder, "slow-python")
            python.write_text("#!/bin/sh\nsleep 2\n")
            python.chmod(0o700)
            gone = ProcessLookupError()
            with mock.patch.object(ci.os, "kill", side_effect=gone), mock.patch.object(ci.os, "killpg",
                                                                                       side_effect=gone):
                result = ci.run(str(python), Path(folder), "mobile_agent.tests.test_x", {}, Path(folder, "scratch"),
                                limit=0.2)
        self.assertFalse(result["ok"])
        self.assertIn("STOPPED: still running after", result["output"])


@unittest.skipUnless(SCRIPT.is_file(), "scripts/ci_backend.py isn't in this checkout")
class ExitCodeTests(unittest.TestCase):
    def test_a_module_that_dies_before_its_summary_is_reported_with_its_return_code(self):
        flaky = ("import os, signal, unittest\nfrom pathlib import Path\n"
                 "class T(unittest.TestCase):\n"
                 "    def test_it(self):\n"
                 "        seen = Path(__file__).with_suffix('.seen')\n"
                 "        if not seen.exists():\n"
                 "            seen.touch()\n"
                 "            os.kill(os.getpid(), signal.SIGTERM)\n")
        files = {"mobile_agent/__init__.py": "", "mobile_agent/tests/__init__.py": "",
                 "mobile_agent/tests/test_exits.py": "import os\nos._exit(23)\n",
                 "mobile_agent/tests/test_flaky.py": flaky}
        ci = load_ci_backend()
        with tempfile.TemporaryDirectory() as folder:
            for path, text in files.items():
                Path(folder, path).parent.mkdir(parents=True, exist_ok=True)
                Path(folder, path).write_text(text)
            Path(folder, "modules.txt").write_text("mobile_agent.tests.test_exits\nmobile_agent.tests.test_flaky\n")
            argv = ["ci_backend.py", "--root", folder, "--python", sys.executable, "--jobs", "2",
                    "--modules", str(Path(folder, "modules.txt"))]
            out = io.StringIO()
            with mock.patch.object(ci, "RESULTS", None), mock.patch.object(ci, "TIMINGS",
                                                                           Path(folder, "timings.json")), \
                 mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
                code = ci.main()
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("FLAKY mobile_agent.tests.test_flaky (returncode -15)", out.getvalue())
        self.assertRegex(out.getvalue(),
                         r"FAIL mobile_agent\.tests\.test_exits \([\d.]+s, returncode 23, and again alone "
                         r"with returncode 23\)")


def load_ci_backend():
    spec = importlib.util.spec_from_file_location("ci_backend", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LOGGING_TEST = (
    "import os, unittest\n{imports}"
    "class T(unittest.TestCase):\n"
    "    def test_it(self):\n"
    "        open(os.environ['RUNLOG_FOR_TEST'], 'a').write(__name__ + '\\n')\n"
    "        self.assertTrue({passes})\n"
)


@unittest.skipUnless(SCRIPT.with_name("ci_select.py").is_file(), "scripts/ci_select.py isn't in this checkout")
class ResultCacheTests(unittest.TestCase):

    SAFE = "mobile_agent.tests.test_permissions"

    def setUp(self):
        self.ci = load_ci_backend()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name, "repo")
        self.cache = Path(folder.name, "cache")
        self.runlog = Path(folder.name, "runs.log")
        self.write({
            "mobile_agent/__init__.py": "",
            "mobile_agent/core.py": "VALUE = 1\n",
            "mobile_agent/tests/__init__.py": "",
            "mobile_agent/tests/test_core.py": LOGGING_TEST.format(imports="from mobile_agent import core\n",
                                                                   passes="True"),
            "mobile_agent/tests/test_other.py": LOGGING_TEST.format(imports="", passes="True"),
            "mobile_agent/tests/test_permissions.py": LOGGING_TEST.format(imports="", passes="True"),
            "mobile_agent/requirements.txt": "",
        })
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.git("add", ".")
        patches = [mock.patch.object(self.ci, "wait_for_room", lambda: None),
                   mock.patch.object(self.ci, "RESULTS", self.cache / "backend-results"),
                   mock.patch.object(self.ci, "TIMINGS", self.cache / "timings.json"),
                   mock.patch.dict(os.environ, {"RUNLOG_FOR_TEST": str(self.runlog)})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def write(self, files):
        for path, text in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def run_ci(self, *args):
        argv = ["ci_backend.py", "--root", str(self.root), "--python", sys.executable, "--jobs", "2", *args]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            code = self.ci.main()
        return code, out.getvalue()

    def ran(self):
        try:
            names = self.runlog.read_text().split()
        except OSError:
            names = []
        self.runlog.unlink(missing_ok=True)
        return sorted(names)

    def stored(self):
        return sorted((self.cache / "backend-results").glob("*"))

    def test_a_cached_pass_skips_the_module_and_counts_its_tests(self):
        self.assertEqual(self.run_ci()[0], 0)
        self.assertEqual(len(self.ran()), 3)
        code, out = self.run_ci()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ran(), [self.SAFE])
        self.assertIn("cached mobile_agent.tests.test_core, mobile_agent.tests.test_other", out)
        self.assertIn("backend: 3 tests in 3 modules", out)

    def test_a_change_to_any_file_in_the_reach_reruns_the_module_and_only_that_one(self):
        self.run_ci()
        self.ran()
        self.write({"mobile_agent/core.py": "VALUE = 2\n"})
        _, out = self.run_ci()
        self.assertEqual(self.ran(), ["mobile_agent.tests.test_core", self.SAFE])
        self.assertIn("cached mobile_agent.tests.test_other ", out)

    def test_a_file_a_module_names_is_in_its_reach(self):
        self.write({"data/rows.txt": "a\n",
                    "mobile_agent/tests/test_other.py": LOGGING_TEST.format(imports="", passes="True")
                    + "ROWS = open('data/rows.txt').read\n"})
        self.git("add", ".")
        self.run_ci()
        self.ran()
        self.write({"data/rows.txt": "b\n"})
        self.run_ci()
        self.assertEqual(self.ran(), ["mobile_agent.tests.test_other", self.SAFE])

    def test_a_file_no_test_names_changes_every_key(self):
        self.write({"data/orphan.bin": "a"})
        self.git("add", ".")
        self.run_ci()
        self.ran()
        self.write({"data/orphan.bin": "b"})
        self.run_ci()
        self.assertEqual(len(self.ran()), 3)

    def test_a_reach_that_cannot_be_known_is_never_cached(self):
        self.write({"mobile_agent/tests/test_other.py": LOGGING_TEST.format(
            imports="import importlib\nimportlib.import_module(os.environ['RUNLOG_FOR_TEST'][:0] or 'os')\n",
            passes="True")})
        self.git("add", ".")
        self.run_ci()
        self.ran()
        _, out = self.run_ci()
        self.assertEqual(self.ran(), ["mobile_agent.tests.test_other", self.SAFE])
        self.assertIn("cached mobile_agent.tests.test_core ", out)
        self.assertNotIn("test_other,", out.split("cached", 1)[1].split("(")[0])

    def test_a_module_that_does_not_parse_is_never_cached(self):
        self.write({"mobile_agent/core.py": "def broken(:\n",
                    "mobile_agent/tests/test_core.py": LOGGING_TEST.format(
                        imports="try:\n    from mobile_agent import core\nexcept SyntaxError:\n    pass\n",
                        passes="True")})
        self.run_ci()
        self.ran()
        self.run_ci()
        self.assertIn("mobile_agent.tests.test_core", self.ran())

    def test_the_safety_core_always_runs_for_real_and_is_never_stored(self):
        self.run_ci()
        for _ in range(2):
            self.ran()
            self.run_ci()
            self.assertIn(self.SAFE, self.ran())
        self.assertEqual(len(self.stored()), 2)

    def test_a_failure_is_not_stored_and_runs_again(self):
        self.write({"mobile_agent/tests/test_other.py": LOGGING_TEST.format(imports="", passes="False")})
        code, _ = self.run_ci()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.stored()), 1)
        self.ran()
        code, out = self.run_ci()
        self.assertEqual(code, 1, out)
        self.assertIn("mobile_agent.tests.test_other", self.ran())

    def test_no_result_cache_runs_everything_and_a_bare_run_never_uses_the_cache(self):
        self.run_ci()
        self.ran()
        code, out = self.run_ci("--no-result-cache")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.ran()), 3)
        self.assertNotIn("cached", out)
        self.assertEqual(len(self.stored()), 2)
        with mock.patch.object(self.ci, "RESULTS", None):
            self.run_ci()
        self.assertEqual(len(self.ran()), 3)

    def test_the_python_version_and_the_requirements_are_part_of_the_key(self):
        self.run_ci()
        self.ran()
        self.write({"mobile_agent/requirements.txt": "pinned==1\n"})
        self.run_ci()
        self.assertEqual(len(self.ran()), 3)

    def test_the_time_scale_is_not_part_of_the_key_and_other_knobs_are(self):
        self.run_ci()
        self.ran()
        with mock.patch.dict(os.environ, {"MOBSTER_TEST_TIME_SCALE": "4"}):
            self.run_ci()
        self.assertEqual(self.ran(), [self.SAFE])
        with mock.patch.dict(os.environ, {"MOBSTER_SOME_SWITCH": "1"}):
            self.run_ci()
        self.assertEqual(len(self.ran()), 3)


class LocalCacheTests(unittest.TestCase):
    def test_full_checks_do_not_reuse_a_partial_or_legacy_tree_cache(self):
        script = SCRIPT.with_name("ci-local.sh")
        if not script.is_file():
            self.skipTest("local CI isn't in this checkout")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def git(*args):
                return subprocess.check_output(["git", "-C", folder, *args], text=True, stderr=subprocess.PIPE).strip()
            git("init", "-q")
            git("remote", "add", "origin", "https://example.invalid/mobster.git")
            (root / "mobile_agent").mkdir()
            (root / "mobile_agent/requirements.txt").write_text("")
            (root / "site").mkdir()
            (root / "site/package-lock.json").write_text("{}")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=fixture", "commit", "-qm", "fixture")
            tree = git("rev-parse", "HEAD^{tree}")
            cache = root / ".git/ci-cache/passed"
            cache.mkdir(parents=True)
            (cache / tree).write_text("legacy partial pass")
            (cache / f"{tree}-site0-desktop0").write_text("partial pass")
            python = root / "stop-before-dependencies"
            python.write_text("#!/bin/sh\necho BUILD_REQUIRED\nexit 73\n")
            python.chmod(0o700)
            env = {**os.environ, "MOBSTER_CI_PYTHON": str(python), "TMPDIR": folder}
            def run(*args):
                return subprocess.run(["bash", str(script), *args], cwd=root, env=env,
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
            partial = run()
            self.assertEqual(partial.returncode, 0, partial.stdout)
            full = run("--full")
            self.assertNotEqual(full.returncode, 0, full.stdout)
            self.assertIn("BUILD_REQUIRED", full.stdout)
            (cache / f"{tree}-site1-desktop1").write_text("full pass")
            cached = run("--full")
            self.assertEqual(cached.returncode, 0, cached.stdout)
            self.assertIn("full pass", cached.stdout)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(SCRIPT.is_file(), "scripts/ci_backend.py isn't in this checkout")
class CountSuitesTest(unittest.TestCase):

    def setUp(self):
        spec = importlib.util.spec_from_file_location("ci_backend", SCRIPT)
        self.ci = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ci)

    def test_counts_other_python_suites_only(self):
        table = "\n".join([
            "   1     0 /sbin/launchd",
            " 100     1 /bin/zsh -c until [ $(pgrep -f ci_backend.py | wc -l) -lt 2 ]; do sleep 20; done",
            " 200     1 make test-backend",
            " 201   200 /bin/sh -c nice -n 10 mobile_agent/.venv/bin/python scripts/ci_backend.py --jobs 4",
            " 202   201 mobile_agent/.venv/bin/python scripts/ci_backend.py --jobs 4",
            " 300     1 /bin/sh -c nice -n 10 /usr/bin/python3 scripts/ci_backend.py",
            " 301   300 /usr/bin/python3 scripts/ci_backend.py",
            " 400     1 /opt/venv/bin/python3.12 /tmp/fast/scripts/ci_backend.py --modules m.txt --jobs 4",
            " 401     1 /opt/venv/bin/python3.12 /tmp/full/scripts/ci_backend.py --jobs 4",
            " 500   202 mobile_agent/.venv/bin/python -c import unittest",
        ])
        self.assertEqual(self.ci.count_suites(table, 202), 2)
        self.assertEqual(self.ci.count_suites(table, 999), 3)

    def test_unreadable_lines_are_skipped(self):
        self.assertEqual(self.ci.count_suites("garbage\n\n  x y z", 1), 0)


@unittest.skipUnless(SCRIPT.is_file(), "scripts/ci_backend.py isn't in this checkout")
class StaleVenvTests(unittest.TestCase):
    def test_a_package_at_another_version_than_the_lock_is_named_and_a_matching_one_is_not(self):
        spec = importlib.util.spec_from_file_location("ci_backend", SCRIPT)
        ci = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ci)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "mobile_agent").mkdir()
            current = subprocess.run([sys.executable, "-c", "import importlib.metadata as m;print(m.version('pip'))"],
                                     capture_output=True, text=True).stdout.strip()
            (root / "mobile_agent" / "requirements.txt").write_text("pip==0.0.1\n# a comment\nnot-installed-xyz==1.0\n")
            self.assertEqual(ci.behind_requirements(sys.executable, root), [f"pip {current} (pinned 0.0.1)"])
            (root / "mobile_agent" / "requirements.txt").write_text(f"pip=={current}\n")
            self.assertEqual(ci.behind_requirements(sys.executable, root), [])
            (root / "mobile_agent" / "requirements.txt").unlink()
            self.assertEqual(ci.behind_requirements(sys.executable, root), [])
