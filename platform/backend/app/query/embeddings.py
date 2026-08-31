from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from app.models import Item


class LocalEmbeddingModel:
    """Small local embedding model for offline semantic retrieval.

    This uses a deterministic hashed bag-of-terms projection. It is deliberately
    local and dependency-free, so Gemini or another hosted model is never used
    for embeddings.
    """

    _token_pattern = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,}")

    def __init__(self, dimensions: int = 64) -> None:
        if dimensions < 8:
            raise ValueError("embedding dimensions must be at least 8")
        self.dimensions = dimensions

    def embed_text(self, text: str) -> list[float]:
        vector = [0.0 for _ in range(self.dimensions)]
        tokens = self._token_pattern.findall(text.lower())
        if not tokens:
            return vector

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [round(value / norm, 8) for value in vector]

    def embed_item(self, item: Item) -> list[float]:
        return self.embed_text(item_embedding_text(item))


def item_embedding_text(item: Item) -> str:
    parts: list[str] = [
        item.name,
        item.description,
        item.type.value,
        item.source.type.value,
        item.source.id,
        item.source.provider or "",
    ]

    if item.tool is not None:
        parts.extend(
            [
                item.tool.server_id,
                item.tool.tool_name,
                _flatten(item.tool.mcp_schema),
            ]
        )

    if item.agent is not None:
        parts.extend(
            [
                str(item.agent.endpoint),
                _flatten(item.agent.agent_card),
                " ".join(item.agent.skills),
                " ".join(item.agent.capabilities),
            ]
        )

    parts.extend(source.id for source in item.provenance)
    parts.extend(evidence.statement for evidence in item.evidence)
    return " ".join(part for part in parts if part)


def cosine_similarity(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right:
        return 0.0
    length = min(len(left), len(right))
    if length == 0:
        return 0.0
    dot = sum(left[index] * right[index] for index in range(length))
    left_norm = math.sqrt(sum(value * value for value in left[:length]))
    right_norm = math.sqrt(sum(value * value for value in right[:length]))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(
            f"{key} {_flatten(item)}" for key, item in sorted(value.items())
        )
    if isinstance(value, list):
        return " ".join(_flatten(item) for item in value)
    return str(value)
