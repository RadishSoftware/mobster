import importlib.util
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_select", ROOT / "scripts" / "ci_select.py")
ci_select = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_select)

RULES = """
mobile_agent      backend lint perf
docs              backend design
dashboard         backend dashboard design
other             backend
CHANGELOG.md      backend
scripts           backend lint perf
site              site lint
"""

FILES = {
    "mobile_agent/__init__.py": "",
    "mobile_agent/core.py": "VALUE = 1\n",
    "mobile_agent/middle.py": "from .core import VALUE\n",
    "mobile_agent/lonely.py": "X = 2\n",
    "mobile_agent/labels.py": "KEYS = {'docs': 1, 'scripts': 2}\n",
    "mobile_agent/uses_labels.py": "from mobile_agent import labels\n",
    "mobile_agent/tests/__init__.py": "",
    "mobile_agent/tests/test_middle.py": "from mobile_agent.middle import VALUE\n",
    "mobile_agent/tests/test_patch.py": "from unittest import mock\nP = mock.patch('mobile_agent.lonely.X', 3)\n",
    "mobile_agent/tests/test_docs.py": "from pathlib import Path\nROOT = Path(__file__).parents[2]\nDOC = ROOT / "
    "'docs' / 'guide.md'\n",
    "mobile_agent/tests/test_changelog.py": "TEXT = open('CHANGELOG.md').read\n",
    "mobile_agent/tests/test_labels.py": "import mobile_agent.uses_labels\n",
    "mobile_agent/tests/test_permissions.py": "",
    "mobile_agent/tests/test_plan_gate.py": "",
    "docs/guide.md": "guide\n",
    "docs/other.md": "other\n",
    "other/data.txt": "nobody names me\n",
    "CHANGELOG.md": "# Changelog\n",
    "scripts/ci-local.sh": "#!/bin/sh\n",
    "dashboard/package.json": "{}\n",
    "dashboard/DESIGN.md": "design\n",
    "dashboard/src/widget.ts": "export const x = 1\n",
    "dashboard/src/widget.test.ts": "import { x } from './widget'\n",
    "dashboard/src/guard.test.ts": "import { readFileSync, readdirSync } from 'node:fs'\n",
    "dashboard/src/design-doc.test.ts": "import { readFileSync } from 'node:fs'\nreadFileSync('../DESIGN.md')\n",
    "site/app/page.tsx": "export default 1\n",
}


class SelectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        for path, text in FILES.items():
            (cls.root / path).parent.mkdir(parents=True, exist_ok=True)
            (cls.root / path).write_text(textwrap.dedent(text))
        subprocess.run(["git", "init", "-q", str(cls.root)], check=True)
        subprocess.run(["git", "-C", str(cls.root), "add", "-A"], check=True)
        cls.rules = ci_select.ci_inputs.Rules(RULES)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def plan(self, *changed):
        plan, why = ci_select.select(self.root, changed, rules=self.rules)
        return plan, why

    def backend(self, *changed):
        plan, _ = self.plan(*changed)
        if plan["backend"] == "ALL":
            return "ALL"
        return {name.rsplit(".", 1)[-1] for name in plan["backend"]}


    def test_a_python_change_runs_the_tests_whose_imports_reach_it_and_the_safety_core(self):
        self.assertEqual(self.backend("mobile_agent/core.py"), {"test_middle", "test_permissions", "test_plan_gate"})

    def test_a_module_named_in_a_string_counts_as_imported(self):
        self.assertIn("test_patch", self.backend("mobile_agent/lonely.py"))

    def test_a_file_a_test_names_by_path_or_by_name_runs_that_test(self):
        self.assertEqual(self.backend("docs/guide.md") - {"test_permissions", "test_plan_gate"}, {"test_docs"})
        self.assertEqual(self.backend("CHANGELOG.md") - {"test_permissions", "test_plan_gate"}, {"test_changelog"})

    def test_a_bare_word_in_a_runtime_module_is_not_a_folder(self):
        self.assertNotIn("test_labels", self.backend("docs/guide.md"))

    def test_a_deleted_file_nothing_names_runs_nothing_extra(self):
        self.assertEqual(self.backend("other/gone.txt"), {"test_permissions", "test_plan_gate"})

    def test_the_dashboard_runs_vitest_related_plus_the_tests_that_read_files(self):
        plan, _ = self.plan("dashboard/src/widget.ts")
        self.assertEqual(plan["dashboard"], {"src/widget.ts", "src/guard.test.ts"})
        plan, _ = self.plan("dashboard/DESIGN.md")
        self.assertEqual(plan["dashboard"], {"src/guard.test.ts", "src/design-doc.test.ts"})

    def test_lint_and_design_see_only_the_changed_files(self):
        plan, _ = self.plan("mobile_agent/core.py", "docs/guide.md", "site/app/page.tsx")
        self.assertEqual(plan["ruff"], ["mobile_agent/core.py"])
        self.assertEqual(plan["design"], {"docs/guide.md"})
        self.assertEqual(plan["eslint"], ["app/page.tsx"])

    def test_a_change_the_dashboard_does_not_read_selects_no_dashboard(self):
        plan, _ = self.plan("site/app/page.tsx")
        self.assertEqual(plan["dashboard"], set())


    def test_a_file_no_test_names_runs_the_whole_backend(self):
        plan, why = self.plan("other/data.txt")
        self.assertEqual(plan["backend"], "ALL")
        self.assertIn("other/data.txt", why["backend"])

    def test_a_module_no_test_imports_runs_the_whole_backend(self):
        (self.root / "mobile_agent" / "orphan.py").write_text("Y = 1\n")
        subprocess.run(["git", "-C", str(self.root), "add", "mobile_agent/orphan.py"], check=True)
        try:
            self.assertEqual(self.backend("mobile_agent/orphan.py"), "ALL")
        finally:
            subprocess.run(["git", "-C", str(self.root), "rm", "-q", "--cached", "mobile_agent/orphan.py"], check=True)
            (self.root / "mobile_agent" / "orphan.py").unlink()

    def test_a_change_to_the_gate_runs_every_stage_in_full(self):
        plan, _ = self.plan("scripts/ci-local.sh")
        self.assertEqual((plan["backend"], plan["dashboard"], plan["design"]), ("ALL", "ALL", "ALL"))

    def test_a_dashboard_config_runs_the_dashboard_in_full(self):
        plan, why = self.plan("dashboard/package.json")
        self.assertEqual(plan["dashboard"], "ALL")
        self.assertIn("dashboard/package.json", why["dashboard"])

    def test_a_file_that_does_not_parse_runs_the_whole_backend(self):
        (self.root / "mobile_agent" / "broken.py").write_text("def (\n")
        subprocess.run(["git", "-C", str(self.root), "add", "mobile_agent/broken.py"], check=True)
        try:
            self.assertEqual(self.backend("mobile_agent/broken.py"), "ALL")
        finally:
            subprocess.run(["git", "-C", str(self.root), "rm", "-q", "--cached", "mobile_agent/broken.py"], check=True)
            (self.root / "mobile_agent" / "broken.py").unlink()


    def test_the_safety_core_and_the_public_cut_tests_exist(self):
        for name in ci_select.SAFETY_CORE + ci_select.PUBLIC_CUT:
            path = ROOT / (name.replace(".", "/") + ".py")
            self.assertTrue(path.is_file(), f"{name} is in ci_select.py but {path} is missing")


class ComputedImportTest(unittest.TestCase):
    def scan(self, source, name="mobile_agent.tests.test_x"):
        module = ci_select.Module(name, "mobile_agent/tests/test_x.py", False)
        ci_select.scan(module, textwrap.dedent(source))
        return module

    def test_a_computed_import_is_dynamic_but_a_constant_or_a_helper_called_with_constants_is_not(self):
        self.assertTrue(self.scan("import importlib\nimportlib.import_module(os.environ['X'])\n").dynamic)
        self.assertFalse(self.scan("import importlib\nimportlib.import_module('mobile_agent.core')\n").dynamic)
        helper = self.scan('''
            import importlib
            def load(name):
                return importlib.import_module("mobile_agent." + name)
            A = load("core")
            ''')
        self.assertFalse(helper.dynamic)
        self.assertIn("mobile_agent.core", helper.imports)
        self.assertTrue(self.scan("def load(name):\n    return __import__('mobile_agent.' + name)\nload(x)\n").dynamic)


if __name__ == "__main__":
    unittest.main()
