import json

import pytest

from app import settings


async def inprocess_business(tool: str, args: dict) -> str:
    """business-mcp 도구 함수를 같은 프로세스에서 부른다(테스트용 call_business 대신).

    에이전트의 쓰기는 모두 business-mcp를 거치므로, 호출마다 stdio 자식 프로세스를 띄우면 테스트가 너무 느려진다.
    도구 함수 자체(값 검사 포함)는 그대로 실행되고, 실제 MCP 연결은 test_architecture.py의 stdio 테스트가 확인한다.
    """
    from mcp_servers import business
    try:
        result = getattr(business, tool)(**args)
    except Exception as exc:   # 실제 MCP처럼 실패를 RuntimeError로 올린다(mcp_client.check_result와 같은 동작)
        raise RuntimeError(f"Error executing tool {tool}: {exc}") from exc
    return json.dumps(result, ensure_ascii=False)


@pytest.fixture(autouse=True)
def business_inprocess(monkeypatch):
    """에이전트 모듈(graph·report·doc_graph)의 business-mcp 호출을 같은 프로세스 호출로 바꾼다."""
    from app import doc_graph, graph, report
    for module in (graph, report, doc_graph):
        monkeypatch.setattr(module, "call_business", inprocess_business)


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """테스트마다 빈 SQLite 파일을 쓴다. 검색은 기본 키워드(테스트에서 OpenAI를 부르지 않음)."""
    monkeypatch.setenv("DAYZERO_DB", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("LANGSMITH_TRACING", "false")   # 테스트(가짜 LLM)는 LangSmith로 trace를 보내지 않는다
    monkeypatch.setitem(settings.config()["rag"], "search_mode", "keyword")
    # 테스트마다 임시 DB를 쓰므로 MCP 서버도 그 DB를 보도록 자식 프로세스(stdio)로 띄운다(HTTP 서버는 다른 DB를 봄)
    monkeypatch.setitem(settings.config()["mcp"], "transport", "stdio")
    # 에이전트 서버(HTTP) 대신 같은 프로세스에서 부른다(테스트는 에이전트 함수를 가짜로 바꿔 흐름만 본다)
    monkeypatch.setitem(settings.config()["agent"], "mode", "inprocess")
    yield


@pytest.fixture
def late_interviews(monkeypatch):
    """문서 공개 시각 장치(F-15) 검사용: 인터뷰 요약을 01:05(65분)에 공개되는 문서로 바꾼다.

    지금 시나리오는 업무를 지문으로 처리해 모든 문서를 처음부터 공개하지만, 늦게 공개하는 장치(검색·열람 차단,
    공개 알림, 발화 검증)는 다른 시나리오가 쓸 수 있게 남아 있다.
    """
    patched = tuple({**d, "available_from_minute": 65} if d["id"] == "interviews" else d
                    for d in settings.docs_index())
    monkeypatch.setattr(settings, "docs_index", lambda pack=None: patched)
