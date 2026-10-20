"""SQLite 저장소.

PRD 부록은 PostgreSQL이지만 MVP는 설치 없이 돌도록 SQLite를 쓴다(부록 변경, README에 기록).
연결은 호출마다 새로 연다. MCP 서버(별도 프로세스)도 같은 파일을 읽는다.
"""
import json
import sqlite3
import time
import uuid

from app.settings import PART_TITLES, db_path, default_assignment

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    alias TEXT NOT NULL,
    status TEXT NOT NULL,              -- invited | in_progress | completed | report_ready | failed
    phase TEXT NOT NULL,               -- waiting | negotiate | work | done
    sim_seconds REAL NOT NULL DEFAULT 0,
    last_seen REAL,
    negotiation_round INTEGER NOT NULL DEFAULT 0,
    assignment TEXT NOT NULL,          -- JSON: 파트 번호 → candidate | coworker | both
    created_at REAL NOT NULL,
    company TEXT NOT NULL DEFAULT '',  -- 초대한 기업. 다른 기업은 볼 수 없다(F-01)
    email TEXT NOT NULL DEFAULT '',
    invite_token TEXT UNIQUE,          -- 초대 링크 토큰(F-02). 지원자 인증 수단
    invite_expires_at REAL,
    consented INTEGER NOT NULL DEFAULT 0
);
-- 기업 관리자 계정(F-01). 비밀번호는 salt + PBKDF2 해시로만 저장한다.
CREATE TABLE IF NOT EXISTS admins (
    email TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    salt TEXT NOT NULL,
    pw_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    channel TEXT NOT NULL,             -- team | dm | system | assistant
    sender TEXT NOT NULL,              -- candidate | coworker | lead | system | assistant
    content TEXT NOT NULL,
    minute REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_parts (
    session_id TEXT NOT NULL,
    part TEXT NOT NULL,
    content TEXT NOT NULL,
    updated_minute REAL NOT NULL,
    PRIMARY KEY (session_id, part)
);
CREATE TABLE IF NOT EXISTS tasks (
    session_id TEXT NOT NULL,
    part TEXT NOT NULL,
    title TEXT NOT NULL,
    owner TEXT NOT NULL,
    status TEXT NOT NULL,              -- todo | doing | done
    PRIMARY KEY (session_id, part)
);
-- 관찰 기록: 요소별 판별 결과(내부용). business-mcp save_observation으로 저장한다.
CREATE TABLE IF NOT EXISTS observations (
    session_id TEXT NOT NULL,
    element TEXT NOT NULL,
    result TEXT NOT NULL,              -- JSON
    PRIMARY KEY (session_id, element)
);
-- LLM 판별 캐시: 같은 입력이면 같은 결과(재현성).
CREATE TABLE IF NOT EXISTS judge_cache (
    cache_key TEXT PRIMARY KEY,
    result TEXT NOT NULL
);
-- 리포트: 세션당 하나. state = generating | ready | failed
CREATE TABLE IF NOT EXISTS reports (
    session_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    body TEXT,                         -- JSON
    error TEXT
);
-- 기업이 등록한 회사 문서(F-06). 승인된 것만 들어간다. 같은 doc_id면 시나리오 문서를 교체한다.
CREATE TABLE IF NOT EXISTS company_docs (
    company TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    available_from_minute REAL NOT NULL DEFAULT 0,
    approved_at REAL NOT NULL,
    PRIMARY KEY (company, doc_id)
);
-- 문서 등록 요청(F-06): waiting(승인 대기) | registered | rejected
CREATE TABLE IF NOT EXISTS doc_requests (
    request_id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,                -- 개인정보 마스킹을 거친 본문
    masked_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL
);
-- 장면은 세션당 한 번만 실행. PRIMARY KEY가 중복 실행을 막는다(F-23).
CREATE TABLE IF NOT EXISTS scene_runs (
    session_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    run_minute REAL NOT NULL,
    PRIMARY KEY (session_id, scene_id)
);
"""


def connect() -> sqlite3.Connection:
    """DB 연결을 열고 스키마를 보장한다."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def create_session(alias: str, *, company: str = "", email: str = "",
                   status: str = "in_progress", invite_token: str | None = None,
                   invite_expires_at: float | None = None) -> str:
    """세션과 기본 태스크 보드를 만들고 세션 id를 반환한다.

    초대(F-02)로 만들 때는 status="invited"와 초대 토큰·만료 시각을 넘긴다.
    """
    session_id = uuid.uuid4().hex[:12]
    assignment = default_assignment()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, alias, status, phase, assignment, created_at, company, email,"
            " invite_token, invite_expires_at) VALUES (?, ?, ?, 'waiting', ?, ?, ?, ?, ?, ?)",
            (session_id, alias, status, json.dumps(assignment), time.time(), company, email,
             invite_token, invite_expires_at))
        conn.executemany(
            "INSERT INTO tasks (session_id, part, title, owner, status) VALUES (?, ?, ?, ?, 'todo')",
            [(session_id, part, PART_TITLES[part], owner) for part, owner in assignment.items()])
    return session_id


def get_session(session_id: str) -> dict:
    """세션 한 건을 dict로 반환한다.

    Raises:
        KeyError: 없는 세션 id.
    """
    with connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise KeyError(f"없는 세션: {session_id}")
    data = dict(row)
    data["assignment"] = json.loads(data["assignment"])
    return data


def update_session(session_id: str, **fields) -> None:
    """세션 컬럼 일부를 갱신한다. assignment는 dict로 넘기면 JSON으로 저장한다."""
    if "assignment" in fields:
        fields["assignment"] = json.dumps(fields["assignment"])
    columns = ", ".join(f"{key} = ?" for key in fields)
    with connect() as conn:
        conn.execute(f"UPDATE sessions SET {columns} WHERE id = ?", (*fields.values(), session_id))


def add_message(session_id: str, channel: str, sender: str, content: str, minute: float) -> int:
    """메시지를 저장하고 id를 반환한다."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages (session_id, channel, sender, content, minute, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, channel, sender, content, minute, time.time()))
        return cur.lastrowid


def list_messages(session_id: str, after_id: int = 0,
                  channels: tuple[str, ...] | None = None) -> list[dict]:
    """메시지를 id 순으로 반환한다. after_id 이후만, channels로 채널을 거른다."""
    sql = "SELECT * FROM messages WHERE session_id = ? AND id > ?"
    params: list = [session_id, after_id]
    if channels:
        sql += f" AND channel IN ({','.join('?' * len(channels))})"
        params.extend(channels)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", params)]


def claim_scene(session_id: str, scene_id: str, minute: float) -> bool:
    """장면 실행 권한을 얻는다. 처음 호출한 쪽만 True (동시에 불러도 한 번만 실행)."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO scene_runs (session_id, scene_id, run_minute) VALUES (?, ?, ?)",
            (session_id, scene_id, minute))
        return cur.rowcount == 1


def unclaim_scene(session_id: str, scene_id: str) -> None:
    """장면 실행이 실패했을 때 실행 표시를 지워 다시 시도할 수 있게 한다."""
    with connect() as conn:
        conn.execute("DELETE FROM scene_runs WHERE session_id = ? AND scene_id = ?",
                     (session_id, scene_id))


def run_scene_ids(session_id: str) -> set[str]:
    """이미 실행된 장면 id 집합."""
    with connect() as conn:
        rows = conn.execute("SELECT scene_id FROM scene_runs WHERE session_id = ?", (session_id,))
        return {r["scene_id"] for r in rows}


def upsert_part(session_id: str, part: str, content: str, minute: float) -> None:
    """기획안 파트 내용을 저장한다(자동 저장, F-13)."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO plan_parts (session_id, part, content, updated_minute) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(session_id, part) DO UPDATE SET content = excluded.content,"
            " updated_minute = excluded.updated_minute",
            (session_id, part, content, minute))


def get_parts(session_id: str) -> dict[str, str]:
    """기획안 파트 번호 → 내용."""
    with connect() as conn:
        rows = conn.execute("SELECT part, content FROM plan_parts WHERE session_id = ?", (session_id,))
        return {r["part"]: r["content"] for r in rows}


def list_tasks(session_id: str) -> list[dict]:
    """태스크 보드 카드 목록 (F-14)."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE session_id = ? ORDER BY part", (session_id,))
        return [dict(r) for r in rows]


def set_task_owners(session_id: str, assignment: dict[str, str]) -> None:
    """분업 결과를 태스크 보드 담당자에 반영한다."""
    with connect() as conn:
        conn.executemany("UPDATE tasks SET owner = ? WHERE session_id = ? AND part = ?",
                         [(owner, session_id, part) for part, owner in assignment.items()])


def set_task_status(session_id: str, part: str, status: str) -> None:
    """태스크 카드 상태를 바꾼다.

    Raises:
        ValueError: 허용되지 않은 상태값.
    """
    if status not in ("todo", "doing", "done"):
        raise ValueError(f"허용되지 않은 상태: {status}")
    with connect() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE session_id = ? AND part = ?",
                     (status, session_id, part))


def scene_run_minutes(session_id: str) -> dict[str, float]:
    """장면 id → 실제 실행된 세션 시각(분)."""
    with connect() as conn:
        rows = conn.execute("SELECT scene_id, run_minute FROM scene_runs WHERE session_id = ?",
                            (session_id,))
        return {r["scene_id"]: r["run_minute"] for r in rows}


def save_observation(session_id: str, element: str, result: dict) -> None:
    """요소별 관찰 결과를 저장한다(덮어쓰기)."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO observations (session_id, element, result) VALUES (?, ?, ?)"
            " ON CONFLICT(session_id, element) DO UPDATE SET result = excluded.result",
            (session_id, element, json.dumps(result, ensure_ascii=False)))


def list_observations(session_id: str) -> dict[str, dict]:
    """요소 이름 → 관찰 결과."""
    with connect() as conn:
        rows = conn.execute("SELECT element, result FROM observations WHERE session_id = ?",
                            (session_id,))
        return {r["element"]: json.loads(r["result"]) for r in rows}


def cache_get(key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT result FROM judge_cache WHERE cache_key = ?", (key,)).fetchone()
    return json.loads(row["result"]) if row else None


def cache_put(key: str, result: dict) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO judge_cache (cache_key, result) VALUES (?, ?)",
                     (key, json.dumps(result, ensure_ascii=False)))


def claim_report(session_id: str) -> bool:
    """리포트 생성 권한을 얻는다. 처음 호출한 쪽만 True (중복 생성 방지)."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO reports (session_id, state) VALUES (?, 'generating')",
            (session_id,))
        return cur.rowcount == 1


def update_report(session_id: str, **fields) -> None:
    """리포트 상태·본문을 갱신한다. body는 dict로 넘기면 JSON으로 저장한다."""
    if "body" in fields and fields["body"] is not None:
        fields["body"] = json.dumps(fields["body"], ensure_ascii=False)
    columns = ", ".join(f"{key} = ?" for key in fields)
    with connect() as conn:
        conn.execute(f"UPDATE reports SET {columns} WHERE session_id = ?",
                     (*fields.values(), session_id))


def get_report(session_id: str) -> dict | None:
    """리포트 한 건. 없으면 None."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM reports WHERE session_id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["body"] = json.loads(data["body"]) if data["body"] else None
    return data


def session_by_invite(token: str) -> dict | None:
    """초대 토큰으로 세션을 찾는다. 없으면 None."""
    with connect() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE invite_token = ?", (token,)).fetchone()
    return get_session(row["id"]) if row else None


def list_sessions(company: str) -> list[dict]:
    """기업의 지원자 세션 목록(F-03). 다른 기업 세션은 포함하지 않는다(F-01)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, alias, email, status, created_at FROM sessions WHERE company = ?"
            " ORDER BY created_at DESC", (company,))
        return [dict(r) for r in rows]


def create_admin(email: str, company: str, salt: str, pw_hash: str) -> bool:
    """관리자 계정을 만든다. 이미 있는 이메일이면 False."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO admins (email, company, salt, pw_hash) VALUES (?, ?, ?, ?)",
            (email, company, salt, pw_hash))
        return cur.rowcount == 1


def get_admin(email: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM admins WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def upsert_company_doc(company: str, doc_id: str, title: str, body: str) -> None:
    """승인된 회사 문서를 저장한다(같은 doc_id면 교체)."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO company_docs (company, doc_id, title, body, approved_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(company, doc_id) DO UPDATE SET title = excluded.title, body = excluded.body,"
            " approved_at = excluded.approved_at", (company, doc_id, title, body, time.time()))


def company_docs(company: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM company_docs WHERE company = ? ORDER BY doc_id", (company,))
        return [dict(r) for r in rows]


def save_doc_request(request_id: str, company: str, doc_id: str, title: str, body: str,
                     masked_count: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO doc_requests (request_id, company, doc_id, title, body, masked_count, status,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, 'waiting', ?)",
            (request_id, company, doc_id, title, body, masked_count, time.time()))


def set_doc_request_status(request_id: str, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE doc_requests SET status = ? WHERE request_id = ?", (status, request_id))


def doc_requests(company: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM doc_requests WHERE company = ? ORDER BY created_at DESC",
                            (company,))
        return [dict(r) for r in rows]


def get_doc_request(request_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM doc_requests WHERE request_id = ?", (request_id,)).fetchone()
    return dict(row) if row else None
