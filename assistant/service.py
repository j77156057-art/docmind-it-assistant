"""Read-only IT knowledge query service."""
from __future__ import annotations

import re
from pathlib import Path

from backend import ModelRouter, QueryDatabase


class ITQueryService:
    def __init__(self, knowledge_path: str, database: QueryDatabase, models: ModelRouter):
        self.knowledge_path = Path(knowledge_path)
        self.database = database
        self.models = models

    @staticmethod
    def _terms(text: str) -> set[str]:
        value = (text or "").lower()
        latin = set(re.findall(r"[a-z0-9_-]{2,}", value))
        cjk = {value[i:i + 2] for i in range(max(0, len(value) - 1))
               if "\u4e00" <= value[i] <= "\u9fff" and "\u4e00" <= value[i + 1] <= "\u9fff"}
        return latin | cjk

    def _sections(self) -> list[dict]:
        try:
            text = self.knowledge_path.read_text(encoding="utf-8")
        except OSError:
            return []
        rows, title, body, line = [], "", [], 1
        for number, raw in enumerate(text.splitlines(), 1):
            if raw.startswith("## "):
                if title:
                    rows.append({"title": title, "text": "\n".join(body).strip(), "line": line})
                title, body, line = raw[3:].strip(), [], number
            elif title and raw.strip():
                body.append(raw.strip())
        if title:
            rows.append({"title": title, "text": "\n".join(body).strip(), "line": line})
        return rows

    def query(self, session_id: str, question: str) -> dict:
        question = (question or "").strip()
        if not question:
            raise ValueError("问题不能为空")
        wanted, ranked = self._terms(question), []
        for section in self._sections():
            title_score = len(wanted & self._terms(section["title"]))
            body_score = len(wanted & self._terms(section["text"]))
            if title_score or body_score >= 3:
                ranked.append((title_score * 3 + body_score, section))
        ranked.sort(key=lambda item: (-item[0], item[1]["line"]))
        hits = [item[1] for item in ranked[:2]]
        if hits:
            evidence = "sufficient"
            answer = "\n\n".join(item["text"] for item in hits)
            citations = [{"source": self.knowledge_path.name, "section": item["title"],
                          "line": item["line"]} for item in hits]
        else:
            evidence = "insufficient"
            answer = "现有资料不足以可靠回答。请补充报错内容、发生时间、设备系统和业务影响。"
            citations = []
        model = self.models.select(question, evidence)
        query_id = self.database.record(session_id, question, evidence, model["route"])
        return {"query_id": query_id, "answer": answer, "citations": citations,
                "evidence": evidence, "model": model}

