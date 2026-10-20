"""세션 진행 Graph (LangGraph StateGraph).

한 번 실행 = 한 번의 입력(정해진 시각이 된 장면, 또는 지원자 메시지) 처리.

    START ─┬─ 이벤트 장면 + 아직 분업 중 ─→ finalize_split ─→ run_scene ─→ END
           ├─ 장면 ─────────────→ run_scene ─→ END
           ├─ 분업 중 + 동료 1:1 ─→ check_agreement ─┬─ 분업 얘기 아님 ─→ reply
           │                                        ├─ 합의 / 조정 한도 도달 ─→ apply_assignment ─→ END
           │                                        └─ 미합의 ─→ counter_proposal ─→ END (다음 메시지에서 다시 판단: 반복)
           └─ 그 외 ───────────→ reply ─→ END

- 답하는 사람: 동료 1:1은 동료, 팀장 1:1은 팀장, 팀 채널은 동료 이름을 부르면 동료·아니면 팀장(persona.responder).
- 후속 반응: 장면에 follow_up이 있으면, 그 장면 뒤 지원자가 같은 채널에서 처음 답할 때 한 번만 덧붙인다.
- 업무 지문 장면(speaker = narrator)은 LLM 없이 정해진 문장을 업무 기록(work)에 남긴다.

분업 조정은 최대 config [negotiation].max_rounds회(F-21). 장면 실행 시점은 engine이 시계로 정한다.
"""
import logging
from typing import Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app import llm, persona, store, work
from app.mcp_client import call_business
from app.settings import config, scene_by_id, scenes

log = logging.getLogger("dayzero.graph")
FOLLOW_UP_PREFIX = "followup:"   # 후속 반응을 한 번만 쓰게 장면 실행 기록(scene_runs)에 남기는 키


class TurnState(TypedDict, total=False):
    session_id: str
    minute: float
    scenario: str            # 시나리오 팩(기업 맞춤 시나리오)
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
                                       description="지원자가 바꾸자고 한 업무만. 없으면 빈 목록")


async def _mark_coworker_cards(session_id: str, assignment: dict[str, str], status: str) -> None:
    """동료가 맡은 카드 상태를 business-mcp 변경 도구로 바꾼다(동료가 일하는 게 보드에 보이게)."""
    for part, owner in sorted(assignment.items()):
        if owner == "coworker":
            await call_business("update_task_status",
                                {"session_id": session_id, "part": part, "status": status})


def _post(state: TurnState, channel: str, sender: str, text: str) -> None:
    store.add_message(state["session_id"], channel, sender, text, state["minute"])


def _already_said(session_id: str, scene: dict) -> bool:
    """장면 발화자가 이미 같은 채널에서 장면의 필수 내용(must_include)을 모두 말했는가."""
    for m in store.list_messages(session_id, channels=(scene["channel"],)):
        if m["sender"] == scene["speaker"] and all(any(o in m["content"] for o in opts)
                                                   for opts in scene.get("must_include", [])):
            return True
    return False


def scene_task(scene: dict) -> str:
    """장면 대사를 만들 때 페르소나에게 주는 지시. 괄호 안은 말하는 방식에 대한 지시다(말로 옮기지 않는다)."""
    return f"다음 내용을 네 말로 빠짐없이 전달하라. 괄호 안은 지시이니 말로 옮기지 마라: {scene['message']}"


async def run_scene(state: TurnState) -> TurnState:
    """시나리오 장면을 실행한다. 전달 내용은 고정, 문장만 페르소나가 자연스럽게 바꾼다(PRD 6.4)."""
    scene = scene_by_id(state["trigger"]["scene_id"], state["scenario"])
    sid, update = state["session_id"], {}
    if scene["speaker"] in ("system", "narrator"):   # 알림·업무 지문은 정해진 문장 그대로(진척 요약은 코드가 채움)
        text = scene["message"].replace("{progress_summary}", work.summary(sid, state["scenario"]))
        _post(state, scene["channel"], scene["speaker"], text)
        return update
    task = scene_task(scene)
    if scene["id"] == "negotiate":
        update = {"phase": "negotiate"}
    must_include, fallback = scene.get("must_include"), scene.get("fallback")
    if scene.get("if_already_said") and _already_said(sid, scene):
        # 지원자가 먼저 물어 이미 전달한 내용이면 반복하지 않고 짧게 확인한다(같은 말 두 번 방지)
        task, must_include, fallback = scene["if_already_said"], None, scene["if_already_said_fallback"]
    # 장면 대사는 필수 내용(must_include)이 들어갔는지 코드가 검증하고, 끝내 빠지면 원문 대사를 쓴다(F-23).
    try:
        text = await persona.speak(scene["speaker"], sid, state["minute"], task, use_tools=False,
                                   must_include=must_include, fallback=fallback)
    except Exception:   # LLM이 끝내 실패해도 세션이 멈추지 않게 정해진 대사로 진행한다(F-23, PRD A.4)
        log.exception("장면 대사 생성 실패, 대체 대사 사용: session=%s scene=%s", sid, scene["id"])
        llm.trace({"session": sid, "node": "graph.run_scene", "fallback": scene["id"]})
        text = fallback
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
        HumanMessage(f"현재 분담안: {persona.assignment_text(state['assignment'], state['scenario'])}\n"
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
        f"{persona.assignment_text(assignment, state['scenario'])}", use_tools=False)
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
        f"{persona.assignment_text(assignment, state['scenario'])}{note}", use_tools=False)
    _post(state, "dm", "coworker", text)
    return {"assignment": assignment, "phase": "work", "negotiation_round": rounds}


def pending_follow_up(session_id: str, role: str, channel: str, pack: str | None = None) -> dict | None:
    """이 역할이 이 채널에서 아직 쓰지 않은 후속 반응 중 가장 최근 장면. 없으면 None.

    더 최근 장면의 후속 반응을 쓰면 그 전 장면의 것은 지나간 것으로 본다(오래된 반응이 뒤늦게 나오지 않게).
    """
    runs = store.scene_run_minutes(session_id)
    ran = [s for s in scenes(pack) if s.get("follow_up") and s["speaker"] == role
           and s["channel"] == channel and s["id"] in runs]
    if not ran:
        return None
    latest = max(ran, key=lambda s: runs[s["id"]])
    return None if f"{FOLLOW_UP_PREFIX}{latest['id']}" in runs else latest


async def reply(state: TurnState) -> TurnState:
    """지원자 메시지에 답한다. 답하는 사람은 채널로 정한다. 회사 정보는 문서 검색 후 출처를 밝힌다."""
    sid, channel = state["session_id"], state["trigger"]["channel"]
    role = persona.responder(channel, state["trigger"]["text"], state["scenario"])
    task = "방금 지원자가 보낸 메시지에 답하라."
    follow = pending_follow_up(sid, role, channel, state["scenario"])
    if follow and store.claim_scene(sid, f"{FOLLOW_UP_PREFIX}{follow['id']}", state["minute"]):
        # 후속 반응은 장면마다 한 번만(지원자마다 같은 조건). 그 전 장면들의 반응도 지나간 것으로 표시한다.
        for s in scenes(state["scenario"]):
            if s.get("follow_up") and s["speaker"] == role and s["channel"] == channel:
                store.claim_scene(sid, f"{FOLLOW_UP_PREFIX}{s['id']}", state["minute"])
        task += f" 이때 다음 반응을 한 번 담아라: {follow['follow_up']}"
        llm.trace({"session": sid, "node": "graph.follow_up", "scene": follow["id"]})
    text = await persona.speak(role, sid, state["minute"], task)
    _post(state, channel, role, text)
    return {}


async def finalize_split(state: TurnState) -> TurnState:
    """분업 조정이 끝나기 전에 첫 이벤트 시각이 오면, 지금 안으로 확정한다.

    PRD가 정하지 않은 빈칸을 채운 처리다. F-21의 "마지막 안으로 진행"과 같은 방식이며,
    이벤트가 나올 때 담당이 항상 정해져 있게 한다.
    """
    assignment = state["assignment"]
    await call_business("assign_tasks", {"session_id": state["session_id"], "assignment": assignment})
    await _mark_coworker_cards(state["session_id"], assignment, "doing")
    store.update_session(state["session_id"], phase="work")
    text = await persona.speak(
        "coworker", state["session_id"], state["minute"],
        f"분담 얘기가 아직 안 끝났지만 시간이 됐으니 일단 지금 안으로 진행하자고 부드럽게 알리고, "
        f"태스크 보드에 반영했다고 말하라: {persona.assignment_text(assignment, state['scenario'])}",
        use_tools=False)
    _post(state, "dm", "coworker", text)
    return {"phase": "work"}


def route_entry(state: TurnState) -> str:
    trigger = state["trigger"]
    if trigger["kind"] == "scene":
        is_event = bool(scene_by_id(trigger["scene_id"], state["scenario"])["element"])
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
         "scenario": s["scenario"],
         "negotiation_round": s["negotiation_round"], "assignment": s["assignment"],
         "requested": {}, "agreed": False, "about_split": False},
        {"configurable": {"thread_id": session_id},
         "recursion_limit": config()["agent_loop"]["max_turns"]})
