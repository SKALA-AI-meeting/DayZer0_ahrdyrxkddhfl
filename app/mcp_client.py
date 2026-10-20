"""직접 만든 MCP 서버 2개(stdio)에 연결한다.

모델에게 주는 문서 도구는 session_minute 인자를 숨긴 래퍼다. 시각은 코드가 넣으므로
모델이 시각을 바꿔 아직 공개되지 않은 문서를 읽을 수 없다(F-15).
"""
import os
import sys
from contextlib import asynccontextmanager

from langchain_core.tools import StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from app.settings import ROOT


def _client() -> MultiServerMCPClient:
    # stdio 자식 프로세스는 환경변수를 일부만 물려받으므로 필요한 것만 명시해서 넘긴다.
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DAYZERO_DB", "TMPDIR")}
    def conn(module: str) -> dict:
        return {"transport": "stdio", "command": sys.executable, "args": ["-m", module],
                "cwd": str(ROOT), "env": env}
    return MultiServerMCPClient({"rag": conn("mcp_servers.rag"),
                                 "business": conn("mcp_servers.business")})


@asynccontextmanager
async def doc_tools(session_minute: float):
    """rag-mcp에 연결해, 시각이 고정된 문서 도구 3개를 모델용으로 돌려준다."""
    async with _client().session("rag") as session:
        raw = {t.name: t for t in await load_mcp_tools(session)}

        async def search_company_docs(query: str) -> str:
            """회사 문서(지표, 정책, 회의록, 소개, 인터뷰 등)에서 관련 문단을 찾는다. 결과의 title을 출처로 밝힌다."""
            return await raw["search_company_docs"].ainvoke(
                {"query": query, "session_minute": session_minute})

        async def read_company_doc(doc_id: str) -> str:
            """회사 문서 전문을 읽는다. doc_id는 list_company_docs 결과의 doc_id."""
            return await raw["read_company_doc"].ainvoke(
                {"doc_id": doc_id, "session_minute": session_minute})

        async def list_company_docs() -> str:
            """지금 읽을 수 있는 회사 문서 목록을 본다."""
            return await raw["list_company_docs"].ainvoke({"session_minute": session_minute})

        yield [StructuredTool.from_function(coroutine=fn, name=fn.__name__,
                                            description=fn.__doc__)
               for fn in (search_company_docs, read_company_doc, list_company_docs)]


async def _spec_only(**_kwargs) -> str:
    raise RuntimeError("설명용 도구입니다. 실행은 doc_tools()로 연결한 도구로 합니다.")


def doc_tool_specs() -> list[StructuredTool]:
    """모델에게 도구 목록만 알려 줄 때 쓰는 설명용 도구(이름·설명·인자가 doc_tools와 같다).

    모델이 도구를 고르는 단계마다 MCP 서버를 띄우지 않으려고 분리했다. 실행은 doc_tools로 한다.
    """
    specs = [("search_company_docs", "회사 문서(지표, 정책, 회의록, 소개, 인터뷰 등)에서 관련 문단을 찾는다. 결과의 title을 출처로 밝힌다.",
              {"query": (str, ...)}),
             ("read_company_doc", "회사 문서 전문을 읽는다. doc_id는 list_company_docs 결과의 doc_id.",
              {"doc_id": (str, ...)}),
             ("list_company_docs", "지금 읽을 수 있는 회사 문서 목록을 본다.", {})]
    from pydantic import create_model
    return [StructuredTool.from_function(coroutine=_spec_only, name=name, description=desc,
                                         args_schema=create_model(f"{name}_args", **fields))
            for name, desc, fields in specs]


async def call_business(tool_name: str, args: dict) -> str:
    """business-mcp 도구를 코드에서 직접 호출한다 (예: 분업 확정 후 assign_tasks)."""
    async with _client().session("business") as session:
        tools = {t.name: t for t in await load_mcp_tools(session)}
        return await tools[tool_name].ainvoke(args)
