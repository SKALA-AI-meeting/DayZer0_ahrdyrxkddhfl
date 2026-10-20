"""도구를 쓰는 LLM 응답 루프 (Harness: Agent Loop, Tools).

모델이 도구 호출을 제안하면 코드가 실행하고 결과를 돌려준다. 한도는 config [agent_loop]:
- 응답 한 번에 도구 호출 최대 max_tool_calls_per_reply회
- 같은 도구 연속 호출 최대 same_tool_max회
- 루프 최대 max_turns턴, 전체 task_timeout_seconds초
도구 결과는 tool_result_max_chars로 자르고, 도구 오류는 ToolMessage로 모델에게 돌려준다.
"""
import asyncio

from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from app import llm
from app.settings import config


def tool_text(result) -> str:
    """MCP 도구 결과(content block 목록 또는 문자열)를 텍스트로 바꾼다."""
    if isinstance(result, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in result)
    return str(result)


async def _loop(system: str, messages: list[BaseMessage], tools: list[BaseTool],
                temperature: float, node: str, session_id: str) -> tuple[str, list[str]]:
    limits = config()["agent_loop"]
    max_chars = config()["context"]["tool_result_max_chars"]
    model = llm.chat_model(temperature)
    by_name = {t.name: t for t in tools}
    bound = model.bind_tools(tools) if tools else model
    history: list[BaseMessage] = [SystemMessage(system), *messages]
    calls, last_tool, streak, sources = 0, None, 0, []

    for _ in range(limits["max_turns"]):
        ai = await llm.ainvoke(bound, history, node, session_id)
        history.append(ai)
        if not ai.tool_calls:
            return str(ai.content), sources
        for tc in ai.tool_calls:
            calls += 1
            streak = streak + 1 if tc["name"] == last_tool else 1
            last_tool = tc["name"]
            if calls > limits["max_tool_calls_per_reply"] or streak > limits["same_tool_max"]:
                text = "도구 호출 한도를 넘었습니다. 지금까지 찾은 정보로 답하세요."
            elif tc["name"] not in by_name:
                text = f"없는 도구입니다: {tc['name']}"
            else:
                try:
                    text = tool_text(await by_name[tc["name"]].ainvoke(tc["args"]))
                except Exception as exc:   # 도구 오류는 모델에게 알려 다른 방법을 찾게 한다
                    text = f"도구 오류: {exc}"
                sources.append(text)
            history.append(ToolMessage(text[:max_chars], tool_call_id=tc["id"]))
        if calls >= limits["max_tool_calls_per_reply"]:
            bound = model   # 한도에 닿으면 도구 없이 마지막 답을 쓰게 한다
    return "지금은 답을 정리하지 못했어요. 잠시 후 다시 물어봐 주세요.", sources


async def run(system: str, messages: list[BaseMessage], tools: list[BaseTool],
              temperature: float, node: str, session_id: str) -> tuple[str, list[str]]:
    """도구 루프를 시간 제한과 함께 실행하고 (최종 답, 도구 결과 목록)을 반환한다.

    Raises:
        asyncio.TimeoutError: task_timeout_seconds를 넘긴 경우.
    """
    timeout = config()["agent_loop"]["task_timeout_seconds"]
    return await asyncio.wait_for(
        _loop(system, messages, tools, temperature, node, session_id), timeout)
