"""
MCP protocol client core (Phase 02).

Wraps the official `mcp` SDK's stdio client transport + ClientSession
into a small class with explicit, independently-failing lifecycle
stages, matching CODEX_EXECUTION_PLAN.md Section 9 (MCP Resolution).

Only stdio transport is implemented here. Remote/HTTP transports are
a Phase 04+ concern.
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
    """Outcome of fully resolving one MCP server candidate."""

    server_id: str
    server_info: MCPServerInfo
    tools: list[ToolMetadata] = field(default_factory=list)


class MCPClient:
    """
    Thin async wrapper around an MCP `ClientSession` connected over
    stdio.

    Each protocol stage is its own method so callers and tests can
    observe and handle failures independently.

    Usage:
        params = StdioServerParameters(
            command="python",
            args=["server.py"],
        )

        async with MCPClient(
            server_id="my-server",
            server_params=params,
        ) as client:
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
        self._connected = False

    async def __aenter__(self) -> "MCPClient":
        await self._connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._disconnect()

    async def _connect(self) -> None:
        """
        Open the stdio transport and construct ClientSession.

        IMPORTANT:
        `_open()` is awaited directly rather than wrapped in
        `asyncio.wait_for()`. The MCP stdio transport owns an AnyIO
        cancel scope whose lifetime must remain in the same task.
        """

        if self._connected:
            return

        self._exit_stack = AsyncExitStack()

        try:
            read_stream, write_stream = (
                await self._exit_stack.enter_async_context(
                    stdio_client(self._server_params)
                )
            )

            session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )

            self._session = session
            self._connected = True

        except Exception as exc:
            stack = self._exit_stack
            self._exit_stack = None
            self._session = None
            self._connected = False

            if stack is not None:
                try:
                    await stack.aclose()
                except Exception:
                    # Preserve the original connection error.
                    # Cleanup failure must not hide the real connection
                    # failure.
                    pass

            raise MCPConnectionError(
                f"Failed to connect to MCP server '{self._server_id}': {exc}"
            ) from exc

    async def _disconnect(self) -> None:
        """
        Close the MCP transport/session exactly once.

        Cleanup is intentionally idempotent so callers can safely invoke
        disconnect after a partial connection failure or from __aexit__.
        """

        stack = self._exit_stack

        self._exit_stack = None
        self._session = None
        self._connected = False

        if stack is None:
            return

        try:
            await stack.aclose()
        except RuntimeError as exc:
            # AnyIO can raise a cancel-scope task-affinity error when a
            # stdio transport is closed outside the task in which its
            # cancel scope was created.
            #
            # Do not mask an otherwise successful MCP operation during
            # context-manager teardown.
            if "cancel scope" not in str(exc).lower():
                raise

    def _require_session(self) -> ClientSession:
        if self._session is None or not self._connected:
            raise MCPConnectionError(
                f"MCP client for '{self._server_id}' is not connected. "
                "Use 'async with MCPClient(...) as client:' before calling "
                "protocol methods."
            )

        return self._session

    async def initialize(
        self,
        timeout_seconds: float = DEFAULT_INITIALIZE_TIMEOUT_SECONDS,
    ) -> MCPServerInfo:
        """
        Perform the MCP `initialize` handshake and return normalized
        server identity/capability metadata.
        """

        session = self._require_session()

        try:
            result: InitializeResult = await asyncio.wait_for(
                session.initialize(),
                timeout=timeout_seconds,
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
        self,
        timeout_seconds: float = DEFAULT_TOOLS_LIST_TIMEOUT_SECONDS,
    ) -> list[ToolMetadata]:
        """
        Call `tools/list` and normalize the result into ToolMetadata
        records.
        """

        session = self._require_session()

        try:
            result = await asyncio.wait_for(
                session.list_tools(),
                timeout=timeout_seconds,
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
        Call `tools/call` for a specific tool.

        Protocol/transport failures raise MCPToolCallError.

        A tool-level application error is returned normally with
        `CallToolResult.isError = True`.
        """

        session = self._require_session()

        try:
            return await asyncio.wait_for(
                session.call_tool(
                    tool_name,
                    arguments or {},
                ),
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
    server_id: str,
    server_params: StdioServerParameters,
) -> MCPResolutionResult:
    """
    Run the full Phase 02 resolution pipeline:

        connect -> initialize -> tools/list -> normalize
    """

    async with MCPClient(
        server_id=server_id,
        server_params=server_params,
    ) as client:
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
    Connect, initialize, verify that the requested tool exists in
    tools/list, and then call it.
    """

    async with MCPClient(
        server_id=server_id,
        server_params=server_params,
    ) as client:
        await client.initialize()
        tools = await client.list_tools()

        if not any(tool.tool_name == tool_name for tool in tools):
            raise MCPToolNotFoundError(
                f"Tool '{tool_name}' was not found on server '{server_id}'."
            )

        return await client.call_tool(tool_name, arguments)