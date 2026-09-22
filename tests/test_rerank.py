"""Tests for the re-ranking layer (backend.rerank)."""
from types import SimpleNamespace

from backend.rerank import ApiReranker, LexicalReranker, Reranker, build_reranker


def _candidates():
    return [
        {"id": 1, "content": "打印机无法连接无线网络，请检查 IP 地址配置"},
        {"id": 2, "content": "VPN 凭据过期需要在统一身份门户重置密码"},
        {"id": 3, "content": "年度财务报表已于上周提交审计"},
    ]


def test_lexical_reranker_promotes_best_match():
    reranker = LexicalReranker()
    query = "VPN 凭据过期 重置密码"
    ranked = reranker.rerank(query, _candidates())
    assert ranked[0]["id"] == 2
    assert ranked[0]["rerank_rank"] == 1
    # every candidate is preserved, only re-ordered
    assert {c["id"] for c in ranked} == {1, 2, 3}


def test_lexical_reranker_empty_input():
    assert LexicalReranker().rerank("anything", []) == []


def test_lexical_reranker_annotates_score():
    ranked = LexicalReranker().rerank("VPN 凭据", _candidates())
    assert "rerank_score" in ranked[0]
    # better match should out-score a non-match
    assert ranked[0]["rerank_score"] >= ranked[-1]["rerank_score"]


def test_api_reranker_falls_back_on_failure():
    fallback = LexicalReranker()
    reranker = ApiReranker(
        base_url="http://127.0.0.1:9/nope", model="x", api_key="", fallback=fallback,
    )
    # No server there -> must degrade to the lexical fallback, never raise.
    ranked = reranker.rerank("VPN 凭据过期", _candidates())
    assert {c["id"] for c in ranked} == {1, 2, 3}


def test_build_reranker_disabled_returns_none():
    settings = SimpleNamespace(
        rerank_enabled=False, rerank_mode="lexical", rerank_base_url="",
        rerank_model="", rerank_api_key=SimpleNamespace(get_secret_value=lambda: ""),
    )
    assert build_reranker(settings) is None


def test_build_reranker_lexical_default():
    settings = SimpleNamespace(
        rerank_enabled=True, rerank_mode="lexical", rerank_base_url="",
        rerank_model="", rerank_api_key=SimpleNamespace(get_secret_value=lambda: ""),
    )
    reranker = build_reranker(settings)
    assert isinstance(reranker, LexicalReranker)
    assert not isinstance(reranker, ApiReranker)


def test_build_reranker_api_mode_selects_api():
    settings = SimpleNamespace(
        rerank_enabled=True, rerank_mode="api", rerank_base_url="http://rerank.local",
        rerank_model="bge-reranker", rerank_api_key=SimpleNamespace(get_secret_value=lambda: "k"),
    )
    reranker = build_reranker(settings)
    assert isinstance(reranker, ApiReranker)


def test_reranker_is_subclass_of_protocol():
    assert isinstance(LexicalReranker(), Reranker)
