import pytest

from app import store
from mcp_servers import business, rag


def test_search_hides_interviews_before_0105():
    before = rag.search_company_docs("인터뷰 어색 일정", 60)
    after = rag.search_company_docs("인터뷰 어색 일정", 65)
    assert all(h["doc_id"] != "interviews" for h in before)
    assert any(h["doc_id"] == "interviews" for h in after)


def test_read_unreleased_doc_is_error():
    with pytest.raises(ValueError, match="아직 공유되지"):
        rag.read_company_doc("interviews", 30)
    assert "22%" in rag.read_company_doc("metrics", 30)["body"]


def test_search_finds_policy_with_josa():
    hits = rag.search_company_docs("유료 기능은 출시해도 되나요", 0)
    assert hits and hits[0]["doc_id"] == "policy"


def test_search_for_unreleased_doc_says_not_shared():
    hits = rag.search_company_docs("사용자 인터뷰 요약 내용 알려줘", 55)
    assert hits[0].get("notice") == "'사용자 인터뷰 요약' 문서는 아직 공유되지 않았습니다."
    assert all("excerpt" not in h for h in hits if h["doc_id"] == "interviews")


def test_search_prefers_doc_named_in_query():
    # "인터뷰 요약에서 … 이유" → 지표 문서가 아니라 인터뷰 문서가 먼저 나와야 한다
    hits = rag.search_company_docs("사용자 인터뷰 요약에서 재참여하지 않은 이유를 정리해 줘", 66)
    assert hits[0]["doc_id"] == "interviews"


def test_assign_tasks_validates_and_updates():
    sid = store.create_session("지원자")
    business.assign_tasks(sid, {"4": "candidate"})
    assert {t["part"]: t["owner"] for t in business.list_tasks(sid)}["4"] == "candidate"
    with pytest.raises(ValueError):
        business.assign_tasks(sid, {"9": "candidate"})
    with pytest.raises(KeyError):
        business.list_tasks("없는세션")


def test_read_shared_plan_returns_candidate_and_coworker_text():
    sid = store.create_session("지원자")
    store.upsert_part(sid, "1", "문제: 재참여율 22%", 20)
    store.upsert_part(sid, "coworker:4", "사례 A, B", 90)
    parts = {p["part"]: p for p in business.read_shared_plan(sid)["parts"]}
    assert parts["1"]["candidate_text"] == "문제: 재참여율 22%" and parts["1"]["owner"] == "candidate"
    assert parts["4"]["coworker_text"] == "사례 A, B"
