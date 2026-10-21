"""직접 만든 MCP 서버 2개(business, rag)의 공통 실행부."""
import sys

from mcp.server.fastmcp import FastMCP

from app.settings import config


def serve(mcp: FastMCP, name: str) -> None:
    """MCP 서버를 실행한다. `--http`면 config [mcp]의 포트로 Streamable HTTP 서버, 아니면 stdio.

    전송 방식을 config가 아니라 실행 인자로 고르는 이유: stdio 클라이언트(테스트)가 이 모듈을
    자식 프로세스로 띄울 때도 config 파일을 읽으므로, config로 고르면 자식이 HTTP로 떠 버린다.

    Args:
        mcp: 실행할 FastMCP 서버.
        name: config [mcp]의 포트 키 이름(business | rag).
    """
    if "--http" in sys.argv:
        cfg = config()["mcp"]
        mcp.settings.host = cfg["host"]
        mcp.settings.port = cfg[f"{name}_port"]
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
