"""Durable, path-safe storage for the original imported document versions."""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import tempfile


class DocumentSourceError(ValueError):
    pass


class DocumentSourceStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = Path(filename or "document").name
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
        return name[:180] or "document"

    def store(self, source: str | Path, *, document_id: int, version: int,
              filename: str) -> Path:
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise DocumentSourceError("原文件不存在")
        target_dir = self.root / str(int(document_id))
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"v{int(version)}-{self._safe_filename(filename)}"
        with tempfile.NamedTemporaryFile(dir=target_dir, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            shutil.copyfile(source_path, temporary)
            temporary.replace(target)
            for obsolete in target_dir.glob(f"v{int(version)}-*"):
                if obsolete != target:
                    obsolete.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def resolve(self, document_id: int, version: int) -> Path | None:
        target_dir = (self.root / str(int(document_id))).resolve()
        if target_dir.parent != self.root or not target_dir.is_dir():
            return None
        matches = sorted(target_dir.glob(f"v{int(version)}-*"))
        for candidate in matches:
            resolved = candidate.resolve()
            if resolved.parent == target_dir and resolved.is_file():
                return resolved
        return None

    def describe(self, document_id: int, version: int) -> dict:
        source = self.resolve(document_id, version)
        if source is None:
            return {"source_available": False, "source_filename": None, "source_bytes": None}
        prefix = f"v{int(version)}-"
        filename = source.name[len(prefix):] if source.name.startswith(prefix) else source.name
        return {
            "source_available": True,
            "source_filename": filename,
            "source_bytes": source.stat().st_size,
        }
