"""LLM-judge semantic validation (spec v2 Section 5.3).

Schema-valid is not the same as working. Given a tool's description,
schema, and the actual output, decide whether the response is a genuine
functional result or a stub/placeholder/disguised error - this catches
"200 OK but garbage" servers that pure schema validation misses.

The judge runs inside the async verification pipeline only. When no
LLM is configured (or the call fails), a deterministic heuristic
fallback keeps the pipeline moving; its verdicts are marked as such in
`verification_details`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_PLACEHOLDER_MARKERS = (
    "placeholder",
    "not implemented",
    "todo",
    "coming soon",
    "lorem ipsum",
    "example response",
    "stub",
    "mock response",
    "dummy",
    "sample data only",
    "hello world",
)
_ERROR_MARKERS = (
    "unauthorized",
    "forbidden",
    "invalid api key",
    "missing api key",
    "authentication required",
    "quota exceeded",
    "rate limit",
    "internal server error",
    "traceback (most recent call last)",
    "exception",
)


@dataclass
class SemanticVerdict:
    """One judge decision for one tool response."""

    tool_name: str
    valid: bool
    confidence: float  # 0..1
    reason: str
    judged_by: str  # "llm" | "heuristic"


def _extract_text(output: Any, depth: int = 0) -> str:
    """Flatten an MCP tool result to text for judging."""
    if depth > 6:
        return ""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, (int, float, bool)):
        return str(output)
    if isinstance(output, list):
        return "\n".join(_extract_text(item, depth + 1) for item in output[:20])
    if isinstance(output, dict):
        # MCP content blocks: prefer the text field of each block.
        if "text" in output and isinstance(output["text"], str):
            return output["text"]
        parts = []
        for key in ("content", "result", "data", "message", "output", "items"):
            if key in output:
                parts.append(_extract_text(output[key], depth + 1))
        if parts:
            return "\n".join(parts)
        try:
            return json.dumps(output, default=str)[:2000]
        except (TypeError, ValueError):
            return str(output)[:2000]
    return str(output)[:2000]


def heuristic_judge(
    tool_name: str,
    tool_description: str | None,
    output: Any,
    *,
    duration_ms: int | None = None,
) -> SemanticVerdict:
    """Deterministic fallback judge: stub/error markers + empty-output checks.

    Kept strict on purpose: it can only *fail* responses that are clearly
    garbage (placeholders, raw exceptions, empty payloads); everything
    else passes with medium confidence. False rejects are the exact
    failure mode (spec Section 12) this pipeline is built to avoid.
    """
    text = _extract_text(output).strip()
    lowered = text.lower()

    if not text:
        return SemanticVerdict(
            tool_name=tool_name,
            valid=False,
            confidence=0.9,
            reason="empty response payload",
            judged_by="heuristic",
        )

    for marker in _ERROR_MARKERS:
        if marker in lowered and len(lowered) < 2000:
            return SemanticVerdict(
                tool_name=tool_name,
                valid=False,
                confidence=0.8,
                reason=f"response looks like a disguised error: contains {marker!r}",
                judged_by="heuristic",
            )

    for marker in _PLACEHOLDER_MARKERS:
        if marker in lowered:
            return SemanticVerdict(
                tool_name=tool_name,
                valid=False,
                confidence=0.7,
                reason=f"response looks like a stub/placeholder: contains {marker!r}",
                judged_by="heuristic",
            )

    return SemanticVerdict(
        tool_name=tool_name,
        valid=True,
        confidence=0.5,
        reason="no placeholder/error markers found (heuristic)",
        judged_by="heuristic",
    )


class LLMSemanticJudge:
    """LLM-judge pass over tool responses, with heuristic fallback."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        *,
        timeout_s: float = 20.0,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s
        self._httpx_client = httpx_client

    async def judge(
        self,
        tool_name: str,
        tool_description: str | None,
        tool_schema: dict[str, Any] | None,
        output: Any,
        *,
        duration_ms: int | None = None,
    ) -> SemanticVerdict:
        if not self._api_key:
            return heuristic_judge(
                tool_name, tool_description, output, duration_ms=duration_ms
            )

        prompt = self._build_prompt(
            tool_name, tool_description, tool_schema, output, duration_ms
        )
        try:
            verdict_text = await self._call_llm(prompt)
            return self._parse_verdict(tool_name, verdict_text)
        except Exception as exc:  # noqa: BLE001 - judge failure must not fail verification
            logger.warning(
                "Semantic judge LLM call failed for tool %r (%s); using heuristic",
                tool_name,
                exc,
            )
            return heuristic_judge(
                tool_name, tool_description, output, duration_ms=duration_ms
            )

    def _build_prompt(
        self,
        tool_name: str,
        tool_description: str | None,
        tool_schema: dict[str, Any] | None,
        output: Any,
        duration_ms: int | None,
    ) -> str:
        output_text = _extract_text(output)[:4000]
        schema_text = ""
        if tool_schema:
            try:
                schema_text = json.dumps(tool_schema, default=str)[:1500]
            except (TypeError, ValueError):
                schema_text = ""
        return (
            "You are judging whether an MCP tool produced a genuine functional "
            "result or a useless/stub/placeholder/disguised-error response.\n"
            f"Tool name: {tool_name}\n"
            f"Tool description: {tool_description or '(none)'}\n"
            f"Input schema: {schema_text or '(none)'}\n"
            f"Call duration ms: {duration_ms or 'unknown'}\n"
            "Actual output (truncated):\n"
            f"{output_text}\n\n"
            "Answer with ONLY a JSON object: "
            '{"valid": true|false, "confidence": 0.0-1.0, "reason": "short explanation"}'
        )

    async def _call_llm(self, prompt: str) -> str:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._model}:generateContent"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        if self._httpx_client is not None:
            response = await self._httpx_client.post(
                url, params={"key": self._api_key}, json=payload,
                timeout=self._timeout_s,
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.post(
                    url, params={"key": self._api_key}, json=payload
                )
        response.raise_for_status()
        body = response.json()
        candidates = body.get("candidates") or []
        if not candidates:
            raise ValueError("judge response had no candidates")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
        if not text.strip():
            raise ValueError("judge response had no text")
        return text

    @staticmethod
    def _parse_verdict(tool_name: str, text: str) -> SemanticVerdict:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"judge returned non-JSON: {text[:200]}") from exc
        valid = bool(data.get("valid"))
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        reason = str(data.get("reason") or "")[:300]
        return SemanticVerdict(
            tool_name=tool_name, valid=valid, confidence=confidence,
            reason=reason, judged_by="llm",
        )
