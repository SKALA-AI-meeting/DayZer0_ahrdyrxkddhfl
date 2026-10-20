"""rag-mcp: 회사 문서 MCP 서버 (직접 구축, stdio).

- 조회 도구: list_company_docs, search_company_docs, read_company_doc
- 변경 도구: register_company_doc (destructiveHint, 사람 승인 토큰 필수, F-06)
- Resource: docs://{session_id}/{doc_id} (회사 문서 원문, 근거 패널용 — 교안 29·33쪽),
            scenario://brief (과제 브리프, 항상 공개)
- 문서 공개 시각(F-15)을 서버가 직접 검사한다. session_minute·company는 모델이 아니라
  에이전트 코드가 채워 넣는다(app/mcp_client.py) → 모델이 값을 바꿔 미리 읽을 수 없다.

검색은 config [rag].search_mode: vector(미리 색인한 임베딩 유사도, app/embedding.py) 또는 keyword.
vector에서 임베딩 호출이 실패하면 keyword로 대신 찾는다(Recovery). 결과마다 via로 방식을 밝힌다.
실행: uv run python -m mcp_servers.rag
"""
import logging
import re

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app import approval, clock, docs, embedding, settings, store

mcp = FastMCP("dayzero-rag")
log = logging.getLogger("dayzero.rag")   # stdio 서버라 stdout에 쓰지 않는다(로그는 stderr)
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
    results = None
    if rag_cfg["search_mode"] == "vector":
        try:
            results = _vector_search(query, tokens, company, open_docs, rag_cfg)
        except Exception:   # 임베딩 실패 시 키워드 검색으로 대신 찾는다(원인은 stderr 로그)
            log.exception("벡터 검색 실패, 키워드 검색으로 대체")
    if results is None:
        results = _keyword_search(tokens, company, open_docs, rag_cfg)
    # 아직 공개되지 않은 문서를 지목한 검색이면, 다른 문서로 대신 답하지 않도록 사실을 알린다.
    open_ids = {d["id"] for d in open_docs}
    for doc in docs.all_docs(company):
        if doc["id"] not in open_ids and _score(tokens, doc["title"]) >= 2:
            results.insert(0, {"doc_id": doc["id"], "title": doc["title"],
                               "notice": f"'{doc['title']}' 문서는 아직 공유되지 않았습니다."})
    return results


def _vector_search(query: str, tokens: list[str], company: str, open_docs: list[dict],
                   rag_cfg: dict) -> list[dict]:
    """미리 색인한 임베딩으로 공개된 문서의 문단을 찾는다. 질문이 제목을 지목하면 가중치를 더한다."""
    titles = {d["id"]: d["title"] for d in open_docs}
    hits = embedding.search(query, company, set(titles))
    for h in hits:
        if _score(tokens, titles[h["doc_id"]]):
            h["score"] += rag_cfg["vector_title_bonus"]
    hits.sort(key=lambda h: h["score"], reverse=True)
    return [{"doc_id": h["doc_id"], "title": titles[h["doc_id"]], "excerpt": h["text"],
             "score": round(h["score"], 3), "via": "vector"} for h in hits[:rag_cfg["top_k"]]]


def _keyword_search(tokens: list[str], company: str, open_docs: list[dict], rag_cfg: dict) -> list[dict]:
    """키워드 일치 수로 문단을 찾는다. 질문이 문서를 지목하면("인터뷰 요약에서…") 그 문서를 우선한다."""
    hits = []
    for doc in open_docs:
        title_bonus = _score(tokens, doc["title"]) * rag_cfg["title_match_weight"]
        for para in docs.body(company, doc["id"]).split("\n\n"):
            score = _score(tokens, para)
            if score:
                hits.append((score + title_bonus, doc, para.strip()))
    hits.sort(key=lambda h: h[0], reverse=True)
    return [{"doc_id": d["id"], "title": d["title"], "excerpt": p, "via": "keyword"}
            for _, d, p in hits[:rag_cfg["top_k"]]]


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
    # 색인은 다음 벡터 검색 때 내용 해시를 비교해 바뀐 문서만 다시 만든다(embedding.ensure_index)
    return {"ok": True, "doc_id": doc_id, "title": title}


@mcp.resource("docs://{session_id}/{doc_id}")
def read_doc_resource(session_id: str, doc_id: str) -> str:
    """회사 문서 원문 Resource. 세션의 기업·시각을 서버가 DB에서 직접 읽어 공개 전 문서는 막는다(F-15).

    Resource에는 시각 인자가 없으므로, 호출하는 쪽이 시각을 속여 미리 읽을 수 없게 서버가 판단한다.
    """
    try:
        s = store.get_session(session_id)
    except KeyError as exc:
        raise ValueError("없는 세션입니다.") from exc
    minute, company = clock.minute_of(s["sim_seconds"]), s["company"]
    known = {d["id"]: d for d in docs.all_docs(company)}
    if doc_id not in known:
        raise ValueError(f"없는 문서입니다: {doc_id}")
    if known[doc_id]["available_from_minute"] > minute:
        raise PermissionError(f"'{known[doc_id]['title']}' 문서는 아직 공유되지 않았습니다.")
    return docs.body(company, doc_id)


@mcp.resource("scenario://brief")
def read_brief() -> str:
    """과제 브리프와 기본 분담안 (모든 지원자에게 공개된 내용)."""
    return (settings.ROOT / "scenario" / "brief.md").read_text(encoding="utf-8")


if __name__ == "__main__":
    mcp.run(transport="stdio")
