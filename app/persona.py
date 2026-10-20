"""동료(이도현)·팀장(한서윤)·어시스턴트의 말을 만든다.

- 대화 맥락은 최근 메시지를 history_token_budget 안에서 잘라 넣는다(Harness: Context).
- 실제 문장 생성과 검증(판정 발언·필수 내용·출처)은 발화 Graph(app/speak_graph.py)가 한다.
"""
from app import clock, docs, speak_graph, store
from app.settings import PART_TITLES, config, prompt

NAMES = {"candidate": "지원자", "coworker": "이도현", "lead": "한서윤 팀장",
         "system": "시스템", "assistant": "AI 어시스턴트", "both": "함께"}
CHARS_PER_TOKEN = 2   # 한국어 대략치: Token 예산을 글자 수로 바꿀 때 쓰는 근사 상수


def transcript(session_id: str, channels: tuple[str, ...]) -> str:
    """최근 대화를 예산 안에서 잘라 텍스트로 만든다. 넘친 앞부분은 생략 표시만 남긴다."""
    budget = config()["context"]["history_token_budget"] * CHARS_PER_TOKEN
    lines = [f"[{clock.format_minute(m['minute'])}][{m['channel']}] {NAMES[m['sender']]}: {m['content']}"
             for m in store.list_messages(session_id, channels=channels)]
    kept, size = [], 0
    for line in reversed(lines):
        if size + len(line) > budget:
            break
        kept.append(line)
        size += len(line)
    skipped = len(lines) - len(kept)
    head = [f"(이전 대화 {skipped}개 생략)"] if skipped else []
    return "\n".join(head + list(reversed(kept))) or "(아직 대화 없음)"


def docs_text(minute: float, company: str = "") -> str:
    """지금 볼 수 있는 회사 문서 목록 (모델이 없는 문서를 다른 문서로 대신 답하지 않게 알려 준다)."""
    return ", ".join(f"{d['title']}(doc_id={d['id']})" for d in docs.available(company, minute))


def is_review_request(text: str) -> bool:
    """지원자 메시지가 기획안 검토를 부탁하는지 (표현 목록은 config [verify].review_request_phrases)."""
    return any(p in text for p in config()["verify"]["review_request_phrases"])


def assignment_text(assignment: dict[str, str]) -> str:
    """분담안을 사람이 읽는 문장으로 바꾼다."""
    return ", ".join(f"{p}번 {PART_TITLES[p]}: {NAMES[o]}" for p, o in sorted(assignment.items()))


async def speak(role: str, session_id: str, minute: float, task: str,
                channels: tuple[str, ...], use_tools: bool = True,
                must_include: list[list[str]] | None = None, fallback: str | None = None,
                review: bool = False) -> str:
    """페르소나(coworker | lead)의 다음 메시지를 발화 Graph로 만든다(생성 → 코드 검증 → 재시도/대체).

    Args:
        role: "coworker" 또는 "lead".
        session_id: 세션 id.
        minute: 현재 세션 시각(분). 문서 공개 시각 판단에 쓴다.
        task: 이번에 할 일(장면의 전달 내용 또는 "지원자 메시지에 답하기").
        channels: 맥락으로 볼 채널.
        use_tools: 회사 문서 검색 도구를 줄지 여부.
        must_include: 장면 대사에 꼭 들어가야 할 사실(F-23 검증용).
        fallback: 검증을 끝내 통과하지 못했을 때 쓸 문장. 없으면 config의 기본 대체 문장.
    """
    session = store.get_session(session_id)
    system = (f"{prompt(role)}\n\n## 현재 상황\n- 세션 시각: {clock.format_minute(minute)}\n"
              f"- 현재 분담: {assignment_text(session['assignment'])}\n"
              f"- 지금 볼 수 있는 회사 문서: {docs_text(minute, session['company'])} (이 목록에 없는 문서는 아직 볼 수 없다)")
    human = (f"최근 대화:\n{transcript(session_id, channels)}\n\n지금 할 일: {task}\n"
             "메신저에 보낼 메시지 본문만 써라.")
    result = await speak_graph.speak(
        session_id=session_id, node=f"persona.{role}", system=system, human=human, minute=minute,
        use_tools=use_tools, must_include=must_include, company=session["company"],
        plan_access=use_tools,   # 동료·팀장은 공유 문서를 읽을 수 있다(편집은 지원자만)
        required_tool="read_shared_plan" if review else "",   # 검토 요청이면 실제로 읽었는지 검증
        fallback=fallback or config()["verify"]["fallback_reply"])
    return result["text"]
