"""아키텍처 경계 검사: 레퍼런스 아키텍처(교안 52쪽)의 금지 사항을 코드가 어기지 않는지 본다.

- Agent는 DB에 직접 쓰지 않는다 → 업무 데이터 변경은 business-mcp 변경 도구로만.
- rag-mcp는 업무 데이터를 바꾸지 않는다 → 회사 문서 등록 외에는 쓰지 않는다.
- 에이전트는 지원자 이름으로 메시지를 쓸 수 없다(business-mcp가 거부).
- 업무 서버(gateway)는 LLM을 부르지 않고 에이전트 모듈을 직접 부르지 않는다 → app/agent_client.py로만.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

from app import mcp_client, settings, store

ROOT = settings.ROOT
# 에이전트(LangGraph·LLM) 쪽 모듈. gateway 쪽(api·auth·engine·clock·work·store)은 업무 데이터를 직접 다룬다.
AGENT_MODULES = ["graph.py", "speak_graph.py", "persona.py", "doc_graph.py", "observe.py", "report.py"]
# 업무 데이터를 바꾸는 저장소 함수. cache_put(LLM 판별 캐시)은 에이전트 자신의 상태라 제외한다(checkpointer와 같은 성격).
STORE_WRITE = re.compile(r"store\.(add|update|set|save|create|claim|delete|put_query)_?\w*\(")


@pytest.mark.parametrize("module", AGENT_MODULES)
def test_agent_does_not_write_db_directly(module):
    code = (ROOT / "app" / module).read_text(encoding="utf-8")
    assert not STORE_WRITE.findall(code), f"{module}가 DB에 직접 씀: business-mcp 변경 도구를 써야 함"


def test_rag_mcp_does_not_change_business_data():
    code = (ROOT / "mcp_servers" / "rag.py").read_text(encoding="utf-8")
    writes = set(re.findall(r"store\.((?:add|update|set|save|create|claim|delete)_\w*)\(", code))
    assert writes <= {"save_company_doc", "set_doc_request_status"}, writes


def test_agent_cannot_post_as_candidate():
    from mcp_servers import business
    sid = store.create_session("x")
    with pytest.raises(ValueError, match="에이전트가 쓸 수 없는"):
        business.post_message(sid, "dm", "candidate", "제가 다 할게요", 3)
    business.post_message(sid, "dm", "coworker", "안녕하세요", 3)
    assert [m["sender"] for m in store.list_messages(sid)] == ["coworker"]


def test_business_mcp_write_over_real_stdio_connection():
    """실제 MCP 연결(stdio)로 변경 도구를 불러 DB에 반영되는지(테스트의 다른 곳은 같은 프로세스 호출로 대신함)."""
    sid = store.create_session("x")

    async def run():
        await mcp_client.call_business("post_message", {"session_id": sid, "channel": "team", "sender": "lead",
                                                        "text": "킥오프", "minute": 0})
        first = json.loads(await mcp_client.call_business("claim_scene", {"session_id": sid, "scene_key": "k",
                                                                          "minute": 0}))
        again = json.loads(await mcp_client.call_business("claim_scene", {"session_id": sid, "scene_key": "k",
                                                                          "minute": 0}))
        return first["claimed"], again["claimed"]
    assert asyncio.run(run()) == (True, False)
    assert [m["content"] for m in store.list_messages(sid)] == ["킥오프"]


# 업무 서버(레퍼런스의 Spring Boot) 쪽 모듈과, 그쪽이 직접 불러오면 안 되는 에이전트 모듈
GATEWAY_MODULES = ["api.py", "engine.py", "auth.py", "clock.py", "work.py", "store.py"]
AGENT_ONLY = re.compile(r"^\s*(?:from app(?:\.\w+)? import .*\b(graph|speak_graph|doc_graph|report|memo_agent|progress)\b"
                        r"|import app\.(graph|speak_graph|doc_graph|report|memo_agent|progress)\b)", re.M)


@pytest.mark.parametrize("module", GATEWAY_MODULES)
def test_gateway_does_not_call_llm_or_agent_directly(module):
    code = (ROOT / "app" / module).read_text(encoding="utf-8")
    assert "llm.chat_model" not in code and "llm.ainvoke" not in code, f"{module}가 LLM을 직접 부름"
    assert not AGENT_ONLY.findall(code), f"{module}가 에이전트 모듈을 직접 불러옴: app/agent_client.py를 써야 함"
