"""발화 Graph: 동료·팀장·어시스턴트가 말 한 마디를 만드는 LangGraph.

AI가 만든 문장을 그대로 내보내지 않고, 코드가 검증한 뒤 통과한 것만 내보낸다.

    START → llm ─┬─ 도구 호출 있음(한도 안) → tools → llm        (도구 순환: Agent Loop)
                 └─ 답 완성 → verify ─┬─ 통과 → done → END
                                      ├─ 실패 + 재시도 가능 → feedback → llm   (검증 순환)
                                      └─ 실패 + 한도 초과 → fallback → END     (안전한 대체 문장)

verify가 검사하는 것 (모두 코드, LLM 아님):
- 검토 요청 무시: 기획안 검토를 부탁받았는데 공유 문서(read_shared_plan)를 읽지 않음
- 말만 하고 실행 안 함: "확인해 볼게요"라고 하면서 이번 답에서 도구를 한 번도 부르지 않음
- 판정 발언(F-24): 합격·평가 결과·연출 같은 금지 표현
- 필수 내용(F-23): 장면 대사에 전달해야 할 사실(events.toml must_include)이 빠졌는지
- 출처(F-22): 답에 밝힌 회사 문서가 실제로 이번에 도구로 읽은 문서인지
"""
import asyncio
import json
from typing import TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from app import docs, guard, llm, progress
from app.mcp_client import doc_tool_specs, doc_tools
from app.settings import config


class SpeakState(TypedDict, total=False):
    session_id: str
    node: str                     # 기록용 이름 (예: "persona.coworker")
    minute: float                 # 세션 시각. 문서 공개 시각 판단에 쓴다(코드가 넣음)
    company: str                  # 기업. 기업이 등록한 문서(F-06)까지 검색한다(코드가 넣음)
    plan_session: str             # 공유 문서 읽기를 허용할 세션 id(동료·팀장만, 없으면 "")
    use_tools: bool
    temperature: float
    messages: list[BaseMessage]   # 시스템·대화·도구 결과
    must_include: list[list[str]]  # 필수 사실. 안쪽 목록은 "이 중 하나"
    fallback: str
    tool_calls: int
    streak: int
    last_tool: str
    read_titles: list[str]        # 이번 발화에서 도구로 실제 읽은 문서 제목
    called_tools: list[str]       # 이번 시도에서 실제로 호출한 도구 이름
    required_tool: str            # 이번 답에서 반드시 호출해야 하는 도구(예: 검토 요청 → read_shared_plan)
    regenerations: int
    text: str
    verdict: str                  # ok | judgement | missing:<사실> | citation:<문서>
    verdicts: list[str]           # 시도별 검증 결과(평가·기록용)


def read_titles_from(result) -> list[str]:
    """도구 결과에서 내용(excerpt·body)이 실제로 온 문서 제목만 뽑는다.

    "아직 공유되지 않음" 안내(notice)만 온 문서는 읽은 것으로 치지 않는다. 결과 항목을 하나씩
    보므로, 안내가 섞여 있어도 함께 온 다른 문서는 읽은 것으로 인정된다.
    """
    blocks = result if isinstance(result, list) else [result]
    items: list = []
    for b in blocks:
        raw = b.get("text", "") if isinstance(b, dict) and "text" in b else b
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        items += parsed if isinstance(parsed, list) else [parsed]
    return [i["title"] for i in items
            if isinstance(i, dict) and i.get("title") and ("excerpt" in i or "body" in i)]


def tool_text(result) -> str:
    """MCP 도구 결과(content block 목록 또는 문자열)를 텍스트로 바꾼다."""
    if isinstance(result, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in result)
    return str(result)


# ---------- 노드 ----------

async def llm_node(state: SpeakState) -> SpeakState:
    """모델이 다음 답 또는 도구 호출을 제안한다. 도구 한도에 닿았으면 도구 없이 답하게 한다."""
    progress.set(state.get("session_id", ""), state.get("node", ""), "llm",
                 "답을 다시 쓰는 중" if state.get("regenerations") else "답장을 쓰는 중")
    model = llm.chat_model(state["temperature"])
    limits = config()["agent_loop"]
    can_use_tools = state["use_tools"] and state["tool_calls"] < limits["max_tool_calls_per_reply"]
    if can_use_tools:
        model = model.bind_tools(doc_tool_specs(bool(state["plan_session"])))   # 설명만 넘기고, 실행은 tools 노드에서
    ai = await llm.ainvoke(model, state["messages"], state["node"], state["session_id"])
    return {"messages": state["messages"] + [ai], "text": str(ai.content)}


def after_llm(state: SpeakState) -> str:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else "verify"


async def tools_node(state: SpeakState) -> SpeakState:
    """제안된 도구를 실행한다(rag-mcp). 결과는 잘라서, 오류는 ToolMessage로 모델에게 돌려준다."""
    limits = config()["agent_loop"]
    max_chars = config()["context"]["tool_result_max_chars"]
    calls, streak, last = state["tool_calls"], state["streak"], state["last_tool"]
    titles = list(state["read_titles"])
    out: list[BaseMessage] = []
    names = {tc["name"] for tc in state["messages"][-1].tool_calls}
    progress.set(state.get("session_id", ""), state.get("node", ""), "tools",
                 "공유 문서를 읽는 중" if names == {"read_shared_plan"} else "회사 문서를 찾는 중")
    async with doc_tools(state["minute"], state["company"], state["plan_session"]) as tools:
        by_name = {t.name: t for t in tools}
        for tc in state["messages"][-1].tool_calls:
            calls += 1
            streak = streak + 1 if tc["name"] == last else 1
            last = tc["name"]
            if calls > limits["max_tool_calls_per_reply"] or streak > limits["same_tool_max"]:
                text = "도구 호출 한도를 넘었습니다. 지금까지 찾은 정보로 답하세요."
            elif tc["name"] not in by_name:
                text = f"없는 도구입니다: {tc['name']}"
            else:
                try:
                    raw = await by_name[tc["name"]].ainvoke(tc["args"])
                    text = tool_text(raw)
                    # 공개된 문서 내용(발췌·본문)이 실제로 돌아온 문서만 "읽은 문서"로 인정한다
                    titles += read_titles_from(raw)
                except Exception as exc:   # 도구 오류는 모델에게 알려 다른 방법을 찾게 한다
                    text = f"도구 오류: {exc}"
            out.append(ToolMessage(text[:max_chars], tool_call_id=tc["id"]))
    called = state["called_tools"] + [tc["name"] for tc in state["messages"][-1].tool_calls]
    return {"messages": state["messages"] + out, "tool_calls": calls, "streak": streak,
            "last_tool": last, "read_titles": titles, "called_tools": called}


def check(text: str, must_include: list[list[str]], read_titles: list[str], use_tools: bool,
          minute: float = float("inf"), company: str = "", tools_used: int = 1,
          called_tools: list[str] | None = None, required_tool: str = "") -> str:
    """AI 문장을 코드로 검증한다. 통과하면 "ok", 아니면 실패 이유.

    출처 검사는 지금 공개된 문서만 대상으로 한다. 공개 전 문서는 내용을 알 수 없으므로
    "아직 볼 수 없다"고 제목을 말하는 것은 근거 인용이 아니다.
    """
    if guard.violates(text):
        return "judgement"
    for options in must_include:
        if not any(o in text for o in options):
            return f"missing:{options[0]}"
    if use_tools and required_tool and required_tool not in (called_tools or []):
        return "not_read"   # 검토를 부탁받았는데 공유 문서를 실제로 읽지 않음
    if use_tools and tools_used == 0 and any(p in text for p in config()["verify"]["deferral_phrases"]):
        return "deferral"   # 확인하겠다고 말만 하고 이번 답에서 도구를 부르지 않음
    if use_tools:
        for d in docs.available(company, minute):
            if d["title"] in text and d["title"] not in read_titles:
                return f"citation:{d['title']}"
    return "ok"


def verify_node(state: SpeakState) -> SpeakState:
    progress.set(state.get("session_id", ""), state.get("node", ""), "verify", "답을 확인하는 중")
    verdict = check(state["text"], state["must_include"], state["read_titles"], state["use_tools"],
                    state["minute"], state["company"], state["tool_calls"],
                    state["called_tools"], state["required_tool"])
    llm.trace({"session": state["session_id"], "node": f"{state['node']}.verify",
               "verdict": verdict, "regenerations": state["regenerations"]})
    return {"verdict": verdict, "verdicts": state["verdicts"] + [verdict]}


def after_verify(state: SpeakState) -> str:
    if state["verdict"] == "ok":
        return "done"
    if state["regenerations"] < config()["verify"]["max_regenerations"]:
        return "feedback"
    return "fallback"


FEEDBACK = {
    "judgement": "방금 답에 평가·합격·연출에 관한 표현이 있었다. 그런 표현 없이 업무 이야기만 다시 써라.",
    "missing": "방금 답에 반드시 전달해야 할 내용 '{x}'이(가) 빠졌다. 빠짐없이 넣어 다시 써라.",
    "not_read": "지원자가 기획안 검토를 부탁했는데 공유 문서를 읽지 않았다. read_shared_plan으로 읽은 뒤, "
                "지원자 파트에 대한 구체적인 의견을 1~2개 주어라(회사 문서 근거가 있으면 함께). 대신 써 주지는 마라.",
    "deferral": "확인하겠다고만 하고 실제로 확인하지 않았다. 지금 도구를 호출해 확인한 뒤, 확인한 내용으로 바로 답하라.",
    "citation": "방금 답에서 '{x}' 문서를 근거로 들었지만 이번에 그 문서를 읽지 않았다. "
                "도구로 확인하거나, 확인하지 않은 문서는 언급하지 말고 다시 써라.",
}


def feedback_node(state: SpeakState) -> SpeakState:
    """실패 이유를 모델에게 알려 다시 쓰게 한다.

    다시 쓰는 답은 새 응답이므로 도구 호출 한도를 새로 준다(출처 실패 후 문서를 읽을 수 있게).
    전체 반복은 max_regenerations와 recursion_limit이 막는다.
    """
    kind, _, detail = state["verdict"].partition(":")
    note = HumanMessage(FEEDBACK[kind].format(x=detail))
    return {"messages": state["messages"] + [note], "regenerations": state["regenerations"] + 1,
            "tool_calls": 0, "streak": 0, "last_tool": "", "called_tools": []}


def done_node(state: SpeakState) -> SpeakState:
    progress.clear(state.get("session_id", ""), state.get("node", ""))
    return {"text": state["text"].strip()}


def fallback_node(state: SpeakState) -> SpeakState:
    """끝내 검증을 통과하지 못하면 사실을 지어내지 않는 대체 문장을 쓴다."""
    progress.clear(state.get("session_id", ""), state.get("node", ""))
    text = guard.notice() if state["verdict"] == "judgement" else state["fallback"]
    return {"text": text}


def build():
    g = StateGraph(SpeakState)
    for name, fn in [("llm", llm_node), ("tools", tools_node), ("verify", verify_node),
                     ("feedback", feedback_node), ("done", done_node), ("fallback", fallback_node)]:
        g.add_node(name, fn)
    g.add_edge(START, "llm")
    g.add_conditional_edges("llm", after_llm, ["tools", "verify"])
    g.add_edge("tools", "llm")
    g.add_conditional_edges("verify", after_verify, ["done", "feedback", "fallback"])
    g.add_edge("feedback", "llm")
    g.add_edge("done", END)
    g.add_edge("fallback", END)
    return g.compile()


SPEAK = build()


async def speak(*, session_id: str, node: str, system: str, human: str, minute: float,
                use_tools: bool, fallback: str, must_include: list[list[str]] | None = None,
                company: str = "", plan_access: bool = False, required_tool: str = "") -> dict:
    """발화 Graph를 실행하고 {text, verdicts, read_titles}를 반환한다.

    Raises:
        asyncio.TimeoutError: config [agent_loop].task_timeout_seconds를 넘긴 경우.
    """
    cfg = config()
    limits = cfg["agent_loop"]
    # 도구 순환·검증 순환을 모두 돌 수 있는 최대 단계 수(무한 반복 방지)
    recursion = (limits["max_tool_calls_per_reply"] * 2 + 3) * (cfg["verify"]["max_regenerations"] + 1) + 5
    try:
        state = await _invoke({
        "session_id": session_id, "node": node, "minute": minute, "company": company,
        "plan_session": session_id if plan_access else "",
        "called_tools": [], "required_tool": required_tool if plan_access else "",
        "use_tools": use_tools,
        "temperature": cfg["llm"]["persona_temperature"],
        "messages": [SystemMessage(system), HumanMessage(human)],
        "must_include": must_include or [], "fallback": fallback, "tool_calls": 0, "streak": 0,
        "last_tool": "", "read_titles": [], "regenerations": 0, "text": "", "verdict": "",
        "verdicts": []}, recursion, limits["task_timeout_seconds"])
    finally:
        progress.clear(session_id, node)
    return {"text": state["text"], "verdicts": state["verdicts"], "read_titles": state["read_titles"]}


async def _invoke(initial: SpeakState, recursion: int, timeout: float) -> SpeakState:
    return await asyncio.wait_for(SPEAK.ainvoke(initial, {"recursion_limit": recursion}), timeout)
