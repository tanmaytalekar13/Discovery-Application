"""
Real, runnable local MCP server used as a test fixture for Phase 02.

Per CODEX_EXECUTION_PLAN.md, Phase 02's Definition of Done requires
"real local MCP server works" - this file is that server. It is a
genuine MCP server (built on the official SDK's FastMCP helper) that
speaks the real protocol over stdio; it is not a mock of MCP
responses. Tests spawn this file as a subprocess and talk to it
through `app.discovery.mcp.client.MCPClient`, exercising the actual
initialize / tools/list / tools/call exchange.

Run directly for manual testing:
    python -m app.discovery.mcp.testing.sample_server
"""

from mcp.server.fastmcp import FastMCP

server = FastMCP(name="sample-mcp-server")


@server.tool()
def echo(message: str) -> str:
    """Echo back the given message."""
    return message


@server.tool()
def add_numbers(a: float, b: float) -> float:
    """Add two numbers together and return the sum."""
    return a + b


if __name__ == "__main__":
    server.run(transport="stdio")