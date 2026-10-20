"""Graph 분기 테스트. LLM과 MCP 호출은 가짜로 바꿔 흐름만 확인한다."""
import asyncio

from app import graph, persona, store


def _fake(monkeypatch):
    async def fake_speak(role, sid, minute, task, channels, use_tools=True, **_kwargs):
        return f"{role}:{task[:10]}"

    async def fake_business(tool, args):
        if tool == "assign_tasks":
            store.set_task_owners(args["session_id"], args["assignment"])
        return "ok"
    monkeypatch.setattr(persona, "speak", fake_speak)
    monkeypatch.setattr(graph, "call_business", fake_business)


def test_unfinished_split_is_fixed_before_first_event(monkeypatch):
    _fake(monkeypatch)
    sid = store.create_session("x")
    store.update_session(sid, phase="negotiate",
                         assignment={"1": "candidate", "2": "coworker", "3": "both",
                                     "4": "candidate", "5": "coworker"})
    asyncio.run(graph.run_turn(sid, 30, {"kind": "scene", "scene_id": "coop_help"}))
    assert store.get_session(sid)["phase"] == "work"
    assert {t["part"]: t["owner"] for t in store.list_tasks(sid)}["4"] == "candidate"
    msgs = [m["content"] for m in store.list_messages(sid)]
    assert msgs[0].startswith("coworker:분담 얘기가")      # 확정 안내가 먼저
    assert len(msgs) == 2                                   # 그다음 30분 이벤트 그대로


def test_no_finalize_when_split_already_done(monkeypatch):
    _fake(monkeypatch)
    sid = store.create_session("x")
    store.update_session(sid, phase="work")
    asyncio.run(graph.run_turn(sid, 30, {"kind": "scene", "scene_id": "coop_help"}))
    assert len(store.list_messages(sid)) == 1


def test_non_event_scene_does_not_finalize(monkeypatch):
    _fake(monkeypatch)
    sid = store.create_session("x")
    store.update_session(sid, phase="negotiate")
    asyncio.run(graph.run_turn(sid, 10, {"kind": "scene", "scene_id": "negotiate"}))
    assert store.get_session(sid)["phase"] == "negotiate"
