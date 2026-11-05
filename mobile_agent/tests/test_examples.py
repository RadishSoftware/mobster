"""The examples run as written: the offline one end to end, and every example is in the README's table."""

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def load(name):
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExamplesTests(unittest.TestCase):
    def test_embed_runtime_finishes_on_the_demo_phone_and_exits_zero(self):
        module, out = load("embed_runtime"), io.StringIO()
        argv, sys.argv = sys.argv, ["embed_runtime.py"]
        try:
            with contextlib.redirect_stdout(out):
                code = module.main()
        finally:
            sys.argv = argv
        lines = out.getvalue().splitlines()
        self.assertEqual(code, 0)
        self.assertIn("  approval: tap “Send” → yes", lines)
        self.assertEqual(lines[-1], "Done: Sent “running 10 minutes late” to Alex Rivera.")

    def test_the_api_client_counts_a_completed_run_as_finished(self):
        module = load("api_client")
        line = module.describe({"event": "run_finished", "status": "completed",
                                "summary": {"answer": "Sent it.", "reason": None}}, {})
        self.assertEqual(line, "completed: Sent it.")

    def test_every_example_folder_and_script_is_in_the_readme(self):
        readme = (EXAMPLES / "README.md").read_text()
        for entry in sorted(EXAMPLES.iterdir()):
            if entry.name != "README.md" and not entry.name.startswith((".", "_")):
                with self.subTest(example=entry.name):
                    self.assertIn(entry.name, readme)


if __name__ == "__main__":
    unittest.main()
