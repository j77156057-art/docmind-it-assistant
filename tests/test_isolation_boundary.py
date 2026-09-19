import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (ROOT / "app.py", ROOT / "assistant", ROOT / "backend")
FORBIDDEN_IMPORTS = {
    "agent", "tools", "orchestrator", "workbench_fs", "projects", "regions",
    "game_workbench", "engine_adapters", "subprocess",
}
FORBIDDEN_CALLS = {"os.system", "os.popen", "subprocess.run", "subprocess.Popen"}


def source_files():
    for root in SOURCE_ROOTS:
        if root.is_file():
            yield root
        elif root.is_dir():
            yield from root.rglob("*.py")


class IsolationBoundaryTests(unittest.TestCase):
    def test_query_application_has_no_development_or_process_execution_imports(self):
        violations = []
        for path in source_files():
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
        for path in source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                owner = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
                name = f"{owner}.{node.func.attr}" if owner else node.func.attr
                if name in FORBIDDEN_CALLS:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} calls {name}")
        self.assertEqual(violations, [], "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
