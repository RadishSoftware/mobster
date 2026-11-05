#!/usr/bin/env python3
"""Chooses what the fast pre-merge gate (scripts/ci-local.sh --fast) runs for a set of changed files.

    ci_select.py --root CHECKOUT --out DIR < changed-paths

The changed paths come one per line, relative to the repository root (git diff --name-only). It writes, in DIR:

    backend.txt     the test modules to run, one per line, or ALL
    dashboard.txt   the files `vitest related` starts from, relative to dashboard/, or ALL, or nothing
    ruff.txt        the Python files ruff checks
    design.txt      the files the design checks scan, or ALL
    eslint.txt      the site files ESLint checks

and prints one "fast: ..." line per stage saying why. A stage it cannot narrow runs in full: a path no test module
names (by import, or by a string that is its path), a dashboard file outside src/, a config file. The backend's choice:

- A test module runs when its import closure reaches a changed Python file. The graph is the AST of every module
  under mobile_agent/: import statements, relative ones resolved, and strings that name a module ("mobile_agent.x",
  as mock.patch and -m take them). Importing a.b.c imports a and a.b too.
- A test module runs when it, or a module in its closure, holds a string that is the changed path: from the
  repository root ("docs/cli.md", ROOT / "docs" / "cli.md"), from a folder above the module (Path(__file__).parent /
  "fixtures"), its file name ("CHANGELOG.md") or a glob that matches it ("*.mdx"). For a changed Python file only the
  test side counts (mobile_agent/tests/), since runtime modules name their own package all the time.
- The safety core always runs (SAFETY_CORE), and the public cut's tests run when a changed path ships in the public
  tree (scripts/public-paths.txt).

Only the standard library, so a macOS system python3 runs it.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_inputs  # noqa: E402

PACKAGE = "mobile_agent"
TESTS = (PACKAGE, "tests")
# Safety code fails closed, so its tests run on every change: approvals and permissions, the journal, the effect
# ledger's unknown-outcome rule, secret masking, the plan gate, the task policy and the replay guard.
SAFETY_CORE = (
    "mobile_agent.tests.test_permissions",
    "mobile_agent.tests.test_permission_review",
    "mobile_agent.tests.test_approvals",
    "mobile_agent.tests.test_double_approval",
    "mobile_agent.tests.test_seam_journal",
    "mobile_agent.tests.test_unknown_outcome_recheck",
    "mobile_agent.tests.test_seam_safety",
    "mobile_agent.tests.test_plan_gate",
    "mobile_agent.tests.test_task_policy",
    "mobile_agent.tests.test_secret_filter",
    "mobile_agent.tests.test_tape",  # the replay guard: a regression hid for a week when it ran only for tape paths
)
PUBLIC_CUT = ("mobile_agent.tests.test_public_cut", "mobile_agent.tests.test_public_publish")
PUBLIC_TOOLS = ("scripts/public-paths.txt", "scripts/cut-public-tree.sh", "scripts/check_public_tree.py",
                "scripts/publish-public-tree.sh", "scripts/public-tree", ".gitattributes")
# dashboard/ paths `vitest related` can start from. The files that set up every test (the configs, the lockfile, the
# test setup) run the dashboard in full, and so does a path outside dashboard/ that it reads (docs/brand, the catalog).
DASHBOARD_SOURCES = ("dashboard/src/", "dashboard/dev-bridge/")
DASHBOARD_SETUP = ("dashboard/package.json", "dashboard/package-lock.json", "dashboard/tsconfig.json",
                   "dashboard/vite.config.ts", "dashboard/vitest.config.ts", "dashboard/index.html",
                   "dashboard/build-revision.ts", "dashboard/src/test-setup.ts")
# A dashboard test that reads files itself is outside vitest's module graph. One that walks folders (readdirSync,
# import.meta.glob, a ** pattern) runs on every dashboard change; one that reads named files runs when it names a
# changed file (its file name, or its name without the extension).
FS_READ = re.compile(r"readFileSync|readdirSync|import\.meta\.glob|globSync|from ['\"](?:node:)?fs(?:/promises)?['\"]")
FS_WALK = re.compile(r"readdirSync|readdir\(|import\.meta\.glob|globSync|\*\*|opendir")
# Strings that are patterns or templates, not paths: regular expressions, format strings.
NOT_A_PATH = re.compile(r"[\[\]\\%{}$^()|+<>]")
WILD = re.compile(r"[*?.]*")
RUFF_ROOTS = ("mobile_agent/", "scripts/", "desktop/scripts/")
# Changes to how the gate itself runs: every stage runs in full.
GATE = ("scripts/ci-local.sh", "scripts/ci_select.py", "scripts/ci_backend.py", "scripts/ci_inputs.py",
        "scripts/ci-inputs.txt", "scripts/ci-sandbox.sb")


def segments(path):
    return tuple(part for part in path.strip("/").split("/") if part and part != ".")


# -- the Python graph -------------------------------------------------------------------------------------------------

class Module:
    __slots__ = ("name", "path", "is_package", "imports", "literals", "parsed", "dynamic")

    def __init__(self, name, path, is_package):
        self.name, self.path, self.is_package = name, path, is_package
        self.imports, self.literals, self.parsed, self.dynamic = set(), set(), True, False


def _joined(node):
    """The constant string parts of ROOT / "a" / "b", os.path.join(x, "a", "b") and x.joinpath("a", "b"), as one
    path per run of constants ("a/b")."""
    parts = []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        stack, flat = [node], []
        while stack:
            current = stack.pop()
            if isinstance(current, ast.BinOp) and isinstance(current.op, ast.Div):
                stack += [current.right, current.left]
            else:
                flat.append(current)
        parts = flat
    elif isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in ("join", "joinpath", "Path", "PurePath"):
            parts = list(node.args)
    runs, current = [], []
    for part in parts:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            current.append(part.value)
        else:
            if len(current) > 1:
                runs.append("/".join(current))
            current = []
    if len(current) > 1:
        runs.append("/".join(current))
    return runs


# Calls that load code by a name or path. The module's constants and f-strings that start with a package name say what
# they load; any other argument is computed at run time, and the import graph cannot see what it reaches.
LOADERS = {"import_module", "__import__", "run_module", "run_path", "spec_from_file_location", "load_module"}


def _loader_name(node):
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name if name in LOADERS else None


def _loads(node, constants, package):
    """(names the call loads, or None when the argument is computed). ``a.*`` names every module under a."""
    if not _loader_name(node):
        return []
    names = []
    for arg in node.args:
        if isinstance(arg, ast.Name) and arg.id in constants:
            names.append(constants[arg.id])
            continue
        if isinstance(arg, ast.Constant):
            continue
        if isinstance(arg, ast.JoinedStr) and arg.values:
            first = arg.values[0]
            if isinstance(first, ast.Constant) and first.value.startswith("mobile_agent."):
                names.append(first.value.rsplit(".", 1)[0] + ".*" if first.value.endswith(".") else None)
                continue
            if (isinstance(first, ast.FormattedValue) and isinstance(first.value, ast.Name)
                    and first.value.id == "__package__"):
                names.append(package + ".*")
                continue
        return None
    return names


def _literal_ok(text):
    return 0 < len(text) <= 300 and "\n" not in text and " " not in text.strip() and text.strip("./") != ""


def scan(module, source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        module.parsed = False
        return
    package = module.name if module.is_package else module.name.rpartition(".")[0]
    constants = {}  # NAME = "text" and NAME = __package__ + ".text" at the top of the module
    for node in tree.body:
        value = node.value if isinstance(node, ast.Assign) else None
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add) and isinstance(value.right, ast.Constant) \
                and isinstance(value.left, ast.Name) and value.left.id == "__package__":
            value = ast.Constant(package + value.right.value) if isinstance(value.right.value, str) else None
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            constants.update({t.id: value.value for t in node.targets if isinstance(t, ast.Name)})
    # def helper(name): import_module("prefix." + name): the helper's callers name what it loads.
    helpers, handled = {}, set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = {a.arg for a in fn.args.args}
            for call in ast.walk(fn):
                arg = call.args[0] if isinstance(call, ast.Call) and call.args else None
                if (isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add) and isinstance(arg.left, ast.Constant)
                        and isinstance(arg.left.value, str) and isinstance(arg.right, ast.Name)
                        and arg.right.id in params and _loader_name(call)):
                    helpers[fn.name] = arg.left.value
                    handled.add(id(call))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module.imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            if prefix:
                module.imports.add(prefix)
                module.imports.update(f"{prefix}.{alias.name}" for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _literal_ok(node.value):
            module.literals.add(node.value.strip())
        if isinstance(node, (ast.BinOp, ast.Call)):
            module.literals.update(_joined(node))
        if isinstance(node, ast.Call) and id(node) not in handled:
            helper = node.func.id if isinstance(node.func, ast.Name) else None
            if helper in helpers:
                arg = node.args[0] if node.args else None
                loaded = ([helpers[helper] + arg.value] if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                          else None)
            else:
                loaded = _loads(node, constants, package)
            if loaded is None or None in loaded:
                module.dynamic = True
            else:
                module.imports.update(loaded)


class Graph:
    def __init__(self, root, cache=None):
        self.root = Path(root)
        self.modules = {}  # dotted name -> Module
        self.by_path = {}  # repository path -> Module
        # What each file imports and names, cached by blob id: parsing 900 files takes 25 s on a loaded Mac.
        memo = {}
        if cache:
            try:
                memo = json.loads(Path(cache).read_text())
            except (OSError, ValueError):
                memo = {}
        fresh = {}
        listed = subprocess.run(["git", "-C", str(self.root), "ls-files", "-s", "-z", "--", "*.py"],
                                check=True, stdout=subprocess.PIPE).stdout.decode("utf-8", "surrogateescape")
        packages = set()
        files = []
        for record in listed.split("\0"):
            if not record:
                continue
            head, path = record.split("\t", 1)
            oid = head.split()[1]
            files.append((path, oid))
            if path.endswith("/__init__.py"):
                packages.add(path[: -len("/__init__.py")])
        self.loose = {}  # stem -> [repository path]: Python files outside the package
        for path, oid in files:
            parts = path[:-3].split("/")
            if parts[0] != PACKAGE:
                self.loose.setdefault(parts[-1], []).append(path)
                continue
            # The package and the packages in it, as unittest discovery and the import system see them.
            if not all("/".join(parts[:i]) in packages for i in range(1, len(parts))):
                continue
            is_package = parts[-1] == "__init__"
            module = Module(".".join(parts[:-1] if is_package else parts), path, is_package)
            known = memo.get(oid)
            if known is None or len(known) < 4:
                full = self.root / path
                scan(module, full.read_text(encoding="utf-8", errors="replace") if full.exists() else "")
                known = [sorted(module.imports), sorted(module.literals), module.parsed, module.dynamic]
            else:
                module.imports, module.literals = set(known[0]), set(known[1])
                module.parsed, module.dynamic = known[2], known[3]
            fresh[oid] = known
            self.modules[module.name] = module
            self.by_path[path] = module
        if cache and fresh != memo:
            Path(cache).parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(f"{cache}.{os.getpid()}")
            tmp.write_text(json.dumps(fresh))
            tmp.replace(cache)
        self._deps = {name: self._resolve(module) for name, module in self.modules.items()}
        self._index()

    def _index(self):
        """The strings that may be paths, by their first segment, so a changed path is checked against a few."""
        self.by_first, self.by_second, self.globbed, self.by_name, self.name_globs = {}, {}, [], {}, []
        for module in self.modules.values():
            here = segments(module.path)[:-1]
            test_side = here[:2] == TESTS
            anchors = [here[:i] for i in range(len(here) + 1)]  # the root and every folder above the module
            for text in module.literals:
                literal = segments(text)
                if not literal or literal in ((".",), ("..",)) or NOT_A_PATH.search(text):
                    continue
                if any(WILD.fullmatch(part) for part in literal):
                    # "**/*.md" and "*/*.json" read files by their extension, wherever they are; "*" alone names nothing
                    if "." in literal[-1] and not WILD.fullmatch(literal[-1]):
                        self.name_globs.append((literal[-1], module.name))
                    continue
                if len(literal) == 1 and "." in literal[0]:
                    (self.name_globs if any(c in literal[0] for c in "*?[") else
                     self.by_name.setdefault(literal[0], [])).append((literal[0], module.name))
                for anchor in anchors:
                    # A runtime module's bare word ("docs", "scripts") is a key or a label far more often than the
                    # repository folder: the installed app has no repository. Its own folder's words still count.
                    if not anchor and len(literal) == 1 and "." not in literal[0] and not test_side:
                        continue
                    run = anchor + literal
                    if any(c in run[0] for c in "*?["):
                        self.globbed.append((run, module.name))
                    elif len(run) > 1 and not any(c in run[1] for c in "*?["):
                        self.by_second.setdefault(run[:2], []).append((run, module.name))
                    else:
                        self.by_first.setdefault(run[0], []).append((run, module.name))

    def naming(self, path):
        """The modules that name the repository path ``path`` (a tuple of segments): from the root or a folder above
        the module ("docs/cli.md", Path(__file__).parent / "fixtures"), by its file name ("CHANGELOG.md") or a glob
        for it ("*.mdx"), or by importing a script whose folder it put on sys.path (import ci_inputs)."""
        found = {name for _, name in self.by_name.get(path[-1], ())}
        found |= {name for pattern, name in self.name_globs if fnmatch.fnmatchcase(path[-1], pattern)}
        for run, name in self.by_first.get(path[0], []) + self.by_second.get(path[:2], []) + self.globbed:
            if len(run) <= len(path) and all(fnmatch.fnmatchcase(p, r) for p, r in zip(path, run)):
                found.add(name)
        if path[-1].endswith(".py") and path[0] != PACKAGE:
            stem = path[-1][:-3]
            found |= {m.name for m in self.modules.values() if any(i.split(".")[0] == stem for i in m.imports)}
        return found

    def _known(self, dotted):
        """The longest prefix of ``dotted`` that is a module here, or None."""
        parts = dotted.split(".")
        while parts:
            name = ".".join(parts)
            if name in self.modules:
                return name
            parts.pop()
        return None

    def _with_parents(self, name):
        found = {name}
        parts = name.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if parent in self.modules:
                found.add(parent)
        if self.modules[name].is_package and f"{name}.__main__" in self.modules:
            found.add(f"{name}.__main__")  # -m package runs its __main__
        return found

    def _resolve(self, module):
        deps = set()
        names = set(module.imports)
        names.update(text for text in module.literals if text == PACKAGE or text.startswith(PACKAGE + "."))
        for name in names:
            if name.endswith(".*"):  # a computed name under a package: any module in it
                deps |= {m for m in self.modules if m.startswith(name[:-1]) or m == name[:-2]}
                continue
            known = self._known(name) if name.split(".")[0] == PACKAGE else None
            if known:
                deps |= self._with_parents(known)
        return deps

    def closure(self, name):
        seen, stack = set(), [name]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self._deps.get(current, ()))
        return seen

    def tests(self):
        """The modules ci_backend.py runs: test*.py in packages (the walk above enters only packages)."""
        return sorted(name for name, module in self.modules.items() if Path(module.path).name.startswith("test"))

    def reach(self, rules):
        """What each test module reads, for a cache of its results: ({test: sorted repository paths, or None when
        the reach is uncertain}, the paths no test module names). The paths of a test are its closure's Python files
        and the files that a module in the closure names, as select() maps a changed file to the tests that run. A
        path nobody names is read by something the graph cannot see, so it counts for every test, as select() runs
        everything when one changes. None: a module in the closure does not parse or loads code by a computed name."""
        reachers = {}
        closures = {test: self.closure(test) for test in self.tests()}
        for test, members in closures.items():
            for member in members:
                reachers.setdefault(member, set()).add(test)
        files = {test: {self.modules[m].path for m in members} for test, members in closures.items()}
        shared = []
        listed = subprocess.run(["git", "-C", str(self.root), "ls-files", "-z"], check=True,
                                stdout=subprocess.PIPE).stdout.decode("utf-8", "surrogateescape")
        for path in listed.split("\0"):
            if not path or path in self.by_path or not (self.root / path).is_file():
                continue
            readers = rules.readers(segments(path))
            if readers is not None and ci_inputs.EVERY not in readers and "backend" not in readers:
                continue
            naming = {name for name in self.naming(segments(path))
                      if not path.endswith(".py") or self.modules[name].path.startswith("mobile_agent/tests/")}
            if not naming:
                shared.append(path)
            for name in naming:
                for test in reachers.get(name, ()):
                    files[test].add(path)
        uncertain = {test for test, members in closures.items()
                     if any(not self.modules[m].parsed or self.modules[m].dynamic for m in members)}
        return {test: None if test in uncertain else sorted(paths) for test, paths in files.items()}, sorted(shared)


# -- the choice -------------------------------------------------------------------------------------------------------

def select(root, changed, rules=None, cache=None):
    """{stage: decision} for the changed paths, and the reasons, one per stage."""
    root = Path(root)
    rules = rules or ci_inputs.Rules(ci_inputs.MANIFEST.read_text())
    changed = sorted({p.strip() for p in changed if p.strip()})
    plan = {"backend": set(), "dashboard": set(), "ruff": [], "design": set(), "eslint": []}
    why = {}

    def full(stage, reason):
        if plan[stage] != "ALL":
            plan[stage], why[stage] = "ALL", reason

    def reads(path, stage):
        readers = rules.readers(segments(path))
        return readers is None or ci_inputs.EVERY in readers or stage in readers

    gate = [p for p in changed if any(p == g or p.startswith(g + "/") for g in GATE)]
    if gate:
        for stage in ("backend", "dashboard", "design"):
            full(stage, f"{gate[0]} decides how CI runs")

    # -- backend
    graph = Graph(root, cache)
    tests = graph.tests()
    closures = {test: graph.closure(test) for test in tests}
    unmapped = []
    for path in changed:
        if plan["backend"] == "ALL" or not reads(path, "backend"):
            continue
        module = graph.by_path.get(path)
        if module is not None and not module.parsed:
            full("backend", f"{path} does not parse")
            break
        # The modules that name the path. For a Python file only the test side counts: runtime modules name their own
        # package all the time, and the import graph already follows them.
        segs = segments(path)
        naming = {name for name in graph.naming(segs)
                  if not path.endswith(".py") or graph.modules[name].path.startswith("mobile_agent/tests/")}
        if module is not None:
            naming.add(module.name)
        hits = {test for test, members in closures.items() if not naming.isdisjoint(members)}
        plan["backend"] |= hits
        # A file nothing names may still be read (a folder walked from a computed path): run everything. A deleted
        # file nothing names cannot be read by anything.
        if not hits and (root / path).exists():
            unmapped.append(path)
    if unmapped and plan["backend"] != "ALL":
        full("backend", f"no test module names {unmapped[0]}"
                        + (f" (and {len(unmapped) - 1} more)" if len(unmapped) > 1 else ""))
    if plan["backend"] != "ALL":
        selected = len(plan["backend"])
        plan["backend"] |= {name for name in SAFETY_CORE if name in graph.modules}
        public = public_paths(root)
        if any(ships(path, public) for path in changed) or any(
                p == t or p.startswith(t + "/") for p in changed for t in PUBLIC_TOOLS):
            plan["backend"] |= {name for name in PUBLIC_CUT if name in graph.modules}
        why["backend"] = (f"{selected} of {len(tests)} test modules reach a changed path, "
                          f"{len(plan['backend'])} with the safety core")

    # -- dashboard
    dash = [p for p in changed if reads(p, "dashboard")]
    if plan["dashboard"] != "ALL" and dash:
        setup = [p for p in dash if p in DASHBOARD_SETUP or not p.startswith("dashboard/")]
        if setup:
            full("dashboard", f"{setup[0]} sets up every dashboard test")
        else:
            # Sources start vitest related; another file under dashboard/ (DESIGN.md, qa/)
            # only the tests that read files.
            plan["dashboard"] = {p[len("dashboard/"):] for p in dash if p.startswith(DASHBOARD_SOURCES)
                                 and (root / p).exists()}
            readers = sorted(fs_tests(root, dash))
            plan["dashboard"] |= set(readers)
            why["dashboard"] = (f"vitest related from {len(dash)} changed files, plus {len(readers)} tests that read "
                                "files themselves; tsc incremental; the startup bundle budget")
    elif not dash:
        why["dashboard"] = "no changed path is read by the dashboard"

    # -- ruff, the design checks, ESLint
    plan["ruff"] = [p for p in changed if p.endswith(".py") and p.startswith(RUFF_ROOTS) and (root / p).exists()]
    design = [p for p in changed if reads(p, "design")]
    if plan["design"] != "ALL":
        if any(p.startswith("scripts/design-checks/") for p in design):
            full("design", "the design checks themselves changed")
        else:
            plan["design"] = {p for p in design if (root / p).exists()}
            why["design"] = f"{len(plan['design'])} changed files scanned"
    plan["eslint"] = [p[len("site/"):] for p in changed if p.startswith("site/") and (root / p).exists()
                      and p.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs"))]
    if any(p.startswith("site/") and Path(p).name.startswith(("eslint", "package")) for p in changed):
        plan["eslint"] = "ALL"
    return plan, why


def fs_tests(root, changed=()):
    """The dashboard tests outside vitest's module graph that may read one of the changed paths."""
    names = set()
    for path in changed:
        name = Path(path).name
        names.add(name)
        stem = name.split(".")[0]
        if len(stem) >= 4:
            names.add(stem)
    found = []
    for base in ("src", "dev-bridge"):
        for directory, dirs, files in os.walk(Path(root, "dashboard", base)):
            dirs[:] = [d for d in dirs if d != "node_modules"]
            for file in files:
                if re.search(r"\.test\.[cm]?[jt]sx?$", file):
                    full = Path(directory, file)
                    text = full.read_text(encoding="utf-8", errors="replace")
                    if FS_READ.search(text) and (FS_WALK.search(text) or any(name in text for name in names)):
                        found.append(full.relative_to(Path(root, "dashboard")).as_posix())
    return found


def public_paths(root):
    """(ships, path) lines of scripts/public-paths.txt."""
    lines = []
    try:
        text = Path(root, "scripts/public-paths.txt").read_text()
    except OSError:
        return lines
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            lines.append((not line.startswith("!"), segments(line.lstrip("!"))))
    return lines


def ships(path, lines):
    segs, best = segments(path), None
    for shipped, rule in lines:
        if segs[: len(rule)] == rule and (best is None or len(rule) > len(best[1])):
            best = (shipped, rule)
    return bool(best and best[0])


def write(out, plan):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for stage in ("backend", "dashboard", "ruff", "design", "eslint"):
        value = plan[stage]
        (out / f"{stage}.txt").write_text("ALL\n" if value == "ALL" else "".join(f"{v}\n" for v in sorted(value)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="the checkout under test")
    parser.add_argument("--out", type=Path, required=True, help="the folder the choice is written to")
    parser.add_argument("--cache", type=Path, help="a file to keep each Python file's imports and strings in")
    args = parser.parse_args(argv)
    plan, why = select(args.root, sys.stdin.read().splitlines(), cache=args.cache)
    write(args.out, plan)
    for stage in ("backend", "dashboard", "design"):
        value = plan[stage]
        if value == "ALL":
            print(f"fast: {stage} in full: {why[stage]}")
        elif stage in why:
            print(f"fast: {stage}: {why[stage]}")
    print(f"fast: ruff on {len(plan['ruff'])} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
