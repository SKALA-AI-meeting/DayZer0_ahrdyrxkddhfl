"""AI 어시스턴트 (F-16): 지원자가 직접 쓰는 업무용 도구.

Graph와 별도의 단순한 도구 호출 흐름이다(PRD A.3). 회사 문서 검색 도구만 쓰며,
지원자 문서에 직접 쓰지 않는다. 요청과 응답은 AI 활용 관찰을 위해 assistant 채널에 남긴다(F-31).
"""
from langchain_core.messages import HumanMessage

from app import agent_loop, guard, persona, store
from app.mcp_client import doc_tools
from app.settings import config, prompt


async def ask(session_id: str, minute: float, text: str) -> str:
    """지원자 요청에 답하고 기록한다."""
    store.add_message(session_id, "assistant", "candidate", text, minute)
    if guard.is_evaluation_question(text):
        answer = guard.notice()
    else:
        human = HumanMessage(f"이전 대화:\n{persona.transcript(session_id, ('assistant',))}\n\n"
                             "가장 마지막 사용자 요청에 답하라.")
        answer = ""
        for _ in range(2):
            async with doc_tools(minute) as tools:
                system = (f"{prompt('assistant')}\n\n## 지금 볼 수 있는 회사 문서\n"
                          f"{persona.docs_text(minute)}\n이 목록에 없는 문서는 아직 볼 수 없다.")
                answer, _ = await agent_loop.run(system, [human], tools,
                                                 config()["llm"]["persona_temperature"],
                                                 "assistant", session_id)
            if not guard.violates(answer):
                break
        else:
            answer = guard.notice()
    store.add_message(session_id, "assistant", "assistant", answer.strip(), minute)
    return answer.strip()
