"""Central model allocation policy for the query application."""
from __future__ import annotations

import os


class ModelRouter:
    def __init__(self, mode: str | None = None):
        self.mode = (mode or os.getenv("IT_MODEL_MODE") or "knowledge").strip().lower()

    def select(self, question: str, evidence: str) -> dict:
        if evidence == "sufficient":
            return {"route": "knowledge", "provider": "builtin", "model": "deterministic"}
        if self.mode == "cloud":
            return {
                "route": "cloud", "provider": os.getenv("IT_CLOUD_PROVIDER", "openai-compatible"),
                "model": os.getenv("IT_CLOUD_MODEL", "configured-by-backend"),
            }
        if self.mode == "local":
            return {
                "route": "local", "provider": os.getenv("IT_LOCAL_PROVIDER", "ollama"),
                "model": os.getenv("IT_LOCAL_MODEL", "configured-by-backend"),
            }
        return {"route": "knowledge", "provider": "builtin", "model": "deterministic"}

    def status(self) -> dict:
        return {"mode": self.mode, "cloud_configured": bool(os.getenv("IT_CLOUD_MODEL")),
                "local_configured": bool(os.getenv("IT_LOCAL_MODEL"))}

