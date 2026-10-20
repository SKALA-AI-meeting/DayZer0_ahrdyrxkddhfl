"""벡터 검색: 미리 색인(모델·차원 기록, 바뀐 문서만 재색인), 공개 시각 유지, 실패 시 키워드 대체.
OpenAI 대신 글자 2-gram 해시로 만든 가짜 임베딩을 쓴다."""
import hashlib
import math

import pytest

from app import embedding, settings, store
from mcp_servers import rag

DIMS = 64
calls = []


def fake_embed(texts):
    calls.append(len(texts))
    out = []
    for t in texts:
        v = [0.0] * DIMS
        for i in range(len(t) - 1):
            v[int(hashlib.md5(t[i:i + 2].encode()).hexdigest(), 16) % DIMS] += 1
        n = math.sqrt(sum(x * x for x in v)) or 1
        out.append([x / n for x in v])
    return out


@pytest.fixture(autouse=True)
def vector_mode(monkeypatch):
    calls.clear()
    monkeypatch.setattr(embedding, "embed", fake_embed)
    monkeypatch.setitem(settings.config()["rag"], "search_mode", "vector")


def test_index_records_model_and_dims_and_skips_unchanged():
    n = embedding.ensure_index("")
    assert n > 0
    rows = store.chunk_index(["pack:moarun"], settings.config()["rag"]["embedding_model"])
    assert {r["dims"] for r in rows} == {DIMS} and {r["model"] for r in rows} == {"text-embedding-3-small"}
    calls.clear()
    assert embedding.ensure_index("") == 0 and calls == []      # 내용이 같으면 다시 임베딩하지 않음


def test_vector_search_finds_policy_and_keeps_release_time():
    hits = rag.search_company_docs("올해 유료 기능 출시해도 돼?", 30)
    assert hits[0]["doc_id"] == "policy" and hits[0]["via"] == "vector"
    assert all(h["doc_id"] != "interviews" for h in rag.search_company_docs("인터뷰 어색 일정", 30)
               if "excerpt" in h)                                # 01:05 전엔 벡터 검색에서도 안 나옴


def test_falls_back_to_keyword_when_embedding_fails(monkeypatch):
    def broken(_texts):
        raise RuntimeError("임베딩 장애")
    monkeypatch.setattr(embedding, "embed", broken)
    hits = rag.search_company_docs("유료 기능은 출시해도 되나요", 0)
    assert hits and hits[0]["via"] == "keyword" and hits[0]["doc_id"] == "policy"


def test_company_doc_replaces_scenario_doc_in_index():
    store.upsert_company_doc("A사", "policy", "운영 정책", "올해부터 프리미엄 구독을 출시한다. 위치는 구 단위까지 공개.")
    hits = rag.search_company_docs("프리미엄 구독 출시", 0, "A사")
    assert hits[0]["doc_id"] == "policy" and "프리미엄" in hits[0]["excerpt"]
    other = rag.search_company_docs("프리미엄 구독 출시", 0, "B사")
    assert all("프리미엄" not in h.get("excerpt", "") for h in other)
