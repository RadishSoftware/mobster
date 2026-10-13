"""The engine imports nothing from the harness.

ENGINE is the open-source engine: the observe, decide, verify, act loop (frontier) and what it needs to run, the
WebDriverAgent driver, the model transport, and the safety core (journal, task_policy, effect_ledger, secret_filter,
system_dialogs, the permission policy and gate). Everything else in mobile_agent is Mobster for Mac's: the harness (the
terminal UI, the MCP server, conversations, workflows, Wi-Fi, several phones, setup) and the quality and speed modules
(the app catalog, skills, deep links, hedged calls, the frame clock). The engine reaches them only through harness_api's
registries and engine hooks, and runs without them. scripts/public-paths.txt ships ENGINE in the open-source tree;
test_public_cut.py checks it does.

Two proofs: every import statement in an ENGINE module, at module level or inside a function, names an ENGINE
module; and in a fresh interpreter where every other mobile_agent module is unavailable, every ENGINE module imports
and the open-source tree's reference example (examples/reference_loop.py) runs a task on a scripted phone and model.
Offline."""

import ast
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

PACKAGE = Path(__file__).resolve().parents[1]

ENGINE = (
    "__init__",
    # the loop and what it needs to run
    "frontier", "engines", "agent_hooks", "contract", "harness_api", "harness", "harness.checkpoints",
    "harness.control", "output_contract", "usage", "costs",
    # the driver and the model transport
    "drivers", "state", "extensions", "transport", "gemini",
    # the safety core
    "journal", "task_policy", "effect_ledger", "secret_filter", "system_dialogs", "permissions",
    "permissions.gate", "permissions.policy", "permissions.rules", "permissions.exceptions", "errors", "api_errors",
    "paths",
)


def module_file(name):
    path = PACKAGE.joinpath(*name.split("."))
    return path / "__init__.py" if path.is_dir() else path.with_suffix(".py")


def imported(name):
    """(line, module) for every mobile_agent module ``name`` imports, wherever the import statement is."""
    path = module_file(name)
    package = name.split(".") if path.name == "__init__.py" else name.split(".")[:-1]
    if name == "__init__":
        package = []
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = package[:len(package) - (node.level - 1)] + ([node.module] if node.module else [])
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "mobile_agent":
            base = node.module.split(".")[1:]
        elif isinstance(node, ast.Import):
            out += [(node.lineno, alias.name.split(".", 1)[1]) for alias in node.names
                    if alias.name.startswith("mobile_agent.")]
            continue
        else:
            continue
        for alias in node.names:
            candidate = ".".join(base + [alias.name])
            # `from .pkg import name`: a module when pkg/name.py exists, else a name inside pkg
            out.append((node.lineno, candidate if module_file(candidate).is_file() else ".".join(base) or "__init__"))
    return out


class StaticTests(unittest.TestCase):
    def test_every_engine_module_exists(self):
        for name in ENGINE:
            with self.subTest(module=name):
                self.assertTrue(module_file(name).is_file(), f"{name} is in ENGINE but not in mobile_agent")

    def test_every_import_in_the_engine_names_an_engine_module(self):
        allowed = set(ENGINE)
        for name in ENGINE:
            for line, target in imported(name):
                with self.subTest(module=name, line=line):
                    self.assertIn(target, allowed, f"{name}.py:{line} imports {target}, which is not in the engine. "
                                  "Reach it through a harness_api registry or engine hook instead.")


# The reference example: at the open-source tree's root, and in this repository in the overlay that puts it there.
EXAMPLE = next((path for path in (PACKAGE.parent / "examples" / "reference_loop.py",
                                  PACKAGE.parent / "scripts" / "public-tree" / "examples" / "reference_loop.py")
                if path.is_file()), None)

CHILD = textwrap.dedent('''
    import importlib, importlib.util, json, os, sys, tempfile
    from pathlib import Path

    ENGINE, EXAMPLE = set(json.loads(sys.argv[1])), sys.argv[2]
    blocked = []

    class Harness:
        """Every mobile_agent module outside the engine is unavailable, as in the open-source tree."""
        def find_spec(self, name, path=None, target=None):
            if name.startswith("mobile_agent.") and name[len("mobile_agent."):] not in ENGINE:
                blocked.append(name)
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return None

    sys.meta_path.insert(0, Harness())
    home = tempfile.mkdtemp()
    os.environ.update(HOME=home, ANTHROPIC_API_KEY="sk-ant-test-not-real", OPENAI_API_KEY="",
                      MOBSTER_SMART_MODEL="", MOBSTER_SMART_PROVIDER="", MOBSTER_FRAME_CLOCK="off")
    for name in sorted(ENGINE):
        importlib.import_module("mobile_agent" if name == "__init__" else "mobile_agent." + name)

    from mobile_agent import engines, frontier, harness_api, journal
    assert all(harness_api.hook(name) is None for name in harness_api.HOOKS), "a hook is set without the harness"
    assert engines.smart_model() == engines.ANTHROPIC_SMART_MODEL
    assert engines.smart_key() == "sk-ant-test-not-real"
    assert engines.model_provider("kimi:k2") == "openai" and not engines.on_plan("kimi:k2")
    assert frontier.cost_usd("claude-sonnet-5-5", {"prompt_tokens": 1000, "completion_tokens": 10}) > 0
    assert frontier.default_skills() == () and frontier._hedge() is None
    assert frontier.transient_model_error(RuntimeError("HTTP 529 overloaded"))
    assert not frontier.transient_model_error(RuntimeError("HTTP 400 invalid_request_error"))
    journal.disable_track("absent", "error: migration")

    # A task through the reference example, on a scripted phone and model.
    from mobile_agent.state import Element, Snapshot
    from mobile_agent.frontier import prompt_text

    about = Snapshot([Element("1", "About", "Button", (.4, .1, .2, .05))], "General", 400, 800, "synthetic_fixture",
                     bundle_id="com.apple.Preferences")

    class Driver:
        can_type = True
        def __init__(self):
            self.actions = []
        def observe(self, timeout=10):
            return about
        def execute(self, operation, target, snapshot, text=None, timeout=10):
            self.actions.append((operation, target.label))
        def close(self):
            pass

    class Model:
        model, provider = "claude-sonnet-5-5", "anthropic"
        def __init__(self):
            self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0}
            self.turns = [("TAP", "About"), ("DONE", None), ("DONE", None)]
        def complete(self, messages, schema, timeout=60, **kwargs):
            if "items" in schema["properties"]:
                return {"items": []}, {}
            op, label = self.turns.pop(0) if self.turns else ("DONE", None)
            rows = prompt_text(messages).split("Screen elements:\\n", 1)[-1].splitlines()
            target = next((r.split()[0] for r in rows if label and f'"{label}"' in r), None)
            return {"thought": "", "plan": "", "notes_add": [], "checklist_updates": [], "answer": "iOS 26.0",
                    "actions": [{"operation": op, "target": target, "target_label": None, "text": None,
                                 "app": None}]}, {}

    sys.dont_write_bytecode = True  # no __pycache__ beside the example: the overlay ships what is there
    spec = importlib.util.spec_from_file_location("reference_loop", EXAMPLE)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    driver = Driver()
    summary, events = example.run(driver, Model())
    assert driver.actions == [("TAP", "About")], (driver.actions, events[-5:])
    assert summary.get("answer"), summary
    assert any(event.get("event") == "frontier_decision" for event in events)
    assert example.decline({"title": "Send?"}) == "denied"
    harnessy = sorted({name for name in blocked if not name.startswith("mobile_agent.private")})
    print(json.dumps({"blocked": harnessy}))
''')


class IsolatedTests(unittest.TestCase):
    @unittest.skipUnless(EXAMPLE, "examples/reference_loop.py is not in this copy")
    def test_the_engine_imports_and_runs_with_the_harness_unavailable(self):
        import json
        done = subprocess.run([sys.executable, "-c", CHILD, json.dumps(list(ENGINE)), str(EXAMPLE)], cwd=PACKAGE.parent,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        report = json.loads(done.stdout.strip().splitlines()[-1])
        # What the engine asked for and did without: only the harness's hook module (harness_api.HOOKS_MODULE).
        self.assertEqual(report["blocked"], ["mobile_agent.engine_hooks"])


if __name__ == "__main__":
    unittest.main()
