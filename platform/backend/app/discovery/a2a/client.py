"""
A2A protocol client core (Phase 03).

Implements a small, spec-literal A2A client over the real wire
protocol - JSON-RPC 2.0 over HTTP(S) (A2A specification Section 3.2.1
and Section 7.1) - matching CODEX_EXECUTION_PLAN.md Section 10 (A2A
Resolution):

    candidate
       -> identify agent endpoint
       -> resolve Agent Card (direct URL / .well-known)
       -> validate Agent Card
       -> normalize identity/capabilities/skills
       -> normalized Agent

and Section 4.5 acceptance criteria ("Test A2A Agent" -> "A2A
request/task" -> "agent response").

Only the JSON-RPC 2.0 transport and the `message/send` RPC method are
implemented here. Streaming (`message/stream`), push notifications,
and gRPC/REST transports are out of scope for this phase.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.discovery.a2a.errors import (
    A2AConnectionError,
    A2ATaskError,
    AgentCardFetchError,
    AgentCardValidationError,
)
from app.discovery.a2a.schema import normalize_agent_card, validate_agent_card
from app.models import AgentMetadata

DEFAULT_AGENT_CARD_PATH = "/.well-known/agent-card.json"
DEFAULT_FETCH_TIMEOUT_SECONDS = 10.0
DEFAULT_TASK_TIMEOUT_SECONDS = 30.0


@dataclass
class A2AResolutionResult:
    """Outcome of fully resolving one A2A agent candidate."""

    endpoint: str
    protocol_version: str | None
    raw_agent_card: dict[str, Any]
    agent: AgentMetadata


class A2AClient:
    """
    Thin async wrapper for talking to a candidate A2A server over
    JSON-RPC 2.0/HTTP(S).

    Each protocol stage is its own method so callers and tests can
    observe and handle failures independently, mirroring
    `app.discovery.mcp.client.MCPClient`.

    Usage:
        async with A2AClient(base_url="http://localhost:9999") as client:
            card = await client.fetch_agent_card()
            result = await client.send_message(card["url"], "hello")
    """

    def __init__(
        self,
        base_url: str,
        httpx_client: httpx.AsyncClient | None = None,
        fetch_timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
        task_timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = httpx_client is None
        self._client = httpx_client or httpx.AsyncClient()
        self._fetch_timeout_seconds = fetch_timeout_seconds
        self._task_timeout_seconds = task_timeout_seconds

    async def __aenter__(self) -> "A2AClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_agent_card(
        self,
        card_path: str = DEFAULT_AGENT_CARD_PATH,
    ) -> dict[str, Any]:
        """
        Fetch and validate the real Agent Card served by a candidate
        A2A server.

        `card_path` defaults to the well-known location defined in
        A2A specification Section 5.3
        (`/.well-known/agent-card.json`). Pass an empty string to
        treat `base_url` itself as the full Agent Card URL (direct
        Agent Card URL resolution, per Section 10).
        """
        url = f"{self._base_url}{card_path}"

        try:
            response = await self._client.get(url, timeout=self._fetch_timeout_seconds)
        except httpx.HTTPError as exc:
            raise A2AConnectionError(
                f"Failed to connect to A2A agent at '{url}': {exc}"
            ) from exc

        if response.status_code != 200:
            raise AgentCardFetchError(
                f"Agent Card fetch failed for '{url}': " f"HTTP {response.status_code}."
            )

        try:
            raw_card = response.json()
        except ValueError as exc:
            raise AgentCardValidationError(
                f"Agent Card at '{url}' is not valid JSON: {exc}"
            ) from exc

        return validate_agent_card(raw_card)

    async def send_message(self, endpoint_url: str, text: str) -> dict[str, Any]:
        """
        Send a real `message/send` JSON-RPC 2.0 request (A2A
        specification Section 7.1) to the agent's declared endpoint
        and return the raw `Message` or `Task` result.

        Protocol/transport failures and JSON-RPC error responses
        raise `A2ATaskError`. This method never fabricates a result
        when the agent reports failure.
        """
        request_id = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "message/send",
            "params": {
                "message": {
                    "kind": "message",
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": text}],
                }
            },
        }

        try:
            response = await self._client.post(
                endpoint_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self._task_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise A2AConnectionError(
                f"Failed to reach A2A agent endpoint '{endpoint_url}': {exc}"
            ) from exc

        if response.status_code != 200:
            raise A2ATaskError(
                f"A2A 'message/send' to '{endpoint_url}' failed with "
                f"HTTP {response.status_code}."
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise A2ATaskError(
                f"A2A 'message/send' response from '{endpoint_url}' is "
                f"not valid JSON: {exc}"
            ) from exc

        if body.get("id") != request_id:
            raise A2ATaskError(
                f"A2A 'message/send' response id mismatch for " f"'{endpoint_url}'."
            )

        error = body.get("error")
        if error is not None:
            raise A2ATaskError(
                f"A2A 'message/send' to '{endpoint_url}' returned error "
                f"{error.get('code')}: {error.get('message')}"
            )

        result = body.get("result")
        if not isinstance(result, dict):
            raise A2ATaskError(
                f"A2A 'message/send' to '{endpoint_url}' returned no " "usable result."
            )

        return result


async def resolve_a2a_agent(
    base_url: str,
    card_path: str = DEFAULT_AGENT_CARD_PATH,
) -> A2AResolutionResult:
    """
    Run the full Phase 03 resolution pipeline:

        fetch Agent Card -> validate -> normalize
    """
    async with A2AClient(base_url=base_url) as client:
        raw_card = await client.fetch_agent_card(card_path=card_path)

    endpoint = raw_card["url"]
    agent_metadata = normalize_agent_card(endpoint=endpoint, raw_card=raw_card)

    return A2AResolutionResult(
        endpoint=endpoint,
        protocol_version=raw_card.get("protocolVersion"),
        raw_agent_card=raw_card,
        agent=agent_metadata,
    )


async def send_task(
    base_url: str,
    text: str,
    card_path: str = DEFAULT_AGENT_CARD_PATH,
) -> dict[str, Any]:
    """
    Resolve a candidate A2A agent's Agent Card and send it one real
    `message/send` task, end to end:

        fetch Agent Card -> validate -> message/send -> agent response
    """
    async with A2AClient(base_url=base_url) as client:
        raw_card = await client.fetch_agent_card(card_path=card_path)
        endpoint = raw_card["url"]
        return await client.send_message(endpoint, text)
