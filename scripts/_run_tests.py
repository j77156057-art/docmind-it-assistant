"""Run the targeted retrieval/embedding test slice in a subprocess.

Why a subprocess: this project has no ``conftest.py`` and no package layout, so
``tests/*.py`` only import ``backend`` / ``admin_app`` / ``app`` / ``assistant``
when the **repo root** is on ``sys.path``. Running ``python -m pytest`` from the
repo root puts cwd on ``sys.path[0]``; calling ``pytest.main()`` in-process does
not (pytest prepends the test file's dir instead). We therefore spawn
``python -m pytest`` with ``cwd=REPO`` and also export ``PYTHONPATH=REPO``.

Output is written to ASCII-safe files because the Windows console code page
mangles UTF-8 Chinese in the runner's own stdout.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "Scripts" / "python.exe"
if not PY.exists():  # fall back to the interpreter running this script
    PY = Path(sys.executable)

SELECTOR = "embedding or provider or golden or retrieval or rerank or parent"
OUT = REPO / "tmp_test_ascii.txt"

env = dict(os.environ)
env["PYTHONPATH"] = str(REPO)
env["PYTHONIOENCODING"] = "utf-8"
# The WorkBuddy sandbox injects proxy vars that break outbound HTTPS.
for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    env.pop(var, None)

proc = subprocess.run(
    [str(PY), "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "-k", SELECTOR, "tests"],
    cwd=str(REPO), env=env, capture_output=True,
)
text = (proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace")
OUT.write_text(text + f"\nPYTEST_RC={proc.returncode}\n", encoding="utf-8")
print(f"rc={proc.returncode} -> {OUT}")
