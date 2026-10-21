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

from app import agent_client, auth, clock, engine, settings, store, work
from app import docs as docs_module
from app import mcp_client

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 재시작 전 "생성 중"이던 리포트를 백그라운드로 다시 만든다(F-34)
    task = asyncio.create_task(agent_client.resume_reports())
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
    scenario: str = ""      # 회사의 업무 시나리오 팩. 가입할 때 한 번 정한다. 비우면 기본 팩(공통 PRD)


class LoginIn(BaseModel):
    email: str
    password: str


class InviteIn(BaseModel):
    alias: str
    email: str = ""


@app.post("/api/auth/signup")
def signup(body: SignupIn) -> dict:
    try:
        return {"token": auth.signup(body.email, body.password, body.company, body.scenario)}
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


_PUBLIC_KEYS = ("id", "company", "company_desc", "service", "service_desc", "task_title",
                "lead_name", "lead_short", "lead_role", "coworker_name", "coworker_short", "coworker_role")


def _pack_public(pack: str) -> dict:
    """화면에 보여줄 시나리오 팩 정보(회사·서비스·등장인물). RANDOM_PACK은 실제 팩이 아니라 안내 문구만."""
    if pack == settings.RANDOM_PACK:
        return {**{k: "" for k in _PUBLIC_KEYS}, "id": pack, "company": "지원자마다 랜덤",
                "task_title": f"등록된 시나리오 {len(settings.pack_ids())}개 중 하나를 지원자마다 무작위로 배정"}
    info = settings.pack_info(pack)
    return {k: info[k] for k in _PUBLIC_KEYS}


@app.get("/api/scenarios")
def scenarios() -> list[dict]:
    """가입 화면에서 고를 시나리오 목록(팩들 + 지원자마다 랜덤). 첫 번째가 기본. 가상 회사 정보뿐이라 로그인 없이 보인다."""
    return [_pack_public(p) for p in [*settings.pack_ids(), settings.RANDOM_PACK]]


@app.get("/api/admin/scenario")
def company_scenario(admin: dict = Depends(_admin)) -> dict:
    """이 기업의 시나리오(가입할 때 정해지고 바뀌지 않는다). 랜덤이면 지원자별 팩은 지원자 목록에 보인다."""
    return {"current": _pack_public(store.company_scenario(admin["company"]))}


@app.get("/api/admin/candidates")
def candidates(admin: dict = Depends(_admin)) -> list[dict]:
    """지원자 목록과 상태(F-03)."""
    rows = store.list_sessions(admin["company"])
    for r in rows:
        r["invite_path"] = f"/invite/{store.get_session(r['id'])['invite_token']}"
        if r["status"] == "failed":   # 실패 원인과 trace 찾는 방법(Harness 표 6-2)
            r["error"] = (store.get_report(r["id"]) or {}).get("error") or ""
    return rows


@app.get("/api/admin/reports/{sid}")
def get_report(sid: str, admin: dict = Depends(_admin)) -> dict:
    """리포트(F-04)와 근거 원문 이동용 전체 대화(F-05)."""
    s = _own_session(sid, admin)
    rep = store.get_report(sid)
    return {"alias": s["alias"], "status": s["status"], "names": settings.names(s["scenario"]),
            "pack": _pack_public(s["scenario"]),
            "report": rep["body"] if rep and rep["state"] == "ready" else None,
            "report_state": rep["state"] if rep else None,
            "messages": [_msg(m) for m in store.list_messages(sid)]}


@app.post("/api/admin/reports/{sid}/retry")
async def retry_report(sid: str, admin: dict = Depends(_admin)) -> dict:
    """자동 재시도(F-34) 끝에 '실패'가 된 리포트를 관리자가 다시 만들게 한다(원인 해결 뒤 수동 재시도)."""
    _own_session(sid, admin)
    if not store.reset_failed_report(sid):
        raise HTTPException(409, "실패 상태인 리포트만 다시 만들 수 있습니다.")
    engine.schedule_report(sid)
    return {"ok": True}


# ---------- 지원자 ----------

class MessageIn(BaseModel):
    channel: str
    text: str


class TextIn(BaseModel):
    text: str


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
            "consented": bool(s["consented"]), "started": s["status"] != "invited",
            "pack": _pack_public(s["scenario"])}


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
    """화면이 2초마다 부른다. 시계를 돌리고 새 메시지·업무 목록·문서 상태를 돌려준다."""
    s = _candidate(token, need_active=False)
    if s["status"] == "invited":
        raise HTTPException(400, "아직 시작하지 않았습니다.")
    state = await engine.tick(s["id"])
    s = store.get_session(s["id"])
    return {
        **state, "time": clock.format_minute(state["minute"]), "phase": s["phase"],
        "messages": [_msg(m) for m in store.list_messages(s["id"], after_id=after)],
        "tasks": store.list_tasks(s["id"]),
        "progress": _progress_view(s),
        "can_finish": engine.can_finish(s["id"]),
        "work_intro": settings.work_spec(s["scenario"])["intro"],
        "memo_max_chars": settings.config()["work"]["memo_max_chars"],
        "docs": [{"id": d["id"], "title": d["title"]}
                 for d in docs_module.available(s["company"], state["minute"], s["scenario"])],
        "part_titles": settings.part_titles(s["scenario"]),
        "names": settings.names(s["scenario"]),
        "pack": _pack_public(s["scenario"]),
        "activity": await agent_client.activity(s["id"]),   # 진행 이벤트: AI가 지금 하는 일
        "poll_interval_seconds": settings.config()["session"]["poll_interval_seconds"],
        "time_scale": settings.config()["session"]["time_scale"],
    }


def _progress_view(s: dict) -> dict:
    """업무 번호 → {done, units, visible}. 동료가 맡은 업무는 지원자가 메운 칸이 있을 때만 진척을 보인다.

    동료 진척(늘 0)을 그대로 보이면 보드만 봐도 동료가 일을 못 하는 게 드러나, 진행 상황을 묻는 행동을 볼 수 없다.
    """
    done, need = work.progress(s["id"], s["scenario"]), work.units(s["scenario"])
    return {p: {"done": done[p], "units": need[p],
                "visible": s["assignment"].get(p) != "coworker" or done[p] > 0} for p in need}


@app.post("/api/invite/{token}/messages")
async def send(token: str, body: MessageIn) -> dict:
    """메신저(F-12): 팀 채널(team), 동료 1:1(dm), 팀장 1:1(lead_dm)."""
    s = _candidate(token)
    try:
        await engine.send_message(s["id"], body.channel, body.text.strip())
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.post("/api/invite/{token}/work")
async def work_step(token: str, body: TextIn) -> dict:
    """작업 계속하기: 업무 메모를 남기면 정해진 지문이 이어지고 시간이 넘어간다(업무는 직접 하지 않음)."""
    s = _candidate(token)
    try:
        return await engine.work_step(s["id"], body.text)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/invite/{token}/docs/{doc_id}")
async def read_doc(token: str, doc_id: str) -> dict:
    """회사 문서 열람(F-15): rag-mcp Resource로 원문을 읽는다. 공개 시각 판단은 MCP 서버가 한다."""
    s = _candidate(token, need_active=False)
    doc = {d["id"]: d for d in docs_module.all_docs(s["company"], s["scenario"])}.get(doc_id)
    if doc is None:
        raise HTTPException(404, "없는 문서입니다.")
    try:
        body = await mcp_client.read_doc_resource(s["id"], doc_id)
    except PermissionError as exc:
        raise HTTPException(403, f"'{doc['title']}' 문서는 아직 공유되지 않았습니다.") from exc
    except LookupError as exc:
        raise HTTPException(404, "없는 문서입니다.") from exc
    return {"id": doc_id, "title": doc["title"], "body": body}


@app.post("/api/invite/{token}/submit")
async def submit(token: str) -> dict:
    """업무 마치기(F-17). 팀장의 보고 요청 뒤에만 가능하다. 세션을 끝내고 리포트 생성을 시작한다(F-32)."""
    s = _candidate(token)
    if not engine.can_finish(s["id"]):
        raise HTTPException(400, "팀장님이 보고를 요청한 뒤에 업무를 마칠 수 있어요.")
    engine.finish(s["id"])
    return {"ok": True}


@app.get("/api/invite/{token}/debrief")
def debrief(token: str) -> dict:
    """디브리핑(F-18): 연출된 상황과 리포트 처리 방식. 끝난 세션만 볼 수 있다."""
    s = _candidate(token, need_active=False)
    if s["status"] in ("invited", "in_progress"):
        raise HTTPException(400, "평가가 끝난 뒤에 볼 수 있습니다.")
    events = [{"time": clock.format_minute(sc["at_minute"]), "element": sc["element"],
               "message": sc["debrief"]} for sc in settings.scenes(s["scenario"]) if sc.get("debrief")]
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
    choice = store.company_scenario(admin["company"])
    if choice == settings.RANDOM_PACK:   # 랜덤이면 모든 팩의 문서를 팩 회사 이름을 붙여 보여 준다
        docs = [{**d, "title": f"[{settings.pack_info(p)['company']}] {d['title']}"}
                for p in settings.pack_ids() for d in docs_module.all_docs(admin["company"], p)]
    else:
        docs = docs_module.all_docs(admin["company"], choice)
    return {"docs": docs, "requests": store.doc_requests(admin["company"])}


@app.post("/api/admin/docs")
async def request_doc(body: DocIn, admin: dict = Depends(_admin)) -> dict:
    try:
        return await agent_client.request_doc(admin["company"], body.doc_id.strip(), body.title, body.body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/admin/docs/{request_id}/decision")
async def decide_doc(request_id: str, body: DecisionIn, admin: dict = Depends(_admin)) -> dict:
    try:
        return {"result": await agent_client.decide_doc(request_id, admin["company"], body.approved)}
    except KeyError as exc:
        raise HTTPException(404, "없는 요청입니다.") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
