"""business-mcp: 세션·태스크 업무 데이터 MCP 서버 (직접 구축, Streamable HTTP :9101 또는 stdio).

- 조회 도구: get_session_status, list_tasks, list_observations, list_work_log(업무 메모·지문)
- 변경 도구: assign_tasks, update_task_status, save_observation,
            post_message, update_session_state, claim_scene, claim_report, record_report_attempt, finish_report,
            save_doc_request, set_doc_request_status (destructiveHint로 표시)
- 에이전트(graph·report·doc_graph)는 DB에 직접 쓰지 않고 이 변경 도구만 쓴다(레퍼런스 금지 사항, tests/test_architecture.py).
  값 검사(채널·발신자·단계·분업·상태)는 서버가 한다 → 모델·에이전트 코드가 잘못된 값을 넣어도 저장되지 않는다.

PRD 부록은 gateway 내부 API를 거치지만, MVP는 같은 SQLite 파일을 직접 읽는다(README에 기록).
실행: uv run python -m mcp_servers.business --http   (인자 없이 실행하면 stdio)
"""
import json

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app import clock, store

mcp = FastMCP("dayzero-business")

VALID_OWNERS = {"candidate", "coworker", "both"}
AGENT_CHANNELS = {"team", "dm", "lead_dm", "work", "system"}
AGENT_SENDERS = {"coworker", "lead", "narrator", "system"}   # candidate 없음: 에이전트가 지원자 말을 지어 넣지 못하게
AGENT_PHASES = {"negotiate", "work"}
DOC_REQUEST_RESULTS = {"registered", "rejected", "failed"}
CHANGE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_session_status(session_id: str) -> dict:
    """세션의 단계, 경과 시각, 분업 결과를 반환한다."""
    s = store.get_session(session_id)
    return {"session_id": s["id"], "phase": s["phase"],
            "elapsed": clock.format_minute(clock.minute_of(s["sim_seconds"])),
            "assignment": s["assignment"]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_tasks(session_id: str) -> list[dict]:
    """태스크 보드 카드(파트, 제목, 담당자, 상태)를 반환한다."""
    store.get_session(session_id)   # 없는 세션이면 KeyError → 오류 응답
    return [{k: t[k] for k in ("part", "title", "owner", "status")}
            for t in store.list_tasks(session_id)]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_work_log(session_id: str) -> list[dict]:
    """업무 기록(지원자의 업무 메모와 그 결과 지문)을 시각 순으로 반환한다. 리포트·검토용(읽기 전용)."""
    store.get_session(session_id)
    return [{"time": clock.format_minute(m["minute"]), "sender": m["sender"], "content": m["content"]}
            for m in store.list_messages(session_id, channels=("work",))]


@mcp.tool(annotations=CHANGE)
def assign_tasks(session_id: str, assignment: dict[str, str]) -> dict:
    """분업 결과를 태스크 보드 담당자에 반영한다. assignment: 파트 번호("1"~"5") → candidate|coworker|both."""
    store.get_session(session_id)
    bad_parts = set(assignment) - {"1", "2", "3", "4", "5"}
    bad_owners = set(assignment.values()) - VALID_OWNERS
    if bad_parts or bad_owners:
        raise ValueError(f"잘못된 분업 값: 파트 {sorted(bad_parts)}, 담당자 {sorted(bad_owners)}")
    store.set_task_owners(session_id, assignment)
    return {"ok": True, "assignment": assignment}


@mcp.tool(annotations=CHANGE)
def update_task_status(session_id: str, part: str, status: str) -> dict:
    """태스크 카드 상태를 todo | doing | done 중 하나로 바꾼다."""
    store.get_session(session_id)
    store.set_task_status(session_id, part, status)
    return {"ok": True, "part": part, "status": status}


@mcp.tool(annotations=CHANGE)
def save_observation(session_id: str, element: str, result_json: str) -> dict:
    """요소별 관찰 결과(JSON 문자열)를 저장한다. 리포트 생성 과정에서 코드가 호출한다."""
    store.get_session(session_id)
    try:
        result = json.loads(result_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"result_json이 올바른 JSON이 아닙니다: {exc}") from exc
    store.save_observation(session_id, element, result)
    return {"ok": True, "element": element}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_observations(session_id: str) -> dict:
    """저장된 관찰 결과를 요소별로 반환한다."""
    store.get_session(session_id)
    return store.list_observations(session_id)


# ---------- 에이전트가 쓰는 변경 도구 (레퍼런스 금지 사항: Agent는 DB에 직접 쓰지 않는다) ----------

@mcp.tool(annotations=CHANGE)
def post_message(session_id: str, channel: str, sender: str, text: str, minute: float) -> dict:
    """동료·팀장의 메시지나 업무 지문을 대화 기록에 남긴다. 에이전트는 지원자 이름으로 쓸 수 없다."""
    store.get_session(session_id)
    if channel not in AGENT_CHANNELS or sender not in AGENT_SENDERS:
        raise ValueError(f"에이전트가 쓸 수 없는 채널·발신자: {channel}/{sender} "
                         f"(채널 {sorted(AGENT_CHANNELS)}, 발신자 {sorted(AGENT_SENDERS)})")
    return {"ok": True, "message_id": store.add_message(session_id, channel, sender, text, minute)}


@mcp.tool(annotations=CHANGE)
def update_session_state(session_id: str, phase: str = "", assignment: dict[str, str] | None = None,
                         negotiation_round: int | None = None) -> dict:
    """세션 진행 단계(negotiate | work)·분업안·조정 횟수를 바꾼다. 비운 값은 그대로 둔다."""
    store.get_session(session_id)
    fields: dict = {}
    if phase:
        if phase not in AGENT_PHASES:
            raise ValueError(f"잘못된 단계: {phase} (가능: {sorted(AGENT_PHASES)})")
        fields["phase"] = phase
    if assignment is not None:
        if set(assignment) - {"1", "2", "3", "4", "5"} or set(assignment.values()) - VALID_OWNERS:
            raise ValueError(f"잘못된 분업 값: {assignment}")
        fields["assignment"] = assignment
    if negotiation_round is not None:
        fields["negotiation_round"] = negotiation_round
    if fields:
        store.update_session(session_id, **fields)
    return {"ok": True, "updated": sorted(fields)}


@mcp.tool(annotations=CHANGE)
def claim_scene(session_id: str, scene_key: str, minute: float) -> dict:
    """장면·후속 반응 실행 권한을 얻는다. 처음 부른 쪽만 claimed=true(같은 반응이 두 번 나가지 않게)."""
    store.get_session(session_id)
    return {"claimed": store.claim_scene(session_id, scene_key, minute)}


@mcp.tool(annotations=CHANGE)
def claim_report(session_id: str) -> dict:
    """리포트 생성 권한을 얻는다. 처음 부른 쪽만 claimed=true(중복 생성 방지)."""
    store.get_session(session_id)
    return {"claimed": store.claim_report(session_id)}


@mcp.tool(annotations=CHANGE)
def record_report_attempt(session_id: str, attempt: int, error: str) -> dict:
    """리포트 생성 실패 1회를 기록한다(재시도 횟수와 원인, F-34)."""
    store.get_session(session_id)
    store.update_report(session_id, attempts=attempt, error=error)
    return {"ok": True}


@mcp.tool(annotations=CHANGE)
def finish_report(session_id: str, state: str, attempts: int = 0, body_json: str = "") -> dict:
    """리포트를 ready(본문 저장) 또는 failed로 끝내고 세션 상태(report_ready | failed)도 맞춘다."""
    store.get_session(session_id)
    if state == "ready":
        try:
            body = json.loads(body_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"body_json이 올바른 JSON이 아닙니다: {exc}") from exc
        store.update_report(session_id, state="ready", attempts=attempts, body=body, error=None)
        store.update_session(session_id, status="report_ready")
    elif state == "failed":
        store.update_report(session_id, state="failed")
        store.update_session(session_id, status="failed")
    else:
        raise ValueError(f"잘못된 리포트 상태: {state} (ready | failed)")
    return {"ok": True, "state": state}


@mcp.tool(annotations=CHANGE)
def save_doc_request(request_id: str, company: str, doc_id: str, title: str, body: str,
                     masked_count: int) -> dict:
    """관리자의 회사 문서 등록 요청(개인정보를 가린 본문)을 승인 대기로 남긴다(F-06)."""
    store.save_doc_request(request_id, company, doc_id, title, body, masked_count)
    return {"ok": True, "request_id": request_id}


@mcp.tool(annotations=CHANGE)
def set_doc_request_status(request_id: str, status: str) -> dict:
    """문서 등록 요청의 결과(registered | rejected | failed)를 남긴다."""
    if status not in DOC_REQUEST_RESULTS:
        raise ValueError(f"잘못된 요청 상태: {status} (가능: {sorted(DOC_REQUEST_RESULTS)})")
    store.set_doc_request_status(request_id, status)
    return {"ok": True, "status": status}


if __name__ == "__main__":
    from mcp_servers import serve
    serve(mcp, "business")
