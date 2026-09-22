"""Guard against test files that no runner ever executes.

Why this exists
---------------
The suite mixes ``unittest.TestCase`` modules with pytest-style modules. Under the historical
runner (``unittest discover``) only the former are collected, so 24 pytest-style tests -- field
encryption (including the production guard that requires ``IT_QUERY_FIELD_KEY``), rate limiting
and reranking -- were silently never executed locally or in CI. That gap was found by a manual,
one-off reconciliation; nothing structural prevented it from reappearing.

This script turns that reconciliation into an assertion: every file matching ``tests/test_*.py``
must contribute at least one collected test under the runner CI actually uses. A file that
contributes zero tests fails the build with exit code 1 and names the file.

Design constraints
------------------
* Standalone: no ``conftest.py`` is added and none is required. pytest is driven as a subprocess
  from the repository root, exactly like CI does.
* Read-only: it only collects (``--collect-only``); it never runs the suite.
* Offline: collection needs no network and no PostgreSQL, so PG-only modules still contribute
  their tests (they are collected even when they would skip at runtime).

Usage
-----
    python -B scripts/check_suite_completeness.py

Exit codes
----------
    0  every test file contributes at least one test
    1  at least one test file contributes zero tests (orphan), or collection itself failed
"""
from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

# Repository root is the parent of ``scripts/``.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
TESTS_DIR: Path = REPO_ROOT / "tests"

# Relative (posix) path prefix that pytest prints for every node id it collects from ``tests``.
TESTS_PREFIX: str = "tests/"

# Same invocation CI uses, plus ``--collect-only`` so nothing is executed.
COLLECT_ARGS: tuple[str, ...] = (
    "-B",
    "-m",
    "pytest",
    "--collect-only",
    "-q",
    "-p",
    "no:cacheprovider",
    "tests",
)


def discover_test_files(tests_dir: Path) -> list[str]:
    """Return every top-level ``tests/test_*.py`` file as a posix path relative to the repo root.

    Args:
        tests_dir: Absolute path of the ``tests`` directory.

    Returns:
        Sorted list of relative posix paths, e.g. ``["tests/test_app.py", ...]``.
    """
    return sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in tests_dir.glob("test_*.py")
        if path.is_file()
    )


def collect_counts() -> tuple[Counter, int, str]:
    """Run pytest collection and count collected tests per module.

    Returns:
        A tuple ``(counts, returncode, output)`` where ``counts`` maps a module's relative posix
        path to the number of tests pytest collected from it, ``returncode`` is pytest's exit
        status and ``output`` is the raw combined stdout/stderr text.

    Raises:
        FileNotFoundError: If ``sys.executable`` cannot be spawned (never expected in practice).
    """
    completed = subprocess.run(
        [sys.executable, *COLLECT_ARGS],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = completed.stdout.decode("utf-8", errors="replace")
    counts: Counter = Counter()
    for raw_line in output.splitlines():
        line = raw_line.strip().replace("\\", "/")
        # A collection line is a pytest node id: "<module>::<test>" or
        # "<module>::<Class>::<test>", optionally suffixed with "[param]".
        if not line.startswith(TESTS_PREFIX) or "::" not in line:
            continue
        module = line.split("::", 1)[0]
        counts[module] += 1
    return counts, completed.returncode, output


def render_table(files: list[str], counts: Counter) -> str:
    """Render the per-module collected-test table.

    Args:
        files: Test files discovered on disk, relative posix paths.
        counts: Collected test count per module.

    Returns:
        A multi-line string ready to print.
    """
    width = max((len(name) for name in files), default=20)
    width = max(width, len("TOTAL"))
    lines: list[str] = []
    lines.append("Test suite completeness (runner: pytest --collect-only)")
    lines.append("")
    lines.append(f"{'module'.ljust(width)}  collected")
    lines.append(f"{'-' * width}  --------")
    for name in files:
        lines.append(f"{name.ljust(width)}  {counts.get(name, 0):9d}")
    lines.append(f"{'-' * width}  --------")
    lines.append(f"{'TOTAL'.ljust(width)}  {sum(counts.get(name, 0) for name in files):9d}")
    return "\n".join(lines)


def main() -> int:
    """Entry point.

    Returns:
        0 when every test file contributes at least one collected test, 1 otherwise.
    """
    if not TESTS_DIR.is_dir():
        print(f"FAIL: tests directory not found: {TESTS_DIR}", file=sys.stderr)
        return 1

    files = discover_test_files(TESTS_DIR)
    if not files:
        print(f"FAIL: no tests/test_*.py found under {TESTS_DIR}", file=sys.stderr)
        return 1

    counts, returncode, output = collect_counts()
    print(render_table(files, counts))
    print()

    if returncode != 0:
        print(f"FAIL: pytest collection exited with {returncode}.", file=sys.stderr)
        print(output, file=sys.stderr)
        return 1

    orphans = [name for name in files if counts.get(name, 0) == 0]
    # Anything pytest collected from a file we did not discover is worth surfacing too: it means
    # the discovery rule (tests/test_*.py) and the runner disagree.
    unknown = sorted(set(counts) - set(files))

    if unknown:
        print("WARNING: collected from paths outside tests/test_*.py:", file=sys.stderr)
        for name in unknown:
            print(f"  - {name} ({counts[name]} tests)", file=sys.stderr)
        print(file=sys.stderr)

    if orphans:
        print(
            f"FAIL: {len(orphans)} test file(s) contribute 0 collected tests. "
            "They are invisible to the test runner:",
            file=sys.stderr,
        )
        for name in orphans:
            print(f"  - {name}", file=sys.stderr)
        print(
            "\nA file matching tests/test_*.py that the runner cannot collect is how tests "
            "silently stop being executed. Fix collection, or move the file out of tests/.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {len(files)} test file(s), every one contributes at least 1 collected test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
