"""회사 문서 목록: 시나리오 팩의 문서 + 기업이 승인해 등록한 문서(F-06).

같은 doc_id면 기업 문서가 팩 문서를 교체한다. 공개 시각(F-15)은 팩 문서 값을 따른다.
rag-mcp(별도 프로세스), API, 페르소나가 모두 이 모듈로 문서를 본다.
"""
from app import clock, settings, store


def all_docs(company: str = "", pack: str | None = None) -> list[dict]:
    """기업·팩 기준 전체 문서 목록(id, title, available_from_minute, source)."""
    docs = {d["id"]: {**d, "source": "scenario"} for d in settings.docs_index(pack)}
    for c in store.company_docs(company) if company else []:
        base = docs.get(c["doc_id"], {})
        docs[c["doc_id"]] = {"id": c["doc_id"], "title": c["title"], "source": "company",
                             "available_from_minute": base.get("available_from_minute",
                                                               c["available_from_minute"])}
    return list(docs.values())


def available(company: str, minute: float, pack: str | None = None) -> list[dict]:
    """세션 시각 기준으로 공개된 문서만 (F-15)."""
    return clock.available_docs(tuple(all_docs(company, pack)), minute)


def body(company: str, doc_id: str, pack: str | None = None) -> str:
    """문서 본문. 기업 문서가 있으면 그것을 쓴다.

    Raises:
        KeyError: 없는 문서.
    """
    for c in store.company_docs(company) if company else []:
        if c["doc_id"] == doc_id:
            return c["body"]
    return settings.doc_text(doc_id, pack)
