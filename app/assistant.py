"""AI 어시스턴트 (F-16): 지원자가 직접 쓰는 업무용 도구.

세션 진행 Graph와 별도로, 요청마다 발화 Graph(app/speak_graph.py)를 한 번 실행한다(PRD A.3).
회사 문서 검색 도구만 쓰며 지원자 문서에 직접 쓰지 않는다. 답의 출처가 실제로 읽은 문서인지
코드가 검증한다. 요청과 응답은 AI 활용 관찰을 위해 assistant 채널에 남긴다(F-31).
"""
from app import guard, persona, speak_graph, store
from app.settings import config, prompt


async def ask(session_id: str, minute: float, text: str) -> str:
    """지원자 요청에 답하고 기록한다."""
    store.add_message(session_id, "assistant", "candidate", text, minute)
    company = store.get_session(session_id)["company"]
    if guard.is_evaluation_question(text):
        answer = guard.notice()
    else:
        system = (f"{prompt('assistant')}\n\n## 지금 볼 수 있는 회사 문서\n"
                  f"{persona.docs_text(minute, company)}\n이 목록에 없는 문서는 아직 볼 수 없다.")
        human = (f"이전 대화:\n{persona.transcript(session_id, ('assistant',))}\n\n"
                 "가장 마지막 사용자 요청에 답하라.")
        result = await speak_graph.speak(
            session_id=session_id, node="assistant", system=system, human=human, minute=minute,
            use_tools=True, fallback=config()["verify"]["fallback_assistant"], company=company)
        answer = result["text"]
    store.add_message(session_id, "assistant", "assistant", answer, minute)
    return answer
