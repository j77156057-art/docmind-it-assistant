"""Read-only IT knowledge query service."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from backend import (
    HybridRetriever, ModelGateway, ModelGatewayError, ModelRouter, QueryDatabase, log_event,
    Principal, request_id_context,
)


LOGGER = logging.getLogger("docmind.it.model")
GREETING_PATTERN = re.compile(
    r"^(?:(?:你|您)?好(?:呀|啊|哇)?|早上好|上午好|下午好|晚上好|嗨|哈喽|在吗|hi|hello|hey)"
    r"[!！,.，。?？~～]*$",
    re.IGNORECASE,
)
OVERVIEW_PATTERNS = (
    re.compile(r"(?:知识库|资料库|文档库|现有资料).*(?:有什么|有哪些|包含|讲了什么|内容|主题|概览|介绍)"),
    re.compile(r"(?:有什么|有哪些|介绍).*(?:知识|资料|文档)"),
    re.compile(r"^(?:你知道什么|你能回答什么|能问什么)[?？]?$"),
)


class ITQueryService:
    def __init__(self, knowledge_path: str, database: QueryDatabase, models: ModelRouter,
                 gateway: ModelGateway | None = None,
                 retriever: HybridRetriever | None = None):
        self.knowledge_path = Path(knowledge_path)
        self.database = database
        self.models = models
        self.gateway = gateway
        self.retriever = retriever

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

    @staticmethod
    def _intent(question: str) -> str:
        normalized = re.sub(r"\s+", "", question.strip())
        if GREETING_PATTERN.fullmatch(normalized):
            return "greeting"
        if any(pattern.search(normalized) for pattern in OVERVIEW_PATTERNS):
            return "overview"
        return "query"

    @staticmethod
    def _overview_answer(documents: list[dict]) -> str:
        if not documents:
            return "当前没有可供你查询的知识文档。"
        lines = [f"当前可查询 {len(documents)} 份知识文档："]
        for index, document in enumerate(documents, 1):
            sections = "、".join(document["sections"]) or "正文"
            lines.extend((
                "",
                f"{index}. {document['title']}（v{document['version']}）",
                f"   主题：{sections}",
            ))
        return "\n".join(lines)

    def query(self, session_id: str, question: str, principal: Principal | None = None) -> dict:
        question = (question or "").strip()
        if not question:
            raise ValueError("问题不能为空")
        subject_id = principal.subject_id if principal else "legacy"
        query_id = self.database.record(
            session_id, question, "pending", "pending", owner_subject_id=subject_id,
        )
        roles = principal.acl_roles if principal else ()
        groups = principal.acl_groups if principal else ()
        intent = self._intent(question)
        if intent == "greeting":
            evidence = "sufficient"
            answer = (
                "你好！我是 DocMind IT 查询助手。你可以直接询问 VPN、账号密码、"
                "软件安装等问题，也可以问“知识库里有什么”查看内容概览。"
            )
            citations = []
        elif intent == "overview":
            documents = self.database.accessible_document_outline(
                subject_id=subject_id, roles=roles, groups=groups,
            )
            if not documents:
                sections = self._sections()
                if sections:
                    documents = [{
                        "title": self.knowledge_path.stem,
                        "version": 1,
                        "sections": [item["title"] for item in sections],
                        "chunk": None,
                        "section": sections[0]["title"],
                        "page": None,
                        "line": sections[0]["line"],
                    }]
            evidence = "sufficient"
            answer = self._overview_answer(documents)
            citations = []
            for item in documents:
                if item.get("chunk"):
                    citations.append({
                        "source": item["title"], "version": item["version"],
                        "chunk": item["chunk"], "section": item["section"],
                        "page": item.get("page"),
                    })
                elif item.get("line"):
                    citations.append({
                        "source": self.knowledge_path.name,
                        "section": item["section"], "line": item["line"],
                    })
        else:
            imported_hits = self.retriever.retrieve(
                question, query_id, subject_id=subject_id, roles=roles, groups=groups,
            ) if self.retriever else []
            if imported_hits:
                evidence = "sufficient"
                answer = "\n\n".join(item["content"] for item in imported_hits[:3])
                citations = [{
                    "source": item["title"],
                    "version": item["version"],
                    "chunk": item["ordinal"] + 1,
                    "section": item["heading"],
                    "page": item.get("page_number"),
                    "score": round(float(item.get("score") or 0), 6),
                } for item in imported_hits[:3]]
            else:
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
        self.database.update_query_route(query_id, model["route"], evidence)
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
