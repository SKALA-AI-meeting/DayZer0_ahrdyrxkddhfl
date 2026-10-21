"""gateway → 에이전트 호출 창구. gateway(api·engine)는 에이전트 모듈을 직접 부르지 않고 이것만 쓴다.

config [agent].mode
- http: 에이전트 서버(app/agent_server.py, 별도 프로세스)에 HTTP로 부른다(레퍼런스: 업무 서버와 FastAPI Agent 분리).
- inprocess: 같은 프로세스에서 바로 부른다(테스트·터미널 시뮬레이션용).
어느 쪽이든 결과와 예외 종류(ValueError·KeyError·RuntimeError)는 같다.
"""
import httpx

from app.settings import config

# 에이전트 서버 HTTP 상태 → gateway에서 다시 올릴 예외(에이전트 함수가 올린 예외 종류를 그대로 되살린다)
STATUS_ERROR = {400: ValueError, 404: KeyError, 409: RuntimeError}


def _http() -> bool:
    return config()["agent"]["mode"] == "http"


async def _post(path: str, payload: dict | None = None, timeout_key: str = "request_timeout_seconds") -> dict:
    """에이전트 서버에 POST하고 JSON을 돌려준다.

    Raises:
        ValueError | KeyError | RuntimeError: 에이전트가 같은 예외로 거절한 경우(400·404·409).
        httpx.HTTPError: 에이전트 서버에 닿지 못했거나 그 밖의 오류.
    """
    cfg = config()["agent"]
    async with httpx.AsyncClient(timeout=cfg[timeout_key]) as client:
        res = await client.post(f"http://{cfg['host']}:{cfg['port']}/agent{path}", json=payload or {})
    if res.status_code in STATUS_ERROR:
        raise STATUS_ERROR[res.status_code](res.json().get("detail", ""))
    res.raise_for_status()
    return res.json()


async def run_turn(session_id: str, minute: float, trigger: dict) -> None:
    """장면 하나 또는 지원자 메시지 하나를 세션 Graph로 처리한다."""
    if _http():
        await _post("/turn", {"session_id": session_id, "minute": minute, "trigger": trigger})
        return
    from app import graph
    await graph.run_turn(session_id, minute, trigger)


async def generate_report(session_id: str) -> str:
    """리포트를 만들고 최종 상태(ready | failed | skipped)를 돌려준다. gateway는 백그라운드 작업으로 부른다."""
    if _http():
        return (await _post(f"/reports/{session_id}", timeout_key="report_timeout_seconds"))["state"]
    from app import report
    return await report.generate_with_retry(session_id)


async def resume_reports() -> list[str]:
    """재시작 전 멈춘 리포트를 다시 만든다(F-34)."""
    if _http():
        return (await _post("/reports/resume", timeout_key="report_timeout_seconds"))["resumed"]
    from app import report
    return await report.resume_stale()


async def activity(session_id: str) -> dict | None:
    """진행 이벤트: 에이전트가 지금 하는 일(예: 동료가 문서를 읽는 중)."""
    if _http():
        cfg = config()["agent"]
        async with httpx.AsyncClient(timeout=cfg["request_timeout_seconds"]) as client:
            res = await client.get(f"http://{cfg['host']}:{cfg['port']}/agent/progress/{session_id}")
        res.raise_for_status()
        return res.json()["activity"]
    from app import progress
    return progress.get(session_id)


async def request_doc(company: str, doc_id: str, title: str, body: str) -> dict:
    """회사 문서 등록 요청을 문서 등록 Graph로 시작한다(승인 대기에서 멈춤, F-06)."""
    if _http():
        return await _post("/docs/request", {"company": company, "doc_id": doc_id, "title": title, "body": body})
    from app import doc_graph
    return await doc_graph.request(company, doc_id, title, body)


async def decide_doc(request_id: str, company: str, approved: bool) -> str:
    """관리자 결정으로 멈춘 문서 등록 Graph를 재개한다."""
    if _http():
        return (await _post("/docs/decide", {"request_id": request_id, "company": company,
                                             "approved": approved}))["result"]
    from app import doc_graph
    return await doc_graph.decide(request_id, company, approved)
