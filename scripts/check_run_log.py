"""Fail a build when a test run log shows signs of a process-level crash.

Why this exists
---------------
A full pytest run was observed emitting ``Windows fatal exception: access violation`` (stack in
SQLite DDL) while still reporting ``223 passed`` and exiting 0. A green gate is therefore not
proof that the suite ran cleanly: the interpreter can die mid-run and pytest's summary can still
look healthy.

The root cause investigation was exhausted (the concurrency hypothesis was disproved, the thread
candidate count was zero, and 6 of 7 ``TestClient`` candidates were false positives) and the
crash happened on the SQLite path, which production does not use. So this script does **not**
attempt to diagnose the crash -- it only makes "crashed but still green" impossible to miss.

Companion guard
---------------
``scripts/check_suite_completeness.py`` catches tests that are never collected at all
("should have run, did not"). This script catches tests that ran but crashed
("ran, died, still reported green").

Usage
-----
    python -B scripts/check_run_log.py <logfile>

In CI the log is produced with ``tee`` under ``set -o pipefail`` so that pytest's own exit code
is preserved *and* a log file is available to scan:

    set -o pipefail
    python -B -m pytest -q -rs -p no:cacheprovider tests 2>&1 | tee pytest-output.log
    python -B scripts/check_run_log.py pytest-output.log

Exit codes
----------
    0  no crash markers found
    1  at least one crash marker found, or the log file is missing/unreadable/empty
"""
from __future__ import annotations

import sys
from pathlib import Path

# Substrings that indicate the interpreter or a native extension died. Matched
# case-insensitively, because Windows and POSIX report these differently and pytest passes the
# platform text through verbatim.
CRASH_MARKERS: tuple[str, ...] = (
    "fatal exception",
    "access violation",
    "segmentation fault",
    "fatal python error",
    "aborted (core dumped)",
)

# Cap the report so a log full of repeated crashes does not flood the build output.
MAX_REPORTED_HITS: int = 10


def scan(lines: list[str]) -> list[tuple[int, str, str]]:
    """Find crash markers in already-read log lines.

    Args:
        lines: The log's lines, without trailing newlines.

    Returns:
        A list of ``(line_number, marker, line_content)`` tuples, ``line_number`` being 1-based.
    """
    hits: list[tuple[int, str, str]] = []
    for number, line in enumerate(lines, start=1):
        lowered = line.lower()
        for marker in CRASH_MARKERS:
            if marker in lowered:
                hits.append((number, marker, line.rstrip()))
                break  # Report each line once even if several markers match it.
    return hits


def main(argv: list[str]) -> int:
    """Entry point.

    Args:
        argv: Command-line arguments excluding the program name.

    Returns:
        0 when no crash markers are found, 1 otherwise.
    """
    if len(argv) != 1:
        print(f"usage: {Path(argv[0] if argv else 'check_run_log.py').name} <logfile>", file=sys.stderr)
        print("error: exactly one log file argument is required", file=sys.stderr)
        return 1

    log_path = Path(argv[0])
    # A missing or empty log must fail loudly: silently passing on "no log" would recreate the
    # exact failure mode this guard exists to prevent.
    if not log_path.is_file():
        print(f"FAIL: log file does not exist: {log_path}", file=sys.stderr)
        print("A run that produced no log at all cannot be trusted as clean.", file=sys.stderr)
        return 1

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"FAIL: cannot read log file {log_path}: {exc}", file=sys.stderr)
        return 1

    if not text.strip():
        print(f"FAIL: log file is empty: {log_path}", file=sys.stderr)
        print("A run that produced no output at all cannot be trusted as clean.", file=sys.stderr)
        return 1

    hits = scan(text.splitlines())
    if hits:
        shown = hits[:MAX_REPORTED_HITS]
        print(
            f"FAIL: {len(hits)} crash marker(s) found in {log_path} "
            f"(showing {len(shown)}):",
            file=sys.stderr,
        )
        for number, marker, content in shown:
            print(f"  line {number} [{marker}]: {content}", file=sys.stderr)
        if len(hits) > len(shown):
            print(f"  ... {len(hits) - len(shown)} more", file=sys.stderr)
        print(
            "\nThe test process crashed. A green pytest summary alongside a crash marker is not "
            "a pass -- investigate before merging.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: no crash markers found in {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
