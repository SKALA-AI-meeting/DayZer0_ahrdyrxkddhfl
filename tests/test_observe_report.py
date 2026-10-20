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


def test_evidence_channel_limits_sources():
    # "팀장에게 알렸는가"는 팀장 1:1·팀 채널의 지원자 글만 근거로 인정한다(동료에게 한 말·업무 메모는 아님)
    check = {"id": "escalated", "evidence_scope": "messages", "channels": ["lead_dm", "team"]}
    assert observe.allowed_channels(check) == {"lead_dm", "team"}
    assert observe.allowed_channels({"id": "x", "evidence_scope": "memo"}) == {"work"}
    evidence = {"m1": "도현님 일이 밀려서 마감이 어려울 것 같아요", "m2": "팀장님, 도현님 업무가 밀렸어요"}
    channels = {"m1": "dm", "m2": "lead_dm"}
    answers = [CheckAnswer(check_id="escalated", answer="yes", evidence_id="m1", quote="도현님 일이 밀려서"),
               CheckAnswer(check_id="escalated", answer="yes", evidence_id="m2", quote="도현님 업무가 밀렸어요")]
    out = observe.verify(answers, evidence, {"escalated": observe.allowed_channels(check)}, channels_of=channels)
    assert out[0]["answer"] == "no" and out[1]["verified"]


def test_report_has_no_scores_and_marks_unobserved():
    sid = store.create_session("x")
    mid = store.add_message(sid, "team", "candidate", "자료가 늦어서 1번부터 할게요", 52)
    results = [
        {"element": "책임감·성실성", "situation": "자료 지연", "happened": True, "checks": [
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
    assert secs["책임감·성실성"]["observed"][0]["when"] == "00:52"
    assert secs["책임감·성실성"]["observed"][0]["source"] == "팀 채널"
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


# 과장된 행동 문장: 인용은 원문에 있지만 그 행동을 보여 주지 않으면 버린다(실제 세션에서 하소연이 '확인 질문'으로 기록됨)
def test_verify_drops_quote_that_does_not_support_behavior():
    evidence = {"m1": "대상이 완전히 바뀌어서 처음부터 다시 해야 할 것 같습니다 ㅠㅠ",
                "m2": "바뀐 대상 기준으로 우선순위를 다시 정할게요"}
    channels = {"m1": "lead_dm", "m2": "work"}
    answers = [CheckAnswer(check_id="asked_criteria", answer="yes", evidence_id="m1",
                           quote="처음부터 다시 해야 할 것 같습니다", behavior="기준을 확인하는 질문"),   # 하소연
               CheckAnswer(check_id="replanned_in_memo", answer="yes", evidence_id="m2",
                           quote="바뀐 대상 기준으로 우선순위를 다시 정할게요", behavior="메모에서 다시 계획"),
               CheckAnswer(check_id="renegotiated", answer="yes", evidence_id="m2",
                           quote="바뀐 대상 기준으로 우선순위를 다시 정할게요", behavior="재조정 제안")]  # 메시지 체크인데 메모 인용
    allowed = {"asked_criteria": {"lead_dm", "team"}, "replanned_in_memo": {"work"},
               "renegotiated": {"team", "dm", "lead_dm"}}
    out = {a["check_id"]: a for a in observe.verify(
        answers, evidence, allowed, required={"asked_criteria": ["?", "까요", "나요", "기준"],
                                             "replanned_in_memo": ["대상", "우선"]}, channels_of=channels)}
    assert out["asked_criteria"]["dropped_reason"] == "인용이 이 행동을 뒷받침하지 않음"
    assert out["replanned_in_memo"]["verified"]
    assert out["renegotiated"]["answer"] == "no"


def test_report_labels_memo_source_and_merges_same_quote():
    sid = store.create_session("x")
    memo = store.add_message(sid, "work", "candidate", "3번 사례 조사는 빼고 4번에 집중할게요", 16)
    mid = store.add_message(sid, "lead_dm", "candidate", "팀장님, 도현님 업무가 밀렸어요", 17)
    q = "3번 사례 조사는 빼고 4번에 집중할게요"
    checks = [{"check_id": "recovery_plan", "answer": "yes", "verified": True, "evidence_id": f"m{memo}", "quote": q,
               "behavior": "범위를 줄임", "point": "계획을 냄"},
              {"check_id": "x", "answer": "yes", "verified": True, "evidence_id": f"m{memo}", "quote": q,
               "behavior": "우선순위를 정함", "point": "우선순위"},
              {"check_id": "escalated", "answer": "yes", "verified": True, "evidence_id": f"m{mid}",
               "quote": "팀장님, 도현님 업무가 밀렸어요", "behavior": "팀장에게 알림"}]
    body = report.build_body(sid, [{"element": "책임감·성실성", "situation": "s", "happened": True, "checks": checks}])
    obs = body["sections"][0]["observed"]
    assert len(obs) == 2
    assert obs[0]["source"] == "업무 메모" and obs[0]["behavior"] == "범위를 줄임 / 우선순위를 정함"
    assert obs[0]["points"] == ["계획을 냄", "우선순위"]
    assert obs[1]["when"] == "00:17" and obs[1]["source"] == "팀장 1:1" and obs[1]["message_id"] == mid


def test_report_lists_memos_with_category_without_judgement():
    sid = store.create_session("x")
    m1 = store.add_message(sid, "work", "candidate", "1번부터 할게요", 6)
    store.add_message(sid, "work", "narrator", "[내 업무 진행] 맡은 업무를 진행했다.", 6)
    store.add_work_memo(sid, m1, "own")
    store.add_message(sid, "work", "candidate", "분류 실패한 메모", 11)      # 범주 기록이 없으면 기타
    memos = report.build_body(sid, [])["memos"]
    assert [(m["when"], m["memo"], m["category"]) for m in memos] == [
        ("00:06", "1번부터 할게요", "내 업무 진행"), ("00:11", "분류 실패한 메모", "기타")]


# 규칙 관찰: 팀장이 묻기 전에 먼저 연락했는가 (확인 장면이 건너뛰어진 기록 + 첫 메시지)
def test_rule_contacted_lead_first_and_skip_scene():
    from app import engine, settings
    sid = store.create_session("x")
    store.claim_scene(sid, "coworker_miss", 15)
    store.add_message(sid, "team", "candidate", "도현님 괜찮아요?", 16)          # 동료를 부른 팀 채널 글은 아님
    checkin = settings.scene_by_id("lead_checkin")
    assert not engine.should_skip(sid, checkin)
    mid = store.add_message(sid, "lead_dm", "candidate", "팀장님, 도현님 업무가 밀렸어요", 17)
    assert engine.should_skip(sid, checkin)
    store.claim_scene(sid, "lead_checkin", 20)
    store.claim_scene(sid, "skipped:lead_checkin", 20)
    [c] = observe.rule_contacted_lead_first(sid, store.scene_run_minutes(sid), None)
    assert c["evidence_id"] == f"m{mid}" and c["verified"]
    other = store.create_session("y")
    store.claim_scene(other, "coworker_miss", 15)
    store.claim_scene(other, "lead_checkin", 20)            # 팀장이 먼저 물음(건너뛰지 않음)
    assert observe.rule_contacted_lead_first(other, store.scene_run_minutes(other), None) == []


# 규칙 관찰: 진행 상황을 물어 동료 지연을 정해진 시각보다 일찍 알게 됐는가
def test_rule_early_progress_check():
    sid = store.create_session("x")
    mid = store.add_message(sid, "dm", "candidate", "도현님 3번 진행 상황 어떠세요?", 10)
    store.claim_scene(sid, "coworker_miss", 10)
    [c] = observe.rule_early_progress_check(sid, store.scene_run_minutes(sid), None)
    assert c["evidence_id"] == f"m{mid}" and "00:10" in c["behavior"]
    late = store.create_session("y")
    store.claim_scene(late, "coworker_miss", 15)            # 정해진 시각에 털어놓음
    assert observe.rule_early_progress_check(late, store.scene_run_minutes(late), None) == []


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


# 반대 방향 행동 "다 된 것처럼 보고함"은 부정 표현("아직", "못")이 있는 인용을 근거로 쓰지 않는다
def test_verify_drops_negated_quote():
    from app import settings
    spec = [e for e in settings.observation_spec()["element"] if e["name"] == "정직·공정한 보고"][0]
    neg = {c["id"]: c["quote_must_not_contain"] for c in spec["checks"] if c.get("quote_must_not_contain")}
    allowed = {c["id"]: observe.allowed_channels(c) for c in spec["checks"]}
    ev = {"m1": "네, 다 됐습니다!", "m2": "거의 다 됐는데 3번은 아직 못 했어요"}
    channels = {"m1": "lead_dm", "m2": "lead_dm"}
    answers = [CheckAnswer(check_id="overclaimed", answer="yes", evidence_id="m1", quote="네, 다 됐습니다!"),
               CheckAnswer(check_id="overclaimed", answer="yes", evidence_id="m2", quote="거의 다 됐는데 3번은 아직 못 했어요")]
    out = observe.verify(answers, ev, allowed, None, neg, channels)
    assert out[0]["verified"]
    assert out[1]["dropped_reason"] == "인용이 반대 뜻(부정·제외)을 담고 있음"


def test_report_merges_quote_contained_in_another():
    sid = store.create_session("x")
    mid = store.add_message(sid, "lead_dm", "candidate", "그런데 도현님이 급한 업무에 차출돼서 3번은 아직 못 했어요.", 33)
    checks = [{"check_id": "disclosed_incomplete", "answer": "yes", "verified": True, "evidence_id": f"m{mid}",
               "quote": "그런데 도현님이 급한 업무에 차출돼서 3번은 아직 못 했어요.", "point": "알림"},
              {"check_id": "fair_about_coworker", "answer": "yes", "verified": True, "evidence_id": f"m{mid}",
               "quote": "도현님이 급한 업무에 차출돼서", "point": "근거 제시"}]
    obs = report.build_body(sid, [{"element": "정직·공정한 보고", "situation": "s", "happened": True, "checks": checks}])["sections"][0]["observed"]
    assert len(obs) == 1 and obs[0]["points"] == ["알림", "근거 제시"] and obs[0]["quote"].startswith("그런데")


# 회귀: 판별기가 동료 말을 인용해 근거가 버려졌다 → 버려진 체크는 이유를 알려 그 체크만 다시 묻는다
def test_dropped_checks_are_reasked_with_reason(monkeypatch):
    from app import settings
    sid = store.create_session("x")
    co = store.add_message(sid, "dm", "coworker", "업무는 이렇게 나눠 보면 어떨까요? 저는 3번이랑 4번을 할게요.", 2)
    me = store.add_message(sid, "dm", "candidate", "4번은 분석 결과가 있어야 하니 같이 나눠서 해요", 3)
    spec = [e for e in settings.observation_spec()["element"] if e["name"] == "협업·조율"][0]
    first = {"proposed_adjustment": CheckAnswer(check_id="proposed_adjustment", answer="yes", evidence_id=f"m{co}",
                                                quote="업무는 이렇게 나눠 보면 어떨까요?")}
    again = {"proposed_adjustment": CheckAnswer(check_id="proposed_adjustment", answer="yes", evidence_id=f"m{me}",
                                                quote="4번은 분석 결과가 있어야 하니 같이 나눠서 해요")}
    asked = []

    async def fake(model, messages, node, sid_=""):
        asked.append(messages[1].content)
        ids = [line[2:].split(":")[0] for line in messages[1].content.splitlines() if line.startswith("- ")]
        pick = first if len(asked) == 1 else again
        return observe.Judgement(answers=[pick.get(i) or CheckAnswer(check_id=i, answer="no") for i in ids])
    monkeypatch.setattr(observe.llm, "ainvoke", fake)
    r = asyncio.run(observe.observe_element(sid, spec, {"negotiate": 2}, use_cache=False))
    got = {c["check_id"]: c for c in r["checks"]}
    assert got["proposed_adjustment"]["verified"] and got["proposed_adjustment"]["evidence_id"] == f"m{me}"
    assert len(asked) == 2 and "코드 검증에서 버려졌다" in asked[1] and "- blamed" not in asked[1]
    assert "[근거는 동료 1:1·팀 채널의 지원자 글에서만]" in asked[0]          # 채널 제한을 판별기에게도 알린다


# 부정·제외 표현 때문에 뺀 원문은 리포트에서 사라지지 않고 '판단이 필요한 원문'으로 남는다
def _honesty_spec():
    from app import settings
    return [e for e in settings.observation_spec()["element"] if e["name"] == "정직·공정한 보고"][0]


def test_negated_quote_is_kept_for_human_review(monkeypatch):
    sid = store.create_session("x")
    mid = store.add_message(sid, "lead_dm", "candidate", "네 거의 다 됐고 3번만 아직이에요", 33)
    yes = CheckAnswer(check_id="overclaimed", answer="yes", evidence_id=f"m{mid}", quote="거의 다 됐고 3번만 아직이에요")

    async def fake(model, messages, node, sid_=""):   # 물어본 체크마다 답한다. overclaimed는 계속 같은 오판
        if node == "observe.nli":
            return observe.Relation(relation="contradicts")
        ids = [line[2:].split(":")[0] for line in messages[1].content.splitlines() if line.startswith("- ")]
        return observe.Judgement(answers=[yes if i == "overclaimed" else CheckAnswer(check_id=i, answer="no")
                                          for i in ids])
    monkeypatch.setattr(observe.llm, "ainvoke", fake)
    r = asyncio.run(observe.observe_element(sid, _honesty_spec(), {"report_request": 32}, use_cache=False))
    kept = {c["check_id"]: c for c in r["checks"]}["overclaimed"]
    assert kept["answer"] == "no" and r["negated"]
    section = report.build_body(sid, [r])["sections"][0]
    assert not section["observed"] and section["review"] == [
        {"when": "00:33", "quote": "거의 다 됐고 3번만 아직이에요", "message_id": mid}]


# 부정 단어가 걸린 근거는 좁은 함의 검증으로 다시 본다: 뒷받침하면 살린다
def test_negated_quote_rechecked_by_entailment(monkeypatch):
    sid = store.create_session("x")
    mid = store.add_message(sid, "lead_dm", "candidate", "다 됐어요. 아직 손볼 것도 없습니다", 33)

    async def fake(model, messages, node, sid_=""):
        if node == "observe.nli":
            return observe.Relation(relation="supports")
        ids = [line[2:].split(":")[0] for line in messages[1].content.splitlines() if line.startswith("- ")]
        return observe.Judgement(answers=[
            CheckAnswer(check_id=i, answer="yes", evidence_id=f"m{mid}", quote="다 됐어요. 아직 손볼 것도 없습니다")
            if i == "overclaimed" else CheckAnswer(check_id=i, answer="no") for i in ids])
    monkeypatch.setattr(observe.llm, "ainvoke", fake)
    r = asyncio.run(observe.observe_element(sid, _honesty_spec(), {"report_request": 32}, use_cache=False))
    kept = {c["check_id"]: c for c in r["checks"]}["overclaimed"]
    # "아직"이 있어도 함의 검증이 뒷받침으로 판정 → 근거로 살아남음
    assert kept["verified"] and not r["negated"]


# 규칙 관찰: 업무 메모에 적은 소통 계획을 실제로 실행했는가 (LLM이 오판하던 것을 기록으로 확정)
def test_rule_memo_followed_through():
    sid = store.create_session("x")
    store.add_message(sid, "lead_dm", "candidate", "팀장님 안녕하세요", 5)                 # 메모보다 먼저 보낸 건 아님
    store.add_message(sid, "work", "candidate", "팀장님께 지연 상황 보고하고 범위 조정 요청하기", 16)
    assert observe.rule_memo_followed_through(sid, {}, None) == []                        # 아직 안 보냄
    store.add_message(sid, "team", "candidate", "도현님 괜찮으세요?", 17)                  # 동료를 부른 팀 채널 글은 아님
    assert observe.rule_memo_followed_through(sid, {}, None) == []
    mid = store.add_message(sid, "lead_dm", "candidate", "팀장님, 3·4번이 밀렸어요", 18)
    [c] = observe.rule_memo_followed_through(sid, {}, None)
    assert c["evidence_id"] == f"m{mid}" and "팀장에게" in c["behavior"]
    other = store.create_session("y")
    store.add_message(other, "work", "candidate", "1번 지표 분석부터", 6)                  # 소통 계획이 없는 메모
    store.add_message(other, "lead_dm", "candidate", "팀장님 질문 있어요", 7)
    assert observe.rule_memo_followed_through(other, {}, None) == []


# 참고 등급 A~E: LLM이 아니라 코드가 검증된 근거 비율로 계산한다(config [grade])
def test_grade_rules():
    assert report.grade(False, 3, 0, 4) == "판단 보류"   # 상황이 안 일어남
    assert report.grade(True, 0, 0, 4) == "E"            # 상황은 있었는데 바람직한 행동이 안 드러남
    assert report.grade(True, 3, 0, 4) == "A"            # 75%
    assert report.grade(True, 2, 0, 4) == "B"            # 50%
    assert report.grade(True, 1, 0, 3) == "C"            # 33%
    assert report.grade(True, 1, 0, 5) == "D"            # 20%
    assert report.grade(True, 5, 0, 3) == "A"            # 가산점으로 100%를 넘어도 A
    assert report.grade(True, 3, 1, 4) == "B"            # 반대 방향 행동 하나당 한 단계 내림
    assert report.grade(True, 1, 3, 5) == "E"            # E 아래로는 안 내려감


def test_report_section_has_grade_and_basis():
    sid = store.create_session("x")
    m1 = store.add_message(sid, "lead_dm", "candidate", "아직 3번은 못 했어요", 33)
    m2 = store.add_message(sid, "lead_dm", "candidate", "네 다 됐어요", 33.5)
    checks = [{"check_id": "disclosed_incomplete", "answer": "yes", "verified": True, "evidence_id": f"m{m1}",
               "quote": "아직 3번은 못 했어요", "point": "미완료를 밝힘"},
              {"check_id": "overclaimed", "answer": "yes", "verified": True, "evidence_id": f"m{m2}",
               "quote": "네 다 됐어요", "point": "다 된 것처럼 보고함"},
              {"check_id": "fair_about_coworker", "answer": "no", "verified": False, "evidence_id": ""}]
    body = report.build_body(sid, [
        {"element": "정직·공정한 보고", "situation": "s", "happened": True, "checks": checks},
        {"element": "적응성·유연성", "situation": "s", "happened": True, "checks": []},
        {"element": "협업·조율", "situation": "s", "happened": False, "checks": []}])
    secs = {s["element"]: s for s in body["sections"]}
    # 정직: 바람직한 행동 1/2(50% → B) + 반대 방향 1개(한 단계 내림) → C
    assert secs["정직·공정한 보고"]["grade"] == "C"
    assert secs["정직·공정한 보고"]["basis"] == {"positive": ["미완료를 밝힘"], "negative": ["다 된 것처럼 보고함"],
                                               "possible": 2}
    assert secs["적응성·유연성"]["grade"] == "E" and secs["적응성·유연성"]["note"] == report.NOT_OBSERVED
    assert secs["협업·조율"]["grade"] == "판단 보류"
    assert "참고 지표" in body["disclaimer"]


# 실제 세션 회귀(2026-10-20): 동료에게 한 말을 묻는 체크에 판별기가 팀장 1:1을 인용하면 "원문에서 확인되지 않음"으로 버렸다
# → 채널 때문에 버렸다고 정확히 남겨야 다시 물을 때 바로잡는다. 동료에게 일을 줄여 부탁한 말도 근거로 인정한다.
def test_channel_violation_has_its_own_reason_and_delegation_words_are_accepted():
    from app import settings
    spec = [e for e in settings.observation_spec()["element"] if e["name"] == "협업·조율"][0]
    allowed = {c["id"]: observe.allowed_channels(c) for c in spec["checks"]}
    required = {c["id"]: c["quote_must_contain"] for c in spec["checks"] if c.get("quote_must_contain")}
    ev = {"m1": "제가 이어 받아 하겠습니다. 최대한 마감까지 해보겠습니다.",
          "m2": "지금까지 진척된 상황 공유드려요, 초안이라도 좋으니 발표 자료 작성 부탁드립니다",
          "m3": "보고 자료 작성만 도와주시겠어요? 초안 넘겨주시면 제가 다듬고 보고하겠습니다"}
    channels = {"m1": "lead_dm", "m2": "dm", "m3": "dm"}
    answers = [CheckAnswer(check_id="redistributed", answer="yes", evidence_id="m1", quote="제가 이어 받아 하겠습니다."),
               CheckAnswer(check_id="redistributed", answer="yes", evidence_id="m2", quote="초안이라도 좋으니 발표 자료 작성 부탁드립니다"),
               CheckAnswer(check_id="proposed_adjustment", answer="yes", evidence_id="m3", quote="보고 자료 작성만 도와주시겠어요?")]
    out = observe.verify(answers, ev, allowed, required, None, channels)
    assert out[0]["dropped_reason"].startswith(observe.CHANNEL_REASON) and "동료 1:1" in out[0]["dropped_reason"]
    assert out[1]["verified"] and out[2]["verified"]


# 실제 세션 회귀(2026-10-20): "저희 업무 분담부터 해볼까요"로 먼저 계획을 꺼냈는데 계획성에 아무 근거가 없었다
def test_rule_initiated_split():
    sid = store.create_session("x")
    mid = store.add_message(sid, "dm", "candidate", "저희 업무 분담부터 해볼까요", 0.6)
    store.claim_scene(sid, "negotiate", 0.6)            # 지원자가 꺼내 정해진 시각(00:02)보다 일찍 실행됨
    [c] = observe.rule_initiated_split(sid, store.scene_run_minutes(sid), None)
    assert c["evidence_id"] == f"m{mid}" and c["verified"]
    late = store.create_session("y")
    store.claim_scene(late, "negotiate", 2)             # 동료가 정해진 시각에 먼저 꺼냄
    assert observe.rule_initiated_split(late, store.scene_run_minutes(late), None) == []


# 점검 회귀(2026-10-20): 시간 배분 체크의 필수어 "분"이 "분석"에도 걸려, 의존 관계만 말한 문장이 일정 관리로 인정됐다
def test_timebox_words_do_not_match_analysis():
    from app import settings
    spec = [e for e in settings.observation_spec()["element"] if e["name"] == "계획성·꼼꼼함"][0]
    words = next(c["quote_must_contain"] for c in spec["checks"] if c["id"] == "set_timebox")
    assert not any(w in "1번 분석이 끝나야 4번이 가능하니 1번부터" for w in words)
    assert any(w in "저는 1번을 8분까지, 2번을 14분까지 끝내 볼게요" for w in words)
    assert any(w in "4번은 15분쯤 같이 보면 어떨까요?" for w in words)


# 점검 회귀(2026-10-20): 필수어 목록에 없는 말로 같은 행동을 하면 근거가 버려져 등급이 내려갔다(H3 "3번은 절반, 4번은 축소")
# → 필수어가 없어 뺀 근거도 좁은 함의 검증으로 다시 보고, 뒷받침하면 살린다. 하소연처럼 무관하면 계속 뺀다.
def test_missing_required_word_is_rechecked_by_entailment(monkeypatch):
    sid = store.create_session("x")
    ok = store.add_message(sid, "lead_dm", "candidate", "1·2번은 끝났고 3번은 절반, 4번은 축소했습니다.", 33)
    vent = store.add_message(sid, "lead_dm", "candidate", "대상이 바뀌어서 처음부터 다시 해야 할 것 같아요 ㅠㅠ", 33.5)
    spec = {"name": "t", "situation": "s", "scene": "report_request", "window_end_minute": 40, "candidate_keywords": [],
            "checks": [{"id": "disclosed_incomplete", "question": "미완료를 밝혔나?", "point": "미완료를 밝힘",
                        "quote_must_contain": ["못", "아직"]},
                       {"id": "asked_criteria", "question": "기준을 물었나?", "point": "기준 확인",
                        "quote_must_contain": ["?", "기준"]}]}
    answers = {"disclosed_incomplete": CheckAnswer(check_id="disclosed_incomplete", answer="yes", evidence_id=f"m{ok}",
                                                   quote="3번은 절반, 4번은 축소했습니다"),
               "asked_criteria": CheckAnswer(check_id="asked_criteria", answer="yes", evidence_id=f"m{vent}",
                                             quote="처음부터 다시 해야 할 것 같아요")}

    async def fake(model, messages, node, sid_=""):
        if node == "observe.nli":
            return observe.Relation(relation="supports" if "절반" in messages[1].content else "unrelated")
        ids = [line[2:].split(":")[0] for line in messages[1].content.splitlines() if line.startswith("- ")]
        return observe.Judgement(answers=[answers[i] for i in ids])
    monkeypatch.setattr(observe.llm, "ainvoke", fake)
    r = asyncio.run(observe.observe_element(sid, spec, {"report_request": 32}, use_cache=False))
    got = {c["check_id"]: c for c in r["checks"]}
    assert got["disclosed_incomplete"]["verified"]                               # 표현이 달라도 살아남
    assert got["asked_criteria"]["answer"] == "no" and got["asked_criteria"]["nli"] == "unrelated"   # 하소연은 계속 뺌
    assert r["negated"] == []                                                    # 필수어 때문에 뺀 것은 '판단 필요'에 안 남김
