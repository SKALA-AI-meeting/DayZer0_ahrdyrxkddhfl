"""업무 메모와 진척: 업무는 직접 하지 않고, 무엇을 할지 적으면 그 업무가 한 칸 오르고 정해진 지문이 이어진다.

능력(문서 작성 실력·속도)이 아니라 태도를 보기 위해 업무 자체를 지문으로 처리한다(팀 결정 2026-10-20).
1. 지원자가 다음 몇 분 동안 할 일을 짧게 적는다(주관식 업무 메모, config [work].memo_max_chars).
2. LLM이 메모에서 범주 하나와 업무 번호 하나를 고른다(temperature 0, 캐시). 여러 업무를 적으면 가장 먼저 적은 하나.
3. 코드가 그 업무를 한 칸 올린다. 업무별 칸 수(pack.toml [part_units])와 장면 영향(work.toml [[effect]])도 코드가 적용한다.
4. 지문은 work.toml 틀에 코드가 계산한 진척을 넣어 만든다 → LLM은 결과를 지어내지 않는다.
대화는 업무를 진행시키지 않는다. 진척은 업무 메모 기록(work_memos)으로만 계산한다.
"""
import hashlib
import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app import llm, store
from app.settings import PART_IDS, config, pack_info, part_titles, work_spec

log = logging.getLogger("dayzero.work")
FALLBACK_CATEGORY = "other"
CLASSIFY_VERSION = "v2"   # 분류 지시문·형식을 바꾸면 올린다(캐시 무효화)
EFFECT_NOTICE_PREFIX = "effect:"   # 장면 영향 알림을 한 번만 보내게 장면 실행 기록에 남기는 키


class MemoClass(BaseModel):
    category: str = Field(description="범주 id 하나")
    part: str = Field(default="", description="메모가 진행하겠다는 업무 번호 하나('1'~'5'). 없으면 빈 문자열")


def categories(pack: str | None = None) -> dict[str, dict]:
    """범주 id → 정의(label, description, progress, 지문 틀)."""
    return {c["id"]: c for c in work_spec(pack)["category"]}


def units(pack: str | None = None) -> dict[str, int]:
    """업무 번호 → 끝내는 데 필요한 칸 수."""
    return {k: int(v) for k, v in pack_info(pack)["part_units"].items()}


def part_name(part: str, pack: str | None = None) -> str:
    return f"{part}번 {part_titles(pack)[part]}"


def stage(session_id: str, pack: str | None = None) -> str:
    """지금 진행 단계. 실행된 장면으로 정한다(work.toml [[stage]], 위에서부터)."""
    ran = store.run_scene_ids(session_id)
    for st in work_spec(pack).get("stage", []):
        if st["after_scene"] in ran:
            return st["id"]
    return "before"


# ---------- 진척 계산 (코드, LLM 없음) ----------

def _active_effects(session_id: str, pack: str | None) -> list[tuple[dict, float]]:
    runs = store.scene_run_minutes(session_id)
    return [(e, runs[e["after_scene"]]) for e in work_spec(pack).get("effect", []) if e["after_scene"] in runs]


def progress(session_id: str, pack: str | None = None) -> dict[str, int]:
    """업무 번호 → 지금 진척 칸. 진척이 오른 업무 메모(advanced)만 세고, 장면 영향(lose·max)을 적용한다."""
    msgs = {m["id"]: m["minute"] for m in store.list_messages(session_id, channels=("work",))}
    steps: dict[str, list[float]] = {p: [] for p in units(pack)}
    for mid, rec in store.work_memos(session_id).items():
        if rec["advanced"] and rec["part"] in steps and mid in msgs:
            steps[rec["part"]].append(msgs[mid])
    value = {p: len(s) for p, s in steps.items()}
    for effect, at in _active_effects(session_id, pack):
        p = effect["part"]
        before = sum(1 for m in steps[p] if m < at)
        value[p] = min(max(before - effect.get("lose", 0), 0) + (len(steps[p]) - before), effect.get("max", units(pack)[p]))
    return {p: min(v, units(pack)[p]) for p, v in value.items()}


def cap_reason(session_id: str, part: str, pack: str | None = None) -> str:
    """이 업무가 장면 영향 때문에 더 오를 수 없으면 그 이유, 아니면 빈 문자열."""
    now = progress(session_id, pack)
    for effect, _ in _active_effects(session_id, pack):
        if effect["part"] == part and now[part] >= effect.get("max", units(pack)[part]):
            return effect["reason"]
    return ""


def summary(session_id: str, pack: str | None = None) -> str:
    """업무별 진척 한 줄 요약(보고 직전 지문용). 예: '1번 지표 분석 1/2칸, 2번 … 2/2칸(완료)'."""
    now, need = progress(session_id, pack), units(pack)
    parts = [f"{part_name(p, pack)} {now[p]}/{need[p]}칸" + ("(완료)" if now[p] >= need[p] else "")
             for p in PART_IDS if p in need]
    return ", ".join(parts) + "."


def announce_effects(session_id: str, minute: float, pack: str | None = None) -> None:
    """실행된 장면의 업무 영향(예: 대상 변경 → 지표 분석 진척 일부 무효)을 업무 기록에 한 번 알린다."""
    for effect, _ in _active_effects(session_id, pack):
        if effect.get("notice") and store.claim_scene(session_id, f"{EFFECT_NOTICE_PREFIX}{effect['after_scene']}:{effect['part']}", minute):
            store.add_message(session_id, "work", "narrator", f"[업무 영향] {effect['notice']}.", minute)


# ---------- 메모 분류 (LLM은 범주·업무 번호만 고른다) ----------

def _system(session_id: str, pack: str | None) -> str:
    assignment = store.get_session(session_id)["assignment"]
    owner = {"candidate": "지원자", "coworker": "동료", "both": "함께"}
    cats = "\n".join(f"- {cid}: {c['description']}" for cid, c in categories(pack).items())
    parts = "\n".join(f"- {p}: {part_titles(pack)[p]} (담당: {owner.get(assignment.get(p, ''), '')})" for p in PART_IDS)
    return ("업무 메모를 분류한다. 메모에 적힌 행동만 본다. 추측하지 않는다.\n"
            "1) 범주를 하나 고른다. 2) 메모가 진행하겠다는 업무 번호를 하나 고른다(번호나 업무 이름으로 알 수 있을 때만, 없으면 빈 값).\n"
            "여러 업무를 적었으면 가장 먼저 적은 업무 하나만 고른다.\n\n"
            f"범주:\n{cats}\n\n업무:\n{parts}")


async def classify(session_id: str, memo: str, pack: str | None = None) -> tuple[str, str]:
    """업무 메모 → (범주 id, 업무 번호). 실패하거나 모르는 값이면 ('other', '')."""
    system = _system(session_id, pack)
    key = hashlib.sha256(json.dumps([CLASSIFY_VERSION, config()["llm"]["model"], system, memo],
                                    ensure_ascii=False).encode()).hexdigest()
    if (cached := store.cache_get(key)) is not None:
        return cached["category"], cached["part"]
    try:
        model = llm.chat_model(config()["llm"]["judge_temperature"]).with_structured_output(
            MemoClass, method="function_calling")
        out: MemoClass = await llm.ainvoke(model, [SystemMessage(system), HumanMessage(f"메모: {memo}")],
                                           "work.classify", session_id)
        category = out.category if out.category in categories(pack) else FALLBACK_CATEGORY
        part = out.part.strip().rstrip("번") if out.part.strip().rstrip("번") in PART_IDS else ""
    except Exception:   # 분류는 진척·지문에만 쓴다. 실패하면 원인을 남기고 '기타'로 진행한다(세션은 멈추지 않음)
        log.exception("업무 메모 분류 실패: session=%s", session_id)
        return FALLBACK_CATEGORY, ""
    store.cache_put(key, {"category": category, "part": part})
    return category, part


def resolve(session_id: str, category: str, part: str, pack: str | None = None) -> tuple[str, str]:
    """분류 결과를 진행할 업무로 확정한다(코드 규칙).

    - 업무 번호가 있으면 그 업무. 범주는 담당자로 정한다(지원자·함께 → 내 업무, 동료 → 동료 업무 메우기).
    - 번호 없이 '내 업무'·'동료 업무'면 그 담당의 아직 안 끝난 첫 업무(모두 끝났으면 첫 업무).
    - 계획·기타는 업무를 진행하지 않는다.
    """
    cats = categories(pack)
    if not cats.get(category, {}).get("progress") and not part:
        return category, ""
    assignment = store.get_session(session_id)["assignment"]
    if not part:
        owners = ("candidate", "both") if category == "own" else ("coworker",)
        mine = [p for p in PART_IDS if assignment.get(p) in owners]
        now, need = progress(session_id, pack), units(pack)
        part = next((p for p in mine if now[p] < need[p]), mine[0] if mine else "")
        if not part:
            return FALLBACK_CATEGORY, ""
    return ("cover" if assignment.get(part) == "coworker" else "own"), part


def narration(session_id: str, category: str, part: str, advanced: bool, pack: str | None = None) -> str:
    """범주·업무·진척에 맞는 지문. 사실(진척 칸)은 코드가 계산한 값만 넣는다."""
    c = categories(pack).get(category) or categories(pack)[FALLBACK_CATEGORY]
    st = stage(session_id, pack)
    if not c.get("progress"):
        return c.get(st) or c["before"]
    name, now, need = part_name(part, pack), progress(session_id, pack), units(pack)
    if advanced:
        text = c["progressed"].format(part=name, done=now[part], units=need[part])
    elif reason := cap_reason(session_id, part, pack):
        text = f"{name}: {reason}."
    else:
        text = c["already_done"].format(part=name)
    flavor = c.get(st, "")
    return f"{text} {flavor}".strip()


def label(category: str, pack: str | None = None) -> str:
    c = categories(pack).get(category) or categories(pack)[FALLBACK_CATEGORY]
    return c["label"]


def can_advance(session_id: str, part: str, pack: str | None = None) -> bool:
    """이 업무를 한 칸 더 올릴 수 있는가(다 끝났거나 장면 영향으로 막히면 False)."""
    return bool(part) and progress(session_id, pack)[part] < units(pack)[part] and not cap_reason(session_id, part, pack)


def task_status(done: int, need: int) -> str:
    """진척 칸 → 태스크 카드 상태."""
    return "done" if done >= need else "doing" if done > 0 else "todo"
