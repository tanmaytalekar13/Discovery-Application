"""
MCP protocol client core (Phase 02).

Wraps the official `mcp` SDK's stdio client transport + ClientSession
into a small class with explicit, independently-failing lifecycle
stages, matching CODEX_EXECUTION_PLAN.md Section 9 (MCP Resolution):

    candidate
       |
    identify server
       |
    resolve endpoint/startup configuration      <- Phase 04+ (discovery)
       |
    security validation                         <- Phase 24 (sandbox/security)
       |
    MCP client connection                       <- this module
       |
    protocol initialization                     <- this module
       |
    capability/server metadata                  <- this module
       |
    tools/list                                  <- this module
       |
    schema validation                           <- app.discovery.mcp.schema
       |
    normalized Tool records                     <- app.discovery.mcp.schema

Only stdio transport is implemented here (used to talk to a locally
spawned MCP server process, as required by Phase 02's Definition of
Done: "real local MCP server works"). Remote/HTTP transports for
discovered internet servers are a Phase 04+ concern and are not
needed to satisfy this phase.

The backend never imports or executes arbitrary discovered source
code directly - this client only ever speaks the MCP wire protocol
to a separate process/endpoint.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, InitializeResult

from app.discovery.mcp.errors import (
    MCPConnectionError,
    MCPInitializeError,
    MCPToolCallError,
    MCPToolNotFoundError,
    MCPToolsListError,
)
from app.discovery.mcp.schema import normalize_tools
from app.models import ToolMetadata

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_INITIALIZE_TIMEOUT_SECONDS = 10.0
DEFAULT_TOOLS_LIST_TIMEOUT_SECONDS = 15.0
DEFAULT_TOOL_CALL_TIMEOUT_SECONDS = 30.0


@dataclass
class MCPServerInfo:
    """Normalized server-identity metadata captured at initialize time."""

    name: str
    version: str | None = None
    protocol_version: str | None = None
    instructions: str | None = None


@dataclass
class MCPResolutionResult:
    """
    Outcome of fully resolving one MCP server candidate: connect,
    initialize, tools/list, and schema normalization, in one call.
    """

    server_id: str
    server_info: MCPServerInfo
    tools: list[ToolMetadata] = field(default_factory=list)


class MCPClient:
    """
    Thin async wrapper around an MCP `ClientSession` connected over
    stdio. Each protocol stage is its own method so that callers
    (and tests) can observe/handle failures stage-by-stage rather
    than getting one opaque exception for the whole pipeline.

    Usage:
        params = StdioServerParameters(command="python", args=["server.py"])
        async with MCPClient(server_id="my-server", server_params=params) as client:
            server_info = await client.initialize()
            tools = await client.list_tools()
            result = await client.call_tool("echo", {"message": "hi"})
    """

    def __init__(
        self,
        server_id: str,
        server_params: StdioServerParameters,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        self._server_id = server_id
        self._server_params = server_params
        self._connect_timeout_seconds = connect_timeout_seconds

        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "MCPClient":
        await self._connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._disconnect()

    async def _connect(self) -> None:
        """
        Open the stdio transport (spawns the server process) and
        construct the ClientSession. Deliberately does NOT call
        `initialize` - that is a separate, independently-testable
        stage (see `initialize()`).
        """
        self._exit_stack = AsyncExitStack()

        async def _open() -> ClientSession:
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                stdio_client(self._server_params)
            )
            return await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )

        try:
            # Note: a plain asyncio timeout (not anyio.fail_after) is used
            # here deliberately. stdio_client/ClientSession are meant to
            # stay open past this method via the AsyncExitStack, and an
            # anyio cancel scope must be exited in the same call frame it
            # was opened in - wrapping resource acquisition that outlives
            # this method in anyio.fail_after raises "cancel scope exited
            # in the wrong order" once the connection actually succeeds.
            session = await asyncio.wait_for(
                _open(), timeout=self._connect_timeout_seconds
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            await self._exit_stack.aclose()
            self._exit_stack = None
            raise MCPConnectionError(
                f"Timed out connecting to MCP server '{self._server_id}' "
                f"after {self._connect_timeout_seconds}s."
            ) from exc
        except Exception as exc:
            if self._exit_stack is not None:
                await self._exit_stack.aclose()
                self._exit_stack = None
            raise MCPConnectionError(
                f"Failed to connect to MCP server '{self._server_id}': {exc}"
            ) from exc

        self._session = session

    async def _disconnect(self) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
        self._exit_stack = None
        self._session = None

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise MCPConnectionError(
                f"MCP client for '{self._server_id}' is not connected. "
                "Use 'async with MCPClient(...) as client:' before calling "
                "protocol methods."
            )
        return self._session

    async def initialize(
        self, timeout_seconds: float = DEFAULT_INITIALIZE_TIMEOUT_SECONDS
    ) -> MCPServerInfo:
        """
        Perform the MCP `initialize` handshake and return normalized
        server identity/capability metadata.
        """
        session = self._require_session()

        try:
            result: InitializeResult = await asyncio.wait_for(
                session.initialize(), timeout=timeout_seconds
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise MCPInitializeError(
                f"MCP 'initialize' timed out for server '{self._server_id}' "
                f"after {timeout_seconds}s."
            ) from exc
        except Exception as exc:
            raise MCPInitializeError(
                f"MCP 'initialize' failed for server '{self._server_id}': {exc}"
            ) from exc

        server_info = result.serverInfo
        if server_info is None or not getattr(server_info, "name", None):
            raise MCPInitializeError(
                f"MCP server '{self._server_id}' returned an initialize "
                "response with no serverInfo.name."
            )

        return MCPServerInfo(
            name=server_info.name,
            version=getattr(server_info, "version", None),
            protocol_version=result.protocolVersion,
            instructions=result.instructions,
        )

    async def list_tools(
        self, timeout_seconds: float = DEFAULT_TOOLS_LIST_TIMEOUT_SECONDS
    ) -> list[ToolMetadata]:
        """
        Call `tools/list` and normalize the result into ToolMetadata
        records. A tool with an invalid schema is skipped rather than
        failing the whole call (see `normalize_tools`).
        """
        session = self._require_session()

        try:
            result = await asyncio.wait_for(
                session.list_tools(), timeout=timeout_seconds
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise MCPToolsListError(
                f"MCP 'tools/list' timed out for server '{self._server_id}' "
                f"after {timeout_seconds}s."
            ) from exc
        except Exception as exc:
            raise MCPToolsListError(
                f"MCP 'tools/list' failed for server '{self._server_id}': {exc}"
            ) from exc

        return normalize_tools(self._server_id, result.tools)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict | None = None,
        timeout_seconds: float = DEFAULT_TOOL_CALL_TIMEOUT_SECONDS,
    ) -> CallToolResult:
        """
        Call `tools/call` for a specific tool. Raises MCPToolCallError
        only for protocol/transport-level failures; a tool that runs
        but reports an application error is returned normally with
        `CallToolResult.isError = True` so the caller (e.g. the Phase
        15 sandbox test runner) can surface it as tool output rather
        than a platform failure.
        """
        session = self._require_session()

        try:
            return await asyncio.wait_for(
                session.call_tool(tool_name, arguments or {}),
                timeout=timeout_seconds,
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise MCPToolCallError(
                f"MCP 'tools/call' for '{tool_name}' timed out on server "
                f"'{self._server_id}' after {timeout_seconds}s."
            ) from exc
        except Exception as exc:
            raise MCPToolCallError(
                f"MCP 'tools/call' for '{tool_name}' failed on server "
                f"'{self._server_id}': {exc}"
            ) from exc


async def resolve_mcp_server(
    server_id: str, server_params: StdioServerParameters
) -> MCPResolutionResult:
    """
    Convenience helper that runs the full Phase 02 resolution pipeline
    for one candidate server in a single call: connect -> initialize
    -> tools/list -> normalize.

    Each stage still raises its own distinct exception type on
    failure (MCPConnectionError / MCPInitializeError /
    MCPToolsListError), so callers can tell exactly which stage
    failed - required for the discovery pipeline's per-source failure
    isolation (rule #21) once this is wired into Phase 09.
    """
    async with MCPClient(server_id=server_id, server_params=server_params) as client:
        server_info = await client.initialize()
        tools = await client.list_tools()

        for tool in tools:
            tool.server_id = server_id

        return MCPResolutionResult(
            server_id=server_id,
            server_info=server_info,
            tools=tools,
        )


async def call_tool_by_name(
    server_id: str,
    server_params: StdioServerParameters,
    tool_name: str,
    arguments: dict | None = None,
) -> CallToolResult:
    """
    Convenience helper for the "test a single tool" path (Phase 15):
    connect, initialize, verify the tool exists in tools/list, then
    call it. Raises MCPToolNotFoundError if the tool is not present,
    matching Section 25's "Verify expected tool" step.
    """
    async with MCPClient(server_id=server_id, server_params=server_params) as client:
        await client.initialize()
        tools = await client.list_tools()

        if not any(tool.tool_name == tool_name for tool in tools):
            raise MCPToolNotFoundError(
                f"Tool '{tool_name}' was not found on server '{server_id}'."
            )

        return await client.call_tool(tool_name, arguments)