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

from app import clock, llm, store
from app.settings import PART_TITLES, config, observation_spec, prompt

JUDGE_VERSION = "v1"   # 판별 프롬프트·형식을 바꾸면 올린다(캐시 무효화)
NAMES = {"candidate": "지원자", "coworker": "이도현", "lead": "한서윤 팀장", "system": "시스템",
         "assistant": "AI 어시스턴트"}


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
    return {k: v for k, v in store.get_parts(session_id).items() if k in PART_TITLES}


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


def verify(answers: list[CheckAnswer], evidence: dict[str, str],
           scopes: dict[str, str] | None = None) -> list[dict]:
    """yes 답 중 인용이 원문에 없거나 허용 범위 밖이면 근거 없음으로 바꾼다.

    Args:
        answers: 판별기 답.
        evidence: 근거 id → 원문.
        scopes: 체크 id → "messages"(지원자 메시지만) | "all". 없으면 all.
    """
    threshold = config()["observation"]["quote_match_threshold"]
    scopes = scopes or {}
    out = []
    for a in answers:
        item = a.model_dump()
        item["verified"] = False
        if a.answer == "yes":
            in_scope = not (scopes.get(a.check_id) == "messages" and a.evidence_id.startswith("plan:"))
            source = evidence.get(a.evidence_id, "") if in_scope else ""
            if quote_found(a.quote, source, threshold):
                item["verified"] = True
            else:
                item.update(answer="no", dropped_reason="인용이 원문에서 확인되지 않음")
        out.append(item)
    return out


# ---------- LLM 판별 ----------

async def _judge(session_id: str, situation: str, checks: list[dict], transcript: str,
                 hint: str, use_cache: bool) -> list[CheckAnswer]:
    cfg = config()
    scope_note = {"messages": " [근거는 지원자 메시지(id=m…)에서만. 기획안 인용 금지]"}
    questions = "\n".join(f"- {c['id']}: {c['question']}{scope_note.get(c.get('evidence_scope'), '')}"
                          for c in checks)
    human = (f"상황: {situation}\n\n확인할 질문:\n{questions}\n\n{hint}\n\n기록:\n{transcript}\n\n"
             "질문마다 하나씩 답하라. 근거는 '지원자' 메시지나 기획안에서만 인용한다.")
    key = hashlib.sha256(json.dumps([JUDGE_VERSION, cfg["llm"]["model"], prompt("judge"), human],
                                    ensure_ascii=False).encode()).hexdigest()
    if use_cache and (cached := store.cache_get(key)) is not None:
        return [CheckAnswer(**a) for a in cached["answers"]]
    model = llm.chat_model(cfg["llm"]["judge_temperature"]).with_structured_output(
        Judgement, method="function_calling")
    result: Judgement = await llm.ainvoke(
        model, [SystemMessage(prompt("judge")), HumanMessage(human)], "observe.judge", session_id)
    store.cache_put(key, result.model_dump())
    return result.answers


def _format(messages: list[dict], plan: dict[str, str] | None) -> str:
    lines = [f"[id=m{m['id']}][{clock.format_minute(m['minute'])}][{m['channel']}] "
             f"{NAMES[m['sender']]}: {m['content']}" for m in messages]
    if plan is not None:
        lines.append("\n[지원자가 쓴 기획안 최종본]")
        lines += [f"[id=plan:{k}] {PART_TITLES[k]}: {v}" for k, v in sorted(plan.items())]
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

    if mode == "rules":   # A: 키워드에 걸린 지원자 메시지가 있으면 관련 행동 있음
        checks = [{"check_id": "keyword", "answer": "yes" if hits else "no",
                   "evidence_id": f"m{min(hits)}" if hits else "", "verified": bool(hits)}]
        return {**base, "happened": True, "checks": checks}

    hint = ("참고: 키워드 후보 메시지 id = " + ", ".join(f"m{i}" for i in sorted(hits))
            + " (후보일 뿐이다. 다른 메시지도 모두 본다)") if hits else "키워드 후보 없음. 모든 메시지를 본다."
    answers = await _judge(session_id, spec["situation"], spec["checks"],
                           _format(messages, plan), hint, use_cache)
    evidence = _evidence_texts(messages, plan or {})
    return {**base, "happened": True, "checks": verify(answers, evidence)}


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
    spec = observation_spec()["ai_use"]
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
    if mode == "rules":
        return {**base, "used": True, "requests": reuse, "checks": []}

    answers = await _judge(session_id, spec["situation"], spec["checks"],
                           _format(log, plan), "AI 어시스턴트 대화 기록과 지원자 기획안이다.", use_cache)
    evidence = {f"m{m['id']}": m["content"] for m in requests}
    evidence.update({f"plan:{k}": v for k, v in plan.items()})
    scopes = {c["id"]: c.get("evidence_scope", "all") for c in spec["checks"]}
    return {**base, "used": True, "requests": reuse, "checks": verify(answers, evidence, scopes)}


async def observe_session(session_id: str, mode: str = "hybrid", use_cache: bool = True) -> list[dict]:
    """세션의 인성 요소 4개 + AI 활용을 판별한다."""
    run_minutes = store.scene_run_minutes(session_id)
    results = [await observe_element(session_id, spec, run_minutes, mode, use_cache)
               for spec in observation_spec()["element"]]
    results.append(await observe_ai_use(session_id, mode, use_cache))
    return results
