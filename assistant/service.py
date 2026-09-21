"""Read-only IT knowledge query service."""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
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
NAMED_OVERVIEW_PATTERN = re.compile(
    r"^(?:请问|帮我看看|介绍一下)?(.{2,80}?)(?:这份|这个|这篇)?(?:文档|资料)?"
    r"(?:主要)?(?:讲了什么|讲什么|说了什么|是什么内容|有哪些内容|内容是什么|主要内容)"
    r"[?？!！。]*$",
    re.IGNORECASE,
)
GENERIC_DOCUMENT_TITLES = {"document", "untitled", "未命名", "文档"}


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
    def _named_overview_subject(question: str) -> str:
        normalized = re.sub(r"\s+", "", question.strip())
        match = NAMED_OVERVIEW_PATTERN.fullmatch(normalized)
        return match.group(1).strip("：:，,。") if match else ""

    @staticmethod
    def _match_key(value: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (value or "").lower())

    @classmethod
    def _document_name_score(cls, subject: str, document: dict) -> float:
        needle = cls._match_key(subject)
        if not needle:
            return 0.0
        candidates = [document.get("title", "")]
        sections = document.get("sections") or []
        if sections:
            candidates.append(sections[0])
        best = 0.0
        for candidate in candidates:
            candidate_key = cls._match_key(candidate)
            if not candidate_key:
                continue
            if needle in candidate_key or candidate_key in needle:
                best = max(best, 1.0)
            best = max(best, SequenceMatcher(None, needle, candidate_key).ratio())
            latin_words = re.findall(r"[a-z0-9]+", candidate.lower())
            if latin_words:
                best = max(
                    best,
                    *(SequenceMatcher(None, needle, word).ratio() for word in latin_words),
                )
        return best

    @classmethod
    def _matching_documents(cls, subject: str, documents: list[dict]) -> list[dict]:
        ranked = sorted(
            ((cls._document_name_score(subject, item), item) for item in documents),
            key=lambda item: (-item[0], item[1]["title"]),
        )
        if not ranked or ranked[0][0] < 0.78:
            return []
        best = ranked[0][0]
        return [item for score, item in ranked if score >= max(0.78, best - 0.05)][:3]

    @staticmethod
    def _display_title(document: dict) -> str:
        title = (document.get("title") or "").strip()
        sections = document.get("sections") or []
        if title.lower() in GENERIC_DOCUMENT_TITLES and sections:
            return sections[0]
        return title or (sections[0] if sections else "未命名文档")

    @staticmethod
    def _overview_answer(documents: list[dict]) -> str:
        if not documents:
            return "当前没有可供你查询的知识文档。"
        lines = [f"当前可查询 {len(documents)} 份知识文档："]
        for index, document in enumerate(documents, 1):
            display_title = ITQueryService._display_title(document)
            section_names = list(document["sections"])
            if (
                section_names
                and ITQueryService._match_key(section_names[0])
                == ITQueryService._match_key(display_title)
            ):
                section_names = section_names[1:]
            sections = "、".join(section_names) or "正文"
            lines.extend((
                "",
                f"{index}. {display_title}（v{document['version']}）",
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
        answer_context = ""
        intent = self._intent(question)
        named_overview = self._named_overview_subject(question) if intent == "query" else ""
        named_documents = []
        if named_overview:
            outline = self.database.accessible_document_outline(
                subject_id=subject_id, roles=roles, groups=groups,
                allow_confidential=bool(principal and principal.confidential_clearance),
            )
            named_documents = self._matching_documents(named_overview, outline)
            if named_documents:
                intent = "overview"
        if intent == "greeting":
            evidence = "sufficient"
            answer = (
                "你好！我是 DocMind IT 查询助手。你可以直接询问 VPN、账号密码、"
                "软件安装等问题，也可以问“知识库里有什么”查看内容概览。"
            )
            citations = []
        elif intent == "overview":
            documents = (
                named_documents if named_overview
                else self.database.accessible_document_outline(
                    subject_id=subject_id, roles=roles, groups=groups,
                    allow_confidential=bool(principal and principal.confidential_clearance),
                )
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
                        "source": self._display_title(item), "version": item["version"],
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
                # Clearance is taken from the asking principal, so a viewer can never be served
                # confidential chunks through a generative answer.
                allow_confidential=bool(principal and principal.confidential_clearance),
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
                answer_context = "\n\n".join(
                    f"[{index}] 来源：{item['title']}\n章节：{item['heading']}\n{item['content'][:2500]}"
                    for index, item in enumerate(imported_hits[:3], 1)
                )
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
                    answer_context = "\n\n".join(
                        f"[{index}] 来源：{self.knowledge_path.name}\n章节：{item['title']}\n{item['text'][:2500]}"
                        for index, item in enumerate(hits, 1)
                    )
                else:
                    evidence = "insufficient"
                    answer = "现有资料不足以可靠回答。请补充报错内容、发生时间、设备系统和业务影响。"
                    citations = []
        response_strategy = self.models.response_strategy()
        model = self.models.select(question, evidence, response_strategy)
        self.database.update_query_route(query_id, model["route"], evidence)
        usage = None
        if model["route"] in {"local", "cloud"}:
            if self.gateway is None:
                if response_strategy != "generative_first":
                    raise ModelGatewayError("model_gateway_unavailable", [])
                answer = answer if evidence == "sufficient" else (
                    "当前生成式模型暂时不可用，现有资料不足以可靠回答。"
                    "请补充报错内容、发生时间、设备系统和业务影响。"
                )
                return {"query_id": query_id, "answer": answer, "citations": citations,
                        "evidence": evidence, "model": model, "usage": usage}
            try:
                result = self.gateway.complete(
                    route=model,
                    base_url=self.models.base_url(model),
                    api_key=self.models.credential(model["provider"]),
                    question=question,
                    context=answer_context,
                    citations=citations,
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
                if response_strategy != "generative_first":
                    raise
                log_event(
                    LOGGER, logging.WARNING, "model_call_degraded",
                    provider=model["provider"], model=model["model"], reason=exc.code,
                )
                answer = answer if evidence == "sufficient" else (
                    "当前生成式模型暂时不可用，现有资料不足以可靠回答。"
                    "请补充报错内容、发生时间、设备系统和业务影响。"
                )
            else:
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
