"""FastAPI gateway: 화면(web/)이 부르는 REST API.

- 관리자 API(/api/admin/*): Authorization: Bearer <토큰>. 자기 기업 세션만 다룬다(F-01).
- 지원자 API(/api/invite/{token}/*): 초대 토큰이 곧 인증이다(F-02, F-10).
- 화면은 2초마다 /poll을 부른다. 그때 세션 시계가 흐르고 시각이 된 장면이 실행된다(engine.tick).

실행: uv run uvicorn app.api:app --port 8000
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from app import auth, clock, doc_graph, engine, progress, report, settings, store
from app import docs as docs_module

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 재시작 전 "생성 중"이던 리포트를 백그라운드로 다시 만든다(F-34)
    task = asyncio.create_task(report.resume_stale())
    yield
    task.cancel()


app = FastAPI(title="Day Zero MVP", lifespan=lifespan)


# ---------- 공통 ----------

def _admin(authorization: str = Header(default="")) -> dict:
    try:
        return auth.verify_token(authorization.removeprefix("Bearer ").strip())
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc


def _own_session(sid: str, admin: dict) -> dict:
    try:
        s = store.get_session(sid)
    except KeyError as exc:
        raise HTTPException(404, "없는 지원자입니다.") from exc
    if s["company"] != admin["company"]:   # 다른 기업 데이터는 없는 것처럼 처리(F-01)
        raise HTTPException(404, "없는 지원자입니다.")
    return s


def _candidate(token: str, need_active: bool = True) -> dict:
    state, s = auth.invite_state(token)
    if state == "not_found":
        raise HTTPException(404, "잘못된 링크입니다.")
    if state == "expired":
        raise HTTPException(410, "링크가 만료되었습니다. 채용 담당자에게 문의해 주세요.")
    if need_active and state == "completed":
        raise HTTPException(409, "이미 완료된 평가입니다.")
    return s


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


# ---------- 관리자 ----------

class SignupIn(BaseModel):
    email: str
    password: str
    company: str


class LoginIn(BaseModel):
    email: str
    password: str


class InviteIn(BaseModel):
    alias: str
    email: str = ""


@app.post("/api/auth/signup")
def signup(body: SignupIn) -> dict:
    try:
        return {"token": auth.signup(body.email, body.password, body.company)}
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/auth/login")
def login(body: LoginIn) -> dict:
    try:
        return {"token": auth.login(body.email, body.password)}
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc


@app.get("/api/admin/me")
def me(admin: dict = Depends(_admin)) -> dict:
    return admin


@app.post("/api/admin/invites")
def create_invite(body: InviteIn, admin: dict = Depends(_admin)) -> dict:
    try:
        return auth.create_invite(admin["company"], body.alias, body.email)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/admin/candidates")
def candidates(admin: dict = Depends(_admin)) -> list[dict]:
    """지원자 목록과 상태(F-03)."""
    rows = store.list_sessions(admin["company"])
    for r in rows:
        r["invite_path"] = f"/invite/{store.get_session(r['id'])['invite_token']}"
    return rows


@app.get("/api/admin/reports/{sid}")
def get_report(sid: str, admin: dict = Depends(_admin)) -> dict:
    """리포트(F-04)와 근거 원문 이동용 전체 대화(F-05)."""
    s = _own_session(sid, admin)
    rep = store.get_report(sid)
    return {"alias": s["alias"], "status": s["status"],
            "report": rep["body"] if rep and rep["state"] == "ready" else None,
            "report_state": rep["state"] if rep else None,
            "messages": [_msg(m) for m in store.list_messages(sid)]}


# ---------- 지원자 ----------

class MessageIn(BaseModel):
    channel: str
    text: str


class TextIn(BaseModel):
    text: str


class PartIn(BaseModel):
    content: str


class TaskIn(BaseModel):
    status: str


def _msg(m: dict) -> dict:
    return {"id": m["id"], "channel": m["channel"], "sender": m["sender"], "content": m["content"],
            "time": clock.format_minute(m["minute"])}


@app.get("/api/invite/{token}")
def invite_info(token: str) -> dict:
    """링크 입장(F-10): 유효 여부, 회사 이름, 소요 시간."""
    state, s = auth.invite_state(token)
    if s is None:
        raise HTTPException(404, "잘못된 링크입니다.")
    return {"state": state, "company": s["company"], "alias": s["alias"],
            "duration_minutes": settings.config()["session"]["duration_minutes"],
            "consented": bool(s["consented"]), "started": s["status"] != "invited"}


@app.post("/api/invite/{token}/consent")
def consent(token: str) -> dict:
    """안내·동의(F-11). 동의해야 시작할 수 있다."""
    s = _candidate(token)
    store.update_session(s["id"], consented=1)
    return {"ok": True}


@app.post("/api/invite/{token}/start")
async def start(token: str) -> dict:
    s = _candidate(token)
    if not s["consented"]:
        raise HTTPException(400, "안내에 동의해야 시작할 수 있습니다.")
    if s["status"] == "invited":
        store.update_session(s["id"], status="in_progress", last_seen=None)
    return await poll(token, 0)


@app.get("/api/invite/{token}/poll")
async def poll(token: str, after: int = 0) -> dict:
    """화면이 2초마다 부른다. 시계를 돌리고 새 메시지·보드·문서 상태를 돌려준다."""
    s = _candidate(token, need_active=False)
    if s["status"] == "invited":
        raise HTTPException(400, "아직 시작하지 않았습니다.")
    state = await engine.tick(s["id"])
    s = store.get_session(s["id"])
    parts = store.get_parts(s["id"])
    return {
        **state, "time": clock.format_minute(state["minute"]), "phase": s["phase"],
        "messages": [_msg(m) for m in store.list_messages(s["id"], after_id=after)],
        "tasks": store.list_tasks(s["id"]),
        "my_parts": {k: v for k, v in parts.items() if k in settings.PART_TITLES},
        "coworker_parts": {k.split(":")[1]: v for k, v in parts.items() if k.startswith("coworker:")},
        "docs": [{"id": d["id"], "title": d["title"]}
                 for d in docs_module.available(s["company"], state["minute"])],
        "part_titles": settings.PART_TITLES,
        "activity": progress.get(s["id"]),   # 진행 이벤트: AI가 지금 하는 일
        "poll_interval_seconds": settings.config()["session"]["poll_interval_seconds"],
        "time_scale": settings.config()["session"]["time_scale"],
    }


@app.post("/api/invite/{token}/messages")
async def send(token: str, body: MessageIn) -> dict:
    """메신저(F-12): 팀 채널(team) 또는 동료 1:1(dm)."""
    s = _candidate(token)
    try:
        await engine.send_message(s["id"], body.channel, body.text.strip())
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.post("/api/invite/{token}/assistant")
async def ask(token: str, body: TextIn) -> dict:
    """AI 어시스턴트(F-16)."""
    s = _candidate(token)
    try:
        return {"answer": await engine.ask_assistant(s["id"], body.text.strip())}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/invite/{token}/parts/{part}")
def save_part(token: str, part: str, body: PartIn) -> dict:
    """공유 문서 자동 저장(F-13)."""
    s = _candidate(token)
    try:
        engine.save_part(s["id"], part, body.content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.put("/api/invite/{token}/tasks/{part}")
def set_task(token: str, part: str, body: TaskIn) -> dict:
    """태스크 보드(F-14): 지원자는 자기 카드(지원자·함께) 상태만 바꿀 수 있다."""
    s = _candidate(token)
    owner = {t["part"]: t["owner"] for t in store.list_tasks(s["id"])}.get(part)
    if owner not in ("candidate", "both"):
        raise HTTPException(403, "자기 카드만 바꿀 수 있습니다.")
    try:
        store.set_task_status(s["id"], part, body.status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.get("/api/invite/{token}/docs/{doc_id}")
def read_doc(token: str, doc_id: str) -> dict:
    """회사 문서 열람(F-15): 공개 시각 전 문서는 열 수 없다."""
    s = _candidate(token, need_active=False)
    minute = clock.minute_of(s["sim_seconds"])
    doc = {d["id"]: d for d in docs_module.all_docs(s["company"])}.get(doc_id)
    if doc is None:
        raise HTTPException(404, "없는 문서입니다.")
    if doc["available_from_minute"] > minute:
        raise HTTPException(403, f"'{doc['title']}' 문서는 아직 공유되지 않았습니다.")
    return {"id": doc_id, "title": doc["title"], "body": docs_module.body(s["company"], doc_id)}


@app.post("/api/invite/{token}/submit")
async def submit(token: str) -> dict:
    """제출(F-17). 세션을 끝내고 리포트 생성을 시작한다(F-32)."""
    s = _candidate(token)
    engine.finish(s["id"])
    return {"ok": True}


@app.get("/api/invite/{token}/debrief")
def debrief(token: str) -> dict:
    """디브리핑(F-18): 연출된 상황 4개와 리포트 처리 방식. 끝난 세션만 볼 수 있다."""
    s = _candidate(token, need_active=False)
    if s["status"] in ("invited", "in_progress"):
        raise HTTPException(400, "평가가 끝난 뒤에 볼 수 있습니다.")
    events = [{"time": clock.format_minute(sc["at_minute"]), "element": sc["element"],
               "message": sc["message"]} for sc in settings.scenes() if sc["element"]]
    return {"events": events,
            "notice": "위 상황은 평가를 위해 연출된 것입니다. 리포트는 점수 없이 관찰 기록만 담아 "
                      "채용 담당자에게 전달되며, 최종 판단은 사람이 합니다."}


# ---------- 회사 문서 관리 (F-06): 등록 요청 → 승인 대기(interrupt) → 승인 시 재개 ----------

class DocIn(BaseModel):
    doc_id: str
    title: str
    body: str


class DecisionIn(BaseModel):
    approved: bool


@app.get("/api/admin/docs")
def list_docs(admin: dict = Depends(_admin)) -> dict:
    """현재 문서 목록(시나리오 + 승인된 기업 문서)과 등록 요청 목록."""
    return {"docs": docs_module.all_docs(admin["company"]),
            "requests": store.doc_requests(admin["company"])}


@app.post("/api/admin/docs")
async def request_doc(body: DocIn, admin: dict = Depends(_admin)) -> dict:
    try:
        return await doc_graph.request(admin["company"], body.doc_id.strip(), body.title, body.body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/admin/docs/{request_id}/decision")
async def decide_doc(request_id: str, body: DecisionIn, admin: dict = Depends(_admin)) -> dict:
    try:
        return {"result": await doc_graph.decide(request_id, admin["company"], body.approved)}
    except KeyError as exc:
        raise HTTPException(404, "없는 요청입니다.") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
