from app.discovery.common.candidate import (
    from_github_candidate,
    from_mcp_registry_candidate,
)
from app.discovery.github.client import GitHubCandidate
from app.discovery.mcp_registry.client import MCPRegistryCandidate
from app.models import DiscoverySource, ItemType, SourceType


def test_from_github_candidate_tool():
    candidate = GitHubCandidate(
        repository="example/mcp-server",
        name="mcp-server",
        html_url="https://github.com/example/mcp-server",
        clone_url="https://github.com/example/mcp-server.git",
        default_branch="main",
        description="A Model Context Protocol server",
        item_type=ItemType.TOOL,
        evidence=("uses tools/call",),
        source=DiscoverySource(
            type=SourceType.GITHUB,
            id="example/mcp-server",
            url="https://github.com/example/mcp-server",
        ),
    )

    result = from_github_candidate(candidate)

    assert result.protocol == "mcp"
    assert result.item_type is ItemType.TOOL
    assert result.source_type is SourceType.GITHUB
    assert result.source_provider == "GitHub"
    assert result.title == "mcp-server"
    assert str(result.repository_url) == "https://github.com/example/mcp-server"
    assert result.evidence == ("uses tools/call",)
    assert result.raw_metadata["source_candidate"] is candidate


def test_from_github_candidate_agent():
    candidate = GitHubCandidate(
        repository="example/research-agent",
        name="research-agent",
        html_url="https://github.com/example/research-agent",
        clone_url="https://github.com/example/research-agent.git",
        default_branch="main",
        description="An A2A research agent",
        item_type=ItemType.AGENT,
        evidence=("has an Agent Card",),
        source=DiscoverySource(
            type=SourceType.GITHUB,
            id="example/research-agent",
            url="https://github.com/example/research-agent",
        ),
    )

    result = from_github_candidate(candidate)

    assert result.protocol == "a2a"
    assert result.item_type is ItemType.AGENT


def test_from_mcp_registry_candidate():
    candidate = MCPRegistryCandidate(
        server_name="io.example/scraper",
        title="Web Scraper",
        description="Scrapes web pages",
        version="1.0.0",
        repository_url="https://github.com/example/scraper",
        packages=(),
        remotes=(),
        raw_server={"name": "io.example/scraper"},
        source=DiscoverySource(
            type=SourceType.MCP_REGISTRY,
            id="io.example/scraper",
            url="https://github.com/example/scraper",
        ),
    )

    result = from_mcp_registry_candidate(candidate)

    assert result.protocol == "mcp"
    assert result.item_type is ItemType.TOOL
    assert result.source_type is SourceType.MCP_REGISTRY
    assert result.title == "Web Scraper"
    assert "1.0.0" in result.evidence[0]
    assert result.raw_metadata["source_candidate"] is candidate
