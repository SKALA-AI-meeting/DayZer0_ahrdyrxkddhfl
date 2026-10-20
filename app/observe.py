"""관찰 판별 (하이브리드 B).

요소마다(역량 5개, scenario/<팩>/observation.toml):
1. 관찰 구간: 시작 장면이 실제로 실행된 시각 ~ window_end_minute 의 메시지와 업무 메모.
2. 키워드 후보: candidate_keywords에 걸린 지원자 메시지를 표시한다(확정 아님, 판별기에게 힌트).
3. LLM 체크리스트: 질문마다 yes/no + 원문 인용 + 메시지 id (temperature 0).
4. 인용 검증(코드): 인용한 문장이 그 id의 지원자 메시지(또는 업무 메모)에 실제로 있어야 인정하고,
   체크가 정한 채널(예: 팀장에게 알렸는가 → 팀장 1:1·팀 채널)에서 온 근거여야 한다.
   아니면 yes를 버린다 → 근거 없는 판단이 리포트에 들어가지 않는다.
5. 규칙 관찰(RULES): LLM 없이 기록만으로 확정한다(예: 팀장이 묻기 전에 먼저 연락했는가).
6. 캐시: 같은 입력이면 같은 결과를 돌려준다(재현성).

mode="rules"(A)는 LLM 없이 키워드 후보만으로 "관련 행동 있음"을 판단한다. A/B 비교 평가용.
"""
import hashlib
import json
from difflib import SequenceMatcher
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app import clock, llm, persona, store
from app.settings import config, names, observation_spec, pack_info, prompt, scenes


def is_progress_question(text: str) -> bool:
    """진행 상황을 묻는 말인가(엔진과 같은 기준, config [work]). engine을 불러오면 순환 import라 여기 둔다."""
    cfg = config()["work"]
    return (any(p in text for p in cfg["progress_check_phrases"])
            and any(q in text for q in cfg["question_markers"]))

JUDGE_VERSION = "v2"   # 판별 프롬프트·형식을 바꾸면 올린다(캐시 무효화)
CHAT_CHANNELS = ("team", "dm", "lead_dm")      # 지원자가 사람에게 보낸 메시지
MEMO_CHANNEL = "work"                          # 지원자의 업무 메모(+ 업무 지문)
CHANNEL_LABEL = {"team": "팀 채널", "dm": "동료 1:1", "lead_dm": "팀장 1:1", "system": "알림", "work": "업무 기록"}
SCOPE_CHANNELS = {"messages": set(CHAT_CHANNELS), "memo": {MEMO_CHANNEL}, "all": {*CHAT_CHANNELS, MEMO_CHANNEL}}


class CheckAnswer(BaseModel):
    check_id: str
    answer: Literal["yes", "no"]
    evidence_id: str = Field(default="", description="근거 메시지 id(예: 'm12'). no면 빈 문자열")
    quote: str = Field(default="", description="근거 원문을 그대로 복사. no면 빈 문자열")
    behavior: str = Field(default="", description="관찰된 행동을 한 문장의 사실로. no면 빈 문자열")


class Judgement(BaseModel):
    answers: list[CheckAnswer]


class Relation(BaseModel):
    relation: Literal["supports", "contradicts", "unrelated"]


# ---------- 입력 만들기 ----------

def _window_messages(session_id: str, start: float, end: float) -> list[dict]:
    return [m for m in store.list_messages(session_id, channels=(*CHAT_CHANNELS, "system", MEMO_CHANNEL))
            if start <= m["minute"] <= end]


def _evidence_texts(messages: list[dict]) -> dict[str, str]:
    """근거로 인정할 수 있는 원문: 지원자 메시지와 업무 메모만(동료·팀장 말과 지문은 근거가 아니다)."""
    return {f"m{m['id']}": m["content"] for m in messages if m["sender"] == "candidate"}


def _channels_of(messages: list[dict]) -> dict[str, str]:
    return {f"m{m['id']}": m["channel"] for m in messages}


def allowed_channels(check: dict) -> set[str]:
    """체크의 근거로 인정할 채널: evidence_scope(messages·memo·all)와 channels의 교집합."""
    allowed = set(SCOPE_CHANNELS[check.get("evidence_scope", "all")])
    if check.get("channels"):
        allowed &= set(check["channels"])
    return allowed


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
           allowed: dict[str, set[str]] | None = None, required: dict[str, list[str]] | None = None,
           forbidden: dict[str, list[str]] | None = None, channels_of: dict[str, str] | None = None) -> list[dict]:
    """yes 답 중 근거로 인정할 수 없는 것을 근거 없음으로 바꾼다.

    1) 인용이 원문에 없거나 허용 채널 밖이면 버린다(지어낸 인용, 다른 상대에게 한 말).
    2) 인용이 원문에 있어도 그 행동을 보여 주는 말(required)이 없으면 버린다(과장된 행동 문장).
    3) 인용에 반대 뜻(부정·제외, forbidden)이 있으면 버린다("다 됐어요"가 아니라 "다 못 했어요").

    Args:
        answers: 판별기 답.
        evidence: 근거 id → 원문.
        allowed: 체크 id → 근거로 인정할 채널 집합(allowed_channels). 없으면 채널을 보지 않는다.
        required: 체크 id → 인용에 하나는 들어 있어야 하는 말 목록. 없으면 검사하지 않는다.
        forbidden: 체크 id → 인용에 있으면 안 되는 말 목록. 없으면 검사하지 않는다.
        channels_of: 근거 id → 채널. allowed를 검사할 때 쓴다.
    """
    threshold = config()["observation"]["quote_match_threshold"]
    allowed, required, forbidden, channels_of = allowed or {}, required or {}, forbidden or {}, channels_of or {}
    out = []
    for a in answers:
        item = a.model_dump()
        item["verified"] = False
        if a.answer == "yes":
            chans = allowed.get(a.check_id)
            in_scope = chans is None or channels_of.get(a.evidence_id) in chans
            source = evidence.get(a.evidence_id, "")
            words = required.get(a.check_id)
            if not in_scope and quote_found(a.quote, source, threshold):
                # 원문은 맞지만 이 체크가 인정하지 않는 채널의 글(예: 동료에게 한 말을 묻는데 팀장 1:1을 인용).
                # 이유를 정확히 남겨야 다시 물을 때 판별기가 채널을 바로잡는다.
                allowed_names = "·".join(CHANNEL_LABEL[ch] for ch in sorted(chans))
                item.update(answer="no", dropped_reason=f"{CHANNEL_REASON}({allowed_names}의 글만 근거)")
            elif not in_scope or not quote_found(a.quote, source, threshold):
                item.update(answer="no", dropped_reason="인용이 원문에서 확인되지 않음")
            elif words and not any(w in a.quote for w in words):
                item.update(answer="no", dropped_reason=UNSUPPORTED_REASON)
            elif any(w in a.quote for w in forbidden.get(a.check_id, [])):
                item.update(answer="no", dropped_reason=NEGATED_REASON)
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
CHANNEL_REASON = "인용이 허용되지 않은 채널의 글임"
SPLIT_SCENE = "negotiate"   # 분업 장면 id(엔진이 지원자가 먼저 분담을 꺼내면 앞당겨 실행하는 장면)
NEGATED_REASON = "인용이 반대 뜻(부정·제외)을 담고 있음"
UNSUPPORTED_REASON = "인용이 이 행동을 뒷받침하지 않음"
# 필수어·부정어 검사로 뺀 근거는 바로 버리지 않고 좁은 함의 검증으로 다시 본다(_recheck_by_entailment).
# 필수어 목록에 없는 말로 같은 행동을 한 지원자가 손해 보지 않게 하기 위함(2026-10-20 점검: "3번은 절반, 4번은 축소했습니다").
RECHECK_REASONS = (NEGATED_REASON, UNSUPPORTED_REASON)


async def _judge(session_id: str, situation: str, checks: list[dict], transcript: str,
                 hint: str, use_cache: bool) -> tuple[list[CheckAnswer], set[str]]:
    """체크리스트를 판별한다. 판별기가 빠뜨린 체크는 그 체크만 다시 묻는다.

    Returns:
        (체크마다 하나씩의 답, 다시 물어도 답이 없던 체크 id). 답이 없던 체크는 "no"로 채운다.
    """
    cfg = config()

    def note(c: dict) -> str:
        """체크마다 근거로 인정하는 기록 범위를 판별기에게 알린다(코드가 같은 범위로 다시 검증한다)."""
        chans = allowed_channels(c)
        if chans == SCOPE_CHANNELS["all"]:
            return ""
        return " [근거는 " + "·".join(CHANNEL_LABEL[ch] for ch in sorted(chans)) + "의 지원자 글에서만]"

    def ask(subset: list[dict]) -> str:
        questions = "\n".join(f"- {c['id']}: {c['question']}{note(c)}" for c in subset)
        return (f"상황: {situation}\n\n확인할 질문:\n{questions}\n\n{hint}\n\n기록:\n{transcript}\n\n"
                "질문마다 하나씩 답하라. 근거는 '지원자' 메시지나 '지원자(업무 메모)'에서만 인용한다.")

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


async def _entails(session_id: str, check: dict, quote: str, use_cache: bool) -> str:
    """좁은 함의 검증: 원문 한 줄이 체크의 행동을 보여 주나(supports), 반대인가(contradicts), 무관한가(unrelated).

    대화 전체를 보는 판별기와 달리 원문 한 줄과 행동 하나만 봐서 부정·제외 문맥을 정확히 읽는다.
    임베딩 유사도는 주제만 재서 "확률형 대신"(0.598)과 "확률형 추가"(0.620)를 가르지 못했다.
    """
    human = f"확인할 행동: {check.get('point', '')} ({check['question']})\n원문: {quote}"
    key = hashlib.sha256(json.dumps(["nli", config()["llm"]["model"], prompt("nli"), human],
                                    ensure_ascii=False).encode()).hexdigest()
    if use_cache and (cached := store.cache_get(key)) is not None:
        return cached["relation"]
    model = llm.chat_model(config()["llm"]["judge_temperature"]).with_structured_output(
        Relation, method="function_calling")
    out: Relation = await llm.ainvoke(model, [SystemMessage(prompt("nli")), HumanMessage(human)],
                                      "observe.nli", session_id)
    store.cache_put(key, {"relation": out.relation})
    return out.relation


async def _recheck_by_entailment(session_id: str, checks: list[dict], spec_checks: list[dict],
                                 use_cache: bool) -> list[dict]:
    """필수어가 없거나(표현 차이) 부정·제외 단어가 있어 뺀 근거를 좁은 함의 검증으로 다시 본다.

    원문 한 줄과 행동 하나만 보고 뒷받침하면 살린다. 아니면 뺀 채로 두고 판정(nli)을 남긴다.
    부정·제외 때문에 뺀 것은 리포트에 '판단이 필요한 원문'으로 남고, 필수어가 없어 뺀 것은 남기지 않는다.
    """
    specs = {c["id"]: c for c in spec_checks}
    out = []
    for c in checks:
        if c.get("dropped_reason") in RECHECK_REASONS:
            relation = await _entails(session_id, specs[c["check_id"]], c["quote"], use_cache)
            llm.trace({"session": session_id, "node": "observe.nli", "check": c["check_id"], "relation": relation})
            if relation == "supports":
                c = {k: v for k, v in c.items() if k != "dropped_reason"} | {"answer": "yes", "verified": True}
            else:
                c = {**c, "nli": relation}
        out.append(c)
    return out


def _mark_missing(checks: list[dict], missing: set[str]) -> list[dict]:
    """다시 물어도 답이 없던 체크에 이유를 남긴다(리포트에는 근거로 쓰이지 않고 내부 기록에만 남음)."""
    return [{**c, "dropped_reason": MISSING_REASON} if c["check_id"] in missing else c for c in checks]


def _format(messages: list[dict], pack: str | None = None) -> str:
    who = names(pack)

    def speaker(m: dict) -> str:
        if m["channel"] == MEMO_CHANNEL and m["sender"] == "candidate":
            return "지원자(업무 메모)"
        return who[m["sender"]]
    lines = [f"[id=m{m['id']}][{clock.format_minute(m['minute'])}][{CHANNEL_LABEL[m['channel']]}] "
             f"{speaker(m)}: {m['content']}" for m in messages]
    return "\n".join(lines) or "(기록 없음)"


async def observe_element(session_id: str, spec: dict, run_minutes: dict[str, float],
                          mode: str = "hybrid", use_cache: bool = True) -> dict:
    """역량 하나를 판별한다.

    Returns:
        {"element", "situation", "happened", "checks": [...]} — checks는 내부용 판별 결과.
    """
    base = {"element": spec["name"], "situation": spec["situation"]}
    if spec["scene"] not in run_minutes:
        return {**base, "happened": False, "checks": []}
    pack = store.get_session(session_id)["scenario"]
    start, end = run_minutes[spec["scene"]], spec["window_end_minute"]
    if spec.get("window_end_scene") in run_minutes:   # 그 장면이 더 일찍 실행됐으면 거기서 구간을 끝낸다
        end = min(end, run_minutes[spec["window_end_scene"]])
    messages = _window_messages(session_id, start, end)
    hits = _keyword_hits(messages, spec["candidate_keywords"])
    # 관찰 구간 안의 지원자 메시지 위치(판단 없음). 관찰 없음일 때 담당자가 원문을 직접 보도록 리포트에 링크한다.
    base["followups"] = [{"id": m["id"], "minute": m["minute"]} for m in messages
                         if m["sender"] == "candidate" and m["channel"] in CHAT_CHANNELS]
    rule_checks = _with_points(run_rules(session_id, spec, run_minutes), spec.get("rules", []))

    if mode == "rules":   # A: 키워드에 걸린 지원자 메시지가 있으면 관련 행동 있음
        checks = [{"check_id": "keyword", "answer": "yes" if hits else "no",
                   "evidence_id": f"m{min(hits)}" if hits else "", "verified": bool(hits)}]
        return {**base, "happened": True, "checks": checks + rule_checks}

    hint = ("참고: 키워드 후보 메시지 id = " + ", ".join(f"m{i}" for i in sorted(hits))
            + " (후보일 뿐이다. 다른 메시지도 모두 본다)") if hits else "키워드 후보 없음. 모든 메시지를 본다."
    transcript = _format(messages, pack)
    answers, missing = await _judge(session_id, spec["situation"], spec["checks"], transcript, hint, use_cache)
    evidence, channels_of = _evidence_texts(messages), _channels_of(messages)
    allowed = {c["id"]: allowed_channels(c) for c in spec["checks"]}
    required = {c["id"]: c["quote_must_contain"] for c in spec["checks"] if c.get("quote_must_contain")}
    forbidden = {c["id"]: c["quote_must_not_contain"] for c in spec["checks"] if c.get("quote_must_not_contain")}

    def check_all(raw: list[CheckAnswer], still_missing: set[str]) -> list[dict]:
        return _mark_missing(verify(raw, evidence, allowed, required, forbidden, channels_of), still_missing)

    checks = await _recheck_by_entailment(session_id, check_all(answers, missing), spec["checks"], use_cache)
    # 함의 검증으로도 뒷받침되지 않은 부정·제외 원문은 버리지 않고 '판단이 필요한 원문'으로 남긴다(PRD 5.5)
    negated = [{"evidence_id": c["evidence_id"], "quote": c["quote"]} for c in checks
               if c.get("dropped_reason") == NEGATED_REASON]
    for _ in range(config()["observation"]["judge_dropped_retries"]):
        # 근거가 검증에서 버려진 체크만 이유를 알려 주고 다시 묻는다(검증 → 피드백 → 재시도)
        dropped = {c["check_id"]: c["dropped_reason"] for c in checks
                   if c.get("dropped_reason") and c["dropped_reason"] != MISSING_REASON}
        if not dropped:
            break
        notes = "; ".join(f"{cid}: {reason}" for cid, reason in dropped.items())
        retry_hint = (f"{hint}\n앞선 답의 근거가 코드 검증에서 버려졌다({notes}). '지원자' 메시지(또는 허용된 업무 메모) 중에서 "
                      "그 질문이 묻는 행동을 직접 보여 주는 원문을 다시 찾아 인용하라. 동료·팀장의 말과 업무 지문은 근거가 아니다. "
                      "질문이 정한 채널 밖의 글도 근거가 아니다. 없으면 no.")
        sub = [c for c in spec["checks"] if c["id"] in dropped]
        again, still_missing = await _judge(session_id, spec["situation"], sub, transcript, retry_hint, use_cache)
        redo = {c["check_id"]: c for c in await _recheck_by_entailment(session_id, check_all(again, still_missing),
                                                                spec["checks"], use_cache)}
        checks = [redo.get(c["check_id"], c) if c["check_id"] in dropped else c for c in checks]
        negated += [{"evidence_id": c["evidence_id"], "quote": c["quote"]} for c in redo.values()
                    if c.get("dropped_reason") == NEGATED_REASON]
    return {**base, "happened": True, "checks": _with_points(checks, spec["checks"]) + rule_checks,
            "negated": negated}


# ---------- 규칙 관찰 (LLM 없음, 기록으로 확정) ----------

def _first_candidate_after(session_id: str, since: float, channels: tuple[str, ...], pack: str | None,
                           until: float = float("inf")) -> dict | None:
    """since 이후(until 이전) 지원자가 이 채널에서 팀장에게 보낸 첫 메시지. 팀 채널은 동료를 부른 말을 뺀다."""
    for m in store.list_messages(session_id, channels=channels):
        if m["sender"] != "candidate" or not since <= m["minute"] <= until:
            continue
        if m["channel"] == "team" and persona.team_responder(m["content"], pack) != "lead":
            continue
        return m
    return None


def rule_contacted_lead_first(session_id: str, run_minutes: dict[str, float], pack: str | None) -> list[dict]:
    """팀장이 진행 상황을 먼저 묻기 전에 지원자가 팀장에게 연락했는가.

    skip_if_candidate_wrote 장면(팀장 확인)이 지원자의 먼저 연락으로 건너뛰어졌으면 yes. 근거는 그 첫 메시지.
    """
    for sc in scenes(pack):
        cond = sc.get("skip_if_candidate_wrote")
        if not cond or f"skipped:{sc['id']}" not in run_minutes or cond["since_scene"] not in run_minutes:
            continue
        m = _first_candidate_after(session_id, run_minutes[cond["since_scene"]], tuple(cond["channels"]), pack)
        if m:
            return [{"check_id": "contacted_lead_first", "answer": "yes", "verified": True,
                     "evidence_id": f"m{m['id']}", "quote": m["content"],
                     "behavior": "팀장이 진행 상황을 묻기 전에 먼저 팀장에게 연락함"}]
    return []


def rule_early_progress_check(session_id: str, run_minutes: dict[str, float], pack: str | None) -> list[dict]:
    """지원자가 동료에게 진행 상황을 물어, 동료의 지연을 정해진 시각보다 일찍 알게 됐는가.

    early_on_progress_check 장면이 정해진 시각보다 일찍 실행됐으면 yes. 근거는 직전의 진행 상황 질문.
    """
    for sc in scenes(pack):
        ran = run_minutes.get(sc["id"])
        if not sc.get("early_on_progress_check") or ran is None or ran >= sc["at_minute"]:
            continue
        asks = [m for m in store.list_messages(session_id, channels=("dm", "team"))
                if m["sender"] == "candidate" and m["minute"] <= ran and is_progress_question(m["content"])]
        if asks:
            m = asks[-1]
            return [{"check_id": "early_progress_check", "answer": "yes", "verified": True,
                     "evidence_id": f"m{m['id']}", "quote": m["content"],
                     "behavior": f"동료에게 진행 상황을 물어 동료 업무 지연을 {clock.format_minute(ran)}에 알게 됨"}]
    return []


def rule_initiated_split(session_id: str, run_minutes: dict[str, float], pack: str | None) -> list[dict]:
    """동료가 분담안을 내기 전에 지원자가 먼저 업무 분담(계획)을 꺼냈는가.

    지원자가 분담을 꺼내면 엔진이 분업 장면을 정해진 시각보다 일찍 실행한다(engine.start_split_early).
    그 기록과, 그때 분담을 꺼낸 지원자 메시지로 확정한다.
    """
    phrases = config()["negotiation"]["split_request_phrases"]
    sc = next((s for s in scenes(pack) if s["id"] == SPLIT_SCENE), None)
    ran = run_minutes.get(SPLIT_SCENE)
    if sc is None or ran is None or ran >= sc["at_minute"]:
        return []
    m = next((m for m in store.list_messages(session_id, channels=("dm",))
              if m["sender"] == "candidate" and m["minute"] <= ran and any(p in m["content"] for p in phrases)), None)
    if m is None:
        return []
    return [{"check_id": "initiated_split", "answer": "yes", "verified": True, "evidence_id": f"m{m['id']}",
             "quote": m["content"], "behavior": "동료가 분담안을 내기 전에 먼저 업무 분담을 꺼냄"}]


def rule_memo_followed_through(session_id: str, run_minutes: dict[str, float], pack: str | None) -> list[dict]:
    """업무 메모에 적은 소통 계획(예: '팀장님께 지연 보고')을 그 뒤 실제 메시지로 실행했는가.

    메모에 소통 표현(config [work].memo_contact_words)과 상대(팀장·동료 이름)가 있으면, 메모 뒤 그 상대에게 보낸
    첫 메시지를 찾는다. 있으면 yes(근거는 그 메시지). 실행하지 않은 경우는 근거가 없으므로 기록하지 않는다.
    """
    info = pack_info(pack)
    words = config()["work"]["memo_contact_words"]
    targets = {"lead": ("팀장", info["lead_name"], info["lead_short"]),
               "coworker": (info["coworker_name"], info["coworker_short"], "동료")}
    channels = {"lead": ("lead_dm", "team"), "coworker": ("dm", "team")}
    msgs = store.list_messages(session_id, channels=("work", *CHAT_CHANNELS))
    for memo in [m for m in msgs if m["channel"] == MEMO_CHANNEL and m["sender"] == "candidate"]:
        if not any(w in memo["content"] for w in words):
            continue
        for role, names_ in targets.items():
            if not any(n in memo["content"] for n in names_):
                continue
            sent = next((m for m in msgs if m["sender"] == "candidate" and m["minute"] >= memo["minute"]
                         and m["id"] > memo["id"] and m["channel"] in channels[role]
                         and (m["channel"] != "team" or persona.team_responder(m["content"], pack) == role)), None)
            if sent:
                who = "팀장" if role == "lead" else "동료"
                return [{"check_id": "memo_followed_through", "answer": "yes", "verified": True,
                         "evidence_id": f"m{sent['id']}", "quote": sent["content"],
                         "behavior": f"업무 메모('{memo['content']}')에 적은 대로 {who}에게 메시지를 보냄"}]
    return []


RULES = {"contacted_lead_first": rule_contacted_lead_first, "early_progress_check": rule_early_progress_check,
         "memo_followed_through": rule_memo_followed_through, "initiated_split": rule_initiated_split}


def run_rules(session_id: str, spec: dict, run_minutes: dict[str, float]) -> list[dict]:
    """요소의 규칙 관찰(observation.toml rules)을 실행한다."""
    pack = store.get_session(session_id)["scenario"]
    out = []
    for rule in spec.get("rules", []):
        out += RULES[rule["id"]](session_id, run_minutes, pack)
    return out


async def observe_session(session_id: str, mode: str = "hybrid", use_cache: bool = True) -> list[dict]:
    """세션의 역량 5개를 판별한다."""
    run_minutes = store.scene_run_minutes(session_id)
    return [await observe_element(session_id, spec, run_minutes, mode, use_cache)
            for spec in observation_spec(store.get_session(session_id)["scenario"])["element"]]
