"""관찰 판별 (하이브리드 B).

요소마다:
1. 관찰 구간: 이벤트가 실제로 실행된 시각 ~ window_end_minute 의 메시지(+필요하면 기획안).
2. 키워드 후보: candidate_keywords에 걸린 지원자 메시지를 표시한다(확정 아님, 판별기에게 힌트).
3. LLM 체크리스트: 질문마다 yes/no + 원문 인용 + 메시지 id (temperature 0).
4. 인용 검증(코드): 인용한 문장이 그 id의 지원자 메시지(또는 기획안)에 실제로 있어야 인정.
   없으면 yes를 버린다 → 근거 없는 판단이 리포트에 들어가지 않는다.
5. 캐시: 같은 입력이면 같은 결과를 돌려준다(재현성).

mode="rules"(A)는 LLM 없이 키워드 후보만으로 "관련 행동 있음"을 판단한다. A/B 비교 평가용.

AI 활용(F-31, F-33)은 규칙으로: 요청 0건이면 "사용하지 않음", 응답과 기획안의 유사도로
"그대로 씀 / 고쳐 씀 / 반영 안 함"을 나눈다. 위임·맥락·검증은 LLM 체크리스트로 본다.
"""
import hashlib
import json
from difflib import SequenceMatcher
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app import clock, docs, llm, store
from app.settings import PART_IDS, config, names, observation_spec, part_titles, prompt

JUDGE_VERSION = "v1"   # 판별 프롬프트·형식을 바꾸면 올린다(캐시 무효화)


class CheckAnswer(BaseModel):
    check_id: str
    answer: Literal["yes", "no"]
    evidence_id: str = Field(default="", description="근거 메시지 id(예: 'm12') 또는 기획안 파트(예: 'plan:3'). no면 빈 문자열")
    quote: str = Field(default="", description="근거 원문을 그대로 복사. no면 빈 문자열")
    behavior: str = Field(default="", description="관찰된 행동을 한 문장의 사실로. no면 빈 문자열")


class Judgement(BaseModel):
    answers: list[CheckAnswer]


# ---------- 입력 만들기 ----------

def _window_messages(session_id: str, start: float, end: float) -> list[dict]:
    return [m for m in store.list_messages(session_id, channels=("team", "dm", "system"))
            if start <= m["minute"] <= end]


def _candidate_plan(session_id: str) -> dict[str, str]:
    """지원자가 쓴 기획안 파트만 (동료 공유분 coworker:* 제외)."""
    return {k: v for k, v in store.get_parts(session_id).items() if k in PART_IDS}


def _evidence_texts(messages: list[dict], plan: dict[str, str]) -> dict[str, str]:
    """근거로 인정할 수 있는 원문: 지원자 메시지와 지원자 기획안만."""
    texts = {f"m{m['id']}": m["content"] for m in messages if m["sender"] == "candidate"}
    texts.update({f"plan:{k}": v for k, v in plan.items()})
    return texts


def _keyword_hits(messages: list[dict], keywords: list[str]) -> set[int]:
    return {m["id"] for m in messages
            if m["sender"] == "candidate" and any(k in m["content"] for k in keywords)}


# ---------- 인용 검증 ----------

def quote_found(quote: str, source: str, threshold: float) -> bool:
    """quote가 source 안에 (거의) 그대로 있는지 확인한다.

    정확히 포함되면 True. 아니면 가장 길게 겹치는 구간이 quote 길이의 threshold 이상이어야 한다.
    """
    quote, source = quote.strip(), source.strip()
    if not quote:
        return False
    if quote in source:
        return True
    match = SequenceMatcher(None, quote, source, autojunk=False).find_longest_match(
        0, len(quote), 0, len(source))
    return match.size / len(quote) >= threshold


def _in_scope(scope: str, evidence_id: str) -> bool:
    if scope == "messages":
        return not evidence_id.startswith("plan:")
    if scope == "plan":
        return evidence_id.startswith("plan:")
    return True


def verify(answers: list[CheckAnswer], evidence: dict[str, str],
           scopes: dict[str, str] | None = None, required: dict[str, list[str]] | None = None,
           forbidden: dict[str, list[str]] | None = None) -> list[dict]:
    """yes 답 중 근거로 인정할 수 없는 것을 근거 없음으로 바꾼다.

    1) 인용이 원문에 없거나 허용 범위 밖이면 버린다(지어낸 인용).
    2) 인용이 원문에 있어도 그 행동을 보여 주는 말(required)이 없으면 버린다(과장된 행동 문장).
    3) 인용에 반대 뜻(부정·제외, forbidden)이 있으면 버린다("(확률형x)"를 '확률형을 넣음'으로 읽는 실수).

    Args:
        answers: 판별기 답.
        evidence: 근거 id → 원문.
        scopes: 체크 id → "messages"(지원자 메시지만) | "plan"(기획안만) | "all". 없으면 all.
        required: 체크 id → 인용에 하나는 들어 있어야 하는 말 목록. 없으면 검사하지 않는다.
        forbidden: 체크 id → 인용에 있으면 안 되는 말 목록. 없으면 검사하지 않는다.
    """
    threshold = config()["observation"]["quote_match_threshold"]
    scopes, required, forbidden = scopes or {}, required or {}, forbidden or {}
    out = []
    for a in answers:
        item = a.model_dump()
        item["verified"] = False
        if a.answer == "yes":
            in_scope = _in_scope(scopes.get(a.check_id, "all"), a.evidence_id)
            source = evidence.get(a.evidence_id, "") if in_scope else ""
            words = required.get(a.check_id)
            if not quote_found(a.quote, source, threshold):
                item.update(answer="no", dropped_reason="인용이 원문에서 확인되지 않음")
            elif words and not any(w in a.quote for w in words):
                item.update(answer="no", dropped_reason="인용이 이 행동을 뒷받침하지 않음")
            elif any(w in a.quote for w in forbidden.get(a.check_id, [])):
                item.update(answer="no", dropped_reason="인용이 반대 뜻(부정·제외)을 담고 있음")
            else:
                item["verified"] = True
        out.append(item)
    return out


def _with_points(checks: list[dict], spec_checks: list[dict]) -> list[dict]:
    """판별 결과마다 관찰 포인트(리포트 표시용 문구)를 붙인다. 어떤 기준으로 근거가 됐는지 담당자가 보게."""
    points = {c["id"]: c.get("point", "") for c in spec_checks}
    return [{**c, "point": c.get("point") or points.get(c["check_id"], "")} for c in checks]


# ---------- LLM 판별 ----------

MISSING_REASON = "판별기가 답하지 않음"


async def _judge(session_id: str, situation: str, checks: list[dict], transcript: str,
                 hint: str, use_cache: bool) -> tuple[list[CheckAnswer], set[str]]:
    """체크리스트를 판별한다. 판별기가 빠뜨린 체크는 그 체크만 다시 묻는다.

    Returns:
        (체크마다 하나씩의 답, 다시 물어도 답이 없던 체크 id). 답이 없던 체크는 "no"로 채운다.
    """
    cfg = config()
    scope_note = {"messages": " [근거는 지원자 메시지(id=m…)에서만. 기획안 인용 금지]",
                  "plan": " [근거는 지원자 기획안(id=plan:…)에서만]"}

    def ask(subset: list[dict]) -> str:
        questions = "\n".join(f"- {c['id']}: {c['question']}{scope_note.get(c.get('evidence_scope'), '')}"
                              for c in subset)
        return (f"상황: {situation}\n\n확인할 질문:\n{questions}\n\n{hint}\n\n기록:\n{transcript}\n\n"
                "질문마다 하나씩 답하라. 근거는 '지원자' 메시지나 기획안에서만 인용한다.")

    human = ask(checks)
    key = hashlib.sha256(json.dumps([JUDGE_VERSION, cfg["llm"]["model"], prompt("judge"), human],
                                    ensure_ascii=False).encode()).hexdigest()
    ids = [c["id"] for c in checks]
    cached = store.cache_get(key) if use_cache else None
    # 이 처리 전에 저장된 캐시는 체크가 빠져 있을 수 있다. 모든 체크가 있을 때만 쓴다.
    if cached is not None and {a["check_id"] for a in cached["answers"]} >= set(ids):
        return [CheckAnswer(**a) for a in cached["answers"]], set(cached.get("missing", []))
    model = llm.chat_model(cfg["llm"]["judge_temperature"]).with_structured_output(
        Judgement, method="function_calling")
    got: dict[str, CheckAnswer] = {}
    pending = checks
    for attempt in range(cfg["observation"]["judge_missing_retries"] + 1):
        result: Judgement = await llm.ainvoke(
            model, [SystemMessage(prompt("judge")), HumanMessage(ask(pending))], "observe.judge", session_id)
        for a in result.answers:   # 묻지 않은 id·중복 답은 버린다
            if a.check_id in ids and a.check_id not in got:
                got[a.check_id] = a
        pending = [c for c in checks if c["id"] not in got]
        if not pending:
            break
        llm.trace({"session": session_id, "node": "observe.judge.missing", "attempt": attempt + 1,
                   "missing": [c["id"] for c in pending]})
    missing = {c["id"] for c in pending}
    answers = [got.get(i) or CheckAnswer(check_id=i, answer="no") for i in ids]
    store.cache_put(key, {"answers": [a.model_dump() for a in answers], "missing": sorted(missing)})
    return answers, missing


def _mark_missing(checks: list[dict], missing: set[str]) -> list[dict]:
    """다시 물어도 답이 없던 체크에 이유를 남긴다(리포트에는 근거로 쓰이지 않고 내부 기록에만 남음)."""
    return [{**c, "dropped_reason": MISSING_REASON} if c["check_id"] in missing else c for c in checks]


def _format(messages: list[dict], plan: dict[str, str] | None, pack: str | None = None) -> str:
    who, titles = names(pack), part_titles(pack)
    lines = [f"[id=m{m['id']}][{clock.format_minute(m['minute'])}][{m['channel']}] "
             f"{who[m['sender']]}: {m['content']}" for m in messages]
    if plan is not None:
        lines.append("\n[지원자가 쓴 기획안 최종본]")
        lines += [f"[id=plan:{k}] {titles[k]}: {v}" for k, v in sorted(plan.items())]
    return "\n".join(lines) or "(기록 없음)"


async def observe_element(session_id: str, spec: dict, run_minutes: dict[str, float],
                          mode: str = "hybrid", use_cache: bool = True) -> dict:
    """인성 요소 하나를 판별한다.

    Returns:
        {"element", "situation", "happened", "checks": [...]} — checks는 내부용 판별 결과.
    """
    base = {"element": spec["name"], "situation": spec["situation"]}
    if spec["scene"] not in run_minutes:
        return {**base, "happened": False, "checks": []}
    start, end = run_minutes[spec["scene"]], spec["window_end_minute"]
    messages = _window_messages(session_id, start, end)
    plan = _candidate_plan(session_id) if spec.get("include_plan_document") else None
    hits = _keyword_hits(messages, spec["candidate_keywords"])
    # 관찰 구간 안의 지원자 메시지 위치(판단 없음). 관찰 없음일 때 담당자가 원문을 직접 보도록 리포트에 링크한다.
    base["followups"] = [{"id": m["id"], "minute": m["minute"]} for m in messages if m["sender"] == "candidate"]

    if mode == "rules":   # A: 키워드에 걸린 지원자 메시지가 있으면 관련 행동 있음
        checks = [{"check_id": "keyword", "answer": "yes" if hits else "no",
                   "evidence_id": f"m{min(hits)}" if hits else "", "verified": bool(hits)}]
        return {**base, "happened": True, "checks": checks}

    hint = ("참고: 키워드 후보 메시지 id = " + ", ".join(f"m{i}" for i in sorted(hits))
            + " (후보일 뿐이다. 다른 메시지도 모두 본다)") if hits else "키워드 후보 없음. 모든 메시지를 본다."
    answers, missing = await _judge(session_id, spec["situation"], spec["checks"],
                                    _format(messages, plan, store.get_session(session_id)["scenario"]), hint, use_cache)
    evidence = _evidence_texts(messages, plan or {})
    scopes = {c["id"]: c.get("evidence_scope", "all") for c in spec["checks"]}
    required = {c["id"]: c["quote_must_contain"] for c in spec["checks"] if c.get("quote_must_contain")}
    forbidden = {c["id"]: c["quote_must_not_contain"] for c in spec["checks"] if c.get("quote_must_not_contain")}
    checks = _mark_missing(verify(answers, evidence, scopes, required, forbidden), missing)
    return {**base, "happened": True, "checks": _with_points(checks, spec["checks"])}


# ---------- AI 활용 ----------

def _best_similarity(text: str, plan: dict[str, str]) -> float:
    """어시스턴트 응답의 줄들과 기획안 줄들 사이에서 가장 높은 유사도."""
    resp_lines = [line.strip(" -*#") for line in text.splitlines() if len(line.strip()) >= 10]
    plan_lines = [line.strip(" -*#") for part in plan.values() for line in part.splitlines()
                  if len(line.strip()) >= 10]
    best = 0.0
    for r in resp_lines:
        for p in plan_lines:
            best = max(best, SequenceMatcher(None, r, p, autojunk=False).ratio())
    return best


def classify_reuse(similarity: float) -> str:
    """F-31: 유사도 → 그대로 씀 / 고쳐 씀 / 반영 안 함 (기준값은 config)."""
    obs = config()["observation"]
    if similarity >= obs["copied_threshold"]:
        return "그대로 씀"
    if similarity >= obs["edited_threshold"]:
        return "고쳐 씀"
    return "반영 안 함"


async def observe_ai_use(session_id: str, mode: str = "hybrid", use_cache: bool = True) -> dict:
    """AI 활용을 판별한다. 요청 0건이면 LLM 없이 '사용하지 않음'(F-33)."""
    pack = store.get_session(session_id)["scenario"]
    spec = observation_spec(pack)["ai_use"]
    log = store.list_messages(session_id, channels=("assistant",))
    requests = [m for m in log if m["sender"] == "candidate"]
    base = {"element": spec["name"], "situation": spec["situation"]}
    if not requests:
        return {**base, "used": False, "requests": [], "checks": []}

    plan = _candidate_plan(session_id)
    reuse = []
    for i, m in enumerate(log):
        if m["sender"] == "assistant" and i > 0:
            req = log[i - 1]
            sim = _best_similarity(m["content"], plan)
            reuse.append({"request_id": f"m{req['id']}", "minute": req["minute"],
                          "request": req["content"], "reuse": classify_reuse(sim)})
    doc_checks = cross_checked_docs(session_id, log)
    if mode == "rules":
        return {**base, "used": True, "requests": reuse, "checks": doc_checks}

    answers, missing = await _judge(session_id, spec["situation"], spec["checks"],
                                    _format(log, plan, pack), "AI 어시스턴트 대화 기록과 지원자 기획안이다.", use_cache)
    evidence = {f"m{m['id']}": m["content"] for m in requests}
    evidence.update({f"plan:{k}": v for k, v in plan.items()})
    scopes = {c["id"]: c.get("evidence_scope", "all") for c in spec["checks"]}
    return {**base, "used": True, "requests": reuse,
            "checks": _with_points(_mark_missing(verify(answers, evidence, scopes), missing), spec["checks"]) + doc_checks}


def cross_checked_docs(session_id: str, log: list[dict]) -> list[dict]:
    """규칙: 어시스턴트가 근거로 든 문서를 지원자가 그 답 이후에 직접 열어 봤는가(결과 검증).

    LLM 판단 없이 열람 기록(doc_views)으로 확정한다. 근거 id는 "view:<분>" 형식.
    """
    s = store.get_session(session_id)
    titles = {d["id"]: d["title"] for d in docs.all_docs(s["company"], s["scenario"])}
    views = store.doc_views(session_id)
    out, seen = [], set()
    for m in log:
        if m["sender"] != "assistant":
            continue
        for doc_id, title in titles.items():
            if title not in m["content"] or doc_id in seen:
                continue
            hit = next((v for v in views if v["doc_id"] == doc_id and v["minute"] >= m["minute"]), None)
            if hit:
                seen.add(doc_id)
                out.append({"check_id": "verified_doc_view", "answer": "yes", "verified": True,
                            "point": "AI 답의 근거 문서를 직접 열어 확인함",
                            "evidence_id": f"view:{hit['minute']}", "quote": f"'{title}' 문서 열람",
                            "behavior": f"지원자가 AI 어시스턴트 답의 근거 문서 '{title}'을(를) 직접 열어 확인함"})
    return out


async def observe_session(session_id: str, mode: str = "hybrid", use_cache: bool = True) -> list[dict]:
    """세션의 인성 요소 4개 + AI 활용을 판별한다."""
    run_minutes = store.scene_run_minutes(session_id)
    results = [await observe_element(session_id, spec, run_minutes, mode, use_cache)
               for spec in observation_spec(store.get_session(session_id)["scenario"])["element"]]
    results.append(await observe_ai_use(session_id, mode, use_cache))
    return results
