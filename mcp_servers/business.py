"""business-mcp: 세션·태스크 업무 데이터 MCP 서버 (직접 구축, stdio).

- 조회 도구: get_session_status, list_tasks
- 변경 도구: assign_tasks, update_task_status (destructiveHint로 표시)
- 관찰 기록 저장 도구는 관찰 단계에서 추가한다.

PRD 부록은 gateway 내부 API를 거치지만, MVP는 같은 SQLite 파일을 직접 읽는다(README에 기록).
실행: uv run python -m mcp_servers.business
"""
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
