"""
Exceptions raised while resolving/validating an A2A candidate.

Per CODEX_EXECUTION_PLAN.md (Section 10 - A2A Resolution, Section 39 -
Phase 03 Definition of Done): every stage of talking to a candidate A2A
agent can fail independently, and a failure at any stage must be
reported explicitly rather than silently downgraded into a "success".
Never fabricate an Agent Card or a task result when a stage fails.
"""

from __future__ import annotations


class A2AResolutionError(Exception):
    """Base class for all A2A protocol resolution failures."""


class A2AConnectionError(A2AResolutionError):
    """
    Raised when a transport-level connection to the candidate A2A
    agent could not be established (endpoint unreachable, DNS
    failure, TLS failure, connection refused, etc).
    """


class AgentCardFetchError(A2AResolutionError):
    """
    Raised when fetching the Agent Card fails at the HTTP level (for
    example, a non-200 response from the Agent Card URL).
    """


class AgentCardValidationError(A2AResolutionError):
    """
    Raised when a fetched Agent Card is missing required fields or is
    not well-formed JSON. Per rule #11 in the plan, a trusted Agent
    Card must never be fabricated - if the served card is invalid or
    incomplete, the agent must be rejected rather than "fixed up".
    """


class A2ATaskError(A2AResolutionError):
    """
    Raised when an A2A `message/send` task fails at the
    protocol/transport level, times out, or the agent returns a
    JSON-RPC error response.
    """
