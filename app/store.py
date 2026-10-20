"""SQLite 저장소.

PRD 부록은 PostgreSQL이지만 MVP는 설치 없이 돌도록 SQLite를 쓴다(부록 변경, README에 기록).
연결은 호출마다 새로 연다. MCP 서버(별도 프로세스)도 같은 파일을 읽는다.
"""
import json
import sqlite3
import time
import uuid

from app.settings import db_path, default_assignment, default_pack, part_titles

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
    consented INTEGER NOT NULL DEFAULT 0,
    scenario TEXT NOT NULL DEFAULT 'moarun'   -- 시나리오 팩(채용 건마다 하나)
);
-- 기업의 시나리오 팩. 같은 기업의 지원자는 모두 같은 팩을 쓴다(PRD 5.5 "조건은 모두에게 같다").
CREATE TABLE IF NOT EXISTS company_settings (
    company TEXT PRIMARY KEY,
    scenario TEXT NOT NULL
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
-- 지원자가 회사 문서를 연 기록(AI 활용 "결과 검증" 관찰용: 어시스턴트가 근거로 든 문서를 직접 열어 봤는가)
CREATE TABLE IF NOT EXISTS doc_views (
    session_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    minute REAL NOT NULL
);
-- 회사 문서 벡터 색인: 문단(chunk)별 임베딩. 모델 이름·차원을 함께 남긴다(교안 65쪽).
CREATE TABLE IF NOT EXISTS doc_chunks (
    company TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    chunk_no INTEGER NOT NULL,
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    dims INTEGER NOT NULL,
    embedding TEXT NOT NULL,           -- JSON 실수 배열
    PRIMARY KEY (company, doc_id, chunk_no, model)
);
-- 질문 임베딩 캐시(같은 질문을 다시 임베딩하지 않음)
CREATE TABLE IF NOT EXISTS query_embeddings (
    query_hash TEXT PRIMARY KEY,
    embedding TEXT NOT NULL
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
    # 이전 DB 파일에 시나리오 팩 컬럼이 없으면 추가한다(기존 세션은 기본 팩)
    if "scenario" not in {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}:
        conn.execute("ALTER TABLE sessions ADD COLUMN scenario TEXT NOT NULL DEFAULT 'moarun'")
    return conn


def create_session(alias: str, *, company: str = "", email: str = "",
                   status: str = "in_progress", invite_token: str | None = None,
                   invite_expires_at: float | None = None, scenario: str | None = None) -> str:
    """세션과 기본 태스크 보드를 만들고 세션 id를 반환한다.

    초대(F-02)로 만들 때는 status="invited"와 초대 토큰·만료 시각을 넘긴다.
    """
    session_id = uuid.uuid4().hex[:12]
    assignment = default_assignment()
    scenario = scenario or default_pack()
    titles = part_titles(scenario)
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, alias, status, phase, assignment, created_at, company, email,"
            " invite_token, invite_expires_at, scenario) VALUES (?, ?, ?, 'waiting', ?, ?, ?, ?, ?, ?, ?)",
            (session_id, alias, status, json.dumps(assignment), time.time(), company, email,
             invite_token, invite_expires_at, scenario))
        conn.executemany(
            "INSERT INTO tasks (session_id, part, title, owner, status) VALUES (?, ?, ?, ?, 'todo')",
            [(session_id, part, titles[part], owner) for part, owner in assignment.items()])
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
            "SELECT id, alias, email, status, scenario, created_at FROM sessions WHERE company = ?"
            " ORDER BY created_at DESC", (company,))
        return [dict(r) for r in rows]


def company_scenario(company: str) -> str:
    """기업의 시나리오 팩. 정한 적이 없으면 첫 지원자의 팩(이전 데이터), 그것도 없으면 기본 팩."""
    with connect() as conn:
        row = conn.execute("SELECT scenario FROM company_settings WHERE company = ?", (company,)).fetchone()
        if row is None:
            row = conn.execute("SELECT scenario FROM sessions WHERE company = ? ORDER BY created_at LIMIT 1",
                               (company,)).fetchone()
    return row["scenario"] if row else default_pack()


def set_company_scenario(company: str, scenario: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO company_settings (company, scenario) VALUES (?, ?)"
                     " ON CONFLICT(company) DO UPDATE SET scenario = excluded.scenario", (company, scenario))


def create_admin(email: str, company: str, salt: str, pw_hash: str) -> str:
    """관리자 계정을 만든다. 한 회사에 계정 하나(F-01: 다른 기업의 지원자·리포트는 보이지 않음).

    기업을 회사 이름으로 구분하므로, 같은 이름(대소문자·공백 무시)으로 가입하면 남의 데이터가 보인다.
    그래서 이름이 겹치면 만들지 않는다. 확인과 저장을 한 문장으로 해 동시에 가입해도 하나만 된다.

    Returns:
        "ok" | "email"(이미 가입한 이메일) | "company"(이미 등록된 회사 이름).
    """
    with connect() as conn:
        if conn.execute("SELECT 1 FROM admins WHERE email = ?", (email,)).fetchone():
            return "email"
        cur = conn.execute(
            "INSERT OR IGNORE INTO admins (email, company, salt, pw_hash) SELECT ?, ?, ?, ?"
            " WHERE NOT EXISTS (SELECT 1 FROM admins"
            "  WHERE lower(replace(company, ' ', '')) = lower(replace(?, ' ', '')))",
            (email, company, salt, pw_hash, company))
        return "ok" if cur.rowcount == 1 else "company"


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


def generating_reports() -> list[str]:
    """아직 "생성 중"인 리포트의 세션 id (서버 재시작으로 중단된 것 포함)."""
    with connect() as conn:
        return [r["session_id"] for r in conn.execute("SELECT session_id FROM reports WHERE state = 'generating'")]


def add_doc_view(session_id: str, doc_id: str, minute: float) -> None:
    """지원자가 회사 문서를 연 시각을 남긴다."""
    with connect() as conn:
        conn.execute("INSERT INTO doc_views (session_id, doc_id, minute) VALUES (?, ?, ?)",
                     (session_id, doc_id, minute))


def doc_views(session_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT doc_id, minute FROM doc_views WHERE session_id = ? ORDER BY minute",
                            (session_id,))
        return [dict(r) for r in rows]


def chunk_index(owners: list[str], model: str) -> list[dict]:
    """소유자(팩 키 'pack:<id>' 또는 기업 이름) 기준 색인된 문단 목록."""
    owners = [o for o in owners if o]
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM doc_chunks WHERE company IN ({','.join('?' * len(owners))}) AND model = ?",
            (*owners, model))
        return [{**dict(r), "embedding": json.loads(r["embedding"])} for r in rows]


def replace_doc_chunks(company: str, doc_id: str, model: str, chunks: list[dict]) -> None:
    """문서 하나의 색인을 통째로 바꾼다. chunks: [{chunk_no, text, content_hash, embedding}]."""
    with connect() as conn:
        conn.execute("DELETE FROM doc_chunks WHERE company = ? AND doc_id = ? AND model = ?",
                     (company, doc_id, model))
        conn.executemany(
            "INSERT INTO doc_chunks (company, doc_id, chunk_no, text, content_hash, model, dims, embedding)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(company, doc_id, c["chunk_no"], c["text"], c["content_hash"], model, len(c["embedding"]),
              json.dumps(c["embedding"])) for c in chunks])


def get_query_embedding(query_hash: str) -> list[float] | None:
    with connect() as conn:
        row = conn.execute("SELECT embedding FROM query_embeddings WHERE query_hash = ?",
                           (query_hash,)).fetchone()
    return json.loads(row["embedding"]) if row else None


def put_query_embedding(query_hash: str, embedding: list[float]) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO query_embeddings (query_hash, embedding) VALUES (?, ?)",
                     (query_hash, json.dumps(embedding)))


def reset_failed_report(session_id: str) -> bool:
    """실패한 리포트를 지우고 세션을 '완료'로 되돌린다(관리자 재생성용). 실패 상태일 때만 True."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM reports WHERE session_id = ? AND state = 'failed'", (session_id,))
        if cur.rowcount != 1:
            return False
        conn.execute("UPDATE sessions SET status = 'completed' WHERE id = ?", (session_id,))
        return True


def completed_without_report() -> list[str]:
    """끝났는데(완료) 리포트 행이 없는 세션 id."""
    with connect() as conn:
        rows = conn.execute("SELECT id FROM sessions WHERE status = 'completed' AND id NOT IN"
                            " (SELECT session_id FROM reports)")
        return [r["id"] for r in rows]
