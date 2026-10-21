"""Graph 분기 테스트. LLM과 MCP 호출은 가짜로 바꿔 흐름만 확인한다."""
import asyncio

from app import graph, persona, store
from conftest import inprocess_business


def _fake(monkeypatch, seen=None):
    async def fake_speak(role, sid, minute, task, channels=None, use_tools=True, **_kwargs):
        if seen is not None:
            seen.append((role, task))
        return f"{role}:{task[:10]}"

    async def fake_business(tool, args):
        return await inprocess_business(tool, args)
    monkeypatch.setattr(persona, "speak", fake_speak)
    monkeypatch.setattr(graph, "call_business", fake_business)


def test_unfinished_split_is_fixed_before_first_event(monkeypatch):
    _fake(monkeypatch)
    sid = store.create_session("x")
    store.update_session(sid, phase="negotiate",
                         assignment={"1": "candidate", "2": "candidate", "3": "both",
                                     "4": "coworker", "5": "candidate"})
    asyncio.run(graph.run_turn(sid, 15, {"kind": "scene", "scene_id": "coworker_miss"}))
    assert store.get_session(sid)["phase"] == "work"
    assert {t["part"]: t["owner"] for t in store.list_tasks(sid)}["3"] == "both"
    msgs = [m["content"] for m in store.list_messages(sid)]
    assert msgs[0].startswith("coworker:분담 얘기가")      # 확정 안내가 먼저
    assert len(msgs) == 2                                   # 그다음 15분 이벤트 그대로


def test_coworker_cards_doing_after_split(monkeypatch):
    async def fake_speak(*_a, **_k):
        return "msg"

    async def fake_business(tool, args):
        return await inprocess_business(tool, args)
    monkeypatch.setattr(persona, "speak", fake_speak)
    monkeypatch.setattr(graph, "call_business", fake_business)
    sid = store.create_session("x")
    store.update_session(sid, phase="negotiate")
    asyncio.run(graph.run_turn(sid, 15, {"kind": "scene", "scene_id": "coworker_miss"}))   # 분업 확정
    status = {t["part"]: t["status"] for t in store.list_tasks(sid)}
    assert status["3"] == "doing" and status["4"] == "doing" and status["1"] == "todo"


def test_no_finalize_when_split_already_done(monkeypatch):
    _fake(monkeypatch)
    sid = store.create_session("x")
    store.update_session(sid, phase="work")
    asyncio.run(graph.run_turn(sid, 15, {"kind": "scene", "scene_id": "coworker_miss"}))
    assert len(store.list_messages(sid)) == 1


def test_non_event_scene_does_not_finalize(monkeypatch):
    _fake(monkeypatch)
    sid = store.create_session("x")
    store.update_session(sid, phase="negotiate")
    asyncio.run(graph.run_turn(sid, 2, {"kind": "scene", "scene_id": "negotiate"}))
    assert store.get_session(sid)["phase"] == "negotiate"


def test_narrator_scene_posts_fixed_text_to_work_log(monkeypatch):
    _fake(monkeypatch)
    sid = store.create_session("x")
    asyncio.run(graph.run_turn(sid, 31, {"kind": "scene", "scene_id": "status_narration"}))
    [m] = store.list_messages(sid)
    assert (m["channel"], m["sender"]) == ("work", "narrator") and "끝내기는 어렵다" in m["content"]


def test_follow_up_is_added_once_on_first_reply_in_that_channel(monkeypatch):
    seen = []
    _fake(monkeypatch, seen)
    sid = store.create_session("x")
    store.update_session(sid, phase="work")
    store.claim_scene(sid, "coworker_miss", 15)
    turn = {"kind": "candidate", "channel": "dm", "text": "괜찮아요, 어떻게 할까요?"}
    asyncio.run(graph.run_turn(sid, 16, turn))
    asyncio.run(graph.run_turn(sid, 17, turn))
    assert "다음 반응을 한 번 담아라" in seen[0][1] and "의견을 한 번 묻는다" in seen[0][1]
    assert "다음 반응" not in seen[1][1]                                      # 두 번째 답에는 없음
    asyncio.run(graph.run_turn(sid, 18, {"kind": "candidate", "channel": "lead_dm", "text": "팀장님"}))
    assert seen[2][0] == "lead" and "다음 반응" not in seen[2][1]             # 팀장 1:1엔 동료 반응 없음


def test_newer_follow_up_retires_older_one(monkeypatch):
    seen = []
    _fake(monkeypatch, seen)
    sid = store.create_session("x")
    store.update_session(sid, phase="work")
    store.claim_scene(sid, "pivot", 22)
    store.claim_scene(sid, "report_request", 32)    # 대상 변경 뒤 답이 없다가 보고 요청이 옴
    turn = {"kind": "candidate", "channel": "lead_dm", "text": "아직 다 못 했어요"}
    asyncio.run(graph.run_turn(sid, 33, turn))
    asyncio.run(graph.run_turn(sid, 34, turn))
    assert "이도현 쪽 업무" in seen[0][1]          # 가장 최근 장면(보고 요청)의 반응
    assert "다음 반응" not in seen[1][1]           # 지나간 대상 변경 반응이 뒤늦게 나오지 않음


def test_team_channel_responder_by_name():
    assert persona.responder("team", "도현님 이거 같이 봐요") == "coworker"
    assert persona.responder("team", "팀장님 보고드립니다") == "lead"
    assert persona.responder("dm", "팀장님 얘기") == "coworker"
    assert persona.responder("lead_dm", "도현님이 차출됐어요") == "lead"
