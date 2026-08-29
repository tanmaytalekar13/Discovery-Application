import sys

import pytest
from mcp import StdioServerParameters

from app.discovery.mcp.client import (
    MCPClient,
    call_tool_by_name,
    resolve_mcp_server,
)
from app.discovery.mcp.errors import MCPConnectionError, MCPToolNotFoundError
from app.discovery.mcp.schema import validate_input_schema
from app.discovery.mcp.errors import MCPInvalidSchemaError

SAMPLE_SERVER_PARAMS = StdioServerParameters(
    command=sys.executable,
    args=["-m", "app.discovery.mcp.testing.sample_server"],
)


# ============================================================
# Happy path: real local server, real protocol exchange
# ============================================================


@pytest.mark.asyncio
async def test_initialize_returns_server_info():
    async with MCPClient(
        server_id="sample-server", server_params=SAMPLE_SERVER_PARAMS
    ) as client:
        server_info = await client.initialize()

    assert server_info.name == "sample-mcp-server"
    assert server_info.protocol_version is not None


@pytest.mark.asyncio
async def test_tools_list_returns_normalized_tools():
    async with MCPClient(
        server_id="sample-server", server_params=SAMPLE_SERVER_PARAMS
    ) as client:
        await client.initialize()
        tools = await client.list_tools()

    tool_names = {tool.tool_name for tool in tools}
    assert tool_names == {"echo", "add_numbers"}

    for tool in tools:
        assert tool.server_id == "sample-server"
        assert tool.mcp_schema["type"] == "object"
        assert "properties" in tool.mcp_schema


@pytest.mark.asyncio
async def test_tools_call_echo_succeeds():
    async with MCPClient(
        server_id="sample-server", server_params=SAMPLE_SERVER_PARAMS
    ) as client:
        await client.initialize()
        result = await client.call_tool("echo", {"message": "hello"})

    assert result.isError is not True
    texts = [block.text for block in result.content if block.type == "text"]
    assert any("hello" in text for text in texts)


@pytest.mark.asyncio
async def test_tools_call_add_numbers_succeeds():
    async with MCPClient(
        server_id="sample-server", server_params=SAMPLE_SERVER_PARAMS
    ) as client:
        await client.initialize()
        result = await client.call_tool("add_numbers", {"a": 2, "b": 3})

    assert result.isError is not True
    texts = [block.text for block in result.content if block.type == "text"]
    assert any("5" in text for text in texts)


@pytest.mark.asyncio
async def test_resolve_mcp_server_end_to_end():
    resolution = await resolve_mcp_server(
        server_id="sample-server", server_params=SAMPLE_SERVER_PARAMS
    )

    assert resolution.server_info.name == "sample-mcp-server"
    assert {t.tool_name for t in resolution.tools} == {"echo", "add_numbers"}


@pytest.mark.asyncio
async def test_call_tool_by_name_end_to_end():
    result = await call_tool_by_name(
        server_id="sample-server",
        server_params=SAMPLE_SERVER_PARAMS,
        tool_name="echo",
        arguments={"message": "ping"},
    )

    texts = [block.text for block in result.content if block.type == "text"]
    assert any("ping" in text for text in texts)


# ============================================================
# Failure isolation: connection
# ============================================================


@pytest.mark.asyncio
async def test_connect_failure_raises_connection_error():
    bad_params = StdioServerParameters(
        command="this-binary-does-not-exist-12345",
        args=[],
    )

    with pytest.raises(MCPConnectionError):
        async with MCPClient(server_id="broken-server", server_params=bad_params):
            pass


@pytest.mark.asyncio
async def test_calling_before_connect_raises_connection_error():
    client = MCPClient(server_id="sample-server", server_params=SAMPLE_SERVER_PARAMS)

    with pytest.raises(MCPConnectionError):
        await client.initialize()


# ============================================================
# Failure isolation: tool call / not found
# ============================================================


@pytest.mark.asyncio
async def test_call_unknown_tool_raises_tool_not_found():
    with pytest.raises(MCPToolNotFoundError):
        await call_tool_by_name(
            server_id="sample-server",
            server_params=SAMPLE_SERVER_PARAMS,
            tool_name="does_not_exist",
        )


@pytest.mark.asyncio
async def test_call_tool_with_invalid_arguments_reports_tool_error():
    async with MCPClient(
        server_id="sample-server", server_params=SAMPLE_SERVER_PARAMS
    ) as client:
        await client.initialize()
        # Missing the required "message" argument entirely: the server
        # should report this as a normal tool-level error rather than
        # the client raising a transport-level exception.
        result = await client.call_tool("echo", {})

    assert result.isError is True


# ============================================================
# Schema validation
# ============================================================


def test_validate_input_schema_accepts_object_schema():
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    assert validate_input_schema("some_tool", schema) == schema


def test_validate_input_schema_rejects_missing_schema():
    with pytest.raises(MCPInvalidSchemaError):
        validate_input_schema("some_tool", None)


def test_validate_input_schema_rejects_non_object_schema():
    with pytest.raises(MCPInvalidSchemaError):
        validate_input_schema("some_tool", "not-a-schema")


def test_validate_input_schema_rejects_wrong_type():
    with pytest.raises(MCPInvalidSchemaError):
        validate_input_schema("some_tool", {"type": "array"})