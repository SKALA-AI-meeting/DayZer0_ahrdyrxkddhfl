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


def test_unobserved_lists_candidate_messages_in_window_without_judgement():
    # 관찰 없음이면 구간 안 지원자 메시지 위치만 링크한다(구간 밖·AI 메시지는 제외).
    sid = store.create_session("x")
    store.add_message(sid, "team", "candidate", "이벤트 전 메시지", 60)
    store.add_message(sid, "team", "lead", "대상이 바뀌었어요", 70)
    inside = store.add_message(sid, "dm", "candidate", "좋은 아이디어네요", 118)
    spec = {"name": "유연성", "situation": "대상 변경", "scene": "flex_change",
            "window_end_minute": 120, "candidate_keywords": ["대상"], "checks": []}
    result = asyncio.run(observe.observe_element(sid, spec, {"flex_change": 70}, mode="rules"))
    assert [f["id"] for f in result["followups"]] == [inside]
    section = report.build_body(sid, [result])["sections"][0]
    assert section["note"] == report.NOT_OBSERVED
    assert section["followups"] == [{"id": inside, "when": "01:58"}]


def test_observed_section_has_no_followup_links():
    sid = store.create_session("x")
    mid = store.add_message(sid, "team", "candidate", "대상을 바꿀게요", 72)
    spec = {"name": "유연성", "situation": "대상 변경", "scene": "flex_change",
            "window_end_minute": 120, "candidate_keywords": ["대상"], "checks": []}
    result = asyncio.run(observe.observe_element(sid, spec, {"flex_change": 70}, mode="rules"))
    result["checks"][0].update(quote="대상을 바꿀게요", behavior="")
    section = report.build_body(sid, [result])["sections"][0]
    assert section["observed"] and "followups" not in section and mid


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


def test_doc_view_after_assistant_citation_counts_as_verification():
    sid = store.create_session("x")
    store.add_message(sid, "assistant", "candidate", "재참여율 요약해 줘", 20)
    store.add_message(sid, "assistant", "assistant", "재참여율은 22%입니다. 참고: 지표 현황", 20)
    store.add_doc_view(sid, "metrics", 10)        # 답보다 먼저 연 건 인정하지 않음
    assert observe.cross_checked_docs(sid, store.list_messages(sid, channels=("assistant",))) == []
    store.add_doc_view(sid, "metrics", 21.5)      # 답 이후에 직접 열어 봄
    checks = observe.cross_checked_docs(sid, store.list_messages(sid, channels=("assistant",)))
    assert checks[0]["verified"] and "지표 현황" in checks[0]["behavior"]
    body = report.build_body(sid, [{"element": "AI 활용", "situation": "세션 전체", "used": True,
                                    "requests": [], "checks": checks}])
    assert body["sections"][0]["observed"][0]["when"] == "00:21"


# 과장된 행동 문장: 인용은 원문에 있지만 그 행동을 보여 주지 않으면 버린다(실제 세션에서 하소연이 '확인 질문'으로 기록됨)
def test_verify_drops_quote_that_does_not_support_behavior():
    evidence = {"m1": "대상 유저가 완전히 바뀌어서 처음부터 다시 검토해야 할 것 같습니다 ㅠㅠ",
                "plan:2": "신규/복귀유저 보상에 확률형 아이템 대신 확정 지급 재화. 여기에 확률형 아이템 뽑기를 추가로."}
    answers = [CheckAnswer(check_id="asked_clarification", answer="yes", evidence_id="m1",
                           quote="처음부터 다시 검토해야 할 것 같습니다", behavior="범위를 확인하는 질문"),
               CheckAnswer(check_id="cited_basis", answer="yes", evidence_id="plan:2",
                           quote="확률형 아이템 대신 확정 지급 재화", behavior="운영 정책을 근거로 제시"),
               CheckAnswer(check_id="kept_issue_in_plan", answer="yes", evidence_id="plan:2",
                           quote="여기에 확률형 아이템 뽑기를 추가로", behavior="기획안에 확률형 뽑기를 넣음"),
               CheckAnswer(check_id="endorsed_issue", answer="yes", evidence_id="plan:2",
                           quote="여기에 확률형 아이템 뽑기를 추가로", behavior="동의")]   # 대화 체크인데 기획안 인용
    out = {a["check_id"]: a for a in observe.verify(
        answers, evidence,
        scopes={"endorsed_issue": "messages", "kept_issue_in_plan": "plan"},
        required={"asked_clarification": ["?", "까요", "나요"], "cited_basis": ["정책", "금지"], "kept_issue_in_plan": ["확률형"]})}
    assert out["asked_clarification"]["dropped_reason"] == "인용이 이 행동을 뒷받침하지 않음"
    assert out["cited_basis"]["dropped_reason"] == "인용이 이 행동을 뒷받침하지 않음"
    assert out["kept_issue_in_plan"]["verified"]
    assert out["endorsed_issue"]["answer"] == "no"


def test_report_labels_plan_part_and_merges_same_quote():
    sid = store.create_session("x")
    mid = store.add_message(sid, "dm", "candidate", "전설 상자 지급 좋은 듯합니다", 115)
    q = "확률형 아이템 대신 확정 지급 재화"
    checks = [{"check_id": "noticed_error", "answer": "yes", "verified": True, "evidence_id": "plan:2", "quote": q,
               "behavior": "기획안에서 바꿈", "point": "알리거나 고침"},
              {"check_id": "proposed_fix", "answer": "yes", "verified": True, "evidence_id": "plan:2", "quote": q,
               "behavior": "대안 반영", "point": "대안 제안"},
              {"check_id": "endorsed_issue", "answer": "yes", "verified": True, "evidence_id": f"m{mid}",
               "quote": "전설 상자 지급 좋은 듯합니다", "behavior": "동료 제안에 동의함"}]
    obs = report.build_body(sid, [{"element": "존중·소통", "situation": "s", "happened": True, "checks": checks}])["sections"][0]["observed"]
    assert len(obs) == 2
    assert obs[0]["when"] == "기획안 2번" and obs[0]["behavior"] == "기획안에서 바꿈 / 대안 반영" and obs[0]["message_id"] is None
    assert obs[1]["when"] == "01:55" and obs[1]["message_id"] == mid
    assert obs[0]["points"] == ["알리거나 고침", "대안 제안"] and obs[1]["points"] == []


def test_observed_checks_carry_observation_point():
    sid = store.create_session("x")
    store.add_message(sid, "team", "candidate", "대상을 바꿔서 1번부터 다시 쓸게요", 72)
    spec = {"name": "유연성", "situation": "대상 변경", "scene": "flex_change", "window_end_minute": 120,
            "candidate_keywords": ["대상"], "checks": [{"id": "keyword", "point": "바뀐 대상에 반응함"}]}
    result = asyncio.run(observe.observe_element(sid, spec, {"flex_change": 70}, mode="rules"))
    checks = observe._with_points(result["checks"], spec["checks"])
    assert checks[0]["point"] == "바뀐 대상에 반응함"


def _section():
    return {"element": "존중·소통", "situation": "s", "observed": [
        {"behavior": "기획안에서 확정 지급으로 바꿈", "quote": "확률형 아이템 대신 확정 지급 재화", "when": "기획안 2번", "source": "기획안"},
        {"behavior": "동료 제안에 동의함", "quote": "전설 장비 상자 지급 둘 다 좋은 듯합니다", "when": "01:55", "source": "대화"}]}


# 요약 검증: 판단 없이 근거 번호·원문 그대로 인용만 통과한다
def test_summary_check_accepts_side_by_side_facts():
    ok = ('지원자는 기획안 2번에 "확률형 아이템 대신 확정 지급 재화"라고 적었다[1]. '
          '01:55 대화에서는 "전설 장비 상자 지급 둘 다 좋은 듯합니다"라고 답했다[2].')
    assert report.check_summary(ok, _section()) == "ok"


def test_summary_check_rejects_judgement_missing_ref_and_made_up_quote():
    s = _section()
    assert report.check_summary("기획안과 대화가 모순된다[1][2].", s) == "evaluative:모순"
    assert report.check_summary("기획안 2번에 확정 지급을 적었다. 대화에서는 동의했다[2].", s) == "no_ref"
    assert report.check_summary("기획안 2번에 확정 지급을 적었다[3].", s) == "bad_ref"
    assert report.check_summary('대화에서 "확률형은 꼭 넣어야 해요"라고 했다[2].', s).startswith("unquoted:")
    # 중간점검 잠재적: 작은따옴표로 지어낸 인용도 잡는다. 근거에 있는 문서 제목은 통과
    assert report.check_summary("지원자는 '확률형은 꼭 넣어야 한다'고 적었다[1].", s).startswith("unquoted:")
    s2 = {"observed": [{"quote": "'지표 현황' 문서 열람", "behavior": "근거 문서 '지표 현황'을 직접 열어 확인함",
                        "when": "01:32", "source": "문서 열람"}]}
    assert report.check_summary("지원자는 '지표 현황' 문서를 직접 열어 확인했다[1].", s2) == "ok"


def test_summary_skipped_when_single_evidence(monkeypatch):
    called = []
    monkeypatch.setattr(report.llm, "ainvoke", lambda *a, **k: called.append(1))
    one = {**_section(), "observed": _section()["observed"][:1]}
    assert asyncio.run(report.summarize_section("x", one)) is None and not called


def test_summary_hidden_when_verification_keeps_failing(monkeypatch):
    class Out:
        content = "지원자는 소통 역량이 부족하다[1]."
    async def fake(*a, **k):
        return Out()
    monkeypatch.setattr(report.llm, "ainvoke", fake)
    assert asyncio.run(report.summarize_section("x", _section())) is None


# 판별기가 일부 체크에 답하지 않으면 그 체크만 다시 묻고, 그래도 없으면 '답하지 않음'으로 기록한다
CHECKS = [{"id": "helped", "question": "도왔나?"}, {"id": "scheduled", "question": "일정을 제안했나?"},
          {"id": "declined", "question": "거절했나?"}]


def _fake_judge(monkeypatch, replies):
    asked = []

    async def fake(model, messages, node, sid=""):
        asked.append([line[2:].split(":")[0] for line in messages[1].content.splitlines() if line.startswith("- ")])
        return observe.Judgement(answers=replies.pop(0))
    monkeypatch.setattr(observe.llm, "ainvoke", fake)
    return asked


def test_judge_reasks_only_missing_checks(monkeypatch):
    asked = _fake_judge(monkeypatch, [
        [CheckAnswer(check_id="helped", answer="yes", evidence_id="m1", quote="같이 봐요"),
         CheckAnswer(check_id="made_up", answer="yes")],                       # scheduled·declined 누락 + 없는 id
        [CheckAnswer(check_id="scheduled", answer="no"), CheckAnswer(check_id="declined", answer="no")]])
    answers, missing = asyncio.run(observe._judge("s", "상황", CHECKS, "기록", "", use_cache=False))
    assert asked == [["helped", "scheduled", "declined"], ["scheduled", "declined"]]
    assert [a.check_id for a in answers] == ["helped", "scheduled", "declined"] and not missing


def test_judge_records_checks_still_missing(monkeypatch):
    _fake_judge(monkeypatch, [[CheckAnswer(check_id="helped", answer="no")]] * 2)
    answers, missing = asyncio.run(observe._judge("s", "상황", CHECKS, "기록", "", use_cache=False))
    assert missing == {"scheduled", "declined"} and all(a.answer == "no" for a in answers)
    marked = observe._mark_missing(observe.verify(answers, {}), missing)
    assert {c["check_id"]: c.get("dropped_reason") for c in marked} == {
        "helped": None, "scheduled": observe.MISSING_REASON, "declined": observe.MISSING_REASON}


def test_judge_ignores_incomplete_old_cache(monkeypatch):
    asked = _fake_judge(monkeypatch, [[CheckAnswer(check_id=c["id"], answer="no") for c in CHECKS]])
    human_key = None
    real_put = store.cache_put

    def spy_put(key, value):
        nonlocal human_key
        human_key = key
        real_put(key, value)
    monkeypatch.setattr(observe.store, "cache_put", spy_put)
    asyncio.run(observe._judge("s", "상황", CHECKS, "기록-캐시", "", use_cache=True))
    real_put(human_key, {"answers": [{"check_id": "helped", "answer": "yes"}]})   # 예전 형식: 체크가 빠진 캐시
    _fake_judge(monkeypatch, [[CheckAnswer(check_id=c["id"], answer="no") for c in CHECKS]])
    answers, _ = asyncio.run(observe._judge("s", "상황", CHECKS, "기록-캐시", "", use_cache=True))
    assert len(answers) == 3 and len(asked) == 1


# 회귀: 기획안 "(확률형x)"(확률형을 쓰지 않음)를 '정책과 어긋나는 확률형 내용이 들어 있음'으로 기록했다
def test_verify_drops_negated_quote():
    from app import settings
    spec = [e for e in settings.observation_spec("pixel")["element"] if e["name"] == "존중·소통"][0]
    req = {c["id"]: c["quote_must_contain"] for c in spec["checks"] if c.get("quote_must_contain")}
    neg = {c["id"]: c["quote_must_not_contain"] for c in spec["checks"] if c.get("quote_must_not_contain")}
    ev = {"plan:3": "-> 복귀 이벤트 진행 (확률형x) 저녁 시간대 위주로 알림 발송",
          "plan:2": "여기에 확률형 아이템 뽑기를 추가로.",
          "m5": "운영 정책에 확률형 아이템을 지급하지 않는다는 항목이 있어서 수정이 필요할 것 같습니다",
          "m6": "전설 장비 상자 지급 둘 다 좋은 듯합니다!"}
    answers = [CheckAnswer(check_id="kept_issue_in_plan", answer="yes", evidence_id="plan:3", quote="-> 복귀 이벤트 진행 (확률형x) 저녁 시간대"),
               CheckAnswer(check_id="kept_issue_in_plan", answer="yes", evidence_id="plan:2", quote="여기에 확률형 아이템 뽑기를 추가로."),
               CheckAnswer(check_id="endorsed_issue", answer="yes", evidence_id="m5", quote="운영 정책에 확률형 아이템을 지급하지 않는다는 항목이 있어서"),
               CheckAnswer(check_id="endorsed_issue", answer="yes", evidence_id="m6", quote="전설 장비 상자 지급 둘 다 좋은 듯합니다!")]
    scopes = {"kept_issue_in_plan": "plan", "endorsed_issue": "messages"}
    out = observe.verify(answers, ev, scopes, req, neg)
    assert out[0]["dropped_reason"] == "인용이 반대 뜻(부정·제외)을 담고 있음"
    assert out[1]["verified"]
    assert out[2]["answer"] == "no" and out[3]["verified"]


def test_report_merges_quote_contained_in_another():
    sid = store.create_session("x")
    mid = store.add_message(sid, "dm", "candidate", "그런데 운영 정책에 확률형 금지 항목이 있어서 수정이 필요할 것 같습니다.", 97)
    checks = [{"check_id": "noticed_error", "answer": "yes", "verified": True, "evidence_id": f"m{mid}",
               "quote": "그런데 운영 정책에 확률형 금지 항목이 있어서 수정이 필요할 것 같습니다.", "point": "알림"},
              {"check_id": "cited_basis", "answer": "yes", "verified": True, "evidence_id": f"m{mid}",
               "quote": "운영 정책에 확률형 금지 항목이 있어서", "point": "근거 제시"}]
    obs = report.build_body(sid, [{"element": "존중·소통", "situation": "s", "happened": True, "checks": checks}])["sections"][0]["observed"]
    assert len(obs) == 1 and obs[0]["points"] == ["알림", "근거 제시"] and obs[0]["quote"].startswith("그런데")


# 회귀: 판별기가 지원자 의견(00:42)을 '일정 조율'에 넣고, '의견을 줌'에는 동료 말을 인용해 버려졌다
# → 일정 포인트는 시점 표현이 있어야 인정, 버려진 체크는 이유를 알려 다시 묻는다
def test_dropped_checks_are_reasked_with_reason(monkeypatch):
    from app import settings
    sid = store.create_session("x", scenario="pixel")
    store.add_message(sid, "dm", "coworker", "비슷한 게임 사례 기준이 헷갈려요. 10분만 같이 봐줄 수 있을까요?", 30)
    co = store.add_message(sid, "dm", "coworker", "어떤 사례를 찾고 싶은지 기준을 정리해 보면 좋겠어요", 37)
    me = store.add_message(sid, "dm", "candidate", "전투 기능이 있는 게임을 찾아보면 좋을 것 같아요", 42)
    spec = [e for e in settings.observation_spec("pixel")["element"] if e["name"] == "협력 태도"][0]
    replies = [
        [CheckAnswer(check_id="helped", answer="yes", evidence_id=f"m{co}", quote="어떤 사례를 찾고 싶은지 기준을 정리해 보면 좋겠어요"),
         CheckAnswer(check_id="scheduled", answer="yes", evidence_id=f"m{me}", quote="전투 기능이 있는 게임을 찾아보면 좋을 것 같아요"),
         CheckAnswer(check_id="declined", answer="no")],
        [CheckAnswer(check_id="helped", answer="yes", evidence_id=f"m{me}", quote="전투 기능이 있는 게임을 찾아보면 좋을 것 같아요"),
         CheckAnswer(check_id="scheduled", answer="no")]]
    asked = []

    async def fake(model, messages, node, sid_=""):
        asked.append(messages[1].content)
        return observe.Judgement(answers=replies.pop(0))
    monkeypatch.setattr(observe.llm, "ainvoke", fake)
    r = asyncio.run(observe.observe_element(sid, spec, {"coop_help": 30}, use_cache=False))
    got = {c["check_id"]: c for c in r["checks"]}
    assert got["helped"]["verified"] and got["helped"]["evidence_id"] == f"m{me}"
    assert got["scheduled"]["answer"] == "no" and got["declined"]["answer"] == "no"
    assert len(asked) == 2 and "코드 검증에서 버려졌다" in asked[1] and "- declined" not in asked[1]


# 부정·제외 표현 때문에 뺀 원문은 리포트에서 사라지지 않고 '판단이 필요한 원문'으로 남는다
def test_negated_quote_is_kept_for_human_review(monkeypatch):
    from app import settings
    sid = store.create_session("x", scenario="pixel")
    store.upsert_part(sid, "3", "-> 복귀 이벤트 진행 (확률형x) 저녁 시간대 위주로 알림 발송", 100)
    spec = [e for e in settings.observation_spec("pixel")["element"] if e["name"] == "존중·소통"][0]
    yes = CheckAnswer(check_id="kept_issue_in_plan", answer="yes", evidence_id="plan:3", quote="(확률형x) 저녁 시간대")

    async def fake(model, messages, node, sid_=""):   # 물어본 체크마다 답한다. kept_issue_in_plan은 계속 같은 오판
        ids = [line[2:].split(":")[0] for line in messages[1].content.splitlines() if line.startswith("- ")]
        return observe.Judgement(answers=[yes if i == "kept_issue_in_plan" else CheckAnswer(check_id=i, answer="no")
                                          for i in ids])
    monkeypatch.setattr(observe.llm, "ainvoke", fake)
    r = asyncio.run(observe.observe_element(sid, spec, {"respect_review": 90}, use_cache=False))
    kept = {c["check_id"]: c for c in r["checks"]}["kept_issue_in_plan"]
    assert kept["answer"] == "no" and r["negated"]
    section = report.build_body(sid, [r])["sections"][0]
    assert not section["observed"] and section["review"] == [{"when": "기획안 3번", "quote": "(확률형x) 저녁 시간대", "message_id": None}]
