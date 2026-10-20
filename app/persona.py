"""동료(이도현)·팀장(한서윤)·어시스턴트의 말을 만든다.

- 대화 맥락(Harness: Context): 최근 메시지는 history_token_budget 안에서 원문 그대로 넣고,
  예산을 넘는 앞부분은 LLM으로 요약해 넣는다(요약은 캐시, 판정 표현이 섞이면 버림).
- 실제 문장 생성과 검증(판정 발언·필수 내용·출처)은 발화 Graph(app/speak_graph.py)가 한다.
"""
import hashlib
import logging

from langchain_core.messages import HumanMessage, SystemMessage

from app import clock, docs, guard, llm, speak_graph, store
from app.settings import PART_TITLES, config, prompt

log = logging.getLogger("dayzero.persona")

NAMES = {"candidate": "지원자", "coworker": "이도현", "lead": "한서윤 팀장",
         "system": "시스템", "assistant": "AI 어시스턴트", "both": "함께"}
CHARS_PER_TOKEN = 2   # 한국어 대략치: Token 예산을 글자 수로 바꿀 때 쓰는 근사 상수


def _split(session_id: str, channels: tuple[str, ...]) -> tuple[list[dict], list[str], list[str]]:
    """대화를 (예산을 넘은 앞부분 메시지, 그 줄들, 예산 안의 최근 줄들)로 나눈다."""
    budget = config()["context"]["history_token_budget"] * CHARS_PER_TOKEN
    msgs = store.list_messages(session_id, channels=channels)
    lines = [f"[{clock.format_minute(m['minute'])}][{m['channel']}] {NAMES[m['sender']]}: {m['content']}"
             for m in msgs]
    kept, size = [], 0
    for line in reversed(lines):
        if size + len(line) > budget:
            break
        kept.append(line)
        size += len(line)
    cut = len(lines) - len(kept)
    return msgs[:cut], lines[:cut], list(reversed(kept))


def transcript(session_id: str, channels: tuple[str, ...]) -> str:
    """요약 없이 예산 안의 최근 대화만 (넘친 앞부분은 생략 표시)."""
    _, old, recent = _split(session_id, channels)
    head = [f"(이전 대화 {len(old)}개 생략)"] if old else []
    return "\n".join(head + recent) or "(아직 대화 없음)"


async def context(session_id: str, channels: tuple[str, ...]) -> str:
    """모델에 넣을 대화 맥락. 예산을 넘는 앞부분은 요약해 앞에 붙인다(Harness: Context).

    요약은 "세션·채널·마지막으로 요약한 메시지"로 캐시해 같은 부분을 다시 요약하지 않는다.
    요약이 실패하거나 판정 표현이 섞이면 요약 대신 생략 표시만 남긴다.
    """
    old_msgs, old, recent = _split(session_id, channels)
    if not old:
        return "\n".join(recent) or "(아직 대화 없음)"
    key = hashlib.sha256(f"summary|{session_id}|{','.join(channels)}|{old_msgs[-1]['id']}|"
                         f"{prompt('summarize')}".encode()).hexdigest()
    cached = store.cache_get(key)
    summary = cached["summary"] if cached else None
    if summary is None:
        try:
            out = await llm.ainvoke(llm.chat_model(config()["llm"]["judge_temperature"]),
                                    [SystemMessage(prompt("summarize")), HumanMessage("\n".join(old))],
                                    "context.summarize", session_id)
            summary = str(out.content).strip()
        except Exception:   # 요약 실패는 생략 표시로 대신한다(원인은 로그에)
            log.exception("대화 요약 실패: session=%s", session_id)
            summary = ""
        if summary and guard.violates(summary):
            summary = ""
        if summary:
            store.cache_put(key, {"summary": summary})
    head = (f"(이전 대화 {len(old)}개 요약)\n{summary}" if summary
            else f"(이전 대화 {len(old)}개 생략)")
    return "\n".join([head] + recent)


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
    if role == "lead":   # 팀장은 지원자와 동료의 1:1 대화를 볼 수 없다(팀 채널·알림만)
        channels = tuple(c for c in channels if c != "dm")
    session = store.get_session(session_id)
    system = (f"{prompt(role)}\n\n## 현재 상황\n- 세션 시각: {clock.format_minute(minute)}\n"
              f"- 현재 분담: {assignment_text(session['assignment'])}\n"
              f"- 지금 볼 수 있는 회사 문서: {docs_text(minute, session['company'])} (이 목록에 없는 문서는 아직 볼 수 없다)")
    human = (f"최근 대화:\n{await context(session_id, channels)}\n\n지금 할 일: {task}\n"
             "메신저에 보낼 메시지 본문만 써라.")
    result = await speak_graph.speak(
        session_id=session_id, node=f"persona.{role}", system=system, human=human, minute=minute,
        use_tools=use_tools, must_include=must_include, company=session["company"],
        plan_access=use_tools,   # 동료·팀장은 공유 문서를 읽을 수 있다(편집은 지원자만)
        required_tool="read_shared_plan" if review else "",   # 검토 요청이면 실제로 읽었는지 검증
        # 동료 파트가 아직 공유 전이면 결과물을 지어내지 않는지 검증
        unshared=role == "coworker" and not any(k.startswith("coworker:") for k in store.get_parts(session_id)),
        fallback=fallback or config()["verify"]["fallback_reply"])
    return result["text"]
