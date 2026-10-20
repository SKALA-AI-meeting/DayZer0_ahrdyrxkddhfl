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
    assert info["state"] == "valid" and info["duration_minutes"] == 120
    assert client.post(f"/api/invite/{token}/start").status_code == 400   # 동의 전 시작 불가
    store.update_session(store.session_by_invite(token)["id"], invite_expires_at=time.time() - 1)
    assert client.post(f"/api/invite/{token}/consent").status_code == 410  # 72시간 만료
    assert client.get("/api/invite/없는토큰").status_code == 404


def test_docs_gated_and_completed_link(monkeypatch):
    a = _signup()
    token = client.post("/api/admin/invites", json={"alias": "x"}, headers=a).json()["invite_path"].rsplit("/", 1)[1]
    sid = store.session_by_invite(token)["id"]
    store.update_session(sid, status="in_progress", sim_seconds=30 * 60)
    assert client.get(f"/api/invite/{token}/docs/interviews").status_code == 403   # 01:05 전
    assert client.get(f"/api/invite/{token}/docs/policy").status_code == 200
    store.update_session(sid, status="report_ready")
    assert client.post(f"/api/invite/{token}/messages", json={"channel": "dm", "text": "hi"}).status_code == 409
    assert len(client.get(f"/api/invite/{token}/debrief").json()["events"]) == 4


def test_task_board_only_own_cards():
    a = _signup()
    token = client.post("/api/admin/invites", json={"alias": "x"}, headers=a).json()["invite_path"].rsplit("/", 1)[1]
    store.update_session(store.session_by_invite(token)["id"], status="in_progress")
    assert client.put(f"/api/invite/{token}/tasks/1", json={"status": "doing"}).status_code == 200
    assert client.put(f"/api/invite/{token}/tasks/4", json={"status": "doing"}).status_code == 403
