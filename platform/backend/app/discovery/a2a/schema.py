"""
Validation/normalization of a real A2A Agent Card into the platform's
normalized catalog model (`app.models.AgentMetadata`).

Per CODEX_EXECUTION_PLAN.md Section 10 (A2A Resolution):
    "Agent Card is metadata, not executable code."

This module only ever normalizes a card that was actually fetched
from a real Agent Card endpoint (direct URL or
`/.well-known/agent-card.json`) - never fabricated from documentation
or guessed defaults. Field names below follow Section 5.5 (`AgentCard`
Object Structure) of the A2A specification.
"""

from __future__ import annotations

from typing import Any

from app.discovery.a2a.errors import AgentCardValidationError
from app.models import AgentMetadata

# The subset of AgentCard fields (A2A spec Section 5.5) the platform
# depends on for resolution/normalization. `preferredTransport` is
# intentionally not required here: agents that omit it default to
# JSONRPC per the spec, and Phase 03 only speaks JSON-RPC.
REQUIRED_FIELDS = (
    "protocolVersion",
    "name",
    "description",
    "url",
    "version",
    "capabilities",
    "defaultInputModes",
    "defaultOutputModes",
    "skills",
)


def validate_agent_card(raw_card: Any) -> dict[str, Any]:
    """
    Validate that a fetched Agent Card is a well-formed JSON object
    per Section 5.5 of the A2A specification.

    This is a structural check for the fields the platform depends
    on, not a full schema validation against the entire AgentCard
    grammar (signatures, security schemes, etc are left as-is).
    """
    if not isinstance(raw_card, dict):
        raise AgentCardValidationError(
            f"Agent Card must be a JSON object, got {type(raw_card).__name__}."
        )

    missing = [name for name in REQUIRED_FIELDS if name not in raw_card]
    if missing:
        raise AgentCardValidationError(
            f"Agent Card is missing required field(s): {', '.join(missing)}."
        )

    if not isinstance(raw_card["name"], str) or not raw_card["name"]:
        raise AgentCardValidationError("Agent Card 'name' must be a non-empty string.")

    if not isinstance(raw_card["url"], str) or not raw_card["url"]:
        raise AgentCardValidationError("Agent Card 'url' must be a non-empty string.")

    if not isinstance(raw_card["capabilities"], dict):
        raise AgentCardValidationError(
            "Agent Card 'capabilities' must be a JSON object."
        )

    if not isinstance(raw_card["defaultInputModes"], list):
        raise AgentCardValidationError(
            "Agent Card 'defaultInputModes' must be a JSON array."
        )

    if not isinstance(raw_card["defaultOutputModes"], list):
        raise AgentCardValidationError(
            "Agent Card 'defaultOutputModes' must be a JSON array."
        )

    if not isinstance(raw_card["skills"], list):
        raise AgentCardValidationError("Agent Card 'skills' must be a JSON array.")

    for skill in raw_card["skills"]:
        if not isinstance(skill, dict) or not skill.get("id"):
            raise AgentCardValidationError(
                "Every Agent Card skill must be an object with a " "non-empty 'id'."
            )

    return raw_card


def normalize_agent_card(endpoint: str, raw_card: dict[str, Any]) -> AgentMetadata:
    """
    Convert a validated Agent Card into the platform's normalized
    `AgentMetadata` record.

    `capabilities` is normalized to the list of capability names the
    agent declared as enabled (e.g. ["streaming"]). `skills` is
    normalized to the list of declared `AgentSkill.id` values.
    `declared_dependencies` (agent -> MCP allowlist, Section 27) is
    left empty here - Phase 03 only resolves the Agent Card itself
    and must not invent dependency declarations the card didn't make.
    """
    capabilities = [
        name
        for name, enabled in raw_card.get("capabilities", {}).items()
        if enabled is True
    ]
    skills = [skill["id"] for skill in raw_card.get("skills", [])]

    return AgentMetadata(
        endpoint=endpoint,
        agent_card=raw_card,
        skills=skills,
        capabilities=capabilities,
        declared_dependencies=[],
    )
