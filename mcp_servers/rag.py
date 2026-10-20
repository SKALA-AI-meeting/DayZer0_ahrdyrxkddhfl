"""rag-mcp: 회사 문서 MCP 서버 (직접 구축, stdio).

- 조회 도구: list_company_docs, search_company_docs, read_company_doc
- 변경 도구: register_company_doc (destructiveHint, 사람 승인 토큰 필수, F-06)
- Resource: scenario://brief (과제 브리프, 항상 공개)
- 문서 공개 시각(F-15)을 서버가 직접 검사한다. session_minute·company는 모델이 아니라
  에이전트 코드가 채워 넣는다(app/mcp_client.py) → 모델이 값을 바꿔 미리 읽을 수 없다.

PRD 부록의 BGE-M3 벡터 검색 대신 키워드 검색을 쓴다(문서 수가 적어 충분, README에 기록).
실행: uv run python -m mcp_servers.rag
"""
import re

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app import approval, docs, settings, store

mcp = FastMCP("dayzero-rag")
READ = ToolAnnotations(readOnlyHint=True)


def _tokens(query: str) -> list[str]:
    """검색어를 2글자 이상 토큰으로 나눈다."""
    return [t for t in re.split(r"[\s,.?!·/()\"'“”]+", query) if len(t) >= 2]


def _score(tokens: list[str], text: str) -> int:
    """토큰이 문단에 몇 개 들어 있는지 센다. 조사가 붙은 경우를 위해 끝 1글자를 뗀 형태도 본다."""
    score = 0
    for t in tokens:
        if t in text or (len(t) >= 3 and t[:-1] in text):
            score += 1
    return score


@mcp.tool(annotations=READ)
def list_company_docs(session_minute: float, company: str = "") -> list[dict]:
    """지금 열람할 수 있는 회사 문서 목록(id, 제목)을 반환한다."""
    return [{"doc_id": d["id"], "title": d["title"]} for d in docs.available(company, session_minute)]


@mcp.tool(annotations=READ)
def search_company_docs(query: str, session_minute: float, company: str = "") -> list[dict]:
    """회사 문서에서 query와 관련된 문단을 찾아 문서 제목과 함께 반환한다.

    지금 공개된 문서만 검색한다. 결과의 title을 답변의 출처로 밝힌다.
    """
    tokens = _tokens(query)
    if not tokens:
        raise ValueError("검색어가 비어 있습니다. 2글자 이상 단어를 넣어 주세요.")
    rag_cfg = settings.config()["rag"]
    open_docs = docs.available(company, session_minute)
    hits = []
    for doc in open_docs:
        # 질문이 문서를 지목하면("인터뷰 요약에서…") 그 문서를 우선한다.
        title_bonus = _score(tokens, doc["title"]) * rag_cfg["title_match_weight"]
        for para in docs.body(company, doc["id"]).split("\n\n"):
            score = _score(tokens, para)
            if score:
                hits.append((score + title_bonus, doc, para.strip()))
    hits.sort(key=lambda h: h[0], reverse=True)
    results = [{"doc_id": d["id"], "title": d["title"], "excerpt": p}
               for _, d, p in hits[:rag_cfg["top_k"]]]
    # 아직 공개되지 않은 문서를 지목한 검색이면, 다른 문서로 대신 답하지 않도록 사실을 알린다.
    open_ids = {d["id"] for d in open_docs}
    for doc in docs.all_docs(company):
        if doc["id"] not in open_ids and _score(tokens, doc["title"]) >= 2:
            results.insert(0, {"doc_id": doc["id"], "title": doc["title"],
                               "notice": f"'{doc['title']}' 문서는 아직 공유되지 않았습니다."})
    return results


@mcp.tool(annotations=READ)
def read_company_doc(doc_id: str, session_minute: float, company: str = "") -> dict:
    """회사 문서 전문을 반환한다. 아직 공개되지 않은 문서는 읽을 수 없다."""
    known = {d["id"]: d for d in docs.all_docs(company)}
    if doc_id not in known:
        raise ValueError(f"없는 문서입니다: {doc_id}. list_company_docs로 목록을 확인하세요.")
    if known[doc_id]["available_from_minute"] > session_minute:
        raise ValueError(f"'{known[doc_id]['title']}' 문서는 아직 공유되지 않았습니다.")
    return {"doc_id": doc_id, "title": known[doc_id]["title"], "body": docs.body(company, doc_id)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def register_company_doc(request_id: str, company: str, doc_id: str, title: str, body: str,
                         approval_token: str) -> dict:
    """관리자가 승인한 회사 문서를 등록·교체한다(F-06).

    승인 토큰이 이 요청 내용(본문 포함)에 대해 발급된 것이 아니면 실행하지 않는다.
    """
    if not approval.verify(approval_token, request_id, company, doc_id, body):
        raise PermissionError("승인되지 않은 요청입니다. 관리자 승인 후에만 등록할 수 있습니다.")
    if not re.fullmatch(r"[a-z0-9_-]{2,40}", doc_id):
        raise ValueError("doc_id는 영문 소문자·숫자·-·_ 2~40자여야 합니다.")
    store.upsert_company_doc(company, doc_id, title, body)
    return {"ok": True, "doc_id": doc_id, "title": title}


@mcp.resource("scenario://brief")
def read_brief() -> str:
    """과제 브리프와 기본 분담안 (모든 지원자에게 공개된 내용)."""
    return (settings.ROOT / "scenario" / "brief.md").read_text(encoding="utf-8")


if __name__ == "__main__":
    mcp.run(transport="stdio")
