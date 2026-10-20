"""인증 (F-01, F-02, F-10).

- 기업 관리자: 이메일·비밀번호 가입/로그인 → HMAC 서명 토큰(Bearer). 기업 이름이 토큰에 들어가
  다른 기업의 지원자·리포트는 조회되지 않는다.
- 지원자: 관리자가 만든 1회용 초대 토큰이 곧 인증 수단이다. 72시간 뒤 만료.
표준 라이브러리(hashlib, hmac, secrets)만 쓴다.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

from app import store
from app.settings import config

log = logging.getLogger("dayzero.auth")
SECONDS_PER_HOUR = 3600
_SECRET = os.environ.get("AUTH_SECRET", "")
if not _SECRET:
    # 키가 없으면 프로세스마다 임시 키를 쓴다(재시작하면 기존 토큰 무효). 코드에 키를 적지 않는다.
    _SECRET = secrets.token_hex(32)
    log.warning("AUTH_SECRET이 없어 임시 서명 키를 사용합니다. .env에 설정하세요.")


class AuthError(Exception):
    """인증 실패. 메시지는 사용자에게 그대로 보여도 되는 문장이다."""


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                               config()["auth"]["pbkdf2_iterations"]).hex()


def _sign(payload: str) -> str:
    return hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def signup(email: str, password: str, company: str) -> str:
    """관리자 가입 후 토큰을 반환한다.

    Raises:
        AuthError: 입력이 비었거나 이미 가입한 이메일.
    """
    email, company = email.strip().lower(), company.strip()
    if not email or not company or len(password) < 8:
        raise AuthError("이메일·회사 이름을 입력하고 비밀번호는 8자 이상으로 해 주세요.")
    salt = secrets.token_hex(16)
    if not store.create_admin(email, company, salt, _hash(password, salt)):
        raise AuthError("이미 가입한 이메일입니다.")
    return issue_token(email, company)


def login(email: str, password: str) -> str:
    """관리자 로그인 후 토큰을 반환한다.

    Raises:
        AuthError: 이메일 또는 비밀번호가 맞지 않음.
    """
    admin = store.get_admin(email.strip().lower())
    if admin is None or not hmac.compare_digest(admin["pw_hash"], _hash(password, admin["salt"])):
        raise AuthError("이메일 또는 비밀번호가 맞지 않습니다.")
    return issue_token(admin["email"], admin["company"])


def issue_token(email: str, company: str) -> str:
    exp = time.time() + config()["auth"]["admin_token_hours"] * SECONDS_PER_HOUR
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email, "company": company, "exp": exp}).encode()).decode()
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str) -> dict:
    """토큰을 검증하고 {email, company}를 반환한다.

    Raises:
        AuthError: 서명이 틀렸거나 만료된 토큰.
    """
    try:
        payload, sig = token.split(".", 1)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError("로그인이 필요합니다.") from exc
    if not hmac.compare_digest(sig, _sign(payload)):
        raise AuthError("로그인이 필요합니다.")
    if data["exp"] < time.time():
        raise AuthError("로그인이 만료되었습니다. 다시 로그인해 주세요.")
    return {"email": data["email"], "company": data["company"]}


def create_invite(company: str, alias: str, email: str, scenario: str = "") -> dict:
    """1회용 초대 링크를 만든다(F-02). 실명 대신 별칭을 쓴다(PRD 5.5).

    scenario는 채용 건의 시나리오 팩(기업 맞춤 시나리오). 비우면 기본 팩(공통 PRD 시나리오).
    """
    if not alias.strip():
        raise AuthError("지원자 별칭을 입력해 주세요.")
    from app.settings import default_pack, pack_ids
    scenario = scenario or default_pack()
    if scenario not in pack_ids():
        raise AuthError(f"없는 시나리오입니다: {scenario}")
    token = secrets.token_urlsafe(24)
    expires = time.time() + config()["session"]["invite_ttl_hours"] * SECONDS_PER_HOUR
    sid = store.create_session(alias.strip(), company=company, email=email.strip(),
                               status="invited", invite_token=token, invite_expires_at=expires,
                               scenario=scenario)
    return {"session_id": sid, "invite_path": f"/invite/{token}", "expires_at": expires}


def invite_state(token: str) -> tuple[str, dict | None]:
    """초대 링크 상태(F-10): valid | expired | completed | not_found 와 세션."""
    s = store.session_by_invite(token)
    if s is None:
        return "not_found", None
    if s["status"] in ("completed", "report_ready", "failed"):
        return "completed", s
    if s["status"] == "invited" and s["invite_expires_at"] < time.time():
        return "expired", s
    return "valid", s
