"""시나리오 팩: 데이터만 갈아끼우면 다른 회사 시뮬레이션이 된다(에이전트 코드는 그대로)."""
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


def test_invite_with_scenario_and_poll_names():
    r = client.post("/api/auth/signup", json={"email": "g@px.kr", "password": "password123", "company": "픽셀HR"})
    H = {"Authorization": f"Bearer {r.json()['token']}"}
    assert [p["id"] for p in client.get("/api/admin/scenarios", headers=H).json()] == ["moarun", "pixel"]
    assert client.post("/api/admin/invites", json={"alias": "x", "scenario": "없는팩"}, headers=H).status_code == 400
    token = client.post("/api/admin/invites", json={"alias": "유저A", "scenario": "pixel"},
                        headers=H).json()["invite_path"].rsplit("/", 1)[1]
    info = client.get(f"/api/invite/{token}").json()
    assert info["pack"]["coworker_name"] == "윤하늘" and info["pack"]["service"] == "픽셀 레전드"
