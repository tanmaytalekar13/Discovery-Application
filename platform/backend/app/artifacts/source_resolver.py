"""
Builds the "Source" preview tab. Never fabricates content — if
item.artifacts.source_code is None, we say so explicitly.
"""

from __future__ import annotations

from app.models import Item


class SourcePreviewResult:
    def __init__(
        self,
        available: bool,
        language: str | None = None,
        content: str | None = None,
        note: str | None = None,
    ) -> None:
        self.available = available
        self.language = language
        self.content = content
        self.note = note


def _infer_language(item: Item) -> str:
    url = str(item.artifacts.source_url) if item.artifacts.source_url else ""
    for ext, lang in (
        (".py", "python"),
        (".ts", "typescript"),
        (".js", "javascript"),
        (".json", "json"),
        (".go", "go"),
    ):
        if url.endswith(ext):
            return lang
    # README / web-extracted docs text is almost always markdown-shaped
    return "markdown"


def resolve_source(item: Item) -> SourcePreviewResult:
    source_code = item.artifacts.source_code

    if not source_code:
        return SourcePreviewResult(
            available=False,
            note="Source code was not retrieved for this item — only registry "
                 "or configuration metadata is available.",
        )

    return SourcePreviewResult(
        available=True,
        language=_infer_language(item),
        content=source_code,
        note="This is the README / extracted documentation text captured during "
             "discovery, not a full repository checkout.",
    )