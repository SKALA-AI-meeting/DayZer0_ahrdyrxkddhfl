"""엔진의 장면 규칙: 진행 상황을 물으면 일찍 털어놓기, 상담 뒤 대상 변경, 작업 계속하기의 시계 넘기기.
LLM은 부르지 않는다(Graph 실행을 가짜로 바꿔 어떤 장면이 언제 실행되는지만 본다)."""
import asyncio

import pytest

from app import engine, graph, store


@pytest.fixture
def turns(monkeypatch):
    seen = []

    async def fake_run_turn(sid, minute, trigger):
        seen.append((round(minute, 2), trigger.get("scene_id") or trigger["channel"]))
    monkeypatch.setattr(graph, "run_turn", fake_run_turn)
    return seen


def _session(minute: float, ran=("kickoff", "kickoff_greeting", "negotiate"), phase="work") -> str:
    sid = store.create_session("x")
    for s in ran:
        store.claim_scene(sid, s, 0)
    store.update_session(sid, phase=phase, sim_seconds=minute * 60)
    return sid


def test_progress_question_reveals_coworker_miss_early(turns):
    sid = _session(8)
    asyncio.run(engine.send_message(sid, "dm", "그럼 그렇게 진행할게요!", now=1000.0))   # 진행 상황 질문 아님
    assert "coworker_miss" not in store.run_scene_ids(sid)
    asyncio.run(engine.send_message(sid, "dm", "도현님 3번 진행 상황 어떠세요?", now=1001.0))
    assert turns[-1][1] == "coworker_miss" and store.scene_run_minutes(sid)["coworker_miss"] < 15
    asyncio.run(engine.send_message(sid, "dm", "진행 상황 다시 알려 주세요", now=1002.0))
    assert turns[-1][1] == "dm"                                 # 이미 털어놓았으면 일반 답장


def test_planning_a_checkpoint_is_not_a_progress_question(turns):
    # 실제 세션 회귀: 분담 단계의 "10분쯤에 중간 점검도 한 번 해요"(…어떨까요?)를 진행 상황 질문으로 잡아 00:03에 털어놓았다
    sid = _session(3, phase="negotiate")
    asyncio.run(engine.send_message(sid, "dm", "4번은 같이 하면 어떨까요? 10분쯤에 중간 점검도 한 번 해요.", now=1000.0))
    asyncio.run(engine.send_message(sid, "dm", "진행 상황은 10분마다 공유해요", now=1001.0))
    assert "coworker_miss" not in store.run_scene_ids(sid)


def test_progress_question_during_negotiation_reveals(turns):
    sid = _session(6, phase="negotiate")
    asyncio.run(engine.send_message(sid, "dm", "3번은 진행 상황 어떠세요?", now=1000.0))
    assert turns[-1][1] == "coworker_miss"       # Graph가 지금 분담안으로 확정한 뒤 털어놓는다(finalize_split)


def test_pivot_runs_after_consult_turns_and_only_once(turns):
    sid = _session(16, ran=("kickoff", "kickoff_greeting", "negotiate", "coworker_miss"))
    asyncio.run(engine.send_message(sid, "lead_dm", "팀장님, 도현님 업무가 밀렸어요", now=1000.0))
    assert "pivot" not in store.run_scene_ids(sid)              # 상담 1번째: 아직
    asyncio.run(engine.send_message(sid, "lead_dm", "3번은 빼도 될까요?", now=1001.0))
    assert [t[1] for t in turns[-2:]] == ["lead_dm", "pivot"]   # 팀장 답 직후 대상 변경("상담 마무리하면서")
    asyncio.run(engine.send_message(sid, "lead_dm", "네 알겠습니다", now=1002.0))
    assert [t[1] for t in turns].count("pivot") == 1


def test_lead_dm_before_coworker_miss_does_not_count_as_consult(turns):
    sid = _session(5)
    for i in range(3):
        asyncio.run(engine.send_message(sid, "lead_dm", f"질문 {i}", now=1000.0 + i))
    assert "pivot" not in store.run_scene_ids(sid)


def test_lead_checkin_skipped_when_candidate_wrote_first(turns):
    sid = _session(19.9, ran=("kickoff", "kickoff_greeting", "negotiate", "coworker_miss"))
    store.add_message(sid, "lead_dm", "candidate", "팀장님 상의드릴 게 있어요", 17)
    store.update_session(sid, last_seen=1000.0)
    asyncio.run(engine.tick(sid, 1000.1 + 6))    # 6초 × 배속 2 = 12초 → 20분 넘김
    runs = store.run_scene_ids(sid)
    assert "lead_checkin" in runs and "skipped:lead_checkin" in runs
    assert "lead_checkin" not in [t[1] for t in turns]


def test_skip_target_stops_at_next_scene_and_session_end():
    sid = _session(3)
    assert engine.skip_target(sid, 3) == 8                       # skip_minutes = 5
    assert engine.skip_target(sid, 12) == 15                     # 다음 장면(00:15) 앞에서 멈춤
    for s in ("coworker_miss", "lead_checkin", "pivot", "status_narration", "report_request", "deadline_report"):
        store.claim_scene(sid, s, 0)
    assert engine.skip_target(sid, 37) == 40                     # 세션 끝을 넘지 않음


def test_work_step_classify_failure_falls_back_to_other(monkeypatch, turns):
    from app import work

    async def broken(*_a, **_k):
        raise RuntimeError("LLM 장애")
    from app import memo_agent
    monkeypatch.setattr(memo_agent.llm, "ainvoke", broken)
    sid = _session(3)
    store.update_session(sid, status="in_progress", last_seen=1000.0)
    asyncio.run(engine.work_step(sid, "음 일단 해볼게요", now=1000.0))
    [memo] = store.work_memos(sid).values()
    assert memo == {"category": "other", "part": "", "advanced": False}
    assert store.list_messages(sid, channels=("work",))[-1]["content"] == "[기타] 어떤 업무를 할지 분명하지 않아 업무 진척은 없었다."


def _step(sid, part, minute, advanced=True, category="own"):
    """업무 메모 한 번을 기록에 넣는다(LLM 없이 진척 계산만 보기 위해)."""
    mid = store.add_message(sid, "work", "candidate", f"{part}번 할게요", minute)
    store.add_work_memo(sid, mid, category, part, advanced)


def test_progress_counts_only_advanced_steps_and_caps_at_units():
    from app import work
    sid = _session(3)
    _step(sid, "2", 3)
    _step(sid, "2", 8)
    _step(sid, "2", 12, advanced=False)        # 이미 끝난 업무에 쓴 메모는 오르지 않음
    _step(sid, "5", 13)
    assert work.progress(sid)["2"] == 2 and work.progress(sid)["5"] == 1 and work.progress(sid)["1"] == 0
    assert not work.can_advance(sid, "2") and work.can_advance(sid, "5")
    assert "2번 사용자 인터뷰 정리 2/2칸(완료)" in work.summary(sid) and "5번 대표 보고 자료 작성 1/3칸" in work.summary(sid)


def test_pivot_effect_loses_progress_and_caps_part_1():
    from app import work
    sid = _session(3)
    _step(sid, "1", 3)
    _step(sid, "1", 8)                          # 대상 변경 전 1번 완료(2/2)
    assert work.progress(sid)["1"] == 2
    store.claim_scene(sid, "pivot", 22)         # 대상 변경: 1칸 무효, 이후 최대 1칸
    assert work.progress(sid)["1"] == 1
    assert not work.can_advance(sid, "1") and "마감 뒤에 나와" in work.cap_reason(sid, "1")
    assert work.narration(sid, "own", "1", False) == (
        "1번 지표 분석: 새 대상 데이터가 마감 뒤에 나와 이번 마감 안에는 더 진행할 수 없었다. 바뀐 대상 기준으로 맞추는 중이다.")
    work.announce_effects(sid, 22)
    work.announce_effects(sid, 23)              # 알림은 한 번만
    notices = [m["content"] for m in store.list_messages(sid, channels=("work",)) if m["sender"] == "narrator"]
    assert notices == ["[업무 영향] 대상이 바뀌어 1번 지표 분석의 진척 일부가 무효가 됐다."]


def test_resolve_memo_without_part_number():
    from app import work
    sid = _session(3)                           # 기본 분담: 지원자 1·2·5, 동료 3·4
    assert work.resolve(sid, "own", "") == ("own", "1")          # 번호 없이 '내 거' → 안 끝난 첫 내 업무
    _step(sid, "1", 3)
    _step(sid, "1", 4)
    assert work.resolve(sid, "own", "") == ("own", "2")
    assert work.resolve(sid, "cover", "") == ("cover", "3")      # 동료 업무 메우기 → 안 끝난 첫 동료 업무
    assert work.resolve(sid, "own", "4") == ("cover", "4")       # 번호가 동료 업무면 범주는 담당자로 정함
    assert work.resolve(sid, "replan", "") == ("replan", "")     # 계획은 업무를 진행하지 않음


def test_work_step_advances_one_part_and_updates_card(monkeypatch, turns):
    from app import work
    calls = []

    async def fake_classify(sid, memo, pack=None):
        return "own", "2"                       # "2번하고 5번하고 다 했음"이어도 업무 하나만

    async def fake_business(tool, args):
        calls.append((tool, args["part"], args["status"]))
        return "ok"
    from app import memo_agent
    monkeypatch.setattr(memo_agent, "classify", fake_classify)
    monkeypatch.setattr(engine, "call_business", fake_business)
    sid = _session(3)
    store.update_session(sid, status="in_progress", last_seen=1000.0)
    asyncio.run(engine.work_step(sid, "2번 하고 5번 하고 다 했음", now=1000.0))
    assert work.progress(sid)["2"] == 1 and work.progress(sid)["5"] == 0
    assert calls == [("update_task_status", "2", "doing")]
    assert store.list_messages(sid, channels=("work",))[-1]["content"].startswith("[내 업무 진행] 2번 사용자 인터뷰 정리: 한 칸")


def test_work_narration_follows_stage():
    from app import work
    sid = _session(3)
    assert work.stage(sid) == "before"
    store.claim_scene(sid, "coworker_miss", 15)
    assert work.stage(sid) == "after_miss"
    _step(sid, "2", 16)
    assert work.narration(sid, "own", "2", True).endswith("동료가 맡았던 업무는 그대로 비어 있다.")
    store.claim_scene(sid, "pivot", 22)
    assert work.stage(sid) == "after_pivot"
    assert work.narration(sid, "unknown", "", False) == "어떤 업무를 할지 분명하지 않아 업무 진척은 없었다."
