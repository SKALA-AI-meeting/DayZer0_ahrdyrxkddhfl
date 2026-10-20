"""사람 승인 토큰 (Harness: Guardrails).

관리자가 승인한 요청에만 서명 토큰을 발급하고, MCP 서버의 변경 도구는 토큰이 요청 내용과
정확히 맞을 때만 실행한다. 모델이 도구를 직접 불러도 토큰이 없으면 실행되지 않는다.
서명 키는 .env의 AUTH_SECRET (MCP 서버 프로세스도 같은 .env를 읽는다).
"""
import hashlib
import hmac
import os

from app import settings  # noqa: F401  (.env 로드)


def _key() -> bytes:
    secret = os.environ.get("AUTH_SECRET", "")
    if not secret:
        raise RuntimeError("AUTH_SECRET이 없어 승인 토큰을 만들거나 검증할 수 없습니다(.env 확인).")
    return hashlib.sha256(b"doc-approval:" + secret.encode()).digest()


def _payload(request_id: str, company: str, doc_id: str, body: str) -> bytes:
    digest = hashlib.sha256(body.encode()).hexdigest()
    return f"{request_id}|{company}|{doc_id}|{digest}".encode()


def issue(request_id: str, company: str, doc_id: str, body: str) -> str:
    """승인된 요청 내용에 묶인 토큰을 발급한다."""
    return hmac.new(_key(), _payload(request_id, company, doc_id, body), hashlib.sha256).hexdigest()


def verify(token: str, request_id: str, company: str, doc_id: str, body: str) -> bool:
    """토큰이 이 요청 내용(본문 포함)에 대해 발급된 것인지 확인한다. 내용이 바뀌면 실패한다."""
    expected = issue(request_id, company, doc_id, body)
    return hmac.compare_digest(token or "", expected)
