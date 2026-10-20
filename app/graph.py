"""세션 진행 Graph (LangGraph StateGraph).

한 번 실행 = 한 번의 입력(정해진 시각이 된 장면, 또는 지원자 메시지) 처리.

    START ─┬─ 이벤트 장면 + 아직 분업 중 ─→ finalize_split ─→ run_scene ─→ END
           ├─ 장면 ─────────────→ run_scene ─→ END
           ├─ 분업 중 + 동료 1:1 ─→ check_agreement ─┬─ 분업 얘기 아님 ─→ reply
           │                                        ├─ 합의 / 조정 한도 도달 ─→ apply_assignment ─→ END
           │                                        └─ 미합의 ─→ counter_proposal ─→ END (다음 메시지에서 다시 판단: 반복)
           └─ 그 외 ───────────→ reply ─→ END

분업 조정은 최대 config [negotiation].max_rounds회(F-21). 이벤트 실행 시점은 engine이 시계로 정한다.
"""
from typing import Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app import llm, persona, store
from app.mcp_client import call_business
from app.settings import PART_TITLES, config, coworker_parts, scene_by_id

ALL_CHANNELS = ("team", "dm", "system")


class TurnState(TypedDict, total=False):
    session_id: str
    minute: float
    trigger: dict            # {"kind": "scene", "scene_id": ...} | {"kind": "candidate", "channel": ..., "text": ...}
    phase: str               # waiting | negotiate | work | done
    negotiation_round: int
    assignment: dict[str, str]
    about_split: bool
    agreed: bool
    requested: dict[str, str]


class PartOwner(BaseModel):
    part: Literal["1", "2", "3", "4", "5"]
    owner: Literal["candidate", "coworker", "both"]


class Agreement(BaseModel):
    """지원자 메시지에 대한 분업 판단."""
    about_split: bool = Field(description="메시지가 업무 분담에 관한 이야기면 true")
    agreed: bool = Field(description="지원자가 현재 분담안을 그대로 받아들였으면 true")
    requested: list[PartOwner] = Field(default_factory=list,
                                       description="지원자가 바꾸자고 한 파트만. 없으면 빈 목록")


async def _mark_coworker_cards(session_id: str, assignment: dict[str, str], status: str) -> None:
    """동료가 맡은 카드 상태를 business-mcp 변경 도구로 바꾼다(동료가 일하는 게 보드에 보이게)."""
    for part, owner in sorted(assignment.items()):
        if owner == "coworker":
            await call_business("update_task_status",
                                {"session_id": session_id, "part": part, "status": status})


def _post(state: TurnState, channel: str, sender: str, text: str) -> None:
    store.add_message(state["session_id"], channel, sender, text, state["minute"])


async def run_scene(state: TurnState) -> TurnState:
    """시나리오 장면을 실행한다. 전달 내용은 고정, 문장만 페르소나가 자연스럽게 바꾼다(PRD 6.4)."""
    scene = scene_by_id(state["trigger"]["scene_id"])
    sid, update = state["session_id"], {}
    if scene["speaker"] == "system":
        _post(state, "system", "system", scene["message"])
        return update
    task = f"다음 내용을 빠짐없이 전달하라: {scene['message']}"
    if scene["id"] == "negotiate":
        update = {"phase": "negotiate"}
    shared = []
    if scene.get("shares_coworker_parts"):
        for part in coworker_parts():
            key = str(part["part"])
            # 지원자가 맡기로 한 파트는 덮어쓰지 않는다. 3번(핵심 기능)은 이벤트 내용이므로 항상 공유.
            if key == "3" or state["assignment"].get(key) != "candidate":
                store.upsert_part(sid, f"coworker:{key}", part["body"].strip(), state["minute"])
                shared.append(f"[{key}번 {part['title']}]\n{part['body'].strip()}")
        # 공유 내용은 코드가 원문 그대로 붙인다(F-23 정해진 내용). LLM은 인사만 하고 특정 항목을 짚지 않는다.
        nums = ", ".join(s.split("번", 1)[0].strip("[") + "번" for s in shared)
        # 지원자가 3번을 혼자 맡았을 수도 있으므로 "내가 맡은"이라고 단정하지 않는다(사실과 다른 말 방지).
        task = (f"기획안 {nums}에 넣을 너(이도현)의 작업 내용을 공유 문서에 올렸으니, 지원자에게 "
                "리뷰해 달라고 한두 문장으로 짧게 부탁하라. '내가 맡은 파트'라고 단정하지 마라. "
                "지원자를 '도현님'이라고 부르지 마라. 내용을 요약하거나 특정 항목을 언급하지 마라.")
    # 장면 대사는 필수 내용(must_include)이 들어갔는지 코드가 검증하고, 끝내 빠지면 원문 대사를 쓴다(F-23).
    text = await persona.speak(scene["speaker"], sid, state["minute"], task, ALL_CHANNELS,
                               use_tools=False, must_include=scene.get("must_include"),
                               fallback=scene.get("fallback"))
    if shared:
        text += "\n\n" + "\n\n".join(shared)
        await _mark_coworker_cards(sid, state["assignment"], "done")
    _post(state, scene["channel"], scene["speaker"], text)
    if update:
        store.update_session(sid, **update)
    return update


async def check_agreement(state: TurnState) -> TurnState:
    """지원자 메시지가 분담안에 동의인지, 무엇을 바꾸자는지 판단한다."""
    judge = llm.chat_model(config()["llm"]["judge_temperature"]).with_structured_output(
        Agreement, method="function_calling")
    result: Agreement = await llm.ainvoke(judge, [
        SystemMessage("동료가 제안한 업무 분담안에 대한 지원자의 답을 분류한다. 추측하지 말고 메시지에 있는 내용만 본다."),
        HumanMessage(f"현재 분담안: {persona.assignment_text(state['assignment'])}\n"
                     f"지원자 메시지: {state['trigger']['text']}")], "graph.check_agreement",
        state["session_id"])
    return {"about_split": result.about_split, "agreed": result.agreed,
            "requested": {r.part: r.owner for r in result.requested}}


def after_check(state: TurnState) -> str:
    if not state["about_split"]:
        return "reply"
    if state["agreed"] or state["negotiation_round"] + 1 >= config()["negotiation"]["max_rounds"]:
        return "apply_assignment"
    return "counter_proposal"


async def counter_proposal(state: TurnState) -> TurnState:
    """지원자 요청을 반영한 새 분담안을 동료가 다시 제안한다 (조정 1회)."""
    assignment = {**state["assignment"], **state["requested"]}
    rounds = state["negotiation_round"] + 1
    store.update_session(state["session_id"], assignment=assignment, negotiation_round=rounds)
    text = await persona.speak(
        "coworker", state["session_id"], state["minute"],
        f"지원자의 요청을 반영한 분담안을 다시 제안하고 이대로 괜찮은지 물어라: "
        f"{persona.assignment_text(assignment)}", ALL_CHANNELS, use_tools=False)
    _post(state, "dm", "coworker", text)
    return {"assignment": assignment, "negotiation_round": rounds}


async def apply_assignment(state: TurnState) -> TurnState:
    """분업을 확정해 business-mcp로 태스크 보드에 반영하고 작업 단계로 넘어간다."""
    assignment = {**state["assignment"], **state.get("requested", {})}
    rounds = state["negotiation_round"] + (0 if state["agreed"] else 1)
    await call_business("assign_tasks", {"session_id": state["session_id"], "assignment": assignment})
    await _mark_coworker_cards(state["session_id"], assignment, "doing")
    store.update_session(state["session_id"], assignment=assignment, negotiation_round=rounds,
                         phase="work")
    note = "" if state["agreed"] else " (조정 횟수 한도에 닿아 마지막 안으로 진행한다고 부드럽게 알려라)"
    text = await persona.speak(
        "coworker", state["session_id"], state["minute"],
        f"분담을 확정했다고 짧게 알리고 태스크 보드에 반영했다고 말하라: "
        f"{persona.assignment_text(assignment)}{note}", ALL_CHANNELS, use_tools=False)
    _post(state, "dm", "coworker", text)
    return {"assignment": assignment, "phase": "work", "negotiation_round": rounds}


async def reply(state: TurnState) -> TurnState:
    """지원자 메시지에 답한다. 1:1이면 동료, 팀 채널이면 팀장. 회사 정보는 문서 검색 후 출처를 밝힌다."""
    channel = state["trigger"]["channel"]
    role = "coworker" if channel == "dm" else "lead"
    text = await persona.speak(role, state["session_id"], state["minute"],
                               "방금 지원자가 보낸 메시지에 답하라.", ALL_CHANNELS)
    _post(state, channel, role, text)
    return {}


async def finalize_split(state: TurnState) -> TurnState:
    """분업 조정이 끝나기 전에 첫 이벤트 시각이 오면, 지금 안으로 확정한다.

    PRD가 정하지 않은 빈칸을 채운 처리다. F-21의 "마지막 안으로 진행"과 같은 방식이며,
    이벤트가 나올 때 담당이 항상 정해져 있게 한다(가이드 7절).
    """
    assignment = state["assignment"]
    await call_business("assign_tasks", {"session_id": state["session_id"], "assignment": assignment})
    await _mark_coworker_cards(state["session_id"], assignment, "doing")
    store.update_session(state["session_id"], phase="work")
    text = await persona.speak(
        "coworker", state["session_id"], state["minute"],
        f"분담 얘기가 아직 안 끝났지만 시간이 됐으니 일단 지금 안으로 진행하자고 부드럽게 알리고, "
        f"태스크 보드에 반영했다고 말하라: {persona.assignment_text(assignment)}",
        ALL_CHANNELS, use_tools=False)
    _post(state, "dm", "coworker", text)
    return {"phase": "work"}


def route_entry(state: TurnState) -> str:
    trigger = state["trigger"]
    if trigger["kind"] == "scene":
        is_event = bool(scene_by_id(trigger["scene_id"])["element"])
        return "finalize_split" if is_event and state["phase"] == "negotiate" else "run_scene"
    if state["phase"] == "negotiate" and trigger["channel"] == "dm":
        return "check_agreement"
    return "reply"


def build():
    """Graph를 컴파일한다. checkpointer로 thread_id(=세션 id)별 상태를 저장한다."""
    g = StateGraph(TurnState)
    for name, fn in [("run_scene", run_scene), ("check_agreement", check_agreement),
                     ("counter_proposal", counter_proposal), ("apply_assignment", apply_assignment),
                     ("reply", reply), ("finalize_split", finalize_split)]:
        g.add_node(name, fn)
    g.add_conditional_edges(START, route_entry,
                            ["run_scene", "finalize_split", "check_agreement", "reply"])
    g.add_edge("finalize_split", "run_scene")   # 확정한 뒤 원래 이벤트를 그대로 실행
    g.add_conditional_edges("check_agreement", after_check,
                            ["reply", "apply_assignment", "counter_proposal"])
    for name in ("run_scene", "counter_proposal", "apply_assignment", "reply"):
        g.add_edge(name, END)
    return g.compile(checkpointer=InMemorySaver())


GRAPH = build()


async def run_turn(session_id: str, minute: float, trigger: dict) -> None:
    """입력 하나를 Graph로 처리한다. 세션 상태는 DB에서 읽어 넣는다."""
    s = store.get_session(session_id)
    await GRAPH.ainvoke(
        {"session_id": session_id, "minute": minute, "trigger": trigger, "phase": s["phase"],
         "negotiation_round": s["negotiation_round"], "assignment": s["assignment"],
         "requested": {}, "agreed": False, "about_split": False},
        {"configurable": {"thread_id": session_id},
         "recursion_limit": config()["agent_loop"]["max_turns"]})
