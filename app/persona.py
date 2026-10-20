"""동료·팀장의 말을 만든다(이름·회사는 세션의 시나리오 팩에서).

- 대화 맥락(Harness: Context): 최근 메시지는 history_token_budget 안에서 원문 그대로 넣고,
  예산을 넘는 앞부분은 LLM으로 요약해 넣는다(요약은 캐시, 판정 표현이 섞이면 버림).
- 실제 문장 생성과 검증(판정 발언·필수 내용·출처)은 발화 Graph(app/speak_graph.py)가 한다.
- 볼 수 있는 채널: 동료는 팀 채널·동료 1:1, 팀장은 팀 채널·팀장 1:1(VISIBLE). 업무 메모(work)는 둘 다 못 본다.
- 장면이 정한 숨은 사실: 장면의 context_before / context_after(events.toml)를 그 장면 발화자의 지시문에 넣는다.
  예: 동료는 처음부터 다른 업무에 차출돼 일을 못 하고 있지만, 털어놓는 장면 전에는 먼저 꺼내지 않는다.
"""
import hashlib
import logging

from langchain_core.messages import HumanMessage, SystemMessage

from app import clock, docs, guard, llm, speak_graph, store
from app.settings import config, names, pack_info, part_titles, prompt, scenes

log = logging.getLogger("dayzero.persona")

CHARS_PER_TOKEN = 2   # 한국어 대략치: Token 예산을 글자 수로 바꿀 때 쓰는 근사 상수
# 역할별로 볼 수 있는 채널. 팀장은 지원자와 동료의 1:1을, 동료는 지원자와 팀장의 1:1을 볼 수 없다.
VISIBLE = {"coworker": ("team", "dm", "system"), "lead": ("team", "lead_dm", "system")}
# 지원자 메시지가 온 채널 → 답하는 역할. 팀 채널은 team_responder가 정한다.
CHANNEL_ROLE = {"dm": "coworker", "lead_dm": "lead"}


def _split(session_id: str, channels: tuple[str, ...]) -> tuple[list[dict], list[str], list[str]]:
    """대화를 (예산을 넘은 앞부분 메시지, 그 줄들, 예산 안의 최근 줄들)로 나눈다."""
    budget = config()["context"]["history_token_budget"] * CHARS_PER_TOKEN
    msgs = store.list_messages(session_id, channels=channels)
    who = names(store.get_session(session_id)["scenario"])
    lines = [f"[{clock.format_minute(m['minute'])}][{m['channel']}] {who[m['sender']]}: {m['content']}"
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


def docs_text(minute: float, company: str = "", pack: str | None = None) -> str:
    """지금 볼 수 있는 회사 문서 목록 (모델이 없는 문서를 다른 문서로 대신 답하지 않게 알려 준다)."""
    return ", ".join(f"{d['title']}(doc_id={d['id']})" for d in docs.available(company, minute, pack))


def shared_late_docs_text(minute: float, company: str = "", pack: str | None = None) -> str:
    """늦게 공개되는 문서 중 이미 공개된 것. 대화에 '늦게 공유됩니다'만 남아 있어도 공개 사실을 알게 한다."""
    late = [d for d in docs.available(company, minute, pack) if d["available_from_minute"] > 0]
    if not late:
        return ""
    names_ = ", ".join(f"'{d['title']}'({clock.format_minute(d['available_from_minute'])}에 공유됨)" for d in late)
    return f"- 늦게 공유된다던 문서 중 이미 공유된 것: {names_}. 지금 회사 문서에서 볼 수 있다\n"


def team_responder(text: str, pack: str | None = None) -> str:
    """팀 채널 메시지에 누가 답할지. 동료 이름을 부르면 동료, 아니면 팀장(팀 채널의 진행자)."""
    info = pack_info(pack)
    if any(n in text for n in (info["coworker_name"], info["coworker_short"])):
        return "coworker"
    return "lead"


def responder(channel: str, text: str, pack: str | None = None) -> str:
    """지원자 메시지에 답할 역할."""
    return CHANNEL_ROLE.get(channel) or team_responder(text, pack)


def scene_context(role: str, session_id: str, pack: str | None = None) -> str:
    """장면이 정한 숨은 사실 중 이 역할에 해당하는 것(장면 전이면 context_before, 실행 뒤면 context_after)."""
    ran = store.run_scene_ids(session_id)
    lines = []
    for sc in scenes(pack):
        if sc["speaker"] != role:
            continue
        text = sc.get("context_after") if sc["id"] in ran else sc.get("context_before")
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)


def assignment_text(assignment: dict[str, str], pack: str | None = None) -> str:
    """분담안을 사람이 읽는 문장으로 바꾼다."""
    titles, who = part_titles(pack), names(pack)
    return ", ".join(f"{p}번 {titles[p]}: {who[o]}" for p, o in sorted(assignment.items()))


async def speak(role: str, session_id: str, minute: float, task: str,
                channels: tuple[str, ...] | None = None, use_tools: bool = True,
                must_include: list[list[str]] | None = None, fallback: str | None = None) -> str:
    """페르소나(coworker | lead)의 다음 메시지를 발화 Graph로 만든다(생성 → 코드 검증 → 재시도/대체).

    Args:
        role: "coworker" 또는 "lead".
        session_id: 세션 id.
        minute: 현재 세션 시각(분). 문서 공개 시각 판단에 쓴다.
        task: 이번에 할 일(장면의 전달 내용 또는 "지원자 메시지에 답하기").
        channels: 맥락으로 볼 채널. 이 역할이 볼 수 있는 채널(VISIBLE)로 다시 거른다. 없으면 볼 수 있는 채널 전부.
        use_tools: 회사 문서 검색 도구를 줄지 여부.
        must_include: 장면 대사에 꼭 들어가야 할 사실(F-23 검증용).
        fallback: 검증을 끝내 통과하지 못했을 때 쓸 문장. 없으면 config의 기본 대체 문장.
    """
    # 팀장은 동료 1:1을, 동료는 팀장 1:1을 볼 수 없다. 업무 메모(work)는 지원자 혼자 보는 기록이다.
    channels = tuple(c for c in (channels or VISIBLE[role]) if c in VISIBLE[role])
    session = store.get_session(session_id)
    pack = session["scenario"]
    system = (f"{prompt(role, pack)}\n\n## 현재 상황\n- 세션 시각: {clock.format_minute(minute)}\n"
              f"- 현재 분담: {assignment_text(session['assignment'], pack)}\n"
              f"- 마감: {clock.deadline_text(minute, pack)}\n"
              f"{shared_late_docs_text(minute, session['company'], pack)}"
              f"- 지금 볼 수 있는 회사 문서: {docs_text(minute, session['company'], pack)} (이 목록에 없는 문서는 아직 볼 수 없다)")
    hidden = scene_context(role, session_id, pack)
    if hidden:
        system += "\n## 너만 아는 사실 (지시가 없으면 먼저 꺼내지 않는다)\n" + hidden
    human = (f"최근 대화:\n{await context(session_id, channels)}\n\n지금 할 일: {task}\n"
             "메신저에 보낼 메시지 본문만 써라.")
    result = await speak_graph.speak(
        session_id=session_id, node=f"persona.{role}", system=system, human=human, minute=minute,
        use_tools=use_tools, must_include=must_include, company=session["company"], scenario=pack,
        # 동료는 업무를 거의 못 하는 설정이라, 결과물을 만든 것처럼 말하지 않는지 늘 검증한다
        unshared=role == "coworker",
        fallback=fallback or config()["verify"]["fallback_reply"])
    return result["text"]
