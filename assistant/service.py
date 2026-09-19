"""Read-only IT knowledge query service."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from backend import (
    ModelGateway, ModelGatewayError, ModelRouter, QueryDatabase, log_event,
    request_id_context,
)


LOGGER = logging.getLogger("docmind.it.model")


class ITQueryService:
    def __init__(self, knowledge_path: str, database: QueryDatabase, models: ModelRouter,
                 gateway: ModelGateway | None = None):
        self.knowledge_path = Path(knowledge_path)
        self.database = database
        self.models = models
        self.gateway = gateway

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
        usage = None
        if model["route"] in {"local", "cloud"}:
            if self.gateway is None:
                raise ModelGatewayError("model_gateway_unavailable", [])
            try:
                result = self.gateway.complete(
                    route=model,
                    base_url=self.models.base_url(model),
                    api_key=self.models.credential(model["provider"]),
                    question=question,
                    request_id=request_id_context.get(),
                )
            except ModelGatewayError as exc:
                self.database.record_model_attempts(
                    query_id, request_id_context.get(), model, exc.attempts,
                )
                log_event(
                    LOGGER, logging.WARNING, "model_call_failed",
                    provider=model["provider"], model=model["model"], reason=exc.code,
                )
                raise
            self.database.record_model_attempts(
                query_id, request_id_context.get(), model, result.attempts,
            )
            answer = result.content
            usage = self.gateway.public_usage(model, result.attempts[-1])
            log_event(
                LOGGER, logging.INFO, "model_call_succeeded",
                provider=model["provider"], model=model["model"],
                duration_ms=result.attempts[-1].latency_ms,
            )
        return {"query_id": query_id, "answer": answer, "citations": citations,
                "evidence": evidence, "model": model, "usage": usage}
