"""business-mcp: 세션·태스크 업무 데이터 MCP 서버 (직접 구축, stdio).

- 조회 도구: get_session_status, list_tasks, read_shared_plan(동료·팀장이 기획안 읽기)
- 조회 도구: list_observations
- 변경 도구: assign_tasks, update_task_status, save_observation (destructiveHint로 표시)

PRD 부록은 gateway 내부 API를 거치지만, MVP는 같은 SQLite 파일을 직접 읽는다(README에 기록).
실행: uv run python -m mcp_servers.business
"""
import json

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app import clock, store

mcp = FastMCP("dayzero-business")

VALID_OWNERS = {"candidate", "coworker", "both"}
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
def read_shared_plan(session_id: str) -> dict:
    """공유 문서(기획안)를 읽는다. 파트별 담당자, 지원자가 쓴 내용, 동료가 공유한 내용을 반환한다.

    동료·팀장이 지원자의 기획안을 보고 피드백할 때 쓴다(읽기 전용, 편집은 지원자만).
    """
    s = store.get_session(session_id)
    parts = store.get_parts(session_id)
    return {"parts": [{"part": p, "title": t["title"], "owner": s["assignment"].get(p, ""),
                       "candidate_text": parts.get(p, ""), "coworker_text": parts.get(f"coworker:{p}", "")}
                      for p, t in ((t["part"], t) for t in store.list_tasks(session_id))]}


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


if __name__ == "__main__":
    mcp.run(transport="stdio")
