"""에이전트 서버 (레퍼런스의 FastAPI Agent): LLM을 부르는 일은 모두 여기서 한다.

gateway(app/api.py)는 LLM을 부르지 않고 app/agent_client.py로 이 서버를 부른다. 이 서버는 DB에 직접 쓰지 않고
업무 데이터 변경은 business-mcp 변경 도구로 한다(tests/test_architecture.py).
실행: uv run uvicorn app.agent_server:app --port 8001   (포트는 config [agent].port)
"""
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import doc_graph, graph, progress, report

log = logging.getLogger("dayzero.agent_server")
app = FastAPI(title="Day Zero Agent")


class TurnIn(BaseModel):
    session_id: str
    minute: float
    trigger: dict


class DocRequestIn(BaseModel):
    company: str
    doc_id: str
    title: str
    body: str


class DocDecisionIn(BaseModel):
    request_id: str
    company: str
    approved: bool


def _http_error(exc: Exception) -> HTTPException:
    """에이전트 함수의 예외를 gateway가 같은 종류로 되살릴 수 있는 상태 코드로 바꾼다(app/agent_client.STATUS_ERROR)."""
    code = 400 if isinstance(exc, ValueError) else 404 if isinstance(exc, KeyError) else 409
    return HTTPException(code, str(exc))


@app.post("/agent/turn")
async def turn(body: TurnIn) -> dict:
    """장면 하나 또는 지원자 메시지 하나를 세션 Graph로 처리한다."""
    await graph.run_turn(body.session_id, body.minute, body.trigger)
    return {"ok": True}


@app.post("/agent/reports/resume")
async def resume_reports() -> dict:
    """재시작 전 멈춘 리포트를 다시 만든다(F-34)."""
    return {"resumed": await report.resume_stale()}


@app.post("/agent/reports/{session_id}")
async def generate_report(session_id: str) -> dict:
    """리포트를 만들고 최종 상태를 돌려준다(재시도 포함)."""
    return {"state": await report.generate_with_retry(session_id)}


@app.get("/agent/progress/{session_id}")
def activity(session_id: str) -> dict:
    """진행 이벤트(에이전트가 지금 하는 일)."""
    return {"activity": progress.get(session_id)}


@app.post("/agent/docs/request")
async def request_doc(body: DocRequestIn) -> dict:
    try:
        return await doc_graph.request(body.company, body.doc_id, body.title, body.body)
    except (ValueError, KeyError, RuntimeError) as exc:
        raise _http_error(exc) from exc


@app.post("/agent/docs/decide")
async def decide_doc(body: DocDecisionIn) -> dict:
    try:
        return {"result": await doc_graph.decide(body.request_id, body.company, body.approved)}
    except (ValueError, KeyError, RuntimeError) as exc:
        raise _http_error(exc) from exc
