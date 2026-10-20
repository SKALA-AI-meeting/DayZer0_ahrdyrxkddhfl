"""F-06 회사 문서 등록: 승인 대기(interrupt) → 승인 시 재개 → rag-mcp 변경 도구."""
import pytest
from fastapi.testclient import TestClient

from app import approval, doc_graph
from app.api import app
from mcp_servers import rag

client = TestClient(app)
DOC = {"doc_id": "design-guide", "title": "디자인 가이드",
       "body": "버튼은 파란색. 문의: kim@looplab.kr, 010-1234-5678"}


def _admin(email, company):
    r = client.post("/api/auth/signup", json={"email": email, "password": "password123", "company": company})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_mask_email_and_phone():
    text, n = doc_graph.mask(DOC["body"])
    assert n == 2 and "kim@looplab.kr" not in text and "010-1234-5678" not in text


def test_register_tool_refuses_without_valid_token():
    with pytest.raises(PermissionError):
        rag.register_company_doc("r1", "A사", "x-doc", "제목", "본문", approval_token="")
    token = approval.issue("r1", "A사", "x-doc", "본문")
    with pytest.raises(PermissionError):   # 승인 뒤 본문을 바꾸면 거부
        rag.register_company_doc("r1", "A사", "x-doc", "제목", "바뀐 본문", approval_token=token)
    assert rag.register_company_doc("r1", "A사", "x-doc", "제목", "본문", approval_token=token)["ok"]


def test_request_waits_then_approve_registers_via_mcp():
    a = _admin("hr@a.com", "A사")
    b = _admin("hr@b.com", "B사")
    r = client.post("/api/admin/docs", json=DOC, headers=a).json()
    assert r["waiting"] is True and r["masked_count"] == 2          # interrupt에서 멈춤
    assert "[이메일 가림]" in r["preview"]
    assert all(d["id"] != "design-guide" for d in client.get("/api/admin/docs", headers=a).json()["docs"])
    # 다른 기업은 결정할 수 없다
    assert client.post(f"/api/admin/docs/{r['request_id']}/decision", json={"approved": True},
                       headers=b).status_code == 404
    out = client.post(f"/api/admin/docs/{r['request_id']}/decision", json={"approved": True}, headers=a)
    assert out.json() == {"result": "registered"}                      # 같은 지점에서 재개 → 등록
    docs_a = {d["id"]: d for d in client.get("/api/admin/docs", headers=a).json()["docs"]}
    assert docs_a["design-guide"]["source"] == "company"
    assert all(d["id"] != "design-guide" for d in client.get("/api/admin/docs", headers=b).json()["docs"])
    # 승인 후 AI가 검색할 수 있다(F-06 "승인한 뒤에만 참고")
    assert rag.search_company_docs("디자인 가이드 버튼", 0, "A사")[0]["doc_id"] == "design-guide"
    assert all(h["doc_id"] != "design-guide" for h in rag.search_company_docs("디자인 가이드 버튼", 0, "B사"))
    # 두 번 결정할 수 없다
    assert client.post(f"/api/admin/docs/{r['request_id']}/decision", json={"approved": True},
                       headers=a).status_code == 409


def test_reject_does_not_register():
    a = _admin("hr@a.com", "A사")
    r = client.post("/api/admin/docs", json={**DOC, "doc_id": "policy"}, headers=a).json()
    out = client.post(f"/api/admin/docs/{r['request_id']}/decision", json={"approved": False}, headers=a)
    assert out.json() == {"result": "rejected"}
    assert rag.read_company_doc("policy", 0, "A사")["title"] == "운영 정책"   # 시나리오 문서 그대로


def test_bad_doc_id_rejected():
    a = _admin("hr@a.com", "A사")
    assert client.post("/api/admin/docs", json={**DOC, "doc_id": "한글 id"}, headers=a).status_code == 400
