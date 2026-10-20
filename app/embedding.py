"""회사 문서 벡터 색인과 유사도 검색 (레퍼런스 아키텍처: 문서 → 청크 → 임베딩 → 색인 → 검색).

- 청크: 문서를 빈 줄 기준 문단으로 나눈다(문서가 짧아 문단 하나가 근거 단위로 적당).
- 색인: 문단별 임베딩을 SQLite에 저장하고 모델 이름·차원을 함께 남긴다. 문서 내용 해시가 같으면
  다시 임베딩하지 않는다(내용이 바뀐 문서만 다시 색인).
- 검색: 질문 임베딩과 코사인 유사도. 질문 임베딩도 캐시한다.
- 임베딩 모델은 config [rag].embedding_model (팀 키로 사용 가능 확인). 새 라이브러리 없이 openai SDK 사용.
"""
import hashlib
import math

from app import docs, store
from app.settings import config


def _model() -> str:
    return config()["rag"]["embedding_model"]


def embed(texts: list[str]) -> list[list[float]]:
    """OpenAI 임베딩. 테스트에서는 이 함수를 가짜로 바꾼다.

    Raises:
        openai.OpenAIError: 호출 실패.
    """
    from openai import OpenAI
    res = OpenAI().embeddings.create(model=_model(), input=texts)
    return [d.embedding for d in res.data]


def chunks_of(text: str) -> list[str]:
    """문서를 문단(빈 줄 기준)으로 나눈다. 너무 짧은 조각(제목만 있는 줄 등)은 다음 문단에 붙인다."""
    out, carry = [], ""
    for para in (p.strip() for p in text.split("\n\n")):
        if not para:
            continue
        if len(para) < 20:          # 짧은 제목줄은 다음 문단과 합친다(코드 상수: 청크 형식의 일부)
            carry = f"{carry}\n{para}".strip()
            continue
        out.append(f"{carry}\n{para}".strip() if carry else para)
        carry = ""
    if carry:
        out.append(carry)
    return out


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _pack_owner(pack: str | None) -> str:
    """팩 문서 색인의 소유자 키. 팩마다 같은 doc_id가 있어도 섞이지 않게 나눈다."""
    from app.settings import default_pack
    return f"pack:{pack or default_pack()}"


def ensure_index(company: str = "", pack: str | None = None) -> int:
    """팩 문서와 기업 문서의 색인을 최신으로 맞춘다. 새로 임베딩한 문단 수를 반환한다."""
    model = _model()
    have = {}
    for c in store.chunk_index([_pack_owner(pack), company], model):
        have.setdefault((c["company"], c["doc_id"]), set()).add(c["content_hash"])
    company_ids = {c["doc_id"] for c in store.company_docs(company)} if company else set()
    embedded = 0
    for d in docs.all_docs(company, pack):
        owner = company if d["id"] in company_ids else _pack_owner(pack)
        body = docs.body(company, d["id"], pack)
        parts = chunks_of(f"# {d['title']}\n\n{body}")
        hashes = [_hash(p) for p in parts]
        if have.get((owner, d["id"])) == set(hashes):
            continue                                   # 내용이 같으면 다시 임베딩하지 않음
        vectors = embed(parts)
        store.replace_doc_chunks(owner, d["id"], model, [
            {"chunk_no": i, "text": p, "content_hash": h, "embedding": v}
            for i, (p, h, v) in enumerate(zip(parts, hashes, vectors))])
        embedded += len(parts)
    return embedded


def query_vector(query: str) -> list[float]:
    key = _hash(f"{_model()}|{query}")
    cached = store.get_query_embedding(key)
    if cached is not None:
        return cached
    vec = embed([query])[0]
    store.put_query_embedding(key, vec)
    return vec


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def search(query: str, company: str, allowed_doc_ids: set[str], pack: str | None = None) -> list[dict]:
    """공개된 문서(allowed_doc_ids)의 문단을 질문과의 유사도 순으로 [{doc_id, text, score}].

    자르기(top_k)와 제목 가중치는 호출하는 쪽(rag-mcp)이 한다.
    """
    ensure_index(company, pack)
    qv = query_vector(query)
    company_ids = {c["doc_id"] for c in store.company_docs(company)} if company else set()
    hits = []
    for c in store.chunk_index([_pack_owner(pack), company], _model()):
        # 기업이 교체한 문서는 기업 색인만, 나머지는 그 팩의 색인만 쓴다
        owner_ok = (c["company"] == company) if c["doc_id"] in company_ids else (c["company"] == _pack_owner(pack))
        if c["doc_id"] in allowed_doc_ids and owner_ok:
            hits.append({"doc_id": c["doc_id"], "text": c["text"], "score": cosine(qv, c["embedding"])})
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits
