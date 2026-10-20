"""Harness: Context — 예산을 넘는 앞부분은 요약하고, 요약은 캐시한다."""
import asyncio

from langchain_core.messages import AIMessage

from app import llm, persona, store


def _fill(sid, n):
    for i in range(n):
        store.add_message(sid, "dm", "candidate", f"메시지 {i} " + "가" * 200, i)


def test_old_part_is_summarized_and_cached(monkeypatch):
    calls = []

    async def fake(model, payload, node, sid=""):
        calls.append(node)
        return AIMessage("- 지원자가 1·2번을 맡기로 함")
    monkeypatch.setattr(llm, "ainvoke", fake)
    monkeypatch.setitem(persona.config()["context"], "history_token_budget", 500)
    sid = store.create_session("x")
    _fill(sid, 10)
    out = asyncio.run(persona.context(sid, ("dm",)))
    assert out.startswith("(이전 대화") and "요약)" in out and "1·2번을 맡기로" in out
    assert "메시지 9" in out                                   # 최근 메시지는 원문 그대로
    asyncio.run(persona.context(sid, ("dm",)))
    assert calls == ["context.summarize"]                      # 같은 부분은 다시 요약하지 않음(캐시)


def test_summary_with_judgement_is_dropped(monkeypatch):
    async def fake(*_a, **_k):
        return AIMessage("- 지원자는 합격권으로 보임")
    monkeypatch.setattr(llm, "ainvoke", fake)
    monkeypatch.setitem(persona.config()["context"], "history_token_budget", 500)
    sid = store.create_session("x")
    _fill(sid, 10)
    out = asyncio.run(persona.context(sid, ("dm",)))
    assert "생략)" in out and "합격" not in out


def test_short_conversation_is_not_summarized():
    sid = store.create_session("x")
    store.add_message(sid, "dm", "candidate", "안녕하세요", 1)
    assert asyncio.run(persona.context(sid, ("dm",))).endswith("안녕하세요")
