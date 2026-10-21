"""직접 만든 MCP 서버 2개에 연결한다(config [mcp].transport: http = 따로 떠 있는 서버, stdio = 자식 프로세스).

모델에게 주는 문서 도구는 session_minute 인자를 숨긴 래퍼다. 시각은 코드가 넣으므로
모델이 시각을 바꿔 아직 공개되지 않은 문서를 읽을 수 없다(F-15).
"""
import os
import sys
from contextlib import asynccontextmanager

from langchain_core.tools import StructuredTool
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from app.settings import ROOT, config


def _client() -> MultiServerMCPClient:
    """config [mcp].transport에 따라 MCP 서버 2개에 붙는 클라이언트를 만든다.

    http: 따로 떠 있는 서버(Streamable HTTP)에 주소로 연결한다(레퍼런스 아키텍처).
    stdio: 서버를 자식 프로세스로 띄운다(테스트용).
    """
    cfg = config()["mcp"]
    if cfg["transport"] == "http":
        def conn(name: str) -> dict:
            return {"transport": "streamable_http",
                    "url": f"http://{cfg['host']}:{cfg[f'{name}_port']}/mcp"}
        return MultiServerMCPClient({"rag": conn("rag"), "business": conn("business")})
    # stdio 자식 프로세스는 환경변수를 일부만 물려받으므로 필요한 것만 명시해서 넘긴다.
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DAYZERO_DB", "TMPDIR")}
    def conn(module: str) -> dict:
        return {"transport": "stdio", "command": sys.executable, "args": ["-m", module],
                "cwd": str(ROOT), "env": env}
    return MultiServerMCPClient({"rag": conn("mcp_servers.rag"),
                                 "business": conn("mcp_servers.business")})


@asynccontextmanager
async def doc_tools(session_minute: float, company: str = "", scenario: str = ""):
    """rag-mcp에 연결해, 시각·기업이 고정된 문서 도구 3개를 모델용으로 돌려준다."""
    client = _client()
    async with client.session("rag") as session:
        raw = {t.name: t for t in await load_mcp_tools(session)}

        async def search_company_docs(query: str) -> str:
            """회사 문서(지표, 정책, 회의록, 소개, 인터뷰 등)에서 관련 문단을 찾는다. 결과의 title을 출처로 밝힌다."""
            return await raw["search_company_docs"].ainvoke(
                {"query": query, "session_minute": session_minute, "company": company,
                 "scenario": scenario})

        async def read_company_doc(doc_id: str) -> str:
            """회사 문서 전문을 읽는다. doc_id는 list_company_docs 결과의 doc_id."""
            return await raw["read_company_doc"].ainvoke(
                {"doc_id": doc_id, "session_minute": session_minute, "company": company,
                 "scenario": scenario})

        async def list_company_docs() -> str:
            """지금 읽을 수 있는 회사 문서 목록을 본다."""
            return await raw["list_company_docs"].ainvoke({"session_minute": session_minute, "company": company,
                 "scenario": scenario})

        fns = [search_company_docs, read_company_doc, list_company_docs]
        yield [StructuredTool.from_function(coroutine=fn, name=fn.__name__, description=fn.__doc__)
               for fn in fns]


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


def check_result(result) -> str:
    """MCP 도구 결과를 텍스트로 바꾸고, 도구가 실패했으면 예외로 올린다.

    MCP 오류는 예외가 아니라 "Error executing tool …" 텍스트로 돌아오므로, 코드가 직접 부르는
    변경 도구(보드 반영·관찰 저장·문서 등록)는 이 검사를 거쳐야 실패를 성공으로 착각하지 않는다.

    Raises:
        RuntimeError: 도구 실행이 실패한 경우.
    """
    if isinstance(result, list):
        text = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in result)
    else:
        text = str(result)
    if text.lstrip().startswith("Error executing tool"):
        raise RuntimeError(text.strip())
    return text


async def call_rag(tool_name: str, args: dict) -> str:
    """rag-mcp 도구를 코드에서 직접 호출한다 (예: 승인 후 register_company_doc).

    Raises:
        RuntimeError: 도구 실행이 실패한 경우.
    """
    async with _client().session("rag") as session:
        tools = {t.name: t for t in await load_mcp_tools(session)}
        return check_result(await tools[tool_name].ainvoke(args))


async def read_doc_resource(session_id: str, doc_id: str) -> str:
    """rag-mcp Resource(docs://{session_id}/{doc_id})로 회사 문서 원문을 읽는다(근거 패널용).

    Raises:
        PermissionError: 아직 공유되지 않은 문서.
        LookupError: 없는 세션·문서.
    """
    # 연결 블록 안에서 예외를 올리면 ExceptionGroup으로 감싸지므로, 오류를 담아 두고 블록 밖에서 올린다
    error: McpError | None = None
    async with _client().session("rag") as session:
        try:
            res = await session.read_resource(AnyUrl(f"docs://{session_id}/{doc_id}"))
        except McpError as exc:
            error = exc
    if error is not None:
        if "아직 공유되지" in str(error):
            raise PermissionError(str(error)) from error
        raise LookupError(str(error)) from error
    return "".join(getattr(c, "text", "") for c in res.contents)


async def call_business(tool_name: str, args: dict) -> str:
    """business-mcp 도구를 코드에서 직접 호출한다 (예: 분업 확정 후 assign_tasks).

    Raises:
        RuntimeError: 도구 실행이 실패한 경우.
    """
    async with _client().session("business") as session:
        tools = {t.name: t for t in await load_mcp_tools(session)}
        return check_result(await tools[tool_name].ainvoke(args))
