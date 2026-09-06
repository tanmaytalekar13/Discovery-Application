"""Tests for awesome-list MCP server discovery adapter."""
from __future__ import annotations

import pytest

from app.discovery.awesome_list.client import AwesomeListAdapter


class TestAwesomeListAdapterParse:
    """Unit tests for awesome-list Markdown parsing."""

    @pytest.fixture
    def adapter(self):
        return AwesomeListAdapter()

    def test_parses_basic_list_entry(self, adapter):
        content = """# Awesome MCP Servers

## Filesystem

- [MCP Filesystem Server](https://github.com/example/mcp-filesystem "A filesystem MCP server")
  Some description here.

## Database

- [PostgreSQL MCP](https://github.com/example/mcp-postgres)
"""
        candidates = adapter._parse(content, "test-source", "https://example.com/README.md")
        assert len(candidates) == 2
        assert candidates[0].name == "MCP Filesystem Server"
        assert "github.com" in candidates[0].html_url
        assert candidates[0].category == "Filesystem"
        assert candidates[1].name == "PostgreSQL MCP"
        assert candidates[1].category == "Database"

    def test_extracts_install_command(self, adapter):
        content = """## Tools

- [My Tool](https://github.com/example/mcp-tool) A useful tool.

  `npx -y @example/mcp-tool`
"""
        candidates = adapter._parse(content, "test-source", "https://example.com/README.md")
        assert len(candidates) == 1
        assert "npx" in candidates[0].install_command

    def test_handles_category_header(self, adapter):
        content = """## Cloud Platforms

- [AWS MCP](https://github.com/example/aws-mcp)

## AI & ML

- [OpenAI MCP](https://github.com/example/openai-mcp)
"""
        candidates = adapter._parse(content, "test-source", "https://example.com/README.md")
        assert len(candidates) == 2
        assert candidates[0].category == "Cloud Platforms"
        assert candidates[1].category == "AI & ML"

    def test_skips_non_github_links(self, adapter):
        content = """## Tools

- [Random site](https://example.com/tool) Not a GitHub link.
- [Real MCP](https://github.com/example/mcp-tool) Real MCP server.
"""
        candidates = adapter._parse(content, "test-source", "https://example.com/README.md")
        assert len(candidates) == 1
        assert candidates[0].name == "Real MCP"

    def test_empty_content_returns_empty_list(self, adapter):
        candidates = adapter._parse("", "test-source", "https://example.com/README.md")
        assert candidates == []

    def test_source_name_extracted_correctly(self, adapter):
        assert "punkpeye" in adapter._extract_source_name(
            "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md"
        )
        assert "wong2" in adapter._extract_source_name(
            "https://raw.githubusercontent.com/wong2/awesome-mcp-servers/main/README.md"
        )
