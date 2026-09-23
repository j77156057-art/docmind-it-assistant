"""Resolve the golden set's repository roots and document paths.

The golden set (``golden/retrieval_golden_set.json``) spans two repositories and used to store
absolute ``D:/WorkBuddy/...`` document paths. That made the CI run measure an *empty* corpus: every
document was "missing", every metric came out 0.000, and because the gate mode is ``warn`` the job
still finished green — a gate that could not fail and a number that meant nothing.

This module makes the corpus portable and makes absence explicit. Each repo entry carries a logical
``root`` key and its documents are relative to it. A root is taken from the first of:

1. ``--repo-root <key>=<path>`` on the command line,
2. the environment variable ``IT_GOLDEN_ROOT_<KEY>`` (KEY uppercased, non-alphanumerics → ``_``),
3. a directory named after the key sitting next to this repository — on the development machine
   that is ``D:/WorkBuddy/<key>``.

The first two are *explicit* and therefore authoritative: if they name a path that is not a
directory, the repo is reported as not found rather than falling back — silently measuring a
different directory than the caller asked for would be worse than skipping it.

A repo whose root cannot be found is **skipped, never scored as zero**: the caller reports the skip
with the paths it tried. Absolute paths in the golden set are still honoured, so an out-of-tree
corpus keeps working without edits.
"""
from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def root_key(repo: dict) -> str:
    """The logical root name for a repo entry (falls back to the repo name)."""
    return str(repo.get("root") or repo["name"])


def env_var_for(key: str) -> str:
    """``docmind-it-assistant`` -> ``IT_GOLDEN_ROOT_DOCMIND_IT_ASSISTANT``."""
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in key)
    return f"IT_GOLDEN_ROOT_{cleaned.upper()}"


def parse_repo_root_args(argv: list) -> dict:
    """Collect repeated ``--repo-root KEY=PATH`` flags; also accepts ``--repo-root=KEY=PATH``."""
    overrides: dict = {}
    args = list(argv)
    for index, arg in enumerate(args):
        value = None
        if arg.startswith("--repo-root="):
            value = arg.split("=", 1)[1]
        elif arg == "--repo-root" and index + 1 < len(args):
            value = args[index + 1]
        if value is None:
            continue
        if "=" not in value:
            raise ValueError(f"--repo-root expects KEY=PATH, got {value!r}")
        key, path = value.split("=", 1)
        overrides[key.strip()] = Path(path.strip())
    return overrides


def resolve_repo_root(repo: dict, overrides: dict | None = None,
                      environ: dict | None = None) -> tuple:
    """Return ``(path_or_None, how)``.

    An explicit source — a ``--repo-root`` flag or ``IT_GOLDEN_ROOT_<KEY>`` — is **authoritative**:
    if it does not point at a directory the repo is reported as not found, with no fallback to the
    sibling default. Falling back would silently measure a *different* corpus than the caller asked
    for, which is worse than skipping: a skip is visible, a substituted corpus looks like a real
    score.

    ``how`` describes the source when found, or what was looked at when not — the caller prints it
    verbatim, because "skipped" is only useful if it says where it looked.
    """
    overrides = overrides or {}
    environ = os.environ if environ is None else environ
    key = root_key(repo)

    explicit = overrides.get(key)
    if explicit is not None:
        return (explicit, f"--repo-root {key}") if explicit.is_dir() else (
            None, f"--repo-root {key}={explicit} is not a directory")

    var = env_var_for(key)
    raw = str(environ.get(var, "")).strip()
    if raw:
        return (Path(raw), var) if Path(raw).is_dir() else (
            None, f"{var}={raw} is not a directory")

    sibling = HERE.parent.parent / key
    if sibling.is_dir():
        return sibling, "sibling of this repository"
    return None, (f"{sibling} (sibling of this repository); "
                  f"no --repo-root {key}= and no {var} were given")


def document_path(root: Path | None, doc: dict) -> Path:
    """Absolute paths in the golden set are honoured; relative ones resolve against ``root``."""
    path = Path(str(doc["path"]))
    if path.is_absolute():
        return path
    if root is None:
        raise ValueError(f"document {doc.get('source_key')!r} needs a repo root")
    return root / path
