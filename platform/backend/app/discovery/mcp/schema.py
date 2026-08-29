"""
Normalization of raw MCP `tools/list` entries into the platform's
normalized catalog model (`app.models.ToolMetadata`).

Per CODEX_EXECUTION_PLAN.md Section 9:
    "Do not create trusted tool schemas from README text alone."

This module only ever normalizes schemas that came back from a real
`tools/list` response on an initialized MCP session - never from
scraped documentation, and never with fabricated defaults.
"""

from __future__ import annotations

from typing import Any

from mcp.types import Tool as MCPTool

from app.discovery.mcp.errors import MCPInvalidSchemaError
from app.models import ToolMetadata


def validate_input_schema(tool_name: str, input_schema: Any) -> dict[str, Any]:
    """
    Validate that a tool's inputSchema is a well-formed JSON Schema
    object shape. This is intentionally a structural check, not a
    full JSON-Schema-meta-schema validation - the goal is to reject
    obviously broken/missing schemas, not to second-guess a
    spec-compliant server.
    """
    if input_schema is None:
        raise MCPInvalidSchemaError(
            f"Tool '{tool_name}' did not provide an inputSchema."
        )

    if not isinstance(input_schema, dict):
        raise MCPInvalidSchemaError(
            f"Tool '{tool_name}' inputSchema must be a JSON object, "
            f"got {type(input_schema).__name__}."
        )

    schema_type = input_schema.get("type")
    if schema_type is not None and schema_type != "object":
        raise MCPInvalidSchemaError(
            f"Tool '{tool_name}' inputSchema.type must be 'object', "
            f"got {schema_type!r}."
        )

    return input_schema


def normalize_tool(server_id: str, tool: MCPTool) -> ToolMetadata:
    """
    Convert a single `mcp.types.Tool` (as returned by a real,
    initialized `tools/list` call) into the platform's normalized
    `ToolMetadata` record.

    Raises:
        MCPInvalidSchemaError: if the tool's inputSchema is missing
            or structurally invalid.
    """
    validated_schema = validate_input_schema(tool.name, tool.inputSchema)

    return ToolMetadata(
        server_id=server_id,
        tool_name=tool.name,
        mcp_schema=validated_schema,
    )


def normalize_tools(server_id: str, tools: list[MCPTool]) -> list[ToolMetadata]:
    """
    Normalize every tool from a `tools/list` response.

    A single tool with an invalid schema does not abort the whole
    batch - it is skipped, matching rule #21 in the plan ("One
    discovery-source failure must not fail the complete search"),
    applied here at tool granularity. Callers that need strict
    all-or-nothing behavior can call `normalize_tool` directly.
    """
    normalized: list[ToolMetadata] = []

    for tool in tools:
        try:
            normalized.append(normalize_tool(server_id, tool))
        except MCPInvalidSchemaError:
            continue

    return normalized