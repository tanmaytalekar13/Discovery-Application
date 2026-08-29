import socket
import subprocess
import sys
import time

import httpx
import pytest

from app.discovery.a2a.client import A2AClient, resolve_a2a_agent, send_task
from app.discovery.a2a.errors import (
    A2AConnectionError,
    A2ATaskError,
    AgentCardFetchError,
    AgentCardValidationError,
)
from app.discovery.a2a.schema import validate_agent_card


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
# Happy path: real local agent, real protocol exchange
# ============================================================


@pytest.mark.asyncio
async def test_fetch_agent_card_returns_valid_card(sample_agent_base_url):
    async with A2AClient(base_url=sample_agent_base_url) as client:
        card = await client.fetch_agent_card()

    assert card["name"] == "sample-a2a-agent"
    assert card["protocolVersion"] == "0.3.0"
    assert card["url"] == f"{sample_agent_base_url}/a2a"
    assert {skill["id"] for skill in card["skills"]} == {"echo", "reverse_text"}


@pytest.mark.asyncio
async def test_resolve_a2a_agent_end_to_end(sample_agent_base_url):
    resolution = await resolve_a2a_agent(base_url=sample_agent_base_url)

    assert resolution.protocol_version == "0.3.0"
    assert resolution.endpoint == f"{sample_agent_base_url}/a2a"
    assert set(resolution.agent.skills) == {"echo", "reverse_text"}
    assert resolution.agent.capabilities == []
    assert str(resolution.agent.endpoint).rstrip("/") == resolution.endpoint


@pytest.mark.asyncio
async def test_send_message_echo_succeeds(sample_agent_base_url):
    async with A2AClient(base_url=sample_agent_base_url) as client:
        card = await client.fetch_agent_card()
        result = await client.send_message(card["url"], "hello there")

    assert result["kind"] == "message"
    assert result["role"] == "agent"
    texts = [part["text"] for part in result["parts"] if part["kind"] == "text"]
    assert any("Echo: hello there" in text for text in texts)


@pytest.mark.asyncio
async def test_send_message_reverse_succeeds(sample_agent_base_url):
    async with A2AClient(base_url=sample_agent_base_url) as client:
        card = await client.fetch_agent_card()
        result = await client.send_message(card["url"], "reverse:hello")

    texts = [part["text"] for part in result["parts"] if part["kind"] == "text"]
    assert any("olleh" in text for text in texts)


@pytest.mark.asyncio
async def test_send_task_end_to_end(sample_agent_base_url):
    result = await send_task(base_url=sample_agent_base_url, text="ping")

    texts = [part["text"] for part in result["parts"] if part["kind"] == "text"]
    assert any("Echo: ping" in text for text in texts)


# ============================================================
# Failure isolation: connection / fetch / task
# ============================================================


@pytest.mark.asyncio
async def test_fetch_agent_card_connection_failure_raises_connection_error():
    async with A2AClient(base_url="http://127.0.0.1:1") as client:
        with pytest.raises(A2AConnectionError):
            await client.fetch_agent_card()


@pytest.mark.asyncio
async def test_fetch_agent_card_missing_path_raises_fetch_error(
    sample_agent_base_url,
):
    async with A2AClient(base_url=sample_agent_base_url) as client:
        with pytest.raises(AgentCardFetchError):
            await client.fetch_agent_card(card_path="/does-not-exist.json")


@pytest.mark.asyncio
async def test_send_message_with_empty_text_raises_task_error(
    sample_agent_base_url,
):
    async with A2AClient(base_url=sample_agent_base_url) as client:
        card = await client.fetch_agent_card()
        with pytest.raises(A2ATaskError):
            await client.send_message(card["url"], "")


@pytest.mark.asyncio
async def test_send_message_connection_failure_raises_connection_error():
    async with A2AClient(base_url="http://127.0.0.1:1") as client:
        with pytest.raises(A2AConnectionError):
            await client.send_message("http://127.0.0.1:1/a2a", "hello")


# ============================================================
# Schema validation
# ============================================================


def _valid_card() -> dict:
    return {
        "protocolVersion": "0.3.0",
        "name": "test-agent",
        "description": "A test agent.",
        "url": "http://localhost:9999/a2a",
        "version": "1.0.0",
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{"id": "echo", "name": "Echo", "description": "Echoes."}],
    }


def test_validate_agent_card_accepts_well_formed_card():
    card = _valid_card()
    assert validate_agent_card(card) == card


def test_validate_agent_card_rejects_non_object():
    with pytest.raises(AgentCardValidationError):
        validate_agent_card("not-a-card")


def test_validate_agent_card_rejects_missing_required_field():
    card = _valid_card()
    del card["skills"]
    with pytest.raises(AgentCardValidationError):
        validate_agent_card(card)


def test_validate_agent_card_rejects_skill_without_id():
    card = _valid_card()
    card["skills"] = [{"name": "Echo", "description": "Echoes."}]
    with pytest.raises(AgentCardValidationError):
        validate_agent_card(card)


def test_validate_agent_card_rejects_non_dict_capabilities():
    card = _valid_card()
    card["capabilities"] = ["streaming"]
    with pytest.raises(AgentCardValidationError):
        validate_agent_card(card)
