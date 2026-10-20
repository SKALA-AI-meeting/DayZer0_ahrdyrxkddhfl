"""근거 리포트 (F-04, F-32~F-34).

리포트에는 "관찰된 행동 + 근거 원문 + 시각"만 넣는다. 점수·개수·합격 표현은 넣지 않는다(PRD 5.5).
관찰 결과는 business-mcp의 save_observation(변경 도구)으로 저장한다.
생성 실패 시 config [report].retry_count회 다시 시도하고, 계속 실패하면 세션 상태를 "failed"로 바꾼다.
"""
import json
import logging

from app import clock, guard, observe, store
from app.mcp_client import call_business
from app.settings import config

log = logging.getLogger("dayzero.report")
NOT_OBSERVED = "해당 상황에서 관련 행동이 기록되지 않음"
NOT_HAPPENED = "세션이 이 상황 전에 끝나 관찰할 수 없음"
NOT_USED = "사용하지 않음"


def _minute_of(evidence_id: str, session_id: str) -> str:
    if evidence_id.startswith("m"):
        for m in store.list_messages(session_id):
            if f"m{m['id']}" == evidence_id:
                return clock.format_minute(m["minute"])
    return "기획안"


def build_body(session_id: str, results: list[dict]) -> dict:
    """판별 결과를 기업용 리포트 본문으로 바꾼다. yes이면서 인용이 검증된 것만 근거로 쓴다."""
    sections = []
    for r in results:
        section = {"element": r["element"], "situation": r["situation"], "observed": []}
        for c in r.get("checks", []):
            if c["answer"] == "yes" and c.get("verified"):
                behavior = c.get("behavior") or ""
                # 판별기가 평가 표현을 섞었으면 행동 문장은 버리고 원문만 남긴다(F-24·PRD 5.5).
                if guard.violates(behavior):
                    behavior = ""
                section["observed"].append({"behavior": behavior, "quote": c.get("quote", ""),
                                            "when": _minute_of(c["evidence_id"], session_id)})
        if "used" in r:   # AI 활용
            section["requests"] = [{"when": clock.format_minute(q["minute"]), "request": q["request"],
                                    "reuse": q["reuse"]} for q in r.get("requests", [])]
            if not r["used"]:
                section["note"] = NOT_USED
        elif not r.get("happened", True):
            section["note"] = NOT_HAPPENED
        if not section["observed"] and "note" not in section and not section.get("requests"):
            section["note"] = NOT_OBSERVED
        sections.append(section)
    return {"session_id": session_id, "sections": sections,
            "disclaimer": "이 리포트는 관찰 기록입니다. 판단은 채용 담당자가 합니다."}


async def generate(session_id: str) -> dict:
    """관찰 → MCP로 저장 → 리포트 본문 생성."""
    results = await observe.observe_session(session_id)
    for r in results:
        await call_business("save_observation", {"session_id": session_id, "element": r["element"],
                                                 "result_json": json.dumps(r, ensure_ascii=False)})
    return build_body(session_id, results)


async def generate_with_retry(session_id: str) -> str:
    """리포트를 만들고 상태를 갱신한다. 이미 다른 쪽이 만들고 있으면 아무것도 하지 않는다.

    Returns:
        최종 리포트 상태 (ready | failed | skipped).
    """
    if not store.claim_report(session_id):
        return "skipped"
    return await _run(session_id)


async def resume_stale() -> list[str]:
    """서버가 재시작되기 전 "생성 중"이던 리포트를 다시 만든다(F-34). 서버 시작 때 부른다."""
    done = []
    for sid in store.generating_reports():
        log.warning("중단된 리포트 다시 생성: session=%s", sid)
        await _run(sid)
        done.append(sid)
    return done


async def _run(session_id: str) -> str:
    """리포트 생성 본체: 재시도 후 실패하면 failed."""
    attempts = config()["report"]["retry_count"] + 1
    for attempt in range(1, attempts + 1):
        try:
            body = await generate(session_id)
        except Exception as exc:   # 실패 원인을 남기고 재시도(F-34)
            log.exception("리포트 생성 실패 %s/%s: session=%s", attempt, attempts, session_id)
            store.update_report(session_id, attempts=attempt, error=f"{type(exc).__name__}: {exc}")
            continue
        store.update_report(session_id, state="ready", attempts=attempt, body=body, error=None)
        store.update_session(session_id, status="report_ready")
        return "ready"
    store.update_report(session_id, state="failed")
    store.update_session(session_id, status="failed")
    return "failed"
