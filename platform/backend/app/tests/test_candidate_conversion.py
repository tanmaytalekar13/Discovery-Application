from app.discovery.a2a.well_known import AgentCardProbeResult
from app.discovery.a2a.client import A2AResolutionResult
from app.discovery.a2a_registry.client import A2ARegistryCandidate
from app.discovery.common.candidate import (
    from_a2a_registry_candidate,
    from_configured_endpoint,
    from_github_candidate,
    from_mcp_registry_candidate,
    from_web_extraction_candidate,
    from_web_search_candidate,
    from_well_known_probe,
)
from app.discovery.github.client import GitHubCandidate
from app.discovery.mcp_registry.client import MCPRegistryCandidate
from app.discovery.web_extraction.client import WebExtractionCandidate
from app.discovery.web_search.client import WebSearchCandidate
from app.models import AgentMetadata, DiscoverySource, ItemType, SourceType


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


def test_from_a2a_registry_candidate():
    candidate = A2ARegistryCandidate(
        agent_name="research-agent",
        description="Researches topics",
        version="2.0.0",
        endpoint="https://agents.example.com",
        agent_card_url="https://agents.example.com/.well-known/agent-card.json",
        repository_url=None,
        raw_entry={"name": "research-agent"},
        source=DiscoverySource(
            type=SourceType.A2A_CATALOG,
            id="research-agent",
            url="https://agents.example.com/.well-known/agent-card.json",
        ),
    )

    result = from_a2a_registry_candidate(candidate)

    assert result.protocol == "a2a"
    assert result.item_type is ItemType.AGENT
    assert result.source_type is SourceType.A2A_CATALOG
    assert result.title == "research-agent"


def test_from_web_search_candidate():
    candidate = WebSearchCandidate(
        title="Example MCP server",
        url="https://example.com/mcp",
        snippet="A Model Context Protocol server implementation",
        item_type=ItemType.TOOL,
        evidence=("explicitly identifies an MCP server",),
        source=DiscoverySource(
            type=SourceType.WEB_SEARCH,
            id="https://example.com/mcp",
            url="https://example.com/mcp",
        ),
        raw_result={},
    )

    result = from_web_search_candidate(candidate)

    assert result.protocol == "mcp"
    assert result.source_provider == "Web Search"
    assert result.title == "Example MCP server"


def test_from_web_extraction_candidate():
    candidate = WebExtractionCandidate(
        title="Agent Card example",
        url="https://example.com/agent",
        text_excerpt="This page describes an A2A agent with an Agent Card.",
        item_type=ItemType.AGENT,
        evidence=("page text mentions A2A/Agent Card",),
        source=DiscoverySource(
            type=SourceType.WEB_PAGE,
            id="https://example.com/agent",
            url="https://example.com/agent",
        ),
        content_type="text/html",
    )

    result = from_web_extraction_candidate(candidate)

    assert result.protocol == "a2a"
    assert result.source_provider == "Web Extraction"
    assert result.item_type is ItemType.AGENT


def test_from_well_known_probe_found():
    resolution = A2AResolutionResult(
        endpoint="https://agents.example.com",
        protocol_version="1.0",
        raw_agent_card={"name": "research-agent"},
        agent=AgentMetadata(
            endpoint="https://agents.example.com",
            agent_card={"name": "research-agent"},
            skills=["research"],
            capabilities=[],
            declared_dependencies=[],
        ),
    )
    probe = AgentCardProbeResult(
        target="https://agents.example.com",
        found=True,
        resolution=resolution,
        error=None,
        source=DiscoverySource(
            type=SourceType.WELL_KNOWN,
            id="https://agents.example.com",
            url="https://agents.example.com/.well-known/agent-card.json",
        ),
    )

    result = from_well_known_probe(probe)

    assert result is not None
    assert result.protocol == "a2a"
    assert result.item_type is ItemType.AGENT
    assert result.source_provider == "A2A Well-Known"
    assert "research" in result.description


def test_from_well_known_probe_not_found_returns_none():
    probe = AgentCardProbeResult(
        target="https://no-agent.example.com",
        found=False,
        resolution=None,
        error="404",
        source=DiscoverySource(
            type=SourceType.WELL_KNOWN,
            id="https://no-agent.example.com",
            url="https://no-agent.example.com/.well-known/agent-card.json",
        ),
    )

    assert from_well_known_probe(probe) is None


def test_from_configured_endpoint_mcp():
    result = from_configured_endpoint(
        url="https://mcp.example.com",
        protocol="mcp",
        title="Example MCP endpoint",
    )

    assert result.protocol == "mcp"
    assert result.item_type is ItemType.TOOL
    assert result.source_type is SourceType.CONFIGURED
    assert result.title == "Example MCP endpoint"


def test_from_configured_endpoint_a2a():
    result = from_configured_endpoint(
        url="https://agent.example.com",
        protocol="a2a",
    )

    assert result.protocol == "a2a"
    assert result.item_type is ItemType.AGENT
    assert result.title == "https://agent.example.com"
