"""업무 메모 분류 (에이전트 쪽): LLM은 메모에서 범주 하나와 업무 번호 하나만 고른다.

진척 계산·지문은 gateway 쪽 app/work.py가 코드로 한다. 레퍼런스 금지 사항(업무 서버는 LLM을 부르지 않는다)에 맞춰
LLM을 부르는 이 분류만 에이전트 쪽으로 분리했다. gateway는 app/agent_client.classify_memo로 부른다.
"""
import hashlib
import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app import llm, store
from app.settings import PART_IDS, config, part_titles
from app.work import FALLBACK_CATEGORY, categories

log = logging.getLogger("dayzero.memo_agent")
CLASSIFY_VERSION = "v2"   # 분류 지시문·형식을 바꾸면 올린다(캐시 무효화)


class MemoClass(BaseModel):
    category: str = Field(description="범주 id 하나")
    part: str = Field(default="", description="메모가 진행하겠다는 업무 번호 하나('1'~'5'). 없으면 빈 문자열")


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
