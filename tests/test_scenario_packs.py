"""시나리오 팩: 데이터만 갈아끼우면 다른 회사 시뮬레이션이 된다(에이전트 코드는 그대로)."""
import time

from fastapi.testclient import TestClient

from app import settings, store
from app.api import app
from mcp_servers import rag

client = TestClient(app)


def test_packs_share_event_ids_and_timing():
    # 이벤트 id·시점·관찰 요소가 같아야 엔진·관찰 코드를 그대로 쓸 수 있다
    moarun = [(s["id"], s["at_minute"], s["element"]) for s in settings.scenes("moarun")]
    pixel = [(s["id"], s["at_minute"], s["element"]) for s in settings.scenes("pixel")]
    assert moarun == pixel
    assert [e["name"] for e in settings.observation_spec("pixel")["element"]] == \
           [e["name"] for e in settings.observation_spec("moarun")["element"]]


def test_pixel_pack_content_and_prompts():
    assert settings.pack_info("pixel")["company"] == "픽셀게임즈"
    p = settings.prompt("coworker", "pixel")
    assert "윤하늘" in p and "픽셀게임즈" in p and "이도현" not in p and "{" not in p
    assert "이도현" in settings.prompt("coworker", "moarun")
    sid = store.create_session("x", scenario="pixel")
    assert store.list_tasks(sid)[3]["title"] == "비슷한 게임 사례"
    assert any("확률형" in part["body"] for part in settings.coworker_parts("pixel"))


def test_rag_searches_only_the_sessions_pack():
    hits = rag.search_company_docs("확률형 아이템 보상 정책", 30, "", "pixel")
    assert hits[0]["doc_id"] == "policy" and "확률형" in hits[0]["excerpt"]
    assert all("확률형" not in h.get("excerpt", "") for h in rag.search_company_docs("확률형 아이템 보상 정책", 30, "", "moarun"))
    # 설문 요약도 01:05 전에는 열 수 없다(F-15는 팩과 무관하게 유지)
    sid = store.create_session("x", scenario="pixel")
    store.update_session(sid, sim_seconds=30 * 60)
    try:
        rag.read_doc_resource(sid, "survey")
        raise AssertionError("공개 전 문서가 열림")
    except PermissionError:
        pass


def _signup(email: str, company: str, scenario: str = "") -> dict:
    r = client.post("/api/auth/signup", json={"email": email, "password": "password123",
                                              "company": company, "scenario": scenario})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_scenario_is_chosen_at_signup_and_fixed_per_company():
    # PRD 5.5 "조건은 모두에게 같다": 시나리오는 가입할 때 정하고, 그 회사 지원자는 모두 같은 시나리오
    assert [p["id"] for p in client.get("/api/scenarios").json()] == ["moarun", "pixel"]   # 로그인 전 목록
    bad = client.post("/api/auth/signup", json={"email": "x@px.kr", "password": "password123",
                                               "company": "없는팩HR", "scenario": "없는팩"})
    assert bad.status_code == 400
    H = _signup("lock@px.kr", "잠금HR", "pixel")
    assert client.get("/api/admin/scenario", headers=H).json()["current"]["company"] == "픽셀게임즈"
    client.post("/api/admin/invites", json={"alias": "A", "scenario": "moarun"}, headers=H)   # 초대마다 고르는 값은 무시
    client.post("/api/admin/invites", json={"alias": "B"}, headers=H)
    assert {r["scenario"] for r in client.get("/api/admin/candidates", headers=H).json()} == {"pixel"}
    assert client.put("/api/admin/scenario", json={"scenario": "moarun"}, headers=H).status_code == 405   # 바꾸는 API 없음
    # 고르지 않으면 기본(공통 PRD) 시나리오, 다른 기업은 영향 없음(F-01)
    assert client.get("/api/admin/scenario", headers=_signup("other@px.kr", "다른HR")).json()["current"]["id"] == "moarun"


def test_candidate_can_read_pack_only_doc():
    # 회귀: 문서 열람 API가 팩을 넘기지 않아 픽셀게임즈 지원자가 '유저 설문 요약'을 열면 404였다
    sid = store.create_session("C", company="문서HR", scenario="pixel", invite_token="tok-pixel-doc",
                               invite_expires_at=time.time() + 3600)
    store.update_session(sid, sim_seconds=70 * 60)   # 설문 요약 공개(01:05) 이후
    r = client.get("/api/invite/tok-pixel-doc/docs/survey")
    assert r.status_code == 200 and r.json()["title"] == "유저 설문 요약"


def test_candidate_doc_list_follows_session_pack():
    # 회귀: 화면 문서 목록이 팩을 넘기지 않아 픽셀게임즈 세션에 루프랩 '사용자 인터뷰 요약'이 보였고, 누르면 404였다
    sid = store.create_session("D", company="목록HR", scenario="pixel", invite_token="tok-pixel-list",
                               invite_expires_at=time.time() + 3600)
    store.update_session(sid, status="completed", phase="done", sim_seconds=70 * 60)   # 시계·LLM이 돌지 않게 끝난 세션
    titles = {d["title"] for d in client.get("/api/invite/tok-pixel-list/poll").json()["docs"]}
    assert "유저 설문 요약" in titles and "사용자 인터뷰 요약" not in titles


def test_admin_docs_follow_company_pack():
    H = _signup("docs@px.kr", "문서목록HR", "pixel")
    titles = {d["title"] for d in client.get("/api/admin/docs", headers=H).json()["docs"]}
    assert "유저 설문 요약" in titles and "사용자 인터뷰 요약" not in titles


def test_invite_uses_company_scenario_and_poll_names():
    H = _signup("g@px.kr", "픽셀HR", "pixel")
    token = client.post("/api/admin/invites", json={"alias": "유저A"},
                        headers=H).json()["invite_path"].rsplit("/", 1)[1]
    info = client.get(f"/api/invite/{token}").json()
    assert info["pack"]["coworker_name"] == "윤하늘" and info["pack"]["service"] == "픽셀 레전드"
