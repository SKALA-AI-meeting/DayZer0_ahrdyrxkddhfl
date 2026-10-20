import asyncio
import json

import pytest

from app import observe, report, store
from app.observe import CheckAnswer
from mcp_servers import business


def test_quote_must_exist_in_source():
    src = "팀장님, 인터뷰 자료가 15분 늦게 와서 2번 파트가 조금 늦어질 것 같아요."
    assert observe.quote_found("인터뷰 자료가 15분 늦게 와서", src, 0.9)
    assert not observe.quote_found("마감을 넘길 것 같습니다", src, 0.9)


def test_verify_drops_unverifiable_yes():
    evidence = {"m1": "지금은 어렵고 10분 뒤에 볼게요"}
    answers = [CheckAnswer(check_id="scheduled", answer="yes", evidence_id="m1",
                           quote="10분 뒤에 볼게요", behavior="일정 조율"),
               CheckAnswer(check_id="helped", answer="yes", evidence_id="m1",
                           quote="바로 같이 봐드릴게요", behavior="도움")]   # 지어낸 인용
    out = {a["check_id"]: a for a in observe.verify(answers, evidence)}
    assert out["scheduled"]["answer"] == "yes" and out["scheduled"]["verified"]
    assert out["helped"]["answer"] == "no" and "dropped_reason" in out["helped"]


def test_evidence_scope_limits_sources():
    # 맥락 전달(context_provided)은 요청 메시지에서만 근거를 인정해야 한다
    evidence = {"m1": "재참여를 높이는 기획에 쓸 거야, 요약해 줘", "plan:1": "대상: 3개월 이상 미참여 사용자"}
    answers = [CheckAnswer(check_id="context_provided", answer="yes", evidence_id="plan:1",
                           quote="대상: 3개월 이상 미참여 사용자", behavior="조건 명시")]
    out = observe.verify(answers, evidence, scopes={"context_provided": "messages"})
    assert out[0]["answer"] == "no"


def test_reuse_classification_thresholds():
    assert observe.classify_reuse(0.95) == "그대로 씀"
    assert observe.classify_reuse(0.6) == "고쳐 씀"
    assert observe.classify_reuse(0.1) == "반영 안 함"


def test_ai_use_without_requests_is_not_used_and_no_llm():
    sid = store.create_session("x")
    result = asyncio.run(observe.observe_ai_use(sid))
    assert result["used"] is False
    body = report.build_body(sid, [result])
    assert body["sections"][0]["note"] == "사용하지 않음"


def test_report_has_no_scores_and_marks_unobserved():
    sid = store.create_session("x")
    mid = store.add_message(sid, "team", "candidate", "자료가 늦어서 1번부터 할게요", 52)
    results = [
        {"element": "책임감", "situation": "자료 지연", "happened": True, "checks": [
            {"check_id": "shared_delay", "answer": "yes", "verified": True, "evidence_id": f"m{mid}",
             "quote": "자료가 늦어서 1번부터 할게요", "behavior": "지연을 알리고 1번부터 진행"},
            {"check_id": "proposed_alternative", "answer": "no", "verified": False, "evidence_id": ""}]},
        {"element": "협력 태도", "situation": "도움 요청", "happened": True, "checks": []},
        {"element": "유연성", "situation": "대상 변경", "happened": False, "checks": []},
    ]
    body = report.build_body(sid, results)
    text = json.dumps(body, ensure_ascii=False)
    assert "점" not in text.replace("시점", "").replace("관점", "")   # 점수 표기 없음
    secs = {s["element"]: s for s in body["sections"]}
    assert secs["책임감"]["observed"][0]["when"] == "00:52"
    assert secs["협력 태도"]["note"] == report.NOT_OBSERVED
    assert secs["유연성"]["note"] == report.NOT_HAPPENED


def test_report_retries_then_fails(monkeypatch):
    sid = store.create_session("x")
    calls = []

    async def broken(_sid):
        calls.append(1)
        raise RuntimeError("LLM 장애")
    monkeypatch.setattr(report, "generate", broken)
    assert asyncio.run(report.generate_with_retry(sid)) == "failed"
    assert len(calls) == 3                                   # 1회 + 재시도 2회 (F-34)
    assert store.get_session(sid)["status"] == "failed"
    assert asyncio.run(report.generate_with_retry(sid)) == "skipped"   # 중복 생성 방지


def test_save_observation_rejects_bad_json():
    sid = store.create_session("x")
    with pytest.raises(ValueError):
        business.save_observation(sid, "책임감", "{깨진 json")
    business.save_observation(sid, "책임감", json.dumps({"a": 1}))
    assert business.list_observations(sid) == {"책임감": {"a": 1}}
