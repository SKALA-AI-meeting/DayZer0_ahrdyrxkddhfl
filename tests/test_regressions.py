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
    assert any("두 번째 모임" in m["content"] for m in msgs)   # 킥오프는 대체 대사로 나와야 한다


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


# 확인필요(나): 팀장은 1:1 대화를 맥락으로 보지 않는다
def test_lead_does_not_see_dm(monkeypatch):
    from app import persona
    seen = {}

    async def fake_speak(**kw):
        seen["human"] = kw["human"]
        return {"text": "ok", "verdicts": [], "read_titles": []}
    monkeypatch.setattr(persona.speak_graph, "speak", fake_speak)
    sid = store.create_session("x")
    store.add_message(sid, "dm", "candidate", "비밀 1:1 메시지", 5)
    store.add_message(sid, "team", "candidate", "팀 채널 메시지", 6)
    asyncio.run(persona.speak("lead", sid, 7, "답하라", ("team", "dm", "system")))
    assert "팀 채널 메시지" in seen["human"] and "비밀 1:1 메시지" not in seen["human"]


# 회귀: 지원자가 동료 아이디어에 동의하면 동료가 그걸 지원자 의견처럼 칭찬했다 → 동료 자신의 공유분을 맥락에 밝힌다
def test_coworker_knows_its_own_shared_work(monkeypatch):
    from app import persona
    seen = {}

    async def fake_speak(**kw):
        seen["system"] = kw["system"]
        return {"text": "ok", "verdicts": [], "read_titles": []}
    monkeypatch.setattr(persona.speak_graph, "speak", fake_speak)
    sid = store.create_session("x")
    store.upsert_part(sid, "coworker:3", "7일 출석 보상: 확률형 전설 상자", 90)
    asyncio.run(persona.speak("coworker", sid, 115, "답하라", ("team", "dm", "system")))
    assert "네가 공유한 작업" in seen["system"] and "확률형 전설 상자" in seen["system"]
    asyncio.run(persona.speak("lead", sid, 115, "답하라", ("team", "dm", "system")))
    assert "네가 공유한 작업" not in seen["system"]


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


# 회귀: "다시 검토해야 할 것 같아요" 같은 하소연을 검토 요청으로 잡아 동료가 말에 답하지 않고 리뷰만 했다
def test_review_request_needs_request_form():
    from app import persona
    assert not persona.is_review_request("대상 유저가 바뀌어서 처음부터 다시 검토해야 할 것 같습니다 ㅠㅠ")
    assert not persona.is_review_request("피드백 반영해서 고쳐 볼게요")
    assert not persona.is_review_request("2번 보완해서 다시 올릴게요")
    assert persona.is_review_request("기획안 검토해 주실 수 있나요?")
    assert persona.is_review_request("한번 봐주세요")
    assert persona.is_review_request("리뷰 부탁드려요")
