"""Isolation boundary guards.

Two independent rules are enforced:

1. **Import-closure rule (the important one).** Walking the import graph from the query entry
   point must never reach an LLM orchestration or tracing framework. This is stronger than a
   name blacklist because it constrains what the query process actually loads, no matter which
   file or helper module performs the import.
2. **Name rule.** No module on the query side may import the development-agent packages or
   execute processes, and no worker module may reach back into the query entry points.
"""
import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
QUERY_ENTRYPOINTS = (ROOT / "app.py",)
QUERY_SOURCES = (ROOT / "app.py", ROOT / "assistant", ROOT / "backend")
WORKER_SOURCES = (ROOT / "ingestion", ROOT / "worker")
# Packages that may not be loaded by the query process, directly or transitively.
FORBIDDEN_ORCHESTRATION = (
    "langgraph", "langgraph_sdk", "langgraph_checkpoint", "langchain", "langchain_core",
    "langchain_protocol", "langsmith",
)
FORBIDDEN_IMPORTS = {
    "agent", "tools", "orchestrator", "workbench_fs", "projects", "regions",
    "game_workbench", "engine_adapters", "subprocess",
}
FORBIDDEN_CALLS = {"os.system", "os.popen", "subprocess.run", "subprocess.Popen"}
# Only the query side is checked for these strings: the worker is allowed to reference the
# framework (it imports it) and must stay the only place that does.
PROJECT_ROOTS = {"app", "admin_app", "assistant", "backend", "ingestion", "worker"}


def source_files(roots=QUERY_SOURCES):
    for root in roots:
        if root.is_file():
            yield root
        elif root.is_dir():
            yield from sorted(root.rglob("*.py"))


def _module_file(name: str) -> Path | None:
    parts = [part for part in name.split(".") if part]
    if not parts:
        return None
    package = ROOT.joinpath(*parts)
    if (package / "__init__.py").is_file():
        return package / "__init__.py"
    candidate = ROOT.joinpath(*parts).with_suffix(".py")
    return candidate if candidate.is_file() else None


def _imported_names(tree: ast.AST, path: Path) -> list[str]:
    """Resolve intra-project module names, including relative imports."""
    relative = path.relative_to(ROOT)
    if path.name == "__init__.py":
        package_parts = list(relative.parent.parts)
    else:
        package_parts = list(relative.with_suffix("").parent.parts)
    package_parts = [part for part in package_parts if part not in {"."}]

    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                trimmed = package_parts[:len(package_parts) - (node.level - 1)] if node.level > 1 \
                    else package_parts
                base = ".".join([*trimmed, base]) if base else ".".join(trimmed)
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names if base)
    return [name for name in names if name and name.split(".", 1)[0] in PROJECT_ROOTS]


def import_closure(entrypoints=QUERY_ENTRYPOINTS):
    """Return (visited files, third-party top-level imports seen in the closure)."""
    seen: set[Path] = set()
    third_party: set[str] = set()
    queue = [path for path in entrypoints if path.is_file()]
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    third_party.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom) and not node.level:
                if node.module:
                    third_party.add(node.module.split(".", 1)[0])
        for name in _imported_names(tree, path):
            target = _module_file(name)
            if target is not None:
                queue.append(target)
    return seen, third_party


class IsolationBoundaryTests(unittest.TestCase):
    def test_query_application_has_no_development_or_process_execution_imports(self):
        violations = []
        for path in (*source_files(QUERY_SOURCES), *source_files(WORKER_SOURCES)):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".", 1)[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".", 1)[0]]
                else:
                    continue
                blocked = sorted(set(names) & FORBIDDEN_IMPORTS)
                if blocked:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {blocked}")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_query_application_has_no_direct_process_execution_calls(self):
        violations = []
        for path in (*source_files(QUERY_SOURCES), *source_files(WORKER_SOURCES)):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                owner = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
                name = f"{owner}.{node.func.attr}" if owner else node.func.attr
                if name in FORBIDDEN_CALLS:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} calls {name}")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_query_import_closure_excludes_orchestration_frameworks(self):
        """The query process must not be able to load LangGraph or LangSmith at all."""
        visited, third_party = import_closure()
        blocking = sorted(
            name for name in third_party
            if name in FORBIDDEN_ORCHESTRATION
        )
        self.assertEqual(
            blocking, [],
            f"query import closure reaches {blocking} via {sorted(p.name for p in visited)}",
        )
        # Sanity check that the walk really followed the package graph.
        visited_names = {path.name for path in visited}
        self.assertIn("database.py", visited_names)
        self.assertIn("auth.py", visited_names)
        self.assertNotIn("graph.py", visited_names)

    def test_query_sources_never_dynamically_import_the_framework(self):
        """Guards against imports that AST analysis of `import` statements cannot see.

        Naming the engine in configuration is fine; pulling the package in at runtime is not.
        """
        violations = []
        pattern = re.compile(
            r"(?:import_module|__import__)\s*\(\s*['\"](langgraph|langchain|langsmith)"
            r"|sys\.modules\s*\[\s*['\"](langgraph|langchain|langsmith)",
        )
        for path in source_files(QUERY_SOURCES):
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                line = text[:match.start()].count("\n") + 1
                violations.append(f"{path.relative_to(ROOT)}:{line} -> {match.group(0)}")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_worker_does_not_import_query_entrypoints(self):
        violations = []
        for path in source_files(WORKER_SOURCES):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name.split(".", 1)[0] in {"app", "admin_app", "assistant"}:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} imports {name}"
                        )
        self.assertEqual(violations, [], "\n".join(violations))

    def test_langsmith_tracing_is_never_enabled_implicitly(self):
        """Tracing must stay an explicit operator decision, never a side effect of our code."""
        offenders = []
        pattern = re.compile(
            r"LANGSMITH_TRACING|LANGCHAIN_TRACING|LANGSMITH_API_KEY|LANGCHAIN_API_KEY",
        )
        for path in (*source_files(QUERY_SOURCES), *source_files(WORKER_SOURCES)):
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                line = text[:match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line} sets {match.group(0)}")
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
