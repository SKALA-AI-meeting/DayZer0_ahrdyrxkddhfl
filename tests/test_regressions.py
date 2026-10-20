"""중간점검(레포 현황 채팅)에서 찾은 잠재적 문제 5개 재현 테스트. 수정 전에는 실패해야 한다."""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from app import doc_graph, engine, graph, report, speak_graph, store


# 1. 검색 결과에 "아직 공유되지 않음" 안내가 섞이면 같이 온 정상 문서까지 "안 읽음"이 된다
def test_notice_in_result_does_not_hide_other_docs(monkeypatch):
    result = json.dumps([
        {"doc_id": "interviews", "title": "사용자 인터뷰 요약", "notice": "'사용자 인터뷰 요약' 문서는 아직 공유되지 않았습니다."},
        {"doc_id": "metrics", "title": "지표 현황", "excerpt": "재참여율 22%"}], ensure_ascii=False)

    class FakeTool:
        name = "search_company_docs"
        async def ainvoke(self, _args):
            return result

    class FakeCtx:
        async def __aenter__(self):
            return [FakeTool()]
        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(speak_graph, "doc_tools", lambda *a, **k: FakeCtx())
    state = {"messages": [AIMessage("", tool_calls=[{"name": "search_company_docs", "args": {"query": "x"}, "id": "1"}])],
             "tool_calls": 0, "streak": 0, "last_tool": "", "read_titles": [], "called_tools": [],
             "minute": 30, "company": "", "plan_session": ""}
    out = asyncio.run(speak_graph.tools_node(state))
    assert "지표 현황" in out["read_titles"]
    assert "사용자 인터뷰 요약" not in out["read_titles"]


# 2. 제목에 "Error"가 들어가면 등록됐는데도 상태가 rejected로 저장된다
def test_doc_title_with_error_word_is_registered():
    async def run():
        r = await doc_graph.request("A사", "error-guide", "Error 대응 가이드", "장애가 나면 먼저 공유한다")
        return await doc_graph.decide(r["request_id"], "A사", True)
    assert asyncio.run(run()) == "registered"


# 3. MCP 변경 도구가 오류를 돌려줘도 성공처럼 진행된다(예외를 삼킴)
def test_mcp_tool_error_is_raised(monkeypatch):
    async def fake_business(tool, args):
        from app import mcp_client
        return mcp_client.check_result("Error executing tool assign_tasks: 잘못된 분업 값")
    monkeypatch.setattr(graph, "call_business", fake_business)
    with pytest.raises(RuntimeError, match="잘못된 분업"):
        asyncio.run(fake_business("assign_tasks", {}))


# 4. 장면 생성 중 LLM이 계속 실패하면 세션이 멈춘다 → 정해진 대체 대사로라도 진행해야 한다(F-23)
def test_scene_uses_fallback_when_llm_keeps_failing(monkeypatch):
    async def broken(*_a, **_k):
        raise RuntimeError("LLM 장애")
    monkeypatch.setattr(graph.persona, "speak", broken)
    sid = store.create_session("x")
    for t in (1000.0, 1001.0, 1002.0):
        asyncio.run(engine.tick(sid, t))
    msgs = store.list_messages(sid)
    assert any("22%" in m["content"] for m in msgs)   # 킥오프는 대체 대사로 나와야 한다


# 5. 리포트 생성 중 서버가 재시작되면 "생성 중"에 영원히 머문다 → 재시작 때 다시 시도해야 한다(F-34)
def test_stale_generating_report_is_retried(monkeypatch):
    sid = store.create_session("x")
    store.update_session(sid, status="completed")
    store.claim_report(sid)                       # 재시작 전 생성 중이던 상태

    async def ok(_sid):
        return {"sections": []}
    monkeypatch.setattr(report, "generate", ok)
    asyncio.run(report.resume_stale())
    assert store.get_report(sid)["state"] == "ready"


# 4-2. 지원자 메시지 답장 생성이 끝내 실패하면 500 대신 정해진 문장으로 답한다(PRD A.4)
def test_reply_failure_posts_fixed_message(monkeypatch):
    async def broken(*_a, **_k):
        raise RuntimeError("LLM 장애")
    monkeypatch.setattr(engine.graph, "run_turn", broken)
    sid = store.create_session("x")
    store.update_session(sid, phase="work")
    asyncio.run(engine.send_message(sid, "dm", "1번 어디까지 쓰셨어요?", now=1000.0))
    last = store.list_messages(sid)[-1]
    assert last["sender"] == "coworker" and "다시 보내" in last["content"]


# 확인필요(나): 팀장은 동료 1:1을, 동료는 팀장 1:1을 맥락으로 보지 않는다. 업무 메모는 둘 다 못 본다
def test_lead_and_coworker_see_only_their_channels(monkeypatch):
    from app import persona
    seen = {}

    async def fake_speak(**kw):
        seen["human"] = kw["human"]
        return {"text": "ok", "verdicts": [], "read_titles": []}
    monkeypatch.setattr(persona.speak_graph, "speak", fake_speak)
    sid = store.create_session("x")
    store.add_message(sid, "dm", "candidate", "동료에게만 한 말", 5)
    store.add_message(sid, "lead_dm", "candidate", "팀장에게만 한 말", 5)
    store.add_message(sid, "work", "candidate", "혼자 쓴 업무 메모", 5)
    store.add_message(sid, "team", "candidate", "팀 채널 메시지", 6)
    asyncio.run(persona.speak("lead", sid, 7, "답하라", ("team", "dm", "lead_dm", "work", "system")))
    assert "팀 채널 메시지" in seen["human"] and "팀장에게만" in seen["human"]
    assert "동료에게만" not in seen["human"] and "업무 메모" not in seen["human"]
    asyncio.run(persona.speak("coworker", sid, 7, "답하라"))
    assert "동료에게만" in seen["human"] and "팀장에게만" not in seen["human"] and "업무 메모" not in seen["human"]


# 동료의 숨은 사정(다른 업무에 차출됨)은 털어놓는 장면 전후로 지시문이 바뀐다(context_before / context_after)
def test_coworker_hidden_fact_changes_after_reveal(monkeypatch):
    from app import persona
    seen = {}

    async def fake_speak(**kw):
        seen["system"] = kw["system"]
        return {"text": "ok", "verdicts": [], "read_titles": []}
    monkeypatch.setattr(persona.speak_graph, "speak", fake_speak)
    sid = store.create_session("x")
    asyncio.run(persona.speak("coworker", sid, 5, "답하라"))
    assert "아직 지원자에게 말하지 않았다" in seen["system"]
    store.claim_scene(sid, "coworker_miss", 15)
    asyncio.run(persona.speak("coworker", sid, 16, "답하라"))
    assert "이미 지원자에게 털어놓았다" in seen["system"] and "아직 지원자에게 말하지" not in seen["system"]
    asyncio.run(persona.speak("lead", sid, 16, "답하라"))
    assert "차출" not in seen["system"]                       # 팀장은 동료 사정을 모른다(지원자가 알려야 함)
    store.claim_scene(sid, "pivot", 22)
    asyncio.run(persona.speak("lead", sid, 23, "답하라"))
    assert "3개월 이상" in seen["system"]                     # 대상 변경 뒤에만 '오래'의 기준을 안다


# 세션은 끝났는데(완료) 리포트 작업이 시작되기 전에 서버가 멈춘 경우도 재시작 때 만든다(F-32)
def test_completed_session_without_report_is_generated_on_startup(monkeypatch):
    sid = store.create_session("x")
    store.update_session(sid, status="completed")

    async def ok(_sid):
        return {"sections": []}
    monkeypatch.setattr(report, "generate", ok)
    asyncio.run(report.resume_stale())
    assert store.get_report(sid)["state"] == "ready"
    assert store.get_session(sid)["status"] == "report_ready"


# 회귀: 지원자가 먼저 분담을 물어 동료가 분담안을 말했는데, 분업 장면이 같은 분담안을 또 말했다
def test_negotiate_scene_does_not_repeat_split(monkeypatch):
    from app import graph, persona
    seen = {}

    async def fake_speak(role, sid, minute, task, channels=None, **kw):
        seen.update(task=task, must_include=kw.get("must_include"))
        return "ok"
    monkeypatch.setattr(persona, "speak", fake_speak)
    sid = store.create_session("x")
    state = {"session_id": sid, "minute": 2, "scenario": "moarun", "assignment": {},
             "trigger": {"kind": "scene", "scene_id": "negotiate"}}
    asyncio.run(graph.run_scene(state))
    assert seen["must_include"]                                       # 처음이면 분담안을 그대로 전달
    store.add_message(sid, "dm", "coworker", "1번·2번, 5번은 지원자님이, 3번·4번은 제가 맡을게요", 1)
    asyncio.run(graph.run_scene(state))
    assert seen["must_include"] is None and "다시 나열하지 말고" in seen["task"]


# 회귀: 인터뷰 요약이 01:05에 공개돼도 알림이 없어 팀장이 "늦게 공유될 예정"이라고 답했다
def test_delayed_doc_release_is_announced_once(late_interviews):
    from app import engine
    sid = store.create_session("x")
    engine.announce_shared_docs(sid, 60)
    assert not [m for m in store.list_messages(sid) if m["sender"] == "system"]
    engine.announce_shared_docs(sid, 66)
    engine.announce_shared_docs(sid, 67)
    notices = [m["content"] for m in store.list_messages(sid) if m["sender"] == "system"]
    assert notices == ["'사용자 인터뷰 요약' 문서가 공유됐어요. 이제 회사 문서에서 열어 볼 수 있습니다."]


# 회귀: 지원자가 00:10 전에 분담을 꺼내 동의했는데 분업 단계 전이라 동의로 처리되지 않았고,
# 분업 장면과 첫 이벤트 전 확정 메시지로 분담안이 세 번 나갔다 → 그때 분업 장면을 시작한다
def test_candidate_can_start_split_before_scene(monkeypatch):
    from app import engine, graph
    triggers = []

    async def fake_run_turn(sid, minute, trigger):
        triggers.append(trigger)
        if trigger.get("scene_id") == "negotiate":
            store.update_session(sid, phase="negotiate")
    monkeypatch.setattr(graph, "run_turn", fake_run_turn)
    sid = store.create_session("x")
    for scene in ("kickoff", "kickoff_greeting"):
        store.claim_scene(sid, scene, 0)
    asyncio.run(engine.send_message(sid, "dm", "안녕하세요, 업무 배분부터 시작할까요?", now=1000.0))
    assert triggers[-1] == {"kind": "scene", "scene_id": "negotiate"} and "negotiate" in store.run_scene_ids(sid)
    asyncio.run(engine.send_message(sid, "dm", "네 좋습니다!", now=1001.0))
    assert triggers[-1]["kind"] == "candidate"            # 동의는 분업 단계 답으로 처리(장면 반복 없음)
    other = store.create_session("y")
    asyncio.run(engine.send_message(other, "dm", "안녕하세요 잘 부탁드려요", now=1000.0))
    assert "negotiate" not in store.run_scene_ids(other)  # 분담 이야기가 아니면 그대로


# Harness 표 6-2: LLM 호출에 세션 메타데이터를 붙이고, 리포트가 실패하면 trace 찾는 방법을 이력에 남긴다
def test_llm_calls_carry_session_metadata_and_failed_report_keeps_trace_hint(monkeypatch):
    from app import llm, report
    seen = {}

    class FakeModel:
        async def ainvoke(self, payload, config=None):
            seen.update(config or {})
            return type("R", (), {"content": "ok", "usage_metadata": {}})()
    asyncio.run(llm.ainvoke(FakeModel(), [], "persona.lead", "sid-1"))
    assert seen["metadata"] == {"session_id": "sid-1", "node": "persona.lead"} and seen["run_name"] == "persona.lead"

    sid = store.create_session("x")
    store.update_session(sid, status="completed")

    async def broken(_sid):
        raise RuntimeError("판별 실패")
    monkeypatch.setattr(report, "generate", broken)
    assert asyncio.run(report.generate_with_retry(sid)) == "failed"
    err = store.get_report(sid)["error"]
    assert "RuntimeError: 판별 실패" in err and f"session={sid}" in err and "trace:" in err
