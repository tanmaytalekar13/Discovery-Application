from __future__ import annotations

import json
import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import Settings

PreferredType = Literal["tool", "agent", "all"]


class QueryPlan(BaseModel):
    """Strict Phase 11 query-understanding contract."""

    keywords: list[str] = Field(default_factory=list)
    preferred_type: PreferredType = "all"
    expanded_query: str
    source_hints: list[str] = Field(default_factory=list)
    used_fallback: bool = False

    @field_validator("keywords", "source_hints")
    @classmethod
    def _clean_short_strings(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = str(value).strip().lower()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                result.append(cleaned)
        return result[:20]

    @field_validator("expanded_query")
    @classmethod
    def _expanded_query_must_be_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("expanded_query must not be empty")
        return cleaned


class FallbackQueryPlanner:
    """Deterministic planner used when Gemini is unavailable or invalid."""

    _token_pattern = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}")
    _stopwords = {
        "and",
        "are",
        "for",
        "from",
        "into",
        "need",
        "that",
        "the",
        "this",
        "with",
        "tool",
        "tools",
        "agent",
        "agents",
    }

    def plan(
        self, raw_query: str, preferred_type: PreferredType | None = None
    ) -> QueryPlan:
        query = raw_query.strip()
        if not query:
            raise ValueError("query must not be empty")

        lowered = query.lower()
        inferred_type: PreferredType = "all"
        if any(term in lowered for term in ("tool", "mcp", "server")):
            inferred_type = "tool"
        if any(term in lowered for term in ("agent", "a2a", "assistant")):
            inferred_type = "agent" if inferred_type == "all" else "all"

        keywords = [
            token
            for token in self._token_pattern.findall(lowered)
            if token not in self._stopwords
        ]

        hints: list[str] = []
        if "github" in lowered:
            hints.append("github")
        if "registry" in lowered:
            hints.append("registry")
        if "well-known" in lowered or "agent-card" in lowered:
            hints.append("well_known")

        return QueryPlan(
            keywords=keywords,
            preferred_type=preferred_type or inferred_type,
            expanded_query=query,
            source_hints=hints,
            used_fallback=True,
        )


class GeminiQueryPlanner:
    """Gemini planner for query understanding only; output is strictly validated."""

    def __init__(
        self,
        settings: Settings,
        *,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._httpx_client = httpx_client

    async def plan(self, raw_query: str) -> QueryPlan:
        if not self._settings.gemini_api_key:
            raise RuntimeError("Gemini planner is not configured")

        query = raw_query.strip()
        if not query:
            raise ValueError("query must not be empty")

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._settings.gemini_model}:generateContent"
        )
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                "Return only JSON with keys keywords, preferred_type, "
                                "expanded_query, source_hints. preferred_type must be "
                                "tool, agent, or all. Interpret this discovery query: "
                                f"{query}"
                            )
                        }
                    ],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0,
            },
        }

        if self._httpx_client is not None:
            response = await self._httpx_client.post(
                url,
                params={"key": self._settings.gemini_api_key},
                json=payload,
                timeout=self._settings.query_planner_timeout_seconds,
            )
        else:
            async with httpx.AsyncClient(
                timeout=self._settings.query_planner_timeout_seconds
            ) as client:
                response = await client.post(
                    url,
                    params={"key": self._settings.gemini_api_key},
                    json=payload,
                )

        response.raise_for_status()
        return _parse_gemini_plan(response.json())


async def plan_query(
    raw_query: str,
    settings: Settings,
    *,
    preferred_type: PreferredType | None = None,
    httpx_client: httpx.AsyncClient | None = None,
) -> QueryPlan:
    fallback = FallbackQueryPlanner()
    if not settings.gemini_api_key:
        return fallback.plan(raw_query, preferred_type=preferred_type)

    try:
        plan = await GeminiQueryPlanner(settings, httpx_client=httpx_client).plan(
            raw_query
        )
    except Exception:
        return fallback.plan(raw_query, preferred_type=preferred_type)

    if preferred_type is not None:
        plan.preferred_type = preferred_type
    return plan


def _parse_gemini_plan(payload: dict[str, Any]) -> QueryPlan:
    text = _extract_text(payload)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Gemini planner returned non-JSON output") from exc

    try:
        return QueryPlan.model_validate(data)
    except ValidationError as exc:
        raise ValueError("Gemini planner returned an invalid query plan") from exc


def _extract_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Gemini planner response contained no candidates")

    content = candidates[0].get("content", {})
    parts = content.get("parts", [])
    if not isinstance(parts, list) or not parts:
        raise ValueError("Gemini planner response contained no content parts")

    text = parts[0].get("text")
    if not isinstance(text, str):
        raise ValueError("Gemini planner response part was not text")
    return text
