import pytest

from app import store
from mcp_servers import business, rag


def test_search_hides_interviews_before_0105(late_interviews):
    before = rag.search_company_docs("인터뷰 어색 일정", 60)
    after = rag.search_company_docs("인터뷰 어색 일정", 65)
    assert all(h["doc_id"] != "interviews" for h in before)
    assert any(h["doc_id"] == "interviews" for h in after)


def test_read_unreleased_doc_is_error(late_interviews):
    with pytest.raises(ValueError, match="아직 공유되지"):
        rag.read_company_doc("interviews", 30)
    assert "22%" in rag.read_company_doc("metrics", 30)["body"]


def test_search_finds_policy_with_josa():
    hits = rag.search_company_docs("유료 기능은 출시해도 되나요", 0)
    assert hits and hits[0]["doc_id"] == "policy"


def test_search_for_unreleased_doc_says_not_shared(late_interviews):
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


def test_list_work_log_returns_memos_and_narration_only():
    sid = store.create_session("지원자")
    store.add_message(sid, "dm", "candidate", "1:1 메시지", 3)
    store.add_message(sid, "work", "candidate", "1번부터 할게요", 6)
    store.add_message(sid, "work", "narrator", "[내 업무 진행] 맡은 업무를 진행했다.", 6)
    log = business.list_work_log(sid)
    assert [(e["time"], e["sender"]) for e in log] == [("00:06", "candidate"), ("00:06", "narrator")]
    with pytest.raises(KeyError):
        business.list_work_log("없는세션")


def test_doc_resource_gated_by_session_clock(late_interviews):
    sid = store.create_session("지원자")
    store.update_session(sid, sim_seconds=30 * 60)
    with pytest.raises(PermissionError):
        rag.read_doc_resource(sid, "interviews")          # 01:05 전
    assert "22%" in rag.read_doc_resource(sid, "metrics")
    store.update_session(sid, sim_seconds=66 * 60)
    assert "인터뷰" in rag.read_doc_resource(sid, "interviews")
    with pytest.raises(ValueError):
        rag.read_doc_resource("없는세션", "metrics")
