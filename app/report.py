"""근거 리포트 (F-04, F-32~F-34).

리포트에는 "관찰된 행동 + 근거 원문 + 시각"만 넣는다. 점수·개수·합격 표현은 넣지 않는다(PRD 5.5).
관찰 결과는 business-mcp의 save_observation(변경 도구)으로 저장한다.
생성 실패 시 config [report].retry_count회 다시 시도하고, 계속 실패하면 세션 상태를 "failed"로 바꾼다.
"""
import hashlib
import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app import clock, guard, llm, observe, store
from app.mcp_client import call_business
from app.settings import config, prompt

log = logging.getLogger("dayzero.report")
NOT_OBSERVED = "해당 상황에서 관련 행동이 기록되지 않음"
NOT_HAPPENED = "세션이 이 상황 전에 끝나 관찰할 수 없음"
NOT_USED = "사용하지 않음"


def _minute_of(evidence_id: str, session_id: str) -> str:
    if evidence_id.startswith("view:"):   # 문서 열람 기록
        return clock.format_minute(float(evidence_id.split(":", 1)[1]))
    if evidence_id.startswith("m"):
        for m in store.list_messages(session_id):
            if f"m{m['id']}" == evidence_id:
                return clock.format_minute(m["minute"])
    if evidence_id.startswith("plan:"):
        return f"기획안 {evidence_id.split(':', 1)[1]}번"
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
                when, quote = _minute_of(c["evidence_id"], session_id), c.get("quote", "")
                point = c.get("point", "")
                same = next((o for o in section["observed"] if o["when"] == when and o["quote"] == quote), None)
                if same:   # 같은 인용이 여러 체크의 근거면 한 번만 보이고 행동 문장·관찰 포인트를 합친다
                    if behavior and behavior not in same["behavior"]:
                        same["behavior"] = f"{same['behavior']} / {behavior}" if same["behavior"] else behavior
                    if point and point not in same["points"]:
                        same["points"].append(point)
                    continue
                eid = c["evidence_id"]
                source = "기획안" if eid.startswith("plan:") else "문서 열람" if eid.startswith("view:") else "대화"
                section["observed"].append({"behavior": behavior, "quote": quote, "when": when, "source": source,
                                            "points": [point] if point else [],
                                            "message_id": int(eid[1:]) if eid[:1] == "m" and eid[1:].isdigit() else None})
        if "used" in r:   # AI 활용
            section["requests"] = [{"when": clock.format_minute(q["minute"]), "request": q["request"],
                                    "reuse": q["reuse"]} for q in r.get("requests", [])]
            if not r["used"]:
                section["note"] = NOT_USED
        elif not r.get("happened", True):
            section["note"] = NOT_HAPPENED
        if not section["observed"] and "note" not in section and not section.get("requests"):
            section["note"] = NOT_OBSERVED
            # 판단 없이 위치만: 그 상황 이후(관찰 구간) 지원자가 보낸 메시지. 해석은 담당자가 한다(PRD 5.5).
            section["followups"] = [{"id": f["id"], "when": clock.format_minute(f["minute"])}
                                    for f in r.get("followups", [])]
        sections.append(section)
    return {"session_id": session_id, "sections": sections,
            "disclaimer": "이 리포트는 관찰 기록입니다. 판단은 채용 담당자가 합니다."}


# ---------- 요소별 사실 요약 (근거 정리, 판단 없음) ----------

def _evidence_list(section: dict) -> str:
    lines = []
    for i, o in enumerate(section["observed"], 1):
        where = o["when"] if o["source"] == "기획안" else f"{o['when']} {o['source']}"
        lines.append(f"[{i}] ({where}) 행동: {o['behavior'] or '(없음)'} / 원문: \"{o['quote']}\"")
    return "\n".join(lines)


def check_summary(text: str, section: dict) -> str:
    """요약을 코드로 검증한다. 통과하면 "ok", 아니면 실패 이유.

    1) 판정·평가 표현이 없어야 한다. 2) 모든 문장 끝에 실제 근거 번호가 있어야 한다.
    3) 큰따옴표 안 원문은 근거 원문에 실제로 있어야 한다(지어낸 인용 금지). 4) 문장 수 상한.
    """
    cfg = config()["report"]
    if guard.violates(text):
        return "judgement"
    for w in cfg["summary_forbidden_words"]:
        if w in text:
            return f"evaluative:{w}"
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    if not sentences or len(sentences) > cfg["summary_max_sentences"]:
        return "length"
    n = len(section["observed"])
    for s in sentences:
        refs = [int(r) for r in re.findall(r"\[(\d+)\]", s)]
        if not refs:
            return "no_ref"
        if any(r < 1 or r > n for r in refs):
            return "bad_ref"
    threshold = config()["observation"]["quote_match_threshold"]
    for q in re.findall(r'["“]([^"”]+)["”]', text):
        if not any(observe.quote_found(q, o["quote"], threshold) for o in section["observed"]):
            return f"unquoted:{q}"
    return "ok"


async def summarize_section(session_id: str, section: dict) -> str | None:
    """검증된 근거를 2~3문장으로 나란히 정리한다. 검증을 끝내 통과하지 못하면 None(원문만 보인다)."""
    cfg = config()
    if len(section["observed"]) < cfg["report"]["summary_min_evidence"]:
        return None
    human = f"요소: {section['element']}\n상황: {section['situation']}\n\n근거 목록:\n{_evidence_list(section)}"
    key = hashlib.sha256(json.dumps(["summary", cfg["llm"]["model"], prompt("report_summary"), human],
                                    ensure_ascii=False).encode()).hexdigest()
    if (cached := store.cache_get(key)) is not None:
        return cached["summary"]
    messages = [SystemMessage(prompt("report_summary")), HumanMessage(human)]
    for attempt in range(1, cfg["report"]["summary_attempts"] + 1):
        out = await llm.ainvoke(llm.chat_model(cfg["llm"]["judge_temperature"]), messages,
                                "report.summary", session_id)
        text = str(out.content).strip()
        verdict = check_summary(text, section)
        llm.trace({"session": session_id, "node": "report.summary.verify", "element": section["element"],
                   "verdict": verdict, "attempt": attempt})
        if verdict == "ok":
            store.cache_put(key, {"summary": text})
            return text
        messages = messages + [out, HumanMessage(
            f"검증 실패({verdict}). 근거 목록에 있는 내용만, 모든 문장 끝에 근거 번호를 달고, "
            "원문은 그대로 인용하고, 평가 표현 없이 다시 써라.")]
    return None


async def generate(session_id: str) -> dict:
    """관찰 → MCP로 저장 → 리포트 본문 생성 → 요소별 사실 요약."""
    results = await observe.observe_session(session_id)
    for r in results:
        await call_business("save_observation", {"session_id": session_id, "element": r["element"],
                                                 "result_json": json.dumps(r, ensure_ascii=False)})
    body = build_body(session_id, results)
    for section in body["sections"]:
        try:
            summary = await summarize_section(session_id, section)
        except Exception:   # 요약은 보조 정보다. 실패하면 원인을 남기고 근거 원문만 보인다(리포트 자체는 만든다).
            log.exception("요약 생성 실패: session=%s element=%s", session_id, section["element"])
            summary = None
        if summary:
            section["summary"] = summary
    return body


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
    # 세션은 끝났는데 리포트 작업이 시작되기 전에 멈춘 경우(F-32)
    for sid in store.completed_without_report():
        log.warning("시작되지 않은 리포트 생성: session=%s", sid)
        await generate_with_retry(sid)
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
