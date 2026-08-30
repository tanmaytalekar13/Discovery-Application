import pytest

from app.discovery.a2a.client import A2AResolutionResult
from app.discovery.a2a.well_known import AgentCardProbeResult
from app.discovery.a2a_registry.client import A2ARegistryCandidate
from app.models import AgentMetadata, DiscoverySource, ItemType, SourceType
from app.search.a2a_adapter import A2ADiscoveryAdapter


def _registry_candidate(name: str) -> A2ARegistryCandidate:
    return A2ARegistryCandidate(
        agent_name=name,
        description="A research agent",
        version="1.0.0",
        endpoint="https://agents.example.com",
        agent_card_url="https://agents.example.com/.well-known/agent-card.json",
        repository_url=None,
        raw_entry={"name": name},
        source=DiscoverySource(type=SourceType.A2A_CATALOG, id=name, url=None),
    )


class FakeA2ARegistry:
    def __init__(self, candidates=None, error=None):
        self._candidates = candidates or []
        self._error = error

    async def search(self, query, max_results):
        if self._error:
            raise self._error
        return self._candidates


@pytest.mark.asyncio
async def test_disabled_sources_are_not_attempted():
    adapter = A2ADiscoveryAdapter()

    outcomes = await adapter.discover("research agent")

    assert outcomes == []


@pytest.mark.asyncio
async def test_aggregates_across_multiple_registries():
    registry_a = FakeA2ARegistry(candidates=[_registry_candidate("agent-a")])
    registry_b = FakeA2ARegistry(candidates=[_registry_candidate("agent-b")])

    adapter = A2ADiscoveryAdapter(a2a_registries=(registry_a, registry_b))

    outcomes = await adapter.discover("research agent")

    assert {outcome.source for outcome in outcomes} == {
        "a2a_registry[0]",
        "a2a_registry[1]",
    }
    all_candidates = [c for outcome in outcomes for c in outcome.candidates]
    assert len(all_candidates) == 2
    assert all(c.protocol == "a2a" for c in all_candidates)


@pytest.mark.asyncio
async def test_one_registry_failure_does_not_break_the_other():
    ok_registry = FakeA2ARegistry(candidates=[_registry_candidate("agent-a")])
    broken_registry = FakeA2ARegistry(error=RuntimeError("timeout"))

    adapter = A2ADiscoveryAdapter(a2a_registries=(ok_registry, broken_registry))

    outcomes = await adapter.discover("research agent")

    by_source = {outcome.source: outcome for outcome in outcomes}

    assert by_source["a2a_registry[0]"].succeeded is True
    assert by_source["a2a_registry[1]"].succeeded is False
    assert "timeout" in by_source["a2a_registry[1]"].error


@pytest.mark.asyncio
async def test_well_known_only_yields_found_agent_cards(monkeypatch):
    found_resolution = A2AResolutionResult(
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

    async def fake_discover_well_known_agents(hosts, **kwargs):
        return [
            AgentCardProbeResult(
                target=hosts[0],
                found=True,
                resolution=found_resolution,
                error=None,
                source=DiscoverySource(
                    type=SourceType.WELL_KNOWN, id=hosts[0], url=None
                ),
            ),
            AgentCardProbeResult(
                target=hosts[1],
                found=False,
                resolution=None,
                error="404",
                source=DiscoverySource(
                    type=SourceType.WELL_KNOWN, id=hosts[1], url=None
                ),
            ),
        ]

    monkeypatch.setattr(
        "app.search.a2a_adapter.discover_well_known_agents",
        fake_discover_well_known_agents,
    )

    adapter = A2ADiscoveryAdapter(
        well_known_hosts=("https://has-agent.example.com", "https://none.example.com")
    )

    outcomes = await adapter.discover("research agent")

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.source == "well_known"
    assert outcome.succeeded is True
    assert len(outcome.candidates) == 1
    assert outcome.candidates[0].item_type is ItemType.AGENT
