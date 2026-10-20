"""LLM 호출 + Recovery(재시도) + Observability(호출 기록).

- 모델은 config [llm].model (팀 키 제약으로 gpt-4o-mini).
- 429·5xx·타임아웃·연결 오류만 retry_count번 재시도한다. 대체 모델은 없다(PRD A.4).
- 호출마다 노드 이름, 지연, 토큰, 시도 횟수를 data/trace.jsonl 에 남긴다.
  LANGSMITH_TRACING=true 와 키가 있으면 LangChain이 LangSmith로도 보낸다.
"""
import asyncio
import json
import time

import openai
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from app.settings import ROOT, config

# override=True: 셸에 다른 OPENAI_API_KEY가 있어도 이 프로젝트 .env 값을 쓴다.
load_dotenv(ROOT / ".env", override=True)

RETRYABLE = (openai.RateLimitError, openai.InternalServerError,
             openai.APITimeoutError, openai.APIConnectionError)
TRACE_PATH = ROOT / "data" / "trace.jsonl"


def chat_model(temperature: float) -> ChatOpenAI:
    """재시도는 우리가 직접 하므로 클라이언트 자체 재시도는 끈다."""
    return ChatOpenAI(model=config()["llm"]["model"], temperature=temperature, max_retries=0,
                      timeout=config()["agent_loop"]["task_timeout_seconds"])


def trace(record: dict) -> None:
    """호출·검증 기록을 data/trace.jsonl에 한 줄로 남긴다(Observability)."""
    TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TRACE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


async def ainvoke(runnable, payload, node: str, session_id: str = ""):
    """runnable.ainvoke를 재시도·기록과 함께 실행한다.

    Args:
        runnable: ChatOpenAI 또는 bind_tools / with_structured_output 결과.
        payload: 메시지 목록.
        node: 기록용 노드 이름 (예: "persona.coworker").
        session_id: 기록용 세션 id.

    Raises:
        openai.OpenAIError: 재시도 후에도 실패하거나 재시도 대상이 아닌 오류.
    """
    llm_cfg = config()["llm"]
    attempts = llm_cfg["retry_count"] + 1
    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            result = await runnable.ainvoke(payload)
        except RETRYABLE as exc:
            trace({"ts": time.time(), "session": session_id, "node": node, "attempt": attempt,
                    "error": type(exc).__name__})
            if attempt == attempts:
                raise
            await asyncio.sleep(llm_cfg["retry_backoff_seconds"] * attempt)
            continue
        usage = getattr(result, "usage_metadata", None) or {}
        trace({"ts": time.time(), "session": session_id, "node": node, "attempt": attempt,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens")})
        return result
    raise RuntimeError("도달할 수 없는 코드")   # pragma: no cover
