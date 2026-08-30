import socket
import subprocess
import sys
import time

import httpx
import pytest

from app.discovery.a2a.well_known import (
    discover_well_known_agents,
    probe_well_known_agent_card,
    resolve_configured_agent_card,
)
from app.models import SourceType


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_ready(base_url: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None

    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/.well-known/agent-card.json", timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_exc = exc
        time.sleep(0.1)

    raise RuntimeError(f"sample A2A agent did not start in time: {last_exc}")


@pytest.fixture(scope="module")
def sample_agent_base_url():
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "app.discovery.a2a.testing.sample_agent",
            "--port",
            str(port),
        ],
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        _wait_until_ready(base_url)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


# ============================================================
# Well-Known resolution against a real local agent
# ============================================================


@pytest.mark.asyncio
async def test_probe_well_known_finds_real_agent(sample_agent_base_url):
    result = await probe_well_known_agent_card(sample_agent_base_url)

    assert result.found is True
    assert result.error is None
    assert result.source.type is SourceType.WELL_KNOWN
    assert result.resolution is not None
    assert result.resolution.endpoint == f"{sample_agent_base_url}/a2a"
    assert set(result.resolution.agent.skills) == {"echo", "reverse_text"}


@pytest.mark.asyncio
async def test_probe_well_known_missing_host_is_isolated_failure():
    result = await probe_well_known_agent_card("http://127.0.0.1:1")

    assert result.found is False
    assert result.resolution is None
    assert result.error is not None
    assert result.source.type is SourceType.WELL_KNOWN


@pytest.mark.asyncio
async def test_discover_well_known_agents_isolates_failures(sample_agent_base_url):
    results = await discover_well_known_agents(
        [sample_agent_base_url, "http://127.0.0.1:1"]
    )

    by_target = {result.target: result for result in results}

    assert by_target[sample_agent_base_url].found is True
    assert by_target["http://127.0.0.1:1"].found is False


@pytest.mark.asyncio
async def test_discover_well_known_agents_handles_empty_list():
    assert await discover_well_known_agents([]) == []


# ============================================================
# Direct configured Agent Card URL resolution
# ============================================================


@pytest.mark.asyncio
async def test_resolve_configured_agent_card_direct_url(sample_agent_base_url):
    card_url = f"{sample_agent_base_url}/.well-known/agent-card.json"

    result = await resolve_configured_agent_card(card_url)

    assert result.found is True
    assert result.source.type is SourceType.CONFIGURED
    assert result.resolution is not None
    assert result.resolution.endpoint == f"{sample_agent_base_url}/a2a"


@pytest.mark.asyncio
async def test_resolve_configured_agent_card_invalid_url_is_isolated_failure():
    result = await resolve_configured_agent_card("http://127.0.0.1:1/card.json")

    assert result.found is False
    assert result.resolution is None
    assert result.error is not None
    assert result.source.type is SourceType.CONFIGURED
