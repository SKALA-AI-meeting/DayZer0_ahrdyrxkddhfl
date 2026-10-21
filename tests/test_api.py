"""API 테스트: 인증·초대·권한·문서 공개 시각. LLM이 필요 없는 경로만 확인한다."""
import time

from fastapi.testclient import TestClient

from app import store
from app.api import app

client = TestClient(app)


def _signup(email="hr@a.com", company="A사"):
    r = client.post("/api/auth/signup", json={"email": email, "password": "password123",
                                               "company": company})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_login_and_wrong_password():
    _signup()
    assert client.post("/api/auth/login", json={"email": "hr@a.com", "password": "password123"}).status_code == 200
    assert client.post("/api/auth/login", json={"email": "hr@a.com", "password": "nope-nope"}).status_code == 401
    assert client.get("/api/admin/candidates").status_code == 401
    assert client.get("/api/admin/candidates", headers={"Authorization": "Bearer forged.token"}).status_code == 401


def test_other_company_cannot_see_candidates_or_report():
    a = _signup("hr@a.com", "A사")
    b = _signup("hr@b.com", "B사")
    sid = client.post("/api/admin/invites", json={"alias": "지원자1"}, headers=a).json()["session_id"]
    assert len(client.get("/api/admin/candidates", headers=a).json()) == 1
    assert client.get("/api/admin/candidates", headers=b).json() == []
    assert client.get(f"/api/admin/reports/{sid}", headers=b).status_code == 404


def test_invite_flow_requires_consent_and_expires():
    a = _signup()
    path = client.post("/api/admin/invites", json={"alias": "지원자1"}, headers=a).json()["invite_path"]
    token = path.rsplit("/", 1)[1]
    info = client.get(f"/api/invite/{token}").json()
    assert info["state"] == "valid" and info["duration_minutes"] == 40
    assert client.post(f"/api/invite/{token}/start").status_code == 400   # 동의 전 시작 불가
    store.update_session(store.session_by_invite(token)["id"], invite_expires_at=time.time() - 1)
    assert client.post(f"/api/invite/{token}/consent").status_code == 410  # 72시간 만료
    assert client.get("/api/invite/없는토큰").status_code == 404


def test_docs_readable_and_completed_link(monkeypatch):
    # 공개 전 문서 차단은 MCP 서버(별도 프로세스)가 하므로 tests/test_mcp_servers.py에서 같은 프로세스로 검사한다
    a = _signup()
    token = client.post("/api/admin/invites", json={"alias": "x"}, headers=a).json()["invite_path"].rsplit("/", 1)[1]
    sid = store.session_by_invite(token)["id"]
    store.update_session(sid, status="in_progress", sim_seconds=10 * 60)
    assert client.get(f"/api/invite/{token}/docs/interviews").status_code == 200   # 지금 시나리오는 처음부터 공개
    assert client.get(f"/api/invite/{token}/docs/policy").status_code == 200
    store.update_session(sid, status="report_ready")
    assert client.post(f"/api/invite/{token}/messages", json={"channel": "dm", "text": "hi"}).status_code == 409
    events = client.get(f"/api/invite/{token}/debrief").json()["events"]
    # 디브리핑은 debrief 설명이 있는 장면만(연출된 상황), 내부 지시문(message)이 아니라 설명문을 보인다
    assert [e["time"] for e in events] == ["00:02", "00:15", "00:20", "00:22", "00:31", "00:32"]
    assert all("지원자" not in e["message"] or "연락" in e["message"] for e in events)


def test_work_step_records_memo_narration_and_moves_clock(monkeypatch):
    from app import engine, work
    async def fake_classify(sid, memo, pack=None):
        return "own", "1"

    async def fake_business(tool, args):
        store.set_task_status(args["session_id"], args["part"], args["status"])
        return "ok"
    from app import memo_agent
    monkeypatch.setattr(memo_agent, "classify", fake_classify)
    monkeypatch.setattr(engine, "call_business", fake_business)
    a = _signup()
    token = client.post("/api/admin/invites", json={"alias": "x"}, headers=a).json()["invite_path"].rsplit("/", 1)[1]
    sid = store.session_by_invite(token)["id"]
    for scene in ("kickoff", "kickoff_greeting", "negotiate"):
        store.claim_scene(sid, scene, 0)
    store.update_session(sid, status="in_progress", sim_seconds=3 * 60, phase="work")
    assert client.post(f"/api/invite/{token}/work", json={"text": ""}).status_code == 400
    assert client.post(f"/api/invite/{token}/work", json={"text": "가" * 101}).status_code == 400   # 100자 제한
    r = client.post(f"/api/invite/{token}/work", json={"text": "1번 지표 분석부터 할게요"})
    assert r.status_code == 200 and 7.9 < r.json()["minute"] < 8.5                 # 3분 → 8분(skip_minutes=5)
    log = store.list_messages(sid, channels=("work",))
    assert [m["sender"] for m in log] == ["candidate", "narrator"]
    assert log[1]["content"] == "[내 업무 진행] 1번 지표 분석: 한 칸 진행했다 (1/2칸)."
    assert store.work_memos(sid) == {log[0]["id"]: {"category": "own", "part": "1", "advanced": True}}
    poll = client.get(f"/api/invite/{token}/poll").json()
    assert poll["progress"]["1"] == {"done": 1, "units": 2, "visible": True}
    assert poll["progress"]["3"]["visible"] is False                 # 동료 업무 진척은 보이지 않음(메운 칸이 없으면)
    assert {t["part"]: t["status"] for t in poll["tasks"]}["1"] == "doing"
    assert client.put(f"/api/invite/{token}/tasks/1", json={"status": "doing"}).status_code in (404, 405)   # 카드는 읽기 전용


def test_finish_only_after_report_request():
    a = _signup()
    token = client.post("/api/admin/invites", json={"alias": "x"}, headers=a).json()["invite_path"].rsplit("/", 1)[1]
    sid = store.session_by_invite(token)["id"]
    store.update_session(sid, status="in_progress", sim_seconds=10 * 60)
    assert client.get(f"/api/invite/{token}/poll").json()["can_finish"] is False
    r = client.post(f"/api/invite/{token}/submit")
    assert r.status_code == 400 and "보고를 요청한 뒤" in r.json()["detail"]
    store.claim_scene(sid, "report_request", 32)
    assert client.post(f"/api/invite/{token}/submit").status_code == 200
    assert store.get_session(sid)["status"] != "in_progress"


def test_admin_can_retry_only_failed_report(monkeypatch):
    from app import engine
    a = _signup()
    sid = client.post("/api/admin/invites", json={"alias": "x"}, headers=a).json()["session_id"]
    started = []

    def fake_schedule(s):
        import asyncio
        asyncio.get_running_loop()      # 실제처럼 실행 중인 이벤트 루프가 있어야 한다(동기 엔드포인트면 실패)
        started.append(s)
    monkeypatch.setattr(engine, "schedule_report", fake_schedule)
    assert client.post(f"/api/admin/reports/{sid}/retry", headers=a).status_code == 409   # 실패 아님
    store.update_session(sid, status="failed")
    store.claim_report(sid)
    store.update_report(sid, state="failed")
    assert client.post(f"/api/admin/reports/{sid}/retry", headers=a).status_code == 200
    assert started == [sid] and store.get_session(sid)["status"] == "completed"


# 중간점검 확인필요(F-01): 같은 회사 이름으로 다른 사람이 가입하면 그 회사의 지원자·리포트가 보였다
def test_same_company_name_cannot_sign_up_again():
    from fastapi.testclient import TestClient
    from app.api import app
    c = TestClient(app)
    h = {"Authorization": "Bearer " + c.post("/api/auth/signup", json={
        "email": "owner@dup.kr", "password": "password123", "company": "중복 테스트사"}).json()["token"]}
    c.post("/api/admin/invites", json={"alias": "비밀지원자"}, headers=h)
    for name in ("중복 테스트사", "중복테스트사", "  중복  테스트사 "):
        r = c.post("/api/auth/signup", json={"email": "stranger@evil.kr", "password": "password123", "company": name})
        assert r.status_code == 400 and "이미 등록된 회사" in r.json()["detail"]
    r = c.post("/api/auth/signup", json={"email": "owner@dup.kr", "password": "password123", "company": "다른사"})
    assert "이미 가입한 이메일" in r.json()["detail"]
    assert c.post("/api/auth/signup", json={"email": "b@other.kr", "password": "password123",
                                            "company": "다른 회사"}).status_code == 200


def test_random_scenario_assigns_pack_per_invite(monkeypatch):
    """랜덤을 고른 회사는 초대마다 팩을 고르고, 세션에는 실제 팩 id가 저장된다."""
    import random
    from app import settings
    assert settings.RANDOM_PACK in [s["id"] for s in client.get("/api/scenarios").json()]
    r = client.post("/api/auth/signup", json={"email": "hr@rand.kr", "password": "password123",
                                               "company": "랜덤사", "scenario": settings.RANDOM_PACK})
    a = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/api/admin/scenario", headers=a).json()["current"]["id"] == settings.RANDOM_PACK
    picks = iter(settings.pack_ids())
    monkeypatch.setattr(random, "choice", lambda seq: next(picks))   # 팩을 차례로 고르게 고정
    sids = [client.post("/api/admin/invites", json={"alias": f"x{i}"}, headers=a).json()["session_id"]
            for i in range(len(settings.pack_ids()))]
    assert [store.get_session(s)["scenario"] for s in sids] == settings.pack_ids()
    titles = [d["title"] for d in client.get("/api/admin/docs", headers=a).json()["docs"]]
    assert len(titles) == sum(len(settings.docs_index(p)) for p in settings.pack_ids())


def test_fixed_scenario_is_same_for_every_invite():
    a = _signup("hr@fixed.kr", "고정사")
    sids = [client.post("/api/admin/invites", json={"alias": f"x{i}"}, headers=a).json()["session_id"]
            for i in range(3)]
    assert {store.get_session(s)["scenario"] for s in sids} == {"moarun"}
    assert client.post("/api/auth/signup", json={"email": "z@z.kr", "password": "password123",
                                                  "company": "Z사", "scenario": "없는팩"}).status_code == 400
